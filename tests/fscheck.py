"""Helpers to prove a cloned test disk is intact: fsck each filesystem and read files back."""
import hashlib
import os
import subprocess
import tempfile


def _extract(img, start, end, out):
    with open(img, "rb") as f, open(out, "wb") as o:
        f.seek(start * 512)
        left = (end - start + 1) * 512
        while left:
            b = f.read(min(left, 8 << 20))
            o.write(b)
            left -= len(b)


def verify_disk(img, meta, samples):
    """Returns list of problems (empty = all good)."""
    problems = []
    (s1, e1), (s2, e2), (s3, e3) = meta["parts"]
    d = tempfile.mkdtemp(prefix="fscheck")
    fat, ntfs, ext = (os.path.join(d, n) for n in ("fat.img", "ntfs.img", "ext.img"))
    _extract(img, s1, e1, fat)
    _extract(img, s2, e2, ntfs)
    _extract(img, s3, e3, ext)
    r = subprocess.run(["fsck.vfat", "-n", fat], capture_output=True, text=True)
    if r.returncode != 0:
        problems.append("FAT32 fsck: " + r.stdout[-300:])
    for n in ("photo.jpg", "photo.png", "report.pdf", "letter.docx"):
        out = os.path.join(d, "x_" + n)
        subprocess.run(["mcopy", "-n", "-i", fat, f"::{n}", out], capture_output=True)
        if not os.path.exists(out) or hashlib.sha256(open(out, "rb").read()).hexdigest() != meta["hashes"][n]:
            problems.append(f"FAT file {n} differs")
    for n in ("bigfile.bin", "small.txt", "contacts.sqlite", "song.mp3"):
        r = subprocess.run(["ntfscat", "-f", ntfs, n], capture_output=True)
        if hashlib.sha256(r.stdout).hexdigest() != meta["hashes"][n]:
            problems.append(f"NTFS file {n} differs")
    r = subprocess.run(["ntfsfix", "-n", ntfs], capture_output=True, text=True)
    if "error" in r.stdout.lower() and "no errors" not in r.stdout.lower() and r.returncode != 0:
        problems.append("NTFS check: " + r.stdout[-300:])
    r = subprocess.run(["e2fsck", "-fn", ext], capture_output=True, text=True)
    if r.returncode != 0:
        problems.append("ext4 fsck: " + (r.stdout + r.stderr)[-300:])
    for p in (fat, ntfs, ext):
        os.remove(p)
    return problems


def garbage_file(path, size):
    """A disk full of random junk, so a smart clone that skips 'free' space must still produce valid filesystems."""
    with open(path, "wb") as f:
        block = os.urandom(1 << 20)
        for _ in range(size >> 20):
            f.write(block)
