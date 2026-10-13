"""Clone this PC to a new drive: copy the running Windows system disk, while Windows is in use, onto another drive
so that drive starts Windows on its own.

The order of work is chosen so it can't fail silently and fails before writing whenever it can:

1. preflight(): every check that can block (rights, power, VSS, disk type, BitLocker, target, sector size,
   capacity, snapshot storage) runs first and says why and how to fix it. Nothing is written.
2. CloneJob.run(): snapshot of every NTFS volume on the source disk, used-space map read from the snapshot, the
   plan re-checked against it (still nothing written), then the target is marked unusable, data copied (hashing
   each chunk), every chunk re-read from the target and compared, the partition table written last, structure
   checks, and the boot fix-up (bcdboot, BCD check, MountedDevices, extend).
3. A cancel or failure after the first write leaves the target marked unusable (first and last MiB zeroed and a
   marker written), never half bootable. The snapshot is always released. A crash leaves a journal in app_dir()
   so the clone resumes from its last verified chunk, or the snapshot is cleaned up at the next start.

Everything OS specific goes through a SystemLayer (sysclone_win.WindowsLayer on Windows, a fake in the tests).
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import struct
import threading
import time
import uuid
import zlib
from dataclasses import asdict, dataclass, field

from . import partitions, usedmap
from . import verify as V
from .device import Device
from .util import Cancelled, Progress, app_dir, get_logger, human_size, is_windows

log = get_logger()

MiB = 1024 * 1024
GiB = 1024 * MiB
CHUNK = 4 * MiB
MARK_OFFSET = 512 * 1024
MARK_MAGIC = b"SECTORSMITH-UNFINISHED-CLONE"
MIN_FREE = 4 * GiB        # free space the Windows partition must keep on the target after a shrink
SHADOW_MIN = 2 * GiB      # free space a snapshotted volume needs for the copy-on-write data
BATTERY_MIN = 30          # percent, when not on mains power
MAX_BAD_SECTORS = 4096    # beyond this the source drive is failing: stop instead of copying zeros
JOURNAL_EVERY = 32        # chunks between journal checkpoints
STALE_HOURS = 48          # an interrupted clone older than this is cleaned up instead of offered for resume

GUID_ESP = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
GUID_MSR = "e3c9e316-0b5c-4db8-817d-f92df00215ae"
GUID_BASIC = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
GUID_RECOVERY = "de94bba4-06d1-4d40-a16a-bfd50179d6ac"
GUID_LDM = ("5808c8aa-7e8f-42e0-85d2-e1e90434cfb3", "af9b60a0-1431-4f62-bc68-3311714a69ad")
GUID_SPACES = "e75caf8f-f680-4cee-afa3-b001e56efc2d"

PHASES = ("snapshot", "copy", "verify", "boot")
PHASE_LABELS = {"snapshot": "Taking a snapshot", "copy": "Copying", "verify": "Verifying",
                "boot": "Making the new drive start Windows"}

SAMPLE_FILES = ("Windows\\System32\\config\\SYSTEM", "Windows\\System32\\config\\SOFTWARE",
                "Windows\\System32\\config\\DEFAULT", "Windows\\System32\\ntoskrnl.exe",
                "Windows\\System32\\hal.dll", "Windows\\System32\\winload.efi", "Windows\\System32\\winload.exe",
                "Windows\\System32\\drivers\\ntfs.sys", "Windows\\explorer.exe")
REQUIRED_SAMPLE = "Windows\\System32\\config\\SYSTEM"


class SysCloneError(Exception):
    """Stops the clone with a plain reason and what to do about it."""

    def __init__(self, reason: str, fix: str = ""):
        super().__init__(reason)
        self.reason, self.fix = reason, fix


def phase_of(label: str) -> str | None:
    for k, v in PHASE_LABELS.items():
        if label.startswith(v):
            return k
    return None


# ---------------------------------------------------------------------------------------------- system layer
class SystemLayer:
    """What the clone needs from the operating system. sysclone_win.WindowsLayer is the real one."""

    name = "none"

    def is_admin(self) -> bool:
        return False

    def power(self) -> dict:
        """{"ac": True/False/None, "percent": 0-100 or None}"""
        return {"ac": None, "percent": None}

    def keep_awake(self, on: bool) -> bool:
        return False

    def vss_status(self) -> tuple[bool, str]:
        return False, "Volume Shadow Copy is only available on Windows."

    def system_disk(self, devices):
        return next((d for d in devices if d.is_system), None)

    def disk_flags(self, dev) -> dict:
        """{"dynamic": bool, "storage_spaces": bool, "raid_volume": bool, "detail": str}"""
        return {}

    def volumes(self, dev) -> list[dict]:
        """Mounted volumes on dev: {"offset", "length", "letters", "guid", "windows": bool,
        "bitlocker": "none"|"unlocked"|"locked"|"converting", "free": bytes or None, "size": bytes or None}"""
        return []

    def target_usage(self, dev) -> dict:
        """{"mounted": [names], "busy": [names], "pagefile": [names]} for the drive about to be overwritten."""
        return {"mounted": [], "busy": [], "pagefile": []}

    def create_snapshot(self, dev, volumes, on_id) -> "Snapshot":
        raise SysCloneError("Snapshots are not available here.")

    def attach_snapshot(self, dev, info) -> "Snapshot | None":
        return None

    def delete_snapshot(self, info) -> None:
        pass

    def mount_target(self, dst, plan: "Plan") -> dict:
        """Temporary drive letters for the target's boot (ESP or active) and Windows partitions."""
        raise SysCloneError("The boot fix-up needs Windows.")

    def open_source_file(self, snap, part: "PartPlan", relpath: str):
        return None

    def open_target_file(self, ctx: dict, part: "PartPlan", relpath: str):
        return None

    def bcdboot(self, ctx: dict, plan: "Plan") -> tuple[bool, str]:
        return False, "not available"

    def read_bcd(self, ctx: dict, plan: "Plan") -> list[dict]:
        """BCD objects: [{"kind": "bootmgr"|"osloader"|..., "elements": {"11000001": bytes, ...}}]"""
        return []

    def patch_registry(self, ctx: dict, plan: "Plan", replacements, bitlocker: bool) -> tuple[bool, list[str]]:
        return False, ["not available"]

    def extend_filesystem(self, ctx: dict, plan: "Plan") -> tuple[bool, str]:
        return False, "not available"

    def unmount_target(self, ctx: dict) -> None:
        pass


class Snapshot:
    """A set of shadow copies. overlay: [(disk byte offset, length, reader Device)] for Device._overlay."""

    def __init__(self, info: dict, overlay=None, volumes=None, notes=None, release=None, alive=None):
        self.info = info
        self.overlay = overlay or []
        self.volumes = volumes or []
        self.notes = notes or []
        self._release = release
        self._alive = alive
        self.released = False

    def covers(self, offset: int) -> bool:
        return any(o <= offset < o + n for o, n, _r in self.overlay)

    def alive(self) -> bool:
        return True if self._alive is None else bool(self._alive())

    def release(self):
        if self.released:
            return
        self.released = True
        for _o, _n, r in self.overlay:
            try:
                r.close()
            except OSError:
                pass
        if self._release:
            self._release()


_LAYER: list = [None]


def set_layer(layer):
    """Use this layer instead of the platform's (tests and the UI smoke test pass a fake)."""
    _LAYER[0] = layer


def get_layer() -> SystemLayer | None:
    if _LAYER[0] is not None:
        return _LAYER[0]
    if is_windows():
        from .sysclone_win import WindowsLayer
        _LAYER[0] = WindowsLayer()
        return _LAYER[0]
    return None


# ---------------------------------------------------------------------------------------------- source layout
@dataclass
class SrcPart:
    slot: int                 # GPT entry index (0-based) or MBR slot
    start_lba: int
    sectors: int
    type_id: str              # GPT type GUID or "0x07" style MBR type
    unique: str = ""          # GPT partition GUID
    attrs: int = 0
    name: str = ""
    bootable: bool = False
    fs: str = ""
    role: str = "other"       # esp, msr, windows, recovery, data, other
    letters: list = field(default_factory=list)
    bitlocker: str = "none"

    def label(self) -> str:
        names = {"esp": "EFI System", "msr": "Microsoft Reserved", "windows": "Windows",
                 "recovery": "Recovery", "data": "Data", "other": "Other"}
        let = ", ".join(x.rstrip("\\") for x in self.letters)
        return f"{names.get(self.role, self.role)}{' (' + let + ')' if let else ''}"


@dataclass
class SrcLayout:
    scheme: str
    sector_size: int
    total_sectors: int
    disk_id: str              # GPT disk GUID or MBR signature as 8 hex digits
    parts: list
    boot_code: bytes = b""
    problems: list = field(default_factory=list)   # (reason, fix) that block a clone


def _guid(b: bytes) -> str:
    return str(uuid.UUID(bytes_le=bytes(b)))


def read_layout(dev: Device, volumes=None) -> SrcLayout:
    """The source disk's partitions with roles. ``volumes`` (from the layer) gives letters, BitLocker state and
    which volume Windows runs from."""
    ss = dev.sector_size
    mbr = dev.read_sectors(0, 1)
    vols = {v["offset"]: v for v in (volumes or [])}
    lay = SrcLayout("None", ss, dev.total_sectors, "", [])
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        lay.problems.append(("The source disk has no partition table.", ""))
        return lay
    entries = list(partitions.parse_mbr_entries(mbr))
    if any(e[2] == 0xEE for e in entries):
        hdr = partitions.read_gpt_header(dev, 1)
        if hdr is None or not hdr["crc_ok"]:
            lay.problems.append(("The source disk's GPT header is damaged.",
                                 "Repair the partition table first (Find lost partitions)."))
            return lay
        table = dev.read(hdr["entries_lba"] * ss, hdr["n_entries"] * hdr["entry_size"])
        if zlib.crc32(table) != hdr["entries_crc"]:
            lay.problems.append(("The source disk's GPT partition list fails its checksum.",
                                 "Repair the partition table first."))
            return lay
        lay.scheme, lay.disk_id = "GPT", hdr["disk_guid"]
        for i in range(hdr["n_entries"]):
            e = table[i * hdr["entry_size"]:(i + 1) * hdr["entry_size"]]
            if len(e) < 128 or e[:16] == bytes(16):
                continue
            first, last, attrs = struct.unpack_from("<QQQ", e, 32)
            name = e[56:128].decode("utf-16-le", "ignore").split("\0")[0]
            lay.parts.append(SrcPart(i, first, last - first + 1, _guid(e[:16]), _guid(e[16:32]), attrs, name))
    else:
        lay.scheme = "MBR"
        lay.disk_id = f"{struct.unpack_from('<I', mbr, 440)[0]:08X}"
        lay.boot_code = bytes(mbr[:440])
        for i, status, ptype, start, count in entries:
            if ptype == 0 or count == 0:
                continue
            lay.parts.append(SrcPart(i, start, count, f"0x{ptype:02X}", bootable=status == 0x80))
    lay.parts.sort(key=lambda p: p.start_lba)
    for p in lay.parts:
        try:
            p.fs = partitions.detect_fs(dev.read(p.start_lba * ss, 4096))[0]
        except OSError:
            p.fs = ""
        v = vols.get(p.start_lba * ss)
        if v:
            p.letters = list(v.get("letters") or [])
            p.bitlocker = v.get("bitlocker") or "none"
        p.role = _role(lay.scheme, p, v)
    return lay


def _role(scheme, p: SrcPart, vol) -> str:
    t = p.type_id.lower()
    if scheme == "GPT":
        if t == GUID_ESP:
            return "esp"
        if t == GUID_MSR:
            return "msr"
        if t == GUID_RECOVERY:
            return "recovery"
        if t in GUID_LDM:
            return "dynamic"
        if t == GUID_SPACES:
            return "spaces"
        if t == GUID_BASIC:
            return "windows" if vol and vol.get("windows") else "data"
        return "other"
    if t == "0x27":
        return "recovery"
    if t == "0x42":
        return "dynamic"
    if t in ("0x05", "0x0F", "0x85"):
        return "extended"
    if t in ("0x07", "0x17"):
        return "windows" if vol and vol.get("windows") else "data"
    return "other"


# ---------------------------------------------------------------------------------------------- plan
@dataclass
class PartPlan:
    src_slot: int
    role: str
    fs: str
    type_id: str
    src_start: int            # source LBA
    src_sectors: int
    start: int                # target LBA
    sectors: int
    action: str               # used (NTFS used space), raw (block for block), zero (rebuilt empty)
    resize: str = ""          # "", grow, shrink
    unique: str = ""          # new GPT partition GUID
    src_unique: str = ""
    attrs: int = 0
    name: str = ""
    bootable: bool = False
    letters: list = field(default_factory=list)

    @property
    def src_bytes(self) -> int:
        return self.src_sectors

    def describe(self, ss) -> str:
        size = human_size(self.sectors * ss)
        what = {"used": "used space copied from the snapshot", "raw": "copied block for block",
                "zero": "rebuilt empty"}[self.action]
        if self.resize == "grow":
            what += f", grown from {human_size(self.src_sectors * ss)}"
        elif self.resize == "shrink":
            what += f", shrunk from {human_size(self.src_sectors * ss)}"
        return f"{size}, {what}"


@dataclass
class Plan:
    scheme: str
    sector_size: int
    target_sectors: int
    disk_id: str              # new GPT disk GUID or MBR signature hex
    src_disk_id: str
    parts: list
    boot_code_hex: str = ""
    firmware: str = "UEFI"
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        d = dict(d)
        d["parts"] = [PartPlan(**p) for p in d["parts"]]
        return cls(**d)

    def part(self, role: str) -> PartPlan | None:
        return next((p for p in self.parts if p.role == role), None)

    def boot_part(self) -> PartPlan | None:
        """The partition bcdboot writes the boot files to: the ESP, or for MBR the active one."""
        if self.scheme == "GPT":
            return self.part("esp")
        return next((p for p in self.parts if p.bootable), None) or self.part("windows")


def _up(n, a):
    return -(-n // a) * a


def make_plan(lay: SrcLayout, target_sectors: int, target_ss: int, used: dict | None = None,
              min_free: int = MIN_FREE) -> tuple[Plan | None, list]:
    """Target layout: same partitions, types and order, starts on 1 MiB boundaries, the last MiB left free, and the
    Windows partition grown to fill the target or shrunk to fit. ``used``: {"extent": bytes from the start of the
    Windows partition to the end of its last used cluster, "bytes": bytes in use} (None when not known yet).
    Returns (plan, problems); problems are (reason, fix) and a plan is None when there are any."""
    ss = lay.sector_size
    probs = []
    if target_ss != ss:
        return None, [(f"The source uses {ss}-byte sectors and the target {target_ss}-byte sectors.",
                       "Windows can't use NTFS or the EFI partition formatted for one sector size on a drive with "
                       "the other. Use a target drive with " + ("512-byte (512e or 512n)" if ss == 512 else
                                                                f"{ss}-byte") + " sectors.")]
    total_bytes = target_sectors * ss
    if lay.scheme == "GPT":
        end = (target_sectors - 1 - -(-128 * 128 // ss)) * ss   # first byte past the last usable LBA
    else:
        end = min(total_bytes, 0xFFFFFFFF * ss)                  # MBR addresses at most 2^32 sectors
    parts = list(lay.parts)
    win = next((p for p in parts if p.role == "windows"), None)
    if win is None:
        return None, [("Couldn't find the Windows partition on the source disk.", "")]

    def layout(wsize):
        """[(start, size)] in bytes with Windows at wsize; each partition starts on a 1 MiB boundary."""
        out, cursor = [], MiB
        for p in parts:
            start = _up(cursor, MiB)
            size = wsize if p is win else p.sectors * ss
            out.append((start, size))
            cursor = start + size
        return out, cursor
    src_win = win.sectors * ss
    _l0, end0 = layout(0)
    avail = end - end0               # with Windows sized in whole MiB everything after it shifts by exactly that
    avail -= avail % MiB
    if layout(src_win)[1] <= end:
        new_win = max(avail, src_win)
        resize = "grow" if new_win > src_win else ""
    else:
        if win.fs not in ("NTFS", "BitLocker"):
            probs.append((f"The target is {human_size(src_win - avail)} too small and the Windows partition "
                          f"({win.fs or 'unknown'}) can't be shrunk.", "Use a target at least as big as the source "
                          "disk."))
        elif used is not None and used["bytes"] + min_free > avail:
            short = used["bytes"] + min_free - avail
            probs.append((f"The target is too small: Windows uses {human_size(used['bytes'])} and should keep "
                          f"{human_size(min_free)} free, which is {human_size(short)} more than fits.",
                          f"Use a target of at least {human_size(total_bytes + short)}, or free up space on C: "
                          "first."))
        elif used is not None and _up(used["extent"] + ss, MiB) > avail:
            probs.append((f"Files on C: reach {human_size(used['extent'])} into the partition, past the "
                          f"{human_size(avail)} that fits on the target.",
                          "Shrink C: on this PC first (Disk Management, right-click C:, Shrink Volume) so Windows "
                          "moves files toward the start, then try again."))
        new_win = avail
        resize = "shrink"
    if probs:
        return None, probs
    if new_win <= 0:
        return None, [("The partitions don't fit on the target.", "Use a bigger target drive.")]
    spans, cursor = layout(new_win)
    if cursor > end:
        return None, [("The partitions don't fit on the target.", "Use a bigger target drive.")]
    pps = []
    for p, (start, size) in zip(parts, spans):
        action = "zero" if p.role == "msr" else ("used" if p.fs in ("NTFS", "BitLocker") and p.role in (
            "windows", "data", "recovery") else "raw")
        pps.append(PartPlan(p.slot, p.role, p.fs, p.type_id, p.start_lba, p.sectors, start // ss, size // ss, action,
                            resize if p is win else "", str(uuid.uuid4()) if lay.scheme == "GPT" else "", p.unique,
                            p.attrs, p.name, p.bootable, list(p.letters)))
    if lay.scheme == "MBR" and not any(p.bootable for p in pps):
        next(p for p in pps if p.role == "windows").bootable = True
    disk_id = str(uuid.uuid4()) if lay.scheme == "GPT" else f"{struct.unpack('<I', _mbr_sig())[0]:08X}"
    plan = Plan(lay.scheme, ss, target_sectors, disk_id, lay.disk_id, pps, lay.boot_code.hex(),
                "UEFI" if lay.scheme == "GPT" else "BIOS")
    if lay.scheme == "MBR" and total_bytes > end + MiB:
        plan.notes.append(f"MBR can address only {human_size(end)}; the rest of the target stays unused.")
    return plan, []


def _mbr_sig() -> bytes:
    while True:
        b = os.urandom(4)
        if b != bytes(4):
            return b


# ---------------------------------------------------------------------------------------------- preflight
@dataclass
class Check:
    key: str
    title: str
    status: str = "ok"        # ok, warn, block
    detail: str = ""
    fix: str = ""


@dataclass
class Preflight:
    checks: list
    plan: Plan | None
    layout: SrcLayout | None
    volumes: list = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return any(c.status == "block" for c in self.checks)

    def blocking(self) -> list:
        return [c for c in self.checks if c.status == "block"]


PREFLIGHT_ITEMS = (("admin", "Administrator rights"), ("power", "Power"), ("vss", "Volume Shadow Copy"),
                   ("source", "Source disk"), ("bitlocker", "BitLocker"), ("target", "Target drive"),
                   ("sectors", "Sector size"), ("space", "Space on the target"), ("shadow", "Snapshot storage"),
                   ("sleep", "Sleep"))


def preflight(src: Device, dst: Device | None, layer: SystemLayer, take_offline: bool = False,
              min_free: int = MIN_FREE, shadow_min: int = SHADOW_MIN, on_check=None) -> Preflight:
    """Every reason the clone could not succeed, before anything is written. ``on_check(check)`` is called as each
    item finishes (the wizard ticks them off one by one). A check that itself errors blocks (fail closed)."""
    checks = []
    st = {"layout": None, "plan": None, "vols": []}

    def add(key, fn):
        title = dict(PREFLIGHT_ITEMS)[key]
        c = Check(key, title)
        try:
            fn(c)
        except Exception as e:  # noqa: BLE001  a check that can't run must not pass
            log.warning("sysclone preflight %s failed: %s", key, e)
            c.status, c.detail, c.fix = "block", f"Couldn't check this: {e}", "Try again, or restart the PC."
        checks.append(c)
        if on_check:
            on_check(c)

    def admin(c):
        if not layer.is_admin():
            c.status, c.detail, c.fix = "block", "SectorSmith isn't running as administrator.", \
                "Close it and start it again with Run as administrator."
        else:
            c.detail = "Running as administrator."

    def power(c):
        p = layer.power()
        if p.get("ac") is False and p.get("percent") is not None and p["percent"] < BATTERY_MIN:
            c.status, c.detail, c.fix = "block", f"On battery at {p['percent']}%.", \
                f"Plug in the charger (or charge above {BATTERY_MIN}%)."
        elif p.get("ac") is False:
            c.status, c.detail = "warn", f"On battery at {p.get('percent')}%. Plugging in is safer."
        else:
            c.detail = "On mains power." if p.get("ac") else "No battery reported."

    def vss(c):
        ok, msg = layer.vss_status()
        if not ok:
            c.status, c.detail, c.fix = "block", msg or "The Volume Shadow Copy service isn't available.", \
                "Open Services, set Volume Shadow Copy to Manual and start it, then check again."
        else:
            c.detail = msg or "Running."

    def source(c):
        st["vols"] = layer.volumes(src)
        lay = read_layout(src, st["vols"])
        st["layout"] = lay
        flags = layer.disk_flags(src) or {}
        reasons = list(lay.problems)
        if flags.get("dynamic") or any(p.role == "dynamic" for p in lay.parts):
            reasons.append(("The system disk is a dynamic disk.", "Dynamic disks can't be cloned this way. Convert "
                            "it to a basic disk with a partition tool first."))
        if flags.get("storage_spaces") or src.bus == "Storage Spaces" or any(p.role == "spaces" for p in lay.parts):
            reasons.append(("Windows runs from Storage Spaces.", "Storage Spaces pools can't be cloned to a single "
                            "drive. Use a backup and restore instead."))
        if flags.get("raid_volume"):
            reasons.append(("Windows runs from a RAID volume (" + (flags.get("detail") or "RAID") + ").",
                            "A RAID volume can't be cloned to a single drive this way. Use the RAID controller's "
                            "own tools, or a backup and restore."))
        if any(p.role == "extended" for p in lay.parts):
            reasons.append(("The system disk uses an extended partition with logical drives.",
                            "Only primary partitions are supported. Move the data off the logical drives first."))
        if lay.scheme in ("GPT", "MBR") and not any(p.role == "windows" for p in lay.parts):
            reasons.append(("Couldn't find the partition Windows runs from on this disk.", ""))
        if lay.scheme == "GPT" and not any(p.role == "esp" for p in lay.parts):
            reasons.append(("The system disk has no EFI System partition: this PC starts from another disk.",
                            "The clone would not start on its own. Clone the disk that holds the EFI partition, "
                            "or create one first."))
        if reasons:
            c.status, c.detail, c.fix = "block", reasons[0][0], reasons[0][1]
            if len(reasons) > 1:
                c.detail += " Also: " + " ".join(r[0] for r in reasons[1:])
        else:
            kinds = ", ".join(p.label() for p in lay.parts)
            c.detail = f"{lay.scheme} ({'UEFI' if lay.scheme == 'GPT' else 'BIOS'}), {len(lay.parts)} partitions: " \
                       f"{kinds}."

    def bitlocker(c):
        lay = st["layout"]
        if lay is None:
            c.status, c.detail = "block", "Source not readable."
            return
        locked = [p.label() for p in lay.parts if p.bitlocker == "locked"]
        conv = [p.label() for p in lay.parts if p.bitlocker == "converting"]
        unlocked = [p.label() for p in lay.parts if p.bitlocker == "unlocked"]
        if locked:
            c.status, c.detail, c.fix = "block", f"Locked by BitLocker: {', '.join(locked)}.", \
                "Unlock the volume (or suspend protection) and check again."
        elif conv:
            c.status, c.detail, c.fix = "block", f"BitLocker is still encrypting or decrypting {', '.join(conv)}.", \
                "Wait until BitLocker finishes (Control Panel, BitLocker Drive Encryption), then try again."
        elif unlocked:
            c.status, c.detail = "warn", (f"{', '.join(unlocked)} use BitLocker. They are copied unencrypted, so the "
                                          "new drive will not be encrypted. You can turn BitLocker back on at its "
                                          "first start.")
        else:
            c.detail = "No BitLocker volumes."

    def target(c):
        if dst is None:
            c.status, c.detail = "block", "No target drive picked."
            return
        if dst.path == src.path or (dst.disk_number is not None and dst.disk_number == src.disk_number):
            c.status, c.detail, c.fix = "block", "The target is the disk Windows runs from.", "Pick another drive."
            return
        if dst.is_system:
            c.status, c.detail, c.fix = "block", "The target is the system or boot disk.", "Pick another drive."
            return
        use = layer.target_usage(dst) or {}
        if use.get("pagefile"):
            c.status, c.detail, c.fix = "block", f"Windows keeps its page file on {', '.join(use['pagefile'])}.", \
                "Move the page file off this drive (System, Advanced, Performance, Virtual memory) and restart."
            return
        mounted = use.get("mounted") or []
        busy = use.get("busy") or []
        if (mounted or busy) and not take_offline:
            c.status = "block"
            c.detail = (f"{', '.join(busy)} on the target {'is' if len(busy) == 1 else 'are'} in use. " if busy else
                        "") + (f"Volumes on the target: {', '.join(mounted)}." if mounted else "")
            c.fix = "Close anything using them, or tick 'Take the target's volumes offline'. Everything on the " \
                    "target will be replaced."
            return
        what = f"{dst.name}, {dst.model or 'drive'}, {human_size(dst.size)}"
        c.detail = what + (f". Its volumes ({', '.join(mounted + busy)}) will be taken offline." if mounted or busy
                           else ". Nothing on it is in use.")

    def sectors(c):
        lay = st["layout"]
        if dst is None or lay is None:
            c.status, c.detail = "block", "Not checked."
            return
        if dst.sector_size != src.sector_size:
            c.status = "block"
            c.detail = f"Source sectors are {src.sector_size} bytes, the target's are {dst.sector_size} bytes."
            c.fix = ("Windows can't use NTFS or EFI partitions formatted for one sector size on a drive with the "
                     f"other. Use a target with {src.sector_size}-byte sectors"
                     + (" (most drives: 512n or 512e)." if src.sector_size == 512 else "."))
        else:
            c.detail = f"{src.sector_size}-byte sectors on both."

    def space(c):
        lay = st["layout"]
        if dst is None or lay is None or lay.scheme not in ("GPT", "MBR"):
            c.status, c.detail = "block", "Not checked."
            return
        used = estimate_used(src, lay, st["vols"])
        plan, probs = make_plan(lay, dst.total_sectors, dst.sector_size, used, min_free)
        if probs:
            c.status, c.detail, c.fix = "block", probs[0][0], probs[0][1]
            return
        st["plan"] = plan
        w = plan.part("windows")
        ss = plan.sector_size
        if w.resize == "shrink":
            c.status = "warn" if used is None else "ok"
            c.detail = (f"The target is smaller than the source. Windows shrinks from "
                        f"{human_size(w.src_sectors * ss)} to {human_size(w.sectors * ss)}"
                        + (f" ({human_size(used['bytes'])} in use)." if used else
                           ". The used space is confirmed from the snapshot before anything is written."))
        elif w.resize == "grow":
            c.detail = f"Windows grows from {human_size(w.src_sectors * ss)} to {human_size(w.sectors * ss)}."
        else:
            c.detail = "Same size layout."

    def shadow(c):
        vols = [v for v in st["vols"] if v.get("letters") or v.get("windows")]
        low = [v for v in vols if v.get("free") is not None and v["free"] < shadow_min]
        if low:
            names = ", ".join((v.get("letters") or [v.get("guid", "?")])[0].rstrip("\\") for v in low)
            c.status, c.detail, c.fix = "block", f"Not enough free space on {names} for the snapshot.", \
                f"Free at least {human_size(shadow_min)} on {names} (empty the Recycle Bin, Disk Cleanup)."
        else:
            c.detail = "Enough free space for the snapshot while copying."

    def sleep(c):
        c.detail = "The PC is kept awake while the clone runs, and may sleep again afterwards."

    for key, fn in (("admin", admin), ("power", power), ("vss", vss), ("source", source), ("bitlocker", bitlocker),
                    ("target", target), ("sectors", sectors), ("space", space), ("shadow", shadow),
                    ("sleep", sleep)):
        add(key, fn)
    return Preflight(checks, st["plan"] if not any(c.status == "block" for c in checks) else None, st["layout"],
                     st["vols"])


def estimate_used(src: Device, lay: SrcLayout, vols) -> dict | None:
    """Used bytes and extent of the Windows partition from a live read of its NTFS bitmap (the snapshot confirms it
    later). None when the volume can't be read raw (BitLocker) or isn't NTFS."""
    win = next((p for p in lay.parts if p.role == "windows"), None)
    if win is None or win.fs != "NTFS":
        return None
    try:
        return ntfs_usage(src, win.start_lba * lay.sector_size)
    except Exception as e:  # noqa: BLE001  unknown here; confirmed from the snapshot before writing
        log.info("sysclone: live usage of C: not readable: %s", e)
        return None


def ntfs_usage(dev: Device, base: int) -> dict:
    """From the NTFS $Bitmap: {"extent": bytes from the volume start to the end of the last used cluster,
    "bytes": bytes in used clusters}."""
    from .ntfs import NTFSVolume
    vol = NTFSVolume(dev, base)
    rec = vol.read_record(6)
    if rec is None or (not rec.runs and rec.resident is None):
        raise SysCloneError("The NTFS $Bitmap can't be read.")
    clusters = vol.total_sectors * vol.bps // vol.cluster
    if rec.resident is not None:
        bm = rec.resident
    else:
        buf = bytearray()
        for lcn, n in rec.runs:
            buf += bytes(n * vol.cluster) if lcn is None else dev.read(base + lcn * vol.cluster, n * vol.cluster)
        bm = bytes(buf[:(clusters + 7) // 8])
    bits = int.from_bytes(bm, "little") & ((1 << clusters) - 1)
    return {"extent": bits.bit_length() * vol.cluster, "bytes": bits.bit_count() * vol.cluster,
            "cluster": vol.cluster, "clusters": clusters}


# ---------------------------------------------------------------------------------------------- tables
def _protective_mbr(ss, total) -> bytes:
    m = bytearray(ss)
    struct.pack_into("<B3sB3sII", m, 446, 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1, min(total - 1, 0xFFFFFFFF))
    m[510:512] = b"\x55\xaa"
    return bytes(m)


def gpt_bytes(plan: Plan) -> tuple[bytes, bytes, bytes, bytes, int]:
    """(protective MBR, primary header, entries padded to sectors, backup header, entry sectors)."""
    ss = plan.sector_size
    last = plan.target_sectors - 1
    n, esize = 128, 128
    ent_sectors = -(-n * esize // ss)
    table = bytearray(n * esize)
    for i, p in enumerate(plan.parts):
        e = bytearray(esize)
        e[0:16] = uuid.UUID(p.type_id).bytes_le
        e[16:32] = uuid.UUID(p.unique).bytes_le
        struct.pack_into("<QQQ", e, 32, p.start, p.start + p.sectors - 1, p.attrs)
        nm = p.name.encode("utf-16-le")[:72]
        e[56:56 + len(nm)] = nm
        table[i * esize:(i + 1) * esize] = e
    ecrc = zlib.crc32(bytes(table))
    first_usable, last_usable = 2 + ent_sectors, last - 1 - ent_sectors

    def header(my, alt, entries_lba):
        h = bytearray(ss)
        struct.pack_into("<8sIII4xQQQQ16sQIII", h, 0, b"EFI PART", 0x00010000, 92, 0, my, alt, first_usable,
                         last_usable, uuid.UUID(plan.disk_id).bytes_le, entries_lba, n, esize, ecrc)
        struct.pack_into("<I", h, 16, zlib.crc32(bytes(h[:92])))
        return bytes(h)
    padded = bytes(table).ljust(ent_sectors * ss, b"\0")
    return (_protective_mbr(ss, plan.target_sectors), header(1, last, 2), padded,
            header(last, 1, last - ent_sectors), ent_sectors)


def mbr_bytes(plan: Plan) -> bytes:
    ss = plan.sector_size
    m = bytearray(ss)
    code = bytes.fromhex(plan.boot_code_hex)[:440]
    m[:len(code)] = code
    struct.pack_into("<I", m, 440, int(plan.disk_id, 16))
    for i, p in enumerate(plan.parts[:4]):
        struct.pack_into("<B3sB3sII", m, 446 + 16 * i, 0x80 if p.bootable else 0, b"\xfe\xff\xff",
                         int(p.type_id, 16), b"\xfe\xff\xff", p.start, p.sectors)
    m[510:512] = b"\x55\xaa"
    return bytes(m)


def write_table(dst: Device, plan: Plan):
    ss = plan.sector_size
    if plan.scheme == "GPT":
        pmbr, primary, entries, backup, ent_sectors = gpt_bytes(plan)
        last = plan.target_sectors - 1
        dst.write_sectors(0, pmbr)
        dst.write_sectors(1, primary)
        dst.write_sectors(2, entries)
        dst.write_sectors(last - ent_sectors, entries)
        dst.write_sectors(last, backup)
    else:
        if len(plan.parts) > 4:
            raise SysCloneError("More than four partitions can't go in an MBR table.")
        dst.write_sectors(0, mbr_bytes(plan))
    dst.write(MARK_OFFSET, bytes(ss))  # the unfinished marker goes once the table is in place
    dst.flush()


def mark_unusable(dst: Device, job_id: str = ""):
    """Zero the first and last MiB (both partition tables) and leave a plain-text marker, so a cancelled or failed
    clone can never be mistaken for a working drive."""
    size = dst.usable_size
    if size < 4 * MiB:
        return
    dst.write(0, bytes(MiB))
    tail = (size - MiB) - (size - MiB) % dst.sector_size
    dst.write(tail, bytes(size - tail))
    note = (MARK_MAGIC + b" " + job_id.encode() + b"\r\nThis drive holds an unfinished SectorSmith clone and is not "
            b"usable. Clone again or erase it.\r\n")
    dst.write(MARK_OFFSET, note.ljust(_up(len(note), dst.sector_size), b"\0"))
    dst.flush()


def is_marked_unusable(dev: Device) -> bool:
    try:
        return dev.read(MARK_OFFSET, len(MARK_MAGIC)) == MARK_MAGIC
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------- NTFS edits
class _Rec:
    """One MFT record of a volume, editable with its update sequence fix-ups handled."""

    def __init__(self, dev, vol, recno):
        from .ntfs import NTFSError
        self.dev, self.vol = dev, vol
        for rec_start, dev_off, nbytes in vol._mft_byte_ranges():
            if rec_start <= recno < rec_start + nbytes // vol.rec_size:
                self.off = dev_off + (recno - rec_start) * vol.rec_size
                break
        else:
            raise NTFSError(f"MFT record {recno} not found")
        raw = dev.read(self.off, vol.rec_size)
        fixed = vol._fixup(raw)
        if fixed is None:
            raise NTFSError(f"MFT record {recno} is damaged")
        self.b = bytearray(fixed)

    def attrs(self):
        off = struct.unpack_from("<H", self.b, 20)[0]
        while off + 16 <= len(self.b):
            atype, alen = struct.unpack_from("<II", self.b, off)
            if atype == 0xFFFFFFFF or alen == 0 or off + alen > len(self.b):
                return
            nlen, noff = self.b[off + 9], struct.unpack_from("<H", self.b, off + 10)[0]
            name = self.b[off + noff: off + noff + 2 * nlen].decode("utf-16-le", "replace")
            yield atype, off, alen, name, self.b[off + 8]
            off += alen

    def find(self, atype, name=""):
        return next(((o, ln, nr) for t, o, ln, nm, nr in self.attrs() if t == atype and nm == name), None)

    def raw(self) -> bytes:
        b = bytearray(self.b)
        usa_off, usa_cnt = struct.unpack_from("<HH", b, 4)
        stride = len(b) // (usa_cnt - 1)
        usn = b[usa_off:usa_off + 2]
        for i in range(1, usa_cnt):
            end = i * stride - 2
            b[usa_off + 2 * i:usa_off + 2 * i + 2] = b[end:end + 2]
            b[end:end + 2] = usn
        return bytes(b)

    def save(self):
        self.dev.write(self.off, self.raw())


def _encode_sparse_run(clusters: int, width: int) -> bytes:
    return bytes([width]) + clusters.to_bytes(width, "little") + b"\0"


def ntfs_shrink_problems(dev: Device, base: int, new_bytes: int) -> list[str]:
    """Why the NTFS volume at ``base`` can't be cut to ``new_bytes`` (partition size), empty when it can."""
    from .ntfs import NTFSVolume, decode_runlist
    vol = NTFSVolume(dev, base)
    bps = vol.bps
    new_total = new_bytes // bps - 1
    new_clusters = new_total * bps // vol.cluster
    old_clusters = vol.total_sectors * bps // vol.cluster
    out = []
    if new_clusters >= old_clusters:
        return out
    bm = _Rec(dev, vol, 6)
    a = bm.find(0x80)
    if a is None or not a[2]:
        return ["$Bitmap has no non-resident data stream."]
    o = a[0]
    runs = decode_runlist(bytes(bm.b[o:o + a[1]]), struct.unpack_from("<H", bm.b, o + 32)[0])
    data = bytearray()
    for lcn, n in runs:
        data += bytes(n * vol.cluster) if lcn is None else dev.read(base + lcn * vol.cluster, n * vol.cluster)
    for c in range(new_clusters, old_clusters):
        if data[c >> 3] >> (c & 7) & 1:
            out.append(f"Cluster {c} past the new end is in use.")
            break
    if vol.mft_lcn >= new_clusters or vol.mftmirr_lcn >= new_clusters:
        out.append("$MFT or $MFTMirr lies past the new end.")
    bad = _Rec(dev, vol, 8)
    b = bad.find(0x80, "$Bad")
    if b is None or not b[2]:
        out.append("$BadClus has no $Bad stream.")
    else:
        o = b[0]
        runs = decode_runlist(bytes(bad.b[o:o + b[1]]), struct.unpack_from("<H", bad.b, o + 32)[0])
        if any(lcn is not None for lcn, _n in runs):
            out.append("The volume has clusters marked bad; it can't be shrunk safely.")
    return out


def ntfs_shrink(dev: Device, base: int, new_bytes: int, hidden: int):
    """Cut the NTFS volume at ``base`` to fit a partition of ``new_bytes``: boot sector size, $Bitmap length (with
    the bits past the end set, as NTFS does), $BadClus:$Bad length and a backup boot sector in the new last sector.
    Only call after ntfs_shrink_problems() returned nothing for the same data."""
    from .ntfs import NTFSVolume, decode_runlist
    vol = NTFSVolume(dev, base)
    bps = vol.bps
    new_total = new_bytes // bps - 1
    new_clusters = new_total * bps // vol.cluster
    # $Bitmap: data length = ceil(clusters / 8) rounded up to 8 bytes, bits past the last cluster set
    bm = _Rec(dev, vol, 6)
    o, ln, _nr = bm.find(0x80)
    runs = decode_runlist(bytes(bm.b[o:o + ln]), struct.unpack_from("<H", bm.b, o + 32)[0])
    old_len = struct.unpack_from("<Q", bm.b, o + 48)[0]
    new_len = _up(-(-new_clusters // 8), 8)
    data = bytearray()
    for lcn, n in runs:
        data += bytes(n * vol.cluster) if lcn is None else dev.read(base + lcn * vol.cluster, n * vol.cluster)
    for c in range(new_clusters, new_len * 8):
        data[c >> 3] |= 1 << (c & 7)
    data[new_len:max(old_len, new_len)] = bytes(max(old_len, new_len) - new_len)
    pos = 0
    for lcn, n in runs:
        if lcn is not None:
            dev.write(base + lcn * vol.cluster, bytes(data[pos:pos + n * vol.cluster]))
        pos += n * vol.cluster
    struct.pack_into("<QQ", bm.b, o + 48, new_len, new_len)
    bm.save()
    # $BadClus:$Bad is a sparse stream as long as the volume
    bad = _Rec(dev, vol, 8)
    o, ln, _nr = bad.find(0x80, "$Bad")
    ro = struct.unpack_from("<H", bad.b, o + 32)[0]
    width = bad.b[o + ro] & 0x0F
    run = _encode_sparse_run(new_clusters, max(width, (new_clusters.bit_length() + 8) // 8))
    if ro + len(run) > ln:
        raise SysCloneError("The $BadClus record has no room for the new length.")
    bad.b[o + ro:o + ln] = run.ljust(ln - ro, b"\0")
    old_size = struct.unpack_from("<Q", bad.b, o + 48)[0]
    init = struct.unpack_from("<Q", bad.b, o + 56)[0]
    size = new_clusters * vol.cluster
    struct.pack_into("<Q", bad.b, o + 24, new_clusters - 1)
    struct.pack_into("<QQQ", bad.b, o + 40, size, size, size if init == old_size else init)
    bad.save()
    # boot sector and its backup in the new last sector
    bs = bytearray(dev.read(base, bps))
    struct.pack_into("<Q", bs, 40, new_total)
    struct.pack_into("<I", bs, 0x1C, hidden & 0xFFFFFFFF)
    dev.write(base, bytes(bs))
    dev.write(base + new_total * bps, bytes(bs))


def fix_hidden_sectors(dev: Device, p: PartPlan, ss: int) -> int:
    """NTFS and FAT boot sectors record where their partition starts (BIOS boot code depends on it). For NTFS the
    backup boot sector is rewritten from the primary: it sits past the end of the file system, where a snapshot
    can't reach it. Returns how many sectors were written."""
    base = p.start * ss
    head = bytearray(dev.read(base, ss))
    if head[510:512] != b"\x55\xaa":
        return 0
    fs = partitions.detect_fs(dev.read(base, 4096))[0]
    end = (p.start + p.sectors) * ss
    n = 0
    if fs == "NTFS":
        total = struct.unpack_from("<Q", head, 40)[0]
        bps = struct.unpack_from("<H", head, 11)[0] or ss
        struct.pack_into("<I", head, 0x1C, p.start & 0xFFFFFFFF)
        for at in (base, base + total * bps):
            if at + len(head) <= end:
                dev.write(at, bytes(head))
                n += 1
        return n
    places = [base]
    if fs == "FAT32":
        bk = struct.unpack_from("<H", head, 50)[0]
        bps = struct.unpack_from("<H", head, 11)[0] or ss
        places += [base + bk * bps] if 0 < bk < 32 else []
    elif fs not in ("FAT16", "FAT12"):
        return 0
    for at in places:
        if at + ss > end:
            continue
        sec = bytearray(dev.read(at, ss))
        if sec[510:512] != b"\x55\xaa":
            continue
        if struct.unpack_from("<I", sec, 0x1C)[0] != p.start & 0xFFFFFFFF:
            struct.pack_into("<I", sec, 0x1C, p.start & 0xFFFFFFFF)
            dev.write(at, bytes(sec))
            n += 1
    return n


# ---------------------------------------------------------------------------------------------- journal
class Journal:
    """Checkpoints in app_dir()/sysclone: <id>.json (plan, snapshot, state) and <id>.chunks (verified chunks)."""

    def __init__(self, data: dict, folder=None):
        self.folder = folder or (app_dir() / "sysclone")
        self.data = data
        self._done_fh = None

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def path(self):
        return os.path.join(self.folder, f"{self.id}.json")

    @property
    def chunks_path(self):
        return os.path.join(self.folder, f"{self.id}.chunks")

    @classmethod
    def create(cls, src: Device, dst: Device, plan: Plan, folder=None) -> "Journal":
        j = cls({"id": uuid.uuid4().hex[:12], "created": time.time(), "state": "running", "phase": "snapshot",
                 "src": _ident(src), "dst": _ident(dst), "plan": plan.to_dict(), "snapshot": None, "ranges": None,
                 "heartbeat": time.time(), "version": 1}, folder)
        os.makedirs(j.folder, exist_ok=True)
        j.save()
        return j

    def save(self):
        self.data["heartbeat"] = time.time()
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    def add_done(self, items):
        if self._done_fh is None:
            self._done_fh = open(self.chunks_path, "a", encoding="ascii")
        self._done_fh.write("".join(f"{i} {h}\n" for i, h in items))
        self._done_fh.flush()
        os.fsync(self._done_fh.fileno())
        if time.time() - self.data.get("heartbeat", 0) > 30:
            self.save()

    def done(self) -> dict:
        out = {}
        try:
            with open(self.chunks_path, encoding="ascii") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and len(parts[1]) == 64:
                        out[int(parts[0])] = parts[1]
        except OSError:
            pass
        return out

    def close(self):
        if self._done_fh is not None:
            self._done_fh.close()
            self._done_fh = None

    def remove(self):
        self.close()
        for p in (self.path, self.chunks_path, self.path + ".tmp"):
            try:
                os.remove(p)
            except OSError:
                pass

    @classmethod
    def all(cls, folder=None) -> list["Journal"]:
        folder = folder or (app_dir() / "sysclone")
        out = []
        try:
            names = os.listdir(folder)
        except OSError:
            return out
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, n), encoding="utf-8") as f:
                    out.append(cls(json.load(f), folder))
            except (OSError, ValueError):
                continue
        return out


def _ident(dev: Device) -> dict:
    return {"path": dev.path, "size": dev.size, "serial": dev.serial, "model": dev.model,
            "sector_size": dev.sector_size, "name": dev.name}


def interrupted(folder=None, max_age_hours=STALE_HOURS, idle=90) -> list[Journal]:
    """Clones that stopped without finishing (crash, power cut) and can still be resumed. A running clone writes a
    heartbeat at least every 30 seconds, so one quiet for ``idle`` seconds is not running any more."""
    now = time.time()
    return [j for j in Journal.all(folder) if j.data.get("state") == "running"
            and now - j.data.get("heartbeat", 0) >= idle and now - j.data.get("created", 0) < max_age_hours * 3600]


def read_marker(dev: Device) -> str | None:
    """The clone id in an unfinished-clone marker, or None."""
    try:
        b = dev.read(MARK_OFFSET, 128)
    except OSError:
        return None
    if not b.startswith(MARK_MAGIC):
        return None
    return b[len(MARK_MAGIC):].split(b"\r", 1)[0].strip().decode("ascii", "replace")


def resume_target_ok(j: Journal, dst: Device) -> bool:
    """The drive is the one this interrupted clone was writing: same size and serial, and still carrying the clone's
    own unfinished marker (so nothing else has been written to it since)."""
    want = j.data.get("dst") or {}
    if dst.size != want.get("size") or dst.sector_size != want.get("sector_size"):
        return False
    if (want.get("serial") or "") != (dst.serial or ""):
        return False
    return read_marker(dst) == j.id


def cleanup_stale(layer: SystemLayer | None = None, folder=None, max_age_hours=STALE_HOURS) -> list[str]:
    """At start-up: delete snapshots left by clones that ended or are too old to resume. Returns what was done."""
    layer = layer or get_layer()
    now = time.time()
    done = []
    for j in Journal.all(folder):
        st = j.data.get("state")
        old = now - j.data.get("created", 0) > max_age_hours * 3600
        if st == "running" and not old:
            continue  # offered for resume; the wizard deletes it if the technician discards it
        snap = j.data.get("snapshot")
        if snap and layer is not None:
            try:
                layer.delete_snapshot(snap)
                done.append(f"Deleted leftover snapshot {snap.get('set_id') or snap.get('ids')}")
            except Exception as e:  # noqa: BLE001
                log.warning("sysclone: couldn't delete snapshot %s: %s", snap, e)
                continue
        j.remove()
        done.append(f"Removed clone journal {j.id}")
    return done


def discard(j: Journal, layer: SystemLayer | None = None):
    """The technician chose not to resume: release its snapshot and forget it."""
    layer = layer or get_layer()
    if j.data.get("snapshot") and layer is not None:
        layer.delete_snapshot(j.data["snapshot"])
    j.remove()


# ---------------------------------------------------------------------------------------------- the clone
class _Fatal(Exception):
    pass


class CloneJob:
    """One clone. run() returns the result dict (with result["verify"]) or raises SysCloneError before writing,
    or Cancelled (with info["state"])."""

    def __init__(self, src: Device, dst: Device, plan: Plan, layer: SystemLayer, prog: Progress,
                 journal: Journal | None = None, journal_dir=None, bitlocker_on_boot: bool = False,
                 take_offline: bool = True, max_bad: int = MAX_BAD_SECTORS, report_dir=None, on_event=None,
                 journal_every: int = JOURNAL_EVERY):
        self.src, self.dst, self.plan, self.layer, self.prog = src, dst, plan, layer, prog
        self.journal = journal
        self.resuming = journal is not None
        self.journal_dir = journal_dir
        self.bitlocker_on_boot = bitlocker_on_boot
        self.max_bad = max_bad
        self.journal_every = journal_every
        self.report_dir = report_dir
        self.on_event = on_event or (lambda *_a: None)
        self.snap: Snapshot | None = None
        self.touched = False
        self.checks: list[dict] = []
        self.bad: list[tuple[int, int]] = []        # (source LBA, target LBA) copied as zeros
        self.timings: dict = {}
        self.copied = self.written = 0
        self.notes: list[str] = []
        self.chunks: list = []                      # (src_off or None, dst_off, length)
        self.fs_end: dict = {}                      # snapshot start -> end of the file system's clusters

    # -- helpers -------------------------------------------------------------------------------------------
    def _check(self, name, status, detail=""):
        self.checks.append({"name": name, "status": status, "detail": detail})
        log.info("sysclone check %s: %s %s", name, status, detail)

    def _phase(self, key, total=1, detail=""):
        self.timings.setdefault(key, [time.time(), None])
        for k, v in self.timings.items():
            if k != key and v[1] is None:
                v[1] = time.time()
        self.prog.reset(total, PHASE_LABELS[key] + "…")
        if detail:
            self.prog.set_detail(detail)
        self.on_event("phase", key)

    # -- main ----------------------------------------------------------------------------------------------
    def run(self) -> dict:
        t0 = time.time()
        awake = False
        try:
            awake = self.layer.keep_awake(True)
            res = self._run()
        except Cancelled as c:
            state = self._abort("cancelled")
            c.info.update(state=state, written=self.written, target=self.dst.path, snapshot_released=True)
            raise
        except _Fatal:
            # a check failed: the verdict says which; the target must not look usable
            state = self._abort("failed")
            res = self._result(t0)
            res.update(target_state=state, verify=self._verdict())
            self._write_report(res)
        except SysCloneError as e:
            if not self.touched:
                self._release(remove_journal=True)
                raise
            state = self._abort("failed")
            res = self._failed_result(t0, f"{e.reason} {e.fix}".strip(), state)
        except Exception as e:  # noqa: BLE001
            log.error("sysclone failed: %s", e, exc_info=True)
            if not self.touched:
                self._release(remove_journal=True)
                raise SysCloneError(f"The clone couldn't start: {e}", "Nothing was written. Check the drives and "
                                    "try again.") from e
            state = self._abort("failed")
            res = self._failed_result(t0, f"{type(e).__name__}: {e}", state)
        finally:
            if awake:
                self.layer.keep_awake(False)
            for d in (self.src, self.dst):
                try:
                    d.close()
                except OSError:
                    pass
        res["seconds"] = round(time.time() - t0, 1)
        return res

    def _release(self, remove_journal: bool):
        if self.snap is not None:
            try:
                self.snap.release()
            except Exception as e:  # noqa: BLE001
                log.warning("sysclone: snapshot release failed: %s", e)
        self.src._overlay = []
        if self.journal is not None:
            if remove_journal:
                self.journal.remove()
            else:
                self.journal.close()

    def _abort(self, why) -> str:
        """After a cancel or failure: target marked unusable (when it was touched), snapshot released, journal
        removed. Returns the state the target was left in."""
        state = "untouched"
        if self.touched:
            try:
                self.dst.close()
                self.dst.open(writable=True)
                mark_unusable(self.dst, self.journal.id if self.journal else "")
                state = "marked-unusable"
            except Exception as e:  # noqa: BLE001
                log.error("sysclone: couldn't mark the target unusable: %s", e)
                state = "part-written"
            finally:
                try:
                    self.dst.close()
                except OSError:
                    pass
        self._release(remove_journal=True)
        log.info("sysclone %s, target %s", why, state)
        return state

    def _failed_result(self, t0, error, state) -> dict:
        self._check("Clone finished", "failed", error)
        res = self._result(t0)
        res.update(error=error, target_state=state)
        res["verify"] = V.verdict(V.FAILED, f"Failed: {error}", [f"{c['name']}: {c['status']} {c['detail']}".strip()
                                                                 for c in self.checks])
        self._write_report(res)
        return res

    def _run(self) -> dict:
        t0 = time.time()
        plan = self.plan
        # 1. snapshot ---------------------------------------------------------------------------------------
        self._phase("snapshot")
        if self.journal is None:
            self.journal = Journal.create(self.src, self.dst, plan, self.journal_dir)
        self.src.open(writable=False)
        if self.resuming and self.journal.data.get("snapshot"):
            self.snap = self.layer.attach_snapshot(self.src, self.journal.data["snapshot"])
            if self.snap is None:
                raise SysCloneError("The snapshot from the interrupted clone is gone (the PC restarted or Windows "
                                    "removed it), so the copy can't continue consistently.",
                                    "Start the clone again from the beginning.")
            self.notes.append("Resumed from the interrupted clone's snapshot.")
        else:
            # Windows and lettered volumes are in use; Recovery and other unmounted volumes are read live (VSS
            # often can't snapshot them: they're too small to hold their own shadow storage)
            vols = [{"offset": p.src_start * plan.sector_size, "length": p.src_sectors * plan.sector_size,
                     "role": p.role, "letters": p.letters} for p in plan.parts
                    if p.action == "used" and (p.role == "windows" or p.letters)]

            def on_id(info):
                self.journal.data["snapshot"] = info
                self.journal.save()
            self.snap = self.layer.create_snapshot(self.src, vols, on_id)
            self.journal.data["snapshot"] = self.snap.info
            self.journal.save()
        self.src._overlay = list(self.snap.overlay)
        self.notes += self.snap.notes
        for o, _n, _r in self.snap.overlay:
            try:
                u = ntfs_usage(self.src, o)
                self.fs_end[o] = o + u["clusters"] * u["cluster"]
            except Exception as e:  # noqa: BLE001  not NTFS: every short read stays an error
                log.info("sysclone: no file system size for the snapshot at %d: %s", o, e)
        self.prog.check()
        for p in plan.parts:
            mounted = p.letters or p.role == "windows"
            if p.action == "used" and mounted and not self.snap.covers(p.src_start * plan.sector_size):
                raise SysCloneError(f"Windows couldn't take a snapshot of {p.role} partition "
                                    f"{', '.join(p.letters) or ''}, so it can't be copied while in use.",
                                    "Restart the PC, make sure the Volume Shadow Copy service runs, and try again.")
        # 2. map used space from the snapshot and re-check the plan (still nothing written) ------------------
        self.prog.set_label(PHASE_LABELS["snapshot"] + "…", "Reading which parts of each volume are in use")
        if self.journal.data.get("ranges"):
            self.chunks = [tuple(c) for c in self.journal.data["ranges"]]
        else:
            self.chunks = self._map()
            self.journal.data["ranges"] = self.chunks
            self.journal.data["phase"] = "copy"
            self.journal.save()
        self.prog.check()
        # 3. copy ------------------------------------------------------------------------------------------
        copy_bytes = sum(n for _s, _d, n in self.chunks)
        self.dst.open(writable=True)
        self.touched = True
        if not (self.resuming and read_marker(self.dst) == self.journal.id):
            if self.resuming:  # the marker is gone, so is any trust in what was copied before
                self.journal.remove()
                self.journal.data["ranges"] = self.chunks
                self.resuming = False
                self.journal.save()
            mark_unusable(self.dst, self.journal.id)
        self._phase("copy", 2 * copy_bytes, f"{human_size(copy_bytes)} to copy, then the same to verify")
        hashes = self._copy(copy_bytes)
        self.journal.data["phase"] = "verify"
        self.journal.save()
        # 4. verify every chunk against the hash taken while copying ----------------------------------------
        self.timings.setdefault("verify", [time.time(), None])
        self.timings["copy"][1] = time.time()
        self.on_event("phase", "verify")
        self.prog.set_label(PHASE_LABELS["verify"] + "…", "Reading every copied block back from the new drive")
        bad_chunks = self._verify(hashes, copy_bytes)
        if bad_chunks:
            self.notes.append(f"{len(bad_chunks)} block(s) read back differently and were copied again.")
            redo = self._recopy(bad_chunks, hashes)
            if redo:
                self._check("Every copied block matches the snapshot", "failed",
                            f"{len(redo)} block(s) still differ after copying them again, first at target byte "
                            f"{self.chunks[redo[0]][1]:,}. The new drive is not storing data reliably.")
                raise _Fatal()
        self._check("Every copied block matches the snapshot", "problems" if self.bad else "passed",
                    f"{len(self.chunks)} blocks, {human_size(copy_bytes)} re-read from the new drive and compared by "
                    "SHA-256" + (f"; {len(self.bad)} unreadable source sector(s) copied as zeros" if self.bad else ""))
        # 5. file system edits and the partition table, then structure checks ---------------------------------
        self._edits()
        self._structure()
        self.dst.close()
        self.journal.data["phase"] = "boot"
        self.journal.save()
        # 6. boot fix-up --------------------------------------------------------------------------------------
        self._phase("boot", 7)
        self._fixup()
        self._release(remove_journal=False)
        self.journal.data["state"] = "finished"
        self.journal.save()
        self.journal.remove()
        res = self._result(t0)
        res["verify"] = self._verdict()
        self._write_report(res)
        return res

    # -- mapping ---------------------------------------------------------------------------------------------
    def _map(self) -> list:
        plan, ss, src = self.plan, self.plan.sector_size, self.src
        chunks = []
        for p in plan.parts:
            self.prog.check()
            sbase, dbase = p.src_start * ss, p.start * ss
            ssize, dsize = p.src_sectors * ss, p.sectors * ss
            if p.action == "zero":
                chunks += [(None, dbase + o, n) for o, n in usedmap.pieces([(0, dsize)], CHUNK)]
                continue
            ranges = None
            if p.action == "used":
                ranges = usedmap.partition_used(src, p.src_start, p.src_sectors, "NTFS")
                if ranges is None:
                    if p.resize == "shrink":
                        raise SysCloneError("Couldn't read which parts of the Windows partition are in use, so it "
                                            "can't be shrunk.", "Use a target at least as big as the source disk.")
                    self.notes.append(f"{p.role}: file system map not readable, copied block for block.")
                else:
                    ranges = self._with_metadata(p, ranges)
            if ranges is None:
                ranges = [(sbase, ssize)]
            if p.resize == "shrink":
                used = ntfs_usage(src, sbase)
                if used["extent"] + ss > dsize:
                    raise SysCloneError(f"Since the check, files on C: have grown past the "
                                        f"{human_size(dsize)} that fits on the target.",
                                        "Shrink C: on this PC first (Disk Management, Shrink Volume), or use a "
                                        "bigger target. Nothing was written.")
                probs = ntfs_shrink_problems(src, sbase, dsize)
                if probs:
                    raise SysCloneError("The Windows partition can't be shrunk safely: " + probs[0],
                                        "Use a target at least as big as the source disk, or run chkdsk on C: and "
                                        "shrink it on this PC first. Nothing was written.")
                ranges = usedmap.clip(ranges, sbase, sbase + dsize - ss)
            rel = usedmap.merge([(o - sbase, n) for o, n in ranges], gap=0)
            for o, n in usedmap.pieces(rel, CHUNK):
                if o + n > dsize:
                    n = dsize - o
                if n > 0:
                    chunks.append((sbase + o, dbase + o, n))
        chunks.sort(key=lambda c: c[1])
        return chunks

    def _with_metadata(self, p, ranges):
        """Belt and braces: the whole $MFT, $MFTMirr and $LogFile are copied even if the bitmap missed them."""
        from .ntfs import NTFSVolume
        ss = self.plan.sector_size
        base = p.src_start * ss
        try:
            vol = NTFSVolume(self.src, base)
            extra = [(base + lcn * vol.cluster, n * vol.cluster) for lcn, n in vol.mft_runs if lcn is not None]
            extra.append((base + vol.mftmirr_lcn * vol.cluster, max(vol.cluster, 4 * vol.rec_size)))
            log_rec = vol.read_record(2)
            if log_rec is not None:
                extra += [(base + lcn * vol.cluster, n * vol.cluster) for lcn, n in log_rec.runs if lcn is not None]
            extra.append((base, max(vol.cluster, 8192)))
        except Exception as e:  # noqa: BLE001
            raise SysCloneError(f"The NTFS metadata of the {p.role} partition can't be read from the snapshot: {e}",
                                "Run chkdsk on that volume and try again. Nothing was written.") from e
        return usedmap.merge(usedmap.clip(ranges + extra, base, base + p.src_sectors * ss), gap=0)

    # -- copy ------------------------------------------------------------------------------------------------
    def _src_read(self, off, n) -> bytes:
        """Read from the snapshot where there is one, from the disk elsewhere. A short read is an error here, never
        zeros: the only exception is the end of a partition past its file system (the NTFS backup boot sector and
        slack), which a shadow copy doesn't serve and which is read from the disk instead."""
        for o, ln, reader in self.src._overlay:
            if o <= off < o + ln:
                if off + n > o + ln:
                    raise OSError(0, f"read at byte {off:,} crosses the end of a snapshot")
                d = reader.read(off - o, n)
                if len(d) == n:
                    return d
                got = off + len(d)
                fs_end = self.fs_end.get(o)
                if fs_end is not None and got >= fs_end:
                    rest = self.src._read_direct(got, n - len(d))
                    if len(rest) == n - len(d):
                        return d + rest
                raise OSError(0, f"short read from the snapshot at byte {got:,}")
        d = self.src._read_direct(off, n)
        if len(d) != n:
            raise OSError(0, f"short read at byte {off + len(d):,}")
        return d

    def _read_chunk(self, off, n) -> tuple[bytes, list]:
        """Read with retries and smaller blocks; unreadable sectors come back as zeros with their offsets."""
        src, ss = self, self.plan.sector_size
        for attempt in range(2):
            try:
                d = src._src_read(off, n)
                if len(d) == n:
                    return d, []
            except OSError:
                pass
            self._snapshot_alive()
        out = bytearray()
        bad = []
        step = 64 * 1024
        for b in range(0, n, step):
            m = min(step, n - b)
            got = None
            for _ in range(2):
                try:
                    d = src._src_read(off + b, m)
                    if len(d) == m:
                        got = d
                        break
                except OSError:
                    pass
            if got is None:
                got = bytearray()
                for s in range(0, m, ss):
                    sec = None
                    for _ in range(2):
                        try:
                            d = src._src_read(off + b + s, ss)
                            if len(d) == ss:
                                sec = d
                                break
                        except OSError:
                            pass
                    if sec is None:
                        bad.append(off + b + s)
                        sec = bytes(ss)
                    got += sec
            out += got
        self._snapshot_alive()
        return bytes(out), bad

    def _snapshot_alive(self):
        if self.snap is not None and self.snap.overlay and not self.snap.alive():
            raise SysCloneError("Windows deleted the snapshot while copying (usually the shadow copy storage ran "
                                "out of space).", "Free space on C: and try again.")

    def _write(self, off, data):
        for attempt in range(3):
            try:
                self.dst.write(off, data)
                return
            except OSError as e:
                if attempt == 2:
                    raise SysCloneError(f"The new drive refused a write at byte {off:,}: {e}",
                                        "Check its cable or USB port, or try another drive.") from e
                time.sleep(0.5)

    def _copy(self, copy_bytes) -> list:
        done_before = self.journal.done() if self.resuming else {}
        hashes: list = [None] * len(self.chunks)
        todo = []
        skipped = 0
        for i, (s, d, n) in enumerate(self.chunks):
            if i in done_before:
                hashes[i] = bytes.fromhex(done_before[i])
                skipped += n
            else:
                todo.append(i)
        if skipped:
            self.notes.append(f"Resumed: {human_size(skipped)} was already copied before the interruption.")
        q: queue.Queue = queue.Queue(maxsize=4)
        stop = threading.Event()
        zero = bytes(CHUNK)
        ss = self.plan.sector_size

        def reader():
            try:
                for i in todo:
                    if stop.is_set():
                        return
                    s, d, n = self.chunks[i]
                    if s is None:
                        data, bad = zero[:n], []
                    else:
                        data, bad = self._read_chunk(s, n)
                    q.put((i, data, hashlib.sha256(data).digest(), bad))
                q.put(None)
            except BaseException as e:  # noqa: BLE001  handed to the writer side
                q.put(e)

        th = threading.Thread(target=reader, daemon=True, name="sysclone-read")
        th.start()
        done = skipped
        pending = []
        last_cp = time.time()
        self.prog.update(done)
        try:
            while True:
                try:
                    item = q.get(timeout=0.25)
                except queue.Empty:
                    self.prog.check()
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                self.prog.check()
                i, data, h, bad = item
                s, d, n = self.chunks[i]
                for b in bad:
                    self.bad.append((b // ss, (d + b - s) // ss))
                if len(self.bad) > self.max_bad:
                    raise SysCloneError(f"More than {self.max_bad} unreadable sectors on the source drive. It is "
                                        "failing; copying zeros over that much data would not give a working copy.",
                                        "Image it with Image and clone (it retries harder) and replace it.")
                if bad:
                    self.prog.set_detail(f"{len(self.bad)} unreadable sector(s) so far, copied as zeros")
                self._write(d, data)
                hashes[i] = h
                self.written += n
                done += n
                pending.append((i, h.hex()))
                self.prog.update(done)
                if len(pending) >= self.journal_every or time.time() - last_cp > 5:
                    self.dst.flush()
                    self.journal.add_done(pending)
                    pending = []
                    last_cp = time.time()
            self.dst.flush()
            if pending:
                self.journal.add_done(pending)
        finally:
            stop.set()
            while th.is_alive():
                try:
                    q.get(timeout=0.1)
                except queue.Empty:
                    pass
            th.join(5)
        self.copied = copy_bytes
        return hashes

    def _verify(self, hashes, copy_bytes) -> list:
        dst = self.dst
        done = copy_bytes
        bad = []
        for i, (_s, d, n) in enumerate(self.chunks):
            self.prog.check()
            try:
                got = dst.read(d, n)
            except OSError:
                got = b""
            if len(got) != n or hashlib.sha256(got).digest() != hashes[i]:
                bad.append(i)
            done += n
            self.prog.update(done)
        return bad

    def _recopy(self, idx, hashes) -> list:
        for i in idx:
            self.prog.check()
            s, d, n = self.chunks[i]
            data = bytes(n) if s is None else self._read_chunk(s, n)[0]
            hashes[i] = hashlib.sha256(data).digest()
            self._write(d, data)
        self.dst.flush()
        still = []
        for i in idx:
            _s, d, n = self.chunks[i]
            if hashlib.sha256(self.dst.read(d, n)).digest() != hashes[i]:
                still.append(i)
        return still

    # -- edits and structure ---------------------------------------------------------------------------------
    def _edits(self):
        plan, ss, dst = self.plan, self.plan.sector_size, self.dst
        for p in plan.parts:
            if p.resize == "shrink":
                ntfs_shrink(dst, p.start * ss, p.sectors * ss, p.start)
                self.notes.append(f"Windows partition shrunk to {human_size(p.sectors * ss)}.")
            if p.action != "zero":
                fix_hidden_sectors(dst, p, ss)
            if p.resize == "grow" and p.fs == "NTFS":
                # until Windows extends it, the volume ends before the partition: keep a boot sector copy in the
                # partition's last sector too, where other NTFS tools look for the backup
                dst.write((p.start + p.sectors - 1) * ss, dst.read(p.start * ss, ss))
        write_table(dst, plan)

    def _structure(self):
        plan, ss, dst = self.plan, self.plan.sector_size, self.dst
        try:
            pt = partitions.read_partition_table(dst)
            got = [(x.start_lba, x.sectors) for x in pt.partitions]
            want = [(p.start, p.sectors) for p in plan.parts]
            types_ok = all((x.type_id.lower() == p.type_id.lower()) for x, p in zip(pt.partitions, plan.parts))
            ok = pt.scheme == plan.scheme and got == want and types_ok and not pt.notes
            self._check("Partition table on the new drive", "passed" if ok else "failed",
                        f"{pt.scheme}, {len(got)} partitions as planned" if ok else
                        f"Expected {plan.scheme} {want}, read {pt.scheme} {got} {pt.notes}")
        except Exception as e:  # noqa: BLE001
            self._check("Partition table on the new drive", "failed", str(e))
        for p in plan.parts:
            if p.action == "zero":
                continue
            base = p.start * ss
            fs = partitions.detect_fs(dst.read(base, 4096))[0]
            if p.fs in ("NTFS", "BitLocker") and p.action == "used":
                self._check(f"NTFS on {p.role} partition", *self._ntfs_ok(base, p))
            elif fs == "FAT32" or p.fs == "FAT32":
                self._check(f"FAT32 on {p.role} partition", *self._fat_ok(base, p))

    def _ntfs_ok(self, base, p) -> tuple[str, str]:
        from .ntfs import NTFSVolume
        dst, ss = self.dst, self.plan.sector_size
        try:
            bs = dst.read(base, 512)
            if bs[3:11] != b"NTFS    " or bs[510:512] != b"\x55\xaa":
                return "failed", "No NTFS boot sector."
            vol = NTFSVolume(dst, base)
            if (vol.total_sectors + 1) * vol.bps > p.sectors * ss:
                return "failed", "The volume is bigger than its partition."
            backup = dst.read(base + vol.total_sectors * vol.bps, vol.bps)
            if backup[:512] != dst.read(base, 512):
                return "failed", "The backup boot sector doesn't match."
            for r in range(4):
                a = vol._fixup(dst.read(base + vol.mft_lcn * vol.cluster + r * vol.rec_size, vol.rec_size))
                b = vol._fixup(dst.read(base + vol.mftmirr_lcn * vol.cluster + r * vol.rec_size, vol.rec_size))
                if a is None or b is None:
                    return "failed", f"$MFT or $MFTMirr record {r} is damaged."
            m0 = vol._parse(0, vol._fixup(dst.read(base + vol.mftmirr_lcn * vol.cluster, vol.rec_size)))
            if m0 is None or m0.runs != vol.mft_runs:
                return "failed", "$MFTMirr doesn't describe the same $MFT."
            bm = vol.read_record(6)
            clusters = vol.total_sectors * vol.bps // vol.cluster
            if bm is None or bm.size * 8 < clusters:
                return "failed", "$Bitmap is shorter than the volume."
            return "passed", f"Boot sector, backup and $MFTMirr valid, {human_size(clusters * vol.cluster)} volume"
        except Exception as e:  # noqa: BLE001
            return "failed", f"Couldn't read it: {e}"

    def _fat_ok(self, base, p) -> tuple[str, str]:
        bs = self.dst.read(base, 512)
        if bs[510:512] != b"\x55\xaa" or bs[82:90] != b"FAT32   ":
            return "failed", "No FAT32 boot sector."
        bps = struct.unpack_from("<H", bs, 11)[0]
        bk = struct.unpack_from("<H", bs, 50)[0]
        if 0 < bk < 32 and self.dst.read(base + bk * bps, 512)[:90] != bs[:90]:
            return "failed", "The backup boot sector doesn't match."
        return "passed", "Boot sector and backup valid"

    # -- boot fix-up -------------------------------------------------------------------------------------------
    def _fixup(self):
        plan, layer = self.plan, self.layer
        ctx = None
        step = 0

        def tick(detail):
            nonlocal step
            step += 1
            self.prog.update(step)
            self.prog.set_detail(detail)
        try:
            tick("Giving the new partitions temporary drive letters")
            ctx = layer.mount_target(self.dst, plan)
            tick("Reading Windows files back from the new drive")
            self._samples(ctx)
            tick("Writing the boot files (bcdboot)")
            ok, out = layer.bcdboot(ctx, plan)
            self._check("Boot files written (bcdboot)", "passed" if ok else "failed", out.strip()[-300:])
            tick("Checking the boot configuration")
            self._check_bcd(ctx)
            tick("Pointing Windows at its new partitions")
            ok, notes = layer.patch_registry(ctx, plan, registry_replacements(plan), self.bitlocker_on_boot)
            self._check("Windows drive letters follow the new partitions", "passed" if ok else "failed",
                        "; ".join(notes))
            if self.bitlocker_on_boot:
                self.notes.append("BitLocker is set to turn back on at the first sign-in on the new drive.")
            w = plan.part("windows")
            if w.resize == "grow":
                tick("Extending the Windows file system to fill the partition")
                ok, out = layer.extend_filesystem(ctx, plan)
                self._check("Windows file system fills its partition", "passed" if ok else "problems",
                            out.strip()[-300:] if ok else f"It still works, but C: stays "
                            f"{human_size(w.src_sectors * plan.sector_size)}. Extend it in Disk Management on the "
                            f"new drive. ({out.strip()[-200:]})")
        except Cancelled:
            raise
        except SysCloneError as e:
            self._check("Boot fix-up", "failed", f"{e.reason} {e.fix}".strip())
        except Exception as e:  # noqa: BLE001
            log.error("sysclone fix-up failed: %s", e, exc_info=True)
            self._check("Boot fix-up", "failed", str(e))
        finally:
            if ctx is not None:
                try:
                    layer.unmount_target(ctx)
                except Exception as e:  # noqa: BLE001
                    self.notes.append(f"Couldn't remove a temporary drive letter: {e}")
        if any(c["status"] == "failed" for c in self.checks):
            raise _Fatal()

    def _samples(self, ctx):
        w = self.plan.part("windows")
        rows, bad, missing_required = [], [], False
        for rel in SAMPLE_FILES:
            self.prog.check()
            a = _hash_stream(self.layer.open_source_file(self.snap, w, rel))
            if a is None:
                if rel == REQUIRED_SAMPLE:
                    missing_required = True
                continue
            b = _hash_stream(self.layer.open_target_file(ctx, w, rel))
            rows.append(rel)
            if a != b:
                bad.append(rel + (" (missing)" if b is None else " (differs)"))
        if missing_required:
            self._check("Windows files read back from the new drive", "failed",
                        f"{REQUIRED_SAMPLE} isn't on the source snapshot, so this is not a Windows partition.")
        elif bad:
            self._check("Windows files read back from the new drive", "failed", ", ".join(bad))
        else:
            self._check("Windows files read back from the new drive", "passed",
                        f"{len(rows)} files match by SHA-256: " + ", ".join(r.rsplit("\\", 1)[-1] for r in rows))

    def _check_bcd(self, ctx):
        plan = self.plan
        try:
            objs = self.layer.read_bcd(ctx, plan)
        except Exception as e:  # noqa: BLE001
            self._check("Boot configuration points at the new drive", "failed", f"BCD store not readable: {e}")
            return
        boot = plan.boot_part()
        want_boot, want_win, old = bcd_patterns(plan)
        mgr = [o for o in objs if o["kind"] == "bootmgr"]
        ldr = [o for o in objs if o["kind"] == "osloader"]
        problems = []
        if not mgr:
            problems.append("no Windows Boot Manager entry")
        if not ldr:
            problems.append("no Windows Boot Loader entry")
        for o in mgr:
            dev = o["elements"].get("11000001", b"")
            if not all(x in dev for x in want_boot):
                problems.append(f"Boot Manager device is not the new {boot.role} partition")
        for o in ldr:
            for el in ("11000001", "21000001"):
                dev = o["elements"].get(el, b"")
                if not all(x in dev for x in want_win):
                    problems.append(f"Boot Loader {'device' if el[0] == '1' else 'osdevice'} is not the new Windows "
                                    "partition")
        for o in objs:
            for k, v in o["elements"].items():
                if any(x in v for x in old):
                    problems.append(f"an entry still points at the old drive ({o['kind']} {k})")
                    break
        self._check("Boot configuration points at the new drive", "failed" if problems else "passed",
                    "; ".join(dict.fromkeys(problems)) if problems else
                    f"BCD on the new {boot.role} partition: {len(mgr)} boot manager and {len(ldr)} loader "
                    "entr" + ("y" if len(ldr) == 1 else "ies") + " point at the new partitions")

    # -- result ------------------------------------------------------------------------------------------------
    def _verdict(self) -> dict:
        details = [f"{c['name']}: {c['status']}" + (f" ({c['detail']})" if c["detail"] else "") for c in self.checks]
        failed = [c for c in self.checks if c["status"] == "failed"]
        if failed:
            return V.verdict(V.FAILED, f"Failed: {failed[0]['name']}", details)
        probs = [c for c in self.checks if c["status"] == "problems"]
        if probs or self.bad:
            what = f"{len(self.bad)} unreadable source sector(s) copied as zeros" if self.bad else probs[0]["name"]
            return V.verdict(V.PROBLEMS, f"Passed with problems: {what}", details)
        return V.verdict(V.PASSED, "Verified: every block, the partition table, the file systems and the boot "
                                   "configuration checked", details)

    def _result(self, t0) -> dict:
        plan, ss = self.plan, self.plan.sector_size
        for v in self.timings.values():
            if v[1] is None:
                v[1] = time.time()
        return {"kind": "sysclone", "source": _ident(self.src), "target": _ident(self.dst), "plan": plan.to_dict(),
                "layout_before": [{"role": p.role, "start": p.src_start * ss, "size": p.src_sectors * ss,
                                   "fs": p.fs} for p in plan.parts],
                "layout_after": [{"role": p.role, "start": p.start * ss, "size": p.sectors * ss, "fs": p.fs,
                                  "action": p.action, "resize": p.resize} for p in plan.parts],
                "bytes_copied": self.copied, "bytes_written": self.written, "chunks": len(self.chunks),
                "bad_sectors": len(self.bad), "bad_lbas": [{"source": s, "target": d} for s, d in self.bad[:5000]],
                "checks": self.checks, "notes": self.notes, "snapshot": self.snap.volumes if self.snap else [],
                "snapshot_mode": (self.snap.info or {}).get("mode") if self.snap else None,
                "timings": {k: round(v[1] - v[0], 1) for k, v in self.timings.items()},
                "started": t0, "ended": time.time(), "journal": self.journal.id if self.journal else None,
                "target_state": "complete"}

    def _write_report(self, res):
        try:
            from .sysclone_report import write_report
            folder = self.report_dir or (app_dir() / "reports")
            os.makedirs(folder, exist_ok=True)
            name = f"Clone report {time.strftime('%Y-%m-%d %H%M%S')} {self.journal.id if self.journal else ''}.html"
            path = os.path.join(folder, name.strip())
            write_report(path, res)
            res["report"] = path
        except Exception as e:  # noqa: BLE001  a report problem must not hide the verdict
            log.error("sysclone report not written: %s", e, exc_info=True)
            res["report"] = None


def _hash_stream(fh) -> str | None:
    if fh is None:
        return None
    h = hashlib.sha256()
    with fh:
        while True:
            b = fh.read(4 * MiB)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def registry_replacements(plan: Plan) -> list[tuple[bytes, bytes]]:
    """MountedDevices values name partitions by GPT partition GUID ("DMIO:ID:" + GUID) or MBR signature + offset;
    map each source partition to its copy so C: stays C: on the new drive."""
    ss = plan.sector_size
    out = []
    for p in plan.parts:
        if plan.scheme == "GPT":
            if p.src_unique and p.unique:
                out.append((b"DMIO:ID:" + uuid.UUID(p.src_unique).bytes_le, b"DMIO:ID:" + uuid.UUID(p.unique).bytes_le))
        else:
            old = struct.pack("<IQ", int(plan.src_disk_id, 16), p.src_start * ss)
            new = struct.pack("<IQ", int(plan.disk_id, 16), p.start * ss)
            out.append((old, new))
    return out


def bcd_patterns(plan: Plan) -> tuple[list, list, list]:
    """Byte patterns BCD device elements must hold (boot partition, Windows partition) and ones they must not."""
    ss = plan.sector_size
    boot, win = plan.boot_part(), plan.part("windows")
    if plan.scheme == "GPT":
        want_boot = [uuid.UUID(boot.unique).bytes_le]
        want_win = [uuid.UUID(win.unique).bytes_le]
        old = [uuid.UUID(p.src_unique).bytes_le for p in plan.parts if p.src_unique] + \
              [uuid.UUID(plan.src_disk_id).bytes_le]
    else:
        sig = struct.pack("<I", int(plan.disk_id, 16))
        want_boot = [sig, struct.pack("<Q", boot.start * ss)]
        want_win = [sig, struct.pack("<Q", win.start * ss)]
        old = [struct.pack("<I", int(plan.src_disk_id, 16))] if plan.src_disk_id != plan.disk_id else []
    return want_boot, want_win, old


def run_clone(src, dst, plan, layer, prog, **kw) -> dict:
    """Run a clone. Always returns a result dict with result["verify"]; raises SysCloneError only when it stopped
    before anything was written, and Cancelled when cancelled (info["state"] says what the target was left as)."""
    return CloneJob(src, dst, plan, layer, prog, **kw).run()


def summary_lines(res: dict) -> list[str]:
    v = res.get("verify") or V.not_run()
    lines = [v["summary"]]
    if res.get("bytes_copied"):
        lines.append(f"{human_size(res['bytes_copied'])} copied and read back in {res.get('seconds', 0):.0f} s")
    if res.get("bad_sectors"):
        lines.append(f"{res['bad_sectors']} unreadable sector(s) on the source were copied as zeros; they are listed "
                     "in the report.")
    return lines
