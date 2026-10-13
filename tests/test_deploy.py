"""SectorSmith Deploy tests: library store, installer analysis, detect -> execute -> re-check sessions, script export,
and a session against a real agent over Link.

Linux only (packages and tasks are shell commands; installed software comes from SECTORSMITH_FAKE_SOFTWARE).
Usage: python tests/test_deploy.py [workdir]
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

W = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else tempfile.mkdtemp(prefix="ssdeploy_"))
shutil.rmtree(W, ignore_errors=True)
os.makedirs(W)
# keep the deploy cache, log and agent files out of the real home folder
os.environ["HOME"] = os.path.join(W, "home")
os.environ.pop("LOCALAPPDATA", None)
FAKE = os.path.join(W, "installed.json")
os.environ["SECTORSMITH_FAKE_SOFTWARE"] = FAKE

from sectorsmith.deploy import analyze as A  # noqa: E402
from sectorsmith.deploy import core  # noqa: E402
from sectorsmith.deploy.core import Client, Deployment, Package, Store, Task  # noqa: E402
from sectorsmith.link.endpoint import LocalEndpoint  # noqa: E402
from sectorsmith.util import Progress  # noqa: E402

OK = []
PY = sys.executable


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    OK.append(bool(c))


# --- fixtures ----------------------------------------------------------------------------
def build_cfb(path, streams):
    """Minimal OLE compound file (v3, 512-byte sectors) with every stream in the mini stream."""
    names = list(streams)
    mini, starts = b"", []
    for n in names:
        starts.append(len(mini) // 64)
        data = streams[n]
        assert len(data) < 4096
        mini += data + b"\0" * (-len(data) % 64)
    n_mini = len(mini) // 64
    n_dir = -(-(len(names) + 1) // 4)
    n_minifat = max(1, -(-n_mini // 128))
    n_ms = -(-len(mini) // 512)
    END, FREE, NOSTREAM = 0xFFFFFFFE, 0xFFFFFFFF, 0xFFFFFFFF

    def chain(first, n):
        return [first + i + 1 for i in range(n - 1)] + [END]
    fat = [0xFFFFFFFD] + chain(1, n_dir) + chain(1 + n_dir, n_minifat) + chain(1 + n_dir + n_minifat, n_ms)
    fat += [FREE] * (128 - len(fat))
    minifat = []
    for i, n in enumerate(names):
        cnt = -(-len(streams[n]) // 64)
        minifat += chain(starts[i], cnt)
    minifat += [FREE] * (n_minifat * 128 - len(minifat))

    def entry(name, typ, start, size, child=NOSTREAM, right=NOSTREAM):
        nm = name.encode("utf-16-le") + b"\0\0"
        return (nm.ljust(64, b"\0") + struct.pack("<HBB3I", len(nm), typ, 1, NOSTREAM, right, child)
                + b"\0" * 36 + struct.pack("<IQ", start, size))
    dirs = entry("Root Entry", 5, 1 + n_dir + n_minifat, len(mini), child=1)
    for i, n in enumerate(names):
        dirs += entry(n, 2, starts[i], len(streams[n]), right=i + 2 if i + 1 < len(names) else NOSTREAM)
    dirs = dirs.ljust(n_dir * 512, b"\0")
    hdr = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
           + b"\0" * 6 + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, 1 + n_dir, n_minifat, END, 0)
           + struct.pack("<I", 0) + struct.pack("<I", FREE) * 108)
    with open(path, "wb") as f:
        f.write(hdr + struct.pack("<128I", *fat) + dirs + struct.pack(f"<{len(minifat)}I", *minifat)
                + mini.ljust(n_ms * 512, b"\0"))


def msi_stream(table):
    """Stream name the way MSI stores table names (the reverse of analyze._msi_name)."""
    out = [chr(0x4840)]
    for i in range(0, len(table), 2):
        a = A._B64.index(table[i])
        out.append(chr(0x3800 + a + (A._B64.index(table[i + 1]) << 6)) if i + 1 < len(table) else chr(0x4800 + a))
    return "".join(out)


def build_msi(path, props, codepage=1252):
    strings = []
    for k, v in props.items():
        strings += [k, v]
    enc = [s.encode(f"cp{codepage}") for s in strings]
    pool = struct.pack("<HH", codepage, 0) + b"".join(struct.pack("<HH", len(e), 1) for e in enc)
    ids = list(range(1, len(strings) + 1))
    table = struct.pack(f"<{len(props)}H", *ids[0::2]) + struct.pack(f"<{len(props)}H", *ids[1::2])
    build_cfb(path, {msi_stream("_StringPool"): pool, msi_stream("_StringData"): b"".join(enc),
                     msi_stream("Property"): table})


def ver_string(key, value, at):
    """A VS_VERSIONINFO String entry as it sits in a PE resource (DWORD aligned), starting at offset ``at``."""
    k = key.encode("utf-16-le") + b"\0\0"
    v = value.encode("utf-16-le") + b"\0\0"
    head = struct.pack("<HHH", 0, len(value) + 1, 1) + k
    head += b"\0" * (-(at + len(head)) % 4)
    body = head + v
    return body + b"\0" * (-(at + len(body)) % 4)


def fake_exe(path, marker, info):
    blob = bytearray(b"MZ" + b"\0" * 254 + b"This program cannot be run in DOS mode.\0\0\0\0")
    for k, v in info.items():
        blob += ver_string(k, v, len(blob))
    blob += b"\0" * 64 + marker + b"\0" * 300
    with open(path, "wb") as _f:
        _f.write(bytes(blob))


FAKEINST = os.path.join(W, "fakeinst.py")
_FAKEINST_SRC = ('''import hashlib, json, os, sys
# stands in for an installer: edits the fake Add/Remove Programs list the endpoint reads
inv_path = os.environ["SECTORSMITH_FAKE_SOFTWARE"]
inv = json.load(open(inv_path))
op, name = sys.argv[1], sys.argv[2]
args = sys.argv[3:]
opt = dict(zip(args[1::2], args[2::2])) if op == "add" else {}
if "--file" in opt:
    data = open(opt["--file"], "rb").read()
    if hashlib.sha256(data).hexdigest() != opt["--sha"]:
        sys.exit(1603)
if "--noop" not in opt:
    inv = [e for e in inv if e["name"] != name]
    if op == "add":
        inv.append({"key": "{" + name + "}", "name": name, "version": args[0], "publisher": "Test",
                    "scope": "machine", "uninstall": '"%s" "%s" remove "%s"' % (sys.executable, __file__, name),
                    "quiet_uninstall": "", "system_component": False})
    json.dump(inv, open(inv_path, "w"))
sys.exit(int(opt.get("--exit", 0)))
''')
with open(FAKEINST, "w") as _f:
    _f.write(_FAKEINST_SRC)


def entry(name, version):
    return {"key": "{" + name + "}", "name": name, "version": version, "publisher": "Test", "scope": "machine",
            "uninstall": f'"{PY}" "{FAKEINST}" remove "{name}"', "quiet_uninstall": "", "system_component": False}


def set_inventory(*entries):
    with open(FAKE, "w") as _f:
        json.dump(list(entries), _f)


def inventory_of(path):
    with open(path) as _f:
        return {e["name"]: e["version"] for e in json.load(_f)}


def inventory():
    return inventory_of(FAKE)


def installer_pkg(store, name, version, extra="", **kw):
    """A package whose 'installer' is a copy of fakeinst.py, run with python like a real setup.exe would be."""
    src = os.path.join(W, "src", f"setup_{name.replace(' ', '_')}.py")
    os.makedirs(os.path.dirname(src), exist_ok=True)
    shutil.copy(FAKEINST, src)
    pkg = Package(name=name, version=version, kind="exe", installer=os.path.basename(src),
                  install=f'"{PY}" "{{installer}}" add "{name}" {{version}}{extra}',
                  uninstall="{registry_uninstall}", detection={"method": "registry", "value": name}, **kw)
    os.makedirs(os.path.join(store.root, "files", pkg.id))
    shutil.copy(src, store.installer_path(pkg))
    return store.upsert("packages", pkg)


def deploy(store, item, desired="installed", **kw):
    kind = "software" if isinstance(item, Package) else "task"
    return store.upsert("deployments", Deployment(kind, item.id, desired, **kw))


# --- 1. store + model ---------------------------------------------------------------------
lib = os.path.join(W, "lib")
st = Store(lib)
p = st.upsert("packages", Package(name="Notepad Plus", version="8.6", publisher="Don", kind="exe",
                                  prerequisites=["x"], notes=["a note"]))
t = st.upsert("tasks", Task(name="Firewall on", test="true", set="true", language="shell", params={"a": 1}))
c = st.upsert("clients", Client(name="Contoso", machines=["PC-1", "pc-2"]))
d1 = deploy(st, p, "latest", target_kind="client", target_value=c.id)
d2 = deploy(st, t, "enforce", params={"a": 2})
d3 = deploy(st, p, "uninstalled", target_kind="machine", target_value="PC-9")
st2 = Store(lib)
check(st2.get("packages", p.id) == p and st2.get("tasks", t.id) == t and st2.get("clients", c.id) == c
      and st2.get("deployments", d1.id) == d1, "store saves and reloads every kind unchanged")
with open(os.path.join(lib, "packages.json")) as _f:
    raw = json.load(_f)
raw[0]["field_from_a_newer_version"] = 1
with open(os.path.join(lib, "packages.json"), "w") as _f:
    json.dump(raw, _f)
check(Store(lib).get("packages", p.id) == p, "unknown fields in the JSON are ignored")
p.version = "8.7"
st.upsert("packages", p)
check(len(st.packages) == 1 and Store(lib).get("packages", p.id).version == "8.7", "upsert replaces by id")
check(st.client_of("pc-1") is c and st.client_of("PC-2") is c and st.client_of("other") is None,
      "client_of matches hostnames case-insensitively")
c2 = st.upsert("clients", Client(name="Branch", machines=["PC-1"]))
check([x.name for x in st.clients_of("pc-1")] == ["Contoso", "Branch"], "a PC can belong to two clients")
st.delete("clients", c.id)
check(st.get("deployments", d1.id) is None and st.get("deployments", d2.id), "deleting a client drops its deployments")
os.makedirs(os.path.join(lib, "files", p.id), exist_ok=True)
st.delete("packages", p.id)
check(not st.packages and st.get("deployments", d3.id) is None and not os.path.exists(os.path.join(lib, "files", p.id)),
      "deleting a package drops its deployments and files")
st.delete("tasks", t.id)
check(not st.deployments and not Store(lib).deployments, "deleting a task drops its deployments")
st.delete("clients", c2.id)

# --- 2. installer analysis ----------------------------------------------------------------
msi = os.path.join(W, "in", "contoso_notes.msi")
os.makedirs(os.path.dirname(msi))
PC, UC = "{11111111-2222-3333-4444-555555555555}", "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}"
build_msi(msi, {"ProductName": "Contoso Notes", "ProductVersion": "4.2.1", "Manufacturer": "Contoso Café",
                "ProductCode": PC, "UpgradeCode": UC, "ALLUSERS": "1"})
props = A.msi_properties(msi)
check(props.get("ProductName") == "Contoso Notes" and props.get("ProductCode") == PC and len(props) == 6,
      f"MSI Property table read ({len(props)} rows)")
check(props.get("Manufacturer") == "Contoso Café", "MSI strings decoded with the database codepage")
s = A.analyze(msi)
check(s["kind"] == "msi" and s["name"] == "Contoso Notes" and s["version"] == "4.2.1" and s["product_code"] == PC
      and s["upgrade_code"] == UC, "MSI analysed: name, version, product + upgrade code")
check("/qn" in s["install"] and "{installer}" in s["install"] and s["uninstall"].startswith("msiexec.exe /x {product_code}"),
      "MSI gets silent install/uninstall commands")

inno = os.path.join(W, "in", "notes-setup.exe")
fake_exe(inno, b"Inno Setup Setup Data (6.2.2)", {"CompanyName": "Contoso Ltd", "FileDescription": "Notes Setup",
                                                  "ProductName": "Contoso Notes", "FileVersion": "4, 3, 0, 12",
                                                  "ProductVersion": "4.3"})
s = A.analyze(inno)
check(s["kind"] == "exe" and s["name"] == "Contoso Notes" and s["publisher"] == "Contoso Ltd" and s["version"] == "4.3",
      "EXE version resource read (name, publisher, version)")
check("/VERYSILENT" in s["install"] and s["uninstall"].startswith("{registry_uninstall}")
      and "Inno Setup" in " ".join(s["notes"]), "Inno Setup recognised with its silent switches")
check(A.exe_info(inno)["FileVersion"] == "4, 3, 0, 12" and A.clean_version("4, 3, 0, 12") == "4.3.0.12"
      and A.clean_version("v2.1.0-beta") == "2.1.0", "old-style comma versions cleaned")
nsis = os.path.join(W, "in", "tool.exe")
fake_exe(nsis, b"Nullsoft.NSIS.exehead", {"FileVersion": "1.0.5"})
s = A.analyze(nsis)
check(s["install"] == '"{installer}" /S' and s["name"] == "tool" and s["version"] == "1.0.5" and "NSIS" in s["notes"][0],
      "NSIS recognised, name falls back to the file name")
unk = os.path.join(W, "in", "mystery.exe")
fake_exe(unk, b"", {})
s = A.analyze(unk)
check("guess" in s["notes"][0] and s["install"] == '"{installer}" /S', "unknown EXE gets a flagged /S guess")
check(not any(ch in n for n in s["notes"] + A.analyze(inno)["notes"] for ch in "\u2014\u2013"),
      "analysis notes are plain text")
mx = os.path.join(W, "in", "app.msix")
with open(mx, "wb") as _f:
    _f.write(b"PK\3\4")
check(A.analyze(mx)["kind"] == "msix", "MSIX recognised")

pk = st.add_from_installer(msi)
check(pk.kind == "msi" and pk.product_code == PC and os.path.exists(st.installer_path(pk)),
      "add_from_installer stores the package and copies the MSI")
msi2 = os.path.join(W, "in", "contoso_notes_5.msi")
build_msi(msi2, {"ProductName": "Contoso Notes", "ProductVersion": "5.0", "Manufacturer": "Contoso",
                 "ProductCode": "{99999999-2222-3333-4444-555555555555}", "UpgradeCode": UC.replace("A", "B")})
old_path = st.installer_path(pk)
st.replace_installer(pk, msi2)
pk = Store(lib).get("packages", pk.id)
check(pk.version == "5.0" and pk.product_code.startswith("{9999") and pk.upgrade_code.startswith("{BBBB")
      and not os.path.exists(old_path) and os.path.exists(st.installer_path(pk)),
      "replace_installer swaps the file and refreshes version + codes")

# --- 3. export ------------------------------------------------------------------------------


def ps_balanced(text):
    body = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    stripped = ""
    for ln in body.splitlines():
        if ln.count("'") % 2:
            return False
        stripped += "".join(part for i, part in enumerate(ln.split("'")) if i % 2 == 0) + "\n"
    return all(stripped.count(a) == stripped.count(b) for a, b in ("{}", "()", "[]")) and stripped.count('"') % 2 == 0


out = st.export_package(pk, os.path.join(W, "export"))
files = sorted(os.listdir(out))
check(files == ["Detect.ps1", "Install.ps1", "Uninstall.ps1", "contoso_notes_5.msi", "package.json"],
      f"export writes package.json, the installer and three scripts {files}")
ps = {}
for _n in files:
    if _n.endswith(".ps1"):
        with open(os.path.join(out, _n), encoding="utf-8-sig") as _f:
            ps[_n] = _f.read()
with open(os.path.join(out, "Install.ps1"), "rb") as _f:
    _head3 = _f.read()[:3]
with open(os.path.join(out, "Install.ps1"), newline="") as _f:
    _rawinst = _f.read()
check(_head3 == b"\xef\xbb\xbf" and "\r\n" in _rawinst, "scripts saved with BOM and CRLF for Windows PowerShell")
check("Join-Path $PSScriptRoot 'contoso_notes_5.msi'" in ps["Install.ps1"]
      and ".Replace('{installer}', $installer)" in ps["Install.ps1"]
      and "'msiexec.exe /i \"{installer}\" /qn /norestart ALLUSERS=1'" in ps["Install.ps1"],
      "Install.ps1 resolves the installer next to the script at run time")
check("@(0,3010,1641) -notcontains $p.ExitCode" in ps["Install.ps1"] and "/s /c" in ps["Install.ps1"],
      "Install.ps1 checks success codes and keeps the command's own quotes")
check(f"msiexec.exe /x {pk.product_code} /qn /norestart" in ps["Uninstall.ps1"], "Uninstall.ps1 uses the product code")
check("[WildcardPattern]::Escape('Contoso Notes')" in ps["Detect.ps1"], "Detect.ps1 looks up the product name")
check(all(ps_balanced(v) for v in ps.values()), "PowerShell quotes and brackets balance")
check(not any(t in v for v in ps.values() for t in ("{product_code}", "{version}", "{winget_id}", "{registry_uninstall}")),
      "no unexpanded placeholders left")
imp = st.import_package(out)
check(imp.id != pk.id and imp.product_code == pk.product_code and imp.install == pk.install
      and os.path.exists(st.installer_path(imp)), "export folder imports back as a new package")


def scripts(**kw):
    return core.render_scripts(Package(name="Thing", **kw))


ex = scripts(installer="setup.exe", install='"{installer}" /VERYSILENT', uninstall="{registry_uninstall} /VERYSILENT",
             detection={"method": "registry", "value": "re:^Thing( \\d+)?$"})
check("-match '^Thing( \\d+)?$'" in ex["Detect.ps1"] and "-like" not in ex["Detect.ps1"], "regex detection uses -match")
check("QuietUninstallString" in ex["Uninstall.ps1"] and "' ' + '/VERYSILENT'" in ex["Uninstall.ps1"]
      and "$Matches[2]" in ex["Uninstall.ps1"], "registry uninstall: quiet string, EXE switches, MSI product code")
fl = scripts(detection={"method": "file", "value": r"%ProgramFiles%\Thing\thing.exe"}, install="x")
check("Test-Path -LiteralPath $f" in fl["Detect.ps1"] and "ExpandEnvironmentVariables('%ProgramFiles%\\Thing\\thing.exe')"
      in fl["Detect.ps1"] and "DisplayName" not in fl["Detect.ps1"], "file detection checks the file")
sh = scripts(language="shell", detection={"method": "script", "value": "[ -f /opt/thing ] && echo 1.0\r\n"})
check(sh.get("Detect.sh") == "[ -f /opt/thing ] && echo 1.0\n" and "Detect.sh" in sh["Detect.ps1"]
      and "[ -f" not in sh["Detect.ps1"], "shell detection goes to Detect.sh, not into Detect.ps1")
pc = scripts(product_code="{ABC}", detection={"method": "registry", "value": ""}, install="x", uninstall="")
check("Uninstall\\{ABC}" in pc["Detect.ps1"] and "No uninstall command" in pc["Uninstall.ps1"],
      "product-code detection; missing uninstall command reported")
check(all(ps_balanced(v) for d in (ex, fl, sh, pc) for k, v in d.items() if k.endswith(".ps1")),
      "all generated PowerShell balances")
check(not any(ch in v for d in (ex, fl, sh, pc, ps) for v in d.values() for ch in "\u2014\u2013"),
      "generated scripts have no typographic dashes")

# --- 4. sessions on this PC ---------------------------------------------------------------
class WinCodes(LocalEndpoint):
    """Linux exit codes are 8 bits, so 1603 and 3010 come back as 67 and 194. Map them back."""

    def run_command(self, cmd, timeout=3600, cwd=None, env=None):
        r = super().run_command(cmd, timeout, cwd, env)
        r["code"] = {1603 & 255: 1603, 3010 & 255: 3010, 1618 & 255: 1618}.get(r["code"], r["code"])
        return r


store = Store(os.path.join(W, "lib2"))
host = LocalEndpoint().info()["hostname"]
set_inventory(entry("Foo App", "1.0"), entry("Bar Tool", "3.0"), entry("Old Junk", "1.0"), entry("Pinned", "3.0"),
              entry("Pin Mismatch", "3.0"), entry("Machine Wins", "1.0"), entry("No Downgrade", "3.0"),
              entry("Ignored App", "1.0"))
foo = installer_pkg(store, "Foo App", "2.0")                          # 1.0 installed -> upgrade
bar = installer_pkg(store, "Bar Tool", "3.0")                         # up to date -> nothing
runtime = installer_pkg(store, "Runtime", "1.0")                      # only a prerequisite
newt = installer_pkg(store, "New Thing", "1.5", prerequisites=[runtime.id])
junk = installer_pkg(store, "Old Junk", "1.0")                        # remove via its registry uninstaller
broken = installer_pkg(store, "Broken", "1.0", " --noop 1 --exit 1603")
liar = installer_pkg(store, "Liar", "1.0", " --noop 1")               # exit 0 but never registers
reboot = installer_pkg(store, "Needs Reboot", "1.0", " --exit 3010")
pinned = installer_pkg(store, "Pinned", "2.0")                        # 3.0 installed, pinned to 2.0 -> roll back
nodown = installer_pkg(store, "No Downgrade", "2.0")                  # same, but downgrades not allowed -> refused
ignored = installer_pkg(store, "Ignored App", "2.0")                  # ignore: never checked or changed
mismatch = installer_pkg(store, "Pin Mismatch", "2.0")                # pinned to 1.0 the library doesn't have
mwins = installer_pkg(store, "Machine Wins", "1.0")
onb = installer_pkg(store, "Onboard Only", "1.0")
off = installer_pkg(store, "Disabled", "1.0")
other = installer_pkg(store, "Other PC Only", "1.0")
mark = os.path.join(W, "scriptpkg.txt")
spkg = store.upsert("packages", Package(name="Script Pkg", kind="script", version="4.2", language="shell",
                                        install=f'echo 4.2 > "{mark}"',
                                        detection={"method": "script", "value": f'[ -f "{mark}" ] && cat "{mark}"'}))
vfile = os.path.join(W, "filepkg", "version.txt")
fpkg = store.upsert("packages", Package(name="File Pkg", kind="script", version="1.3",
                                        install=f'mkdir -p "{os.path.dirname(vfile)}" && echo 1.3 > "{vfile}"',
                                        detection={"method": "file", "value": vfile}))
flag_a, flag_b = os.path.join(W, "flag_a"), os.path.join(W, "flag_b")
t_fix = store.upsert("tasks", Task(name="Flag file", test='[ -f "$flag" ]', set='touch "$flag"', language="shell",
                                   params={"flag": flag_a}))
t_audit = store.upsert("tasks", Task(name="Audit only", test="echo nope; exit 1", set=f'touch "{W}/must_not_exist"',
                                     language="shell"))
t_stuck = store.upsert("tasks", Task(name="Can't fix", test="exit 1", set="true", language="shell"))
t_ps = store.upsert("tasks", Task(name="PowerShell task", test="exit 0", set="exit 0"))
cl = store.upsert("clients", Client(name="Contoso", machines=[host.upper()]))
for pkg in (foo, bar, newt, broken, liar, reboot, fpkg):
    deploy(store, pkg, "latest" if pkg is foo else "installed")
deploy(store, junk, "uninstalled")
deploy(store, pinned, "version", version="2.0", allow_downgrade=True)
deploy(store, nodown, "version", version="2.0")
deploy(store, ignored, "latest")
deploy(store, ignored, "ignore", target_kind="machine", target_value=host)
deploy(store, mismatch, "version", version="1.0", allow_downgrade=True)
deploy(store, mwins, "installed")
deploy(store, mwins, "uninstalled", target_kind="machine", target_value=host.lower())
deploy(store, onb, "installed", onboarding_only=True)
deploy(store, off, "installed", enabled=False)
deploy(store, other, "installed", target_kind="machine", target_value="SOME-OTHER-PC")
deploy(store, spkg, "installed", target_kind="client", target_value=cl.id)
deploy(store, t_fix, "enforce", params={"flag": flag_b})
deploy(store, t_audit, "audit")
deploy(store, t_stuck, "enforce")
deploy(store, t_ps, "ignore")

order = [d.item_id for d in core.applicable(store, host, False)]
check(order.index(runtime.id) < order.index(newt.id), "prerequisite comes before the package that needs it")
check(onb.id not in order and off.id not in order and other.id not in order and t_ps.id in order,
      "onboarding-only, disabled and other-PC deployments left out")
check(onb.id in [d.item_id for d in core.applicable(store, host, True)], "onboarding run includes onboarding-only")
mw = [d for d in core.applicable(store, host, False) if d.item_id == mwins.id]
check(len(mw) == 1 and mw[0].desired == "uninstalled", "machine deployment beats the all-PCs one")
loop_a = store.upsert("packages", Package(name="Loop A"))
loop_b = store.upsert("packages", Package(name="Loop B", prerequisites=[loop_a.id]))
loop_a.prerequisites = [loop_b.id]
la = deploy(store, loop_a)
got = [d.item_id for d in core.applicable(store, host, False)]
check(got.count(loop_a.id) == 1 and got.count(loop_b.id) == 1, "prerequisite loop doesn't repeat packages")
store.delete("packages", loop_a.id)
store.delete("packages", loop_b.id)
gone = deploy(store, newt, "uninstalled", target_kind="machine", target_value=host)
check(runtime.id not in [d.item_id for d in core.applicable(store, host, False)],
      "removing a package doesn't install its prerequisites")
store.delete("deployments", gone.id)

with open(FAKE) as _f:
    before = _f.read()
s1 = core.run_session(WinCodes(), store, Progress(1), mode="detect")
by = {a["name"]: a for a in s1["actions"]}
with open(FAKE) as _f:
    _after = _f.read()
check(_after == before and not os.path.exists(mark) and not os.path.exists(flag_b),
      "detect mode changes nothing")
check({n: by[n]["action"] for n in ("Foo App", "Bar Tool", "New Thing", "Runtime", "Old Junk", "Pinned",
                                    "Pin Mismatch", "Machine Wins", "Script Pkg", "File Pkg", "Flag file",
                                    "Audit only")} ==
      {"Foo App": "upgrade", "Bar Tool": "none", "New Thing": "install", "Runtime": "install",
       "Old Junk": "uninstall", "Pinned": "reinstall", "Pin Mismatch": "audit", "Machine Wins": "uninstall",
       "Script Pkg": "install", "File Pkg": "install", "Flag file": "set", "Audit only": "audit"},
      "detection plans the right action for every deployment")
check(by["No Downgrade"]["action"] == "refused" and by["No Downgrade"]["status"] == "non-compliant"
      and "newer" in by["No Downgrade"]["result"], "a pin older than the installed version is refused, not run")
check(by["Ignored App"]["action"] == "none" and by["Ignored App"]["status"] == "ignored"
      and by["Ignored App"]["current"] == "not checked" and by["PowerShell task"]["status"] == "ignored"
      and s1["summary"]["ignored"] == 2, "ignore on this PC beats the wider deployment and is shown as ignored")
check(by["Foo App"]["status"] == "pending" and by["Bar Tool"]["status"] == "compliant"
      and by["Audit only"]["status"] == "non-compliant", "detect statuses: pending / compliant / non-compliant")
R = core.resolve
check([R("latest", False, None, "2"), R("latest", True, "1.0", "2.0"), R("latest", True, "2.0", "2.0"),
       R("latest", True, "3.0", "2.0"), R("version", True, "3.0", "2.0"), R("version", True, "3.0", "2.0", True),
       R("version", True, "1.0", "2.0"), R("installed", True, "0.1", "2.0"), R("installed", False, None, ""),
       R("uninstalled", True, "1", ""), R("uninstalled", False, None, ""), R("ignore", False, None, "")] ==
      ["install", "upgrade", "none", "none", "refused", "reinstall", "upgrade", "none", "install", "uninstall",
       "none", "none"], "desired states resolve to install / upgrade / downgrade refused / uninstall / nothing")
check("1.0" in by["Pin Mismatch"].get("result", ""), "pin to a version the library lacks is reported, not run")
check(s1["client"] == "Contoso" and s1["summary"]["pending"] >= 8, f"session summary {s1['summary']}")

s2 = core.run_session(WinCodes(), store, Progress(1), mode="full")
by = {a["name"]: a for a in s2["actions"]}
inv = inventory()
check(inv.get("Foo App") == "2.0" and by["Foo App"]["status"] == "compliant" and by["Foo App"]["result"] == "now 2.0",
      "upgrade installs and re-check confirms the new version")
check(inv.get("Runtime") == "1.0" and inv.get("New Thing") == "1.5", "prerequisite and package installed")
check("Old Junk" not in inv and by["Old Junk"]["result"] == "removed", "uninstall via the registry uninstall string")
check(inv.get("Pinned") == "2.0" and by["Pinned"]["status"] == "compliant", "pinned version rolled back (remove + install)")
check(inv.get("Pin Mismatch") == "3.0" and by["Pin Mismatch"]["status"] == "non-compliant", "unavailable pin left alone")
check(inv.get("No Downgrade") == "3.0" and inv.get("Ignored App") == "1.0", "refused downgrade and ignored app untouched")
check("Machine Wins" not in inv, "machine-specific removal applied")
check(by["Broken"]["status"] == "failed" and "1603" in by["Broken"]["result"], "installer error code reported")
check(by["Liar"]["status"] == "failed" and "still missing" in by["Liar"]["result"],
      "installer that claims success but installs nothing is caught by the re-check")
check(by["Needs Reboot"]["status"] == "compliant" and by["Needs Reboot"].get("reboot") and s2["summary"]["reboot"],
      "exit 3010 counts as success and flags a reboot")
check(by["Script Pkg"]["status"] == "compliant" and by["Script Pkg"]["result"] == "now 4.2", "script detection (sh)")
check(by["File Pkg"]["status"] == "compliant" and by["File Pkg"]["result"] == "now 1.3", "file detection")
check(os.path.exists(flag_b) and not os.path.exists(flag_a) and by["Flag file"]["result"] == "fixed",
      "task fixed with the deployment's params overriding the task's")
check(by["Audit only"]["status"] == "non-compliant" and not os.path.exists(os.path.join(W, "must_not_exist")),
      "audit task never runs its set script")
check(by["Can't fix"]["status"] == "failed" and "[set] exit 0" in by["Can't fix"]["log"], "task still failing after set")
check(not os.listdir(LocalEndpoint().deploy_dir()), "uploaded installers cleaned up after install")
check(all(a.get("entry") is None for a in s2["actions"] + s2["detected"]), "session JSON leaves out registry entries")

s3 = core.run_session(WinCodes(), store, Progress(1), mode="full")
again = {a["name"]: a["action"] for a in s3["detected"]}
fixed = [n for n, a in by.items() if a["status"] == "compliant"]
check(all(again[n] == "none" for n in fixed), "second run finds everything that was fixed still fine")
check(set(n for n, a in again.items() if a not in core.IDLE) == {"Broken", "Liar", "Can't fix"},
      "second run only retries what failed")
hist = store.sessions()
check([h["id"] for h in hist] == [s3["id"], s2["id"], s1["id"]], "sessions saved, newest first")

# the package or task disappears between check and apply
gone_pkg = {"type": "software", "item": "nope", "action": "install", "desired": "installed", "deployment": "x",
            "name": "Gone"}
r = core.execute(LocalEndpoint(), store, gone_pkg)
check(r["status"] == "failed" and "deleted" in r["result"], "deleted package gives a clear failure")
r = core.execute(LocalEndpoint(), store, dict(gone_pkg, type="task", action="set"))
check(r["status"] == "failed" and "deleted" in r["result"], "deleted task gives a clear failure")
noreg = store.upsert("packages", Package(name="No Entry", uninstall="{registry_uninstall} /S",
                                         detection={"method": "file", "value": vfile}))
r = core.execute(LocalEndpoint(), store, {"type": "software", "item": noreg.id, "action": "uninstall",
                                          "desired": "uninstalled", "entry": None, "deployment": "x", "name": "No Entry"})
check(r["status"] == "failed" and "Add/Remove Programs" in r["result"], "registry uninstall without an entry refused")
check(core._registry_uninstall({"uninstall": "MsiExec.exe /I{12345678-AAAA-BBBB-CCCC-1234567890AB}"})
      == "msiexec.exe /x {12345678-AAAA-BBBB-CCCC-1234567890AB} /qn /norestart", "MSI repair string turned into /x")
ep = LocalEndpoint()
check(ep.run_script("echo $x", language="shell", params={"x": "a'b c"})["out"].strip() == "a'b c",
      "shell params are quoted safely")
r = ep.run_script("exit 0", language="cmd")
check(r["code"] == 127 and "Windows" in r["err"], "cmd scripts refused off Windows with a clear message")
r = ep.run_script("exit 0", language="cobol")
check(r["code"] == 127 and "Unknown" in r["err"], "unknown script language refused")

# msiexec 1618 (another installation in progress) is waited out and retried
busy_n = os.path.join(W, "busy_count")
busy = store.upsert("packages", Package(
    name="Busy App", kind="script", version="1.0",
    install=f'"{PY}" -c "import os,sys; p=r\'{busy_n}\'; n=os.path.exists(p); open(p,\'a\').write(\'x\'); '
            f'sys.exit(0 if n else 1618)" && "{PY}" "{FAKEINST}" add "Busy App" 1.0',
    detection={"method": "registry", "value": "Busy App"}))
core.BUSY_WAIT = 0
r = core.execute(WinCodes(), store, {"type": "software", "item": busy.id, "action": "install", "desired": "installed",
                                     "deployment": "x", "name": "Busy App"})
core.BUSY_WAIT = 20
with open(busy_n) as _f:
    _tries = _f.read()
check(r["status"] == "compliant" and r["log"].count("exit 1618") == 1 and _tries == "xx",
      f"installer exit 1618 (another install running) waited out and retried ({r['status']}, {r.get('result')})")

# installed_software reads the 64-bit registry view, so a 32-bit process still sees 64-bit apps
import types  # noqa: E402

_UN = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall"
_REG = {_UN + "\\{A64}": {"DisplayName": "App64", "DisplayVersion": "6.4"},
        _UN.replace("SOFTWARE", "SOFTWARE\\WOW6432Node") + "\\{B32}": {"DisplayName": "App32",
                                                                       "DisplayVersion": "3.2"}}


class _Key:
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _open(key, sub, reserved=0, access=0x20019):
    if isinstance(key, _Key):
        path = f"{key.path}\\{sub}"
    elif key == "HKU":
        return _Key("HKU")
    else:  # what WOW64 does for a 32-bit process: HKLM\SOFTWARE goes to WOW6432Node unless the 64-bit view is asked for
        path = sub
        if not access & 0x100 and path.upper().startswith("SOFTWARE\\") \
                and not path.upper().startswith("SOFTWARE\\WOW6432NODE"):
            path = "SOFTWARE\\WOW6432Node\\" + path[9:]
    if not any(k.lower().startswith(path.lower()) for k in _REG):
        raise OSError(2, "not found")
    return _Key(path)


def _enum(k, i):
    kids = sorted({x[len(k.path) + 1:].split("\\")[0] for x in _REG if x.lower().startswith(k.path.lower() + "\\")})
    if i >= len(kids):
        raise OSError(259, "no more items")
    return kids[i]


def _query(k, name):
    try:
        return _REG[k.path][name], 1
    except KeyError:
        raise OSError(2, "no value") from None


_fake = types.ModuleType("winreg")
_fake.__dict__.update(HKEY_LOCAL_MACHINE="HKLM", HKEY_USERS="HKU", KEY_READ=0x20019, KEY_WOW64_64KEY=0x100,
                      OpenKey=_open, EnumKey=_enum, QueryValueEx=_query)
_saved = sys.platform, sys.modules.get("winreg"), os.environ.pop("SECTORSMITH_FAKE_SOFTWARE")
sys.modules["winreg"], sys.platform = _fake, "win32"
try:
    _got = LocalEndpoint().installed_software()
finally:
    sys.platform, os.environ["SECTORSMITH_FAKE_SOFTWARE"] = _saved[0], _saved[2]
    if _saved[1] is None:
        sys.modules.pop("winreg", None)
    else:
        sys.modules["winreg"] = _saved[1]
check(sorted((e["name"], e["scope"]) for e in _got) == [("App32", "machine32"), ("App64", "machine")],
      f"Add/Remove Programs read in the 64-bit view, each app once {[(e['name'], e['scope']) for e in _got]}")

# --- 5. a session on a linked PC -------------------------------------------------------------
from sectorsmith.link.server import LinkServer  # noqa: E402

remote_fake = os.path.join(W, "remote_installed.json")
with open(remote_fake, "w") as _f:
    json.dump([entry("Foo App", "1.0")], _f)
connected = []
srv = LinkServer(on_connect=connected.append, port=47430)
srv.start()
env = dict(os.environ, SECTORSMITH_FAKE_SOFTWARE=remote_fake, HOME=os.path.join(W, "remote_home"))
ag = subprocess.Popen([PY, "-m", "sectorsmith", "--agent", "--headless", "--no-elevate", "--connect",
                       f"127.0.0.1:{srv.port}", "--token", srv.token, "--pin", srv.fingerprint], cwd=ROOT, env=env,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
end = time.time() + 30
while not connected and time.time() < end:
    time.sleep(.2)
check(connected, "agent linked")
remote = connected[0]
rstore = Store(os.path.join(W, "lib3"))
big = os.path.join(W, "src", "big_suite.bin")
with open(big, "wb") as _f:
    _f.write(os.urandom(core.UPLOAD_CHUNK * 2 + 12345))
with open(big, "rb") as _f:
    sha = hashlib.sha256(_f.read()).hexdigest()
bigpkg = Package(name="Big Suite", version="1.0", installer="big_suite.bin",
                 install=f'"{PY}" "{FAKEINST}" add "Big Suite" 1.0 --sha {sha} --file "{{installer}}"',
                 detection={"method": "registry", "value": "Big Suite"})
os.makedirs(os.path.join(rstore.root, "files", bigpkg.id))
shutil.copy(big, rstore.installer_path(bigpkg))
rstore.upsert("packages", bigpkg)
deploy(rstore, bigpkg)
deploy(rstore, installer_pkg(rstore, "Foo App", "2.0"), "latest")
rflag = os.path.join(W, "remote_flag")
deploy(rstore, rstore.upsert("tasks", Task(name="Remote flag", test='[ -f "$f" ]', set='touch "$f"',
                                           language="shell", params={"f": rflag})), "enforce")
rs = core.run_session(remote, rstore, Progress(1), mode="full")
by = {a["name"]: a for a in rs["actions"]}
check(by["Big Suite"]["status"] == "compliant", f"{os.path.getsize(big) >> 20} MiB installer uploaded in chunks "
      f"byte-exact and installed on the linked PC ({by['Big Suite'].get('result')})")
check(by["Foo App"]["result"] == "now 2.0" and inventory_of(remote_fake).get("Foo App") == "2.0",
      "upgrade ran on the linked PC")
check(by["Remote flag"]["status"] == "compliant" and os.path.exists(rflag), "task with params ran on the linked PC")
check(rs["machine"] == remote.label and rs["summary"]["failed"] == 0, f"linked session summary {rs['summary']}")
check(not os.listdir(remote.deploy_dir()), "installers removed from the linked PC afterwards")
check("Big Suite" not in inventory(), "this PC's own software list untouched by the remote session")

# --- 6. Cancel a maintenance run on a linked PC while an installer hangs --------------------------
import threading  # noqa: E402
from sectorsmith.util import Cancelled, cancel_scope  # noqa: E402

cstore = Store(os.path.join(W, "lib4"))
slow = cstore.upsert("packages", Package(name="Slow App", version="1.0", kind="script", language="shell",
                                         install=f'sleep 60; "{PY}" "{FAKEINST}" add "Slow App" 1.0',
                                         detection={"method": "registry", "value": "Slow App"}))
deploy(cstore, slow)
deploy(cstore, installer_pkg(cstore, "Never Run", "1.0"))
prog = Progress(1)
out = {}


def _session():
    try:
        with cancel_scope(prog.check):
            out["r"] = core.run_session(remote, cstore, prog, mode="full")
    except Exception as e:  # noqa: BLE001
        out["r"] = e
    out["t"] = time.time()


th = threading.Thread(target=_session, daemon=True)
th.start()
time.sleep(2)
pressed = time.time()
prog.cancel()
th.join(30)
r = out.get("r")
dt = out.get("t", 999) - pressed
check(isinstance(r, Cancelled) and dt < 3, f"Cancel stops a hanging installer on the linked PC in {dt:.2f}s")
sess = getattr(r, "info", {}).get("session") or {}
st = {a["name"]: a for a in sess.get("actions", [])}
check(st.get("Slow App", {}).get("status") == "cancelled" and "part-way" in st["Slow App"]["result"]
      and st.get("Never Run", {}).get("status") == "cancelled" and "not run" in st["Never Run"]["result"],
      "the cancelled session says what was stopped part-way and what never ran")
check(sess.get("cancelled") and os.path.exists(sess.get("path", "")) and sess["summary"]["cancelled"] == 2,
      "the cancelled session is saved for the Sessions tab")
check("Slow App" not in inventory_of(remote_fake) and remote.ping() == "pong",
      "nothing half-registered, and the link is still up")
check(not os.listdir(remote.deploy_dir()), "no installer left behind on the linked PC after Cancel")

# --- 7. Apps come across in a user migration -----------------------------------------------------
from sectorsmith.link import apps as mapps, migrate  # noqa: E402

set_inventory(entry("Bar Tool", "1.2"), entry("Mozilla Firefox (x64 en-GB)", "120.0"), entry("Acme Payroll", "3"),
              entry("Microsoft Visual C++ 2019 X64 Minimum Runtime", "14.2"), entry("Already There", "1.0"),
              dict(entry("Hidden Part", "1"), system_component=True))
with open(remote_fake) as _f:
    _rinv = json.load(_f)
with open(remote_fake, "w") as _f:
    json.dump(_rinv + [entry("Already There", "1.0")], _f)
astore = Store(os.path.join(W, "lib5"))
installer_pkg(astore, "Bar Tool", "1.5")
src_inv = mapps.clean_inventory(ep.installed_software())
check([e["name"] for e in src_inv] == ["Acme Payroll", "Already There", "Bar Tool", "Mozilla Firefox (x64 en-GB)"],
      f"old PC's apps listed without runtimes or system parts {[e['name'] for e in src_inv]}")
rows = mapps.match(src_inv, astore, remote.installed_software())
by = {r_["name"]: r_ for r_ in rows}
check(by["Bar Tool"]["source"] == "library" and by["Mozilla Firefox (x64 en-GB)"]["winget_id"] == "Mozilla.Firefox"
      and by["Acme Payroll"]["source"] == "manual" and by["Already There"]["source"] == "installed",
      "apps matched: Deploy library, winget, by hand, already there")
prof = os.path.join(W, "mig_src", "erin")
os.makedirs(os.path.join(prof, "Desktop"), exist_ok=True)
with open(os.path.join(prof, "Desktop", "hello.txt"), "w") as _f:
    _f.write("hi")
mdst = os.path.join(W, "mig_dst", "erin")
desk = [i for i in migrate.ITEMS if i.key == "desktop"]
res = migrate.run(migrate.Plan(ep, prof, remote, mdst, desk, apps=rows, store=astore), Progress(1))
got = {a["name"]: a for a in res["apps"]}
check(got.get("Bar Tool", {}).get("status") == "compliant" and inventory_of(remote_fake).get("Bar Tool") == "1.5",
      f"library app installed on the new PC by the Deploy engine during the move {got.get('Bar Tool')}")
check(got.get("Mozilla Firefox", {}).get("status") == "failed",
      "winget app tried (no winget here, so it fails and says so)")
check(res["manual_apps"] == ["Acme Payroll"] and os.path.exists(os.path.join(mdst, "Desktop", "hello.txt")),
      "files still copied, and apps to install by hand listed")
with open(res["report"]) as fh:
    rep = fh.read()
check("Acme Payroll" in rep and "Bar Tool" in rep, "migration report lists the apps")
remote.close()
ag.wait(30)
check(ag.returncode == 0, "agent exits cleanly")
srv.stop()

# --- 8. Bundles, client baselines and onboarding a new PC -------------------------------------------
from sectorsmith.deploy import catalogue as cat, schedule as sch  # noqa: E402

set_inventory(entry("Have Already", "1.0"))
bstore = Store(os.path.join(W, "lib6"))
ba = installer_pkg(bstore, "Base A", "1.0")
bb = installer_pkg(bstore, "Base B", "2.0")
bc = installer_pkg(bstore, "Have Already", "1.0")
outside = installer_pkg(bstore, "Not In Baseline", "1.0")
deploy(bstore, outside)  # every PC, but not part of an onboarding run
newco = bstore.upsert("clients", Client(name="NewCo"))
check(bstore.baseline_for(newco.id) is None, "a new client has no baseline")
base = bstore.set_baseline(newco.id, [ba.id, bb.id, bc.id, "no-such-package"])
check(base.item_type == "bundle" and base.items == [ba.id, bb.id, bc.id] and base.onboarding_only and base.baseline
      and Store(bstore.root).baseline_for(newco.id).items == base.items,
      "baseline saved as an onboarding-only bundle for the client (unknown ids dropped)")
check(bstore.set_baseline(newco.id, [ba.id, bb.id, bc.id], "latest").id == base.id and len(bstore.deployments) == 2
      and bstore.baseline_for(newco.id).desired == "latest", "saving the baseline again updates it in place")
check(not core.applicable(bstore, host, True, only=[base.id]), "a baseline doesn't apply to PCs outside the client")
check(len(bstore.used_by(ba.id)) == 1 and len(bstore.used_by(outside.id)) == 1,
      "used_by counts bundles as well as single deployments")
try:
    core.onboard(WinCodes(), bstore, bstore.upsert("clients", Client(name="Empty")).id, Progress(1))
    no_base = False
except ValueError as e:
    no_base = "no baseline" in str(e)
check(no_base, "onboarding a client without a baseline is refused with a clear message")
ob = core.onboard(WinCodes(), bstore, newco.id, Progress(1))
byo = {a["name"]: a for a in ob["actions"]}
inv = inventory()
check(set(byo) == {"Base A", "Base B", "Have Already"} and byo["Base A"]["status"] == "compliant"
      and byo["Have Already"]["action"] == "none" and inv.get("Base B") == "2.0",
      f"onboarding installs the baseline ({sorted(byo)})")
check("Not In Baseline" not in inv, "onboarding runs only the baseline, not every deployment")
check(host.lower() in [m.lower() for m in bstore.get("clients", newco.id).machines] and ob["trigger"] == "onboarding"
      and ob["onboarding"], "the new PC joins the client and the session is marked as onboarding")
check(all(core.source_id(a["deployment"]) == base.id for a in ob["actions"]), "baseline actions point at the baseline")
res = core.deployment_results(bstore.sessions())
check(res[base.id]["compliant"] == 3 and res[base.id]["failed"] == 0 and res[base.id]["machine"] == host,
      "latest result per deployment read back from sessions")
oc = bstore.upsert("clients", Client(name="OtherCo"))
bstore.set_baseline(oc.id, [ba.id])
try:
    core.onboard(WinCodes(), bstore, oc.id, Progress(1))
    refused = False
except ValueError as e:
    refused = "NewCo" in str(e)
check(refused, "onboarding refuses a PC that's already in another client")
bstore.delete("packages", bb.id)
check(bstore.baseline_for(newco.id).items == [ba.id, bc.id], "deleting a package takes it out of baselines")
check(bstore.set_baseline(oc.id, []) is None and bstore.baseline_for(oc.id) is None, "an empty baseline is removed")
mach = bstore.upsert("deployments", Deployment("software", ba.id, "uninstalled", target_kind="machine",
                                               target_value=host))
check([d.desired for d in core.applicable(bstore, host, True, only=[bstore.baseline_for(newco.id).id])
       if d.item_id == ba.id] == ["uninstalled"], "a PC's own deployment still wins over its client's baseline")
bstore.delete("deployments", mach.id)

# --- 9. Schedules ------------------------------------------------------------------------------------
noon = time.mktime((2026, 10, 7, 12, 0, 0, 0, 0, -1))  # a Wednesday, local time
daily = Deployment("software", ba.id, schedule=sch.make("daily", "9:00", now=noon - 2 * 86400))
check(daily.schedule["time"] == "09:00" and sch.due_for(daily, "PC-1", noon, "start"),
      "daily schedule is due once a 09:00 has passed since it was set")
daily.last_runs = {"pc-1": noon - 3600}
check(not sch.due_for(daily, "PC-1", noon, "start") and sch.due_for(daily, "PC-2", noon, "start"),
      "not due again on a PC that ran today, still due on the others")
check(sch.due_for(daily, "pc-1", noon + 86400, "connect"), "due again the next day (at start or on connect)")
weekly = Deployment("software", ba.id, schedule=sch.make("weekly", "08:30", day=4, now=noon))
nxt = time.localtime(sch.next_due(weekly))
check((nxt.tm_wday, nxt.tm_hour, nxt.tm_min) == (4, 8, 30) and 0 < sch.next_due(weekly) - noon < 7 * 86400
      and not sch.due_for(weekly, "PC-1", noon, "start"), "weekly schedule: next Friday 08:30, not due yet")
conn = Deployment("software", ba.id, schedule=sch.make("connect"))
check(sch.due_for(conn, "x", noon, "connect") and not sch.due_for(conn, "x", noon, "start"),
      "on-connect schedule runs only when a PC connects")
check(sch.describe(weekly) == "Fridays at 08:30" and sch.describe(conn) == "When a PC connects"
      and sch.describe(Deployment("task", "t")) == "" and sch.next_text(conn) == "Next connect",
      "schedules described in words")
off = Deployment("software", ba.id, enabled=False, schedule=sch.make("daily", now=noon - 2 * 86400))
check(not sch.due_for(off, "PC-1", noon, "start") and sch.make("") == {}, "switched-off and empty schedules never run")
for bad in ("25:00", "9:75", "noon"):
    try:
        sch.parse_time(bad)
        ok_bad = False
    except ValueError:
        ok_bad = True
    check(ok_bad, f"bad time {bad!r} refused")
set_inventory()
sstore = Store(os.path.join(W, "lib7"))
s_all = installer_pkg(sstore, "Sched All", "1.0")
s_me = installer_pkg(sstore, "Sched Me", "1.0")
s_conn = installer_pkg(sstore, "Sched Conn", "1.0")
s_chk = installer_pkg(sstore, "Sched Check", "1.0")
d_all = deploy(sstore, s_all, schedule=sch.make("daily", now=noon - 2 * 86400))
d_me = deploy(sstore, s_me, target_kind="machine", target_value=host, schedule=sch.make("daily", now=noon - 2 * 86400))
d_conn = deploy(sstore, s_conn, schedule=sch.make("connect"))
d_chk = deploy(sstore, s_chk, target_kind="machine", target_value=host,
               schedule=sch.make("daily", mode="detect", now=noon - 2 * 86400))
deploy(sstore, installer_pkg(sstore, "Unscheduled", "1.0"))
due = sch.due_runs(sstore, [host, "REMOTE-1"], "start", now=noon, local_host=host)
check(sorted(due.get(host, [])) == sorted([d_me.id, d_chk.id]) and due.get("REMOTE-1") == [d_all.id],
      f"due at start: Every PC schedules skip the technician's own PC ({due})")
due = sch.due_runs(sstore, ["REMOTE-1"], "connect", now=noon, local_host=host)
check(sorted(due["REMOTE-1"]) == sorted([d_all.id, d_conn.id]), "due when a linked PC connects")
runs = sch.run_due(WinCodes(), sstore, [d_me.id, d_chk.id], Progress(1))
inv = inventory()
check(len(runs) == 2 and all(r["trigger"] == "schedule" for r in runs) and inv.get("Sched Me") == "1.0"
      and "Sched Check" not in inv and "Unscheduled" not in inv and "Sched All" not in inv,
      "scheduled run changes only its own deployments; a check-only schedule changes nothing")
check({a["name"]: a["status"] for r in runs for a in r["actions"]} == {"Sched Me": "compliant",
                                                                        "Sched Check": "pending"},
      "check-only schedule reports what it would change")
sch.mark_ran(sstore, [d_me.id, d_chk.id], host.upper(), when=noon)
again = Store(sstore.root)
check(again.get("deployments", d_me.id).last_runs == {host.lower(): noon}
      and not sch.due_runs(again, [host], "start", now=noon + 60, local_host=host),
      "last run saved per PC, so it isn't due again until the next slot")

# --- 10. Package catalogue and winget search -----------------------------------------------------------
import re  # noqa: E402
import threading  # noqa: E402,F811
from sectorsmith.util import check_cancel  # noqa: E402

apps = cat.load()
wids = [a["winget"] for a in apps if a.get("winget")]
check(len(apps) >= 60 and all(a.get("category") and a.get("publisher") and a.get("detect") and
                              (a.get("winget") or a.get("placeholder")) for a in apps),
      f"curated catalogue has {len(apps)} apps, each with a category, publisher, detection and winget ID "
      "(or marked as needing your own installer)")
check(len(set(wids)) == len(wids) and all(cat.WINGET_ID.match(w) for w in wids), "winget IDs unique and well formed")
bad_rx = [a["name"] for a in apps if (a["detect"].get("value") or "").startswith("re:")
          and not re.compile(a["detect"]["value"][3:])]
check(not bad_rx, "every regular expression detection compiles")
for want in ("Google Chrome", "Mozilla Firefox", "7-Zip", "Adobe Acrobat Reader", "Office Deployment Tool",
             "Microsoft Teams", "Zoom Workplace", "VLC media player", "Notepad++", "PuTTY", "WinSCP", "Greenshot",
             "RMM agent", "Antivirus or EDR agent"):
    check(any(a["name"] == want for a in apps), f"catalogue has {want}")
check(cat.search(apps, "7zip")[0]["name"] == "7-Zip" and cat.search(apps, "mozilla firefox")[0]["name"] ==
      "Mozilla Firefox" and all(a["category"] == "Browsers" for a in cat.search(apps, "", "Browsers")),
      "catalogue search by words, IDs and category")
chrome = next(a for a in apps if a["name"] == "Google Chrome")
p = cat.to_package(chrome)
scripts_ = core.render_scripts(p)
check(p.kind == "winget" and p.winget_id == "Google.Chrome" and p.uninstall and p.version == ""
      and p.detection == {"method": "registry", "value": "Google Chrome"} and p.category == "Browsers"
      and "winget install --id Google.Chrome -e --silent" in scripts_["Install.ps1"]
      and "Google Chrome" in scripts_["Detect.ps1"], "catalogue app becomes a silent winget package with detection")
pc = cat.to_package(chrome, "choco")
check(pc.install == "choco install googlechrome -y --no-progress" and "choco uninstall" in pc.uninstall,
      "Chocolatey variant when the app has a Chocolatey ID")
for a, src in ((next(a for a in apps if a.get("placeholder")), "winget"),
               (next(a for a in apps if a.get("winget") and not a.get("choco")), "choco")):
    try:
        cat.to_package(a, src)
        refused = False
    except ValueError:
        refused = True
    check(refused, f"{a['name']} via {src} refused with a reason")
cstore7 = Store(os.path.join(W, "lib8"))
first = cat.add(cstore7, chrome)
check(cat.add(cstore7, chrome).id == first.id and len(cstore7.packages) == 1
      and cat.in_library(cstore7, chrome) is first, "adding an app twice keeps one library package")
SAMPLE = ("   - \r   \\ \r\r"
          "Name                 Id                          Version     Match       Source\n"
          "-------------------------------------------------------------------------------\n"
          "7-Zip                7zip.7zip                   24.08                   winget\n"
          "7-Zip ZS             mcmilk.7zip-zstd            24.08.0.1   Tag: 7zip   winget\n"
          "NanaZip Preview…     M2Team.NanaZip.Preview      5.0.1252.0  Tag: 7-zip  winget\n"
          "Ünïcödé Ärchiver     Some.Archiver               1.0                     winget\n")
calls = []


def fake(args, timeout):
    calls.append(list(args))
    return 0, SAMPLE


rows = cat.winget_search("zip", runner=fake)
check(calls and calls[0][1:3] == ["search", "zip"] and calls[0][3:5] == ["--source", "winget"],
      "winget search runs 'winget search <query> --source winget' as an argument list (no shell)")
check([r_["winget"] for r_ in rows] == ["7zip.7zip", "mcmilk.7zip-zstd", "M2Team.NanaZip.Preview", "Some.Archiver"]
      and rows[2]["name"] == "NanaZip Preview" and rows[1]["version"] == "24.08.0.1"
      and rows[0]["detect"] == {"method": "registry", "value": "7-Zip"}, "winget output parsed defensively")
check(cat.parse_search("No package found matching input criteria.\n") == [] and cat.parse_search("") == []
      and cat.winget_search("  ", runner=fake) == [] and len(calls) == 1, "no results and empty queries handled")
check(cat.to_package(rows[3]).winget_id == "Some.Archiver", "a live winget result can be added to the library")


def stuck(args, timeout):
    while True:
        check_cancel()
        time.sleep(.05)


prog = Progress(1)
threading.Timer(.3, prog.cancel).start()
t0 = time.time()
try:
    with cancel_scope(prog.check):
        cat.winget_search("zip", runner=stuck)
    stopped = False
except Cancelled:
    stopped = True
check(stopped and time.time() - t0 < 3, "Cancel stops a winget search")
prog = Progress(1)
threading.Timer(.3, prog.cancel).start()
t0 = time.time()
try:
    with cancel_scope(prog.check):
        cat.run_process([PY, "-c", "import time; time.sleep(30)"])
    stopped = False
except Cancelled:
    stopped = True
check(stopped and time.time() - t0 < 5, "Cancel kills the running search process")
try:
    cat.run_process([PY, "-c", "import time; time.sleep(30)"], timeout=.5)
    timed = False
except TimeoutError:
    timed = True
check(timed, "a search that hangs times out")
check(cat.run_process([PY, "-c", "print('hi')"]) == (0, "hi\n"), "search runner returns exit code and output")
if not cat.winget_path():
    try:
        cat.winget_search("zip")
        missing = False
    except FileNotFoundError:
        missing = True
    check(missing, "no winget on this PC: search says so (the UI falls back to the curated list)")

# --- 11. Dynamic versions: winget, GitHub, a vendor page or a script, never the real network -----------------------
from sectorsmith.deploy import versions as V  # noqa: E402

WG_SHOW = ("Found 7-Zip [7zip.7zip]\r\nVersion: 24.08\r\nPublisher: Igor Pavlov\r\nInstaller:\r\n"
           "  Installer Type: exe\r\n  Installer Url: https://www.7-zip.org/a/7z2408-x64.exe\r\n"
           "  Installer SHA256: " + "AB" * 32 + "\r\n")
got = V.parse_winget_show(WG_SHOW)
check(got == {"version": "24.08", "url": "https://www.7-zip.org/a/7z2408-x64.exe", "sha256": "ab" * 32},
      "winget show: Version, Installer Url and SHA256 parsed")
GH = json.dumps({"tag_name": "v8.7.1", "assets": [
    {"name": "npp.8.7.1.Installer.arm64.exe", "browser_download_url": "https://github.com/x/arm.exe"},
    {"name": "npp.8.7.1.Installer.x64.exe", "browser_download_url": "https://github.com/x/x64.exe",
     "digest": "sha256:" + "cd" * 32}]})
check(V.parse_github(GH, r"Installer\.x64\.exe$") == {"version": "8.7.1", "url": "https://github.com/x/x64.exe",
                                                       "sha256": "cd" * 32},
      "GitHub release: tag as version, asset by pattern, its digest as the hash")
page = '<a href="/files/tool-5.2.0-setup.msi">Download Tool 5.2.0</a>'
check(V.parse_page(page, r'href="(?P<url>[^"]+-(?P<version>[\d.]+)-setup\.msi)"', "https://vendor.example/dl/")
      == {"version": "5.2.0", "url": "https://vendor.example/files/tool-5.2.0-setup.msi"},
      "vendor page: named groups give version and link, relative links resolved")
check(V.parse_page("Latest release: 3.4", r"release: ([\d.]+)") == {"version": "3.4"}, "page: first group as version")
check(V.parse_script('{"version": "v2.0.1", "url": "https://x.example/a.exe"}') ==
      {"version": "2.0.1", "url": "https://x.example/a.exe"}
      and V.parse_script("2.0.2\nhttps://x.example/b.exe\n" + "EF" * 32) ==
      {"version": "2.0.2", "url": "https://x.example/b.exe", "sha256": "ef" * 32}, "script output: JSON or lines")
for bad, why in ((lambda: V.parse_page("nothing", r"v([\d.]+)"), "pattern didn't match"),
                 (lambda: V.parse_github(json.dumps({"tag_name": "1.0", "assets": []}), "x64"), "no file"),
                 (lambda: V.parse_github("{}", ""), "no published release"),
                 (lambda: V.parse_script(""), "didn't print a version"),
                 (lambda: V.fetch("http://insecure.example/x"), "only https"),
                 (lambda: V.resolve({"kind": "script", "script": "x"},
                                    runner=lambda a, t: (0, "1.0\nhttp://p.example/a.exe")), "only https")):
    try:
        bad()
        msg = ""
    except V.SourceError as e:
        msg = str(e)
    check(why in msg, f"version source problem explained: {msg}")
for src, why in (({"kind": "github", "repo": "not a repo"}, "owner/name"), ({"kind": "page", "url": "http://a.example",
                                                                             "regex": "x"}, "https"),
                 ({"kind": "winget", "id": "nope"}, "winget ID"), ({"kind": "page", "url": "https://a.example",
                                                                    "regex": "("}, "regular expression")):
    try:
        V.validate(src)
        msg = ""
    except ValueError as e:
        msg = str(e)
    check(why in msg, f"bad version source refused: {msg}")

calls = []


def fake_fetch(url, **kw):
    calls.append(url)
    if urllib.parse.urlsplit(url).hostname == "api.github.com":
        return GH.encode()
    return page.encode()


def fake_runner(args, timeout):
    calls.append(" ".join(args[:4]))
    if args[1] == "show":
        return 0, WG_SHOW
    import base64
    script = base64.b64decode(args[args.index("-EncodedCommand") + 1]).decode("utf-16-le")
    return (0, "9.9\nhttps://s.example/app.exe") if "Write-Output" in script else (1, "boom")


check(V.resolve({"kind": "winget", "id": "7zip.7zip"}, runner=fake_runner)["version"] == "24.08"
      and calls[-1] == "winget show --id 7zip.7zip", "winget source runs 'winget show --id X --exact'")
check(V.resolve({"kind": "github", "repo": "notepad-plus-plus/notepad-plus-plus", "asset": "x64"},
                fetcher=fake_fetch)["version"] == "8.7.1"
      and calls[-1] == "https://api.github.com/repos/notepad-plus-plus/notepad-plus-plus/releases/latest",
      "GitHub source asks the releases API")
check(V.resolve({"kind": "script", "script": "Write-Output '9.9'"}, runner=fake_runner) ==
      {"version": "9.9", "url": "https://s.example/app.exe"} and "-EncodedCommand" in calls[-1],
      "script source runs PowerShell with the script encoded (no shell)")

vstore = Store(os.path.join(W, "lib11"))
vstore.fetcher, vstore.runner = fake_fetch, fake_runner
gh = vstore.upsert("packages", Package(name="Dyn GH", version_source={"kind": "github", "repo": "a/b", "asset": "x64"}))
calls.clear()
day1 = time.mktime((2026, 3, 2, 10, 0, 0, 0, 0, -1))
a1 = V.latest(vstore, gh, now=day1)
a2 = V.latest(vstore, gh, now=day1 + 3600)
check(a1["version"] == "8.7.1" and not a1["cached"] and a2["cached"] and len(calls) == 1,
      "newest version looked up once a day (second ask the same day comes from the cache)")
V.latest(vstore, gh, now=day1 + 86400)
gh.version_source = {"kind": "github", "repo": "a/c", "asset": "x64"}
V.latest(vstore, gh, now=day1 + 86400 + 60)
check(len(calls) == 3 and V.cached(vstore, gh)["version"] == "8.7.1", "a new day or a changed source asks again")

# a session: 3.0 installed, the source says 3.1, its download is checked against the source's SHA256
dyn_src = os.path.join(W, "src", "dyn-3.1.py")
shutil.copy(FAKEINST, dyn_src)
with open(dyn_src, "rb") as _f:
    dyn_sha = hashlib.sha256(_f.read()).hexdigest()
REL = {"tag_name": "release-3.1", "assets": [{"name": "dyn-3.1.py", "browser_download_url":
                                              "https://downloads.example/dyn-3.1.py", "digest": "sha256:" + dyn_sha}]}
downloads = []


def fake_download(url, dest, progress=None):
    downloads.append(url)
    shutil.copy(dyn_src if len(downloads) == 1 else FAKE, dest)


vstore.fetcher = lambda url, **kw: json.dumps(REL).encode()
vstore.downloader = fake_download
dyn = vstore.upsert("packages", Package(name="Dyn App", kind="exe", install=f'"{PY}" "{{installer}}" add "Dyn App" '
                                                                            '{version}',
                                        uninstall="{registry_uninstall}",
                                        detection={"method": "registry", "value": "Dyn App"},
                                        version_source={"kind": "github", "repo": "dyn/app", "asset": r"\.py$"}))
deploy(vstore, dyn, "latest")
set_inventory(entry("Dyn App", "3.0"))
s_ = core.run_session(WinCodes(), vstore, Progress(1), mode="detect")
a_ = s_["actions"][0]
check(a_["action"] == "upgrade" and a_["want"] == "3.1" and a_["source"]["url"].endswith("dyn-3.1.py"),
      "Latest compares against the source's newest version (3.0 installed, 3.1 out: upgrade)")
s_ = core.run_session(WinCodes(), vstore, Progress(1), mode="full")
a_ = s_["actions"][0]
check(a_["status"] == "compliant" and inventory().get("Dyn App") == "3.1" and "SHA256 checked" in a_["log"]
      and downloads == ["https://downloads.example/dyn-3.1.py"],
      f"installer downloaded, hash checked, installed with the resolved version ({a_.get('result')})")
core.run_session(WinCodes(), vstore, Progress(1), mode="full")
check(len(downloads) == 1, "the download is kept for that version, not fetched again")
REL["tag_name"], REL["assets"][0]["browser_download_url"] = "3.2", "https://downloads.example/dyn-3.2.py"
os.remove(os.path.join(vstore.root, V.CACHE))
s_ = core.run_session(WinCodes(), vstore, Progress(1), mode="full")
a_ = s_["actions"][0]
check(a_["status"] == "failed" and "SHA256" in a_["result"] and inventory().get("Dyn App") == "3.1"
      and not os.listdir(os.path.join(vstore.root, "downloads", dyn.id, "3.2")),
      f"a download whose SHA256 doesn't match is thrown away, not run ({a_['result']})")
vstore.fetcher = lambda url, **kw: (_ for _ in ()).throw(V.SourceError("offline"))
os.remove(os.path.join(vstore.root, V.CACHE))
s_ = core.run_session(WinCodes(), vstore, Progress(1), mode="detect")
check(s_["actions"][0]["action"] == "none" and "offline" in s_["actions"][0]["result"],
      "source unreachable: said so, and an installed app is left as it is")

# --- 12. Dependencies: installed first, removed after, loops refused ----------------------------------------------
dstore = Store(os.path.join(W, "lib12"))
base_ = installer_pkg(dstore, "Dep Base", "1.0")
mid = installer_pkg(dstore, "Dep Mid", "1.0", prerequisites=[base_.id])
top = installer_pkg(dstore, "Dep Top", "1.0", prerequisites=[mid.id])
base_.prerequisites = [top.id]
try:
    dstore.upsert("packages", base_)
    msg = ""
except ValueError as e:
    msg = str(e)
base_.prerequisites = []
check("loop" in msg and "Dep Base needs Dep Top needs Dep Mid needs Dep Base" in msg
      and dstore.get("packages", base_.id).prerequisites == [], f"dependency loop refused in plain words: {msg}")
selfish = Package(name="Selfish")
selfish.prerequisites = [selfish.id]
try:
    dstore.upsert("packages", selfish)
    refused = False
except ValueError:
    refused = True
check(refused and dstore.get("packages", selfish.id) is None, "a package can't need itself")
set_inventory()
d_top = deploy(dstore, top, "installed")
s_ = core.run_session(WinCodes(), dstore, Progress(1), mode="full")
names = [a["name"] for a in s_["actions"]]
check(names == ["Dep Base", "Dep Mid", "Dep Top"] and all(a["status"] == "compliant" for a in s_["actions"])
      and set(inventory()) == {"Dep Base", "Dep Mid", "Dep Top"},
      "dependencies installed first, in order (Base, Mid, then Top)")
other_ = installer_pkg(dstore, "Dep Other", "1.0")
deploy(dstore, other_, "installed")
d_gone = deploy(dstore, base_, "uninstalled")
p_ = core.plan(WinCodes(), dstore, host, LocalEndpoint().installed_software())
by = {a["name"]: a for a in p_}
check(by["Dep Base"]["action"] == "audit" and "Dep Top needs it" in by["Dep Base"]["result"],
      "a package something else still needs is kept, and the plan says why")
dstore.delete("deployments", d_top.id)
dstore.delete("deployments", d_gone.id)
for pk in (top, mid, base_):
    deploy(dstore, pk, "uninstalled")
# the fake installers registered their uploaded copies (now gone) as uninstallers; point them back at fakeinst.py
set_inventory(*(entry(n, "1.0") for n in ("Dep Base", "Dep Mid", "Dep Top")))
p_ = core.plan(WinCodes(), dstore, host, LocalEndpoint().installed_software())
check([a["name"] for a in p_][:3] == ["Dep Top", "Dep Mid", "Dep Base"] and p_[3]["name"] == "Dep Other"
      and all(a["action"] == "uninstall" for a in p_[:3]),
      "removals run first, each before what it needs (Top, Mid, then Base)")
s_ = core.run_session(WinCodes(), dstore, Progress(1), mode="full")
check(set(inventory()) == {"Dep Other"} and s_["summary"]["failed"] == 0, "all three removed")
dstore.delete("packages", mid.id)
check(dstore.get("packages", top.id).prerequisites == [] and Store(dstore.root).get("packages", top.id).prerequisites
      == [], "deleting a package takes it off the lists of packages that needed it")

# --- 13. Exit codes, pending restarts and the restart policy ---------------------------------------------------------
from sectorsmith.link import endpoint as E  # noqa: E402

pk = Package(name="Codes", success_codes=[0, 7], reboot_codes=[3010, 5])
check([core.exit_outcome(pk, c) for c in (0, 7, 3010, 5, 1603, 1641)] == ["ok", "ok", "reboot", "reboot", "failed",
                                                                           "failed"],
      "exit codes: success codes, restart codes (success plus a restart) and the rest failed")
check(core.suppress_reboot('msiexec.exe /i "a.msi" /qn') == 'msiexec.exe /i "a.msi" /qn REBOOT=ReallySuppress'
      and core.suppress_reboot('"C:\\Windows\\System32\\msiexec.exe" /x {X} /qn').endswith("REBOOT=ReallySuppress")
      and core.suppress_reboot("msiexec /i a.msi REBOOT=Force") == "msiexec /i a.msi REBOOT=Force"
      and core.suppress_reboot('"setup.exe" /S') == '"setup.exe" /S', "suppress adds REBOOT=ReallySuppress to msiexec only")


class FakeReg:
    def __init__(self, keys=(), renames=None):
        self.keys, self.renames = set(keys), renames

    def key_exists(self, path):
        return path in self.keys

    def value(self, path, name):
        return self.renames if path == E.RENAME_KEY and name == "PendingFileRenameOperations" else None


check(E.pending_reboot_reasons(FakeReg()) == [] and E.pending_reboot_reasons(FakeReg(renames=["", " "])) == [],
      "no pending restart on a clean registry")
r_ = E.pending_reboot_reasons(FakeReg([k for k, _w in E.REBOOT_KEYS], ["\\??\\C:\\old.dll", ""]))
check(len(r_) == 3 and "CBS" in r_[0] and "Windows Update" in r_[1] and "PendingFileRenameOperations" in r_[2],
      "pending restart read from CBS, Windows Update and PendingFileRenameOperations")
check(set(LocalEndpoint().pending_reboot()) == {"pending", "reasons"}, "this PC answers pending_reboot")


class FarPC(WinCodes):
    """Another PC as far as the session is concerned. Restart commands are recorded, never run."""
    is_local = False
    pending = []
    restarts = []

    def info(self):
        return dict(super().info(), hostname="FAR-PC", os="Windows-11-10.0.26100")

    def pending_reboot(self):
        return {"pending": bool(self.pending), "reasons": list(self.pending)}

    def run_command(self, cmd, timeout=3600, cwd=None, env=None):
        if cmd.lower().startswith(("shutdown", "restart-computer")):
            FarPC.restarts.append(cmd)
            return {"code": 0, "out": "", "err": "", "seconds": 0}
        return super().run_command(cmd, timeout, cwd, env)


rstore = Store(os.path.join(W, "lib13"))
set_inventory()
rb = installer_pkg(rstore, "Reboot Me", "1.0", " --exit 5", reboot_codes=[5], success_codes=[0])
d_rb = deploy(rstore, rb, "installed")
FarPC.pending = ["Windows Update installed updates that need a restart"]
s_ = core.run_session(FarPC(), rstore, Progress(1), mode="detect")
check(s_["pending_reboot"] == FarPC.pending and s_["summary"]["reboot"], "the check step reports a pending restart")
FarPC.pending = []
s_ = core.run_session(FarPC(), rstore, Progress(1), mode="full")
a_ = s_["actions"][0]
check(a_["status"] == "compliant" and a_.get("reboot") and s_["summary"]["reboot"] and not FarPC.restarts
      and "restart" not in s_, "policy Never: exit 5 (a restart code) works, says a restart is needed, doesn't restart")
d_rb.reboot_policy = "end"
rstore.upsert("deployments", d_rb)
set_inventory()
s_ = core.run_session(FarPC(), rstore, Progress(1), mode="full")
check(len(FarPC.restarts) == 1 and FarPC.restarts[0].startswith("shutdown.exe /r /t 120") and
      "restarting" in s_["restart"], f"policy At the end: one restart with a warning, at the end ({s_.get('restart')})")
set_inventory()
s_ = core.run_session(WinCodes(), rstore, Progress(1), mode="full")
check(len(FarPC.restarts) == 1 and "skipped" in s_["restart"] and s_["summary"]["reboot"],
      "the technician's own PC is never restarted automatically, only told")
s_ = core.run_session(FarPC(), rstore, Progress(1), mode="full")
check(len(FarPC.restarts) == 1 and "restart" not in s_, "nothing to do and nothing pending: no restart")
d_rb.reboot_policy = "suppress"
rstore.upsert("deployments", d_rb)
set_inventory()
s_ = core.run_session(FarPC(), rstore, Progress(1), mode="full")
check(len(FarPC.restarts) == 1 and s_["summary"]["reboot"], "policy Suppress: never restarts, still reports it")
check(Store(rstore.root).get("deployments", d_rb.id).reboot_policy == "suppress"
      and Store(rstore.root).get("packages", rb.id).reboot_codes == [5], "restart settings saved")

# --- 14. Tasks as configuration: typed parameters, safe script text, secrets, the built-in library ----------------
from sectorsmith.deploy import taskparams as TP, tasklib  # noqa: E402
from sectorsmith.integrations import dpapi  # noqa: E402

for bad, why in (([{"name": "bad name"}], "can't be a parameter name"), ([{"name": "SectorSmithX"}], "reserved"),
                 ([{"name": "a"}, {"name": "A"}], "two parameters"), ([{"name": "c", "type": "choice"}], "option"),
                 ([{"name": "n", "type": "number", "default": "ten"}], "should be a number"),
                 ([{"name": "x", "type": "colour"}], "unknown type")):
    try:
        TP.tidy_parameters(bad)
        msg = ""
    except ValueError as e:
        msg = str(e)
    check(why in msg, f"bad parameter refused: {msg}")
num, yn = {"name": "n", "type": "number"}, {"name": "b", "type": "bool"}
ch = {"name": "c", "type": "choice", "choices": ["Red", "Blue"]}
check(TP.coerce(num, "12") == 12 and TP.coerce(num, "-1.5") == -1.5 and TP.coerce(yn, "yes") is True
      and TP.coerce(yn, "0") is False and TP.coerce(ch, "Blue") == "Blue", "values coerced to their types")
for p_, v_ in ((num, "1; Remove-Item C:\\"), (num, "$(whoami)"), (yn, "maybe"), (ch, "Green"),
               ({"name": "s", "type": "secret"}, "plain text")):
    try:
        TP.coerce(p_, v_)
        refused = False
    except ValueError:
        refused = True
    check(refused, f"{p_['type']} value {v_!r} refused")
evil = "a'; Remove-Item C:\\ -Recurse; '"
hdr = TP.render("Write-Output $Name\n", {"Name": evil, "Count": 3, "Ratio": 0.5, "On": True, "Off": False},
                context={"SectorSmithClient": "O'Brien Ltd"})
check(hdr.splitlines()[:6] == ["$SectorSmithClient = 'O''Brien Ltd'", "$Name = 'a''; Remove-Item C:\\ -Recurse; '''",
                               "$Count = 3", "$Ratio = 0.5", "$On = $true", "$Off = $false"]
      and ps_balanced(hdr), "PowerShell header: quoted literals with quotes doubled, numbers and $true/$false bare")
try:
    TP.render("x", {"bad-name": 1})
    refused = False
except ValueError:
    refused = True
check(refused, "a variable name that isn't an identifier is refused")
marker = os.path.join(W, "injected")
ep = LocalEndpoint()
val = f"x'; touch {marker}; echo '$(touch {marker})`touch {marker}`"
r_ = ep.run_script(TP.render('printf "%s" "$Name"', {"Name": val}, "shell"), language="shell")
check(r_["out"] == val and not os.path.exists(marker), "sh: a hostile value arrives intact and runs nothing")
r_ = ep.run_script("echo hi", language="shell", params={"bad name": 1})
check(r_["code"] == 127 and "letters" in r_["err"], "the endpoint refuses parameter names that aren't identifiers")

# secrets: DPAPI only, so refused here and nothing written
tstore = Store(os.path.join(W, "lib14"))
try:
    TP.save_secret(tstore.root, "hunter2")
    refused = False
except dpapi.SecretsUnavailable:
    refused = True
check(refused and not os.path.exists(os.path.join(tstore.root, TP.SECRETS)) if not dpapi.available() else True,
      "without DPAPI a secret is refused and nothing is written")
# a stand-in for DPAPI so the rest can run here
_real = (dpapi.protect, dpapi.unprotect)
dpapi.protect = lambda b: bytes(x ^ 0x5A for x in b)
dpapi.unprotect = lambda b: bytes(x ^ 0x5A for x in b)
ref = TP.save_secret(tstore.root, "hunter2-s3cret")
with open(os.path.join(tstore.root, TP.SECRETS)) as _f:
    check("hunter2" not in _f.read(), "secrets.json holds the encrypted value only")
check(TP.load_secret(tstore.root, ref["secret"]) == "hunter2-s3cret", "a stored secret decrypts for the run")
flag5 = os.path.join(W, "task5_flag")
cfg = tstore.upsert("tasks", Task(name="Configured", language="shell",
                                  parameters=[{"name": "Size", "type": "number", "default": 2},
                                              {"name": "Mode", "type": "choice", "choices": ["a", "b"],
                                               "default": "a"},
                                              {"name": "Pw", "type": "secret"}],
                                  test=f'echo "TEST size=$Size mode=$Mode pw=$Pw"; [ -f "{flag5}" ]',
                                  set=f'echo "SET pw=$Pw"; echo "$Size $Mode" > "{flag5}"',
                                  get=f'echo "GET $(cat "{flag5}") $SectorSmithComputer pw=$Pw"'))
check([p_["type"] for p_ in cfg.parameters] == ["number", "choice", "secret"] and cfg.parameters[2]["default"] == "",
      "typed parameters saved on the task")
dcfg = tstore.upsert("deployments", Deployment("task", cfg.id, "enforce", params={"Size": 5, "Mode": "b", "Pw": ref}))


class Recorder(LocalEndpoint):
    scripts = []

    def run_script(self, script, language="powershell", timeout=1800, params=None):
        Recorder.scripts.append(script)
        return super().run_script(script, language, timeout, params)


s_ = core.run_session(Recorder(), tstore, Progress(1), mode="detect")
a_ = s_["actions"][0]
check(len(Recorder.scripts) == 1 and "TEST" in Recorder.scripts[0] and "SET" not in Recorder.scripts[0]
      and a_["action"] == "set", "the check step runs Test only")
check("size=5 mode=b" in a_["test_output"] and "hunter2" not in a_["test_output"] and "pw=********" in
      a_["test_output"], "deployment values reach the script; the secret is masked in its output")
s_ = core.run_session(Recorder(), tstore, Progress(1), mode="full")
a_ = s_["actions"][0]
with open(s_["path"]) as _f:
    saved = _f.read()
check(a_["status"] == "compliant" and a_["value"].startswith("GET 5 b " + host) and "hunter2" not in saved
      and "pw=********" in a_["log"], "Set, Test again, then Get records the new state; the secret never lands in "
                                      "the session")
dcfg.params = {"Size": "lots"}
tstore.upsert("deployments", dcfg)
s_ = core.run_session(Recorder(), tstore, Progress(1), mode="detect")
check(s_["actions"][0]["action"] == "audit" and "should be a number" in s_["actions"][0]["result"],
      "a bad saved value is reported, not run")
tstore.delete("deployments", dcfg.id)
d2_ = tstore.upsert("deployments", Deployment("task", cfg.id, "enforce", params={"Pw": ref}))
tstore.delete("deployments", d2_.id)
check(ref["secret"] not in TP._load(tstore.root), "removing a deployment forgets its secrets")
dpapi.protect, dpapi.unprotect = _real

# the built-in library
check(8 <= len(tasklib.LIBRARY) <= 12 and len({t["library_id"] for t in tasklib.LIBRARY}) == len(tasklib.LIBRARY),
      f"{len(tasklib.LIBRARY)} built-in tasks")
want_ids = {"rename-computer", "time-zone", "power-plan", "bitlocker", "local-admin", "windows-update",
            "remove-bloatware", "defender", "fast-startup", "oem-info"}
check(want_ids <= {t["library_id"] for t in tasklib.LIBRARY}, "the library covers renaming, time zone, power, "
      "BitLocker, local admin, Windows Update, bloatware, Defender, fast startup and OEM support info")
bad_scripts = []
for entry_ in tasklib.LIBRARY:
    t_ = tasklib.to_task(entry_)
    vals = {p_["name"]: (p_["default"] if p_["default"] not in ("", None) else ("x" if p_["type"] != "secret"
                                                                                  else TP.Revealed("pw")))
            for p_ in t_.parameters}
    for which in ("test", "get", "set"):
        text = core.task_script(t_, which, vals, core.task_context(tstore, "PC-1"))
        if not (getattr(t_, which).strip() and ps_balanced(text) and all(f"${n} = " in text for n in vals)):
            bad_scripts.append(f"{t_.name} {which}")
check(not bad_scripts, f"every built-in Test, Get and Set builds with its parameters and balances {bad_scripts}")
check(all(t["test"].count("exit 0") or "exit 0" in t["test"] for t in tasklib.LIBRARY)
      and "exit 3010" in tasklib.get("rename-computer")["set"], "Tests exit 0 when fine; renaming asks for a restart")
check("RecoveryPassword" in tasklib.get("bitlocker")["set"] and "Write-Output $rp" not in tasklib.get("bitlocker")["set"]
      and "never printed" in tasklib.get("bitlocker")["description"], "BitLocker never prints the recovery key")
one = tasklib.add(tstore, "time-zone")
check(tasklib.add(tstore, "time-zone").id == one.id and one.library_id == "time-zone" and one.get
      and Store(tstore.root).get("tasks", one.id).parameters[0]["name"] == "TimeZone",
      "adding a built-in task twice keeps one copy, with its parameters")

# --- 15. Targeting: tags on PCs, filter scripts, applies-to counts ---------------------------------------------------
gstore = Store(os.path.join(W, "lib15"))
check(core.clean_tags("Laptops, kiosk , laptops,, ") == ["Laptops", "kiosk"], "tags tidied, case-insensitive")
gstore.set_tags(host, ["Kiosk", "Laptops"])
gstore.set_tags("PC-2", "laptops")
gstore.set_tags("PC-3", [])
cl15 = gstore.upsert("clients", Client(name="Acme", machines=["PC-2", "PC-4"]))
check(Store(gstore.root).tags_of(host.upper()) == ["Kiosk", "Laptops"] and gstore.all_tags() == ["Kiosk", "Laptops"]
      and gstore.known_hosts() == sorted([host, "PC-2", "PC-3", "PC-4"], key=str.lower),
      "tags saved per PC; known PCs come from clients and tagged PCs")
set_inventory(entry("Tag App", "1.0"))
tag_app = installer_pkg(gstore, "Tag App", "1.0")
d_all = deploy(gstore, tag_app, "installed")
d_kiosk = deploy(gstore, tag_app, "uninstalled", tags=["kiosk"])
d_lap = deploy(gstore, tag_app, "installed", target_kind="client", target_value=cl15.id, tags=["Laptops"])
check(core.applies_to(gstore, d_all) == gstore.known_hosts() and core.applies_to(gstore, d_kiosk) == [host]
      and core.applies_to(gstore, d_lap) == ["PC-2"], "applies-to: every PC, tag only, client and tag together")
check([d.desired for d in core.applicable(gstore, host, False) if d.item_id == tag_app.id] == ["uninstalled"],
      "a tagged deployment beats the untagged one for the same target")
check([d.desired for d in core.applicable(gstore, "PC-3", False) if d.item_id == tag_app.id] == ["installed"],
      "an untagged PC gets the wider deployment")
for d_ in (d_all, d_kiosk, d_lap):
    gstore.delete("deployments", d_.id)

f_yes = installer_pkg(gstore, "Filter Yes", "1.0")
f_no = installer_pkg(gstore, "Filter No", "1.0")
f_bad = installer_pkg(gstore, "Filter Broken", "1.0")
f_wide = installer_pkg(gstore, "Filter Wide", "1.0")
probe = os.path.join(W, "filter_runs")
yes_script = f'echo run >> "{probe}"; echo checking; echo True'
deploy(gstore, f_yes, "installed", filter_script=yes_script, filter_language="shell")
deploy(gstore, f_no, "installed", filter_script="echo False", filter_language="shell")
deploy(gstore, f_bad, "installed", filter_script="exit 3", filter_language="shell")
deploy(gstore, f_wide, "installed")
deploy(gstore, f_wide, "uninstalled", target_kind="machine", target_value=host, filter_script=yes_script,
       filter_language="shell")
set_inventory(entry("Filter Wide", "1.0"))
s_ = core.run_session(WinCodes(), gstore, Progress(1), mode="detect")
by = {a["name"]: a for a in s_["actions"]}
why = {gstore.get("deployments", f["deployment"]).item_id: f["why"] for f in s_["filtered"]}
with open(probe) as _f:
    runs = _f.read().count("run")
check("Filter Yes" in by and "Filter No" not in by and "Filter Broken" not in by,
      "filter scripts decide on the PC: True applies, anything else doesn't")
check(why.get(f_no.id) == "the filter script said false" and "exit 3" in why.get(f_bad.id, ""),
      f"the session says why a deployment was filtered out {why}")
check(runs == 1, "the same filter script runs once per check, not once per deployment")
check(by["Filter Wide"]["action"] == "uninstall", "a filtered one-PC deployment still beats the wider one when it passes")

# --- 16. Inventory: snapshots, search, conditions and detection from the snapshot -----------------------------------
from sectorsmith.deploy import inventory as INV  # noqa: E402

facts = LocalEndpoint().inventory()
check({"os", "build", "model", "serial", "cpu", "ram_gb", "disks", "volumes", "bitlocker", "last_boot"} <= set(facts)
      and facts["ram_gb"] > 0 and facts["volumes"], "this PC answers inventory with OS, hardware, disks and last boot")
istore = Store(os.path.join(W, "lib16"))
FACTS = {"hostname": "FAR-PC", "os": "Windows 11 Pro", "build": "26100", "model": "Latitude 7450",
         "manufacturer": "Dell Inc.", "serial": "ABC1234", "cpu": "Intel Core Ultra 7", "cores": 12, "ram_gb": 16.0,
         "disks": [{"name": "NVMe SSD", "size_gb": 476.9, "type": "SCSI"}],
         "volumes": [{"drive": "C:", "size_gb": 475.0, "free_gb": 120.5}], "bitlocker": "On", "domain": "acme.local",
         "last_boot": time.time() - 3 * 86400}


class InvPC(FarPC):
    def inventory(self):
        return dict(FACTS)


set_inventory(entry("Inv Present", "1.0"), entry("Old Viewer", "2.0"))
snap = INV.take(InvPC(), istore)
check(snap["serial"] == "ABC1234" and [e["name"] for e in snap["software"]] == ["Inv Present", "Old Viewer"]
      and INV.load(istore, "far-pc")["model"] == "Latitude 7450" and Store(istore.root).machine("FAR-PC").last_seen,
      "snapshot kept per PC with its software, and the PC marked as seen")


class OldAgent(InvPC):
    def inventory(self):
        raise AttributeError("inventory")


snap2 = INV.take(OldAgent(), istore, host="OLD-PC")
check(snap2 and snap2["software"] and "serial" not in snap2, "an older agent still gives a software-only snapshot")
check(dict(INV.summary(snap))["Model"] == "Dell Inc. Latitude 7450" and dict(INV.summary(snap))["Software"] == "2 apps",
      "snapshot summary for the details panel")
check(INV.matches(snap, "latitude abc1234") and INV.matches(snap, "old viewer") and not INV.matches(snap, "thinkpad"),
      "inventory search covers hardware facts and installed software")
for text, why in (("ram_gb about 8", "isn't a condition"), ("colour = red", "isn't something"),
                  ("ram_gb >= lots", "is a number"), ("software = Chrome", "contains")):
    try:
        INV.parse_conditions(text)
        msg = ""
    except ValueError as e:
        msg = str(e)
    check(why in msg, f"bad condition explained: {msg}")
P_ = INV.conditions_pass
check(P_(snap, "ram_gb >= 16; os contains windows 11; software contains present")[0] is True
      and P_(snap, "ram_gb > 16")[0] is False and P_(snap, "free_gb < 100")[0] is False
      and P_(snap, "days_since_boot >= 2")[0] is True and P_(snap, "software not contains Old Viewer")[0] is False
      and P_(snap, "model = latitude 7450")[0] is True and P_(None, "ram_gb >= 1")[0] is None,
      "conditions judged against the snapshot (numbers, text, software, derived fields; unknown without one)")
istore.set_tags("NEW-PC", ["x"])
big = installer_pkg(istore, "Big RAM Tool", "1.0")
d_big = deploy(istore, big, "installed", conditions="ram_gb >= 32")
viewer = installer_pkg(istore, "Old Viewer", "3.0")
deploy(istore, viewer, "latest", conditions="software contains Old Viewer")
unk = []
check(core.applies_to(istore, d_big, unknown=unk) == [] and core.applies_to(istore, deploy(
    istore, viewer, "installed", target_kind="machine", target_value="nobody", conditions="ram_gb >= 8")) == []
      and unk == ["NEW-PC"], "applies-to judges conditions from snapshots and counts PCs without one apart")
exp = {r["name"]: r["action"] for r in INV.expected(istore, "FAR-PC")}
check(exp == {"Old Viewer": "upgrade"}, f"from the snapshot alone: Old Viewer needs an update, Big RAM Tool doesn't "
                                        f"apply {exp}")
FACTS["ram_gb"] = 64.0
s_ = core.run_session(InvPC(), istore, Progress(1), mode="detect")
by = {a["name"]: a["action"] for a in s_["actions"]}
check(by == {"Big RAM Tool": "install", "Old Viewer": "upgrade"} and INV.load(istore, "FAR-PC")["ram_gb"] == 64.0,
      "each session takes a fresh snapshot first and judges conditions by it")
FACTS["ram_gb"] = 8.0
s_ = core.run_session(InvPC(), istore, Progress(1), mode="detect")
check([a["name"] for a in s_["actions"]] == ["Old Viewer"] and "ram_gb >= 32" in s_["filtered"][0]["why"],
      "a PC that no longer matches is left out, and the session says why")

# --- 17. Licences: per client, encrypted, handed to the installer, masked everywhere ---------------------------------
from sectorsmith.deploy import licences as LIC  # noqa: E402

lstore = Store(os.path.join(W, "lib17"))
acme = lstore.upsert("clients", Client(name="Acme", machines=[host]))
other_c = lstore.upsert("clients", Client(name="Other", machines=["PC-OTHER"]))
key_out = os.path.join(W, "licence_key_seen")
file_out = os.path.join(W, "licence_file_seen")
lk = lstore.upsert("packages", Package(name="Key App", kind="script", version="1.0", language="shell",
                                       install=f'echo "{{licence_key}}|$SECTORSMITH_LICENCE_KEY" > "{key_out}"; '
                                               'echo "installing with {licence_key}"',
                                       detection={"method": "script", "value": f'[ -f "{key_out}" ] && echo 1.0'}))
lf = lstore.upsert("packages", Package(name="File App", kind="script", version="1.0", language="shell",
                                       install=f'cp "{{licence_file}}" "{file_out}" && [ "$SECTORSMITH_LICENCE_FILE" = '
                                               '"{licence_file}" ]',
                                       detection={"method": "script", "value": f'[ -f "{file_out}" ] && echo 1.0'}))
KEY = "ABCDE-12345-FGHIJ-7Q2X"
if not dpapi.available():
    try:
        LIC.save(lstore, core.Licence(package_id=lk.id, client_id=acme.id), key=KEY)
        refused = False
    except dpapi.SecretsUnavailable:
        refused = True
    check(refused and not lstore.licences and not os.path.exists(os.path.join(lstore.root, TP.SECRETS)),
          "without DPAPI a licence key is refused and nothing is kept")
dpapi.protect = lambda b: bytes(x ^ 0x5A for x in b)
dpapi.unprotect = lambda b: bytes(x ^ 0x5A for x in b)
lic_k = LIC.save(lstore, core.Licence(package_id=lk.id, client_id=acme.id), key=KEY)
fallback = LIC.save(lstore, core.Licence(package_id=lk.id), key="EVERY-ONE-ELSE-0000")
lic_src = os.path.join(W, "site.lic")
with open(lic_src, "wb") as _f:
    _f.write(b"LICENSE acme seats=25\n\x00\x01binary")
lic_f = LIC.save(lstore, core.Licence(package_id=lf.id, client_id=acme.id, kind="file"), file_path=lic_src)
try:
    LIC.save(lstore, core.Licence(package_id=lk.id, client_id=acme.id), key="dupe")
    dup = ""
except ValueError as e:
    dup = str(e)
check("already has a licence" in dup, "one licence per client and package")
with open(os.path.join(lstore.root, TP.SECRETS)) as _f:
    raw_secrets = _f.read()
with open(os.path.join(lstore.root, "licences.json")) as _f:
    raw_lic = _f.read()
with open(os.path.join(lstore.root, "licences", lic_f.id, "licence.bin"), "rb") as _f:
    raw_file = _f.read()
check(KEY not in raw_secrets and KEY not in raw_lic and b"seats" not in raw_file, "keys and files are stored encrypted")
check(LIC.masked(lic_k) == "Key ending 7Q2X" and LIC.masked(lic_f) == "File site.lic",
      "licences shown masked: the key's last four characters only")
check(LIC.for_host(lstore, lk.id, host).id == lic_k.id and LIC.for_host(lstore, lk.id, "PC-OTHER").id == fallback.id
      and LIC.for_host(lstore, lf.id, "PC-OTHER") is None, "a PC gets its client's licence, else the every-client one")
deploy(lstore, lk, "installed")
deploy(lstore, lf, "installed")
s_ = core.run_session(LocalEndpoint(), lstore, Progress(1), mode="full")
by = {a["name"]: a for a in s_["actions"]}
with open(key_out) as _f:
    seen_key = _f.read().strip()
with open(file_out, "rb") as _f:
    seen_file = _f.read()
with open(s_["path"]) as _f:
    sess_text = _f.read()
check(by["Key App"]["status"] == "compliant" and seen_key == f"{KEY}|{KEY}",
      "the key reaches the installer as {licence_key} and SECTORSMITH_LICENCE_KEY")
check(KEY not in sess_text and "********" in by["Key App"]["log"] and "Key ending 7Q2X" in by["Key App"]["log"],
      "the key never appears in the session or its log, only masked")
check(by["File App"]["status"] == "compliant" and seen_file == b"LICENSE acme seats=25\n\x00\x01binary"
      and not [n for n in os.listdir(LocalEndpoint().deploy_dir()) if n.startswith("lic-")],
      "the licence file is copied next to the install, passed as {licence_file}, then removed from the PC")
lstore.delete("clients", acme.id)
check([x.id for x in lstore.licences] == [fallback.id] and lic_k.secret["secret"] not in TP._load(lstore.root)
      and not os.path.exists(os.path.join(lstore.root, "licences", lic_f.id)),
      "removing a client removes its licences and their encrypted keys and files")
dpapi.protect, dpapi.unprotect = _real

# --- 18. Audit log: who changed what in the library, and what every session did -----------------------------------
from sectorsmith.deploy import audit as AUD  # noqa: E402

astore = Store(os.path.join(W, "lib18"))
astore.actor = lambda: "Dominic"
set_inventory()
ap = installer_pkg(astore, "Audit App", "1.0")
ad = deploy(astore, ap, "installed")
ad.enabled = False
astore.upsert("deployments", ad)
ad.enabled = True
ad.params = {"Pw": {"secret": "abc"}}
astore.upsert("deployments", ad)
astore.upsert("deployments", ad)                     # nothing changed: no line
astore.set_tags("AUDIT-PC", ["Kiosk"])
m_ = astore.machine("AUDIT-PC")
m_.last_seen = time.time()
astore.upsert("machines", m_)                        # only the last-seen time: no line
s_ = core.run_session(WinCodes(), astore, Progress(1), mode="full")
astore.delete("deployments", ad.id)
rows = AUD.read(astore)
lines = [(r["action"], r["kind"], r["name"], r["detail"]) for r in reversed(rows)]
check(all(r["who"] == "Dominic" and r["when"] for r in rows), "every line says who and when")
check(lines[:2] == [("added", "packages", "Audit App", ""), ("added", "deployments", "Audit App", "")]
      and ("changed", "deployments", "Audit App", "enabled: True -> False") in lines,
      "library changes logged with the field that changed")
pw = [x for x in lines if x[0] == "changed" and "params" in x[3]]
check(len(pw) == 1 and pw[0][3] == "enabled: False -> True; params" and "abc" not in json.dumps(rows),
      "secret-bearing fields are named, never shown")
check(sum(1 for x in lines if x[1] == "deployments" and x[0] == "changed") == 2
      and ("first seen", "machines", "AUDIT-PC", "") in lines
      and not [x for x in lines if x[1] == "machines" and "last_seen" in x[3]],
      "saving without a change, or a PC's last-seen time, adds no line")
check(any(x[0] == "installed" and x[1] == "session" and x[2] == f"Audit App on {host}" for x in lines)
      and any(x[0] == "maintenance" and x[2] == host and "1 OK" in x[3] for x in lines),
      "each session action and the session itself are logged")
check(lines[-1] == ("removed", "deployments", "Audit App", ""), "removals logged")
csv_path = os.path.join(W, "audit.csv")
evil_row = dict(rows[0], name="=HYPERLINK(\"http://x\")")
AUD.export_csv([evil_row] + rows, csv_path)
with open(csv_path, encoding="utf-8-sig") as _f:
    exported = _f.read().splitlines()
check(exported[0] == "When,Who,Action,Kind,Name,Details,Id" and len(exported) == len(rows) + 2
      and "'=HYPERLINK" in exported[1] and ",Deployment," in exported[1], "export to CSV, safe to open in Excel")
check(AUD.matches(rows[0], "dominic deployment") and not AUD.matches(rows[0], "nobody"), "audit search")
with open(os.path.join(astore.root, AUD.FILE), "a") as _f:
    _f.write("not json\n")
check(len(AUD.read(astore)) == len(rows), "a damaged line is skipped, the rest still read")

# --- 19. Migration: the client's baseline on the new PC, and the domain account's notes in the report ---------------
from sectorsmith.integrations.ad import ADUser  # noqa: E402

bstore = Store(os.path.join(W, "lib19"))
set_inventory()
bl_pkg = installer_pkg(bstore, "Baseline Office", "2.0")
mig_client = bstore.upsert("clients", Client(name="Mig Co"))
bstore.set_baseline(mig_client.id, [bl_pkg.id])
prof19 = os.path.join(W, "mig19_src", "sam")
os.makedirs(os.path.join(prof19, "Desktop"), exist_ok=True)
with open(os.path.join(prof19, "Desktop", "notes.txt"), "w") as _f:
    _f.write("x")
dst19 = os.path.join(W, "mig19_dst", "sam")
user = ADUser(sam="sam", home_drive="H:", home_folder="\\\\fs1\\home\\sam", logon_script="logon.bat")
plan19 = migrate.Plan(LocalEndpoint(), prof19, WinCodes(), dst19, desk, store=bstore, baseline_client=mig_client.id,
                      domain_notes=user.migration_notes())
res = migrate.run(plan19, Progress(1))
b_ = res["baseline"]
with open(res["report"]) as fh:
    rep = fh.read()
check(b_ and b_["client"] == "Mig Co" and [(a["name"], a["status"]) for a in b_["actions"]] ==
      [("Baseline Office", "compliant")] and inventory().get("Baseline Office") == "2.0"
      and host.lower() in [m_.lower() for m_ in bstore.get("clients", mig_client.id).machines],
      "the new PC joins the client and gets its baseline at the end of the move")
check("Mig Co baseline: 1 of 1 packages in place" in rep and "H: maps to \\\\fs1\\home\\sam" in rep
      and "logon.bat" in rep and os.path.exists(os.path.join(dst19, "Desktop", "notes.txt")),
      "the report has the baseline and the domain account's notes; the files came across")
check(any(s_.get("trigger") == "onboarding" for s_ in bstore.sessions()), "the baseline ran as an onboarding session")
taken = bstore.upsert("clients", Client(name="Other Co"))
bstore.set_baseline(taken.id, [bl_pkg.id])
res = migrate.run(migrate.Plan(LocalEndpoint(), prof19, WinCodes(), dst19, desk, store=bstore,
                               baseline_client=taken.id), Progress(1))
check("already in Mig Co" in res["baseline"]["error"] and "not installed" in migrate.baseline_lines(
    res["baseline"])[0] and not res["failed"], "a PC in another client: the baseline is refused with why, files "
                                                 "still copied")

print(f"\n{sum(OK)}/{len(OK)} deploy checks passed")
sys.exit(0 if all(OK) else 1)
