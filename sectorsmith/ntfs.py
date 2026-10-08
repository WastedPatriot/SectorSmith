"""Read-only NTFS parser for listing and recovering deleted (and existing) files via the MFT."""
from __future__ import annotations

import datetime as _dt
import os
import struct
from dataclasses import dataclass, field

from .device import Device
from .util import Cancelled, Progress, get_logger

log = get_logger()

ROOT_RECORD = 5


class NTFSError(Exception):
    pass


@dataclass
class MFTEntry:
    recno: int
    seq: int
    name: str
    parent: int
    parent_seq: int
    is_dir: bool
    deleted: bool
    size: int
    mtime: _dt.datetime | None
    runs: list = field(default_factory=list)  # [(lcn or None, clusters)]
    resident: bytes | None = None
    flags: int = 0  # $DATA attribute flags (compressed/encrypted/sparse)
    path: str = ""
    has_data: bool = False
    init_size: int | None = None  # valid data length; NTFS reads anything past it as zeros
    attrs: int = 0  # file attributes from $STANDARD_INFORMATION (0x400 = reparse point, e.g. a junction)

    @property
    def recoverability(self) -> str:
        if self.is_dir:
            return "-"
        if self.flags & 0x0001:
            return "Compressed (not supported)"
        if self.flags & 0x4000:
            return "EFS encrypted"
        if not self.has_data:
            return "No data attribute (extension record)"
        return "Good" if not self.deleted else "Possible"


def _filetime(v: int):
    if not v:
        return None
    try:
        return _dt.datetime(1601, 1, 1) + _dt.timedelta(microseconds=v // 10)
    except OverflowError:
        return None


def decode_runlist(data: bytes, off: int) -> list:
    runs = []
    lcn = 0
    while off < len(data):
        hdr = data[off]
        if hdr == 0:
            break
        ln_sz, of_sz = hdr & 0x0F, hdr >> 4
        off += 1
        if ln_sz == 0 or off + ln_sz + of_sz > len(data):
            break
        length = int.from_bytes(data[off: off + ln_sz], "little")
        off += ln_sz
        if of_sz == 0:
            runs.append((None, length))  # sparse
        else:
            delta = int.from_bytes(data[off: off + of_sz], "little", signed=True)
            lcn += delta
            runs.append((lcn, length))
        off += of_sz
    return runs


class NTFSVolume:
    def __init__(self, dev: Device, part_offset: int):
        self.dev = dev
        self.base = part_offset
        bs = dev.read(part_offset, 512)
        if bs[3:11] != b"NTFS    ":
            raise NTFSError("No NTFS boot sector at this offset.")
        self.bps = struct.unpack_from("<H", bs, 11)[0]
        spc = bs[13]
        self.spc = 1 << (256 - spc) if spc > 0x80 else spc
        self.cluster = self.bps * self.spc
        self.total_sectors = struct.unpack_from("<Q", bs, 40)[0]
        self.mft_lcn = struct.unpack_from("<Q", bs, 48)[0]
        self.mftmirr_lcn = struct.unpack_from("<Q", bs, 56)[0]
        cpr = struct.unpack_from("<b", bs, 64)[0]
        self.rec_size = (1 << -cpr) if cpr < 0 else cpr * self.cluster
        self.serial = bs[72:80][::-1].hex().upper()
        rec0 = self._fixup(dev.read(self.base + self.mft_lcn * self.cluster, self.rec_size))
        if rec0 is None:
            rec0 = self._fixup(dev.read(self.base + self.mftmirr_lcn * self.cluster, self.rec_size))
            if rec0 is None:
                raise NTFSError("$MFT record 0 is unreadable (and so is the mirror).")
        e = self._parse(0, rec0)
        if not e or not e.runs:
            raise NTFSError("Could not locate the $MFT data runs.")
        self.mft_runs = e.runs
        self.mft_size = e.size
        self.n_records = e.size // self.rec_size
        self.entries: dict[int, MFTEntry] = {}

    # ------------------------------------------------------------------
    def _fixup(self, rec: bytes) -> bytes | None:
        if len(rec) < self.rec_size or rec[:4] != b"FILE":
            return None
        usa_off, usa_cnt = struct.unpack_from("<HH", rec, 4)
        if usa_cnt < 2 or usa_off + 2 * usa_cnt > len(rec):
            return None
        stride = self.rec_size // (usa_cnt - 1)
        b = bytearray(rec)
        usn = b[usa_off: usa_off + 2]
        for i in range(1, usa_cnt):
            end = i * stride - 2
            if b[end: end + 2] != usn:
                return None  # torn write
            b[end: end + 2] = b[usa_off + 2 * i: usa_off + 2 * i + 2]
        return bytes(b)

    def _parse(self, recno: int, rec: bytes) -> MFTEntry | None:
        seq, _links, attr_off, flags = struct.unpack_from("<HHHH", rec, 16)
        base_ref = struct.unpack_from("<Q", rec, 32)[0]
        if base_ref & 0xFFFFFFFFFFFF:
            return None  # extension record; attributes belong to another record
        in_use = bool(flags & 1)
        is_dir = bool(flags & 2)
        names = []
        mtime = None
        size = 0
        runs: list = []
        resident = None
        dflags = 0
        has_data = False
        init_size = None
        attrs = 0
        off = attr_off
        while off + 16 <= len(rec):
            atype, alen = struct.unpack_from("<II", rec, off)
            if atype == 0xFFFFFFFF or alen == 0 or off + alen > len(rec):
                break
            nonres = rec[off + 8]
            name_len = rec[off + 9]
            aflags = struct.unpack_from("<H", rec, off + 12)[0]
            if atype == 0x10 and not nonres:  # $STANDARD_INFORMATION
                voff = struct.unpack_from("<H", rec, off + 20)[0]
                mtime = _filetime(struct.unpack_from("<Q", rec, off + voff + 8)[0])
                if off + voff + 36 <= len(rec):
                    attrs = struct.unpack_from("<I", rec, off + voff + 32)[0]
            elif atype == 0x30 and not nonres:  # $FILE_NAME
                voff = struct.unpack_from("<H", rec, off + 20)[0]
                v = off + voff
                pref = struct.unpack_from("<Q", rec, v)[0]
                nlen, ns = rec[v + 64], rec[v + 65]
                nm = rec[v + 66: v + 66 + 2 * nlen].decode("utf-16-le", "replace")
                names.append((ns, nm, pref & 0xFFFFFFFFFFFF, pref >> 48))
            elif atype == 0x80 and name_len == 0:  # unnamed $DATA
                has_data = True
                dflags = aflags
                if nonres:
                    start_vcn = struct.unpack_from("<Q", rec, off + 16)[0]
                    ro = struct.unpack_from("<H", rec, off + 32)[0]
                    if start_vcn == 0:
                        size, init_size = struct.unpack_from("<QQ", rec, off + 48)
                    runs += decode_runlist(rec[off: off + alen], ro)
                else:
                    vlen, voff = struct.unpack_from("<IH", rec, off + 16)
                    resident = rec[off + voff: off + voff + vlen]
                    size = vlen
            off += alen
        if not names:
            return None
        # prefer Win32 / POSIX name over the 8.3 DOS name
        names.sort(key=lambda t: (t[0] == 2, t[0]))
        _ns, nm, parent, pseq = names[0]
        return MFTEntry(recno, seq, nm, parent, pseq, is_dir, not in_use, size, mtime, runs, resident, dflags,
                        has_data=has_data, init_size=init_size, attrs=attrs)

    @staticmethod
    def _ext_data(rec: bytes):
        """For an extension record (big or very fragmented files keep $DATA pieces there, listed in the base
        record's $ATTRIBUTE_LIST): (base_recno, [(start_vcn, runs, size, init_size)]), else None."""
        base_ref = struct.unpack_from("<Q", rec, 32)[0] & 0xFFFFFFFFFFFF
        if not base_ref or not struct.unpack_from("<H", rec, 22)[0] & 1:
            return None
        off = struct.unpack_from("<H", rec, 20)[0]
        pieces = []
        while off + 16 <= len(rec):
            atype, alen = struct.unpack_from("<II", rec, off)
            if atype == 0xFFFFFFFF or alen == 0 or off + alen > len(rec):
                break
            if atype == 0x80 and rec[off + 9] == 0 and rec[off + 8]:
                start_vcn = struct.unpack_from("<Q", rec, off + 16)[0]
                ro = struct.unpack_from("<H", rec, off + 32)[0]
                size, init = struct.unpack_from("<QQ", rec, off + 48) if start_vcn == 0 else (None, None)
                pieces.append((start_vcn, decode_runlist(rec[off: off + alen], ro), size, init))
            off += alen
        return (base_ref, pieces) if pieces else None

    # ------------------------------------------------------------------
    def _mft_byte_ranges(self):
        """Yield (record_index_start, device_offset, byte_length) for each $MFT run."""
        rec_idx = 0
        for lcn, clusters in self.mft_runs:
            nbytes = clusters * self.cluster
            if lcn is not None:
                yield rec_idx, self.base + lcn * self.cluster, nbytes
            rec_idx += nbytes // self.rec_size

    def read_record(self, recno: int) -> MFTEntry | None:
        """Parse a single MFT record by number."""
        for rec_start, dev_off, nbytes in self._mft_byte_ranges():
            if rec_start <= recno < rec_start + nbytes // self.rec_size:
                raw = self.dev.read(dev_off + (recno - rec_start) * self.rec_size, self.rec_size)
                rec = self._fixup(raw)
                return self._parse(recno, rec) if rec else None
        return None

    def scan(self, prog: Progress, deleted_only: bool = False) -> list[MFTEntry]:
        prog.reset(self.n_records * self.rec_size, "Reading NTFS master file table")
        entries: dict[int, MFTEntry] = {}
        ext: dict[int, list] = {}
        done = 0
        read_chunk = 4 * 1024 * 1024
        for rec_start, dev_off, nbytes in self._mft_byte_ranges():
            pos = 0
            while pos < nbytes:
                prog.check()
                n = min(read_chunk, nbytes - pos)
                try:
                    data = self.dev.read(dev_off + pos, n)
                except OSError:
                    data = b""
                for k in range(0, len(data) - self.rec_size + 1, self.rec_size):
                    recno = rec_start + (pos + k) // self.rec_size
                    rec = self._fixup(data[k: k + self.rec_size])
                    if rec is None:
                        continue
                    try:
                        e = self._parse(recno, rec)
                        x = None if e else self._ext_data(rec)
                    except (struct.error, IndexError):
                        continue
                    if e:
                        entries[recno] = e
                    elif x:
                        ext.setdefault(x[0], []).extend(x[1])
                pos += n
                done += n
                prog.update(done)
        for recno, pieces in ext.items():
            e = entries.get(recno)
            if e is None or e.is_dir or e.deleted:
                continue
            pieces.sort(key=lambda p: p[0])
            if not e.has_data:
                e.has_data = True
                e.runs = []
            elif pieces[0][0] == 0:
                continue  # the base record already holds the start; don't guess how the pieces interleave
            for _vcn, runs, size, init in pieces:
                e.runs += runs
                if size is not None:
                    e.size, e.init_size = size, init
        self.entries = entries
        self._build_paths()
        res = [e for e in entries.values() if e.recno >= 16 or e.recno == ROOT_RECORD]
        if deleted_only:
            res = [e for e in res if e.deleted]
        log.info("NTFS scan: %d records parsed, %d deleted", len(entries), sum(e.deleted for e in entries.values()))
        return res

    def _parent_ok(self, e: MFTEntry) -> bool:
        parent = self.entries.get(e.parent)
        if parent is None or e.parent == e.recno or not parent.is_dir:
            return False
        # NTFS bumps a record's sequence number when it is deleted, so a deleted parent
        # folder is legitimately one ahead of the reference its children hold.
        return e.parent_seq == 0 or parent.seq == e.parent_seq or (parent.deleted and parent.seq == e.parent_seq + 1)

    def _build_paths(self):
        cache: dict[int, str] = {ROOT_RECORD: ""}

        def dir_path(recno: int, depth: int = 0) -> str:
            if recno in cache:
                return cache[recno]
            e = self.entries.get(recno)
            if e is None or depth > 64 or not self._parent_ok(e):
                res = "?Orphaned"
            else:
                pp = dir_path(e.parent, depth + 1)
                res = f"{pp}\\{e.name}" if pp else e.name
            cache[recno] = res
            return res

        for e in self.entries.values():
            if e.recno == ROOT_RECORD:
                e.path = ""
                continue
            pp = dir_path(e.parent) if self._parent_ok(e) else "?Orphaned"
            e.path = pp if pp.startswith("?") else "\\" + pp

    # ------------------------------------------------------------------
    def recover(self, e: MFTEntry, out_path: str, prog: Progress | None = None) -> int:
        """Write the file's data to ``out_path``. Returns bytes written."""
        if e.flags & 0x0001:
            raise NTFSError("NTFS-compressed files are not supported yet.")
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        written = 0
        with open(out_path, "wb") as f:
            if e.resident is not None:
                f.write(e.resident)
                return len(e.resident)
            remaining = e.size
            # clusters past the valid data length hold stale bytes from earlier files
            valid = e.size if e.init_size is None else min(e.size, e.init_size)
            for lcn, clusters in e.runs:
                nbytes = min(clusters * self.cluster, remaining)
                if nbytes <= 0:
                    break
                if lcn is None:
                    f.write(bytes(nbytes))
                else:
                    pos = 0
                    while pos < nbytes:
                        if prog:
                            prog.check()
                        n = min(4 * 1024 * 1024, nbytes - pos)
                        keep = max(0, min(n, valid - (written + pos)))
                        try:
                            d = self.dev.read(self.base + lcn * self.cluster + pos, keep) if keep else b""
                        except OSError:
                            d = bytes(keep)
                        f.write(d[:keep].ljust(n, b"\0"))
                        pos += n
                remaining -= nbytes
                written += nbytes
        return written


def safe_rel_path(p: str) -> str:
    parts = [x for x in p.replace("/", "\\").split("\\") if x not in ("", ".", "..")]
    bad = '<>:"|?*'
    return os.path.join(*[("".join("_" if c in bad or ord(c) < 32 else c for c in x)) for x in parts]) if parts else ""


def recover_entries(vol: NTFSVolume, entries: list[MFTEntry], out_dir: str, prog: Progress,
                    keep_paths: bool = True) -> dict:
    files = [e for e in entries if not e.is_dir]
    prog.reset(max(1, sum(e.size for e in files)), f"Recovering {len(files)} file(s)")
    done = 0
    ok, failed = 0, []
    used = set()
    for e in files:
        try:
            prog.check()
        except Cancelled as c:
            c.info.update(recovered=ok, total=len(files), out_dir=out_dir)
            raise
        prog.set_detail(e.name)
        rel = safe_rel_path((e.path + "\\" + e.name) if keep_paths else e.name)
        if not rel:
            rel = f"record_{e.recno}"
        target = os.path.join(out_dir, rel)
        base, ext = os.path.splitext(target)
        k = 1
        while target.lower() in used or os.path.exists(target):
            target = f"{base} ({k}){ext}"
            k += 1
        used.add(target.lower())
        try:
            vol.recover(e, target, prog)
            ok += 1
        except Cancelled as c:
            try:
                os.remove(target)  # half a file is worse than none: it looks recovered but isn't
            except OSError:
                pass
            c.info.update(recovered=ok, total=len(files), out_dir=out_dir)
            raise
        except Exception as ex:  # noqa: BLE001
            failed.append(f"{e.name}: {ex}")
        done += e.size
        prog.update(done)
    return {"recovered": ok, "failed": failed, "out_dir": out_dir}
