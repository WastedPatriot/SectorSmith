"""Active Directory user lookup on this PC's own domain, with this PC's own sign-in (no stored credentials).

ADSI through Windows PowerShell's [adsisearcher]. The script is fixed text; the LDAP filter is built here, escaped
by RFC 4515 rules, and handed over in an environment variable, so a user name is never part of the script."""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import subprocess  # nosec B404
import sys
import threading
import time
from dataclasses import dataclass, field

from ..util import Cancelled, cancel_scope, check_cancel, get_logger

log = get_logger()

FILTER_ENV = "SECTORSMITH_AD_FILTER"
TIMEOUT = 25

STATUS_TEXT = {
    "ok": "Found in the domain",
    "not_found": "No domain account with that name",
    "not_joined": "This PC isn't joined to a domain",
    "unavailable": "Active Directory lookup isn't available here",
    "error": "The domain lookup failed",
}

SCRIPT = r"""
$ErrorActionPreference = 'Stop'
function Say($o) { $o | ConvertTo-Json -Depth 4 -Compress }
try { $cs = Get-CimInstance -ClassName Win32_ComputerSystem } catch { $cs = Get-WmiObject -Class Win32_ComputerSystem }
if (-not $cs.PartOfDomain) { Say @{ status = 'not_joined' }; exit 0 }
$domain = "$($cs.Domain)"
try {
    $s = [adsisearcher]"$env:SECTORSMITH_AD_FILTER"
    $s.ClientTimeout = [TimeSpan]::FromSeconds(15)
    $s.ServerTimeLimit = [TimeSpan]::FromSeconds(15)
    $s.SizeLimit = 5
    [void]$s.PropertiesToLoad.AddRange([string[]]@('samaccountname', 'displayname', 'mail', 'department',
        'useraccountcontrol', 'lastlogontimestamp', 'memberof', 'homedrive', 'homedirectory', 'scriptpath',
        'userprincipalname', 'distinguishedname'))
    $found = $s.FindAll()
} catch {
    Say @{ status = 'unavailable'; domain = $domain; message = "$($_.Exception.Message)" }; exit 0
}
$users = @()
foreach ($r in $found) {
    $p = $r.Properties
    $u = @{}
    foreach ($n in @('samaccountname', 'displayname', 'mail', 'department', 'useraccountcontrol',
                     'lastlogontimestamp', 'homedrive', 'homedirectory', 'scriptpath', 'userprincipalname',
                     'distinguishedname')) {
        if ($p[$n].Count) { $u[$n] = "$($p[$n][0])" } else { $u[$n] = '' }
    }
    $u['memberof'] = @($p['memberof'] | ForEach-Object { "$_" })
    $users += $u
}
Say @{ status = 'ok'; domain = $domain; users = $users }
"""


@dataclass
class ADUser:
    sam: str
    display_name: str = ""
    email: str = ""
    department: str = ""
    enabled: bool = True
    last_logon: float | None = None    # lastLogonTimestamp: replicated, can lag by up to two weeks
    groups: list = field(default_factory=list)
    home_drive: str = ""
    home_folder: str = ""
    logon_script: str = ""
    upn: str = ""
    dn: str = ""

    def migration_notes(self) -> list[str]:
        """What a migration should tell the technician: things that live on the server, not in the profile."""
        notes = []
        if self.home_folder:
            where = f"{self.home_drive} maps to {self.home_folder}" if self.home_drive \
                else f"Home folder is {self.home_folder}"
            notes.append(f"{where} on the server, so it isn't copied. It maps again when {self.sam} signs in "
                         "on the new PC.")
        if self.logon_script:
            notes.append(f"Logon script {self.logon_script} runs at sign-in and may map drives or printers. "
                         "Check that the new PC can reach it.")
        if not self.enabled:
            notes.append(f"The domain account {self.sam} is disabled, so it can't sign in to the new PC.")
        return notes


@dataclass
class ADLookup:
    status: str                  # ok | not_found | not_joined | unavailable | error
    message: str = ""
    domain: str = ""
    user: ADUser | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok" and self.user is not None

    def text(self) -> str:
        base = STATUS_TEXT.get(self.status, self.status)
        return f"{base}: {self.message}" if self.message and self.status != "ok" else base


# ---------------------------------------------------------------------------- LDAP filter
def ldap_escape(value: str) -> str:
    """Escape a value for an LDAP search filter (RFC 4515): * ( ) \\ and NUL become \\2a \\28 \\29 \\5c \\00."""
    out = []
    for ch in str(value):
        if ch in "*()\\\x00":
            out.append("\\%02x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def user_filter(names) -> str:
    """Filter for user objects whose sAMAccountName is one of names (each escaped)."""
    parts = [f"(sAMAccountName={ldap_escape(n)})" for n in names]
    match = parts[0] if len(parts) == 1 else "(|" + "".join(parts) + ")"
    return f"(&(objectCategory=person)(objectClass=user){match})"


def candidates(name: str) -> list[str]:
    """sAMAccountName guesses for a profile folder: 'alice' and, for 'alice.CORP' or 'alice.000', 'alice'."""
    n = os.path.basename((name or "").replace("\\", "/").rstrip("/")).strip()
    out = [n] if n else []
    if "." in n:
        head = n.rsplit(".", 1)[0]
        if head and head not in out:
            out.append(head)
    return [c for c in out if len(c) <= 256]


# ---------------------------------------------------------------------------- output parsing
def group_name(dn: str) -> str:
    """'CN=Sales\\, North,OU=Groups,DC=corp,DC=local' -> 'Sales, North'."""
    rdn, esc = [], False
    for ch in dn:
        if esc:
            rdn.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ",":
            break
        else:
            rdn.append(ch)
    text = "".join(rdn)
    return text.split("=", 1)[1] if "=" in text else text


def filetime(value) -> float | None:
    """AD FILETIME (100 ns ticks since 1601) to a Unix timestamp. None for 0 or never."""
    try:
        v = int(str(value).strip() or 0)
    except ValueError:
        return None
    if v <= 0 or v >= 0x7FFFFFFFFFFFFFFF:
        return None
    return (dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(microseconds=v // 10)).timestamp()


def _user(d: dict) -> ADUser:
    def s(k):
        v = d.get(k)
        return "" if v is None else str(v).strip()
    try:
        uac = int(s("useraccountcontrol") or 0)
    except ValueError:
        uac = 0
    groups = d.get("memberof") or []
    if isinstance(groups, str):
        groups = [groups]
    return ADUser(sam=s("samaccountname"), display_name=s("displayname"), email=s("mail"),
                  department=s("department"), enabled=not uac & 2, last_logon=filetime(s("lastlogontimestamp")),
                  groups=sorted({group_name(str(g)) for g in groups if g}, key=str.lower),
                  home_drive=s("homedrive"), home_folder=s("homedirectory"), logon_script=s("scriptpath"),
                  upn=s("userprincipalname"), dn=s("distinguishedname"))


def parse_output(text: str, wanted=()) -> ADLookup:
    """The script's JSON line to an ADLookup. With several matches, the first of wanted wins."""
    line = next((ln for ln in reversed((text or "").splitlines()) if ln.strip().startswith("{")), "")
    try:
        data = json.loads(line.strip().lstrip("﻿"))
    except ValueError:
        return ADLookup("error", "PowerShell gave no readable answer.")
    if not isinstance(data, dict):
        return ADLookup("error", "PowerShell gave no readable answer.")
    status = str(data.get("status") or "error")
    domain = str(data.get("domain") or "")
    if status != "ok":
        if status not in STATUS_TEXT:
            status = "error"
        return ADLookup(status, str(data.get("message") or "").strip(), domain)
    users = data.get("users") or []
    if isinstance(users, dict):  # one result: PowerShell 5 may unwrap the array
        users = [users]
    found = [_user(u) for u in users if isinstance(u, dict)]
    if not found:
        return ADLookup("not_found", "", domain)
    order = [w.lower() for w in wanted]
    found.sort(key=lambda u: order.index(u.sam.lower()) if u.sam.lower() in order else len(order))
    return ADLookup("ok", "", domain, found[0])


# ---------------------------------------------------------------------------- PowerShell
def run_powershell(script: str, env: dict, timeout: float):
    """Run a fixed script with Windows PowerShell. Values travel in env, never in the command line or the script.
    Cancel (check_cancel) or the timeout stops it. Returns (exit code, stdout, stderr)."""
    if sys.platform != "win32":
        raise FileNotFoundError("Windows PowerShell")
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,  # nosec B603
                         env={**os.environ, **env}, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    t0 = time.monotonic()
    while True:
        try:
            out, err = p.communicate(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            stop = time.monotonic() - t0 > timeout
            try:
                check_cancel()
            except Cancelled:
                p.kill()
                p.communicate()
                raise
            if stop:
                p.kill()
                p.communicate()
                raise TimeoutError(f"no answer after {int(timeout)} s")
    dec = (lambda b: (b or b"").decode("utf-8", "replace"))
    return p.returncode, dec(out), dec(err)


def lookup_user(name: str, runner=None, timeout: float = TIMEOUT, cancel: threading.Event | None = None) -> ADLookup:
    """Look a user up by sAMAccountName (or a profile folder name) in this PC's domain. Never raises, except
    Cancelled when cancel is set. runner(script, env, timeout) -> (code, out, err) replaces PowerShell in tests."""
    names = candidates(name)
    if not names:
        return ADLookup("not_found", "No user name to look up.")
    if runner is None:
        if sys.platform != "win32":
            return ADLookup("unavailable", "Needs Windows.")
        runner = run_powershell

    def check():
        if cancel is not None and cancel.is_set():
            raise Cancelled("Domain lookup cancelled")
    with cancel_scope(check):
        check()
        try:
            code, out, err = runner(SCRIPT, {FILTER_ENV: user_filter(names)}, timeout)
        except Cancelled:
            raise
        except TimeoutError as e:
            return ADLookup("error", f"The domain didn't answer ({e}).")
        except FileNotFoundError:
            return ADLookup("unavailable", "Windows PowerShell wasn't found.")
        except OSError as e:
            return ADLookup("unavailable", str(e))
        check()
    res = parse_output(out, names)
    if res.status == "error" and err.strip():
        res.message = err.strip().splitlines()[-1][:200]
    log.info("AD lookup %s: %s", names[0], res.status)
    return res
