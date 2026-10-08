"""Build a realistic GPT test disk image (FAT32 + NTFS + ext4 + loose files) for SectorSmith tests.

Linux only; needs sgdisk, mkfs.vfat, mtools, mkntfs/ntfscp, mkfs.ext4 and Pillow.
Usage: python build_test_disk.py <workdir>
"""
import io
import json
import os
import sqlite3
import struct
import subprocess
import sys
import wave
import zipfile
import hashlib

from PIL import Image

W = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sstest"
os.makedirs(W, exist_ok=True)
SAMPLES = os.path.join(W, "samples")
os.makedirs(SAMPLES, exist_ok=True)
MB = 1024 * 1024


def run(*cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def make_samples():
    files = {}
    img = Image.new("RGB", (640, 480))
    px = img.load()
    for x in range(640):
        for y in range(480):
            px[x, y] = ((x * 7) % 256, (y * 3) % 256, (x * y) % 256)
    for ext, fmt in (("jpg", "JPEG"), ("png", "PNG"), ("gif", "GIF"), ("bmp", "BMP"), ("webp", "WEBP")):
        b = io.BytesIO()
        img.save(b, fmt, **({"quality": 85, "progressive": True} if fmt == "JPEG" else {}))
        files[f"photo.{ext}"] = b.getvalue()
    # PDF with an incremental update
    pdf = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
           b"xref\n0 3\n0000000000 65535 f \ntrailer<</Size 3/Root 1 0 R>>\nstartxref\n9\n%%EOF\n")
    pdf += b"3 0 obj<</Producer(test)>>endobj\nxref\n0 1\ntrailer<</Size 4/Root 1 0 R/Prev 9>>\nstartxref\n120\n%%EOF\n"
    files["report.pdf"] = pdf + os.urandom(3000).replace(b"%%EOF", b"xxxxx")[:0]
    # docx-like
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", "<w:document>" + "hello " * 5000 + "</w:document>")
    files["letter.docx"] = b.getvalue()
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w", zipfile.ZIP_STORED) as z:
        z.writestr("readme.txt", "plain zip " * 300)
        z.writestr("data.bin", os.urandom(20000))
    files["archive.zip"] = b.getvalue()
    # sqlite
    p = os.path.join(W, "db.sqlite")
    if os.path.exists(p):
        os.remove(p)
    c = sqlite3.connect(p)
    c.execute("create table t(a,b)")
    c.executemany("insert into t values(?,?)", [(i, "x" * 100) for i in range(2000)])
    c.commit()
    c.close()
    files["contacts.sqlite"] = open(p, "rb").read()
    # wav
    b = io.BytesIO()
    with wave.open(b, "wb") as wv:
        wv.setnchannels(1)
        wv.setsampwidth(2)
        wv.setframerate(8000)
        wv.writeframes(os.urandom(32000))
    files["voice.wav"] = b.getvalue()
    # mp3: ID3v2 header + 40 MPEG1 L3 128k/44.1k frames (417 bytes each)
    frame = b"\xff\xfb\x90\x00" + bytes(413)
    files["song.mp3"] = b"ID3\x03\x00\x00\x00\x00\x00\x0a" + bytes(10) + frame * 40
    # mp4: ftyp + moov + mdat
    ftyp = struct.pack(">I4s4sI4s4s", 24, b"ftyp", b"isom", 512, b"isom", b"mp41")
    moov = struct.pack(">I4s", 8 + 100, b"moov") + bytes(100)
    mdat_payload = os.urandom(50000)
    mdat = struct.pack(">I4s", 8 + len(mdat_payload), b"mdat") + mdat_payload
    files["clip.mp4"] = ftyp + moov + mdat
    # a big-ish random file for NTFS non-resident + fragmentation-free recovery
    files["bigfile.bin"] = os.urandom(3 * MB + 1234)
    files["small.txt"] = b"tiny resident file\r\n"
    for name, data in files.items():
        open(os.path.join(SAMPLES, name), "wb").write(data)
    return files


def build():
    files = make_samples()
    disk = os.path.join(W, "disk.img")
    with open(disk, "wb") as f:
        f.truncate(256 * MB)
    run("sgdisk", "-Z", disk)
    run("sgdisk", "-n1:2048:+40M", "-t1:0700", "-c1:FATPART", "-n2:0:+80M", "-t2:0700", "-c2:NTFSPART",
        "-n3:0:+60M", "-t3:8300", "-c3:LINUX", disk)
    out = subprocess.run(["sgdisk", "-p", disk], capture_output=True, text=True).stdout
    parts = []
    for line in out.splitlines():
        cols = line.split()
        if cols and cols[0].isdigit():
            parts.append((int(cols[1]), int(cols[2])))
    (s1, e1), (s2, e2), (s3, e3) = parts

    def place(img, start):
        with open(img, "rb") as src, open(disk, "r+b") as dst:
            dst.seek(start * 512)
            dst.write(src.read())

    p1 = os.path.join(W, "p1.img")
    with open(p1, "wb") as f:
        f.truncate((e1 - s1 + 1) * 512)
    run("mkfs.vfat", "-F", "32", "-n", "FATVOL", "-h", str(s1), p1)
    for n in ("photo.jpg", "photo.png", "report.pdf", "letter.docx"):
        run("mcopy", "-i", p1, os.path.join(SAMPLES, n), f"::{n}")
    place(p1, s1)

    p2 = os.path.join(W, "p2.img")
    with open(p2, "wb") as f:
        f.truncate((e2 - s2 + 1) * 512)
    run("mkntfs", "-F", "-Q", "-s", "512", "-c", "4096", "-p", str(s2), "-L", "NTFSVOL", p2)
    for n in ("bigfile.bin", "small.txt", "photo.jpg", "contacts.sqlite", "song.mp3"):
        run("ntfscp", "-f", p2, os.path.join(SAMPLES, n), n)
    place(p2, s2)

    p3 = os.path.join(W, "p3.img")
    with open(p3, "wb") as f:
        f.truncate((e3 - s3 + 1) * 512)
    run("mkfs.ext4", "-F", "-q", "-L", "EXTVOL", p3)
    place(p3, s3)

    # loose files in unallocated space after partition 3 (simulates a formatted area)
    loose = {}
    off = (e3 + 1 + 2048) * 512
    with open(disk, "r+b") as f:
        for n, d in files.items():
            if n in ("bigfile.bin", "small.txt"):
                continue
            off = -(-off // 4096) * 4096
            f.seek(off)
            f.write(d)
            loose[n] = (off, len(d), hashlib.sha256(d).hexdigest())
            off += len(d) + 4096
    meta = {"parts": parts, "loose": loose,
            "hashes": {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}}
    json.dump(meta, open(os.path.join(W, "meta.json"), "w"), indent=1)
    for p in (p1, p2, p3):
        os.remove(p)
    print(json.dumps({"disk": disk, "parts": parts}))


if __name__ == "__main__":
    build()
