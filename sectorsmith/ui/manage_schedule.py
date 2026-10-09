"""Runs due deployment schedules while SectorSmith is open: shortly after it starts and whenever a linked PC
connects. One run at a time, in the background, cancellable; it waits while another job is running."""
from __future__ import annotations

import os
import threading
import time

from ..deploy import schedule
from ..util import Cancelled, Progress, app_dir, cancel_scope, get_logger

log = get_logger()
START_DELAY_MS = 2500
BUSY_RETRY_MS = 60_000


def _host(m):
    try:
        info = m.info() if m.is_local else (m.info_cache or {})
        return info.get("hostname") or m.label
    except Exception:  # noqa: BLE001
        return m.label


class Scheduler:
    def __init__(self, app):
        self.app = app
        self.prog: Progress | None = None
        self.known: set = set()
        self.last: dict | None = None    # summary of the latest run, for tests and the toast

    def start(self):
        self.known = {id(m) for m in self.app.machines()}
        self.app.machine_listeners.append(self._machines_changed)
        self.app.after(START_DELAY_MS, lambda: self.check("start", self.app.machines()))

    def _machines_changed(self):
        ms = self.app.machines()
        new = [m for m in ms if id(m) not in self.known]
        self.known = {id(m) for m in ms}
        if new:
            self.check("connect", new)

    def _store(self):
        if self.app._deploy_store is None and not os.path.exists(os.path.join(app_dir(), "deploy",
                                                                              "deployments.json")):
            return None  # no Deploy library yet: nothing can be scheduled
        try:
            return self.app.deploy_store()
        except Exception as e:  # noqa: BLE001
            log.warning("Schedules: couldn't open the Deploy library: %s", e)
            return None

    def check(self, event: str, machines: list) -> bool:
        """Start a run for whatever is due on these PCs. Returns True if one started."""
        store = self._store()
        if store is None or not any((d.schedule or {}).get("kind") for d in store.deployments):
            return False
        machines = [m for m in machines if m.is_local or getattr(m, "alive", True)]
        if self.prog is not None or self.app.job is not None:
            self.app.after(BUSY_RETRY_MS, lambda: self.check(event, machines))
            return False
        by_host = {_host(m): m for m in machines}
        due = schedule.due_runs(store, list(by_host), event, local_host=_host(self.app.local_ep))
        if not due:
            return False
        self._run(store, [(by_host[h], ids) for h, ids in due.items()], event)
        return True

    def _run(self, store, work, event):
        from .shell import technician
        prog = self.prog = Progress(1, "Scheduled maintenance")
        hosts = ", ".join(_host(m) for m, _ids in work)
        record = dict(task="Scheduled maintenance", title="Scheduled maintenance", started=time.time(), ended=None,
                      client="", ticket="", technician=technician(), machine=hosts, detail=hosts, result="Running")
        self.app.jobs.append(record)
        log.info("Schedules (%s): running %s", event, {_host(m): ids for m, ids in work})

        def worker():
            results, cancelled = [], False
            try:
                with cancel_scope(prog.check):
                    for m, ids in work:
                        prog.check()
                        prog.set_label(_host(m))
                        try:
                            sessions = schedule.run_due(m, store, ids, prog)
                        except Cancelled:
                            raise
                        except Exception as e:  # noqa: BLE001  (one PC failing doesn't stop the others)
                            log.warning("Scheduled maintenance on %s failed: %s", _host(m), e)
                            results.append((_host(m), None, str(e)))
                            continue
                        results.append((_host(m), sessions, None))
                        self.app.call_soon(schedule.mark_ran, store, ids, _host(m))
            except Cancelled:
                cancelled = True
            self.app.call_soon(self._finished, results, cancelled, record)
        threading.Thread(target=worker, daemon=True, name="deploy-schedule").start()

    def _finished(self, results, cancelled, record):
        self.prog = None
        acts = [a for _h, ss, _e in results for s in ss or [] for a in s.get("actions", [])
                if a.get("action") not in ("none", "audit")]
        failed = [a for a in acts if a.get("status") == "failed"]
        errors = [h for h, _ss, e in results if e]
        record["ended"] = time.time()
        record["result"] = "Cancelled" if cancelled else ("Failed" if failed or errors else "Done")
        self.last = {"pcs": [h for h, _ss, _e in results], "changes": len(acts), "failed": len(failed),
                     "errors": errors, "cancelled": cancelled}
        if cancelled:
            text, tone = "Scheduled maintenance stopped. What finished is on the Sessions page.", "warn"
        elif failed or errors:
            text, tone = (f"Scheduled maintenance: {len(failed)} of {len(acts)} changes failed"
                          + (f", couldn't reach {', '.join(errors)}" if errors else "") + "."), "danger"
        else:
            n = len(results)
            text, tone = (f"Scheduled maintenance on {n} PC{'s' if n != 1 else ''}: "
                          + (f"{len(acts)} change{'s' if len(acts) != 1 else ''} made." if acts else
                             "nothing needed changing.")), "success"
        self.app.toast(text, tone)
        if type(self.app.screen).__name__ in ("DeploymentsScreen", "SessionsScreen", "JobsScreen"):
            self.app._refresh_screen()


def start(app):
    """Hook for the main window: run due schedules shortly after start and when a linked PC connects."""
    if getattr(app, "_deploy_scheduler", None) is None:
        app._deploy_scheduler = Scheduler(app)
        app._deploy_scheduler.start()
    return app._deploy_scheduler


def running(app) -> Progress | None:
    sch = getattr(app, "_deploy_scheduler", None)
    return sch.prog if sch is not None else None
