"""ScreenConnect (ConnectWise Control): open a machine on the instance's host page, and read the ScreenConnect
client installed on this PC (read-only) to show which instance and session it belongs to.

Settings (settings.json): screenconnect_url is the instance, screenconnect_clients maps a client name to its own
instance when that client has one. Nothing here needs an API key."""
from __future__ import annotations

import re
import sys
import webbrowser
from urllib.parse import parse_qs, quote, urlsplit

GROUP = "All Machines"   # session group the host page searches in
KEY_URL = "screenconnect_url"
KEY_CLIENTS = "screenconnect_clients"
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")
# custom properties in the order the default instance setup names them
PROPERTY_NAMES = ("Company", "Site", "Department", "Device type", "Property 5", "Property 6", "Property 7",
                  "Property 8")


def normalize_url(url: str) -> str:
    """Instance URL as https://host[:port][/path], no trailing slash. Raises ValueError when it isn't usable."""
    url = (url or "").strip()
    if not url:
        raise ValueError("Enter the address of your ScreenConnect instance.")
    if "://" not in url:
        url = "https://" + url
    try:
        u = urlsplit(url)
        port = u.port
    except ValueError:
        raise ValueError("That address isn't a valid URL.") from None
    if u.scheme.lower() != "https":
        raise ValueError("Use an https:// address. ScreenConnect sign-in must not go over plain http.")
    host = u.hostname or ""
    if not host or not _HOST_RE.match(host) or host.startswith(("-", ".")) or ".." in host:
        raise ValueError("That address has no valid host name.")
    if u.username or u.password:
        raise ValueError("Leave the user name and password out of the address.")
    if u.query or u.fragment:
        raise ValueError("Use the instance address only, without ? or # parts.")
    path = u.path.rstrip("/")
    if path.lower().endswith("/host"):  # pasted the host page itself
        path = path[:-5]
    if any(c in path for c in " \t\r\n\\"):
        raise ValueError("That address has spaces or backslashes in it.")
    netloc = host.lower() + (f":{port}" if port and port != 443 else "")
    return f"https://{netloc}{path}"


def instance_for(settings: dict, client: str | None = None) -> str | None:
    """The instance to use: the client's own one if set, else the default. None when not configured."""
    per = settings.get(KEY_CLIENTS) or {}
    for url in ((per.get(client) if client else None), settings.get(KEY_URL)):
        if url:
            try:
                return normalize_url(url)
            except ValueError:
                continue
    return None


def configured(settings: dict) -> bool:
    return bool(settings.get(KEY_URL) or any((settings.get(KEY_CLIENTS) or {}).values()))


def host_url(instance: str, machine: str) -> str:
    """Host page URL that searches the session group for this machine name. Every part is percent-encoded, so a
    name with / # ? or spaces stays one search term."""
    name = " ".join((machine or "").split())  # no control characters or line breaks
    if not name:
        raise ValueError("No machine name to search for.")
    base = normalize_url(instance)
    return f"{base}/Host#Access/{quote(GROUP, safe='')}/{quote(name, safe='')}"


def open_machine(instance: str, machine: str, opener=None) -> str:
    """Open the host page for a machine in the default browser. Returns the URL. Never runs a shell."""
    url = host_url(instance, machine)
    (opener or webbrowser.open)(url)
    return url


# ---------------------------------------------------------------------------- installed client on this PC
def parse_client_imagepath(name: str, image_path: str) -> dict | None:
    """Session details from a ScreenConnect client service's command line, e.g.
    "...\\ScreenConnect.ClientService.exe" "?e=Access&y=Guest&h=host&p=8041&s=<id>&k=<key>&c=Acme&c=London".
    The k (public key) value is dropped. None when it isn't a ScreenConnect client."""
    i = (image_path or "").find("?")
    if i < 0:
        return None
    q = parse_qs(image_path[i + 1:].strip().strip('"'), keep_blank_values=True)

    def one(k):
        return (q.get(k) or [""])[0].strip()
    host, session = one("h"), one("s")
    if not host and not session:
        return None
    props = [(PROPERTY_NAMES[n] if n < len(PROPERTY_NAMES) else f"Property {n + 1}", v.strip())
             for n, v in enumerate(q.get("c") or []) if v.strip()]
    m = re.search(r"\(([0-9A-Fa-f]{8,})\)", name or "")
    return {"service": name, "instance_id": m.group(1) if m else "", "host": host, "port": one("p"),
            "session": session, "type": one("e") or "Access", "properties": props}


def _service_image_paths():
    """(service name, ImagePath) for every 'ScreenConnect Client (...)' service, read-only from the registry."""
    if sys.platform != "win32":
        return []
    import winreg
    out = []
    base = r"SYSTEM\CurrentControlSet\Services"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base, 0, winreg.KEY_READ) as k:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, i)
                except OSError:
                    break
                i += 1
                if not sub.lower().startswith("screenconnect client"):
                    continue
                try:
                    with winreg.OpenKey(k, sub, 0, winreg.KEY_READ) as sk:
                        out.append((sub, str(winreg.QueryValueEx(sk, "ImagePath")[0])))
                except OSError:
                    continue
    except OSError:
        pass
    return out


def local_clients(reader=None) -> list[dict]:
    """ScreenConnect clients installed on this PC (usually none or one)."""
    out = []
    for name, path in (reader or _service_image_paths)():
        info = parse_client_imagepath(name, path)
        if info:
            out.append(info)
    return out


def describe_client(c: dict) -> str:
    bits = [f"Guest of {c['host']}" if c.get("host") else "ScreenConnect guest"]
    if c.get("session"):
        bits.append(f"session {c['session']}")
    bits += [f"{k}: {v}" for k, v in c.get("properties", [])[:4]]
    return "  ·  ".join(bits)
