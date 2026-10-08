"""MBR / GPT partition tables: parsing, filesystem detection, and safe restoration of entries."""
from __future__ import annotations

import os
import struct
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from .device import Device, DeviceError
from .util import app_dir, get_logger, human_size

log = get_logger()

MBR_TYPES = {
    0x00: "Empty", 0x01: "FAT12", 0x04: "FAT16 <32M", 0x05: "Extended", 0x06: "FAT16", 0x07: "NTFS/exFAT/HPFS",
    0x0B: "FAT32 (CHS)", 0x0C: "FAT32 (LBA)", 0x0E: "FAT16 (LBA)", 0x0F: "Extended (LBA)", 0x11: "Hidden FAT12",
    0x12: "OEM/Recovery", 0x14: "Hidden FAT16", 0x17: "Hidden NTFS", 0x1B: "Hidden FAT32", 0x1C: "Hidden FAT32 LBA",
    0x27: "Windows RE", 0x42: "Windows Dynamic", 0x82: "Linux swap", 0x83: "Linux", 0x85: "Linux extended",
    0x8E: "Linux LVM", 0xA5: "FreeBSD", 0xAF: "HFS+", 0xEE: "GPT protective", 0xEF: "EFI System", 0xFD: "Linux RAID",
}
EXTENDED_TYPES = {0x05, 0x0F, 0x85}

GPT_TYPES = {
    "c12a7328-f81f-11d2-ba4b-00a0c93ec93b": "EFI System",
    "e3c9e316-0b5c-4db8-817d-f92df00215ae": "Microsoft Reserved",
    "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7": "Basic data",
    "de94bba4-06d1-4d40-a16a-bfd50179d6ac": "Windows Recovery",
    "5808c8aa-7e8f-42e0-85d2-e1e90434cfb3": "LDM metadata",
    "af9b60a0-1431-4f62-bc68-3311714a69ad": "LDM data",
    "e75caf8f-f680-4cee-afa3-b001e56efc2d": "Storage Spaces",
    "0fc63daf-8483-4772-8e79-3d69d8477de4": "Linux filesystem",
    "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f": "Linux swap",
    "e6d6d379-f507-44c2-a23c-238f2a3df928": "Linux LVM",
    "a19d880f-05fc-4d3b-a006-743f0f84911e": "Linux RAID",
    "4f68bce3-e8cd-4db1-96e7-fbcaf984b709": "Linux root (x86-64)",
    "933ac7e1-2eb4-4f13-b844-0e14e2aef915": "Linux /home",
    "48465300-0000-11aa-aa11-00306543ecac": "Apple HFS+",
    "7c3457ef-0000-11aa-aa11-00306543ecac": "Apple APFS",
    "21686148-6449-6e6f-744e-656564454649": "BIOS boot",
}
GUID_BASIC_DATA = "ebd0a0a2-b9e5-4433-87c0-68b6b72699c7"
GUID_LINUX_FS = "0fc63daf-8483-4772-8e79-3d69d8477de4"


@dataclass
class Partition:
    index: int
    start_lba: int
    sectors: int
    type_name: str
    scheme: str  # "MBR", "MBR-logical", "GPT"
    type_id: str = ""
    name: str = ""
    bootable: bool = False
    fs: str = ""
    label: str = ""
    mount: list[str] = field(default_factory=list)

    @property
    def end_lba(self) -> int:
        return self.start_lba + self.sectors - 1

    def size_bytes(self, ss: int) -> int:
        return self.sectors * ss


@dataclass
class PartitionTable:
    scheme: str  # "MBR", "GPT", "None"
    partitions: list[Partition]
    disk_guid: str = ""
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Filesystem detection
# ---------------------------------------------------------------------------
def detect_fs(head: bytes) -> tuple[str, str]:
    """Identify a filesystem from its first 4 KiB. Returns (fs_name, label)."""
    if len(head) < 512:
        return "", ""
    oem = head[3:11]
    if oem == b"NTFS    ":
        return "NTFS", ""
    if oem == b"EXFAT   ":
        return "exFAT", ""
    if oem == b"-FVE-FS-":
        return "BitLocker", ""
    if head[3:7] == b"ReFS":
        return "ReFS", ""
    if head[82:90] == b"FAT32   ":
        return "FAT32", head[71:82].decode("ascii", "replace").strip()
    if head[54:59] in (b"FAT12", b"FAT16", b"FAT  "):
        return head[54:59].decode().strip() or "FAT", head[43:54].decode("ascii", "replace").strip()
    if len(head) >= 1024 + 0x80 and head[1024 + 56: 1024 + 58] == b"\x53\xef":
        sb = head[1024:]
        compat, incompat = struct.unpack_from("<II", sb, 0x5C)
        kind = "ext4" if incompat & 0x2C0 else ("ext3" if compat & 0x4 else "ext2")
        return kind, sb[120:136].split(b"\0")[0].decode("utf-8", "replace")
    if head[:6] == b"LUKS\xba\xbe":
        return "LUKS", ""
    if len(head) >= 1024 and head[512:520] == b"LABELONE":
        return "LVM2 PV", ""
    if len(head) >= 1024 + 2 and head[1024:1026] in (b"H+", b"HX"):
        return "HFS+", ""
    if head[32:36] == b"NXSB":
        return "APFS", ""
    if len(head) >= 4096 and head[4086:4096] in (b"SWAPSPACE2", b"SWAP-SPACE"):
        return "Linux swap", ""
    return "", ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _guid(b: bytes) -> str:
    return str(uuid.UUID(bytes_le=bytes(b)))


def parse_mbr_entries(sector: bytes):
    for i in range(4):
        e = sector[446 + 16 * i: 446 + 16 * (i + 1)]
        status, ptype = e[0], e[4]
        start, count = struct.unpack_from("<II", e, 8)
        yield i, status, ptype, start, count


def read_gpt_header(dev: Device, lba: int) -> dict | None:
    raw = dev.read_sectors(lba, 1)
    if raw[:8] != b"EFI PART":
        return None
    hsize = struct.unpack_from("<I", raw, 12)[0]
    if not 92 <= hsize <= len(raw):
        return None
    crc = struct.unpack_from("<I", raw, 16)[0]
    tmp = bytearray(raw[:hsize])
    tmp[16:20] = b"\0\0\0\0"
    my_lba, alt_lba, first_usable, last_usable = struct.unpack_from("<QQQQ", raw, 24)
    entries_lba, n_entries, entry_size, entries_crc = struct.unpack_from("<QIII", raw, 72)
    return {
        "raw": raw, "header_size": hsize, "crc_ok": zlib.crc32(bytes(tmp)) == crc,
        "my_lba": my_lba, "alt_lba": alt_lba, "first_usable": first_usable, "last_usable": last_usable,
        "disk_guid": _guid(raw[56:72]), "entries_lba": entries_lba, "n_entries": n_entries,
        "entry_size": entry_size, "entries_crc": entries_crc,
    }


def read_partition_table(dev: Device, detect: bool = True) -> PartitionTable:
    ss = dev.sector_size
    mbr = dev.read_sectors(0, 1)
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return PartitionTable("None", [], notes=["No valid MBR signature (disk is blank or table destroyed)."])

    entries = list(parse_mbr_entries(mbr))
    parts: list[Partition] = []
    notes: list[str] = []

    if any(p[2] == 0xEE for p in entries):
        hdr = read_gpt_header(dev, 1)
        if hdr is None:
            alt = read_gpt_header(dev, dev.total_sectors - 1)
            if alt:
                notes.append("Primary GPT header damaged — using backup header.")
                hdr = alt
        if hdr is not None:
            if not hdr["crc_ok"]:
                notes.append("GPT header CRC mismatch.")
            table = dev.read(hdr["entries_lba"] * ss, hdr["n_entries"] * hdr["entry_size"])
            if zlib.crc32(table) != hdr["entries_crc"]:
                notes.append("GPT entry array CRC mismatch.")
            for i in range(hdr["n_entries"]):
                e = table[i * hdr["entry_size"]: (i + 1) * hdr["entry_size"]]
                if len(e) < 128 or e[:16] == b"\0" * 16:
                    continue
                tguid = _guid(e[:16])
                first, last, attrs = struct.unpack_from("<QQQ", e, 32)
                name = e[56:128].decode("utf-16-le", "ignore").split("\0")[0]
                parts.append(Partition(i + 1, first, last - first + 1, GPT_TYPES.get(tguid, tguid), "GPT",
                                       type_id=tguid, name=name))
            pt = PartitionTable("GPT", parts, disk_guid=hdr["disk_guid"], notes=notes)
            if detect:
                _detect_all(dev, pt)
            return pt
        notes.append("Protective MBR present but no readable GPT header.")

    for i, status, ptype, start, count in entries:
        if ptype == 0 or count == 0:
            continue
        if ptype in EXTENDED_TYPES:
            parts.append(Partition(i + 1, start, count, MBR_TYPES[ptype], "MBR", type_id=f"0x{ptype:02X}"))
            parts.extend(_walk_ebr(dev, start, notes))
            continue
        parts.append(Partition(i + 1, start, count, MBR_TYPES.get(ptype, f"Type 0x{ptype:02X}"), "MBR",
                               type_id=f"0x{ptype:02X}", bootable=status == 0x80))
    sig = struct.unpack_from("<I", mbr, 440)[0]
    pt = PartitionTable("MBR", parts, disk_guid=f"{sig:08X}", notes=notes)
    if detect:
        _detect_all(dev, pt)
    return pt


def _walk_ebr(dev: Device, ext_start: int, notes: list[str]) -> list[Partition]:
    res = []
    ebr_lba = ext_start
    seen = set()
    n = 5
    while ebr_lba not in seen and len(seen) < 128:
        seen.add(ebr_lba)
        ebr = dev.read_sectors(ebr_lba, 1)
        if ebr[510:512] != b"\x55\xaa":
            notes.append(f"Broken EBR chain at LBA {ebr_lba}.")
            break
        e = list(parse_mbr_entries(ebr))
        _, st, ptype, rel, count = e[0]
        if ptype and count:
            res.append(Partition(n, ebr_lba + rel, count, MBR_TYPES.get(ptype, f"Type 0x{ptype:02X}"),
                                 "MBR-logical", type_id=f"0x{ptype:02X}", bootable=st == 0x80))
            n += 1
        _, _, nptype, nrel, ncount = e[1]
        if not nptype or not ncount:
            break
        ebr_lba = ext_start + nrel
    return res


def _detect_all(dev: Device, pt: PartitionTable):
    for p in pt.partitions:
        if p.scheme == "MBR" and p.type_id in ("0x05", "0x0F", "0x85"):
            continue
        try:
            head = dev.read(p.start_lba * dev.sector_size, 4096)
            p.fs, p.label = detect_fs(head)
        except OSError as e:
            p.fs = f"read error ({e})"


# ---------------------------------------------------------------------------
# Writing (used by lost-partition recovery)
# ---------------------------------------------------------------------------
def backup_table_area(dev: Device, folder: str | None = None) -> str:
    """Save the first and last 1 MiB of a disk before touching its partition table."""
    folder = Path(folder) if folder else app_dir() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() else "_" for c in dev.name)
    out = folder / f"{safe}_{stamp}_ptable.bin"
    span = 1024 * 1024
    head = dev.read(0, span)
    tail_off = max(0, dev.usable_size - span)
    tail = dev.read(tail_off, span)
    with open(out, "wb") as f:
        f.write(struct.pack("<8sQQQ", b"SSPTBAK1", len(head), tail_off, len(tail)))
        f.write(head)
        f.write(tail)
    log.info("Partition table backup written to %s", out)
    return str(out)


def restore_table_backup(dev: Device, path: str):
    with open(path, "rb") as f:
        magic, hlen, tail_off, tlen = struct.unpack("<8sQQQ", f.read(32))
        if magic != b"SSPTBAK1":
            raise DeviceError("Not a SectorSmith partition-table backup.")
        head = f.read(hlen)
        tail = f.read(tlen)
    dev.write(0, head)
    dev.write(tail_off, tail)
    dev.flush()


def _fs_to_mbr_type(fs: str) -> int:
    return {"NTFS": 0x07, "exFAT": 0x07, "BitLocker": 0x07, "ReFS": 0x07, "FAT32": 0x0C, "FAT16": 0x0E,
            "FAT12": 0x01, "Linux swap": 0x82, "LVM2 PV": 0x8E}.get(fs, 0x83 if fs.startswith("ext") else 0x07)


def _fs_to_gpt_type(fs: str) -> str:
    if fs.startswith("ext"):
        return GUID_LINUX_FS
    if fs == "Linux swap":
        return "0657fd6d-a4ab-43c4-84e5-0933c84b4f4f"
    if fs == "LVM2 PV":
        return "e6d6d379-f507-44c2-a23c-238f2a3df928"
    return GUID_BASIC_DATA


def _write_mbr_entry(dev: Device, start: int, count: int, fs: str):
    mbr = bytearray(dev.read_sectors(0, 1))
    if mbr[510:512] != b"\x55\xaa":
        mbr = bytearray(dev.sector_size)
        mbr[440:444] = os.urandom(4)
        mbr[510:512] = b"\x55\xaa"
    if start > 0xFFFFFFFF or count > 0xFFFFFFFF:
        raise DeviceError("Partition is beyond the 2 TiB MBR limit — use GPT.")
    for i, _st, ptype, s, c in parse_mbr_entries(mbr):
        if ptype and c and not (start + count <= s or start >= s + c):
            raise DeviceError(f"Overlaps existing MBR partition {i + 1}.")
    for i, _st, ptype, _s, c in parse_mbr_entries(mbr):
        if ptype == 0 or c == 0:
            off = 446 + 16 * i
            mbr[off: off + 16] = struct.pack("<B3sB3sII", 0x00, b"\xfe\xff\xff", _fs_to_mbr_type(fs),
                                             b"\xfe\xff\xff", start, count)
            dev.write_sectors(0, bytes(mbr))
            return i + 1
    raise DeviceError("All 4 primary MBR slots are in use.")


def _gpt_header_bytes(hdr_raw: bytes, hsize: int, entries_crc: int, my_lba=None, alt_lba=None,
                      entries_lba=None) -> bytes:
    h = bytearray(hdr_raw)
    if my_lba is not None:
        struct.pack_into("<QQ", h, 24, my_lba, alt_lba)
    if entries_lba is not None:
        struct.pack_into("<Q", h, 72, entries_lba)
    struct.pack_into("<I", h, 88, entries_crc)
    struct.pack_into("<I", h, 16, 0)
    struct.pack_into("<I", h, 16, zlib.crc32(bytes(h[:hsize])))
    return bytes(h)


def create_empty_gpt(dev: Device):
    ss = dev.sector_size
    last = dev.total_sectors - 1
    n, esize = 128, 128
    ent_sectors = -(-n * esize // ss)
    entries = bytes(n * esize)
    ecrc = zlib.crc32(entries)
    pmbr = bytearray(ss)
    struct.pack_into("<B3sB3sII", pmbr, 446, 0, b"\x00\x02\x00", 0xEE, b"\xff\xff\xff", 1,
                     min(last, 0xFFFFFFFF))
    pmbr[510:512] = b"\x55\xaa"
    hdr = bytearray(ss)
    struct.pack_into("<8sIII4xQQQQ16sQIII", hdr, 0, b"EFI PART", 0x00010000, 92, 0, 1, last,
                     2 + ent_sectors, last - 1 - ent_sectors, uuid.uuid4().bytes_le, 2, n, esize, ecrc)
    primary = _gpt_header_bytes(bytes(hdr), 92, ecrc)
    backup = _gpt_header_bytes(bytes(hdr), 92, ecrc, my_lba=last, alt_lba=1, entries_lba=last - ent_sectors)
    dev.write_sectors(0, bytes(pmbr))
    dev.write_sectors(1, primary)
    dev.write_sectors(2, entries.ljust(ent_sectors * ss, b"\0"))
    dev.write_sectors(last - ent_sectors, entries.ljust(ent_sectors * ss, b"\0"))
    dev.write_sectors(last, backup)


def _write_gpt_entry(dev: Device, start: int, count: int, fs: str, name: str):
    ss = dev.sector_size
    hdr = read_gpt_header(dev, 1)
    if hdr is None:
        raise DeviceError("No readable primary GPT header.")
    esize, n = hdr["entry_size"], hdr["n_entries"]
    table = bytearray(dev.read(hdr["entries_lba"] * ss, n * esize))
    last = start + count - 1
    if start < hdr["first_usable"] or last > hdr["last_usable"]:
        raise DeviceError("Partition lies outside the GPT usable area.")
    free = None
    for i in range(n):
        e = table[i * esize:(i + 1) * esize]
        if e[:16] == b"\0" * 16:
            free = i if free is None else free
            continue
        f, l = struct.unpack_from("<QQ", e, 32)
        if not (last < f or start > l):
            raise DeviceError(f"Overlaps existing GPT partition {i + 1}.")
    if free is None:
        raise DeviceError("GPT entry array is full.")
    entry = bytearray(esize)
    entry[0:16] = uuid.UUID(_fs_to_gpt_type(fs)).bytes_le
    entry[16:32] = uuid.uuid4().bytes_le
    struct.pack_into("<QQQ", entry, 32, start, last, 0)
    nm = name.encode("utf-16-le")[:72]
    entry[56:56 + len(nm)] = nm
    table[free * esize:(free + 1) * esize] = entry
    ecrc = zlib.crc32(bytes(table))
    ent_sectors = -(-len(table) // ss)
    padded = bytes(table).ljust(ent_sectors * ss, b"\0")

    dev.write_sectors(hdr["entries_lba"], padded)
    dev.write_sectors(1, _gpt_header_bytes(hdr["raw"], hdr["header_size"], ecrc))
    alt_lba = hdr["alt_lba"] if 0 < hdr["alt_lba"] < dev.total_sectors else dev.total_sectors - 1
    bk = read_gpt_header(dev, alt_lba)
    bk_entries = bk["entries_lba"] if bk else alt_lba - ent_sectors
    dev.write_sectors(bk_entries, padded)
    dev.write_sectors(alt_lba, _gpt_header_bytes(hdr["raw"], hdr["header_size"], ecrc, my_lba=alt_lba,
                                                 alt_lba=1, entries_lba=bk_entries))
    return free + 1


def add_partition_entry(dev: Device, start: int, count: int, fs: str, name: str = "Recovered",
                        prefer_gpt: bool | None = None) -> str:
    """Write one partition entry into the disk's table, creating a table if there is none.

    Returns a short description of what was written. Caller must have opened the
    device writable and taken a backup with :func:`backup_table_area`.
    """
    pt = read_partition_table(dev, detect=False)
    if pt.scheme == "GPT":
        idx = _write_gpt_entry(dev, start, count, fs, name)
        res = f"GPT entry #{idx}"
    elif pt.scheme == "MBR":
        idx = _write_mbr_entry(dev, start, count, fs)
        res = f"MBR primary #{idx}"
    else:
        use_gpt = prefer_gpt if prefer_gpt is not None else dev.usable_size > 2 * 1024 ** 4
        if use_gpt:
            create_empty_gpt(dev)
            idx = _write_gpt_entry(dev, start, count, fs, name)
            res = f"new GPT, entry #{idx}"
        else:
            idx = _write_mbr_entry(dev, start, count, fs)
            res = f"new MBR, primary #{idx}"
    dev.flush()
    log.info("Restored partition start=%d count=%d fs=%s -> %s", start, count, fs, res)
    return res


def describe(pt: PartitionTable, ss: int) -> str:
    lines = [f"Scheme: {pt.scheme}  Disk ID: {pt.disk_guid}"]
    for p in pt.partitions:
        lines.append(f"  #{p.index:<3} {p.start_lba:>12} {human_size(p.size_bytes(ss)):>10}  {p.fs or '-':8} "
                     f"{p.type_name} {p.name}")
    lines += [f"  ! {n}" for n in pt.notes]
    return "\n".join(lines)
