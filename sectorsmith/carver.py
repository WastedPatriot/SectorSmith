"""Signature-based file carving ("RAW recovery"): finds files by their headers and structure,
independent of any filesystem. Works on formatted, RAW or partially overwritten disks."""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from .device import Device
from .util import Progress, get_logger

log = get_logger()

MB = 1024 * 1024


class Source:
    """Random-access reader over a device region with a small block cache."""

    BLOCK = 1 * MB

    def __init__(self, dev: Device, limit: int):
        self.dev = dev
        self.limit = limit
        self._cache: dict[int, bytes] = {}

    def read(self, off: int, n: int) -> bytes:
        if off >= self.limit or n <= 0:
            return b""
        n = min(n, self.limit - off)
        out = bytearray()
        pos = off
        while len(out) < n:
            b = pos // self.BLOCK
            blk = self._cache.get(b)
            if blk is None:
                try:
                    blk = self.dev.read(b * self.BLOCK, self.BLOCK)
                except OSError:
                    blk = bytes(min(self.BLOCK, self.limit - b * self.BLOCK))
                if len(self._cache) > 64:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[b] = blk
            within = pos - b * self.BLOCK
            take = blk[within: within + (n - len(out))]
            if not take:
                break
            out += take
            pos += len(take)
        return bytes(out)

    def find(self, pat: bytes, start: int, max_len: int) -> int:
        """Find ``pat`` in [start, start+max_len). Returns absolute offset or -1."""
        end = min(self.limit, start + max_len)
        pos = start
        while pos < end:
            n = min(4 * MB, end - pos)
            buf = self.read(pos, n + len(pat) - 1)
            i = buf.find(pat)
            if i != -1 and pos + i + len(pat) <= end:
                return pos + i
            pos += n
        return -1


# ---------------------------------------------------------------------------
# Format parsers: each returns (length, ext) or None. ``o`` is the absolute offset.
# ---------------------------------------------------------------------------
def _jpeg(s: Source, o: int, maxlen: int):
    pos = o + 2
    end = o + maxlen
    while pos < end:
        h = s.read(pos, 4)
        if len(h) < 4 or h[0] != 0xFF:
            return None
        m = h[1]
        if m == 0xD9:
            return pos + 2 - o, "jpg"
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            pos += 2
            continue
        seglen = struct.unpack(">H", h[2:4])[0]
        if seglen < 2:
            return None
        pos += 2 + seglen
        if m == 0xDA:  # start of scan: entropy-coded data runs until the next real marker
            while True:
                if pos >= end:
                    return None
                buf = s.read(pos, MB)
                if len(buf) < 2:
                    return None
                i = buf.find(b"\xff")
                while i != -1 and i + 1 < len(buf):
                    nb = buf[i + 1]
                    if nb == 0x00 or 0xD0 <= nb <= 0xD7 or nb == 0xFF:
                        i = buf.find(b"\xff", i + 1)
                        continue
                    break
                if i != -1 and i + 1 < len(buf):
                    pos += i
                    break
                pos += len(buf) - 1
    return None


def _png(s: Source, o: int, maxlen: int):
    pos = o + 8
    while pos - o < maxlen:
        h = s.read(pos, 8)
        if len(h) < 8:
            return None
        ln, typ = struct.unpack(">I4s", h)
        if not typ.isalpha() or ln > maxlen:
            return None
        pos += 12 + ln
        if typ == b"IEND":
            return pos - o, "png"
    return None


def _gif(s: Source, o: int, maxlen: int):
    h = s.read(o, 13)
    if len(h) < 13:
        return None
    flags = h[10]
    pos = o + 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)

    def skip_sub(p):
        while p - o < maxlen:
            b = s.read(p, 1)
            if not b:
                return None
            if b[0] == 0:
                return p + 1
            p += 1 + b[0]
        return None

    while pos - o < maxlen:
        b = s.read(pos, 1)
        if not b:
            return None
        if b[0] == 0x3B:
            return pos + 1 - o, "gif"
        if b[0] == 0x21:
            pos = skip_sub(pos + 2)
        elif b[0] == 0x2C:
            d = s.read(pos, 10)
            if len(d) < 10:
                return None
            lf = d[9]
            pos += 10 + (3 * (2 << (lf & 7)) if lf & 0x80 else 0) + 1
            pos = skip_sub(pos)
        else:
            return None
        if pos is None:
            return None
    return None


def _pdf(s: Source, o: int, maxlen: int):
    e = s.find(b"%%EOF", o, maxlen)
    if e == -1:
        return None
    end = e + 5
    # incremental updates append more %%EOF markers; follow them while they are close
    while True:
        nxt = s.find(b"%%EOF", end, min(4 * MB, maxlen - (end - o)))
        if nxt == -1:
            break
        between = s.read(end, nxt - end)
        if b"startxref" not in between:
            break
        end = nxt + 5
    tail = s.read(end, 2)
    end += len(tail) - len(tail.lstrip(b"\r\n"))
    return end - o, "pdf"


def _zip(s: Source, o: int, maxlen: int):
    pos = o
    while True:
        e = s.find(b"PK\x05\x06", pos, maxlen - (pos - o))
        if e == -1:
            return None
        eocd = s.read(e, 22)
        if len(eocd) < 22:
            return None
        cd_size, cd_off, clen = struct.unpack_from("<IIH", eocd, 12)
        if e - o == cd_off + cd_size:
            first = s.read(o, 30 + 64)
            name_len = struct.unpack_from("<H", first, 26)[0] if len(first) >= 30 else 0
            fname = first[30:30 + name_len]
            cd = s.read(o + cd_off, min(cd_size, 256 * 1024))
            ext = "zip"
            if b"word/" in cd:
                ext = "docx"
            elif b"xl/" in cd:
                ext = "xlsx"
            elif b"ppt/" in cd:
                ext = "pptx"
            elif fname == b"mimetype":
                mt = s.read(o + 30 + name_len, 60)
                ext = "odt" if b"opendocument.text" in mt else "ods" if b"spreadsheet" in mt else (
                    "odp" if b"presentation" in mt else "epub" if b"epub" in mt else "zip")
            elif b"META-INF/MANIFEST.MF" in cd:
                ext = "jar"
            elif b"AndroidManifest.xml" in cd:
                ext = "apk"
            return e + 22 + clen - o, ext
        pos = e + 4


def _bmp(s: Source, o: int, maxlen: int):
    h = s.read(o, 30)
    if len(h) < 30:
        return None
    size, res, data_off, dib = struct.unpack_from("<IIII", h, 2)
    if res != 0 or dib not in (12, 40, 52, 56, 108, 124) or not (26 <= data_off < size <= maxlen):
        return None
    bpp = struct.unpack_from("<H", h, 28)[0]
    if bpp not in (1, 4, 8, 16, 24, 32):
        return None
    return size, "bmp"


def _riff(s: Source, o: int, maxlen: int):
    h = s.read(o, 12)
    size = struct.unpack_from("<I", h, 4)[0] + 8
    form = h[8:12]
    ext = {b"WAVE": "wav", b"AVI ": "avi", b"WEBP": "webp"}.get(form)
    if not ext or size > maxlen or size < 44:
        return None
    return size, ext


_ISO_BOXES = {b"ftyp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid", b"meta", b"pdin", b"moof",
              b"mfra", b"sidx", b"styp", b"emsg", b"prft", b"udta", b"pnot", b"junk", b"iinf", b"iloc",
              b"idat", b"iref", b"pitm", b"iprp", b"dinf", b"hdlr"}


def _isobmff(s: Source, o: int, maxlen: int):
    h = s.read(o, 12)
    brand = h[8:12]
    pos = o
    saw_media = False
    while pos - o < maxlen:
        bh = s.read(pos, 16)
        if len(bh) < 8:
            break
        size, typ = struct.unpack(">I4s", bh[:8])
        if typ not in _ISO_BOXES:
            break
        if size == 1:
            size = struct.unpack(">Q", bh[8:16])[0]
        elif size == 0:
            break
        if size < 8:
            break
        if typ in (b"moov", b"mdat", b"meta", b"moof"):
            saw_media = True
        pos += size
    if not saw_media or pos - o > maxlen:
        return None
    ext = "mov" if brand == b"qt  " else "m4a" if brand.startswith(b"M4A") else (
        "heic" if brand in (b"heic", b"heix", b"mif1", b"msf1") else "3gp" if brand.startswith(b"3g") else "mp4")
    return pos - o, ext


def _7z(s: Source, o: int, maxlen: int):
    h = s.read(o, 32)
    if len(h) < 32:
        return None
    nh_off, nh_size = struct.unpack_from("<QQ", h, 12)
    total = 32 + nh_off + nh_size
    return (total, "7z") if 32 < total <= maxlen else None


def _sqlite(s: Source, o: int, maxlen: int):
    h = s.read(o, 100)
    ps = struct.unpack_from(">H", h, 16)[0]
    ps = 65536 if ps == 1 else ps
    change, pages = struct.unpack_from(">II", h, 24)
    valid_for = struct.unpack_from(">I", h, 92)[0]
    if ps < 512 or ps & (ps - 1) or pages == 0 or change != valid_for:
        return None
    total = ps * pages
    return (total, "sqlite") if total <= maxlen else None


def _ole(s: Source, o: int, maxlen: int):
    h = s.read(o, 512)
    if len(h) < 512:
        return None
    shift = struct.unpack_from("<H", h, 30)[0]
    if shift not in (9, 12):
        return None
    ssz = 1 << shift
    n_fat = struct.unpack_from("<I", h, 44)[0]
    difat_start, n_difat = struct.unpack_from("<II", h, 68)
    fat_secs = [x for x in struct.unpack_from("<109I", h, 76) if x < 0xFFFFFFFA][:n_fat]
    nxt = difat_start
    guard = 0
    while len(fat_secs) < n_fat and nxt < 0xFFFFFFFA and guard < 10000:
        d = s.read(o + (nxt + 1) * ssz, ssz)
        vals = struct.unpack(f"<{ssz // 4}I", d) if len(d) == ssz else ()
        if not vals:
            break
        fat_secs += [x for x in vals[:-1] if x < 0xFFFFFFFA]
        nxt = vals[-1]
        guard += 1
    max_used = 0
    per = ssz // 4
    for i, fs in enumerate(fat_secs[:n_fat]):
        d = s.read(o + (fs + 1) * ssz, ssz)
        if len(d) < ssz:
            break
        vals = struct.unpack(f"<{per}I", d)
        for j in range(per - 1, -1, -1):
            if vals[j] != 0xFFFFFFFF:
                max_used = max(max_used, i * per + j)
                break
    total = (max_used + 2) * ssz
    if total > maxlen or total < 1024:
        return None
    head = s.read(o, min(total, 2 * MB))
    ext = "doc" if "WordDocument".encode("utf-16-le") in head else "xls" if "Workbook".encode("utf-16-le") in head \
        else "ppt" if "PowerPoint Document".encode("utf-16-le") in head else "msg" if b"_\x00_\x00s\x00u\x00b\x00s\x00t\x00g\x00" in head \
        else "ole"
    return total, ext


_MP3_BR = {1: [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320],  # MPEG1 L3
           2: [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]}  # MPEG2/2.5 L3
_MP3_SR = {3: [44100, 48000, 32000], 2: [22050, 24000, 16000], 0: [11025, 12000, 8000]}


def _mp3(s: Source, o: int, maxlen: int):
    h = s.read(o, 10)
    if len(h) < 10:
        return None
    tag = ((h[6] & 0x7F) << 21) | ((h[7] & 0x7F) << 14) | ((h[8] & 0x7F) << 7) | (h[9] & 0x7F)
    pos = o + 10 + tag + (10 if h[5] & 0x10 else 0)
    frames = 0
    while pos - o < maxlen:
        fh = s.read(pos, 4)
        if len(fh) < 4 or fh[0] != 0xFF or (fh[1] & 0xE0) != 0xE0:
            break
        ver = (fh[1] >> 3) & 3
        layer = (fh[1] >> 1) & 3
        bri = fh[2] >> 4
        sri = (fh[2] >> 2) & 3
        pad = (fh[2] >> 1) & 1
        if layer != 1 or ver == 1 or bri in (0, 15) or sri == 3:
            break
        br = _MP3_BR[1 if ver == 3 else 2][bri] * 1000
        sr = _MP3_SR[ver][sri]
        flen = (144 if ver == 3 else 72) * br // sr + pad
        if flen < 24:
            break
        pos += flen
        frames += 1
    if frames < 10:
        return None
    if s.read(pos, 3) == b"TAG":
        pos += 128
    return pos - o, "mp3"


@dataclass(frozen=True)
class Sig:
    name: str
    magic: bytes
    offset: int  # where the magic sits relative to file start
    parser: object
    max_size: int
    group: str


SIGNATURES = [
    Sig("JPEG", b"\xff\xd8\xff", 0, _jpeg, 64 * MB, "Photos"),
    Sig("PNG", b"\x89PNG\r\n\x1a\n", 0, _png, 128 * MB, "Photos"),
    Sig("GIF", b"GIF8", 0, _gif, 64 * MB, "Photos"),
    Sig("BMP", b"BM", 0, _bmp, 256 * MB, "Photos"),
    Sig("HEIC/MP4/MOV/M4A/3GP", b"ftyp", 4, _isobmff, 8192 * MB, "Video & audio"),
    Sig("WAV/AVI/WEBP", b"RIFF", 0, _riff, 4096 * MB, "Video & audio"),
    Sig("MP3 (ID3)", b"ID3", 0, _mp3, 256 * MB, "Video & audio"),
    Sig("PDF", b"%PDF-", 0, _pdf, 512 * MB, "Documents"),
    Sig("Office/ZIP (docx, xlsx, pptx, odt, zip...)", b"PK\x03\x04", 0, _zip, 2048 * MB, "Documents"),
    Sig("Legacy Office (doc, xls, ppt, msg)", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 0, _ole, 1024 * MB, "Documents"),
    Sig("7-Zip", b"7z\xbc\xaf\x27\x1c", 0, _7z, 8192 * MB, "Archives & data"),
    Sig("SQLite DB", b"SQLite format 3\x00", 0, _sqlite, 4096 * MB, "Archives & data"),
]


def carve(dev: Device, out_dir: str, prog: Progress, sigs: list[Sig] | None = None, start_lba: int = 0,
          end_lba: int | None = None, align: int | None = None, min_size: int = 64, skip_carved: bool = True,
          on_file=None, max_files: int = 1_000_000) -> dict:
    """Scan a region and write every recognisable file to ``out_dir/<ext>/``.

    ``align`` (bytes) is where file starts may occur — the sector size by default
    (files always begin on a cluster boundary, and clusters are multiples of sectors).
    """
    sigs = sigs or SIGNATURES
    ss = dev.sector_size
    align = align or ss
    end_lba = min(end_lba or dev.total_sectors, dev.total_sectors)
    start, end = start_lba * ss, end_lba * ss
    dev.open(writable=False)
    src = Source(dev, end)
    os.makedirs(out_dir, exist_ok=True)
    prog.reset(end - start, "Deep scan (file signatures)")
    counts: dict[str, int] = {}
    total_files = 0
    chunk = 8 * MB
    pos = start
    resume_at = start
    maxmagic = max(len(s.magic) + s.offset for s in sigs)
    try:
        while pos < end and total_files < max_files:
            prog.check()
            n = min(chunk, end - pos)
            buf = src.read(pos, n + maxmagic)
            hits = []
            for sg in sigs:
                k = buf.find(sg.magic)
                while k != -1:
                    fstart = pos + k - sg.offset
                    if k < n + sg.offset and fstart % align == 0 and fstart >= start:
                        hits.append((fstart, sg))
                    k = buf.find(sg.magic, k + 1)
            hits.sort(key=lambda t: t[0])
            for fstart, sg in hits:
                if fstart < resume_at and skip_carved:
                    continue
                if fstart >= pos + n or fstart < pos:
                    continue
                try:
                    r = sg.parser(src, fstart, min(sg.max_size, end - fstart))
                except (struct.error, IndexError, ValueError, KeyError):
                    r = None
                if not r:
                    continue
                length, ext = r
                if length < min_size:
                    continue
                folder = os.path.join(out_dir, ext)
                os.makedirs(folder, exist_ok=True)
                fname = os.path.join(folder, f"{ext}_{fstart // ss:012d}.{ext}")
                _copy_out(src, fstart, length, fname, prog)
                counts[ext] = counts.get(ext, 0) + 1
                total_files += 1
                prog.set_detail(f"{total_files} files found — last: {os.path.basename(fname)}")
                if on_file:
                    on_file(fname, length, ext, fstart // ss)
                if skip_carved:
                    resume_at = fstart + length
            pos += n
            if resume_at > pos:
                pos = resume_at - resume_at % align
            prog.update(pos - start)
    finally:
        dev.close()
    log.info("Carving done: %s", counts)
    return {"files": total_files, "by_type": counts, "out_dir": out_dir}


def _copy_out(src: Source, off: int, length: int, path: str, prog: Progress):
    with open(path, "wb") as f:
        p = 0
        while p < length:
            prog.check()
            d = src.read(off + p, min(4 * MB, length - p))
            if not d:
                break
            f.write(d)
            p += len(d)
