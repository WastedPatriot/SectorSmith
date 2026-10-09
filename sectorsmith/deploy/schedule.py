"""Simple schedules on deployments: daily or weekly at a time, or whenever a linked PC connects.

SectorSmith is not a service, so nothing runs at the exact time. A schedule is due on a PC when a slot has passed
since it last ran there; the app runs due schedules when it starts and when a linked PC connects."""
from __future__ import annotations

import datetime as _dt
import re
import time

from ..util import Progress, get_logger
from . import core

log = get_logger()
KINDS = ("daily", "weekly", "connect")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def make(kind: str, at: str = "09:00", day: int = 0, mode: str = "full", now: float | None = None) -> dict:
    """A schedule dict, or {} for none. Raises ValueError for a bad time."""
    if kind not in KINDS:
        return {}
    s = {"kind": kind, "mode": mode if mode in ("full", "detect") else "full", "since": now or time.time()}
    if kind != "connect":
        s["time"] = parse_time(at)
    if kind == "weekly":
        s["day"] = int(day) % 7
    return s


def parse_time(text: str) -> str:
    m = re.fullmatch(r"\s*(\d{1,2})(?::|\.)?(\d{2})?\s*", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        raise ValueError("Use a 24-hour time like 09:00 or 17:30.")
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


def _slot_after(s: dict, t: float) -> float | None:
    """First scheduled time strictly after t (local time), or None for 'connect' and no schedule."""
    kind = s.get("kind")
    if kind not in ("daily", "weekly"):
        return None
    hh, mm = (int(x) for x in s.get("time", "09:00").split(":"))
    base = _dt.datetime.fromtimestamp(t)
    cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if kind == "weekly":
        cand += _dt.timedelta(days=(int(s.get("day", 0)) - cand.weekday()) % 7)
        step = _dt.timedelta(days=7)
    else:
        step = _dt.timedelta(days=1)
    while cand.timestamp() <= t:
        cand += step
    return cand.timestamp()


def last_run(d: core.Deployment, host: str | None = None) -> float:
    s = d.schedule or {}
    if host is not None:
        return float(d.last_runs.get(host.lower()) or s.get("since") or 0)
    return float(max(d.last_runs.values(), default=0) or s.get("since") or 0)


def next_due(d: core.Deployment) -> float | None:
    """When the schedule next comes round after its latest run on any PC (may be in the past: due now)."""
    return _slot_after(d.schedule or {}, last_run(d))


def due_for(d: core.Deployment, host: str, now: float, event: str) -> bool:
    """Should this deployment's schedule run on this PC now? event: 'start' or 'connect'."""
    s = d.schedule or {}
    if not d.enabled or s.get("kind") not in KINDS:
        return False
    if s["kind"] == "connect":
        return event == "connect"
    slot = _slot_after(s, last_run(d, host))
    return slot is not None and slot <= now


def describe(d: core.Deployment) -> str:
    """'Daily at 09:00', 'Mondays at 08:30', 'When a PC connects' or ''."""
    s = d.schedule or {}
    kind = s.get("kind")
    if kind == "daily":
        text = f"Daily at {s.get('time', '09:00')}"
    elif kind == "weekly":
        text = f"{DAYS[int(s.get('day', 0)) % 7]}s at {s.get('time', '09:00')}"
    elif kind == "connect":
        text = "When a PC connects"
    else:
        return ""
    return text + (" (check only)" if s.get("mode") == "detect" else "")


def next_text(d: core.Deployment, now: float | None = None) -> str:
    """Next due time in words, for the tables."""
    now = now or time.time()
    s = d.schedule or {}
    if not s.get("kind"):
        return "-"
    if not d.enabled:
        return "Off"
    if s["kind"] == "connect":
        return "Next connect"
    t = next_due(d)
    if t is None:
        return "-"
    if t <= now:
        return "Due now"
    lt, today = time.localtime(t), time.localtime(now)
    if lt.tm_yday == today.tm_yday and lt.tm_year == today.tm_year:
        return time.strftime("Today %H:%M", lt)
    if t - now < 7 * 86400:
        return time.strftime("%a %H:%M", lt)
    return time.strftime("%d %b %H:%M", lt)


def due_runs(store: core.Store, hosts: list[str], event: str, now: float | None = None,
             local_host: str | None = None) -> dict:
    """{hostname: [deployment ids due there]}. 'Every PC' schedules skip the technician's own PC (local_host): it
    only gets scheduled changes when a deployment names it, or a client it belongs to."""
    now = now or time.time()
    out: dict = {}
    for h in hosts:
        for d in store.deployments:
            if not (d.schedule or {}).get("kind") or not core.targets(store, d, h):
                continue
            if local_host and h.lower() == local_host.lower() and d.target_kind == "all":
                continue
            if due_for(d, h, now, event):
                out.setdefault(h, []).append(d.id)
    return out


def run_due(ep, store: core.Store, ids: list[str], prog: Progress) -> list[dict]:
    """Run the due deployments on one PC: one session for those that apply changes and one for 'check only'
    schedules. Cancellable like any session."""
    deps = [d for d in (store.get("deployments", i) for i in ids) if d is not None]
    out = []
    for mode in ("full", "detect"):
        group = [d for d in deps if (d.schedule or {}).get("mode", "full") == mode]
        if group:
            out.append(core.run_session(ep, store, prog, mode=mode, onboarding=any(d.onboarding_only for d in group),
                                        only=[d.id for d in group], trigger="schedule"))
    return out


def mark_ran(store: core.Store, ids: list[str], host: str, when: float | None = None):
    when = when or time.time()
    for i in ids:
        d = store.get("deployments", i)
        if d is not None:
            d.last_runs = dict(d.last_runs, **{host.lower(): when})
            store.upsert("deployments", d)
