"""SectorSmith Deploy: software library, tasks, clients, deployments (desired state) and maintenance
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

from ..util import Cancelled, Progress, app_dir, cancel_scope, check_cancel, get_logger

log = get_logger()
UPLOAD_CHUNK = 4 * 1024 * 1024
WINGET_INSTALL = "winget install --id {winget_id} -e --silent --accept-package-agreements --accept-source-agreements"
WINGET_UNINSTALL = "winget uninstall --id {winget_id} -e --silent"


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
            raw = []
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    raw = json.load(f)
            names = {f.name for f in dataclasses.fields(cls)}
            self.data[k] = [cls(**{a: b for a, b in r.items() if a in names}) for r in raw]

    def save(self, kind=None):
        for k in ([kind] if kind else self.KINDS):
            p = os.path.join(self.root, f"{k}.json")
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump([dataclasses.asdict(x) for x in self.data[k]], f, indent=1)
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
        if kind == "clients":
            # a client-targeted deployment without its client would never match anything again
            self.data["deployments"] = [d for d in self.deployments if not (d.target_kind == "client"
                                                                            and d.target_value == id_)]
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
        pkg.kind = s["kind"]
        if s.get("product_code"):
            pkg.product_code = s["product_code"]
        if s.get("upgrade_code"):
            pkg.upgrade_code = s["upgrade_code"]
        dest = os.path.join(self.root, "files", pkg.id)
        os.makedirs(dest, exist_ok=True)
        shutil.copy2(path, os.path.join(dest, pkg.installer))
        return self.upsert("packages", pkg)

    def client_of(self, hostname: str) -> Client | None:
        return next(iter(self.clients_of(hostname)), None)

    def clients_of(self, hostname: str) -> list[Client]:
        h = hostname.lower()
        return [c for c in self.clients if h in [m.lower() for m in c.machines]]

    # ------------------------------------------------------------------ export / import
    def export_package(self, pkg: Package, folder: str) -> str:
        """Writes package.json, the installer and ready-to-use PowerShell scripts (Install, Uninstall,
        Detect), usable in SectorSmith, other RMM/deployment tools, or by hand."""
        out = os.path.join(folder, re.sub(r"[^\w.-]+", "_", f"{pkg.name}_{pkg.version}").strip("_"))
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "package.json"), "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(pkg), f, indent=1)
        ip = self.installer_path(pkg)
        if ip and os.path.exists(ip):
            shutil.copy2(ip, os.path.join(out, pkg.installer))
        scripts = render_scripts(pkg)
        for name, body in scripts.items():
            ps = name.endswith(".ps1")
            # BOM so Windows PowerShell 5.1 reads non-ASCII names right; sh wants neither BOM nor CRLF
            with open(os.path.join(out, name), "w", encoding="utf-8-sig" if ps else "utf-8",
                      newline="\r\n" if name.endswith((".ps1", ".cmd")) else "\n") as f:
                f.write(body)
        return out

    def import_package(self, folder: str) -> Package:
        with open(os.path.join(folder, "package.json"), encoding="utf-8") as f:
            meta = json.load(f)
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
        # milliseconds in the name so sessions() lists runs from the same second in order
        t = sess.get("started") or time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + f"{int(t * 1000) % 1000:03d}"
        name = f"{stamp}_{re.sub(r'[^\w-]+', '_', sess['machine'])}_{sess['id']}.json"
        p = os.path.join(self.root, "sessions", name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(sess, f, indent=1)
        return p

    def sessions(self, limit=200) -> list[dict]:
        d = os.path.join(self.root, "sessions")
        files = sorted(os.listdir(d), reverse=True)[:limit]
        out = []
        for f in files:
            try:
                with open(os.path.join(d, f), encoding="utf-8") as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                pass
        return out


# ---------------------------------------------------------------------------
_UNINSTALL_KEYS = """$paths = 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
         'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*',
         'Registry::HKEY_USERS\\*\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*'
"""
# same ordering as vtuple(): up to four numeric parts, missing parts count as 0
_VERSION_KEY = """function Get-VersionKey($v) {
    $n = @([regex]::Matches("$v", '\\d+') | Select-Object -First 4 | ForEach-Object { [int64]$_.Value })
    while ($n.Count -lt 4) { $n += 0 }
    '{0:D12}.{1:D12}.{2:D12}.{3:D12}' -f $n
}
"""
# /s strips exactly the outer quotes we add, so commands with several quoted parts survive cmd.exe
_RUN = """$p = Start-Process -FilePath $env:ComSpec -ArgumentList ('/s /c "' + $cmd + '"') -Wait -PassThru -WindowStyle Hidden
"""
SCRIPT_EXT = {"powershell": ".ps1", "shell": ".sh", "sh": ".sh", "cmd": ".cmd"}


def _ps(text: str) -> str:
    """PowerShell single-quoted literal."""
    return "'" + str(text).replace("'", "''") + "'"


def _name_filter(value: str) -> str:
    if value.startswith("re:"):
        return f"$_.DisplayName -match {_ps(value[3:])}"
    return f"$_.DisplayName -like ('*' + [WildcardPattern]::Escape({_ps(value)}) + '*')"


def render_scripts(pkg: Package) -> dict:
    """Stand-alone scripts for a package: Install.ps1, Uninstall.ps1 and Detect.ps1 (plus Detect.sh/.cmd when the
    detection script is not PowerShell)."""
    det = pkg.detection or {}
    method, value = det.get("method", "registry"), det.get("value") or ""
    out = {}
    if method == "script":
        lang = pkg.language or "powershell"
        if lang == "powershell":
            detect = value
        else:
            other = "Detect" + SCRIPT_EXT.get(lang, ".txt")
            out[other] = value if lang == "cmd" else value.replace("\r\n", "\n")
            detect = (f"# Detection for '{pkg.name}' is a {lang} script, not PowerShell: see {other}\n"
                      f"Write-Error 'Use {other} to detect this package'\nexit 1\n")
    elif method == "file":
        detect = f"""# Detect '{pkg.name}' by file: outputs its version, or nothing if the file is missing
$f = [Environment]::ExpandEnvironmentVariables({_ps(value)})
if (Test-Path -LiteralPath $f -PathType Leaf) {{
    $v = (Get-Item -LiteralPath $f).VersionInfo.FileVersion
    if (-not $v -and $f -notmatch '\\.(exe|dll|sys)$') {{ $v = Get-Content -LiteralPath $f -TotalCount 1 }}
    if ($v) {{ "$v".Trim() }} else {{ '0' }}
}}
"""
    elif pkg.product_code and (method == "msi_product_code" or (method == "registry" and not value)):
        detect = f"""# Detect '{pkg.name}' by MSI product code: outputs the installed version, or nothing if not installed
$k = Get-ItemProperty 'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{pkg.product_code}',
     'HKLM:\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\{pkg.product_code}' -ErrorAction SilentlyContinue
if ($k) {{ @($k)[0].DisplayVersion }}
"""
    else:
        detect = f"""# Detect '{pkg.name}': outputs the installed version, or nothing if not installed
{_VERSION_KEY}{_UNINSTALL_KEYS}$hit = Get-ItemProperty $paths -ErrorAction SilentlyContinue |
       Where-Object {{ {_name_filter(value or pkg.name)} }} |
       Sort-Object {{ Get-VersionKey $_.DisplayVersion }} -Descending |
       Select-Object -First 1
if ($hit) {{ if ($hit.DisplayVersion) {{ $hit.DisplayVersion }} else {{ '0' }} }}
"""
    install = f"# Install '{pkg.name}' {pkg.version} silently\n$ErrorActionPreference = 'Stop'\n"
    cmd = expand(pkg.install, pkg, installer="{installer}")
    if pkg.installer:
        install += f"$installer = Join-Path $PSScriptRoot {_ps(pkg.installer)}\n"
        install += f"$cmd = {_ps(cmd)}.Replace('{{installer}}', $installer)\n"
    else:
        install += f"$cmd = {_ps(cmd)}\n"
    codes = ",".join(map(str, pkg.success_codes or [0]))
    install += _RUN + f"""if (@({codes}) -notcontains $p.ExitCode) {{ throw "Install failed with exit code $($p.ExitCode)" }}
Write-Output "Installed (exit code $($p.ExitCode))"
exit $p.ExitCode
"""
    if "{registry_uninstall}" in pkg.uninstall:
        extra = expand(pkg.uninstall, pkg).strip()
        uninstall = f"""# Uninstall '{pkg.name}' using its own uninstaller from Add/Remove Programs
{_VERSION_KEY}{_UNINSTALL_KEYS}$hit = Get-ItemProperty $paths -ErrorAction SilentlyContinue |
       Where-Object {{ {_name_filter(value if method == "registry" and value else pkg.name)} }} |
       Sort-Object {{ Get-VersionKey $_.DisplayVersion }} -Descending |
       Select-Object -First 1
if (-not $hit) {{ Write-Output 'Not installed'; exit 0 }}
if ($hit.UninstallString -match 'msiexec(\\.exe)?\\s+/[ix]\\s*(\\{{[0-9A-Fa-f-]+\\}})') {{
    $cmd = 'msiexec.exe /x ' + $Matches[2] + ' /qn /norestart'
}} elseif ($hit.QuietUninstallString) {{
    $cmd = $hit.QuietUninstallString
}} else {{
    $cmd = $hit.UninstallString + ' ' + {_ps(extra)}
}}
""" + _RUN + """Write-Output "Uninstall exit code $($p.ExitCode)"
exit $p.ExitCode
"""
    elif pkg.uninstall.strip():
        uninstall = f"# Uninstall '{pkg.name}'\n$cmd = {_ps(expand(pkg.uninstall, pkg))}\n" + _RUN + \
            'Write-Output "Uninstall exit code $($p.ExitCode)"\nexit $p.ExitCode\n'
    else:
        uninstall = f"# '{pkg.name}' has no uninstall command\nWrite-Error 'No uninstall command is set for this package'\nexit 1\n"
    out.update({"Install.ps1": install, "Uninstall.ps1": uninstall, "Detect.ps1": detect})
    return out


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
    client_ids = {c.id for c in store.clients_of(hostname)}
    res = []
    for d in store.deployments:
        if not d.enabled or (d.onboarding_only and not onboarding):
            continue
        if d.target_kind == "all" or (d.target_kind == "machine" and d.target_value.lower() == hostname.lower()) \
                or (d.target_kind == "client" and d.target_value in client_ids):
            res.append(d)
    # most specific deployment wins per item: machine > client > all
    rank = {"machine": 0, "client": 1, "all": 2}
    best = {}
    for d in sorted(res, key=lambda d: rank.get(d.target_kind, 3)):
        best.setdefault((d.item_type, d.item_id), d)
    # prerequisites first
    order, seen, visiting = [], set(), set()

    def visit(d):
        key = (d.item_type, d.item_id)
        if key in seen or key in visiting:  # done already, or a prerequisite loop
            return
        visiting.add(key)
        pkg = store.get("packages", d.item_id) if d.item_type == "software" else None
        # removing a package must not pull its prerequisites onto the machine
        if pkg and d.desired not in ("uninstalled", "ignore"):
            for pre in pkg.prerequisites:
                visit(best.get(("software", pre)) or Deployment("software", pre, "installed", id=f"pre-{pre}"))
        visiting.discard(key)
        seen.add(key)
        order.append(d)
    for d in best.values():
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
            check_cancel()
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
            note = ""
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
            # the library holds one installer per package, so a pin to any other version can't be installed
            if d.desired == "version" and act != "none" and want and pkg.version \
                    and vtuple(pkg.version) != vtuple(want):
                act, note = "audit", f"the library has {pkg.version}, this deployment pins {want}"
            a = {"type": "software", "deployment": d.id, "item": pkg.id, "name": pkg.name,
                 "desired": d.desired + (f" {want}" if d.desired in ("latest", "version") and want else ""),
                 "want": want if d.desired in ("latest", "version") else "",
                 "current": ver or "not installed", "action": act, "entry": entry}
            if note:
                a["result"] = note
            actions.append(a)
        else:
            task = store.get("tasks", d.item_id)
            if task is None or d.desired == "ignore":
                continue
            r = ep.run_script(script=task.test, language=task.language, params={**task.params, **d.params},
                              timeout=600)
            ok = r["code"] == 0
            act = "none" if ok else ("set" if d.desired == "enforce" else "audit")
            actions.append({"type": "task", "deployment": d.id, "item": task.id, "name": task.name,
                            "desired": d.desired, "current": "compliant" if ok else "not compliant",
                            "action": act, "test_output": (r["out"] + r["err"])[-500:]})
    return actions


def execute(ep, store: Store, a: dict, prog: Progress | None = None) -> dict:
    """Execution stage for one action. Returns the action updated with result/status/log."""
    t0 = time.time()
    a = dict(a)
    log_lines = []

    def run(cmd):
        r = ep.run_command(cmd=cmd, timeout=3600)
        log_lines.append(f"$ {cmd}\nexit {r['code']}\n{r['out']}{r['err']}")
        return r
    try:
        if a["type"] == "software":
            pkg = store.get("packages", a["item"])
            if pkg is None:
                a.update(status="failed", result="the package was deleted from the library before it could run")
                return a
            if a["action"] in ("uninstall", "reinstall"):
                reg = _registry_uninstall(a.get("entry"))
                if "{registry_uninstall}" in pkg.uninstall and not reg:
                    a.update(status="failed", result="no uninstall command found in Add/Remove Programs")
                    return a
                if "{registry_uninstall}" in pkg.uninstall and reg.lower().startswith("msiexec"):
                    cmd = reg  # EXE switches like /S or /VERYSILENT would make msiexec refuse to run
                else:
                    cmd = expand(pkg.uninstall, pkg, registry_uninstall=reg)
                if not cmd.strip():
                    a.update(status="failed", result="no uninstall command is set for this package")
                    return a
                if prog:
                    prog.set_detail(f"Removing {pkg.name}")
                run(cmd)
            if a["action"] in ("install", "upgrade", "reinstall"):
                remote = _upload(ep, store, pkg, prog) if pkg.installer else ""
                try:
                    if prog:
                        prog.set_detail(f"Installing {pkg.name} {pkg.version}")
                    r = run(expand(pkg.install, pkg, installer=remote))
                finally:
                    if remote:
                        try:
                            ep.remove_path(path=remote[:-len(pkg.installer) - 1])
                        except Exception as e:  # noqa: BLE001
                            log.warning("Couldn't remove the uploaded installer %s: %s", remote, e)
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
            elif a["desired"].startswith("version") and a.get("want"):
                ok = installed and vtuple(ver) == vtuple(a["want"])
            else:
                ok = installed and (not pkg.version or a["desired"].startswith("installed")
                                    or vtuple(ver) >= vtuple(pkg.version))
            a.update(status="compliant" if ok else "failed",
                     result=("removed" if want_absent else f"now {ver}") if ok else
                     f"still {'installed' if installed else 'missing'} after {a['action']} ({ver or '-'})")
        else:
            task = store.get("tasks", a["item"])
            if task is None:
                a.update(status="failed", result="the task was deleted before it could run")
                return a
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
                a.update(status="non-compliant", result="audit only, not changed")
    except Cancelled as c:
        a.update(status="cancelled", result="stopped part-way by Cancel: check this item on the PC")
        c.info["action"] = a
        raise
    except Exception as e:  # noqa: BLE001
        a.update(status="failed", result=str(e))
    finally:
        a["log"] = "\n".join(log_lines)[-8000:]
        a["seconds"] = round(time.time() - t0, 1)
    return a


def run_session(ep, store: Store, prog: Progress, mode: str = "full", onboarding: bool = False) -> dict:
    """Detect → (execute) → re-check. mode: 'detect' (read-only) or 'full'.

    Cancel stops a running installer or script (on a linked PC too). What already ran is saved as a session
    with the rest marked 'cancelled', and Cancelled.info["session"] holds it."""
    with cancel_scope(prog.check):
        return _run_session(ep, store, prog, mode, onboarding)


def _run_session(ep, store, prog, mode, onboarding):
    info = ep.info()
    host = info["hostname"]
    sess = {"id": _id(), "machine": host, "started": time.time(), "mode": mode, "onboarding": onboarding,
            "client": ", ".join(c.name for c in store.clients_of(host)), "actions": []}
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
        try:
            for i, a in enumerate(todo):
                prog.check()
                prog.set_label(f"{a['action'].capitalize()}: {a['name']}")
                r = execute(ep, store, a, prog)
                done[a["deployment"]] = r
                prog.update(i + 1)
        except Cancelled as c:
            if c.info.get("action"):
                part = c.info.pop("action")
                done[part["deployment"]] = part
            sess["actions"] = [dict(done.get(a["deployment"]) or
                                    (dict(a, status="cancelled", result="not run: cancelled")
                                     if a["action"] not in ("none", "audit") else
                                     dict(a, status="compliant" if a["action"] == "none" else "non-compliant")),
                                    entry=None) for a in actions]
            _finish(store, sess, cancelled=True)
            c.info["session"] = sess
            raise
        sess["actions"] = [dict(done.get(a["deployment"]) or
                                dict(a, status="compliant" if a["action"] == "none" else "non-compliant"),
                                entry=None) for a in actions]
    return _finish(store, sess)


def _finish(store, sess, cancelled=False):
    sess["finished"] = time.time()
    st = [a["status"] for a in sess["actions"]]
    sess["summary"] = {"compliant": st.count("compliant"), "failed": st.count("failed"),
                       "pending": st.count("pending"), "non_compliant": st.count("non-compliant"),
                       "cancelled": st.count("cancelled"),
                       "reboot": any(a.get("reboot") for a in sess["actions"])}
    if cancelled:
        sess["cancelled"] = True
    sess["path"] = store.save_session(sess)
    log.info("Deploy session %s on %s%s: %s", sess["id"], sess["machine"], " (cancelled)" if cancelled else "",
             sess["summary"])
    return sess
