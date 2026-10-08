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
        r["code"] = {1603 & 255: 1603, 3010 & 255: 3010}.get(r["code"], r["code"])
        return r


store = Store(os.path.join(W, "lib2"))
host = LocalEndpoint().info()["hostname"]
set_inventory(entry("Foo App", "1.0"), entry("Bar Tool", "3.0"), entry("Old Junk", "1.0"), entry("Pinned", "3.0"),
              entry("Pin Mismatch", "3.0"), entry("Machine Wins", "1.0"))
foo = installer_pkg(store, "Foo App", "2.0")                          # 1.0 installed -> upgrade
bar = installer_pkg(store, "Bar Tool", "3.0")                         # up to date -> nothing
runtime = installer_pkg(store, "Runtime", "1.0")                      # only a prerequisite
newt = installer_pkg(store, "New Thing", "1.5", prerequisites=[runtime.id])
junk = installer_pkg(store, "Old Junk", "1.0")                        # remove via its registry uninstaller
broken = installer_pkg(store, "Broken", "1.0", " --noop 1 --exit 1603")
liar = installer_pkg(store, "Liar", "1.0", " --noop 1")               # exit 0 but never registers
reboot = installer_pkg(store, "Needs Reboot", "1.0", " --exit 3010")
pinned = installer_pkg(store, "Pinned", "2.0")                        # 3.0 installed, pinned to 2.0 -> roll back
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
deploy(store, pinned, "version", version="2.0")
deploy(store, mismatch, "version", version="1.0")
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
check(by["Foo App"]["status"] == "pending" and by["Bar Tool"]["status"] == "compliant"
      and by["Audit only"]["status"] == "non-compliant" and "PowerShell task" not in by,
      "detect statuses: pending / compliant / non-compliant, ignored task skipped")
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
check(set(n for n, a in again.items() if a not in ("none", "audit")) == {"Broken", "Liar", "Can't fix"},
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

print(f"\n{sum(OK)}/{len(OK)} deploy checks passed")
sys.exit(0 if all(OK) else 1)
