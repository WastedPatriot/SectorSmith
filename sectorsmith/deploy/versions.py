"""Dynamic versions: a package can say where its newest version comes from instead of (or as well as) holding a
fixed installer. Sources: winget (`winget show`), a GitHub release, a vendor web page with a pattern, or a
PowerShell script that prints the version and download link.

Resolved once per day per package (cached in the library). Every network call goes through a fetcher and every
program through a runner, both injectable, so tests never touch the network."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request

from ..util import check_cancel, get_logger

log = get_logger()
KINDS = {"winget": "winget", "github": "GitHub release", "page": "Vendor page", "script": "PowerShell script"}
CACHE = "versions.json"
TIMEOUT = 30
MAX_DOWNLOAD = 8 * 1024 ** 3
GITHUB_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class SourceError(RuntimeError):
    """The version source didn't give a usable answer. The message says why, in plain words."""


# ---------------------------------------------------------------------------- the network, injectable
def fetch(url: str, timeout: float = TIMEOUT, headers: dict | None = None) -> bytes:
    """GET a URL (HTTPS only) and return the body. Raises SourceError."""
    _https(url)
    req = urllib.request.Request(url, headers={"User-Agent": "SectorSmith", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310  (https only, checked above)
            return r.read(16 * 1024 * 1024)
    except OSError as e:
        raise SourceError(f"couldn't fetch {url}: {e}") from e


def download(url: str, dest: str, timeout: float = TIMEOUT, progress=None):
    """Stream a URL (HTTPS only) to a file, checking Cancel between chunks."""
    _https(url)
    req = urllib.request.Request(url, headers={"User-Agent": "SectorSmith"})
    tmp = dest + ".part"
    got = 0
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:  # nosec B310  (https only)
            total = int(r.headers.get("Content-Length") or 0)
            while True:
                check_cancel()
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                got += len(chunk)
                if got > MAX_DOWNLOAD:
                    raise SourceError("the download is larger than 8 GB, so it was stopped")
                f.write(chunk)
                if progress:
                    progress(got, total)
        os.replace(tmp, dest)
    except OSError as e:
        raise SourceError(f"couldn't download {url}: {e}") from e
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _https(url: str):
    if urllib.parse.urlparse(url or "").scheme.lower() != "https":
        raise SourceError(f"only https links are used, not {url or 'an empty link'}")


# ---------------------------------------------------------------------------- parsing each source
def clean(v: str) -> str:
    """'v24.08', 'release-1.2.3' or 'Version 5' -> the numeric part."""
    m = re.search(r"\d+(?:\.\d+){0,3}", v or "")
    return m.group(0) if m else (v or "").strip()


def parse_winget_show(text: str) -> dict:
    """Version, Installer Url and Installer SHA256 from `winget show --id X --exact`."""
    out = {}
    for ln in text.replace("\r\n", "\n").split("\n"):
        ln = ln.split("\r")[-1].strip()
        k, sep, v = ln.partition(":")
        if not sep:
            continue
        k, v = k.strip().lower(), v.strip()
        if k == "version" and "version" not in out:
            out["version"] = v
        elif k in ("installer url", "download url") and "url" not in out:
            out["url"] = v
        elif k in ("installer sha256", "sha256") and "sha256" not in out:
            out["sha256"] = v.lower()
    if not out.get("version"):
        raise SourceError("winget show gave no version (is the ID right?)")
    return out


def parse_github(data: bytes | str, asset: str = "") -> dict:
    """The newest release from the GitHub API JSON: its tag as the version and the first asset that matches."""
    try:
        rel = json.loads(data)
    except ValueError as e:
        raise SourceError("GitHub didn't send release details") from e
    if isinstance(rel, list):
        rel = next((r for r in rel if not r.get("draft") and not r.get("prerelease")), None)
    if not isinstance(rel, dict) or not rel.get("tag_name"):
        raise SourceError("that repository has no published release")
    out = {"version": clean(rel["tag_name"])}
    assets = rel.get("assets") or []
    if asset:
        try:
            rx = re.compile(asset, re.I)
        except re.error as e:
            raise SourceError(f"the asset pattern isn't valid: {e}") from e
        hit = next((a for a in assets if rx.search(a.get("name") or "")), None)
        if hit is None:
            raise SourceError(f"no file in release {rel['tag_name']} matches {asset}")
        out["url"] = hit.get("browser_download_url") or ""
        digest = hit.get("digest") or ""
        if digest.lower().startswith("sha256:"):
            out["sha256"] = digest[7:].lower()
    return out


def parse_page(text: str, pattern: str, base_url: str = "") -> dict:
    """A vendor page and a pattern: named groups version, url and sha256, or the first group as the version."""
    try:
        rx = re.compile(pattern, re.I | re.S)
    except re.error as e:
        raise SourceError(f"the pattern isn't valid: {e}") from e
    m = rx.search(text)
    if not m:
        raise SourceError("the pattern didn't match anything on the page")
    g = m.groupdict()
    version = g.get("version") or (m.group(1) if rx.groups else m.group(0))
    out = {"version": clean(version)}
    if g.get("url"):
        out["url"] = urllib.parse.urljoin(base_url, g["url"])
    if g.get("sha256"):
        out["sha256"] = g["sha256"].lower()
    return out


def parse_script(text: str) -> dict:
    """A script's output: JSON with version, url and sha256, or lines 'version', 'url' (and 'sha256')."""
    text = (text or "").strip()
    if text.startswith("{"):
        try:
            d = json.loads(text)
            out = {k: str(d[k]).strip() for k in ("version", "url", "sha256") if d.get(k)}
        except ValueError as e:
            raise SourceError("the script printed something that isn't valid JSON") from e
    else:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        out = {}
        for ln in lines:
            if re.match(r"^https?://", ln, re.I) and "url" not in out:
                out["url"] = ln
            elif SHA256.match(ln) and "sha256" not in out:
                out["sha256"] = ln.lower()
            elif "version" not in out:
                out["version"] = ln
    if not out.get("version"):
        raise SourceError("the script didn't print a version")
    out["version"] = clean(out["version"])
    if out.get("sha256"):
        out["sha256"] = out["sha256"].lower()
    return out


# ---------------------------------------------------------------------------- resolving
def validate(src: dict) -> dict:
    """A tidy copy of a version source, or {} for none. Raises ValueError with a plain message."""
    kind = (src or {}).get("kind") or ""
    if not kind:
        return {}
    if kind not in KINDS:
        raise ValueError(f"Unknown version source {kind}.")
    s = {"kind": kind}
    if kind == "winget":
        s["id"] = (src.get("id") or "").strip()
        if not re.match(r"^[A-Za-z0-9][\w.+-]*\.[\w.+-]+$", s["id"]):
            raise ValueError("Give the winget ID, for example 7zip.7zip.")
    elif kind == "github":
        s["repo"] = (src.get("repo") or "").strip().strip("/")
        s["asset"] = (src.get("asset") or "").strip()
        if not GITHUB_REPO.match(s["repo"]):
            raise ValueError("Give the GitHub repository as owner/name, for example notepad-plus-plus/notepad-plus-plus.")
        _rx(s["asset"], "asset pattern")
    elif kind == "page":
        s["url"], s["regex"] = (src.get("url") or "").strip(), (src.get("regex") or "").strip()
        try:
            _https(s["url"])
        except SourceError:
            raise ValueError("The vendor page must be an https link.") from None
        if not s["regex"]:
            raise ValueError("Add a pattern that finds the version on the page.")
        _rx(s["regex"], "pattern")
    else:
        s["script"] = (src.get("script") or "").strip()
        if not s["script"]:
            raise ValueError("Add the PowerShell script that prints the version and download link.")
    return s


def _rx(pattern, what):
    try:
        re.compile(pattern)
    except re.error as e:
        raise ValueError(f"The {what} isn't a valid regular expression: {e}") from None


def describe(src: dict) -> str:
    kind = (src or {}).get("kind")
    if kind == "winget":
        return f"winget {src.get('id')}"
    if kind == "github":
        return f"GitHub {src.get('repo')}"
    if kind == "page":
        return "Page " + urllib.parse.urlparse(src.get("url") or "").netloc
    if kind == "script":
        return "Script"
    return ""


def run_powershell(script: str, runner, timeout: float = 120) -> str:
    """Run a version script on this PC with PowerShell (no shell; the script goes in as a base64 command)."""
    import base64
    exe = "powershell.exe" if os.name == "nt" else "pwsh"
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    code, out = runner([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], timeout)
    if code != 0:
        raise SourceError(f"the version script exited with {code}: {out.strip()[-200:]}")
    return out


def resolve(src: dict, fetcher=None, runner=None) -> dict:
    """Ask the source for the newest version now: {"version", "url"?, "sha256"?}. Raises SourceError."""
    src = validate(src)
    fetcher = fetcher or fetch
    kind = src.get("kind")
    if kind == "winget":
        if runner is None:
            from .catalogue import run_process, winget_path
            exe = winget_path()
            if not exe:
                raise SourceError("winget isn't installed on this PC, so its versions can't be looked up")
            runner, args = run_process, [exe]
        else:
            args = ["winget"]
        code, text = runner(args + ["show", "--id", src["id"], "--exact", "--accept-source-agreements"], 60)
        out = parse_winget_show(text)
    elif kind == "github":
        url = f"https://api.github.com/repos/{src['repo']}/releases/latest"
        out = parse_github(fetcher(url, headers={"Accept": "application/vnd.github+json"}), src.get("asset", ""))
    elif kind == "page":
        out = parse_page(fetcher(src["url"]).decode("utf-8", "replace"), src["regex"], src["url"])
    elif kind == "script":
        if runner is None:
            from .catalogue import run_process
            runner = run_process
        out = parse_script(run_powershell(src["script"], runner))
    else:
        raise SourceError("this package has no version source")
    if out.get("url"):
        _https(out["url"])
    if out.get("sha256") and not SHA256.match(out["sha256"]):
        out.pop("sha256")
    return out


def _key(src: dict) -> str:
    return hashlib.sha256(json.dumps(src, sort_keys=True).encode()).hexdigest()[:16]


def latest(store, pkg, fetcher=None, runner=None, now: float | None = None, refresh=False) -> dict:
    """The package's newest version from its source, asked at most once a day (cached in the library; changing
    the source asks again). Adds "when" and "cached". Raises SourceError."""
    src = validate(pkg.version_source)
    if not src:
        raise SourceError("this package has no version source")
    now = now or time.time()
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    path = os.path.join(store.root, CACHE)
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    hit = cache.get(pkg.id)
    if not refresh and hit and hit.get("day") == day and hit.get("key") == _key(src):
        return dict(hit["data"], cached=True)
    data = resolve(src, fetcher or getattr(store, "fetcher", None), runner or getattr(store, "runner", None))
    data["when"] = now
    cache[pkg.id] = {"day": day, "key": _key(src), "data": data}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1)
    os.replace(tmp, path)
    log.info("Version source for %s: %s", pkg.name, data)
    return dict(data, cached=False)


def cached(store, pkg) -> dict | None:
    """What the last lookup found (any day), without asking again."""
    try:
        with open(os.path.join(store.root, CACHE), encoding="utf-8") as f:
            hit = json.load(f).get(pkg.id)
    except (OSError, ValueError):
        return None
    return hit["data"] if hit else None


def get_installer(store, pkg, found: dict, downloader=None, progress=None) -> str:
    """Download the installer a source pointed at into the library's download cache (once per version), checking
    its SHA256 when the source gave one. Returns the local path. Raises SourceError."""
    url = found.get("url") or ""
    if not url:
        raise SourceError("the version source gave no download link")
    name = re.sub(r"[^\w.+-]+", "_", os.path.basename(urllib.parse.urlparse(url).path)) or "installer.exe"
    folder = os.path.join(store.root, "downloads", pkg.id, re.sub(r"[^\w.-]+", "_", found.get("version") or "x"))
    os.makedirs(folder, exist_ok=True)
    dest = os.path.join(folder, name)
    want = (found.get("sha256") or "").lower()
    if os.path.exists(dest) and (not want or sha256_file(dest) == want):
        return dest
    downloader = downloader or getattr(store, "downloader", None) or download
    downloader(url, dest, progress=progress)
    if want:
        got = sha256_file(dest)
        if got != want:
            os.remove(dest)
            raise SourceError(f"the download's SHA256 is {got[:12]}..., the source says {want[:12]}..., so it "
                              "wasn't used")
    # older versions of this package aren't needed any more
    base = os.path.dirname(folder)
    for other in os.listdir(base):
        if other != os.path.basename(folder):
            import shutil
            shutil.rmtree(os.path.join(base, other), ignore_errors=True)
    return dest


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
