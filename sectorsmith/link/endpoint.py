"""Endpoints: the same file/disk API on this PC (LocalEndpoint) or on a linked machine (RemoteEndpoint).

The agent on a remote machine simply runs a LocalEndpoint and dispatches RPC calls to it.
"""
from __future__ import annotations

import fnmatch
import getpass
import hashlib
import os
import platform
import shutil
import socket
import sys
import threading
import time
import zlib

from .. import offline
from ..device import Device, list_disks, open_image, volume_letters_for
from ..partitions import read_partition_table
from ..util import APP_VERSION, Cancelled, cancel_scope, check_cancel, current_check, get_logger, is_admin
from .proto import Conn, LinkError, RemoteError, local_ips

log = get_logger()

SKIP_DIRS = {"cache", "code cache", "gpucache", "cachestorage", "crashpad", "temp", "tmp", "shadercache",
             "grshadercache", "dawncache", "graphitedawncache", "component_crx_cache", "inetcache",
             "temporary internet files", "$recycle.bin", "system volume information", "crashdumps"}
SKIP_FILES = ["*.tmp", "~$*", "thumbs.db", "ntuser.dat*", "usrclass.dat*", "*.lock", "lockfile", "parent.lock",
              "desktop.ini"]  # desktop.ini: every PC has its own (hidden+system) copy for folder names/icons
# app-data files that are either held open while the app runs or only work on the PC that made them
# (browser cookies are encrypted with a key tied to that PC/user, so they can't be used on the new one)
SKIP_APPDATA_FILES = {"lock", "cookies", "cookies-journal", "singletonlock", "singletoncookie", "singletonsocket"}

# ops a remote controller may call on an agent
OPS = {"info", "list_disks", "list_profiles", "scan_tree", "dir_size", "read_file", "read_files", "write_file",
       "write_files", "mkdirs", "free_space", "path_exists", "disk_open", "disk_read", "disk_write", "disk_close",
       "ping", "list_dir", "disk_fix_gpt", "disk_plan", "make_folder",
       "installed_software", "put_file", "run_command", "run_script", "file_version", "remove_path", "deploy_dir",
       "list_volumes", "offline_profiles", "apps_on_disk"}
# ops that can take a while: inside a cancel_scope a controller runs these as background jobs on the agent,
# polls them and can cancel them (see Session)
LONG_OPS = {"scan_tree", "dir_size", "run_command", "run_script", "disk_open", "disk_plan", "installed_software",
            "offline_profiles", "list_disks", "apps_on_disk"}
DEV_FIELDS = ("path", "name", "size", "sector_size", "model", "bus", "serial", "is_image", "is_system", "removable",
              "disk_number", "data_size")


def _long(p: str) -> str:
    if sys.platform == "win32":
        p = os.path.abspath(p)
        if not p.startswith("\\\\?\\"):
            return "\\\\?\\UNC\\" + p[2:] if p.startswith("\\\\") else "\\\\?\\" + p
    return p


def _safe_rel(rel: str) -> str:
    parts = [x for x in rel.replace("\\", "/").split("/") if x not in ("", ".")]
    if any(x == ".." for x in parts):
        raise ValueError(f"Unsafe path: {rel}")
    return os.path.join(*parts) if parts else ""


def _skip_file(name: str, in_appdata: bool = False) -> bool:
    n = name.lower()
    if in_appdata and n in SKIP_APPDATA_FILES:
        return True
    return any(fnmatch.fnmatch(n, pat) for pat in SKIP_FILES)


def _read_only(root: str):
    if offline.is_raw(root):
        raise LinkError("That disk is read directly (no drive letter), so nothing can be written to it.")


def _kill_tree(p):
    """Stop a command and everything it started (an installer often runs a child that does the real work)."""
    import subprocess
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True,
                           timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            import signal
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # tree already gone; p.kill() below covers the parent
    try:
        p.kill()
    except OSError:
        pass  # already exited


def _clear_attrs(p: str):
    """Windows refuses to overwrite hidden/system/read-only files ("Permission denied") — clear those first."""
    if sys.platform == "win32" and os.path.exists(p):
        import ctypes
        ctypes.windll.kernel32.SetFileAttributesW(p, 0x80)  # FILE_ATTRIBUTE_NORMAL


class LocalEndpoint:
    is_local = True

    def __init__(self, extra_images: list[str] | None = None, label: str | None = None):
        self._devs: dict[str, Device] = {}
        self._open: dict[str, Device] = {}
        self._snaps: dict = {}
        self._raw: dict = {}
        self.extra_images = list(extra_images or [])
        self.label = label or "This PC"
        self.lock = threading.Lock()

    # --- identity -------------------------------------------------------
    def info(self):
        return {"hostname": socket.gethostname(), "os": platform.platform(terse=True), "user": getpass.getuser(),
                "ips": local_ips(), "admin": is_admin(), "version": APP_VERSION,
                "system_drive": os.environ.get("SystemDrive", "/")}

    def ping(self):
        return "pong"

    # --- disks ----------------------------------------------------------
    def list_disks(self):
        devs = []
        try:
            devs = list_disks()
        except Exception as e:  # noqa: BLE001
            log.warning("list_disks: %s", e)
        for p in self.extra_images:
            try:
                devs.append(open_image(p))
            except OSError:
                pass
        out = []
        for d in devs:
            self._devs[d.path] = d
            parts, scheme, err = [], "?", None
            try:
                pt = read_partition_table(d)
                scheme = pt.scheme
                letters = volume_letters_for(d)
                for p in pt.partitions:
                    parts.append({"index": p.index, "start": p.start_lba, "sectors": p.sectors, "fs": p.fs,
                                  "label": p.label or p.name, "type": p.type_name,
                                  "mount": letters.get(p.start_lba * d.sector_size, [])})
            except OSError as e:
                err = e.strerror or str(e)
            finally:
                d.close()
            out.append({"path": d.path, "name": d.name, "model": d.model, "size": d.size,
                        "usable": d.usable_size, "sector_size": d.sector_size, "bus": d.bus, "serial": d.serial,
                        "is_system": d.is_system, "is_image": d.is_image, "scheme": scheme, "partitions": parts,
                        "removable": d.removable or d.bus in ("USB", "SD", "MMC"),
                        "error": err})
        return out

    def _dev(self, path) -> Device:
        if path not in self._devs:
            self.list_disks()
        if path not in self._devs:
            raise LinkError(f"Unknown disk {path}")
        return self._devs[path]

    def disk_open(self, path, writable=False, snapshot=False):
        d = self._dev(path)
        dev = Device(**{f: getattr(d, f) for f in DEV_FIELDS})
        dev.open(writable=writable)
        self._open[path] = dev
        info = {"usable": dev.usable_size, "sector_size": dev.sector_size, "snapshot": [], "notes": []}
        try:
            if snapshot and not writable:
                from ..vss import needs_snapshot, snapshot_disk
                if needs_snapshot(dev):
                    snap = snapshot_disk(dev)  # releases its own shadows if cancelled
                    self._snaps[path] = snap
                    info["snapshot"], info["notes"] = snap.volumes, snap.notes
        except BaseException:
            self._open.pop(path, None)
            dev.close()
            raise
        return info

    def disk_plan(self, path, start_lba=0, sectors=None, smart=True):
        """Which byte ranges to copy (used space of known filesystems; everything else in full)."""
        dev = self._open.get(path) or self._dev(path)
        from ..usedmap import copy_plan
        p = copy_plan(dev, start_lba, sectors, smart=smart)
        return {"ranges": [list(r) for r in p["ranges"]], "copy": p["copy"], "total": p["total"],
                "by_partition": p["by_partition"]}

    def disk_fix_gpt(self, path):
        dev = self._open.get(path)
        if dev is None or not dev.writable:
            raise LinkError("Disk is not open for writing")
        from ..partitions import fix_gpt_after_grow
        return fix_gpt_after_grow(dev)

    def disk_read(self, path, offset, size, zskip=True, compress=True, tolerant=True):
        dev = self._open.get(path) or self._dev(path)
        bad = 0
        try:
            data = dev.read(offset, size)
            if len(data) < size:
                data = data.ljust(size, b"\0")
        except OSError:
            if not tolerant:
                raise
            ss = dev.sector_size
            buf = bytearray()
            for s in range(offset, offset + size, ss):
                try:
                    buf += dev.read(s, ss).ljust(ss, b"\0")
                except OSError:
                    buf += bytes(ss)
                    bad += 1
            data = bytes(buf)
        sha = hashlib.sha256(data).hexdigest()
        if zskip and data.count(0) == len(data):
            return {"zero": True, "sha": sha, "bad": bad}, None
        if compress:
            z = zlib.compress(data, 1)
            if len(z) < len(data) * .9:
                return {"z": True, "sha": sha, "bad": bad}, z
        return {"sha": sha, "bad": bad}, data

    def disk_write(self, path, offset, size, zero=False, z=False, blob=None):
        dev = self._open.get(path)
        if dev is None or not dev.writable:
            raise LinkError("Disk is not open for writing")
        data = bytes(size) if zero else (zlib.decompress(blob) if z else blob)
        if len(data) != size:
            raise LinkError("Size mismatch")
        dev.write(offset, data)
        return {"sha": hashlib.sha256(data).hexdigest()}

    def disk_close(self, path):
        snap = self._snaps.pop(path, None)
        if snap is not None:
            snap.release()
        dev = self._open.pop(path, None)
        if dev is not None:
            dev.flush() if dev.writable else None
            dev.close()
        return True

    # --- users / files ----------------------------------------------------
    def list_profiles(self):
        res = []
        if sys.platform == "win32":
            import winreg
            key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                i = 0
                while True:
                    try:
                        sid = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    i += 1
                    if not sid.startswith(("S-1-5-21-", "S-1-12-1-")):  # local/domain and Entra ID accounts
                        continue
                    try:
                        with winreg.OpenKey(k, sid) as sk:
                            path = os.path.expandvars(winreg.QueryValueEx(sk, "ProfileImagePath")[0])
                    except OSError:
                        continue
                    if os.path.isdir(path):
                        res.append({"name": os.path.basename(path), "path": path, "sid": sid})
        else:
            root = os.environ.get("SECTORSMITH_PROFILES_ROOT", "/home")
            if os.path.isdir(root):
                for n in sorted(os.listdir(root)):
                    p = os.path.join(root, n)
                    if os.path.isdir(p):
                        res.append({"name": n, "path": p, "sid": ""})
        return res

    def offline_profiles(self, raw=False):
        """User folders on other drives: old Windows disks attached by USB, or this PC's own disk when it was
        booted from the SectorSmith USB. raw=True also reads NTFS partitions that have no drive letter (slow)."""
        mine = {p["path"] for p in self.list_profiles()}
        out = offline.offline_profiles(exclude=mine)
        if raw:
            out += offline.raw_profiles(self.list_disks(), self._rawfs)
        return out

    def list_volumes(self):
        return offline.volumes()

    def apps_on_disk(self, profile):
        """Apps of a Windows that isn't running (old disk, or raw NTFS), found by their Program Files folders."""
        from .apps import apps_on_disk, volume_of
        if offline.is_raw(profile):
            def join(*p):
                return "/".join(x.rstrip("/") for x in p)
        else:
            join = os.path.join
        return apps_on_disk(self.list_dir, self.path_exists, join, volume_of(profile), profile)

    def _rawfs(self, disk, start_lba):
        key = (disk, int(start_lba))
        fs = self._raw.get(key)
        if fs is None:
            d = self._dev(disk)
            dev = Device(**{f: getattr(d, f) for f in DEV_FIELDS})
            dev.open(writable=False)
            try:
                fs = offline.RawNTFS(dev, int(start_lba))
            except BaseException:
                dev.close()
                raise
            self._raw[key] = fs
        return fs

    def _raw_entry(self, root, rel=""):
        disk, lba, inner = offline.parse_raw(root)
        fs = self._rawfs(disk, lba)
        r = rel.replace("\\", "/").strip("/")
        if any(x == ".." for x in r.split("/")):
            raise ValueError(f"Unsafe path: {rel}")
        return fs, fs.find("/".join(x for x in (inner, r) if x)), r

    def _raw_scan(self, root, rel):
        fs, base, prefix = self._raw_entry(root, rel)
        out = []
        if base is None or not base.is_dir:
            return {"files": out, "errors": 0}
        stack, n = [(base, prefix)], 0
        while stack:
            e, rp = stack.pop()
            in_appdata = rp.lower().split("/")[:1] == ["appdata"]
            for c in fs.list(e):
                n += 1
                if n % 512 == 0:
                    check_cancel()
                if c.attrs & 0x400:  # junctions and cloud-only placeholders hold no data of their own
                    continue
                crel = f"{rp}/{c.name}" if rp else c.name
                if c.is_dir:
                    if c.name.lower() not in SKIP_DIRS:
                        stack.append((c, crel))
                elif not _skip_file(c.name, in_appdata):
                    out.append([crel, c.size, fs.mtime(c)])
        return {"files": out, "errors": 0}

    def list_dir(self, path):
        """Sub-folder names of path."""
        if offline.is_raw(path):
            fs, e, _ = self._raw_entry(path)
            return sorted(c.name for c in fs.list(e) if c.is_dir and not c.attrs & 0x400) if e is not None else []
        try:
            with os.scandir(_long(path)) as it:
                return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def path_exists(self, path):
        if offline.is_raw(path):
            return self._raw_entry(path)[1] is not None
        return os.path.exists(_long(path))

    def free_space(self, path):
        if offline.is_raw(path):
            return None
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def scan_tree(self, root, rel=""):
        """All files under root/rel: [[relpath, size, mtime], ...] (relative to root). Skips caches/temp."""
        if offline.is_raw(root):
            return self._raw_scan(root, rel)
        base = os.path.join(root, _safe_rel(rel)) if rel else root
        out = []
        if not os.path.isdir(_long(base)):
            return {"files": [], "errors": 0}
        errors = 0
        stack = [base]
        seen = 0
        while stack:
            d = stack.pop()
            in_appdata = "appdata" in os.path.relpath(d, root).lower().replace("\\", "/").split("/")[:1]
            try:
                with os.scandir(_long(d)) as it:
                    for e in it:
                        seen += 1
                        if seen % 256 == 0:
                            check_cancel()
                        try:
                            if e.is_symlink():
                                continue
                            if e.is_dir(follow_symlinks=False):
                                if e.name.lower() in SKIP_DIRS:
                                    continue
                                # skip junctions / reparse points (e.g. legacy "My Music" links)
                                if sys.platform == "win32" and e.stat(follow_symlinks=False).st_file_attributes & 0x400:
                                    continue
                                stack.append(os.path.join(d, e.name))
                            elif not _skip_file(e.name, in_appdata):
                                st = e.stat(follow_symlinks=False)
                                out.append([os.path.relpath(os.path.join(d, e.name), root).replace("\\", "/"),
                                            st.st_size, int(st.st_mtime)])
                        except OSError:
                            errors += 1
            except OSError:
                errors += 1
        return {"files": out, "errors": errors}

    def dir_size(self, root, rel=""):
        t = self.scan_tree(root, rel)
        return {"bytes": sum(f[1] for f in t["files"]), "files": len(t["files"])}

    def read_file(self, root, rel, offset, size):
        if offline.is_raw(root):
            fs, e, _ = self._raw_entry(root, rel)
            if e is None:
                raise FileNotFoundError(rel)
            data = fs.read(e, offset, size)
            return {"n": len(data)}, data
        with open(_long(os.path.join(root, _safe_rel(rel))), "rb") as f:
            f.seek(offset)
            data = f.read(size)
        return {"n": len(data)}, data

    def read_files(self, root, rels):
        sizes, buf, errs = [], bytearray(), {}
        raw = offline.is_raw(root)
        for r in rels:
            check_cancel()
            try:
                if raw:
                    fs, e, _ = self._raw_entry(root, r)
                    if e is None:
                        raise FileNotFoundError(2, "not found")
                    d = fs.read(e, 0, e.size)
                else:
                    with open(_long(os.path.join(root, _safe_rel(r))), "rb") as f:
                        d = f.read()
                sizes.append(len(d))
                buf += d
            except OSError as e:
                sizes.append(-1)
                errs[r] = e.strerror or str(e)
        return {"sizes": sizes, "errors": errs}, bytes(buf)

    def make_folder(self, path):
        """Create a new destination folder (e.g. a profile folder for a user who has no account here yet)."""
        _read_only(path)
        p = _long(path)
        existed = os.path.isdir(p)
        os.makedirs(p, exist_ok=True)
        return {"path": path, "existed": existed, "free": self.free_space(path)}

    def mkdirs(self, root, rels):
        _read_only(root)
        for r in rels:
            os.makedirs(_long(os.path.join(root, _safe_rel(r))), exist_ok=True)
        return True

    def write_file(self, root, rel, offset, total, mtime=None, blob=b""):
        _read_only(root)
        p = _long(os.path.join(root, _safe_rel(rel)))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        if not offset:
            _clear_attrs(p)
        with open(p, "r+b" if offset and os.path.exists(p) else "wb") as f:
            f.seek(offset)
            f.write(blob)
            if offset + len(blob) >= total:
                f.truncate(total)
        if mtime is not None and offset + len(blob) >= total:
            os.utime(p, (mtime, mtime))
        return True

    def write_files(self, root, items, blob=b""):
        _read_only(root)
        pos = 0
        errs = {}
        for it in items:
            n = it["size"]
            data = blob[pos: pos + n]
            pos += n
            try:
                self.write_file(root, it["rel"], 0, n, it.get("mtime"), data)
            except OSError as e:
                errs[it["rel"]] = e.strerror or str(e)
        return {"errors": errs}

    # --- software deployment (SectorSmith Deploy) ---------------------------------------
    def deploy_dir(self):
        if sys.platform == "win32":
            d = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "SectorSmith", "Deploy")
        else:
            d = os.path.join(os.path.expanduser("~"), ".local", "share", "SectorSmith", "deploy-cache")
        os.makedirs(d, exist_ok=True)
        return d

    def installed_software(self):
        """Add/Remove Programs entries (all hives, 32+64-bit). On Linux: dpkg + optional test fixture."""
        out = []
        fake = os.environ.get("SECTORSMITH_FAKE_SOFTWARE")
        if fake and os.path.exists(fake):
            import json
            return json.load(open(fake))
        if sys.platform == "win32":
            import winreg
            roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "machine"),
                     (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                      "machine32")]
            try:
                with winreg.OpenKey(winreg.HKEY_USERS, "") as hku:
                    i = 0
                    while True:
                        try:
                            sid = winreg.EnumKey(hku, i)
                        except OSError:
                            break
                        i += 1
                        if sid.startswith("S-1-5-21") and not sid.endswith("_Classes"):
                            roots.append((winreg.HKEY_USERS,
                                          sid + r"\Software\Microsoft\Windows\CurrentVersion\Uninstall", sid))
            except OSError:
                pass
            for hive, path, scope in roots:
                try:
                    k = winreg.OpenKey(hive, path)
                except OSError:
                    continue
                with k:
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(k, i)
                        except OSError:
                            break
                        i += 1
                        try:
                            with winreg.OpenKey(k, name) as sk:
                                def val(v, sk=sk):
                                    try:
                                        return winreg.QueryValueEx(sk, v)[0]
                                    except OSError:
                                        return None
                                dn = val("DisplayName")
                                if not dn:
                                    continue
                                out.append({"key": name, "name": dn, "version": val("DisplayVersion") or "",
                                            "publisher": val("Publisher") or "", "scope": scope,
                                            "uninstall": val("UninstallString") or "",
                                            "quiet_uninstall": val("QuietUninstallString") or "",
                                            "system_component": bool(val("SystemComponent"))})
                        except OSError:
                            continue
        else:
            import subprocess
            try:
                r = subprocess.run(["dpkg-query", "-W", "-f=${Package}\t${Version}\n"], capture_output=True,
                                   text=True, timeout=60)
                for line in r.stdout.splitlines():
                    n, _, v = line.partition("\t")
                    out.append({"key": n, "name": n, "version": v, "publisher": "", "scope": "machine",
                                "uninstall": "", "quiet_uninstall": "", "system_component": False})
            except (OSError, subprocess.SubprocessError):
                pass
        return out

    def put_file(self, path, offset, total, blob=b""):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "r+b" if offset and os.path.exists(path) else "wb") as f:
            f.seek(offset)
            f.write(blob)
            if offset + len(blob) >= total:
                f.truncate(total)
        return True

    def run_command(self, cmd, timeout=3600, cwd=None, env=None):
        """Run a shell command. Cancel (or the timeout) stops it and everything it started."""
        import subprocess
        t0 = time.time()
        p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,  # nosec B602
                             env={**os.environ, **env} if env else None, start_new_session=sys.platform != "win32",
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        timed_out = False
        while True:
            try:
                out, err = p.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                stop = None
                if time.time() - t0 > timeout:
                    stop = "timeout"
                else:
                    try:
                        check_cancel()
                    except Cancelled:
                        stop = "cancel"
                if stop is None:
                    continue
                _kill_tree(p)
                try:
                    out, err = p.communicate(timeout=15)
                except subprocess.TimeoutExpired:
                    out, err = b"", b""
                if stop == "cancel":
                    log.info("Command stopped by Cancel: %s", cmd[:200])
                    raise Cancelled("Stopped the running command")
                timed_out = True
                break
        code = -1 if timed_out else p.returncode
        if timed_out:
            err = (err or b"") + b"\n[timed out]"
        dec = (lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or ""))
        return {"code": code, "out": dec(out)[-6000:], "err": dec(err)[-3000:], "seconds": round(time.time() - t0, 1)}

    def run_script(self, script, language="powershell", timeout=1800, params=None):
        """language: powershell, cmd (Windows batch) or shell/sh. Params become variables at the top."""
        import tempfile
        params = params or {}
        win = sys.platform == "win32"

        def unavailable(msg):
            return {"code": 127, "out": "", "err": msg, "seconds": 0}
        if language == "powershell":
            exe = "powershell.exe" if win else shutil.which("pwsh")
            if not exe:
                return unavailable("PowerShell (pwsh) is not installed on this machine.")
            head = "".join(f"${k} = '{str(v).replace(chr(39), chr(39) * 2)}'\n" for k, v in params.items())
            fd, p = tempfile.mkstemp(suffix=".ps1", dir=self.deploy_dir())
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                f.write(head + script)
            cmd = f'{exe} -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{p}"'
        elif language == "cmd":
            if not win:
                return unavailable("Batch (cmd) scripts only run on Windows.")
            # cmd has no safe quoting for every value, so params go in as environment variables
            head = "@echo off\r\n"
            fd, p = tempfile.mkstemp(suffix=".cmd", dir=self.deploy_dir())
            with os.fdopen(fd, "w", encoding="utf-8", newline="\r\n") as f:
                f.write(head + script)
            cmd = f'cmd.exe /d /c "{p}"'
        elif language in ("shell", "sh"):
            sh = shutil.which("sh")
            if not sh:
                return unavailable("Shell (sh) scripts need sh, which Windows doesn't have. Use PowerShell or cmd "
                                   "for Windows PCs.")
            import shlex
            head = "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in params.items())
            fd, p = tempfile.mkstemp(suffix=".sh", dir=self.deploy_dir())
            with os.fdopen(fd, "w", newline="\n") as f:
                f.write(head + script.replace("\r\n", "\n"))
            cmd = f'"{sh}" "{p}"'
        else:
            return unavailable(f"Unknown script language: {language}")
        try:
            env = {str(k): str(v) for k, v in params.items()} if language == "cmd" else None
            return self.run_command(cmd, timeout=timeout, env=env)
        finally:
            try:
                os.remove(p)
            except OSError:
                pass

    def file_version(self, path):
        if not os.path.exists(path):
            return None
        if path.lower().endswith((".exe", ".dll", ".sys")):
            from ..deploy.analyze import clean_version, exe_info
            e = exe_info(path)
            return clean_version(e.get("FileVersion") or e.get("ProductVersion")) or "0"
        try:
            with open(path, "r", errors="replace") as f:
                return (f.readline().strip() or "0")[:64]
        except OSError:
            return "0"

    def remove_path(self, path):
        import shutil as _sh
        if os.path.isdir(path):
            _sh.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
        return True

    def close(self):
        for p in list(self._open):
            self.disk_close(p)
        for fs in self._raw.values():
            fs.dev.close()
        self._raw.clear()


# ---------------------------------------------------------------------------
BLOB_ARG_OPS = {"write_file", "write_files", "disk_write", "put_file"}


def dispatch(ep: LocalEndpoint, req: dict, blob: bytes | None):
    """Run one RPC request against a local endpoint. Returns (response_dict, response_blob)."""
    op = req.get("op")
    if op not in OPS:
        return {"id": req.get("id"), "ok": False, "error": f"Unknown op {op}", "kind": "ValueError"}, None
    args = req.get("args") or {}
    if op in BLOB_ARG_OPS:
        args["blob"] = blob or b""
    try:
        res = getattr(ep, op)(**args)
        rblob = None
        if isinstance(res, tuple) and len(res) == 2 and (res[1] is None or isinstance(res[1], bytes)):
            res, rblob = res
        return {"id": req.get("id"), "ok": True, "result": res}, rblob
    except Exception as e:  # noqa: BLE001
        if not isinstance(e, Cancelled):
            log.warning("Link op %s failed: %s", op, e)
        return {"id": req.get("id"), "ok": False, "error": str(e), "kind": type(e).__name__}, None


class Session:
    """Agent side of one controller connection. Most ops run straight away; a long op sent with "async" runs in a
    background job that the controller polls ("job_poll") and can stop ("job_cancel"), so Cancel on the
    controller reaches a running installer, file scan or snapshot here."""

    def __init__(self, ep: LocalEndpoint):
        self.ep = ep
        self.jobs: dict[int, dict] = {}
        self._next = 0

    def handle(self, req: dict, blob: bytes | None):
        op, rid = req.get("op"), req.get("id")
        args = req.get("args") or {}
        if op in ("job_poll", "job_cancel"):
            j = self.jobs.get(args.get("job"))
            if j is None:
                return {"id": rid, "ok": False, "error": "No such job", "kind": "LinkError"}, None
            if op == "job_cancel":
                j["cancel"].set()
                return {"id": rid, "ok": True, "result": True}, None
            if not j["done"].wait(min(float(args.get("wait", 0.3)), 5.0)):
                return {"id": rid, "ok": True, "pending": True}, None
            del self.jobs[args["job"]]
            resp, rblob = j["resp"]
            return dict(resp, id=rid), rblob
        if req.get("async") and op in LONG_OPS:
            self._next += 1
            jid = self._next
            j = {"cancel": threading.Event(), "done": threading.Event(), "resp": None}
            self.jobs[jid] = j

            def stop_if_cancelled(ev=j["cancel"]):
                if ev.is_set():
                    raise Cancelled("Cancelled by the controller")

            def work():
                with cancel_scope(stop_if_cancelled):
                    j["resp"] = dispatch(self.ep, req, blob)
                j["done"].set()
            threading.Thread(target=work, daemon=True, name=f"link-job-{op}").start()
            return {"id": rid, "ok": True, "pending": True, "job": jid}, None
        return dispatch(self.ep, req, blob)

    def close(self):
        """Controller gone: stop whatever it left running here."""
        for j in self.jobs.values():
            j["cancel"].set()
        for j in list(self.jobs.values()):
            j["done"].wait(20)
        self.jobs.clear()


class RemoteEndpoint:
    """Calls a LocalEndpoint on a linked machine. Thread-safe (one request at a time)."""

    is_local = False

    def __init__(self, conn: Conn, info: dict, address: str):
        self.conn = conn
        self.info_cache = info
        self.address = address
        self.label = info.get("hostname", address)
        self.lock = threading.Lock()
        self._id = 0
        self.alive = True

    def _rpc(self, msg, blob=None):
        self._id += 1
        try:
            self.conn.send(dict(msg, id=self._id), blob)
            return self.conn.recv()
        except (OSError, LinkError) as e:
            self.alive = False
            raise LinkError(f"Lost connection to {self.label}: {e}") from e

    def _call(self, op, blob=None, **args):
        check = current_check()
        cancelled = False
        with self.lock:
            if not self.alive:
                raise LinkError(f"{self.label} is disconnected")
            if check is None or op not in LONG_OPS:
                res, rblob = self._rpc({"op": op, "args": args}, blob)
            else:
                # run it as a job on the agent and poll, so Cancel here can stop it there
                res, rblob = self._rpc({"op": op, "args": args, "async": True}, blob)
                jid, give_up = res.get("job"), None
                while res.get("pending") and jid is not None:
                    if not cancelled:
                        try:
                            check()
                        except Cancelled:
                            cancelled = True
                            give_up = time.monotonic() + 30
                            self._rpc({"op": "job_cancel", "args": {"job": jid}})
                    elif time.monotonic() > give_up:
                        log.warning("%s: %s didn't stop within 30 s of Cancel", self.label, op)
                        break
                    res, rblob = self._rpc({"op": "job_poll", "args": {"job": jid, "wait": 0.3}})
        if cancelled or res.get("kind") == "Cancelled":
            raise Cancelled(f"Stopped {op} on {self.label}")
        if not res.get("ok"):
            raise RemoteError(res.get("kind", "Error"), res.get("error", "?"))
        return (res["result"], rblob) if rblob is not None or op in ("disk_read", "read_file", "read_files") \
            else res["result"]

    def __getattr__(self, op):
        if op in OPS:
            if op in BLOB_ARG_OPS:
                return lambda blob=b"", **kw: self._call(op, blob=blob, **kw)
            return lambda **kw: self._call(op, **kw)
        raise AttributeError(op)

    def info(self):
        return self._call("info")

    def close(self):
        """Polite disconnect: tells the agent to finish (it exits instead of waiting to reconnect)."""
        if self.alive:
            try:
                with self.lock:
                    self.conn.send({"id": 0, "op": "bye"})
            except (OSError, LinkError):
                pass
        self.alive = False
        self.conn.close()
