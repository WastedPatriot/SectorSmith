"""Volume Shadow Copy snapshots, so a disk that Windows is running from can be cloned consistently.

Every mounted NTFS/ReFS volume on the disk gets a point-in-time shadow copy; reads that fall inside
those volumes are served from the snapshot (via Device._overlay) while the rest of the disk
(partition table, EFI/MSR partitions, gaps) is read directly.
"""
from __future__ import annotations

import subprocess

from .device import Device
from .util import get_logger, is_windows

log = get_logger()


def _ps(script: str, timeout=600) -> str:
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
    return r.stdout.strip()


def create_shadow(volume: str) -> tuple[str, str]:
    """Snapshot one volume ('C:\\' or '\\\\?\\Volume{guid}\\'). Returns (shadow_id, device_object)."""
    vol = volume.replace("'", "''")
    out = _ps(
        "$r = Invoke-CimMethod -ClassName Win32_ShadowCopy -MethodName Create "
        f"-Arguments @{{Volume='{vol}'; Context='ClientAccessible'}};"
        "if ($r.ReturnValue -ne 0) { throw \"VSS error $($r.ReturnValue)\" };"
        "$s = Get-CimInstance Win32_ShadowCopy | Where-Object { $_.ID -eq $r.ShadowID };"
        "Write-Output ($s.ID + '|' + $s.DeviceObject)")
    sid, dev = out.splitlines()[-1].split("|", 1)
    return sid.strip(), dev.strip()


def delete_shadow(shadow_id: str):
    sid = shadow_id.replace("'", "''")
    try:
        _ps(f"Get-CimInstance Win32_ShadowCopy | Where-Object {{ $_.ID -eq '{sid}' }} | Remove-CimInstance",
            timeout=120)
    except Exception as e:  # noqa: BLE001
        log.warning("Could not delete shadow copy %s: %s", shadow_id, e)


class Snapshot:
    def __init__(self, dev: Device):
        self.dev = dev
        self.shadows: list[str] = []
        self.readers: list[Device] = []
        self.volumes: list[str] = []
        self.notes: list[str] = []

    def release(self):
        self.dev._overlay = []
        for r in self.readers:
            r.close()
        for sid in self.shadows:
            delete_shadow(sid)
        self.shadows.clear()
        self.readers.clear()


def snapshot_disk(dev: Device, progress=None) -> Snapshot:
    """Snapshot every mounted volume on ``dev`` and route reads of those volumes to the snapshots.

    On non-Windows (or for image files) this is a no-op Snapshot.
    """
    snap = Snapshot(dev)
    if not is_windows() or dev.is_image or dev.disk_number is None:
        return snap
    from .device import windows_volume_map
    overlay = []
    for v in windows_volume_map():
        if v["disk"] != dev.disk_number:
            continue
        name = (v["letters"] or [v["guid"]])[0]
        if progress:
            progress(f"Taking a snapshot of {name}…")
        try:
            sid, devobj = create_shadow(v["guid"])
        except Exception as e:  # noqa: BLE001  (FAT/EFI volumes can't be snapshotted — read directly)
            snap.notes.append(f"{name}: no snapshot ({str(e)[:80]}) — read live")
            continue
        snap.shadows.append(sid)
        reader = Device(path=devobj, name=f"snapshot {name}", size=v["length"], sector_size=dev.sector_size)
        reader.open(writable=False)
        snap.readers.append(reader)
        overlay.append((v["offset"], v["length"], reader))
        snap.volumes.append(name)
        log.info("Snapshot of %s on %s: %s", name, dev.name, devobj)
    dev._overlay = overlay
    return snap


def needs_snapshot(dev: Device) -> bool:
    """True when the disk has mounted volumes that could change while we read it."""
    if not is_windows() or dev.is_image or dev.disk_number is None:
        return False
    from .device import windows_volume_map
    return any(v["disk"] == dev.disk_number for v in windows_volume_map())

