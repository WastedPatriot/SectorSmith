"""Secure erase engine: whole disk, partition / sector range, free space, and file shredding."""
from __future__ import annotations

import hashlib
import os
import random
import secrets
from dataclasses import dataclass

from .device import Device, DeviceError
from .util import Cancelled, Progress, get_logger

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
    for pi in range(npass):
        prog.set_label(f"{method.name} — pass {pi + 1}/{npass}", _pass_desc(method.passes[pi]))
        write_pass(pi)
        if verify == "all" or (verify == "last" and pi == npass - 1):
            prog.set_label(f"{method.name} — verifying pass {pi + 1}/{npass}")
            mismatches += verify_pass(pi)
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
    for f in files:
        prog.check()
        prog.set_detail(f)
        try:
            sz = os.path.getsize(f)
            shred_file(f, method, prog)
            done += sz * len(method.passes)
            prog.update(done)
        except OSError as e:
            failed.append(f"{f}: {e}")
    for p in paths:  # remove now-empty folders, deepest first
        if os.path.isdir(p):
            for root, dirs, _ in sorted(os.walk(p), key=lambda t: -len(t[0])):
                try:
                    os.rmdir(root)
                except OSError:
                    pass
    return {"files": len(files) - len(failed), "failed": failed}
