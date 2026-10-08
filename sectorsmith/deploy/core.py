"""SectorSmith Deploy — software library, tasks, clients, deployments (desired state) and maintenance
sessions that bring linked machines into line. Runs over SectorSmith Link (or on this PC)."""
from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field

from ..util import Cancelled, Progress, app_dir, get_logger

log = get_logger()
UPLOAD_CHUNK = 4 * 1024 * 1024


def _id():
    return uuid.uuid4().hex[:10]


@dataclass
class Package:
    name: str
    kind: str = "exe"                      # msi | exe | msix | winget | script
    version: str = ""
    publisher: str = ""
    installer: str = ""                    # file name inside the library (empty for winget/script)
    install: str = ""                      # command template: {installer} {product_code} {version} {winget_id}
    uninstall: str = ""                    # may use {registry_uninstall}
    detection: dict = field(default_factory=lambda: {"method": "registry", "value": ""})
    success_codes: list = field(default_factory=lambda: [0, 3010, 1641])
    language: str = "powershell"           # for script detection
    product_code: str = ""
    upgrade_code: str = ""
    winget_id: str = ""
    category: str = ""
    prerequisites: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    id: str = field(default_factory=_id)
    created: float = field(default_factory=time.time)


@dataclass
class Task:
    name: str
    test: str = ""                         # exit 0 = compliant
    set: str = ""                          # makes it compliant
    language: str = "powershell"
    description: str = ""
    params: dict = field(default_factory=dict)
    id: str = field(default_factory=_id)


@dataclass
class Client:
    name: str
    machines: list = field(default_factory=list)  # hostnames
    id: str = field(default_factory=_id)


@dataclass
class Deployment:
    item_type: str                         # software | task
    item_id: str
    desired: str = "latest"                # software: installed|latest|version|uninstalled|ignore · task: enforce|audit
    version: str = ""
    target_kind: str = "all"               # all | client | machine
    target_value: str = ""
    onboarding_only: bool = False
    enabled: bool = True
    params: dict = field(default_factory=dict)
    id: str = field(default_factory=_id)


# ---------------------------------------------------------------------------
class Store:
    """JSON-backed library under %LOCALAPPDATA%\\SectorSmith\\deploy (or a given folder)."""

    KINDS = {"packages": Package, "tasks": Task, "clients": Client, "deployments": Deployment}

    def __init__(self, root: str | None = None):
        self.root = root or str(app_dir() / "deploy")
        os.makedirs(os.path.join(self.root, "files"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "sessions"), exist_ok=True)
        self.data = {}
        for k, cls in self.KINDS.items():
            p = os.path.join(self.root, f"{k}.json")
            raw = json.load(open(p)) if os.path.exists(p) else []
            names = {f.name for f in dataclasses.fields(cls)}
            self.data[k] = [cls(**{a: b for a, b in r.items() if a in names}) for r in raw]

    def save(self, kind=None):
        for k in ([kind] if kind else self.KINDS):
            p = os.path.join(self.root, f"{k}.json")
            tmp = p + ".tmp"
            json.dump([dataclasses.asdict(x) for x in self.data[k]], open(tmp, "w"), indent=1)
            os.replace(tmp, p)

    # convenience
    @property
    def packages(self):
        return self.data["packages"]

    @property
    def tasks(self):
        return self.data["tasks"]

    @property
    def clients(self):
        return self.data["clients"]

    @property
    def deployments(self):
        return self.data["deployments"]

    def get(self, kind, id_):
        return next((x for x in self.data[kind] if x.id == id_), None)

    def upsert(self, kind, obj):
        lst = self.data[kind]
        for i, x in enumerate(lst):
            if x.id == obj.id:
                lst[i] = obj
                break
        else:
            lst.append(obj)
        self.save(kind)
        return obj

    def delete(self, kind, id_):
        self.data[kind] = [x for x in self.data[kind] if x.id != id_]
        if kind == "packages":
            shutil.rmtree(os.path.join(self.root, "files", id_), ignore_errors=True)
            self.data["deployments"] = [d for d in self.deployments if not (d.item_type == "software"
                                                                            and d.item_id == id_)]
            self.save("deployments")
        if kind == "tasks":
            self.data["deployments"] = [d for d in self.deployments if not (d.item_type == "task"
                                                                            and d.item_id == id_)]
            self.save("deployments")
        self.save(kind)

    def installer_path(self, pkg: Package) -> str | None:
        if not pkg.installer:
            return None
        return os.path.join(self.root, "files", pkg.id, pkg.installer)

    def add_from_installer(self, path: str) -> Package:
        from .analyze import analyze
        s = analyze(path)
        pkg = Package(name=s["name"], kind=s["kind"], version=s["version"], publisher=s["publisher"],
                      installer=s["installer"], install=s["install"], uninstall=s["uninstall"],
                      detection=s["detection"], success_codes=s["success_codes"], language=s["language"],
                      product_code=s.get("product_code", ""), upgrade_code=s.get("upgrade_code", ""),
                      notes=s["notes"])
        dest = os.path.join(self.root, "files", pkg.id)
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(path, os.path.join(dest, pkg.installer))
        return self.upsert("packages", pkg)

    def replace_installer(self, pkg: Package, path: str):
        """New version of a package: swap the installer and refresh version/codes."""
        from .analyze import analyze
        s = analyze(path)
        old = self.installer_path(pkg)
        if old and os.path.exists(old):
            os.remove(old)
        pkg.installer = s["installer"]
        pkg.version = s["version"] or pkg.version
        if s.get("product_code"):
            pkg.product_code = s["product_code"]
        dest = os.path.join(self.root, "files", pkg.id)
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(path, os.path.join(dest, pkg.installer))
        return self.upsert("packages", pkg)

    def client_of(self, hostname: str) -> Client | None:
        h = hostname.lower()
        return next((c for c in self.clients if h in [m.lower() for m in c.machines]), None)

    # ------------------------------------------------------------------ export / import
    def export_package(self, pkg: Package, folder: str) -> str:
        """Writes package.json, the installer and ready-to-use PowerShell scripts (Install, Uninstall,
        Detect) — usable in SectorSmith, other RMM/deployment tools, or by hand."""
        out = os.path.join(folder, re.sub(r"[^\w.-]+", "_", f"{pkg.name}_{pkg.version}").strip("_"))
        os.makedirs(out, exist_ok=True)
        json.dump(dataclasses.asdict(pkg), open(os.path.join(out, "package.json"), "w"), indent=1)
        ip = self.installer_path(pkg)
        if ip and os.path.exists(ip):
            shutil.copy2(ip, os.path.join(out, pkg.installer))
        scripts = render_scripts(pkg)
        for name, body in scripts.items():
            with open(os.path.join(out, name), "w", encoding="utf-8-sig") as f:
                f.write(body)
        return out

    def import_package(self, folder: str) -> Package:
        meta = json.load(open(os.path.join(folder, "package.json")))
        names = {f.name for f in dataclasses.fields(Package)}
        pkg = Package(**{k: v for k, v in meta.items() if k in names})
        pkg.id = _id()
        if pkg.installer and os.path.exists(os.path.join(folder, pkg.installer)):
            dest = os.path.join(self.root, "files", pkg.id)
            os.makedirs(dest, exist_ok=True)
            shutil.copy2(os.path.join(folder, pkg.installer), os.path.join(dest, pkg.installer))
        return self.upsert("packages", pkg)

    # ------------------------------------------------------------------ sessions
    def save_session(self, sess: dict) -> str:
        name = f"{time.strftime('%Y%m%d-%H%M%S')}_{re.sub(r'[^\w-]+', '_', sess['machine'])}_{sess['id']}.json"
        p = os.path.join(self.root, "sessions", name)
        json.dump(sess, open(p, "w"), indent=1)
        return p

    def sessions(self, limit=200) -> list[dict]:
        d = os.path.join(self.root, "sessions")
        files = sorted(os.listdir(d), reverse=True)[:limit]
        out = []
        for f in files:
            try:
                out.append(json.load(open(os.path.join(d, f))))
            except (OSError, ValueError):
                pass
        return out


# ---------------------------------------------------------------------------
def render_scripts(pkg: Package) -> dict:
    """Stand-alone PowerShell for a package (Install / Uninstall / Detect)."""
    name = pkg.detection.get("value") or pkg.name
    detect = f"""# Detect '{pkg.name}' — outputs the installed version, or nothing if not installed
$paths = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
         'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'
$hit = Get-ItemProperty $paths -ErrorAction SilentlyContinue |
       Where-Object {{ $_.DisplayName -like '*{name.replace("'", "''")}*' }} |
       Sort-Object {{ [version]($_.DisplayVersion -replace '[^0-9.]','' -replace '^$','0') }} -Descending |
       Select-Object -First 1
if ($hit) {{ $hit.DisplayVersion }}
"""
    if pkg.detection.get("method") == "msi_product_code" and pkg.product_code:
        detect = f"""$k = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{pkg.product_code}',
     'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{pkg.product_code}' -ErrorAction SilentlyContinue
if ($k) {{ $k[0].DisplayVersion }}
"""
    elif pkg.detection.get("method") == "script":
        detect = pkg.detection.get("value", "")
    install_cmd = expand(pkg.install, pkg, installer='$PSScriptRoot\\' + pkg.installer if pkg.installer else "")
    uninstall_cmd = expand(pkg.uninstall, pkg, installer="")
    install = f"""# Install '{pkg.name}' {pkg.version} silently
$ErrorActionPreference = 'Stop'
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', '{install_cmd.replace("'", "''")}' -Wait -PassThru -WindowStyle Hidden
if (@({",".join(map(str, pkg.success_codes))}) -notcontains $p.ExitCode) {{ throw "Install failed with exit code $($p.ExitCode)" }}
Write-Output "Installed (exit code $($p.ExitCode))"
"""
    if "{registry_uninstall}" in pkg.uninstall:
        uninstall = f"""# Uninstall '{pkg.name}' using its own uninstaller from Add/Remove Programs
$paths = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
         'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'
$hit = Get-ItemProperty $paths -ErrorAction SilentlyContinue | Where-Object {{ $_.DisplayName -like '*{name.replace("'", "''")}*' }} | Select-Object -First 1
if (-not $hit) {{ Write-Output 'Not installed'; return }}
$cmd = if ($hit.QuietUninstallString) {{ $hit.QuietUninstallString }} else {{ $hit.UninstallString + ' {pkg.uninstall.replace("{registry_uninstall}", "").strip()}' }}
$cmd = $cmd -replace 'MsiExec.exe /I', 'MsiExec.exe /X'
if ($cmd -match 'msiexec' -and $cmd -notmatch '/qn') {{ $cmd += ' /qn /norestart' }}
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', $cmd -Wait -PassThru -WindowStyle Hidden
Write-Output "Uninstall exit code $($p.ExitCode)"
"""
    else:
        uninstall = f"""# Uninstall '{pkg.name}'
$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', '{uninstall_cmd.replace("'", "''")}' -Wait -PassThru -WindowStyle Hidden
Write-Output "Uninstall exit code $($p.ExitCode)"
"""
    return {"Install.ps1": install, "Uninstall.ps1": uninstall, "Detect.ps1": detect}


def expand(template: str, pkg: Package, installer: str = "", registry_uninstall: str = "") -> str:
    return (template.replace("{installer}", installer).replace("{product_code}", pkg.product_code or "")
            .replace("{version}", pkg.version or "").replace("{winget_id}", pkg.winget_id or "")
            .replace("{registry_uninstall}", registry_uninstall))


def vtuple(v: str | None):
    if not v:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4])


def _match_inventory(pkg: Package, inv: list[dict]):
    det = pkg.detection or {}
    method, value = det.get("method", "registry"), (det.get("value") or pkg.name)
    if method == "msi_product_code" or (method == "registry" and pkg.product_code and not det.get("value")):
        hits = [e for e in inv if e["key"].lower() == (pkg.product_code or value).lower()]
    else:
        if value.startswith("re:"):
            rx = re.compile(value[3:], re.I)
            hits = [e for e in inv if rx.search(e["name"])]
        else:
            hits = [e for e in inv if value.lower() in e["name"].lower()]
    hits.sort(key=lambda e: vtuple(e.get("version")), reverse=True)
    return hits


def detect(ep, pkg: Package, inv: list[dict]) -> tuple[bool, str | None, dict | None]:
    """(installed, version, inventory_entry)"""
    method = (pkg.detection or {}).get("method", "registry")
    if method == "file":
        v = ep.file_version(path=pkg.detection.get("value", ""))
        return v is not None, v, None
    if method == "script":
        r = ep.run_script(script=pkg.detection.get("value", ""), language=pkg.language, timeout=300)
        lines = [ln.strip() for ln in r["out"].splitlines() if ln.strip()]
        v = lines[-1] if lines and r["code"] == 0 else None
        return v is not None, v, None
    hits = _match_inventory(pkg, inv)
    if not hits:
        return False, None, None
    return True, hits[0].get("version") or "0", hits[0]


# ---------------------------------------------------------------------------
def applicable(store: Store, hostname: str, onboarding: bool) -> list[Deployment]:
    client = store.client_of(hostname)
    res = []
    for d in store.deployments:
        if not d.enabled or (d.onboarding_only and not onboarding):
            continue
        if d.target_kind == "all" or (d.target_kind == "machine" and d.target_value.lower() == hostname.lower()) \
                or (d.target_kind == "client" and client and d.target_value == client.id):
            res.append(d)
    # most specific deployment wins per item: machine > client > all
    rank = {"machine": 0, "client": 1, "all": 2}
    best = {}
    for d in sorted(res, key=lambda d: rank[d.target_kind]):
        best.setdefault((d.item_type, d.item_id), d)
    out = list(best.values())
    # prerequisites first
    order, seen = [], set()

    def visit(d, depth=0):
        if (d.item_type, d.item_id) in seen or depth > 20:
            return
        if d.item_type == "software":
            pkg = store.get("packages", d.item_id)
            for pre in (pkg.prerequisites if pkg else []):
                pd = best.get(("software", pre)) or Deployment("software", pre, "installed")
                visit(pd, depth + 1)
        seen.add((d.item_type, d.item_id))
        order.append(d)
    for d in out:
        visit(d)
    return order


def _upload(ep, store: Store, pkg: Package, prog: Progress | None) -> str:
    src = store.installer_path(pkg)
    remote_dir = ep.deploy_dir()
    sep = "\\" if "\\" in remote_dir else "/"
    dest = f"{remote_dir}{sep}{pkg.id}{sep}{pkg.installer}"
    size = os.path.getsize(src)
    with open(src, "rb") as f:
        off = 0
        while off < size or size == 0:
            data = f.read(UPLOAD_CHUNK)
            ep.put_file(path=dest, offset=off, total=size, blob=data)
            off += len(data)
            if prog:
                prog.set_detail(f"Uploading {pkg.installer}: {off * 100 // max(1, size)}%")
            if size == 0:
                break
    return dest


def _registry_uninstall(entry: dict | None) -> str:
    if not entry:
        return ""
    cmd = entry.get("quiet_uninstall") or entry.get("uninstall") or ""
    m = re.search(r"msiexec(\.exe)?\s+/[ix]\s*(\{[0-9A-Fa-f-]+\})", cmd, re.I)
    if m:
        return f"msiexec.exe /x {m.group(2)} /qn /norestart"
    return cmd


def plan(ep, store: Store, hostname: str, inv: list[dict], onboarding=False) -> list[dict]:
    """Detection stage: what each applicable deployment needs on this machine (read-only)."""
    actions = []
    for d in applicable(store, hostname, onboarding):
        if d.item_type == "software":
            pkg = store.get("packages", d.item_id)
            if pkg is None or d.desired == "ignore":
                continue
            installed, ver, entry = detect(ep, pkg, inv)
            want = d.version if d.desired == "version" else pkg.version
            if d.desired == "uninstalled":
                act = "uninstall" if installed else "none"
            elif not installed:
                act = "install"
            elif d.desired in ("latest", "version") and want and vtuple(ver) < vtuple(want):
                act = "upgrade"
            elif d.desired == "version" and want and vtuple(ver) > vtuple(want):
                act = "reinstall"  # downgrade
            else:
                act = "none"
            actions.append({"type": "software", "deployment": d.id, "item": pkg.id, "name": pkg.name,
                            "desired": d.desired + (f" {want}" if d.desired in ("latest", "version") and want else ""),
                            "current": ver or "not installed", "action": act, "entry": entry})
        else:
            task = store.get("tasks", d.item_id)
            if task is None:
                continue
            r = ep.run_script(script=task.test, language=task.language, params={**task.params, **d.params},
                              timeout=600)
            ok = r["code"] == 0
            act = "none" if ok else ("set" if d.desired == "enforce" else "audit")
            actions.append({"type": "task", "deployment": d.id, "item": task.id, "name": task.name,
                            "desired": d.desired, "current": "compliant" if ok else "not compliant",
                            "action": act, "test_output": r["out"][-500:]})
    return actions


def execute(ep, store: Store, a: dict, prog: Progress | None = None) -> dict:
    """Execution stage for one action. Returns the action updated with result/status/log."""
    t0 = time.time()
    a = dict(a)
    log_lines = []
    try:
        if a["type"] == "software":
            pkg = store.get("packages", a["item"])
            if a["action"] in ("uninstall", "reinstall"):
                cmd = expand(pkg.uninstall, pkg, registry_uninstall=_registry_uninstall(a.get("entry")))
                r = ep.run_command(cmd=cmd, timeout=3600)
                log_lines.append(f"$ {cmd}\nexit {r['code']}\n{r['out']}{r['err']}")
            if a["action"] in ("install", "upgrade", "reinstall"):
                remote = _upload(ep, store, pkg, prog) if pkg.installer else ""
                cmd = expand(pkg.install, pkg, installer=remote)
                if prog:
                    prog.set_detail(f"Installing {pkg.name} {pkg.version}")
                r = ep.run_command(cmd=cmd, timeout=3600)
                log_lines.append(f"$ {cmd}\nexit {r['code']}\n{r['out']}{r['err']}")
                if r["code"] not in pkg.success_codes:
                    a.update(status="failed", result=f"Installer exit code {r['code']}")
                    return a
                if r["code"] in (3010, 1641):
                    a["reboot"] = True
            inv = ep.installed_software()
            installed, ver, _ = detect(ep, pkg, inv)
            want_absent = a["action"] == "uninstall"
            if want_absent:
                ok = not installed
            else:
                ok = installed and (not pkg.version or a["desired"].startswith("installed")
                                    or vtuple(ver) >= vtuple(pkg.version))
            a.update(status="compliant" if ok else "failed",
                     result=("removed" if want_absent else f"now {ver}") if ok else
                     f"still {'installed' if installed else 'missing'} after {a['action']} ({ver or '-'})")
        else:
            task = store.get("tasks", a["item"])
            dep = store.get("deployments", a["deployment"])
            params = {**task.params, **(dep.params if dep else {})}
            if a["action"] == "set":
                r = ep.run_script(script=task.set, language=task.language, params=params, timeout=1800)
                log_lines.append(f"[set] exit {r['code']}\n{r['out']}{r['err']}")
                t = ep.run_script(script=task.test, language=task.language, params=params, timeout=600)
                log_lines.append(f"[test] exit {t['code']}\n{t['out']}{t['err']}")
                a.update(status="compliant" if t["code"] == 0 else "failed",
                         result="fixed" if t["code"] == 0 else "still not compliant after set")
            else:
                a.update(status="non-compliant", result="audit only — not changed")
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        a.update(status="failed", result=str(e))
    finally:
        a["log"] = "\n".join(log_lines)[-8000:]
        a["seconds"] = round(time.time() - t0, 1)
    return a


def run_session(ep, store: Store, prog: Progress, mode: str = "full", onboarding: bool = False) -> dict:
    """Detect → (execute) → re-check. mode: 'detect' (read-only) or 'full'."""
    info = ep.info()
    host = info["hostname"]
    sess = {"id": _id(), "machine": host, "started": time.time(), "mode": mode, "onboarding": onboarding,
            "client": (store.client_of(host).name if store.client_of(host) else ""), "actions": []}
    prog.reset(1, f"Detecting on {host}…")
    inv = ep.installed_software()
    actions = plan(ep, store, host, inv, onboarding)
    sess["detected"] = [dict(a, entry=None) for a in actions]
    todo = [a for a in actions if a["action"] not in ("none", "audit")]
    if mode == "detect" or not todo:
        sess["actions"] = [dict(a, status="compliant" if a["action"] == "none" else
                                ("non-compliant" if a["action"] == "audit" else "pending"), entry=None)
                           for a in actions]
    else:
        prog.reset(len(todo), f"Maintaining {host}: {len(todo)} change(s)")
        done = {}
        for i, a in enumerate(todo):
            prog.check()
            prog.set_label(f"{a['action'].capitalize()}: {a['name']}")
            r = execute(ep, store, a, prog)
            done[a["deployment"]] = r
            prog.update(i + 1)
        sess["actions"] = [dict(done.get(a["deployment"]) or
                                dict(a, status="compliant" if a["action"] == "none" else "non-compliant"),
                                entry=None) for a in actions]
    sess["finished"] = time.time()
    st = [a["status"] for a in sess["actions"]]
    sess["summary"] = {"compliant": st.count("compliant"), "failed": st.count("failed"),
                       "pending": st.count("pending"), "non_compliant": st.count("non-compliant"),
                       "reboot": any(a.get("reboot") for a in sess["actions"])}
    sess["path"] = store.save_session(sess)
    log.info("Deploy session %s on %s: %s", sess["id"], host, sess["summary"])
    return sess
