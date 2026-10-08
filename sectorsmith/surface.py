"""Surface (bad sector) scanning, bad-sector repair, and sector-level imaging / cloning."""
from __future__ import annotations

import hashlib
import os
import struct
import time
import uuid

from .device import Device, DeviceError, open_image
from .util import Progress, get_logger

log = get_logger()

# block status classes, from best to worst
CLASSES = ["<25ms", "<75ms", "<200ms", "<600ms", ">=600ms", "bad"]
THRESHOLDS_MS = [25, 75, 200, 600]


def classify(ms: float) -> int:
    for i, t in enumerate(THRESHOLDS_MS):
        if ms < t:
            return i
    return 4


def surface_scan(dev: Device, prog: Progress, start_lba: int = 0, sectors: int | None = None,
                 block_sectors: int = 2048, on_block=None, repair: bool = False) -> dict:
    """Read every sector, timing 1 MiB blocks. Errored blocks are re-read per sector.

    ``on_block(block_index, total_blocks, class_index, ms)`` is called for each block.
    With ``repair=True`` unreadable sectors are overwritten with zeros (forces the drive to
    reallocate them) and re-read — the data in those sectors is already lost.
    """
    ss = dev.sector_size
    if sectors is None:
        sectors = dev.total_sectors - start_lba
    if repair:
        dev.open(writable=True)
    else:
        dev.open(writable=False)
    total_blocks = -(-sectors // block_sectors)
    prog.reset(sectors * ss, "Surface scan" + (" + repair" if repair else ""))
    counts = [0] * len(CLASSES)
    bad: list[int] = []
    repaired: list[int] = []
    try:
        for b in range(total_blocks):
            prog.check()
            lba = start_lba + b * block_sectors
            n = min(block_sectors, start_lba + sectors - lba)
            t0 = time.perf_counter()
            err = False
            try:
                data = dev.read_sectors(lba, n)
                if len(data) < n * ss:
                    err = True
            except OSError:
                err = True
            ms = (time.perf_counter() - t0) * 1000
            if err:
                cls = 5
                for s in range(lba, lba + n):
                    prog.check()
                    try:
                        if len(dev.read_sectors(s, 1)) == ss:
                            continue
                    except OSError:
                        pass
                    bad.append(s)
                    prog.set_detail(f"Bad sector at LBA {s}")
                    if repair:
                        try:
                            dev.write_sectors(s, bytes(ss))
                            dev.flush()
                            if len(dev.read_sectors(s, 1)) == ss:
                                repaired.append(s)
                        except OSError:
                            pass
                if not any(lba <= x < lba + n for x in bad[-n:]):
                    cls = classify(ms)  # transient error, re-read was fine
            else:
                cls = classify(ms)
            counts[cls] += 1
            if on_block:
                on_block(b, total_blocks, cls, ms)
            prog.update((lba - start_lba + n) * ss)
    finally:
        dev.close()
    res = {"blocks": total_blocks, "counts": dict(zip(CLASSES, counts)), "bad_sectors": len(bad),
           "bad_lbas": bad[:5000], "repaired": len(repaired)}
    log.info("Surface scan %s: %s", dev.path, {k: v for k, v in res.items() if k != "bad_lbas"})
    return res


# ---------------------------------------------------------------------------
# Imaging / cloning
# ---------------------------------------------------------------------------
def vhd_footer(size: int) -> bytes:
    """Fixed-size VHD footer so Windows can attach the image directly (Disk Management)."""
    total = size // 512
    if total > 65535 * 16 * 255:
        total = 65535 * 16 * 255
    if total >= 65535 * 16 * 63:
        spt, heads = 255, 16
        cth = total // spt
    else:
        spt = 17
        cth = total // spt
        heads = max(4, (cth + 1023) // 1024)
        if cth >= heads * 1024 or heads > 16:
            spt, heads = 31, 16
            cth = total // spt
        if cth >= heads * 1024:
            spt, heads = 63, 16
            cth = total // spt
    cyl = cth // heads
    ts = int(time.time() - 946684800)
    f = bytearray(512)
    struct.pack_into(">8sIIQI4sI4sQQHBBII16sB", f, 0, b"conectix", 2, 0x00010000, 0xFFFFFFFFFFFFFFFF, ts,
                     b"win ", 0x000A0000, b"Wi2k", size, size, cyl, heads, spt, 2, 0, uuid.uuid4().bytes, 0)
    checksum = (~sum(f)) & 0xFFFFFFFF
    struct.pack_into(">I", f, 64, checksum)
    return bytes(f)


def image_copy(src: Device, dst, prog: Progress, start_lba: int = 0, sectors: int | None = None,
               fmt: str = "raw", fill_bad: bytes = b"\x00", do_hash: bool = True, snapshot: bool = False,
               smart: bool = False) -> dict:
    """Copy sectors from ``src`` to ``dst`` (a file path for an image, or a Device for clone/restore).

    Two passes like ddrescue: pass 1 copies in 4 MiB chunks and skips any chunk that errors;
    pass 2 retries the skipped chunks one sector at a time. Unreadable sectors are filled.
    """
    ss = src.sector_size
    if sectors is None:
        sectors = src.total_sectors - start_lba
    length = sectors * ss
    chunk = 4 * 1024 * 1024
    src.open(writable=False)
    snap = None
    if snapshot:  # consistent copy of a disk whose volumes are in use (e.g. the running Windows disk)
        from .vss import snapshot_disk
        prog.set_label("Taking a snapshot of the running disk…")
        snap = snapshot_disk(src, progress=prog.set_detail)

    to_device = isinstance(dst, Device)
    if to_device:
        if dst.path == src.path:
            raise DeviceError("Source and destination are the same.")
        if dst.usable_size < length:
            raise DeviceError("Destination is smaller than the source range.")
        dst.open(writable=True)
        write = dst.write
        out_path = dst.path
    else:
        out_path = dst
        fh = open(dst, "wb")
        fh.truncate(length)  # unused areas of a smart copy stay as (sparse) zeros

        def write(off, data):
            fh.seek(off)
            fh.write(data)

    # what to copy: everything, or only the used parts of understood filesystems
    base = start_lba * ss
    plan_info = None
    if smart:
        from .usedmap import copy_plan
        prog.set_label("Mapping used space…")
        plan_info = copy_plan(src, start_lba, sectors, smart=True)
        ranges = [(o - base, n) for o, n in plan_info["ranges"]]
    else:
        ranges = [(0, length)]
    from .usedmap import pieces
    todo = list(pieces(ranges, chunk))
    to_copy = sum(n for _, n in todo)
    linear = not smart
    hasher = hashlib.sha256() if (do_hash and linear) else None
    retry: list[tuple[int, int]] = []
    bad: list[int] = []
    prog.reset(to_copy, "Imaging — pass 1 (fast copy)" + (" · used space only" if smart else ""))
    try:
        done = 0
        for pos, n in todo:
            prog.check()
            try:
                data = src.read(base + pos, n)
                if len(data) != n:
                    raise OSError("short read")
            except OSError:
                retry.append((pos, n))
                data = (fill_bad * (n // len(fill_bad) + 1))[:n]
                prog.set_detail(f"Read error near LBA {start_lba + pos // ss} — will retry")
            write(pos, data)
            if hasher is not None and not retry:
                hasher.update(data)
            done += n
            prog.update(done)

        if retry:
            prog.reset(len(retry) * chunk, f"Imaging — pass 2 (retrying {len(retry)} block(s) per sector)")
            for i, (cpos, n) in enumerate(retry):
                for s in range(0, n, ss):
                    prog.check()
                    off = start_lba * ss + cpos + s
                    try:
                        d = src.read(off, ss)
                        if len(d) == ss:
                            write(cpos + s, d)
                            continue
                    except OSError:
                        pass
                    bad.append(off // ss)
                prog.update((i + 1) * chunk)
        grown = None
        if to_device:
            dst.flush()
            if start_lba == 0 and sectors == src.total_sectors and dst.total_sectors > sectors:
                from .partitions import fix_gpt_after_grow
                try:
                    grown = fix_gpt_after_grow(dst)
                except Exception as e:  # noqa: BLE001
                    grown = f"Couldn't adjust the partition table to the larger disk: {e}"
        else:
            if fmt == "vhd":
                fh.seek(length)
                fh.write(vhd_footer(length))
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if to_device:
            dst.close()
        else:
            fh.close()
        if snap is not None:
            snap.release()
        src.close()

    digest = None
    if do_hash:
        if retry or hasher is None:  # re-hash the finished image so the hash matches what is on disk
            if to_device:
                digest = None
            else:
                img = open_image(out_path)
                prog.reset(length, "Hashing image (SHA-256)")
                h = hashlib.sha256()
                p = 0
                while p < length:
                    prog.check()
                    d = img.read(p, min(chunk, length - p))
                    h.update(d)
                    p += len(d)
                    prog.update(p)
                img.close()
                digest = h.hexdigest()
        else:
            digest = hasher.hexdigest()

    res = {"bytes": length, "bad_sectors": len(bad), "bad_lbas": bad[:5000], "sha256": digest, "dest": out_path,
           "grown": grown, "snapshot": snap.volumes if snap else [], "snapshot_notes": snap.notes if snap else [],
           "copied_bytes": to_copy, "plan": plan_info["by_partition"] if plan_info else []}
    if not to_device:
        with open(str(out_path) + ".log.txt", "w", encoding="utf-8") as lf:
            lf.write(f"SectorSmith image log\nSource: {src.describe()}\nStart LBA: {start_lba}\n"
                     f"Sectors: {sectors} x {ss} bytes\nFormat: {fmt}\nSHA-256 (data): {digest}\n"
                     f"Unreadable sectors: {len(bad)}\n")
            for b in bad:
                lf.write(f"  bad LBA {b}\n")
    log.info("Image copy done: %s", {k: v for k, v in res.items() if k != "bad_lbas"})
    return res
