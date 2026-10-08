"""Real-Windows integration test (run as Administrator; used by CI on windows-latest).

Creates and attaches a 256 MB virtual disk with diskpart, then drives the raw-disk code paths:
enumeration, volume map, partition parsing, partition search + restore, NTFS undelete,
surface scan, imaging, and a wipe that has to lock/dismount a mounted volume.
"""
import hashlib
import os
import string
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sectorsmith import ntfs, partitions, partscan, surface, wipe  # noqa: E402
from sectorsmith.device import VolumeLocker, list_disks, volume_letters_for, windows_volume_map  # noqa: E402
from sectorsmith.util import Progress, is_admin  # noqa: E402

OK = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    OK.append(bool(cond))


def diskpart(script):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(script)
    r = subprocess.run(["diskpart", "/s", f.name], capture_output=True, text=True)
    os.remove(f.name)
    print(r.stdout[-600:])
    return r


assert sys.platform == "win32" and is_admin(), "run on Windows as Administrator"
letter = next(c for c in reversed(string.ascii_uppercase) if c not in "AB" and not os.path.exists(f"{c}:\\"))
vhd = os.path.join(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()), "ss_test.vhd")

# --- enumeration on the real machine -----------------------------------------------------
disks = list_disks()
for d in disks:
    print("  ", d.describe(), d.bus, d.sector_size, d.serial)
check(len(disks) >= 1, f"list_disks found {len(disks)} disk(s)")
check(sum(d.is_system for d in disks) == 1, "exactly one disk flagged as system")
sysd = next(d for d in disks if d.is_system)
pt = partitions.read_partition_table(sysd)
print(partitions.describe(pt, sysd.sector_size))
check(any(p.fs == "NTFS" for p in pt.partitions), "system disk has an NTFS partition")
check(any("C:\\" in v["letters"] for v in windows_volume_map()), "volume map contains C:")
check(any("C:\\" in m for p in pt.partitions for m in volume_letters_for(sysd).get(p.start_lba * sysd.sector_size, [])),
      "C: mapped to a system-disk partition")
try:
    sysd.open(writable=True)
    check(False, "system disk write must be refused")
except Exception as e:  # noqa: BLE001
    check("system disk" in str(e).lower(), "system disk write refused")
sysd.close()

# --- attach a virtual disk ----------------------------------------------------------------
if os.path.exists(vhd):
    diskpart(f'select vdisk file="{vhd}"\ndetach vdisk noerr\n')
    os.remove(vhd)
diskpart(f'create vdisk file="{vhd}" maximum=256 type=fixed\nselect vdisk file="{vhd}"\nattach vdisk\n'
         f'convert gpt\ncreate partition primary\nformat fs=ntfs quick label=SSTEST\nassign letter={letter}\n')
time.sleep(3)
root = f"{letter}:\\"
check(os.path.isdir(root), f"test volume mounted at {root}")
payload = os.urandom(300_000)
with open(root + "hello.bin", "wb") as f:
    f.write(payload)
with open(root + "note.txt", "w") as f:
    f.write("SectorSmith was here\n")

try:
    dev = next(d for d in list_disks() if any(root in m for ms in volume_letters_for(d).values() for m in ms))
    print("test disk:", dev.describe(), dev.bus)
    check(not dev.is_system and abs(dev.size - 256 * 1024 * 1024) < 4 * 1024 * 1024, "virtual disk enumerated")
    pt = partitions.read_partition_table(dev)
    print(partitions.describe(pt, dev.sector_size))
    data_part = next(p for p in pt.partitions if p.fs == "NTFS")
    check(pt.scheme == "GPT" and not pt.notes, "GPT parsed with valid CRCs")

    # NTFS undelete on a live volume
    os.remove(root + "hello.bin")
    lk = VolumeLocker(dev.disk_number)
    lk.lock()   # dismount flushes the MFT to disk
    lk.release()
    time.sleep(1)
    vol = ntfs.NTFSVolume(dev, data_part.start_lba * dev.sector_size)
    deleted = {e.name: e for e in vol.scan(Progress(1), deleted_only=True)}
    check("hello.bin" in deleted, "deleted file found in MFT")
    out = os.path.join(tempfile.gettempdir(), "ss_rec")
    r = ntfs.recover_entries(vol, [deleted["hello.bin"]], out, Progress(1), keep_paths=False)
    got = open(os.path.join(out, "hello.bin"), "rb").read()
    check(got == payload, "deleted file recovered byte-exact on real NTFS")

    # Volume Shadow Copy: a snapshot must keep showing the volume as it was
    from sectorsmith.vss import needs_snapshot, snapshot_disk
    check(needs_snapshot(dev), "mounted test volume needs a snapshot")
    dev.open()
    snap = snapshot_disk(dev)
    check(snap.volumes, f"snapshot taken ({snap.volumes} {snap.notes})")
    p_off, p_len = data_part.start_lba * dev.sector_size, data_part.sectors * dev.sector_size

    def part_hash(direct=False):
        h = hashlib.sha256()
        pos = 0
        while pos < p_len:
            n = min(8 * 1024 * 1024, p_len - pos)
            h.update(dev._read_direct(p_off + pos, n) if direct else dev.read(p_off + pos, n))
            pos += n
        return h.hexdigest()
    before = part_hash()
    with open(root + "after_snapshot.bin", "wb") as f:
        f.write(os.urandom(2_000_000))
    lk = VolumeLocker(dev.disk_number)
    lk.lock()
    lk.release()
    time.sleep(1)
    check(part_hash() == before, "snapshot reads unaffected by later writes")
    check(part_hash(direct=True) != before, "live volume did change (so the snapshot really was used)")
    snap.release()
    dev.close()
    os.remove(root + "after_snapshot.bin")

    # surface scan + imaging
    r = surface.surface_scan(dev, Progress(1))
    check(r["bad_sectors"] == 0, f"surface scan clean ({r['blocks']} blocks)")
    img = os.path.join(tempfile.gettempdir(), "ss_copy.img")
    r = surface.image_copy(dev, img, Progress(1))
    h = hashlib.sha256()
    dev.open()
    pos = 0
    while pos < dev.usable_size:
        d = dev.read(pos, 4 * 1024 * 1024)
        h.update(d)
        pos += len(d)
    dev.close()
    check(r["sha256"] == h.hexdigest(), "raw image hash matches disk")

    # destroy the partition table, find it again, restore it (needs locking the mounted volume)
    dev.open(writable=True)
    dev.write(0, bytes(34 * dev.sector_size))
    dev.write(dev.usable_size - 33 * dev.sector_size, bytes(33 * dev.sector_size))
    dev.close()
    check(partitions.read_partition_table(dev).scheme == "None", "partition table destroyed")
    found = partscan.scan_partitions(dev, Progress(1), mode="quick")
    print("found:", [(f.start_lba, f.fs, f.sectors) for f in found])
    fnt = [f for f in found if f.fs == "NTFS"]
    check(fnt and fnt[0].start_lba == data_part.start_lba, "lost NTFS partition found at the right place")
    dev.open(writable=True)
    partitions.add_partition_entry(dev, fnt[0].start_lba, fnt[0].sectors, "NTFS", prefer_gpt=True)
    dev.close()
    pt2 = partitions.read_partition_table(dev)
    check(pt2.scheme == "GPT" and any(p.start_lba == data_part.start_lba and p.fs == "NTFS" for p in pt2.partitions)
          and not pt2.notes, "partition restored with valid GPT")
    diskpart(f'select vdisk file="{vhd}"\nselect partition 1\nassign letter={letter} noerr\n')
    time.sleep(3)
    check(os.path.exists(root + "note.txt") and open(root + "note.txt").read().startswith("SectorSmith"),
          "Windows mounts the restored partition and files are intact")

    # wipe the partition while Windows has it mounted (lock + dismount path)
    pt3 = partitions.read_partition_table(dev)
    p = next(p for p in pt3.partitions if p.start_lba == data_part.start_lba)
    r = wipe.wipe_disk(dev, wipe.METHODS["NIST 800-88 Clear (1 pass + verify)"], Progress(1),
                       start_lba=p.start_lba, sectors=p.sectors)
    check(r["write_errors"] == 0 and r["verify_mismatched_blocks"] == 0, "partition wiped + verified")
    head = dev.read(p.start_lba * dev.sector_size, 1024 * 1024)
    check(head == bytes(len(head)), "partition content is zeros")
    check(partitions.read_partition_table(dev).scheme == "GPT", "partition table survived the partition wipe")
    dev.close()
finally:
    diskpart(f'select vdisk file="{vhd}"\ndetach vdisk noerr\n')
    try:
        os.remove(vhd)
    except OSError:
        pass

print(f"\n{sum(OK)}/{len(OK)} Windows checks passed")
sys.exit(0 if all(OK) else 1)
