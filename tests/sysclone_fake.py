"""Test helpers for "Clone this PC": system disk images (GPT/UEFI and MBR/BIOS, 512 or 4096-byte sectors) laid out
like a real Windows install, and a fake SystemLayer that snapshots, mounts and runs bcdboot on image files.

Linux only; needs sgdisk, sfdisk, mkfs.vfat, mtools, mkntfs and ntfs-3g (mounted as root, or with sudo -n).
"""
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sectorsmith import partitions, sysclone  # noqa: E402
from sectorsmith.device import open_image  # noqa: E402
from sectorsmith.ntfs import NTFSVolume  # noqa: E402
from sectorsmith.util import Progress  # noqa: E402

MiB = 1024 * 1024
ENV = dict(os.environ, MTOOLS_SKIP_CHECK="1")
SUDO = [] if os.geteuid() == 0 else ["sudo", "-n"]

WIN_FILES = {  # path inside the Windows partition: size
    "Windows/System32/config/SYSTEM": 3 * MiB + 4096,
    "Windows/System32/config/SOFTWARE": 5 * MiB,
    "Windows/System32/config/DEFAULT": 512 * 1024,
    "Windows/System32/ntoskrnl.exe": 2 * MiB + 77,
    "Windows/System32/hal.dll": 300 * 1024,
    "Windows/System32/winload.efi": 1 * MiB + 5,
    "Windows/System32/drivers/ntfs.sys": 900 * 1024,
    "Windows/explorer.exe": 1500 * 1024,
    "Users/Bob/Documents/report.docx": 700 * 1024,
    "Users/Bob/Pictures/holiday.jpg": 6 * MiB,
    "ProgramData/app/cache.bin": 12 * MiB,
}


def run(*cmd, env=None, check=True, inp=None):
    r = subprocess.run(cmd, capture_output=True, env=env or ENV, input=inp)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))} failed: {r.stderr.decode(errors='replace')[-400:]}")
    return r


def _data(name, size):
    """Deterministic file contents, so the tests know what every file must hold."""
    seed = hashlib.sha256(name.encode()).digest()
    out = bytearray()
    block = 0
    while len(out) < size:
        out += hashlib.sha256(seed + block.to_bytes(8, "little")).digest() * 128
        block += 1
    return bytes(out[:size])


def _mk_ntfs(path, size, ss, hidden, label, files, tail_file=None):
    with open(path, "wb") as f:
        f.truncate(size)
    run("mkntfs", "-F", "-Q", "-s", str(ss), "-c", "4096", "-p", str(hidden), "-L", label, path)
    mnt = tempfile.mkdtemp(prefix="ssmnt_")
    try:
        run(*SUDO, "ntfs-3g", path, mnt)
        try:
            for rel, n in files.items():
                p = os.path.join(mnt, *rel.split("/"))
                run(*SUDO, "mkdir", "-p", os.path.dirname(p))
                tmp = os.path.join(os.path.dirname(path), "_f.bin")
                with open(tmp, "wb") as f:
                    f.write(_data(rel, n))
                run(*SUDO, "cp", tmp, p)
                os.remove(tmp)
        finally:
            run(*SUDO, "umount", mnt)
    finally:
        os.rmdir(mnt)


def _mk_fat(path, size, ss, hidden, label, files):
    with open(path, "wb") as f:
        f.truncate(size)
    run("mkfs.vfat", "-F", "32", "-S", str(ss), "-s", "1", "-n", label, "-h", str(hidden), path)
    dirs = set()
    for rel in files:
        parts = rel.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    for d in sorted(dirs):
        run("mmd", "-i", path, "::/" + d)
    for rel, n in files.items():
        tmp = path + ".f"
        with open(tmp, "wb") as f:
            f.write(_data(rel, n))
        run("mcopy", "-i", path, tmp, "::/" + rel)
        os.remove(tmp)


def _place(disk, part_img, offset):
    with open(part_img, "rb") as s, open(disk, "r+b") as d:
        d.seek(offset)
        while True:
            b = s.read(8 * MiB)
            if not b:
                break
            d.write(b)
    os.remove(part_img)


def build_system_disk(path, scheme="GPT", ss=512, disk_mib=320, win_mib=200, esp_mib=40, rec_mib=40,
                      win_files=None):
    """A Windows-like system disk image. GPT: ESP (FAT32), MSR, Windows (NTFS), Recovery (NTFS) at the end.
    MBR: System Reserved (NTFS, active), Windows (NTFS), Recovery (type 0x27). Returns a dict with the layout."""
    win_files = WIN_FILES if win_files is None else win_files
    with open(path, "wb") as f:
        f.truncate(disk_mib * MiB)
    total = disk_mib * MiB // ss
    a = MiB // ss
    if scheme == "GPT":
        spec = [("esp", sysclone.GUID_ESP, esp_mib, "EFI system partition", 0),
                ("msr", sysclone.GUID_MSR, 16, "Microsoft reserved partition", 0),
                ("windows", sysclone.GUID_BASIC, win_mib, "Basic data partition", 0),
                ("recovery", sysclone.GUID_RECOVERY, rec_mib, "", 0x8000000000000001)]
        parts, cur = [], a
        for role, t, mib, name, attrs in spec:
            parts.append(sysclone.PartPlan(len(parts), role, "", t, 0, 0, cur, mib * MiB // ss, "raw",
                                           unique=str(uuid.uuid4()), attrs=attrs, name=name))
            cur += mib * MiB // ss
        plan = sysclone.Plan("GPT", ss, total, str(uuid.uuid4()), "", parts)
        dev = open_image(path, ss)
        dev.open(writable=True)
        sysclone.write_table(dev, plan)
        dev.close()
    else:
        spec = [("sysres", 0x07, 50, True), ("windows", 0x07, win_mib, False), ("recovery", 0x27, rec_mib, False)]
        parts, cur = [], a
        mbr = bytearray(512)
        mbr[:440] = b"\xfa\x33\xc0\x8e\xd0\xbc\x00\x7c" + bytes(432)   # stand-in boot code
        struct.pack_into("<I", mbr, 440, 0x1234ABCD)
        for i, (role, t, mib, act) in enumerate(spec):
            n = mib * MiB // ss
            struct.pack_into("<B3sB3sII", mbr, 446 + 16 * i, 0x80 if act else 0, b"\xfe\xff\xff", t, b"\xfe\xff\xff",
                             cur, n)
            parts.append(sysclone.PartPlan(i, role, "", f"0x{t:02X}", 0, 0, cur, n, "raw", bootable=act))
            cur += n
        mbr[510:512] = b"\x55\xaa"
        with open(path, "r+b") as f:
            f.write(bytes(mbr))
    w = os.path.dirname(path)
    for p in parts:
        size, off = p.sectors * ss, p.start * ss
        tmp = os.path.join(w, f"_part{p.src_slot}.img")
        if p.role == "esp":
            _mk_fat(tmp, size, ss, p.start, "SYSTEM", {"EFI/Microsoft/Boot/bootmgfw.efi": 1500 * 1024,
                                                       "EFI/Boot/bootx64.efi": 1500 * 1024})
        elif p.role == "windows":
            _mk_ntfs(tmp, size, ss, p.start, "Windows", win_files)
        elif p.role == "recovery":
            _mk_ntfs(tmp, size, ss, p.start, "Recovery", {"Recovery/WindowsRE/Winre.wim": 9 * MiB})
        elif p.role == "sysres":
            _mk_ntfs(tmp, size, ss, p.start, "System Reserved", {"Boot/BCD": 32 * 1024, "bootmgr": 400 * 1024})
        else:
            continue
        _place(path, tmp, off)
    return {"path": path, "scheme": scheme, "ss": ss, "parts": [(p.role, p.start, p.sectors) for p in parts]}


def blank(path, mib, fill=None):
    with open(path, "wb") as f:
        if fill is None:
            f.truncate(mib * MiB)
        else:
            block = (fill * (MiB // len(fill) + 1))[:MiB]
            for _ in range(mib):
                f.write(block)
    return path


def extract(img, start_lba, sectors, ss, out):
    with open(img, "rb") as f, open(out, "wb") as o:
        f.seek(start_lba * ss)
        left = sectors * ss
        while left:
            b = f.read(min(left, 8 * MiB))
            o.write(b)
            left -= len(b)
    return out


def ntfs_file(dev, base, relpath):
    """Bytes of a file in the NTFS volume at ``base`` of ``dev`` (via SectorSmith's own MFT parser)."""
    vol = NTFSVolume(dev, base)
    vol.scan(Progress(1))
    want = "\\" + relpath.replace("/", "\\").strip("\\")
    folder, name = want.rsplit("\\", 1)
    for e in vol.entries.values():
        if not e.deleted and not e.is_dir and e.name.lower() == name.lower() and e.path.lower() == folder.lower():
            out = tempfile.mktemp(prefix="ssf_")
            vol.recover(e, out)
            with open(out, "rb") as f:
                data = f.read()
            os.remove(out)
            return data
    return None


# ------------------------------------------------------------------------------------------------- fake layer
class FakeLayer(sysclone.SystemLayer):
    name = "fake"
    store: dict = {}   # snapshot set id -> files, shared like Windows' own shadow copies

    def __init__(self, work, admin=True, power=None, vss=(True, "Running"), flags=None, bitlocker=None,
                 usage=None, free=None, bad_lbas=(), extend_ok=True, bcd_wrong=False, windows_offset=None,
                 letters=None):
        self.work = work
        self.admin, self.pw, self.vss = admin, power or {"ac": True, "percent": 100}, vss
        self.flags, self.bitlocker = flags or {}, bitlocker or {}
        self.usage = usage or {"mounted": [], "busy": [], "pagefile": []}
        self.free = free
        self.bad_lbas = set(bad_lbas)
        self.extend_ok, self.bcd_wrong = extend_ok, bcd_wrong
        self.windows_offset = windows_offset
        self.letters = letters
        self.awake = []
        self.calls = []
        self.bcd = {}
        self.alive_flag = True
        self.deleted = []
        self.src = None

    def is_admin(self):
        return self.admin

    def power(self):
        return self.pw

    def keep_awake(self, on):
        self.awake.append(on)
        return True

    def vss_status(self):
        return self.vss

    def system_disk(self, devices):
        return devices[0] if devices else None

    def disk_flags(self, dev):
        return self.flags

    def volumes(self, dev):
        pt = partitions.read_partition_table(dev)
        ss = dev.sector_size
        ntfs = [p for p in pt.partitions if p.fs in ("NTFS", "BitLocker")]
        win_off = self.windows_offset
        if win_off is None:
            cands = [p for p in ntfs if p.type_id in (sysclone.GUID_BASIC, "0x07")]
            win_off = max(cands, key=lambda p: p.sectors).start_lba * ss if cands else None
        out = []
        for p in ntfs:
            off = p.start_lba * ss
            win = off == win_off
            out.append({"offset": off, "length": p.sectors * ss, "guid": f"vol{p.index}",
                        "letters": (self.letters or {}).get(off, ["C:\\"] if win else []), "windows": win,
                        "bitlocker": self.bitlocker.get(off, "none"),
                        "free": (self.free if self.free is not None else 50 * 1024 ** 3) if win else None,
                        "size": p.sectors * ss})
        return out

    def target_usage(self, dev):
        return self.usage

    # -- snapshots -----------------------------------------------------------------------------------------
    def _reader(self, path, offset, ss):
        r = open_image(path, ss)
        bad = {b * ss - offset for b in self.bad_lbas}
        if bad:
            orig = r._raw_read

            def flaky(off, n):
                if any(off <= b < off + n for b in bad):
                    raise OSError(23, "Data error (cyclic redundancy check)")
                return orig(off, n)
            r._raw_read = flaky
        return r

    def create_snapshot(self, dev, volumes, on_id):
        self.src = dev
        sid = str(uuid.uuid4())
        on_id({"set_id": sid, "mode": "fake set"})
        files, overlay = [], []
        for i, v in enumerate(volumes):
            p = os.path.join(self.work, f"snap_{sid[:8]}_{i}.bin")
            with open(dev.path, "rb") as s, open(p, "wb") as d:
                s.seek(v["offset"])
                left = v["length"]
                while left:
                    b = s.read(min(left, 8 * MiB))
                    d.write(b)
                    left -= len(b)
            files.append((v["offset"], v["length"], p))
            overlay.append((v["offset"], v["length"], self._reader(p, v["offset"], dev.sector_size)))
        FakeLayer.store[sid] = files
        info = {"set_id": sid, "mode": "fake set", "files": files}
        return sysclone.Snapshot(info, overlay, [f"vol@{o >> 20}MiB" for o, _n, _p in files],
                                 release=lambda: self.delete_snapshot(info), alive=lambda: self.alive_flag)

    def attach_snapshot(self, dev, info):
        self.src = dev
        files = FakeLayer.store.get(info.get("set_id"))
        if not files or not all(os.path.exists(p) for _o, _n, p in files):
            return None
        overlay = [(o, n, self._reader(p, o, dev.sector_size)) for o, n, p in files]
        return sysclone.Snapshot(info, overlay, [], release=lambda: self.delete_snapshot(info),
                                 alive=lambda: self.alive_flag)

    def delete_snapshot(self, info):
        for _o, _n, p in FakeLayer.store.pop(info.get("set_id"), []):
            try:
                os.remove(p)
            except OSError:
                pass
        self.deleted.append(info.get("set_id"))

    # -- target fix-up -------------------------------------------------------------------------------------
    def mount_target(self, dst, plan):
        self.calls.append(("mount", dst.path))
        return {"dst": dst.path, "ss": plan.sector_size, "letters": {"boot": "S", "windows": "W"}}

    def open_source_file(self, snap, part, relpath):
        data = ntfs_file(self.src, part.src_start * self.src.sector_size, relpath)
        return io.BytesIO(data) if data is not None else None

    def open_target_file(self, ctx, part, relpath):
        dev = open_image(ctx["dst"], ctx["ss"])
        try:
            data = ntfs_file(dev, part.start * ctx["ss"], relpath)
        finally:
            dev.close()
        return io.BytesIO(data) if data is not None else None

    @staticmethod
    def device_blob(plan, part, old=False):
        """Stand-in for a BCD device element: the bytes that identify a partition (as the real ones hold)."""
        ss = plan.sector_size
        if plan.scheme == "GPT":
            g = part.src_unique if old else part.unique
            d = plan.src_disk_id if old else plan.disk_id
            return bytes(16) + struct.pack("<I", 6) + bytes(12) + uuid.UUID(g).bytes_le + uuid.UUID(d).bytes_le
        sig = int(plan.src_disk_id if old else plan.disk_id, 16)
        start = part.src_start if old else part.start
        return bytes(16) + struct.pack("<I", 6) + bytes(12) + struct.pack("<Q", start * ss) + bytes(8) + \
            struct.pack("<I", sig)

    def bcdboot(self, ctx, plan):
        boot, win = plan.boot_part(), plan.part("windows")
        objs = [{"kind": "bootmgr", "elements": {"11000001": self.device_blob(plan, boot, self.bcd_wrong).hex()}},
                {"kind": "osloader", "elements": {"11000001": self.device_blob(plan, win, self.bcd_wrong).hex(),
                                                  "21000001": self.device_blob(plan, win, self.bcd_wrong).hex()}}]
        body = json.dumps(objs).encode()
        if plan.scheme == "GPT":
            img = f"{ctx['dst']}@@{boot.start * plan.sector_size}"
            for d in ("::/EFI", "::/EFI/Microsoft", "::/EFI/Microsoft/Boot"):
                run("mmd", "-i", img, d, check=False)
            tmp = os.path.join(self.work, "bcd.json")
            with open(tmp, "wb") as f:
                f.write(body)
            run("mcopy", "-o", "-i", img, tmp, "::/EFI/Microsoft/Boot/BCD")
            os.remove(tmp)
        else:
            self.bcd[ctx["dst"]] = body
        self.calls.append(("bcdboot", plan.firmware))
        return True, "Boot files successfully created."

    def read_bcd(self, ctx, plan):
        if plan.scheme == "GPT":
            boot = plan.boot_part()
            r = run("mtype", "-i", f"{ctx['dst']}@@{boot.start * plan.sector_size}", "::/EFI/Microsoft/Boot/BCD")
            body = r.stdout
        else:
            body = self.bcd[ctx["dst"]]
        return [{"kind": o["kind"], "elements": {k: bytes.fromhex(v) for k, v in o["elements"].items()}}
                for o in json.loads(body)]

    def patch_registry(self, ctx, plan, replacements, bitlocker):
        self.calls.append(("registry", len(replacements), bitlocker))
        return True, [f"MountedDevices: {len(replacements)} partition id(s) mapped to the new drive"]

    def extend_filesystem(self, ctx, plan):
        self.calls.append(("extend",))
        return (True, "DiskPart successfully extended the file system.") if self.extend_ok else \
            (False, "Virtual Disk Service error: The volume is not extendable.")

    def unmount_target(self, ctx):
        self.calls.append(("unmount", ctx["dst"]))


def cleanup(*paths):
    for p in paths:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
