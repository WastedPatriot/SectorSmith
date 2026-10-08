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
import zlib

from ..device import Device, list_disks, open_image, volume_letters_for
from ..partitions import read_partition_table
from ..util import APP_VERSION, get_logger, is_admin
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
       "installed_software", "put_file", "run_command", "run_script", "file_version", "remove_path", "deploy_dir"}


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
        dev = Device(**{f: getattr(d, f) for f in ("path", "name", "size", "sector_size", "model", "bus", "serial",
                                                   "is_image", "is_system", "removable", "disk_number",
                                                   "data_size")})
        dev.open(writable=writable)
        self._open[path] = dev
        info = {"usable": dev.usable_size, "sector_size": dev.sector_size, "snapshot": [], "notes": []}
        if snapshot and not writable:
            from ..vss import needs_snapshot, snapshot_disk
            if needs_snapshot(dev):
                snap = snapshot_disk(dev)
                self._snaps[path] = snap
                info["snapshot"], info["notes"] = snap.volumes, snap.notes
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

    def list_dir(self, path):
        """Sub-folder names of path."""
        try:
            with os.scandir(_long(path)) as it:
                return sorted(e.name for e in it if e.is_dir(follow_symlinks=False))
        except OSError:
            return []

    def path_exists(self, path):
        return os.path.exists(_long(path))

    def free_space(self, path):
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return None

    def scan_tree(self, root, rel=""):
        """All files under root/rel: [[relpath, size, mtime], ...] (relative to root). Skips caches/temp."""
        base = os.path.join(root, _safe_rel(rel)) if rel else root
        out = []
        if not os.path.isdir(_long(base)):
            return {"files": [], "errors": 0}
        errors = 0
        stack = [base]
        while stack:
            d = stack.pop()
            in_appdata = "appdata" in os.path.relpath(d, root).lower().replace("\\", "/").split("/")[:1]
            try:
                with os.scandir(_long(d)) as it:
                    for e in it:
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
        with open(_long(os.path.join(root, _safe_rel(rel))), "rb") as f:
            f.seek(offset)
            data = f.read(size)
        return {"n": len(data)}, data

    def read_files(self, root, rels):
        sizes, buf, errs = [], bytearray(), {}
        for r in rels:
            try:
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
        p = _long(path)
        existed = os.path.isdir(p)
        os.makedirs(p, exist_ok=True)
        return {"path": path, "existed": existed, "free": self.free_space(path)}

    def mkdirs(self, root, rels):
        for r in rels:
            os.makedirs(_long(os.path.join(root, _safe_rel(r))), exist_ok=True)
        return True

    def write_file(self, root, rel, offset, total, mtime=None, blob=b""):
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

    def run_command(self, cmd, timeout=3600, cwd=None):
        import subprocess
        import time as _t
        t0 = _t.time()
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, timeout=timeout, cwd=cwd,  # nosec B602
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            code, out, err = r.returncode, r.stdout, r.stderr
        except subprocess.TimeoutExpired as e:
            code, out, err = -1, e.stdout or b"", (e.stderr or b"") + b"\n[timed out]"
        dec = (lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or ""))
        return {"code": code, "out": dec(out)[-6000:], "err": dec(err)[-3000:], "seconds": round(_t.time() - t0, 1)}

    def run_script(self, script, language="powershell", timeout=1800, params=None):
        import tempfile
        params = params or {}
        if language == "powershell":
            head = "".join(f"${k} = '{str(v).replace(chr(39), chr(39) * 2)}'\n" for k, v in params.items())
            fd, p = tempfile.mkstemp(suffix=".ps1", dir=self.deploy_dir())
            with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
                f.write(head + script)
            exe = "powershell.exe" if sys.platform == "win32" else "pwsh"
            cmd = f'{exe} -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{p}"'
        else:
            import shlex
            head = "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in params.items())
            fd, p = tempfile.mkstemp(suffix=".sh", dir=self.deploy_dir())
            with os.fdopen(fd, "w") as f:
                f.write(head + script)
            cmd = f'sh "{p}"'
        try:
            return self.run_command(cmd, timeout=timeout)
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
        log.warning("Link op %s failed: %s", op, e)
        return {"id": req.get("id"), "ok": False, "error": str(e), "kind": type(e).__name__}, None


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

    def _call(self, op, blob=None, **args):
        with self.lock:
            if not self.alive:
                raise LinkError(f"{self.label} is disconnected")
            self._id += 1
            try:
                self.conn.send({"id": self._id, "op": op, "args": args}, blob)
                res, rblob = self.conn.recv()
            except (OSError, LinkError) as e:
                self.alive = False
                raise LinkError(f"Lost connection to {self.label}: {e}") from e
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
