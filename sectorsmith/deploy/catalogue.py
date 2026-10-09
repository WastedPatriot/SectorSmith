"""Package catalogue for Manage: a curated list of common MSP apps, live winget search when winget is installed, and
turning either into a library package (silent install, uninstall and detection)."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess  # nosec B404
import time

from ..util import Cancelled, check_cancel, get_logger
from . import core

log = get_logger()
CATALOGUE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "assets",
                         "catalogue.json")
CHOCO_INSTALL = "choco install {choco_id} -y --no-progress"
CHOCO_UNINSTALL = "choco uninstall {choco_id} -y --no-progress"
WINGET_ID = re.compile(r"^[A-Za-z0-9][\w.+-]*\.[\w.+-]+$")
_cache: dict = {}


def load(path: str | None = None) -> list[dict]:
    """The curated apps, each with name, winget, choco, publisher, category, detect and maybe note/placeholder."""
    path = path or CATALOGUE
    if path not in _cache:
        try:
            with open(path, encoding="utf-8") as f:
                apps = json.load(f).get("apps", [])
        except (OSError, ValueError) as e:
            log.warning("Couldn't read the package catalogue %s: %s", path, e)
            apps = []
        _cache[path] = [dict(a, source="catalogue") for a in apps if a.get("name")]
    return [dict(a) for a in _cache[path]]


def categories(apps: list[dict]) -> list[str]:
    return sorted({a.get("category") or "Other" for a in apps})


def search(apps: list[dict], query: str = "", category: str = "") -> list[dict]:
    """Every word of the query has to appear in the name, publisher, IDs or category."""
    words = query.lower().split()
    out = []
    for a in apps:
        if category and (a.get("category") or "Other") != category:
            continue
        hay = " ".join(str(a.get(k) or "") for k in ("name", "publisher", "winget", "choco", "category")).lower()
        if all(w in hay for w in words):
            out.append(a)
    # names starting with the query first
    q = query.lower().strip()
    out.sort(key=lambda a: (not a["name"].lower().startswith(q) if q else False, a["name"].lower()))
    return out


# ---------------------------------------------------------------------------- winget
def winget_path() -> str | None:
    return shutil.which("winget")


def run_process(args: list[str], timeout: float = 30) -> tuple[int, str]:
    """Run a program (no shell) and return (exit code, stdout). Cancel (check_cancel) and the timeout kill it."""
    p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,  # nosec B603
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    t0 = time.monotonic()
    while True:
        try:
            out, _ = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            try:
                check_cancel()
                if time.monotonic() - t0 > timeout:
                    raise TimeoutError(f"{os.path.basename(args[0])} took longer than {int(timeout)} s")
            except (Cancelled, TimeoutError):
                p.kill()
                p.communicate()
                raise
    return p.returncode, out.decode("utf-8", "replace")


def winget_search(query: str, runner=None, timeout: float = 30) -> list[dict]:
    """Live search of the winget source. ``runner(args, timeout) -> (code, text)`` defaults to running winget.
    Raises FileNotFoundError when winget isn't installed, Cancelled on Cancel, TimeoutError on the timeout."""
    query = query.strip()
    if not query:
        return []
    if runner is None:
        exe = winget_path()
        if not exe:
            raise FileNotFoundError("winget is not installed on this PC")
        runner = run_process
    else:
        exe = "winget"
    code, text = runner([exe, "search", query, "--source", "winget", "--accept-source-agreements"], timeout)
    rows = parse_search(text)
    if not rows and code not in (0, -1978335212):  # that one is 'no packages found'
        log.info("winget search %r exited with %s: %s", query, code, text[-300:])
    return rows


def parse_search(text: str) -> list[dict]:
    """Rows from `winget search` output. Copes with the progress spinner, a missing Match column, cut-off names
    and wide characters that shift the columns."""
    # the spinner writes '\r' and backspaces before the table; keep what's after the last carriage return
    lines = [ln.split("\r")[-1].replace("\b", "").rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    head = next((i for i, ln in enumerate(lines)
                 if re.search(r"\bId\b", ln, re.I) and i + 1 < len(lines)
                 and re.fullmatch(r"\s*-{10,}\s*", lines[i + 1] or "")), None)
    if head is None:
        return []
    hdr = lines[head]
    starts = [m.start() for m in re.finditer(r"\S+", hdr)]
    names = [m.group(0).lower() for m in re.finditer(r"\S+", hdr)]
    out, seen = [], set()
    for ln in lines[head + 2:]:
        if not ln.strip():
            continue
        row = _by_columns(ln, starts, names)
        if not row or not WINGET_ID.match(row.get("id", "")):
            row = _by_gaps(ln)
        if not row or row["id"] in seen:
            continue
        seen.add(row["id"])
        name = row.get("name", "").rstrip("…").rstrip(".").strip() or row["id"]
        out.append({"name": name, "winget": row["id"], "version": row.get("version", ""), "choco": "",
                    "publisher": row["id"].split(".")[0], "category": "winget", "source": "winget",
                    "detect": {"method": "registry", "value": name}})
    return out


def _by_columns(line, starts, names):
    # by position, so a translated header (Nom, ID, Version...) still works: name first, then id, then version
    if len(starts) < 3 or "id" not in names:
        return None
    cells = [line[s:(starts[i + 1] if i + 1 < len(starts) else len(line))].strip() for i, s in enumerate(starts)]
    i = names.index("id")
    return {"name": cells[0], "id": cells[i], "version": cells[i + 1] if i + 1 < len(cells) else ""}


def _by_gaps(line):
    parts = [p for p in re.split(r"\s{2,}", line.strip()) if p]
    for i, p in enumerate(parts):
        if i and WINGET_ID.match(p):
            return {"name": " ".join(parts[:i]), "id": p, "version": parts[i + 1] if i + 1 < len(parts) else ""}
    return None


# ---------------------------------------------------------------------------- to a package
def to_package(app: dict, source: str = "winget") -> core.Package:
    """A library package for a catalogue entry or a winget search result. source: 'winget' or 'choco'.
    The version is left empty: winget and Chocolatey install their newest, so detection just checks it's there."""
    if app.get("placeholder"):
        raise ValueError(f"{app['name']} needs your own installer. Drop it on Packages.")
    det = dict(app.get("detect") or {"method": "registry", "value": app["name"]})
    if det.get("method") == "registry" and not det.get("value"):
        det["value"] = app["name"]
    notes = [app["note"]] if app.get("note") else []
    if source == "choco":
        cid = app.get("choco") or ""
        if not cid:
            raise ValueError(f"{app['name']} has no Chocolatey package. Use winget.")
        install, uninstall = CHOCO_INSTALL.format(choco_id=cid), CHOCO_UNINSTALL.format(choco_id=cid)
        notes.append("Chocolatey must be installed on the PC.")
        kind, wid = "script", ""
    else:
        wid = app.get("winget") or ""
        if not wid:
            raise ValueError(f"{app['name']} has no winget ID.")
        install, uninstall, kind = core.WINGET_INSTALL, core.WINGET_UNINSTALL, "winget"
        if app.get("choco"):
            notes.append(f"Also on Chocolatey as {app['choco']}.")
    return core.Package(name=app["name"], kind=kind, publisher=app.get("publisher") or "", install=install,
                        uninstall=uninstall, detection=det, winget_id=wid, category=app.get("category") or "",
                        notes=notes)


def in_library(store: core.Store, app: dict) -> core.Package | None:
    """The library package that already covers this app (same winget ID, or same name)."""
    wid, name = (app.get("winget") or "").lower(), app["name"].lower()
    return next((p for p in store.packages if (wid and p.winget_id.lower() == wid) or p.name.lower() == name), None)


def add(store: core.Store, app: dict, source: str = "winget") -> core.Package:
    """Add to the library, or return the package that's already there."""
    have = in_library(store, app)
    if have is not None:
        return have
    return store.upsert("packages", to_package(app, source))
