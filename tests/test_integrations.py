"""Integrations: ScreenConnect URLs and the installed client, the Active Directory lookup (fake PowerShell), LDAP
escaping and the DPAPI secret store. Needs no fixture, no network and no domain.

Usage: python tests/test_integrations.py
"""
import json
import os
import shutil
import sys
import tempfile
import threading
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["LOCALAPPDATA"] = tempfile.mkdtemp(prefix="ss_integ_")  # keep secrets/settings away from the real ones

from sectorsmith.integrations import ad, dpapi, screenconnect as sc  # noqa: E402
from sectorsmith.util import Cancelled, app_dir  # noqa: E402

OK = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    OK.append(bool(cond))


def raises(fn, exc):
    try:
        fn()
    except exc as e:
        return e
    return None


# --- ScreenConnect: instance URL -------------------------------------------------------------------
check(sc.normalize_url("https://Control.Example.com/") == "https://control.example.com", "instance URL normalized")
check(sc.normalize_url("control.example.com") == "https://control.example.com", "bare host gets https://")
check(sc.normalize_url("https://sc.example.com:8040/sc/Host") == "https://sc.example.com:8040/sc",
      "port and path kept, pasted /Host page trimmed")
check(sc.normalize_url("https://x.example.com:443") == "https://x.example.com", "default port dropped")
for bad, why in [("http://control.example.com", "plain http"), ("ftp://x.example.com", "other scheme"),
                 ("https://user:pw@x.example.com", "credentials in URL"), ("https://x.example.com/?a=1", "query"),
                 ("https://x.example.com/#Host", "fragment"), ("https://", "no host"), ("", "empty"),
                 ("https://exa mple.com", "space in host"), ("https://x.example.com:99999", "bad port"),
                 ("javascript:alert(1)", "javascript scheme"), ("https://x.example.com/a b", "space in path")]:
    check(raises(lambda b=bad: sc.normalize_url(b), ValueError) is not None, f"instance URL refused: {why}")

# --- ScreenConnect: host page URL and quoting --------------------------------------------------------
u = sc.host_url("https://control.example.com", "ACME-PC01")
check(u == "https://control.example.com/Host#Access/All%20Machines/ACME-PC01", "host page URL: " + u)
evil = "PC 1/../#x?y=1&z=%41\n"
u = sc.host_url("https://control.example.com/", evil)
parts = urlsplit(u)
check(parts.netloc == "control.example.com" and parts.path == "/Host" and not parts.query,
      "machine name can't change host, path or query")
seg = parts.fragment.split("/")
check(len(seg) == 3 and unquote(seg[2]) == "PC 1/../#x?y=1&z=%41", "machine name is one quoted search term")
check("\n" not in u and " " not in u, "no raw whitespace or line breaks in the URL")
check(raises(lambda: sc.host_url("https://c.example.com", "  "), ValueError) is not None, "empty name refused")
check(raises(lambda: sc.host_url("http://c.example.com", "PC"), ValueError) is not None,
      "host_url re-validates the instance")
opened = []
url = sc.open_machine("https://control.example.com", "Front Desk", opener=opened.append)
check(opened == [url] and url.endswith("/Front%20Desk"), "open_machine hands the URL to the browser")

# --- ScreenConnect: settings ---------------------------------------------------------------------
st = {}
check(not sc.configured(st) and sc.instance_for(st, "Acme") is None, "off until configured")
st = {sc.KEY_URL: "https://msp.example.com", sc.KEY_CLIENTS: {"Acme": "acme.example.com", "Bad": "http://x"}}
check(sc.configured(st), "configured with an instance")
check(sc.instance_for(st, "Acme") == "https://acme.example.com", "per-client instance override wins")
check(sc.instance_for(st, "Other") == "https://msp.example.com", "other clients use the default instance")
check(sc.instance_for(st, "Bad") == "https://msp.example.com", "invalid override falls back to the default")
check(sc.instance_for(st, None) == "https://msp.example.com", "no client: default instance")

# --- ScreenConnect: installed client (read-only) --------------------------------------------------
IMG = ('"C:\\Program Files (x86)\\ScreenConnect Client (0a1b2c3d4e5f6789)\\ScreenConnect.ClientService.exe" '
       '"?e=Access&y=Guest&h=relay.example.com&p=8041&s=6f9a1c3e-1111-2222-3333-444455556666'
       '&k=BgIAAACkAABSU0ExAAgAAAEAAQ&c=Acme%20Ltd&c=London&c=&c=Laptop&c=&c=&c=&c="')
c = sc.parse_client_imagepath("ScreenConnect Client (0a1b2c3d4e5f6789)", IMG)
check(c and c["host"] == "relay.example.com" and c["port"] == "8041", "client: relay host and port")
check(c and c["session"] == "6f9a1c3e-1111-2222-3333-444455556666", "client: session id")
check(c and c["instance_id"] == "0a1b2c3d4e5f6789", "client: instance id from the service name")
check(c and c["properties"] == [("Company", "Acme Ltd"), ("Site", "London"), ("Device type", "Laptop")],
      "client: custom properties, blanks dropped")
check(c and "k" not in c and "BgIAAA" not in json.dumps(c), "client: key value not kept")
check(sc.parse_client_imagepath("Other", '"C:\\x.exe" -service') is None, "not a ScreenConnect command line")
found = sc.local_clients(reader=lambda: [("ScreenConnect Client (0a1b2c3d4e5f6789)", IMG), ("x", "nothing")])
check(len(found) == 1 and "relay.example.com" in sc.describe_client(found[0]), "local_clients with a fake registry")
check(sys.platform == "win32" or sc.local_clients() == [], "no registry off Windows: no clients")

# --- LDAP escaping (RFC 4515) -----------------------------------------------------------------------
check(ad.ldap_escape("alice") == "alice", "plain name unchanged")
check(ad.ldap_escape("*") == "\\2a", "* escaped")
check(ad.ldap_escape("a(b)c") == "a\\28b\\29c", "( ) escaped")
check(ad.ldap_escape("a\\b") == "a\\5cb", "backslash escaped")
check(ad.ldap_escape("a\x00b") == "a\\00b", "NUL escaped")
check(ad.ldap_escape("*)(objectClass=*)(\\") == "\\2a\\29\\28objectClass=\\2a\\29\\28\\5c", "injection attempt")
check(ad.ldap_escape("Zoë.O'Neil") == "Zoë.O'Neil", "accents and quotes stay as they are")
f = ad.user_filter(["*)(|(cn=*"])
check(f == "(&(objectCategory=person)(objectClass=user)(sAMAccountName=\\2a\\29\\28|\\28cn=\\2a))",
      "filter for one name: " + f)
check(f.count("(") == f.count(")"), "filter parentheses stay balanced")
check(ad.user_filter(["alice.CORP", "alice"]).endswith("(|(sAMAccountName=alice.CORP)(sAMAccountName=alice)))"),
      "two candidates become an OR")
check(ad.candidates("C:\\Users\\alice.CORP") == ["alice.CORP", "alice"], "profile folder candidates")
check(ad.candidates("bob") == ["bob"] and ad.candidates("") == [], "single and empty candidates")

# --- AD output parsing -------------------------------------------------------------------------
FT = 133700000000000000  # 2024-09-02
OUT = json.dumps({"status": "ok", "domain": "corp.example.com", "users": [{
    "samaccountname": "alice", "displayname": "Alice Smith", "mail": "alice@example.com", "department": "Sales",
    "useraccountcontrol": "512", "lastlogontimestamp": str(FT), "homedrive": "H:",
    "homedirectory": "\\\\fs01\\home$\\alice", "scriptpath": "logon.bat", "userprincipalname": "alice@corp.example.com",
    "distinguishedname": "CN=Alice Smith,OU=Staff,DC=corp,DC=example,DC=com",
    "memberof": ["CN=Sales\\, North,OU=Groups,DC=corp,DC=example,DC=com", "CN=VPN Users,CN=Users,DC=corp"]}]})
r = ad.parse_output("WARNING: noise\r\n" + OUT + "\r\n", ["alice"])
check(r.ok and r.domain == "corp.example.com", "parse: ok with domain")
uu = r.user
check(uu.display_name == "Alice Smith" and uu.email == "alice@example.com" and uu.department == "Sales",
      "parse: name, email, department")
check(uu.enabled and uu.home_drive == "H:" and uu.home_folder == "\\\\fs01\\home$\\alice"
      and uu.logon_script == "logon.bat", "parse: enabled, home drive and folder, logon script")
check(uu.groups == ["Sales, North", "VPN Users"], "parse: group names from DNs (escaped comma)")
check(uu.last_logon and abs(uu.last_logon - 1725526400) < 86400 * 3, "parse: last logon from FILETIME")
notes = uu.migration_notes()
check(len(notes) == 2 and "H: maps to \\\\fs01\\home$\\alice" in notes[0] and "logon.bat" in notes[1],
      "migration notes mention the home drive and logon script")
dis = json.loads(OUT)
dis["users"][0].update(useraccountcontrol="514", lastlogontimestamp="0", memberof="CN=Solo,DC=x",
                       homedirectory="", scriptpath="")
r2 = ad.parse_output(json.dumps({**dis, "users": dis["users"][0]}))
check(r2.ok and not r2.user.enabled and r2.user.last_logon is None and r2.user.groups == ["Solo"],
      "parse: disabled, never logged on, single group and unwrapped array")
check(len(r2.user.migration_notes()) == 1 and "disabled" in r2.user.migration_notes()[0],
      "migration note for a disabled account")
two = json.loads(OUT)
two["users"] = [dict(two["users"][0], samaccountname="alice.CORP"), two["users"][0]]
check(ad.parse_output(json.dumps(two), ["alice", "alice.CORP"]).user.sam == "alice", "first wanted name wins")
check(ad.parse_output('{"status":"ok","domain":"d","users":[]}').status == "not_found", "parse: no users")
check(ad.parse_output('{"status":"not_joined"}').status == "not_joined", "parse: not domain joined")
r3 = ad.parse_output('{"status":"unavailable","domain":"d","message":"The server is not operational."}')
check(r3.status == "unavailable" and "not operational" in r3.text(), "parse: ADSI unavailable with reason")
check(ad.parse_output("garbage").status == "error", "parse: unreadable output")
check(ad.parse_output('{"status":"weird"}').status == "error", "parse: unknown status")


# --- AD lookup through a fake PowerShell runner -------------------------------------------------------------
class FakePS:
    def __init__(self, out="", code=0, err="", exc=None, wait=None):
        self.out, self.code, self.err, self.exc, self.wait = out, code, err, exc, wait
        self.calls = []

    def __call__(self, script, env, timeout):
        self.calls.append((script, dict(env), timeout))
        if self.wait:
            self.wait()
        if self.exc:
            raise self.exc
        return self.code, self.out, self.err


ps = FakePS(OUT)
r = ad.lookup_user("alice", runner=ps, timeout=7)
check(r.ok and r.user.sam == "alice", "lookup: found")
script, env, to = ps.calls[0]
check(script == ad.SCRIPT and to == 7, "lookup: fixed script, timeout passed through")
check(env == {ad.FILTER_ENV: ad.user_filter(["alice"])}, "lookup: filter travels in an environment variable")
ps = FakePS(OUT)
ad.lookup_user("x*)(cn=*", runner=ps)
check("x*)(cn=*" not in ps.calls[0][0] and "x\\2a\\29\\28cn=\\2a" in ps.calls[0][1][ad.FILTER_ENV],
      "lookup: user name never in the script, escaped in the filter")
check(ad.lookup_user("alice", runner=FakePS('{"status":"not_joined"}')).status == "not_joined",
      "lookup: not domain joined")
check(ad.lookup_user("alice", runner=FakePS(exc=FileNotFoundError())).status == "unavailable",
      "lookup: no PowerShell is 'unavailable'")
r = ad.lookup_user("alice", runner=FakePS(exc=TimeoutError("no answer after 25 s")))
check(r.status == "error" and "25 s" in r.message, "lookup: timeout reported")
r = ad.lookup_user("alice", runner=FakePS("", 1, "Access denied\r\n"))
check(r.status == "error" and "Access denied" in r.message, "lookup: PowerShell error text kept")
check(ad.lookup_user("", runner=FakePS(OUT)).status == "not_found", "lookup: empty name")
if sys.platform != "win32":
    check(ad.lookup_user("alice").status == "unavailable", "lookup: off Windows is 'unavailable', nothing runs")
ev = threading.Event()
ev.set()
ps = FakePS(OUT)
check(raises(lambda: ad.lookup_user("alice", runner=ps, cancel=ev), Cancelled) is not None and not ps.calls,
      "lookup: cancelled before it starts")
ev2 = threading.Event()
check(raises(lambda: ad.lookup_user("alice", runner=FakePS(OUT, wait=ev2.set), cancel=ev2), Cancelled)
      is not None, "lookup: cancel while PowerShell runs wins over its answer")


def cancel_inside():
    from sectorsmith.util import check_cancel
    ev3 = threading.Event()

    def runner(script, env, timeout):
        ev3.set()
        check_cancel()  # the real runner polls this while it waits
        return 0, OUT, ""
    return ad.lookup_user("alice", runner=runner, cancel=ev3)


check(raises(cancel_inside, Cancelled) is not None, "lookup: runner sees the cancel scope")

# --- DPAPI secret store -------------------------------------------------------------------------
store = app_dir() / dpapi.STORE
if sys.platform == "win32":
    dpapi.save_secret("test", "s3cret")
    check("s3cret" not in store.read_text(), "DPAPI: secret not stored in plain text")
    check(dpapi.load_secret("test") == "s3cret", "DPAPI: round trip")
    dpapi.forget_secret("test")
    check(dpapi.load_secret("test") is None, "DPAPI: forgotten")
else:
    check(not dpapi.available(), "DPAPI: not available off Windows")
    check(raises(lambda: dpapi.save_secret("api", "s3cret"), dpapi.SecretsUnavailable) is not None,
          "DPAPI fallback refuses to save a secret")
    check(not store.exists(), "DPAPI fallback wrote nothing to disk")
    check(raises(lambda: dpapi.protect(b"x"), dpapi.SecretsUnavailable) is not None, "protect refused")
    check(dpapi.load_secret("api") is None, "load_secret: nothing there")

shutil.rmtree(os.environ["LOCALAPPDATA"], ignore_errors=True)
print(f"\n{sum(OK)}/{len(OK)} passed")
sys.exit(0 if all(OK) else 1)
