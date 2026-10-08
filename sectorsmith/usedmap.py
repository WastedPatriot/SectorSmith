"""Used-space maps: which byte ranges of a disk actually hold data.

Smart cloning copies only allocated clusters/blocks of filesystems it understands (NTFS, FAT12/16/32,
exFAT, ext2/3/4) and falls back to a full sector copy for everything else (other OSes' filesystems,
BitLocker/LUKS, unpartitioned space, partition tables) — so any disk can be cloned, and common ones fast.
"""
from __future__ import annotations

import struct

from .device import Device
from .partitions import read_partition_table
from .util import get_logger

log = get_logger()
MERGE_GAP = 4 * 1024 * 1024  # merge used ranges closer than this (fewer, larger reads)


def _bits_to_ranges(bitmap: bytes, nbits: int, unit: int, base: int, first_index: int = 0) -> list[tuple[int, int]]:
    """Runs of set bits -> [(absolute_offset, length)]."""
    out = []
    i = 0
    n = min(nbits, len(bitmap) * 8)
    while i < n:
        byte = bitmap[i >> 3]
        if byte == 0 and (i & 7) == 0:
            i += 8
            continue
        if byte == 0xFF and (i & 7) == 0 and i + 8 <= n:
            start = i
            while i + 8 <= n and bitmap[i >> 3] == 0xFF:
                i += 8
            while i < n and bitmap[i >> 3] >> (i & 7) & 1:
                i += 1
            out.append((base + (start + first_index) * unit, (i - start) * unit))
            continue
        if byte >> (i & 7) & 1:
            start = i
            while i < n and bitmap[i >> 3] >> (i & 7) & 1:
                i += 1
            out.append((base + (start + first_index) * unit, (i - start) * unit))
        else:
            i += 1
    return out


def merge(ranges, gap=MERGE_GAP):
    out = []
    for s, ln in sorted(r for r in ranges if r[1] > 0):
        if out and s <= out[-1][0] + out[-1][1] + gap:
            e = max(out[-1][0] + out[-1][1], s + ln)
            out[-1] = (out[-1][0], e - out[-1][0])
        else:
            out.append((s, ln))
    return out


def clip(ranges, start, end):
    out = []
    for s, ln in ranges:
        a, b = max(s, start), min(s + ln, end)
        if b > a:
            out.append((a, b - a))
    return out


# ---------------------------------------------------------------------------
def _ntfs(dev: Device, base: int, size: int):
    from .ntfs import NTFSVolume
    vol = NTFSVolume(dev, base)
    rec = vol.read_record(6)  # $Bitmap
    if rec is None or not rec.runs and rec.resident is None:
        return None
    total_clusters = vol.total_sectors * vol.bps // vol.cluster
    if rec.resident is not None:
        bitmap = rec.resident
    else:
        buf = bytearray()
        for lcn, n in rec.runs:
            buf += bytes(n * vol.cluster) if lcn is None else dev.read(base + lcn * vol.cluster, n * vol.cluster)
        bitmap = bytes(buf[: (total_clusters + 7) // 8])
    used = _bits_to_ranges(bitmap, total_clusters, vol.cluster, base)
    used.append((base, vol.cluster))                                  # boot sector & first cluster
    tail = base + total_clusters * vol.cluster
    used.append((tail, base + size - tail))                           # backup boot sector / slack
    return used


def _fat(dev: Device, base: int, size: int, bs: bytes):
    bps = struct.unpack_from("<H", bs, 11)[0]
    spc = bs[13]
    reserved = struct.unpack_from("<H", bs, 14)[0]
    nfats = bs[16]
    root_ent = struct.unpack_from("<H", bs, 17)[0]
    total = struct.unpack_from("<H", bs, 19)[0] or struct.unpack_from("<I", bs, 32)[0]
    fatsz = struct.unpack_from("<H", bs, 22)[0] or struct.unpack_from("<I", bs, 36)[0]
    root_secs = (root_ent * 32 + bps - 1) // bps
    data_start = reserved + nfats * fatsz + root_secs
    nclusters = (total - data_start) // spc
    kind = 12 if nclusters < 4085 else 16 if nclusters < 65525 else 32
    fat = dev.read(base + reserved * bps, fatsz * bps)
    used_bits = bytearray((nclusters + 7) // 8)
    for c in range(2, nclusters + 2):
        if kind == 32:
            v = struct.unpack_from("<I", fat, c * 4)[0] & 0x0FFFFFFF if c * 4 + 4 <= len(fat) else 0
        elif kind == 16:
            v = struct.unpack_from("<H", fat, c * 2)[0] if c * 2 + 2 <= len(fat) else 0
        else:
            o = c * 3 // 2
            if o + 2 > len(fat):
                v = 0
            else:
                w = struct.unpack_from("<H", fat, o)[0]
                v = (w >> 4) if c & 1 else (w & 0xFFF)
        if v:
            used_bits[(c - 2) >> 3] |= 1 << ((c - 2) & 7)
    cluster = spc * bps
    used = _bits_to_ranges(bytes(used_bits), nclusters, cluster, base + data_start * bps)
    used.append((base, data_start * bps))                              # boot, FATs, root dir
    tail = base + data_start * bps + nclusters * cluster
    used.append((tail, base + size - tail))
    return used


def _exfat(dev: Device, base: int, size: int, bs: bytes):
    fat_off, fat_len, heap_off, count, root = struct.unpack_from("<IIIII", bs, 80)
    bps = 1 << bs[108]
    cluster = bps << bs[109]
    root_dir = dev.read(base + heap_off * bps + (root - 2) * cluster, cluster)
    bm_cluster = bm_len = None
    for i in range(0, len(root_dir), 32):
        if root_dir[i] == 0x81:  # allocation bitmap entry
            bm_cluster, bm_len = struct.unpack_from("<IQ", root_dir, i + 20)
            break
    if bm_cluster is None:
        return None
    bitmap = dev.read(base + heap_off * bps + (bm_cluster - 2) * cluster, bm_len)
    used = _bits_to_ranges(bitmap, count, cluster, base + heap_off * bps)
    used.append((base, heap_off * bps))                                # boot region + FAT
    tail = base + heap_off * bps + count * cluster
    used.append((tail, base + size - tail))
    return used


def _ext(dev: Device, base: int, size: int):
    sb = dev.read(base + 1024, 1024)
    if sb[56:58] != b"\x53\xef":
        return None
    blocks_lo, = struct.unpack_from("<I", sb, 4)
    first_data, log_bs = struct.unpack_from("<II", sb, 20)
    bpg, = struct.unpack_from("<I", sb, 32)
    incompat, = struct.unpack_from("<I", sb, 0x60)
    bs = 1024 << log_bs
    is64 = bool(incompat & 0x80)
    blocks = blocks_lo | (struct.unpack_from("<I", sb, 0x150)[0] << 32 if is64 else 0)
    desc_size = struct.unpack_from("<H", sb, 0xFE)[0] if is64 else 32
    desc_size = desc_size or 32
    groups = -(-(blocks - first_data) // bpg)
    gdt = dev.read(base + (first_data + 1) * bs, groups * desc_size)
    used = []
    for g in range(groups):
        d = gdt[g * desc_size:(g + 1) * desc_size]
        if len(d) < 32:
            return None
        bb = struct.unpack_from("<I", d, 0)[0] | ((struct.unpack_from("<I", d, 0x20)[0] << 32)
                                                  if is64 and desc_size >= 64 else 0)
        flags = struct.unpack_from("<H", d, 0x12)[0]
        g_first = first_data + g * bpg
        g_blocks = min(bpg, blocks - g_first)
        if flags & 0x2 or bb == 0:  # BLOCK_UNINIT — bitmap not written yet: copy the whole group (safe)
            used.append((base + g_first * bs, g_blocks * bs))
            continue
        bitmap = dev.read(base + bb * bs, bs)
        used += _bits_to_ranges(bitmap, g_blocks, bs, base + g_first * bs)
    used.append((base, (first_data + 1) * bs + len(gdt)))              # boot block, superblock, GDT
    tail = base + blocks * bs
    used.append((tail, base + size - tail))
    return used


def partition_used(dev: Device, start_lba: int, sectors: int, fs: str):
    """Used ranges for one partition, or None if the filesystem isn't understood (copy it all)."""
    ss = dev.sector_size
    base, size = start_lba * ss, sectors * ss
    try:
        if fs == "NTFS":
            r = _ntfs(dev, base, size)
        elif fs in ("FAT12", "FAT16", "FAT32"):
            r = _fat(dev, base, size, dev.read(base, 512))
        elif fs == "exFAT":
            r = _exfat(dev, base, size, dev.read(base, 512))
        elif fs.startswith("ext"):
            r = _ext(dev, base, size)
        else:
            r = None
    except Exception as e:  # noqa: BLE001 — anything odd: be safe and copy everything
        log.warning("Used-space map failed for %s at LBA %d: %s", fs, start_lba, e)
        r = None
    return clip(r, base, base + size) if r is not None else None


def copy_plan(dev: Device, start_lba: int = 0, sectors: int | None = None, smart: bool = True) -> dict:
    """Byte ranges to copy for [start_lba, start_lba+sectors). Returns
    {"ranges": [(offset, length)], "total": bytes_in_range, "copy": bytes_to_copy, "by_partition": [...]}"""
    ss = dev.sector_size
    if sectors is None:
        sectors = dev.total_sectors - start_lba
    start, end = start_lba * ss, (start_lba + sectors) * ss
    if not smart:
        return {"ranges": [(start, end - start)], "total": end - start, "copy": end - start, "by_partition": []}
    try:
        pt = read_partition_table(dev)
        parts = [p for p in pt.partitions if p.type_id not in ("0x05", "0x0F", "0x85")]
    except OSError:
        parts = []
    ranges = []
    covered = []
    info = []
    for p in parts:
        ps, pe = p.start_lba * ss, (p.end_lba + 1) * ss
        if pe <= start or ps >= end:
            continue
        used = partition_used(dev, p.start_lba, p.sectors, p.fs) if p.fs else None
        covered.append((ps, pe - ps))
        if used is None:
            ranges.append((ps, pe - ps))
            info.append({"index": p.index, "fs": p.fs or "unknown", "mode": "full", "copy": pe - ps})
        else:
            ranges += used
            info.append({"index": p.index, "fs": p.fs, "mode": "used", "copy": sum(r[1] for r in used),
                         "size": pe - ps})
    # everything not inside a partition (tables, gaps, unpartitioned space) is copied in full
    pos = start
    for cs, cl in sorted(covered):
        if cs > pos:
            ranges.append((pos, cs - pos))
        pos = max(pos, cs + cl)
    if pos < end:
        ranges.append((pos, end - pos))
    ranges = merge(clip(ranges, start, end))
    # align to sectors
    al = []
    for s, ln in ranges:
        a = s - s % ss
        b = -(-(s + ln) // ss) * ss
        al.append((a, min(b, end) - a))
    ranges = merge(al, gap=0)
    return {"ranges": ranges, "total": end - start, "copy": sum(r[1] for r in ranges), "by_partition": info}


def pieces(ranges, chunk=4 * 1024 * 1024):
    """Split ranges into chunk-sized (offset, length) pieces aligned to chunk boundaries."""
    for s, ln in ranges:
        pos, end = s, s + ln
        while pos < end:
            n = min(chunk - pos % chunk, end - pos)
            yield pos, n
            pos += n
