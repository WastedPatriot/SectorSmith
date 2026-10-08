"""Sector-by-sector disk clone between two endpoints (this PC and/or linked machines)."""
from __future__ import annotations

import time

from ..util import Progress, get_logger

log = get_logger()
CHUNK = 4 * 1024 * 1024


def clone(src, src_path: str, dst, dst_path: str, prog: Progress, start_lba: int = 0, sectors: int | None = None,
          dst_lba: int = 0, verify: bool = True, snapshot: bool = True) -> dict:
    prog.set_label("Preparing (snapshotting the source if it's in use)…")
    s_info = src.disk_open(path=src_path, writable=False, snapshot=snapshot)
    try:
        d_info = dst.disk_open(path=dst_path, writable=True)
    except Exception:
        src.disk_close(path=src_path)
        raise
    ss = s_info["sector_size"]
    if d_info["sector_size"] != ss:
        src.disk_close(path=src_path)
        dst.disk_close(path=dst_path)
        raise ValueError(f"Sector sizes differ ({ss} vs {d_info['sector_size']}) — can't clone sector-for-sector.")
    length = (sectors if sectors is not None else s_info["usable"] // ss - start_lba) * ss
    if dst_lba * ss + length > d_info["usable"]:
        src.disk_close(path=src_path)
        dst.disk_close(path=dst_path)
        raise ValueError("The destination is smaller than the source.")
    over_network = not (getattr(src, "is_local", False) and getattr(dst, "is_local", False))
    prog.reset(length, f"Cloning {src.label} → {dst.label}")
    done = bad = mismatches = sent = zeros = 0
    t0 = time.time()
    try:
        while done < length:
            prog.check()
            n = min(CHUNK, length - done)
            meta, blob = src.disk_read(path=src_path, offset=start_lba * ss + done, size=n, zskip=True,
                                       compress=over_network)
            w = dst.disk_write(path=dst_path, offset=dst_lba * ss + done, size=n, zero=meta.get("zero", False),
                               z=meta.get("z", False), blob=blob or b"")
            if verify and w["sha"] != meta["sha"]:
                mismatches += 1
            bad += meta.get("bad", 0)
            zeros += 1 if meta.get("zero") else 0
            sent += len(blob or b"")
            done += n
            prog.update(done)
            if done % (64 * CHUNK) == 0:
                prog.set_detail(f"sent {sent / max(1, done):.0%} of raw size · {bad} unreadable sector(s)")
        grown = None
        whole = start_lba == 0 and dst_lba == 0 and length == s_info["usable"] // ss * ss
        if whole and d_info["usable"] > length:
            try:
                grown = dst.disk_fix_gpt(path=dst_path)
            except Exception as e:  # noqa: BLE001
                grown = f"Couldn't adjust the partition table to the larger disk: {e}"
    finally:
        src.disk_close(path=src_path)
        dst.disk_close(path=dst_path)
    res = {"bytes": length, "bad_sectors": bad, "verify_mismatches": mismatches, "sent_bytes": sent,
           "empty_chunks_skipped": zeros, "seconds": round(time.time() - t0, 1), "grown": grown,
           "snapshot": s_info.get("snapshot", [])}
    log.info("Network clone %s:%s -> %s:%s %s", src.label, src_path, dst.label, dst_path, res)
    return res
