"""Secure erase engine: the drive's own erase (ATA Secure Erase, NVMe Sanitize/Format), overwrite of a whole disk,
partition or sector range, free space, and file shredding."""
from __future__ import annotations

import hashlib
import os
import random
import secrets
import struct
import threading
import time
from dataclasses import dataclass, field

from . import device as _device
from .device import Device, DeviceError
from .util import Cancelled, Progress, get_logger, human_size

log = get_logger()

CHUNK = 4 * 1024 * 1024  # 4 MiB writes


@dataclass(frozen=True)
class Method:
    name: str
    passes: tuple  # each pass: bytes pattern, or "random"
    verify: str  # "none" | "last" | "all"
    note: str = ""


_GUTMANN_MID = [b"\x55", b"\xaa", b"\x92\x49\x24", b"\x49\x24\x92", b"\x24\x92\x49"] + \
    [bytes([v * 0x11]) for v in range(16)] + \
    [b"\x92\x49\x24", b"\x49\x24\x92", b"\x24\x92\x49", b"\x6d\xb6\xdb", b"\xb6\xdb\x6d", b"\xdb\x6d\xb6"]

METHODS: dict[str, Method] = {m.name: m for m in [
    Method("Zero fill (1 pass)", (b"\x00",), "none", "Fastest. Fine for reuse inside the business."),
    Method("Random data (1 pass)", ("random",), "none", "One pass of pseudorandom data."),
    Method("NIST 800-88 Clear (1 pass + verify)", (b"\x00",), "last",
           "Modern standard for HDDs. For SSDs prefer the drive's own Secure Erase / Sanitize."),
    Method("HMG IS5 Enhanced (3 pass + verify)", (b"\x00", b"\xff", "random"), "last",
           "UK government legacy standard."),
    Method("DoD 5220.22-M (3 pass + verify)", (b"\x00", b"\xff", "random"), "last",
           "US DoD short method."),
    Method("DoD 5220.22-M ECE (7 pass)", (b"\x00", b"\xff", "random", "random", b"\x00", b"\xff", "random"),
           "last", "Extended 7-pass DoD variant."),
    Method("Schneier (7 pass)", (b"\xff", b"\x00", "random", "random", "random", "random", "random"), "last",
           "Bruce Schneier's 7-pass method."),
    Method("Gutmann (35 pass)", tuple(["random"] * 4 + _GUTMANN_MID + ["random"] * 4), "none",
           "Historic method for old MFM/RLL drives. Very slow; no benefit on modern disks."),
]}


def custom_method(pattern_hex: str, passes: int, verify: bool) -> Method:
    pat = bytes.fromhex(pattern_hex.replace(" ", "")) if pattern_hex.strip() else b"\x00"
    return Method(f"Custom 0x{pat.hex()} x{passes}", tuple([pat] * passes), "last" if verify else "none")


class PassData:
    """Produces the bytes for one pass at any absolute offset (deterministic, so we can verify)."""

    POOL = 32 * 1024 * 1024

    def __init__(self, spec, seed: int):
        self.spec = spec
        self.seed = seed
        if spec != "random":
            self._big = (spec * (CHUNK // len(spec) + 2 * len(spec)))
        else:
            # A 32 MiB seeded random pool per pass; each 4 MiB block is taken from a
            # block-specific offset. Fast (memcpy speed) and reproducible for verify.
            pool = random.Random(seed).randbytes(self.POOL)
            self._pool = pool + pool[:CHUNK]

    def _rand_block(self, blk: int) -> bytes:
        h = hashlib.blake2b(blk.to_bytes(8, "little"), key=self.seed.to_bytes(8, "little"), digest_size=8)
        off = (int.from_bytes(h.digest(), "little") % (self.POOL // 64)) * 64
        return self._pool[off: off + CHUNK]

    def block(self, offset: int, n: int) -> bytes:
        if self.spec == "random":
            out = bytearray()
            pos = offset
            while len(out) < n:
                within = pos % CHUNK
                take = min(CHUNK - within, n - len(out))
                out += self._rand_block(pos // CHUNK)[within: within + take]
                pos += take
            return bytes(out)
        phase = offset % len(self.spec)
        if n + phase <= len(self._big):
            return self._big[phase: phase + n]
        return (self.spec * (n // len(self.spec) + 2))[phase: phase + n]


def wipe_range(dev: Device, start: int, length: int, method: Method, prog: Progress,
               verify_override: str | None = None) -> dict:
    """Overwrite ``length`` bytes at ``start`` on an already-writable device."""
    if not dev.writable:
        raise DeviceError("Device must be opened for writing.")
    ss = dev.sector_size
    if start % ss or length % ss:
        raise DeviceError("Wipe range must be sector aligned.")
    verify = verify_override or method.verify
    npass = len(method.passes)
    total_work = length * npass + (length * (npass if verify == "all" else 1) if verify != "none" else 0)
    prog.reset(total_work, f"{method.name}")
    done_work = 0
    errors: list[int] = []
    seeds = [secrets.randbits(32) for _ in method.passes]

    def write_pass(pi: int):
        nonlocal done_work
        pdata = PassData(method.passes[pi], seeds[pi])
        pos = start
        end = start + length
        while pos < end:
            prog.check()
            n = min(CHUNK - pos % CHUNK, end - pos)  # stay on 4 MiB boundaries (fast RNG path)
            buf = pdata.block(pos, n)
            try:
                dev.write(pos, buf)
            except OSError as e:
                # retry sector-by-sector so one bad sector doesn't stop the wipe
                log.warning("Write error at %d: %s", pos, e)
                for s in range(pos, pos + n, ss):
                    try:
                        dev.write(s, buf[s - pos: s - pos + ss])
                    except OSError:
                        errors.append(s // ss)
            pos += n
            done_work += n
            prog.update(done_work)
        dev.flush()

    def verify_pass(pi: int) -> int:
        nonlocal done_work
        pdata = PassData(method.passes[pi], seeds[pi])
        bad = 0
        pos = start
        end = start + length
        while pos < end:
            prog.check()
            n = min(CHUNK - pos % CHUNK, end - pos)
            try:
                got = dev.read(pos, n)
            except OSError:
                got = b""
            if got != pdata.block(pos, n):
                bad += 1
                log.warning("Verify mismatch in block at %d", pos)
            pos += n
            done_work += n
            prog.update(done_work)
        return bad

    mismatches = 0
    pi = 0
    try:
        for pi in range(npass):
            prog.set_label(f"{method.name} — pass {pi + 1}/{npass}", _pass_desc(method.passes[pi]))
            write_pass(pi)
            if verify == "all" or (verify == "last" and pi == npass - 1):
                prog.set_label(f"{method.name} — verifying pass {pi + 1}/{npass}")
                mismatches += verify_pass(pi)
    except Cancelled as c:
        try:
            dev.flush()
        except OSError:
            pass  # best effort; the stopped report already says part-erased
        # every pass starts at the beginning, so after pass 1 the whole range has been overwritten once
        c.info.update(pass_no=pi + 1, passes=npass, overwritten=min(length, done_work) if pi == 0 else length,
                      length=length, verified=False)
        log.info("Wipe cancelled on %s in pass %d/%d", dev.path, pi + 1, npass)
        raise
    result = {"bytes": length, "passes": npass, "write_errors": len(errors), "bad_lbas": errors[:1000],
              "verify_mismatched_blocks": mismatches, "verified": verify != "none"}
    log.info("Wipe finished on %s [%d +%d]: %s", dev.path, start, length, result)
    return result


def _pass_desc(spec) -> str:
    return "pseudorandom data" if spec == "random" else "pattern 0x" + spec.hex().upper()


def wipe_disk(dev: Device, method: Method, prog: Progress, start_lba: int = 0, sectors: int | None = None) -> dict:
    """Wipe the whole device or an LBA range. Opens the device writable (locks volumes on Windows)."""
    ss = dev.sector_size
    if sectors is None:
        sectors = dev.total_sectors - start_lba
    dev.open(writable=True)
    try:
        return wipe_range(dev, start_lba * ss, sectors * ss, method, prog)
    finally:
        dev.close()


# ---------------------------------------------------------------------------
# Hardware erase: the drive erases itself, including spare flash the host can't address
# ---------------------------------------------------------------------------
NIST_PURGE = "Purge"
NIST_CLEAR = "Clear"
ATA_TEMP_PASSWORD = "SectorSmith"  # set just before Secure Erase, which clears it again
MARKERS = 32  # sectors stamped before a hardware erase and checked after it
MARK_TAG = b"SECTORSMITH-ERASE-MARK"

FROZEN_ADVICE = ("The drive is frozen: the PC's firmware locked its security commands when it started. Put the PC to "
                 "sleep and wake it again, or unplug the drive and plug it back in while Windows is running (a "
                 "SATA dock or hot-swap bay works), then try again. Or erase it with an overwrite instead.")


class DriveFrozen(DeviceError):
    """The drive supports a hardware erase but refuses it until it is power-cycled or replugged."""


class HardwareEraseFailed(DeviceError):
    """The hardware erase didn't complete. ``fallback_ok`` says whether an overwrite can still run safely."""

    def __init__(self, msg: str, fallback_ok: bool = True):
        super().__init__(msg)
        self.fallback_ok = fallback_ok


def nist_category(hardware: bool) -> str:
    """NIST SP 800-88: the drive's own erase or sanitize is Purge, a host overwrite is Clear."""
    return NIST_PURGE if hardware else NIST_CLEAR


def _ata_string(words, a, b) -> str:
    return b"".join(struct.pack(">H", w) for w in words[a:b]).decode("ascii", "replace").strip()


def _ata_minutes(w: int) -> int:
    """Erase time from IDENTIFY word 89/90, in minutes (0 if the drive doesn't say)."""
    if w & 0x8000:  # ACS-3 extended format: bits 14:0 in 2-minute units
        return (w & 0x7FFF) * 2
    return (w & 0xFF) * 2


def parse_ata_identify(data: bytes) -> dict:
    """The parts of ATA IDENTIFY DEVICE (512 bytes) that matter for Secure Erase."""
    if len(data) < 512:
        raise ValueError("IDENTIFY data must be 512 bytes")
    w = struct.unpack("<256H", data[:512])
    sec = w[128]
    cmdset = w[82] if w[82] not in (0, 0xFFFF) else 0
    return {
        "model": _ata_string(w, 27, 47), "serial": _ata_string(w, 10, 20), "firmware": _ata_string(w, 23, 27),
        "checksum_ok": data[510] != 0xA5 or sum(data[:512]) % 256 == 0,
        "security_supported": bool(cmdset & 0x0002 or sec & 0x0001),
        "security_enabled": bool(sec & 0x0002), "locked": bool(sec & 0x0004), "frozen": bool(sec & 0x0008),
        "count_expired": bool(sec & 0x0010), "enhanced_supported": bool(sec & 0x0020),
        "erase_minutes": _ata_minutes(w[89]), "enhanced_minutes": _ata_minutes(w[90]),
        "sanitize_supported": bool(w[59] & 0x1000), "solid_state": w[217] == 1,
    }


def parse_nvme_identify(data: bytes) -> dict:
    """The parts of NVMe Identify Controller (4096 bytes) that matter for Sanitize and Format."""
    if len(data) < 4096:
        raise ValueError("Identify Controller data must be 4096 bytes")
    oacs = struct.unpack_from("<H", data, 256)[0]
    sanicap = struct.unpack_from("<I", data, 328)[0]
    fna = data[524]
    return {
        "model": data[24:64].decode("ascii", "replace").strip(), "serial": data[4:24].decode("ascii", "replace").strip(),
        "firmware": data[64:72].decode("ascii", "replace").strip(),
        "format_supported": bool(oacs & 0x0002), "sanitize_crypto": bool(sanicap & 0x1),
        "sanitize_block": bool(sanicap & 0x2), "sanitize_overwrite": bool(sanicap & 0x4),
        "format_all_namespaces": bool(fna & 0x1), "crypto_erase_supported": bool(fna & 0x4),
    }


@dataclass
class HardwarePlan:
    """What the drive's own erase would do, or why it can't run."""
    available: bool
    kind: str = ""     # "ata" or "nvme"
    command: str = ""  # ata: "enhanced" / "normal"; nvme: "crypto" / "block" / "format"
    name: str = ""
    frozen: bool = False
    reason: str = ""
    minutes: int = 0   # the drive's own estimate (ATA), 0 when unknown
    info: dict = field(default_factory=dict)

    @property
    def nist(self) -> str:
        return nist_category(self.available)


def plan_from_ata(ident: dict) -> HardwarePlan:
    if not ident["security_supported"]:
        return HardwarePlan(False, "ata", reason="This drive doesn't support ATA Secure Erase.", info=ident)
    if ident["locked"]:
        return HardwarePlan(False, "ata", reason="The drive is locked with a password set elsewhere. Unlock it "
                                                 "with that password first.", info=ident)
    if ident["security_enabled"]:
        return HardwarePlan(False, "ata", reason="A drive password is already set, so Secure Erase would need it.",
                            info=ident)
    if ident["count_expired"]:
        return HardwarePlan(False, "ata", frozen=True, info=ident,
                            reason="The drive has used up its password attempts. Turn the PC off and on, or "
                                   "unplug the drive and plug it back in, then try again.")
    if ident["frozen"]:
        return HardwarePlan(False, "ata", frozen=True, reason=FROZEN_ADVICE, info=ident)
    if ident["enhanced_supported"]:
        return HardwarePlan(True, "ata", "enhanced", "ATA Secure Erase (enhanced)",
                            minutes=ident["enhanced_minutes"], info=ident)
    return HardwarePlan(True, "ata", "normal", "ATA Secure Erase", minutes=ident["erase_minutes"], info=ident)


def plan_from_nvme(ident: dict) -> HardwarePlan:
    if ident["sanitize_crypto"]:
        return HardwarePlan(True, "nvme", "crypto", "NVMe Sanitize (crypto erase)", info=ident)
    if ident["sanitize_block"]:
        return HardwarePlan(True, "nvme", "block", "NVMe Sanitize (block erase)", info=ident)
    if ident["format_supported"]:
        return HardwarePlan(True, "nvme", "format", "NVMe Format with secure erase", info=ident)
    return HardwarePlan(False, "nvme", reason="This NVMe drive supports neither Sanitize nor Format.", info=ident)


def hardware_plan(dev: Device, pt: _device.PassThrough | None = None) -> HardwarePlan:
    """Ask the drive what it can do. Read-only: sends IDENTIFY only."""
    if dev.is_image:
        return HardwarePlan(False, reason="Image files have no built-in erase.")
    if dev.is_system:
        return HardwarePlan(False, reason="This is the system disk. It can't be erased while Windows runs from it.")
    bus = (dev.bus or "").upper()
    kind = "nvme" if bus == "NVME" else "ata" if bus in ("SATA", "ATA") else ""
    if not kind:
        return HardwarePlan(False, reason=f"The built-in erase needs a SATA or NVMe drive connected directly; this "
                                          f"one is on {dev.bus or 'an unknown bus'}.")
    own = pt is None
    try:
        if own:
            pt = _device.open_passthrough(dev)
        if kind == "nvme":
            return plan_from_nvme(parse_nvme_identify(pt.nvme_identify_controller()))
        return plan_from_ata(parse_ata_identify(pt.ata_identify()))
    except (OSError, DeviceError, ValueError) as e:
        return HardwarePlan(False, kind, reason=f"The drive didn't answer the capability check ({e}). A RAID or USB "
                                                "controller in between can block it.")
    finally:
        if own and pt is not None:
            pt.close()


def _password_block(enhanced: bool = False) -> bytes:
    """Data for SECURITY SET PASSWORD / ERASE UNIT / DISABLE PASSWORD: user password, high security."""
    word0 = 0x0002 if enhanced else 0  # bit 0 = 0: user password; bit 1: enhanced erase
    return struct.pack("<H", word0) + ATA_TEMP_PASSWORD.encode().ljust(32, b"\0") + bytes(512 - 34)


def _ata_erase(pt: _device.PassThrough, plan: HardwarePlan, timeout: int):
    try:
        pt.ata_command(_device.ATA_SECURITY_SET_PASSWORD, data_out=_password_block())
    except OSError as e:
        raise HardwareEraseFailed(f"The drive refused to set a temporary password: {e}") from e
    try:
        pt.ata_command(_device.ATA_SECURITY_ERASE_PREPARE)
        pt.ata_command(_device.ATA_SECURITY_ERASE_UNIT, data_out=_password_block(plan.command == "enhanced"),
                       timeout=timeout)
    except OSError as e:
        try:
            pt.ata_command(_device.ATA_SECURITY_DISABLE_PASSWORD, data_out=_password_block())
        except OSError:
            raise HardwareEraseFailed(f"Secure Erase failed ({e}) and the drive may still have the temporary "
                                      f"password \"{ATA_TEMP_PASSWORD}\". Unlock it with that password before "
                                      "using it.", fallback_ok=False) from e
        raise HardwareEraseFailed(f"Secure Erase failed: {e}") from e
    after = parse_ata_identify(pt.ata_identify())
    if after["security_enabled"]:  # a finished erase disables security; make sure no password is left
        try:
            pt.ata_command(_device.ATA_SECURITY_DISABLE_PASSWORD, data_out=_password_block())
        except OSError as e:
            raise HardwareEraseFailed(f"The erase ran but the temporary password \"{ATA_TEMP_PASSWORD}\" is still "
                                      f"set ({e}). Unlock the drive with it.", fallback_ok=False) from e


def _marker_lbas(total: int, n: int = MARKERS) -> list[int]:
    picks = {0, total - 1} | {total * i // n for i in range(n)}
    rng = secrets.SystemRandom()
    while len(picks) < min(n, total):
        picks.add(rng.randrange(total))
    return sorted(picks)[:n]


def _write_markers(dev: Device) -> dict[int, bytes]:
    ss = dev.sector_size
    marks = {}
    for lba in _marker_lbas(dev.total_sectors):
        data = (MARK_TAG + lba.to_bytes(8, "little") + secrets.token_bytes(ss))[:ss]
        dev.write(lba * ss, data)
        marks[lba] = data
    dev.flush()
    return marks


def _check_markers(dev: Device, marks: dict[int, bytes]) -> tuple[int, str]:
    """(markers still readable, what the sampled sectors read as now)."""
    survived = 0
    kinds = set()
    for lba, data in marks.items():
        try:
            got = dev.read(lba * dev.sector_size, dev.sector_size)
        except OSError:
            kinds.add("unreadable")
            continue
        if got == data or MARK_TAG in got:
            survived += 1
        kinds.add("zeros" if got.count(0) == len(got) else "ones" if got.count(0xFF) == len(got) else "other data")
    return survived, " and ".join(sorted(kinds))


def hardware_erase(dev: Device, plan: HardwarePlan, prog: Progress, pt: _device.PassThrough | None = None) -> dict:
    """Run the drive's own erase on a whole drive and check it by sampling. Can't be stopped once sent."""
    if not plan.available:
        raise HardwareEraseFailed(plan.reason or "No hardware erase available.")
    dev.open(writable=True)  # refuses the system disk; locks and dismounts its volumes on Windows
    own = pt is None
    try:
        prog.reset(1, plan.name)
        prog.set_label(plan.name, "Marking sample sectors so the result can be checked")
        prog.check()
        marks = _write_markers(dev)
        if own:
            pt = _device.open_passthrough(dev)
        if plan.minutes:
            est = plan.minutes * 60
        elif plan.kind == "ata":
            est = max(60, dev.usable_size // (200 * 1024 * 1024))  # roughly what an overwrite at 200 MB/s takes
        else:
            est = 120  # NVMe crypto and block erase usually take seconds to a few minutes
        timeout = max(600, est * 3) if plan.minutes else 12 * 3600
        try:
            prog.check()  # last point where Cancel stops it; only the marked sectors have changed
        except Cancelled as c:
            c.info.update(pass_no=1, passes=1, overwritten=len(marks) * dev.sector_size, length=dev.usable_size,
                          verified=False, hardware=True)
            raise
        prog.reset(1000, plan.name)
        prog.set_label(plan.name, "The drive is erasing itself. This can't be stopped once started.")
        log.info("Hardware erase %s on %s (%s), estimate %ss", plan.name, dev.path, dev.model, est)
        failure: list[Exception] = []

        def send():
            try:
                if plan.kind == "ata":
                    _ata_erase(pt, plan, timeout)
                else:
                    try:
                        pt.nvme_sanitize(plan.command, timeout)
                    except OSError as e:
                        raise HardwareEraseFailed(f"The drive refused {plan.name}: {e}") from e
            except Exception as e:  # noqa: BLE001  handed back to the job thread below
                failure.append(e)
        t0 = time.monotonic()
        th = threading.Thread(target=send, daemon=True, name="hardware-erase")
        th.start()
        while th.is_alive():
            th.join(0.25)
            prog.update(min(990, int(1000 * (time.monotonic() - t0) / max(1, est))))
        if failure:
            raise failure[0]
        prog.update(1000)
        prog.set_label("Checking the erase", "Reading back the marked sectors")
        survived, now_reads = _check_markers(dev, marks)
    finally:
        if own and pt is not None:
            pt.close()
        dev.close()
    ok = survived == 0
    verification = (f"{len(marks) - survived} of {len(marks)} sampled sectors no longer hold the marks written "
                    f"before the erase (they now read as {now_reads})")
    res = {"bytes": dev.usable_size, "passes": 1, "write_errors": 0, "bad_lbas": [],
           "verify_mismatched_blocks": survived, "verified": True, "verify_ok": ok, "verification": verification,
           "method": plan.name, "hardware": True, "nist": NIST_PURGE, "fallback_reason": "",
           "drive_firmware": plan.info.get("firmware", "")}
    log.info("Hardware erase finished on %s: %s", dev.path, res)
    return res


def erase_disk(dev: Device, method: Method, prog: Progress, start_lba: int = 0, sectors: int | None = None,
               hardware: bool = True, pt: _device.PassThrough | None = None,
               plan: HardwarePlan | None = None) -> dict:
    """Erase a whole drive with its own erase when it can, else overwrite with ``method``.

    Raises DriveFrozen when the drive could do it but is frozen (the operator sleeps or replugs, or picks overwrite).
    Partitions and sector ranges are always overwritten. The result says which happened and why.
    """
    whole = start_lba == 0 and (sectors is None or sectors >= dev.total_sectors)
    reason = ""
    if not hardware:
        reason = "Overwrite chosen."
    elif not whole:
        reason = "The built-in erase works on whole drives only, so this partition was overwritten."
    else:
        if dev.is_system:  # same refusal as opening it for writing, before any command reaches the drive
            raise DeviceError("Refusing to open the system disk for writing.")
        plan = plan or hardware_plan(dev, pt)
        if plan.frozen:
            raise DriveFrozen(plan.reason)
        if plan.available:
            try:
                return hardware_erase(dev, plan, prog, pt)
            except HardwareEraseFailed as e:
                if not e.fallback_ok:
                    raise
                reason = f"{e} Overwritten instead."
                log.warning("Hardware erase failed on %s, falling back to overwrite: %s", dev.path, e)
        else:
            reason = f"{plan.reason} Overwritten instead."
    res = wipe_disk(dev, method, prog, start_lba=start_lba, sectors=sectors)
    clean = res["write_errors"] == 0 and res["verify_mismatched_blocks"] == 0
    if res["verified"]:
        verification = ("Read-back of the last pass matched" if clean else
                        f"Read-back found {res['verify_mismatched_blocks']} mismatched block(s) and "
                        f"{res['write_errors']} unwritable sector(s)")
    else:
        verification = "Not verified (this method has no read-back pass)"
    res.update(method=method.name, hardware=False, nist=NIST_CLEAR, fallback_reason=reason,
               verification=f"{verification}, {human_size(res['bytes'])} overwritten",
               verify_ok=bool(res["verified"] and clean))
    return res


# ---------------------------------------------------------------------------
# File-system level wipes (no raw access needed)
# ---------------------------------------------------------------------------
def wipe_free_space(folder: str, method: Method, prog: Progress) -> dict:
    """Fill a volume's free space with the wipe patterns via a temp file, then delete it."""
    import shutil
    free = shutil.disk_usage(folder).free
    fname = os.path.join(folder, f"~SectorSmith_freespace_{secrets.token_hex(4)}.tmp")
    npass = len(method.passes)
    prog.reset(max(1, free) * npass, f"Free-space wipe: {method.name}")
    written_total = 0
    seeds = [secrets.randbits(32) for _ in method.passes]
    filled = 0
    try:
        with open(fname, "wb", buffering=0) as f:
            for pi, spec in enumerate(method.passes):
                prog.set_label(f"Free-space wipe — pass {pi + 1}/{npass}", _pass_desc(spec))
                pdata = PassData(spec, seeds[pi])
                f.seek(0)
                pos = 0
                limit = filled if pi > 0 else None
                while limit is None or pos < limit:
                    prog.check()
                    n = CHUNK if limit is None else min(CHUNK, limit - pos)
                    try:
                        w = f.write(pdata.block(pos, n))
                    except OSError:
                        # disk full: try smaller writes to fill the tail
                        w = 0
                        for small in (65536, 4096, 512):
                            try:
                                w = f.write(pdata.block(pos, small))
                                break
                            except OSError:
                                continue
                        if not w:
                            break
                    pos += w
                    written_total += w
                    prog.update(written_total)
                f.flush()
                os.fsync(f.fileno())
                if pi == 0:
                    filled = pos
                    prog.reset(max(1, filled) * npass, prog.label)
                    written_total = filled
                    prog.update(written_total)
    except Cancelled:
        raise
    finally:
        try:
            os.remove(fname)
        except OSError:
            pass
    return {"bytes": filled, "passes": npass}


def shred_file(path: str, method: Method, prog: Progress | None = None) -> None:
    """Overwrite a file in place, rename it, truncate and delete it."""
    size = os.path.getsize(path)
    with open(path, "r+b", buffering=0) as f:
        for spec in method.passes:
            pdata = None if spec == "random" else PassData(spec, 0)
            f.seek(0)
            pos = 0
            while pos < size:
                if prog:
                    prog.check()
                n = min(CHUNK, size - pos)
                f.write(os.urandom(n) if pdata is None else pdata.block(pos, n))
                pos += n
            f.flush()
            os.fsync(f.fileno())
        f.truncate(0)
    d = os.path.dirname(path)
    newname = os.path.join(d, secrets.token_hex(8))
    os.replace(path, newname)
    os.remove(newname)


def shred_paths(paths: list[str], method: Method, prog: Progress) -> dict:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, fnames in os.walk(p):
                files += [os.path.join(root, n) for n in fnames]
        elif os.path.isfile(p):
            files.append(p)
    total = sum(os.path.getsize(f) for f in files) * len(method.passes) or 1
    prog.reset(total, f"Shredding {len(files)} file(s)")
    done = 0
    failed = []
    shredded = 0
    for f in files:
        try:
            prog.check()
            prog.set_detail(f)
            sz = os.path.getsize(f)
            shred_file(f, method, prog)
            shredded += 1
            done += sz * len(method.passes)
            prog.update(done)
        except OSError as e:
            failed.append(f"{f}: {e}")
        except Cancelled as c:
            c.info.update(shredded=shredded, total=len(files), current=f)
            raise
    for p in paths:  # remove now-empty folders, deepest first
        if os.path.isdir(p):
            for root, dirs, _ in sorted(os.walk(p), key=lambda t: -len(t[0])):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    return {"files": len(files) - len(failed), "failed": failed}
