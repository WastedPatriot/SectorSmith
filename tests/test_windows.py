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

from sectorsmith import ntfs, partitions, partscan, surface, usedmap, wipe  # noqa: E402
from sectorsmith.device import (VolumeLocker, list_disks, open_image, volume_letters_for,  # noqa: E402
                                windows_volume_map)
from sectorsmith.util import Progress, is_admin  # noqa: E402

OK = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg, flush=True)
    OK.append(bool(cond))


def first_diff(a: bytes, b: bytes):
    """Offset of the first differing byte (None if equal)."""
    for i in range(0, min(len(a), len(b)), 4096):
        if a[i:i + 4096] != b[i:i + 4096]:
            return i + next(k for k in range(4096) if a[i + k:i + k + 1] != b[i + k:i + k + 1])
    return None if len(a) == len(b) else min(len(a), len(b))


def range_hashes(read, ranges, piece=1024 * 1024):
    """[(offset, length, sha256)] per piece of ``ranges``, so two passes can be compared piecewise."""
    return [(o, n, hashlib.sha256(read(o, n)).hexdigest()) for o, n in usedmap.pieces(ranges, piece)]


def subtract(ranges, holes):
    out = []
    for s, ln in ranges:
        segs = [(s, s + ln)]
        for hs, hl in holes:
            segs = [x for a, b in segs for x in ((a, min(b, hs)), (max(a, hs + hl), b)) if x[1] > x[0]]
        out += [(a, b - a) for a, b in segs]
    return out


def owners(vol, off, n):
    """Names of files (in ``vol``'s MFT) whose clusters overlap [off, off+n)."""
    res = []
    for e in vol.entries.values():
        for lcn, cl in e.runs:
            if lcn is not None and vol.base + lcn * vol.cluster < off + n and off < vol.base + (lcn + cl) * vol.cluster:
                res.append(f"{e.path}\\{e.name}#{e.recno}" + (" (deleted)" if e.deleted else ""))
                break
    return res or ["<no file: free space or metadata outside $DATA runs>"]


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
    # without this the data sits in the cache, and deleting the file discards it before it ever
    # reaches the disk: there would be nothing to recover
    f.flush()
    os.fsync(f.fileno())
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
    out = tempfile.mkdtemp(prefix="ss_rec_")  # fresh, so a leftover hello.bin can't be read instead
    e = deleted["hello.bin"]
    r = ntfs.recover_entries(vol, [e], out, Progress(1), keep_paths=False)
    rec_path = os.path.join(out, "hello.bin")
    got = open(rec_path, "rb").read() if os.path.exists(rec_path) else b""
    ok = got == payload
    if not ok:
        diff = first_diff(got, payload)
        print(f"  record {e.recno} seq {e.seq}: size {e.size} init_size {e.init_size} flags 0x{e.flags:04X} "
              f"resident {e.resident is not None} runs {e.runs[:8]} cluster {vol.cluster}")
        print(f"  recover result {r}; got {len(got)} bytes, payload {len(payload)}, first diff at {diff}, "
              f"recovered data all zeros: {not got.strip(bytes(1))}")
        if diff is not None and diff < len(got):
            print(f"  got  {got[diff:diff + 16].hex()}\n  want {payload[diff:diff + 16].hex()}")
    check(ok, "deleted file recovered byte-exact on real NTFS")

    # Volume Shadow Copy: a snapshot must keep showing the volume as it was
    from sectorsmith.vss import needs_snapshot, snapshot_disk
    check(needs_snapshot(dev), "mounted test volume needs a snapshot")
    dev.open()
    snap = snapshot_disk(dev)
    check(snap.volumes, f"snapshot taken ({snap.volumes} {snap.notes})")
    p_off = data_part.start_lba * dev.sector_size
    # VSS only preserves clusters that were allocated when the snapshot was taken. Free clusters,
    # and its own store files in System Volume Information, are read through to the live volume,
    # so a raw hash of the whole partition legitimately changes. Compare what VSS does freeze:
    # the allocated clusters of the snapshot's $Bitmap minus the VSS store.
    snap_vol = ntfs.NTFSVolume(dev, p_off)  # reads go through the snapshot overlay
    snap_vol.scan(Progress(1))
    vss_store = [(p_off + lcn * snap_vol.cluster, cl * snap_vol.cluster) for e in snap_vol.entries.values()
                 if not e.deleted and e.path.lower().startswith("\\system volume information")
                 for lcn, cl in e.runs if lcn is not None]
    used = usedmap.partition_used(dev, data_part.start_lba, data_part.sectors, "NTFS")
    frozen = subtract(used or [], vss_store)
    before = range_hashes(dev.read, frozen)
    with open(root + "after_snapshot.bin", "wb") as f:
        f.write(os.urandom(2_000_000))
    lk = VolumeLocker(dev.disk_number)
    lk.lock()
    lk.release()
    time.sleep(1)
    after = range_hashes(dev.read, frozen)
    live = range_hashes(dev._read_direct, frozen)
    changed = [(o, n) for (o, n, h), (_, _, h2) in zip(before, after) if h != h2]
    ok = bool(frozen) and not changed
    if not ok:
        print(f"  snapshot overlay {[(o, ln, r.path) for o, ln, r in dev._overlay]}")
        print(f"  frozen {sum(n for _, n in frozen)} bytes in {len(frozen)} range(s); "
              f"VSS store excluded {sum(n for _, n in vss_store)} bytes; {len(changed)} MiB piece(s) changed")
        for o, n in changed[:8]:
            print(f"    +{o - p_off:#x} ({n} bytes) in {owners(snap_vol, o, n)}")
    check(ok, "snapshot reads unaffected by later writes")
    check(live != before, "live volume did change (so the snapshot really was used)")
    snap.release()
    dev.close()
    os.remove(root + "after_snapshot.bin")

    # surface scan + imaging
    r = surface.surface_scan(dev, Progress(1))
    check(r["bad_sectors"] == 0, f"surface scan clean ({r['blocks']} blocks)")
    img = os.path.join(tempfile.gettempdir(), "ss_copy.img")
    # Z: is still mounted, so Windows keeps writing metadata (the deleted file, the removed shadow
    # copy, $LogFile). Keep it locked + dismounted so the image and the re-read see the same disk.
    with VolumeLocker(dev.disk_number):
        r = surface.image_copy(dev, img, Progress(1))
        dev.open()
        whole = [(0, dev.usable_size)]
        h = hashlib.sha256()
        disk_pieces = []
        for o, n in usedmap.pieces(whole):
            d = dev.read(o, n)
            h.update(d)
            disk_pieces.append((o, n, hashlib.sha256(d).hexdigest()))
        dev.close()
    h_img = hashlib.sha256()
    with open(img, "rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h_img.update(chunk)
    ok = r["sha256"] == h.hexdigest() == h_img.hexdigest()
    if not ok:
        print(f"  disk {dev.usable_size} bytes, image file {os.path.getsize(img)} bytes, reported {r['bytes']}, "
              f"bad sectors {r['bad_sectors']}")
        print(f"  sha256 reported {r['sha256']}\n         disk     {h.hexdigest()}\n"
              f"         image    {h_img.hexdigest()}")
        img_dev = open_image(img)
        img_pieces = range_hashes(img_dev.read, whole, 4 * 1024 * 1024)
        img_dev.close()
        diffs = [o for (o, _, a), (_, _, b) in zip(disk_pieces, img_pieces) if a != b]
        print(f"  {len(diffs)} differing 4 MiB piece(s) between image and disk, first at "
              f"{[hex(o) for o in diffs[:8]]} (NTFS partition starts at {p_off:#x})")
    check(ok, "raw image hash matches disk")

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
