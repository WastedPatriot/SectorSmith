"""Inventory: a snapshot of each PC (OS, build, model, serial, CPU, RAM, disks, BitLocker, last boot and installed
software) kept per machine in the library, searchable, and usable by deployment conditions and offline detection.

Snapshots are taken when a linked PC connects, at the start of every maintenance session and from the Inventory
page. Conditions are simple lines like ``ram_gb >= 8`` or ``software contains Chrome``."""
from __future__ import annotations

import json
import os
import re
import time

from ..util import Cancelled, get_logger

log = get_logger()
FOLDER = "inventory"
NUMERIC = {"build", "cores", "ram_gb", "disk_gb", "free_gb", "days_since_boot"}
FIELDS = {"os": "Windows edition", "build": "Build number", "version": "Version", "model": "Model",
          "manufacturer": "Manufacturer", "serial": "Serial number", "cpu": "Processor", "cores": "Cores",
          "ram_gb": "Memory (GB)", "disk_gb": "System drive size (GB)", "free_gb": "System drive free (GB)",
          "bitlocker": "BitLocker", "domain": "Domain", "days_since_boot": "Days since restart",
          "software": "Installed software"}
OPS = (">=", "<=", "!=", "=", ">", "<", "not contains", "contains")
_COND = re.compile(r"^\s*([A-Za-z_]+)\s*(>=|<=|!=|=|>|<|not\s+contains|contains)\s*(.*?)\s*$", re.I)


# ---------------------------------------------------------------------------- snapshots
def _file(store, host: str) -> str:
    return os.path.join(store.root, FOLDER, re.sub(r"[^\w.-]+", "_", host.lower()) + ".json")


def take(ep, store, software: list | None = None, host: str | None = None) -> dict | None:
    """Ask the PC for a snapshot and keep it. software: an installed_software() list already read (saves a call).
    Returns the snapshot, or None when the PC can't give one (an older agent); never raises except on Cancel."""
    try:
        facts = ep.inventory()
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("Inventory on %s: %s", host or getattr(ep, "label", "?"), e)
        facts = None
    try:
        if software is None:
            software = ep.installed_software()
        host = host or (facts or {}).get("hostname") or ep.info()["hostname"]
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        log.info("Inventory software list: %s", e)
        if facts is None or not host:
            return None
        software = software or []
    if facts is None and not software:
        return None
    snap = dict(facts or {})
    snap["hostname"] = host
    snap["taken"] = time.time()
    apps = [e for e in software or [] if e.get("name") and not e.get("system_component")]
    snap["software"] = sorted(({"name": e["name"], "version": e.get("version") or "", "key": e.get("key") or "",
                                "publisher": e.get("publisher") or ""} for e in apps), key=lambda e: e["name"].lower())
    save(store, host, snap)
    return snap


def save(store, host: str, snap: dict):
    path = _file(store, host)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f, indent=1)
    os.replace(tmp, path)
    m = store.machine(host, create=True)
    m.last_seen = snap.get("taken") or time.time()
    store.upsert("machines", m)


def load(store, host: str) -> dict | None:
    try:
        with open(_file(store, host), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def all_snapshots(store) -> dict:
    """{hostname lower case: snapshot}"""
    out = {}
    folder = os.path.join(store.root, FOLDER)
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if name.endswith(".json"):
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as f:
                    snap = json.load(f)
                out[(snap.get("hostname") or name[:-5]).lower()] = snap
            except (OSError, ValueError):
                continue
    return out


# ---------------------------------------------------------------------------- reading a snapshot
def value(snap: dict, field: str):
    """A field of a snapshot, including the derived ones (disk_gb, free_gb, days_since_boot)."""
    if field in ("disk_gb", "free_gb"):
        vols = snap.get("volumes") or []
        sysvol = next((v for v in vols if str(v.get("drive", "")).upper() in ("C:", "/")), vols[0] if vols else None)
        return (sysvol or {}).get("size_gb" if field == "disk_gb" else "free_gb")
    if field == "days_since_boot":
        boot = snap.get("last_boot")
        return round((time.time() - boot) / 86400, 1) if boot else None
    return snap.get(field)


def summary(snap: dict) -> list[tuple[str, str]]:
    """(label, text) pairs for the details panel."""
    if not snap:
        return []
    disks = "; ".join(f"{d.get('name') or '?'} {d.get('size_gb', '?')} GB" for d in snap.get("disks") or [])
    vols = "; ".join(f"{v.get('drive')} {v.get('free_gb', '?')} of {v.get('size_gb', '?')} GB free"
                     for v in snap.get("volumes") or [])
    boot = snap.get("last_boot")
    n = len(snap.get("software") or [])
    rows = [("OS", " ".join(x for x in (snap.get("os"), f"build {snap['build']}" if snap.get("build") else "")
                                 if x)),
            ("Model", " ".join(x for x in (snap.get("manufacturer"), snap.get("model")) if x)),
            ("Serial", snap.get("serial") or ""), ("Processor", snap.get("cpu") or ""),
            ("Memory", f"{snap['ram_gb']} GB" if snap.get("ram_gb") else ""), ("Disks", disks), ("Space", vols),
            ("BitLocker", snap.get("bitlocker") or ""), ("Domain", snap.get("domain") or ""),
            ("Last restart", time.strftime("%d %b %Y %H:%M", time.localtime(boot)) if boot else ""),
            ("Software", f"{n} app" + ("" if n == 1 else "s"))]
    return [(k, v) for k, v in rows if v]


def matches(snap: dict, query: str) -> bool:
    """Every word of the query appears somewhere in the snapshot (facts or software names)."""
    words = query.lower().split()
    if not words:
        return True
    hay = " ".join(str(snap.get(k) or "") for k in FIELDS if k != "software").lower()
    hay += " " + " ".join(f"{e['name']} {e.get('version', '')}" for e in snap.get("software") or []).lower()
    return all(w in hay for w in words)


# ---------------------------------------------------------------------------- conditions
def parse_conditions(text: str) -> list[tuple[str, str, str]]:
    """'ram_gb >= 8; software contains Chrome' -> [(field, op, value)]. Raises ValueError in plain words."""
    out = []
    for part in re.split(r"[;\n]+", text or ""):
        if not part.strip():
            continue
        m = _COND.match(part)
        if not m:
            raise ValueError(f"'{part.strip()}' isn't a condition. Write it like: ram_gb >= 8")
        field, op, val = m.group(1).lower(), " ".join(m.group(2).lower().split()), m.group(3)
        if field not in FIELDS:
            raise ValueError(f"'{field}' isn't something the inventory knows. Use one of: {', '.join(FIELDS)}")
        if field == "software" and op not in ("contains", "not contains"):
            raise ValueError("software only works with contains or not contains")
        if field in NUMERIC and op not in ("contains", "not contains"):
            try:
                float(val)
            except ValueError:
                raise ValueError(f"{field} is a number, so compare it with a number, not '{val}'") from None
        if not val:
            raise ValueError(f"'{part.strip()}' needs a value to compare with")
        out.append((field, op, val))
    return out


def conditions_pass(snap: dict | None, conds) -> tuple[bool | None, str]:
    """(True/False, why) for a snapshot; (None, why) when there's no snapshot to judge by."""
    if not conds:
        return True, ""
    if isinstance(conds, str):
        conds = parse_conditions(conds)
    if not snap:
        return None, "no inventory snapshot of this PC yet"
    for field, op, want in conds:
        if field == "software":
            names = [e.get("name", "").lower() for e in snap.get("software") or []]
            has = any(want.lower() in n for n in names)
            ok = has if op == "contains" else not has
        else:
            got = value(snap, field)
            if got in (None, ""):
                return False, f"{field} isn't known for this PC"
            if op in ("contains", "not contains"):
                has = want.lower() in str(got).lower()
                ok = has if op == "contains" else not has
            elif field in NUMERIC:
                try:
                    a, b = float(got), float(want)
                except (TypeError, ValueError):
                    return False, f"{field} isn't a number here ({got})"
                ok = {">=": a >= b, "<=": a <= b, "!=": a != b, "=": a == b, ">": a > b, "<": a < b}[op]
            else:
                a, b = str(got).lower(), want.lower()
                ok = {"=": a == b, "!=": a != b, ">=": a >= b, "<=": a <= b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False, f"{field} {op} {want} isn't true here ({value(snap, field) if field != 'software' else 'no'})"
    return True, ""


# ---------------------------------------------------------------------------- detection from a snapshot
def expected(store, host: str, snap: dict | None = None) -> list[dict]:
    """What the next run would probably do on this PC, judged from its last snapshot without connecting: software
    found by name or product code only (file and script detection need the PC). Each row: name, action, current."""
    from . import core
    snap = snap if snap is not None else load(store, host)
    if not snap:
        return []
    inv = [{"key": e.get("key") or e["name"], "name": e["name"], "version": e.get("version") or ""}
           for e in snap.get("software") or []]
    out = []
    for d in core.applicable(store, host, False,
                             check=lambda d: not d.conditions.strip() or bool(conditions_pass(snap, d.conditions)[0])):
        if d.item_type != "software":
            continue
        pkg = store.get("packages", d.item_id)
        if pkg is None or d.desired == "ignore":
            continue
        method = (pkg.detection or {}).get("method", "registry")
        if method in ("file", "script"):
            out.append({"name": pkg.name, "action": "unknown", "current": "checked on the PC"})
            continue
        hits = core._match_inventory(pkg, inv)
        ver = hits[0]["version"] if hits else None
        want = d.version if d.desired == "version" else pkg.version
        act = core.resolve(d.desired, bool(hits), ver, want, d.allow_downgrade)
        out.append({"name": pkg.name, "action": act, "current": ver or "not installed"})
    return out
