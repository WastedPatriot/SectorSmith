"""End-to-end engine tests against the image from build_test_disk.py.

Usage: python tests/test_core.py <workdir>
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sectorsmith import carver, ntfs, partitions, partscan, surface, wipe  # noqa: E402
from sectorsmith.device import open_image  # noqa: E402
from sectorsmith.util import Progress  # noqa: E402

W = sys.argv[1] if len(sys.argv) > 1 else "/tmp/sstest"
meta = json.load(open(os.path.join(W, "meta.json")))
SRC = os.path.join(W, "disk.img")
OK = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    OK.append(bool(cond))


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def fresh(name):
    p = os.path.join(W, name)
    shutil.copyfile(SRC, p)
    return p


# 1. partition table -------------------------------------------------------
dev = open_image(SRC)
pt = partitions.read_partition_table(dev)
print(partitions.describe(pt, 512))
check(pt.scheme == "GPT" and len(pt.partitions) == 3, "GPT with 3 partitions parsed")
check([p.fs for p in pt.partitions] == ["FAT32", "NTFS", "ext4"], "filesystems detected")
check(pt.partitions[0].label == "FATVOL" and pt.partitions[2].label == "EXTVOL", "labels read")
check(not pt.notes, "CRC checks pass")
dev.close()

# 2. lost partition search after destroying the table ---------------------
p = fresh("lost.img")
d = open_image(p)
d.open(writable=True)
d.write(0, bytes(34 * 512))
d.write(d.usable_size - 33 * 512, bytes(33 * 512))
d.close()
check(partitions.read_partition_table(d).scheme == "None", "table destroyed")
for mode in ("quick", "full"):
    t = time.time()
    found = partscan.scan_partitions(d, Progress(1), mode=mode)
    got = [(f.start_lba, f.fs) for f in found]
    print(mode, got, f"{time.time() - t:.2f}s", [(f.sectors, f.source) for f in found])
    check(got == [(2048, "FAT32"), (83968, "NTFS"), (247808, "ext4")], f"{mode} scan finds all 3 partitions")
    exp = [e - s + 1 for s, e in meta["parts"]]
    check([f.sectors for f in found] == exp, f"{mode} scan sizes exact")

# restore them onto the blank disk
partitions.backup_table_area(d, W)
d.open(writable=True)
for f in found:
    partitions.add_partition_entry(d, f.start_lba, f.sectors, f.fs, name="Recovered", prefer_gpt=True)
d.close()
pt2 = partitions.read_partition_table(d)
print(partitions.describe(pt2, 512))
check(pt2.scheme == "GPT" and [(x.start_lba, x.fs) for x in pt2.partitions] ==
      [(2048, "FAT32"), (83968, "NTFS"), (247808, "ext4")] and not pt2.notes, "restored GPT is valid (our parser)")
v = subprocess.run(["sgdisk", "-v", p], capture_output=True, text=True).stdout
check("No problems found" in v, "restored GPT passes sgdisk -v")
# MBR restore path
p = fresh("lost_mbr.img")
d = open_image(p)
d.open(writable=True)
d.write(0, bytes(34 * 512))
d.write(d.usable_size - 33 * 512, bytes(33 * 512))
for f in found:
    partitions.add_partition_entry(d, f.start_lba, f.sectors, f.fs, prefer_gpt=False)
d.close()
pt3 = partitions.read_partition_table(d)
check(pt3.scheme == "MBR" and [x.fs for x in pt3.partitions] == ["FAT32", "NTFS", "ext4"], "restored MBR valid")
sf = subprocess.run(["sfdisk", "-d", p], capture_output=True, text=True).stdout
check("start=        2048" in sf and "type=7" in sf and "type=83" in sf, "sfdisk agrees with MBR")

# 3. NTFS: list, 'delete' (clear in-use flag), recover ------------------------
p = fresh("ntfs.img")
d = open_image(p)
vol = ntfs.NTFSVolume(d, meta["parts"][1][0] * 512)
ents = vol.scan(Progress(1))
names = {e.name: e for e in ents if not e.is_dir}
check(all(n in names for n in ("bigfile.bin", "small.txt", "photo.jpg", "contacts.sqlite", "song.mp3")),
      "NTFS user files listed")
# simulate deletion of bigfile.bin and small.txt: clear in-use flag in their MFT records
d.open(writable=True)
for n in ("bigfile.bin", "small.txt"):
    rec = names[n].recno
    off = None
    for rs, doff, nb in vol._mft_byte_ranges():
        if rs <= rec < rs + nb // vol.rec_size:
            off = doff + (rec - rs) * vol.rec_size
    raw = bytearray(d.read(off, vol.rec_size))
    raw[22] &= ~1
    d.write(off, bytes(raw))
d.close()
vol = ntfs.NTFSVolume(d, meta["parts"][1][0] * 512)
deleted = vol.scan(Progress(1), deleted_only=True)
dn = {e.name: e for e in deleted}
check("bigfile.bin" in dn and "small.txt" in dn, "deleted files detected")
check(dn["bigfile.bin"].path == "\\", "deleted file path resolves to root")
out = os.path.join(W, "ntfs_out")
shutil.rmtree(out, ignore_errors=True)
r = ntfs.recover_entries(vol, [dn["bigfile.bin"], dn["small.txt"]], out, Progress(1))
check(sha(os.path.join(out, "bigfile.bin")) == meta["hashes"]["bigfile.bin"], "bigfile.bin recovered byte-exact")
check(sha(os.path.join(out, "small.txt")) == meta["hashes"]["small.txt"], "resident small.txt recovered byte-exact")

# 4. carving ----------------------------------------------------------------
out = os.path.join(W, "carve_out")
shutil.rmtree(out, ignore_errors=True)
t = time.time()
res = carver.carve(open_image(SRC), out, Progress(1))
print("carve", res["by_type"], f"{time.time() - t:.2f}s")
carved = {}
for root, _, fs in os.walk(out):
    for f in fs:
        carved[sha(os.path.join(root, f))] = f
for n, (off, ln, h) in meta["loose"].items():
    check(h in carved, f"carved {n} exact ({carved.get(h, 'missing')})")

# 5. surface scan + imaging + vhd ------------------------------------------
blocks = []
r = surface.surface_scan(open_image(SRC), Progress(1), on_block=lambda *a: blocks.append(a))
check(r["bad_sectors"] == 0 and len(blocks) == 256, f"surface scan clean ({r['counts']})")
img_out = os.path.join(W, "copy.vhd")
r = surface.image_copy(open_image(SRC), img_out, Progress(1), fmt="vhd")
check(os.path.getsize(img_out) == 256 * 1024 * 1024 + 512, "vhd size = data + footer")
qi = subprocess.run(["qemu-img", "info", "--output=json", "-f", "vpc", img_out], capture_output=True, text=True)
check('"virtual-size": 268435456' in qi.stdout.replace("\n", ""), "qemu-img recognises the VHD")
check(r["sha256"] == sha(SRC), "image hash matches source")
vd = open_image(img_out)
check(vd.data_size == 256 * 1024 * 1024 and partitions.read_partition_table(vd).scheme == "GPT",
      "VHD re-opens as device")
# clone to another 'disk'
dst = open_image(fresh("clone_target.img"))
dst.open(writable=True)
dst.write(0, bytes(1024 * 1024))
dst.close()
surface.image_copy(open_image(SRC), dst, Progress(1))
check(sha(dst.path) == sha(SRC), "disk-to-disk clone identical")

# 6. wipe ------------------------------------------------------------------
p = fresh("wipe.img")
d = open_image(p)
for mname in ("Zero fill (1 pass)", "DoD 5220.22-M (3 pass + verify)"):
    pr = Progress(1)
    t = time.time()
    s1, n1 = meta["parts"][0][0], meta["parts"][0][1] - meta["parts"][0][0] + 1
    r = wipe.wipe_disk(d, wipe.METHODS[mname], pr, start_lba=s1, sectors=n1)
    dt = time.time() - t
    print(mname, r, f"{n1 * 512 * len(wipe.METHODS[mname].passes) / dt / 1e6:.0f} MB/s")
    check(r["verify_mismatched_blocks"] == 0 and r["write_errors"] == 0, f"{mname}: verified clean")
raw = open(p, "rb").read()
check(raw[2048 * 512: 2048 * 512 + 4096] != open(SRC, "rb").read()[2048 * 512: 2048 * 512 + 4096],
      "FAT partition content destroyed")
check(raw[83968 * 512: 83968 * 512 + 512] == open(SRC, "rb").read()[83968 * 512: 83968 * 512 + 512],
      "neighbouring NTFS partition untouched")
check(raw[:17408] == open(SRC, "rb").read()[:17408], "partition table untouched")
# free space + shred
fs_dir = os.path.join(W, "fsw")
os.makedirs(fs_dir, exist_ok=True)
tf = os.path.join(fs_dir, "secret.txt")
open(tf, "wb").write(b"secret" * 1000)
wipe.shred_paths([tf], wipe.METHODS["DoD 5220.22-M (3 pass + verify)"], Progress(1))
check(not os.path.exists(tf), "file shredded and removed")
# cancel works
pr = Progress(1)
pr.cancel()
try:
    wipe.wipe_disk(d, wipe.METHODS["Zero fill (1 pass)"], pr)
    check(False, "cancel")
except Exception as e:  # noqa: BLE001
    check(type(e).__name__ == "Cancelled", "cancel stops a wipe")

# 7. simulated bad sectors ---------------------------------------------------
BAD = {5000, 5001, 70000}


def make_flaky(path):
    d = open_image(path)
    orig = d._raw_read

    def flaky(off, n):
        first, last = off // 512, (off + n - 1) // 512
        if any(first <= b <= last for b in BAD):
            raise OSError(23, "Data error (cyclic redundancy check)")
        return orig(off, n)
    d._raw_read = flaky
    return d


r = surface.surface_scan(make_flaky(SRC), Progress(1))
check(sorted(r["bad_lbas"]) == sorted(BAD) and r["counts"]["bad"] == 2, f"surface scan pinpoints bad LBAs {r['bad_lbas']}")
outp = os.path.join(W, "flaky.img")
r = surface.image_copy(make_flaky(SRC), outp, Progress(1))
src_b, out_b = open(SRC, "rb").read(), open(outp, "rb").read()
diff = [i // 512 for i in range(0, len(src_b), 512) if src_b[i:i + 512] != out_b[i:i + 512]]
check(sorted(r["bad_lbas"]) == sorted(BAD) and set(diff) <= BAD, f"imaging skips only bad sectors (diff {diff})")
check(r["sha256"] == sha(outp), "hash re-computed for image with gaps")

# 8. snapshot overlay routing + cloning onto a bigger disk -------------------------
base = open_image(SRC)
snapfile = os.path.join(W, "fake_snapshot.bin")
p1s, p1e = meta["parts"][0]
plen = (p1e - p1s + 1) * 512
with open(snapfile, "wb") as f:
    f.write(bytes([0xAB]) * plen)
reader = open_image(snapfile)
base._overlay = [(p1s * 512, plen, reader)]
chunk = base.read(p1s * 512 - 4096, 8192)   # straddles the start of the overlaid partition
check(chunk[:4096] == open(SRC, "rb").read()[p1s * 512 - 4096: p1s * 512] and chunk[4096:] == b"\xab" * 4096,
      "snapshot overlay serves partition reads, direct reads elsewhere")
tail = base.read((p1e + 1) * 512 - 512, 1024)
check(tail[:512] == b"\xab" * 512 and tail[512:] == open(SRC, "rb").read()[(p1e + 1) * 512: (p1e + 1) * 512 + 512],
      "overlay boundary at partition end is exact")
base._overlay = []
base.close()
reader.close()

big = os.path.join(W, "bigger_target.img")
with open(big, "wb") as f:
    f.truncate(512 * 1024 * 1024)
bdev = open_image(big)
r = surface.image_copy(open_image(SRC), bdev, Progress(1))
check(r["grown"] and "unallocated" in r["grown"], f"clone onto bigger disk: {r['grown']}")
v = subprocess.run(["sgdisk", "-v", big], capture_output=True, text=True).stdout
check("No problems found" in v, "bigger target passes sgdisk -v (backup GPT moved to the end)")
ptb = partitions.read_partition_table(open_image(big))
check([(x.start_lba, x.fs) for x in ptb.partitions] == [(2048, "FAT32"), (83968, "NTFS"), (247808, "ext4")]
      and not ptb.notes, "partitions intact on the bigger disk")

# 9. smart (used-space) imaging onto junk ----------------------------------------
sys.path.insert(0, os.path.dirname(__file__))
from fscheck import garbage_file, verify_disk  # noqa: E402
from sectorsmith import usedmap  # noqa: E402
plan = usedmap.copy_plan(open_image(SRC))
check(plan["copy"] < plan["total"] * .5 and all(p["mode"] == "used" for p in plan["by_partition"]),
      f"used-space plan understands FAT32/NTFS/ext4 ({plan['copy'] >> 20} of {plan['total'] >> 20} MiB)")
junk = os.path.join(W, "junk_target.img")
garbage_file(junk, 256 * 1024 * 1024)
r = surface.image_copy(open_image(SRC), open_image(junk), Progress(1), smart=True)
check(r["copied_bytes"] == plan["copy"], "smart clone copied only the planned bytes")
probs = verify_disk(junk, meta, os.path.join(W, "samples"))
check(not probs, f"smart clone onto random junk: every filesystem checks clean, files intact {probs[:3]}")
check(verify_disk(SRC, meta, os.path.join(W, "samples")) == [], "verifier sanity check on the source")
r = surface.image_copy(open_image(SRC), os.path.join(W, "smart.img"), Progress(1), smart=True)
check(not verify_disk(os.path.join(W, "smart.img"), meta, None) and r["sha256"],
      "smart image file valid (unused space sparse) and hashed")

print(f"\n{sum(OK)}/{len(OK)} checks passed")
sys.exit(0 if all(OK) else 1)
