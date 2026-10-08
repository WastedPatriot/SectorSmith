"""Lost / deleted partition search by filesystem signature scanning."""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .device import Device
from .partitions import read_partition_table
from .util import Progress, get_logger

log = get_logger()


@dataclass
class FoundPartition:
    start_lba: int
    sectors: int
    fs: str
    label: str = ""
    source: str = "boot sector"
    status: str = "Lost"  # "Lost" or "Existing"
    confidence: str = "high"

    @property
    def end_lba(self):
        return self.start_lba + self.sectors - 1


def _pow2(n):
    return n > 0 and (n & (n - 1)) == 0


def _ntfs_params(bs: bytes):
    if bs[3:11] != b"NTFS    " or bs[510:512] != b"\x55\xaa":
        return None
    bps = struct.unpack_from("<H", bs, 11)[0]
    spc = bs[13]
    if spc > 0x80:
        spc = 1 << (256 - spc)
    if bps not in (512, 1024, 2048, 4096) or not _pow2(spc):
        return None
    total = struct.unpack_from("<Q", bs, 40)[0]
    mft = struct.unpack_from("<Q", bs, 48)[0]
    serial = bs[72:80]
    if total == 0 or mft == 0:
        return None
    return bps, spc, total, mft, serial


def _fat_params(bs: bytes):
    if bs[510:512] != b"\x55\xaa" or bs[0] not in (0xEB, 0xE9):
        return None
    bps = struct.unpack_from("<H", bs, 11)[0]
    spc = bs[13]
    reserved = struct.unpack_from("<H", bs, 14)[0]
    nfats = bs[16]
    if bps not in (512, 1024, 2048, 4096) or not _pow2(spc) or reserved == 0 or nfats not in (1, 2):
        return None
    total = struct.unpack_from("<H", bs, 19)[0] or struct.unpack_from("<I", bs, 32)[0]
    if bs[82:90] == b"FAT32   ":
        return "FAT32", bps, total, bs[71:82].decode("ascii", "replace").strip()
    if bs[54:59] in (b"FAT12", b"FAT16"):
        return bs[54:59].decode(), bps, total, bs[43:54].decode("ascii", "replace").strip()
    return None


def _exfat_params(bs: bytes):
    if bs[3:11] != b"EXFAT   " or bs[510:512] != b"\x55\xaa":
        return None
    vol_len = struct.unpack_from("<Q", bs, 72)[0]
    bps_shift = bs[108]
    if not 9 <= bps_shift <= 12 or vol_len == 0:
        return None
    return 1 << bps_shift, vol_len


def _ext_params(sb: bytes):
    if len(sb) < 264 or sb[56:58] != b"\x53\xef":
        return None
    blocks_lo, = struct.unpack_from("<I", sb, 4)
    first_data_block, log_bs = struct.unpack_from("<II", sb, 20)
    bpg, = struct.unpack_from("<I", sb, 32)
    group_nr, = struct.unpack_from("<H", sb, 90)
    incompat, = struct.unpack_from("<I", sb, 0x60)
    if log_bs > 6:
        return None
    bs = 1024 << log_bs
    if bpg != 8 * bs:
        return None
    blocks = blocks_lo
    if incompat & 0x80 and len(sb) >= 0x154:
        blocks |= struct.unpack_from("<I", sb, 0x150)[0] << 32
    if blocks == 0:
        return None
    compat, = struct.unpack_from("<I", sb, 0x5C)
    kind = "ext4" if incompat & 0x2C0 else ("ext3" if compat & 0x4 else "ext2")
    label = sb[120:136].split(b"\0")[0].decode("utf-8", "replace")
    return kind, bs, blocks, first_data_block, bpg, group_nr, label


def scan_partitions(dev: Device, prog: Progress, start_lba: int = 0, end_lba: int | None = None,
                    mode: str = "quick", skip_found: bool = True, on_found=None) -> list[FoundPartition]:
    """Search the disk for filesystem boot sectors / superblocks.

    ``mode="quick"`` checks every 2048-sector (1 MiB) boundary plus the classic 63-sector
    track boundaries; ``mode="full"`` checks every single sector (reads the whole disk).
    """
    ss = dev.sector_size
    total = dev.total_sectors
    end_lba = min(end_lba or total, total)
    dev.open(writable=False)
    found: list[FoundPartition] = []
    starts: set[int] = set()

    try:
        existing = {p.start_lba for p in read_partition_table(dev, detect=False).partitions}
    except OSError:
        existing = set()

    def add(fp: FoundPartition):
        if fp.start_lba in starts or fp.start_lba < 0 or fp.sectors <= 0:
            return False
        if fp.start_lba + fp.sectors > total + 1:
            fp.confidence = "low (extends past end of disk)"
        starts.add(fp.start_lba)
        fp.status = "Existing" if fp.start_lba in existing else "Lost"
        found.append(fp)
        if on_found:
            on_found(fp)
        log.info("Found %s at LBA %d (%d sectors) via %s", fp.fs, fp.start_lba, fp.sectors, fp.source)
        return True

    def check_at(lba: int, buf: bytes, off: int) -> int | None:
        """Inspect sector at ``lba`` (bytes in buf[off:]). Returns LBA to jump to, or None."""
        sec = buf[off: off + ss]
        # NTFS (primary or backup boot sector)
        p = _ntfs_params(sec)
        if p:
            bps, spc, nsect, mft, serial = p
            nsect_dev = nsect * bps // ss
            # Decide primary vs backup by looking for a "FILE" record at the MFT location.
            for cand, src in ((lba, "NTFS boot sector"), (lba - nsect_dev, "NTFS backup boot sector")):
                if cand < 0:
                    continue
                mft_lba = cand + mft * spc * bps // ss
                try:
                    if dev.read_sectors(mft_lba, 1)[:4] == b"FILE":
                        if add(FoundPartition(cand, nsect_dev + 1, "NTFS", source=src)):
                            return cand + nsect_dev + 1
                        return None
                except OSError:
                    pass
            add(FoundPartition(lba, nsect_dev + 1, "NTFS", source="NTFS boot sector (MFT unverified)",
                               confidence="medium"))
            return lba + nsect_dev + 1
        q = _exfat_params(sec)
        if q:
            bps, vol_len = q
            if add(FoundPartition(lba, vol_len * bps // ss, "exFAT")):
                return lba + vol_len * bps // ss
            return None
        f = _fat_params(sec)
        if f:
            kind, bps, nsect, label = f
            if add(FoundPartition(lba, nsect * bps // ss, kind, label=label)):
                return lba + nsect * bps // ss
            return None
        # ext2/3/4 superblock: located 1024 bytes into the block group
        if ss <= 1024:
            e = _ext_params(buf[off: off + 1024])
            if e and (lba * ss) % 512 == 0:
                kind, bs, blocks, fdb, bpg, gnr, label = e
                sb_off = lba * ss
                start_bytes = sb_off - 1024 if gnr == 0 else sb_off - (fdb + gnr * bpg) * bs
                if start_bytes >= 0 and start_bytes % ss == 0:
                    src = "ext superblock" if gnr == 0 else f"ext backup superblock (group {gnr})"
                    st = start_bytes // ss
                    if add(FoundPartition(st, blocks * bs // ss, kind, label=label, source=src)):
                        return st + blocks * bs // ss
        return None

    span = end_lba - start_lba
    prog.reset(span * ss, f"Partition search ({mode})")
    if mode == "full":
        chunk_sectors = (4 * 1024 * 1024) // ss
        lba = start_lba
        while lba < end_lba:
            prog.check()
            n = min(chunk_sectors, end_lba - lba)
            try:
                buf = dev.read_sectors(lba, n + 2)  # +2 so an ext superblock straddling the end is visible
            except OSError:
                lba += n
                continue
            jump = None
            # fast pre-filter with C-speed find(): only sectors ending in 55AA or carrying an
            # ext magic at +56 are worth parsing
            hits = set()
            for pat, rel in ((b"\x55\xaa", 510), (b"\x53\xef", 56)):
                k = buf.find(pat)
                while k != -1:
                    if (k - rel) % ss == 0 and 0 <= (k - rel) // ss < n:
                        hits.add((k - rel) // ss)
                    k = buf.find(pat, k + 1)
            for i in sorted(hits):
                if jump and lba + i < jump:
                    continue
                j = check_at(lba + i, buf, i * ss)
                if j and skip_found and j > lba + i:
                    jump = j
            lba = jump if (jump and jump > lba + n) else lba + n
            prog.update((min(lba, end_lba) - start_lba) * ss)
    else:
        cands = set()
        step = max(1, 2048 * 512 // ss)
        cands.update(range(start_lba - start_lba % step, end_lba, step))
        cands.update(range(start_lba - start_lba % 63, min(end_lba, 63 * 255 * 1024), 63))
        lba_list = sorted(c for c in cands if start_lba <= c < end_lba)
        nread = max(1, 2048 // ss)  # boot sector + where an ext superblock would sit (+1024 bytes)
        skip_until = -1
        for i, lba in enumerate(lba_list):
            if lba < skip_until:
                continue
            if i % 256 == 0:
                prog.check()
                prog.update((lba - start_lba) * ss)
            try:
                buf = dev.read_sectors(lba, nread)
            except OSError:
                continue
            j = None
            if buf[510:512] == b"\x55\xaa":
                j = check_at(lba, buf, 0)
            if not j and ss <= 1024 and buf[1024 + 56:1024 + 58] == b"\x53\xef":
                j = check_at(lba + 1024 // ss, buf, 1024)
            if j and skip_found:
                skip_until = j
        prog.update(span * ss)
    dev.close()
    found.sort(key=lambda f: f.start_lba)
    return found
