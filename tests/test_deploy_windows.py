"""SectorSmith Deploy on real Windows (run as Administrator; used by CI on windows-latest).

Builds a small test MSI (1.2.3, then 1.3.0 as a major upgrade) with msilib and a script package that writes a
registry key, then runs maintenance sessions through a real SectorSmith Link agent on this PC: check, install,
an idempotent re-run, the upgrade, removal, the exported Detect.ps1 scripts, and an app install from a user
migration. Nothing is downloaded, and everything it installs is removed again at the end.
"""
import dataclasses
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import types
import warnings

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)

CI = bool(os.environ.get("GITHUB_ACTIONS"))


def skip(why):
    """Skip cleanly, except in the CI Windows job, where not running is a failure."""
    strict = CI and sys.platform == "win32"
    print(f"{'FAIL' if strict else 'SKIP'} test_deploy_windows.py: {why}")
    sys.exit(1 if strict else 0)


if sys.platform != "win32":
    skip("needs Windows as Administrator (it installs a test MSI); it runs in the CI Windows job")

import winreg  # noqa: E402

from sectorsmith.deploy import analyze as A  # noqa: E402
from sectorsmith.deploy import core  # noqa: E402
from sectorsmith.deploy.core import Deployment, Package, Store  # noqa: E402
from sectorsmith.link import apps as mapps  # noqa: E402
from sectorsmith.link.endpoint import LocalEndpoint  # noqa: E402
from sectorsmith.link.server import LinkServer  # noqa: E402
from sectorsmith.util import Progress, is_admin  # noqa: E402

if not is_admin():
    skip("run it as Administrator (it installs into Program Files and HKLM)")
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    try:
        import msilib
        from msilib import schema, sequence
    except ImportError:
        msilib = None
if msilib is None:
    skip(f"msilib is not available in Python {sys.version.split()[0]} (removed in 3.13); use Python 3.12")
if not msilib.AMD64:
    skip("needs 64-bit Python (the test MSI is an x64 package)")

NAME = "SectorSmith Test App"
SNAME = "SectorSmith Test Script"
PC1 = "{3F2B6C1A-7D4E-4A8B-9C10-5E6F7A8B9C01}"
PC2 = "{3F2B6C1A-7D4E-4A8B-9C10-5E6F7A8B9C02}"
UPGRADE = "{3F2B6C1A-7D4E-4A8B-9C10-5E6F7A8B9CFF}"
COMPONENT = "{3F2B6C1A-7D4E-4A8B-9C10-5E6F7A8B9CC0}"
UNINST = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
TEST_KEY = r"SOFTWARE\SectorSmithTest"
APP_FILE = os.path.join(os.environ.get("ProgramW6432") or os.environ["ProgramFiles"], "SectorSmithTestApp",
                        "readme.txt")
W = os.path.join(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()), "ss deploy win")  # a space on purpose
OK = []


def check(c, m):
    print(("PASS " if c else "FAIL ") + m, flush=True)
    OK.append(bool(c))


# --- fixtures ----------------------------------------------------------------------------
def build_msi(path, version, product_code):
    """Per-machine x64 MSI: one text file in Program Files, ARP entry, major upgrade of older versions."""
    src = os.path.join(W, f"payload {version}")
    os.makedirs(src, exist_ok=True)
    payload = os.path.join(src, "readme.txt")
    with open(payload, "w") as f:
        f.write(f"{NAME} {version}\n")
    msilib._directories.clear()  # module-level: a second database would get TARGETDIR1
    db = msilib.init_database(path, schema, NAME, product_code, version, "SectorSmith Tests")
    msilib.add_data(db, "Property", [("UpgradeCode", UPGRADE), ("ALLUSERS", "1"),
                                     ("SecureCustomProperties", "OLDPRODUCTFOUND")])
    own = {"FindRelatedProducts", "RemoveExistingProducts"}
    seq = types.SimpleNamespace(tables=sequence.tables, **{t: [r for r in getattr(sequence, t) if r[0] not in own]
                                                           for t in sequence.tables})
    seq.InstallExecuteSequence = seq.InstallExecuteSequence + [("FindRelatedProducts", None, 25),
                                                               ("RemoveExistingProducts", None, 1401)]
    seq.InstallUISequence = seq.InstallUISequence + [("FindRelatedProducts", None, 25)]
    msilib.add_tables(db, seq)
    # every older version of this UpgradeCode is removed before the new one goes on
    msilib.add_data(db, "Upgrade", [(UPGRADE, "0.0.0", version, None, 257, None, "OLDPRODUCTFOUND")])
    cab = msilib.CAB("payload")
    root = msilib.Directory(db, cab, None, src, "TARGETDIR", "SourceDir")
    pf = msilib.Directory(db, cab, root, ".", "ProgramFiles64Folder", "PFiles")
    app = msilib.Directory(db, cab, pf, ".", "INSTALLDIR", "SSTEST|SectorSmithTestApp")
    feat = msilib.Feature(db, "Complete", NAME, "Test payload", 1, directory="INSTALLDIR")
    feat.set_current()
    app.start_component("MainFile", feat, 0, keyfile="readme.txt", uuid=COMPONENT)
    app.add_file("readme.txt", src=payload)
    cab.commit(db)
    db.Commit()
    db.Close()


def arp(product_code, view=winreg.KEY_WOW64_64KEY):
    """(DisplayName, DisplayVersion) of an Add/Remove Programs entry, read independently of SectorSmith."""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, f"{UNINST}\\{product_code}", 0, winreg.KEY_READ | view) as k:
            return winreg.QueryValueEx(k, "DisplayName")[0], winreg.QueryValueEx(k, "DisplayVersion")[0]
    except OSError:
        return None


def test_key():
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, TEST_KEY, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, "Version")[0]
    except OSError:
        return None


def app_file():
    try:
        with open(APP_FILE) as f:
            return f.read().strip()
    except OSError:
        return None


def ps(script):
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
                        script], capture_output=True, text=True, timeout=180)
    if r.stderr.strip():
        print("   stderr:", r.stderr.strip()[-400:])
    return r.returncode, r.stdout.strip()


def msiexec(*args):
    return subprocess.run(["msiexec.exe", *args, "/qn", "/norestart"], capture_output=True, timeout=600).returncode


def cleanup():
    codes = [msiexec("/x", pc) for pc in (PC2, PC1)]
    try:
        winreg.DeleteKeyEx(winreg.HKEY_LOCAL_MACHINE, TEST_KEY, winreg.KEY_WOW64_64KEY, 0)
    except OSError:
        pass  # key not there, nothing to clean
    shutil.rmtree(os.path.dirname(APP_FILE), ignore_errors=True)
    return codes


def msi_log(msi):
    """Install once more with a verbose log and print its end (only when an install failed)."""
    lp = os.path.join(W, "msi.log")
    code = msiexec("/i", msi, "ALLUSERS=1", "/l*v", lp)
    try:
        with open(lp, "rb") as f:
            raw = f.read()
        text = raw.decode("utf-16", "replace") if raw[:2] == b"\xff\xfe" else raw.decode("mbcs", "replace")
        print(f"   msiexec exit {code}, log tail:\n" + "\n".join(text.splitlines()[-40:]))
    except OSError as e:
        print(f"   msiexec exit {code}, no log ({e})")


def by(sess):
    return {a["name"]: a for a in sess["actions"]}


def show(sess):
    for a in sess["actions"]:
        print(f"   {a['name']}: {a['action']} -> {a.get('status')} ({a.get('result', '')})")
        if a.get("status") == "failed" and a.get("log"):
            print("   " + a["log"][-1500:].replace("\n", "\n   "))


def full(store):
    s = core.run_session(remote, store, Progress(1), mode="full")
    show(s)
    return s


# --- setup ---------------------------------------------------------------------------------
shutil.rmtree(W, ignore_errors=True)
os.makedirs(W)
print("cleaning up any earlier run:", cleanup())
check(arp(PC1) is None and arp(PC2) is None and test_key() is None and app_file() is None,
      "this PC starts without the test app, file or registry key")

msi1 = os.path.join(W, "src", f"{NAME} 1.2.3.msi")
msi2 = os.path.join(W, "src", f"{NAME} 1.3.0.msi")
os.makedirs(os.path.dirname(msi1))
build_msi(msi1, "1.2.3", PC1)
build_msi(msi2, "1.3.0", PC2)
check(os.path.getsize(msi1) > 10_000 and os.path.getsize(msi2) > 10_000, "test MSIs built with msilib")

connected = []
srv = LinkServer(on_connect=connected.append, port=47450)
srv.start()
env = dict(os.environ, LOCALAPPDATA=os.path.join(W, "agent appdata"))
env.pop("SECTORSMITH_FAKE_SOFTWARE", None)
agent_log = open(os.path.join(W, "agent.log"), "wb")
ag = subprocess.Popen([sys.executable, "-m", "sectorsmith", "--agent", "--headless", "--no-elevate", "--connect",
                       f"127.0.0.1:{srv.port}", "--token", srv.token, "--pin", srv.fingerprint], cwd=ROOT, env=env,
                      stdout=agent_log, stderr=subprocess.STDOUT)
end = time.time() + 30
while not connected and time.time() < end:
    time.sleep(.2)
check(connected, "Link agent connected over TLS on localhost")
remote = connected[0] if connected else None

try:
    if remote is None:
        raise RuntimeError("no agent, nothing to test")
    info = remote.info()
    host = info["hostname"]
    check(info["admin"] and host.lower() == socket.gethostname().lower(), f"agent runs elevated on this PC ({host})")

    # 1. installer analysis on a real MSI
    s = A.analyze(msi1)
    print("   analyze:", {k: s[k] for k in ("kind", "name", "version", "product_code", "install", "uninstall",
                                          "detection")})
    check(s["kind"] == "msi" and s["name"] == NAME and s["version"] == "1.2.3" and s["product_code"] == PC1
          and s["upgrade_code"] == UPGRADE and s["publisher"] == "SectorSmith Tests",
          "analyze reads name, version, publisher, ProductCode and UpgradeCode from the MSI")
    check(s["install"] == 'msiexec.exe /i "{installer}" /qn /norestart ALLUSERS=1'
          and s["uninstall"] == "msiexec.exe /x {product_code} /qn /norestart" and 3010 in s["success_codes"],
          "analyze proposes msiexec /qn install and uninstall by product code")
    check(s["detection"] == {"method": "registry", "value": NAME},
          "analyze proposes detection by the Add/Remove Programs name (product code kept for msi_product_code)")

    # 2. packages the way the Package Builder saves them, a deployment for this PC
    store = Store(os.path.join(W, "Deploy Library"))
    mpkg = store.add_from_installer(msi1)
    check(mpkg.kind == "msi" and mpkg.product_code == PC1 and os.path.exists(store.installer_path(mpkg)),
          "MSI package added to the library")
    # "+ Script or command": the blank form, filled in (install runs in cmd.exe, detection is PowerShell)
    spkg = store.upsert("packages", Package(
        name=SNAME, kind="script", version="2.0", publisher="SectorSmith Tests",
        install=f'reg add "HKLM\\{TEST_KEY}" /v Version /t REG_SZ /d {{version}} /f /reg:64',
        uninstall=f'reg delete "HKLM\\{TEST_KEY}" /f /reg:64',
        detection={"method": "script", "value": f"$k = Get-ItemProperty -LiteralPath 'HKLM:\\{TEST_KEY}' "
                                                "-ErrorAction SilentlyContinue\nif ($k.Version) { $k.Version }\n"},
        success_codes=[0, 3010, 1641], language="powershell"))
    dm = store.upsert("deployments", Deployment("software", mpkg.id, "latest", target_kind="machine",
                                                target_value=host))
    ds = store.upsert("deployments", Deployment("software", spkg.id, "installed", target_kind="machine",
                                                target_value=host))
    check({d.item_id for d in core.applicable(store, host, False)} == {mpkg.id, spkg.id},
          "both deployments apply to this PC")
    pc_pkg = dataclasses.replace(mpkg, detection={"method": "msi_product_code", "value": ""})

    # 8 (before). exported Detect.ps1 on a PC without the software
    exports = {"name": store.export_package(mpkg, os.path.join(W, "export name")),
               "product code": store.export_package(pc_pkg, os.path.join(W, "export code")),
               "script": store.export_package(spkg, os.path.join(W, "export script"))}
    for k, d in exports.items():
        code, out = ps(os.path.join(d, "Detect.ps1"))
        check(code == 0 and out == "", f"exported Detect.ps1 ({k}) before install: exit {code}, output {out!r}")

    # 4. check mode, then the real thing
    s0 = core.run_session(remote, store, Progress(1), mode="detect")
    b = by(s0)
    check(b[NAME]["action"] == "install" and b[SNAME]["action"] == "install"
          and b[NAME]["status"] == b[SNAME]["status"] == "pending", "check mode: both will install")
    check(arp(PC1) is None and test_key() is None, "check mode changed nothing")
    s1 = full(store)
    b = by(s1)
    if b[NAME]["status"] != "compliant":
        msi_log(msi1)
    check(b[NAME]["status"] == "compliant" and b[NAME]["result"] == "now 1.2.3", f"MSI installed: {b[NAME]['result']}")
    check(b[SNAME]["status"] == "compliant" and b[SNAME]["result"] == "now 2.0",
          f"script package installed: {b[SNAME]['result']}")
    check(arp(PC1) == (NAME, "1.2.3") and arp(PC1, winreg.KEY_WOW64_32KEY) is None,
          f"Add/Remove Programs has it in the 64-bit view only ({arp(PC1)})")
    check(app_file() == f"{NAME} 1.2.3", f"file installed in {os.path.dirname(APP_FILE)}")
    check(test_key() == "2.0", "script wrote HKLM\\SOFTWARE\\SectorSmithTest in the 64-bit view")
    inv = remote.installed_software()
    check(core.detect(remote, mpkg, inv)[:2] == (True, "1.2.3") and core.detect(remote, pc_pkg, inv)[:2] ==
          (True, "1.2.3") and core.detect(remote, spkg, inv)[:2] == (True, "2.0"),
          "detect() agrees: name, product code and script detection")
    here = LocalEndpoint()
    check(core.detect(here, mpkg, here.installed_software())[:2] == (True, "1.2.3"),
          "this PC's own endpoint sees the same")
    check(not os.path.exists(os.path.join(remote.deploy_dir(), mpkg.id)), "uploaded installer removed afterwards")
    check(s1["summary"]["failed"] == 0 and not s1["summary"]["reboot"], f"session summary {s1['summary']}")

    # 8 (after)
    want = {"name": "1.2.3", "product code": "1.2.3", "script": "2.0"}
    for k, d in exports.items():
        code, out = ps(os.path.join(d, "Detect.ps1"))
        check(code == 0 and out == want[k], f"exported Detect.ps1 ({k}) after install: exit {code}, output {out!r}")

    # 5. idempotent
    s2 = full(store)
    check(all(a["action"] == "none" and a["status"] == "compliant" and "log" not in a for a in s2["actions"])
          and len(s2["actions"]) == 2, "second run: nothing to do")

    # 6. major upgrade to 1.3.0
    store.replace_installer(mpkg, msi2)
    check(mpkg.version == "1.3.0" and mpkg.product_code == PC2 and mpkg.installer.endswith("1.3.0.msi"),
          "installer replaced with 1.3.0 (new product code)")
    s3 = full(store)
    b = by(s3)
    if b[NAME]["status"] != "compliant":
        msi_log(msi2)
    check(b[NAME]["action"] == "upgrade" and b[NAME]["status"] == "compliant" and b[NAME]["result"] == "now 1.3.0",
          f"MSI upgraded: {b[NAME]['result']}")
    check(b[SNAME]["action"] == "none", "script package left alone")
    check(arp(PC2) == (NAME, "1.3.0") and arp(PC1) is None, f"1.3.0 registered, 1.2.3 gone ({arp(PC2)}, {arp(PC1)})")
    check(app_file() == f"{NAME} 1.3.0", "file is the 1.3.0 one")
    inv = remote.installed_software()
    check(core.detect(remote, mpkg, inv)[:2] == (True, "1.3.0") and not core.detect(remote, pc_pkg, inv)[0],
          "detect() sees 1.3.0, and the old product code is gone")

    # 7. removal
    for d in (dm, ds):
        d.desired = "uninstalled"
        store.upsert("deployments", d)
    s4 = full(store)
    b = by(s4)
    check(all(b[n]["action"] == "uninstall" and b[n]["status"] == "compliant" and b[n]["result"] == "removed"
              for n in (NAME, SNAME)), "both removed by the session")
    check(arp(PC2) is None and arp(PC1) is None and app_file() is None, "MSI gone from the registry and disk")
    check(test_key() is None, "script package's registry key gone")
    code, out = ps(os.path.join(exports["name"], "Detect.ps1"))
    check(code == 0 and out == "", f"exported Detect.ps1 after removal: exit {code}, output {out!r}")

    # 9. a user migration brings the app across from the library
    rows = mapps.match([{"key": PC2, "name": NAME, "version": "1.3.0", "publisher": "SectorSmith Tests"}], store,
                       remote.installed_software())
    check(len(rows) == 1 and rows[0]["source"] == "library" and rows[0]["package_id"] == mpkg.id,
          f"migration matches the app to the Deploy library ({rows[0]['source'] if rows else None})")
    ms = mapps.build(rows, store, host)
    check([p.id for p in ms.packages] == [mpkg.id] and [d.desired for d in ms.deployments] == ["installed"],
          "migration store holds just that package")
    res = mapps.install(remote, rows, store, Progress(1))
    print("   migration apps:", res["apps"])
    check(res["apps"] and res["apps"][0]["status"] == "compliant" and arp(PC2) == (NAME, "1.3.0")
          and app_file() == f"{NAME} 1.3.0", "migration installed the app through the agent")
    check(res["session"] and os.path.exists(res["session"]), "migration install saved as a Deploy session")
except Exception as e:  # noqa: BLE001
    traceback.print_exc()
    check(False, f"test stopped early: {e}")
finally:
    if remote is not None:
        remote.close()
    try:
        ag.wait(30)
    except subprocess.TimeoutExpired:
        ag.kill()
    srv.stop()
    agent_log.close()
    if not all(OK):
        with open(os.path.join(W, "agent.log"), "rb") as f:
            print("agent output:\n" + f.read().decode("utf-8", "replace")[-3000:])
    codes = cleanup()
    print("cleanup msiexec /x:", codes)
    check(arp(PC1) is None and arp(PC2) is None and test_key() is None and app_file() is None,
          "cleaned up: no test app, file or registry key left")

print(f"\n{sum(OK)}/{len(OK)} Windows Deploy checks passed")
sys.exit(0 if all(OK) else 1)
