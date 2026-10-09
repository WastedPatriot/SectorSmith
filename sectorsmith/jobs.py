"""Job history: every job the UI runs, kept across sessions in app_dir()/jobs.jsonl.

One JSON object per line. A job is written when it starts, again when it ends and again if a report or
certificate is attached later; loading keeps the newest line for each id, so a crash between two writes loses at
most the end of one job. Lines that do not parse are skipped. Once the file holds well over CAP jobs it is
rewritten with the newest CAP and the older ones move to jobs.1.jsonl (one generation is kept)."""
from __future__ import annotations

import csv
import json
import os
import threading
import time
import uuid
from pathlib import Path

from .util import get_logger

log = get_logger()

CAP = 5000
FILE = "jobs.jsonl"
FIELDS = ("id", "task", "title", "client", "ticket", "technician", "machine", "detail", "started", "ended",
          "result", "report")
RESULTS = ("Done", "Cancelled", "Failed", "Interrupted")
SEARCHED = ("task", "title", "client", "ticket", "technician", "machine", "detail", "result", "report")
CSV_COLUMNS = (("Started", "started"), ("Ended", "ended"), ("Task", "task"), ("Target", "detail"),
               ("Client", "client"), ("Ticket", "ticket"), ("Technician", "technician"), ("Machine", "machine"),
               ("Result", "result"), ("Report", "report"))


class JobStore:
    def __init__(self, path=None, cap: int = CAP):
        if path is None:
            from .util import app_dir
            path = app_dir() / FILE
        self.path = Path(path)
        self.cap = max(1, int(cap))
        self.active: set[str] = set()   # ids of jobs this process is running right now
        self._lock = threading.Lock()
        self._lines = None              # lines in the file, counted on the first write

    @property
    def archive(self) -> Path:
        return self.path.with_name(self.path.stem + ".1" + self.path.suffix)

    # -- writing -----------------------------------------------------------------------------------------------
    def start(self, **fields) -> dict:
        rec = {k: fields.get(k) or "" for k in FIELDS}
        rec.update(id=uuid.uuid4().hex[:16], started=fields.get("started") or time.time(), ended=None,
                   result="Running")
        self.active.add(rec["id"])
        self._write(rec)
        return rec

    def finish(self, rec: dict, result: str, ended: float | None = None, report=None) -> dict:
        rec["result"] = result
        rec["ended"] = ended or time.time()
        if report:
            rec["report"] = str(report)
        self.active.discard(rec.get("id"))
        self._write(rec)
        return rec

    def attach(self, rec: dict, path) -> dict:
        """Record the report or certificate a job produced (saved after the job ended, for example)."""
        rec["report"] = str(path)
        self._write(rec)
        return rec

    def _write(self, rec: dict):
        row = {k: rec.get(k) for k in FIELDS}
        data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a+b") as f:
                    f.seek(0, os.SEEK_END)
                    if f.tell():
                        f.seek(-1, os.SEEK_END)
                        if f.read(1) != b"\n":  # a torn last line: start ours on a fresh one
                            data = b"\n" + data
                    f.seek(0, os.SEEK_END)
                    f.write(data)  # one write per line, so a second SectorSmith appending can't interleave
                    f.flush()
                    os.fsync(f.fileno())
                if self._lines is None:
                    self._lines = _count_lines(self.path)
                else:
                    self._lines += 1
                if self._lines > 2 * self.cap + 50:
                    self._compact()
            except OSError as e:  # history must never stop a job
                log.warning("job history not written: %s", e)

    def _compact(self):
        recs = self._read()
        keep, old = recs[-self.cap:], recs[:-self.cap]
        if old:
            _write_lines(self.archive, old)
        _write_lines(self.path, keep)
        self._lines = len(keep)

    # -- reading -----------------------------------------------------------------------------------------------
    def _read(self) -> list[dict]:
        merged: dict[str, dict] = {}
        try:
            fh = open(self.path, encoding="utf-8", errors="replace")
        except OSError:
            return []
        with fh:
            for n, line in enumerate(fh, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    log.warning("job history: skipped unreadable line %d", n)
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                    continue
                merged.setdefault(row["id"], {}).update(row)
        out = []
        for rec in merged.values():
            rec = {k: rec.get(k) for k in FIELDS}
            try:
                rec["started"] = float(rec["started"] or 0)
                rec["ended"] = float(rec["ended"]) if rec["ended"] else None
            except (TypeError, ValueError):
                continue
            for k in FIELDS:
                if rec[k] is None and k != "ended":
                    rec[k] = ""
            out.append(rec)
        out.sort(key=lambda r: r["started"])
        return out

    def load(self) -> list[dict]:
        """Every job, oldest first. A job still marked Running that this process isn't running was cut short
        (SectorSmith closed or crashed), so it shows as Interrupted."""
        with self._lock:
            recs = self._read()
        for r in recs:
            if r["result"] == "Running" and r["id"] not in self.active:
                r["result"] = "Interrupted"
        return recs


def _count_lines(path: Path) -> int:
    try:
        with open(path, "rb") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def _write_lines(path: Path, recs: list[dict]):
    """Replace path in one step: write a temp file next to it, then rename over it."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for r in recs:
            f.write(json.dumps({k: r.get(k) for k in FIELDS}, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------- filters and export
def day_range(start: str = "", end: str = "") -> tuple[float | None, float | None]:
    """'YYYY-MM-DD' strings (either may be blank) to local [since, until) timestamps; until covers the whole end
    day. Raises ValueError for a date that doesn't parse."""
    since = until = None
    if start.strip():
        since = time.mktime(time.strptime(start.strip(), "%Y-%m-%d"))
    if end.strip():
        t = time.strptime(end.strip(), "%Y-%m-%d")
        until = time.mktime((t.tm_year, t.tm_mon, t.tm_mday + 1, 0, 0, 0, 0, 0, -1))
    return since, until


def filter_jobs(recs, client=None, result=None, since=None, until=None, text="") -> list[dict]:
    """client: exact name, '' for jobs with no client, None for any. result: one of RESULTS (or 'Running'), None
    for any. since/until: timestamps on the start time, until exclusive. text: words that must all appear."""
    words = [w for w in (text or "").lower().split() if w]
    out = []
    for r in recs:
        if client is not None and (r.get("client") or "") != client:
            continue
        if result is not None and r.get("result") != result:
            continue
        started = r.get("started") or 0
        if since is not None and started < since:
            continue
        if until is not None and started >= until:
            continue
        if words:
            hay = " ".join(str(r.get(k) or "") for k in SEARCHED).lower()
            if not all(w in hay for w in words):
                continue
        out.append(r)
    return out


def _cell(v) -> str:
    s = "" if v is None else str(v)
    # a leading = + - @ makes Excel treat the cell as a formula
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def _stamp(ts) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def export_csv(recs, path) -> int:
    """Write jobs as CSV (UTF-8 with BOM so Excel reads the accents). Returns the number of rows."""
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([title for title, _k in CSV_COLUMNS])
        for r in recs:
            w.writerow([_stamp(r.get(k)) if k in ("started", "ended") else _cell(r.get(k)) for _t, k in CSV_COLUMNS])
    return len(recs)
