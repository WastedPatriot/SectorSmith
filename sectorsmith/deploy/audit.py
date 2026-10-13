"""Audit log for Deploy: every change to the library (packages, tasks, clients, deployments, licences, PC tags) and
every action a maintenance session takes, with who, when and what. Append-only JSON lines in the library
(audit.jsonl); secrets and licence keys never go in, only which fields changed."""
from __future__ import annotations

import csv
import getpass
import json
import os
import threading
import time

FILE = "audit.jsonl"
KIND_LABEL = {"packages": "Package", "tasks": "Task", "clients": "Client", "deployments": "Deployment",
              "machines": "PC", "licences": "Licence", "session": "Session"}
# fields that change on their own and aren't worth a line (a PC's last snapshot time, a schedule's last run)
QUIET = {"machines": {"last_seen"}, "deployments": {"last_runs"}}
SECRET_FIELDS = {"secret", "params", "hint"}
CSV_COLUMNS = (("When", "when"), ("Who", "who"), ("Action", "action"), ("Kind", "kind"), ("Name", "name"),
               ("Details", "detail"), ("Id", "id"))
_lock = threading.Lock()


def who(store) -> str:
    actor = getattr(store, "actor", None)
    try:
        name = actor() if callable(actor) else actor
    except Exception:  # noqa: BLE001
        name = None
    if not name:
        try:
            name = getpass.getuser()
        except (OSError, KeyError):
            name = "unknown"
    return name


def record(store, action: str, kind: str, obj_id: str = "", name: str = "", detail: str = ""):
    """Append one line. Never raises: an audit write that fails is logged, the change itself still stands."""
    row = {"when": time.time(), "who": who(store), "action": action, "kind": kind, "id": obj_id, "name": name,
           "detail": detail}
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        with _lock, open(os.path.join(store.root, FILE), "a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        from ..util import get_logger
        get_logger().warning("Couldn't write the Deploy audit log: %s", e)


def changed_fields(kind: str, before: dict | None, after: dict) -> list[str]:
    if before is None:
        return []
    quiet = QUIET.get(kind, set())
    return [k for k in after if k not in quiet and before.get(k) != after.get(k)]


def describe_change(kind: str, before: dict | None, after: dict) -> str:
    """'enabled: True -> False' for plain fields; just the name for secrets, scripts and long values."""
    parts = []
    for k in changed_fields(kind, before, after):
        old, new = before.get(k), after.get(k)
        if k in SECRET_FIELDS or isinstance(new, (dict, list)) or len(str(new)) > 60 or len(str(old)) > 60 \
                or "\n" in str(new) or "\n" in str(old):
            parts.append(k)
        else:
            parts.append(f"{k}: {old!r} -> {new!r}")
    return "; ".join(parts)


def read(store, limit: int | None = None) -> list[dict]:
    """Newest first. Lines that don't parse are skipped."""
    out = []
    try:
        with open(os.path.join(store.root, FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
    except OSError:
        return []
    out.reverse()
    return out[:limit] if limit else out


def matches(row: dict, query: str) -> bool:
    hay = " ".join(str(row.get(k) or "") for k in ("who", "action", "kind", "name", "detail")).lower()
    hay += " " + KIND_LABEL.get(row.get("kind"), "").lower()
    return all(w in hay for w in query.lower().split())


def export_csv(rows: list[dict], path: str) -> str:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([c for c, _k in CSV_COLUMNS])
        for r in rows:
            vals = []
            for _c, k in CSV_COLUMNS:
                v = r.get(k, "")
                if k == "when" and v:
                    v = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
                if k == "kind":
                    v = KIND_LABEL.get(v, v)
                text = str(v)
                # a cell starting with = + - @ would be a formula in Excel
                vals.append("'" + text if text[:1] in ("=", "+", "-", "@") else text)
            w.writerow(vals)
    return path
