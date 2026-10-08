"""Sector-level disk cloning between endpoints (this PC and/or linked machines):
one source → one or many destinations, full or used-space-only, verified per block."""
from __future__ import annotations

import queue
import threading
import time

from ..usedmap import pieces
from ..util import Cancelled, Progress, get_logger

log = get_logger()
CHUNK = 4 * 1024 * 1024


class _Target:
    def __init__(self, ep, path):
        self.ep, self.path = ep, path
        self.q: queue.Queue = queue.Queue(maxsize=6)
        self.error = None
        self.mismatches = 0
        self.written = 0
        self.info = None
        self.grown = None
        self.thread = None

    @property
    def name(self):
        return f"{self.ep.label}:{self.path}"

    def run(self):
        while True:
            item = self.q.get()
            if item is None:
                return
            if self.error:
                continue  # drain after a failure
            off, n, meta, blob = item
            try:
                w = self.ep.disk_write(path=self.path, offset=off, size=n, zero=meta.get("zero", False),
                                       z=meta.get("z", False), blob=blob or b"")
                if w["sha"] != meta["sha"]:
                    self.mismatches += 1
                self.written += n
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
                log.warning("Clone target %s failed: %s", self.name, e)


def clone_many(src, src_path: str, targets: list[tuple], prog: Progress, start_lba: int = 0,
               sectors: int | None = None, smart: bool = True, snapshot: bool = True) -> dict:
    """Read ``src`` once and write it to every (endpoint, disk_path) in ``targets`` in parallel."""
    if not targets:
        raise ValueError("No destination selected.")
    prog.set_label("Preparing (snapshotting the source if it's in use)…")
    s_info = src.disk_open(path=src_path, writable=False, snapshot=snapshot)
    ss = s_info["sector_size"]
    total_sectors = s_info["usable"] // ss
    if sectors is None:
        sectors = total_sectors - start_lba
    length = sectors * ss
    tg = [_Target(ep, path) for ep, path in targets]
    opened = []
    try:
        for t in tg:
            t.info = t.ep.disk_open(path=t.path, writable=True)
            opened.append(t)
            if t.info["sector_size"] != ss:
                raise ValueError(f"{t.name}: sector size differs ({t.info['sector_size']} vs {ss}).")
            if t.info["usable"] < length:
                raise ValueError(f"{t.name} is smaller than the source.")
        prog.set_label("Mapping used space…" if smart else "Starting…")
        plan = src.disk_plan(path=src_path, start_lba=start_lba, sectors=sectors, smart=smart)
        todo = list(pieces([tuple(r) for r in plan["ranges"]], CHUNK))
        to_copy = sum(n for _, n in todo)
        over_network = not (getattr(src, "is_local", False) and all(getattr(t.ep, "is_local", False) for t in tg))
        for t in tg:
            t.thread = threading.Thread(target=t.run, daemon=True)
            t.thread.start()
        label = f"Cloning {src.label} → " + (tg[0].ep.label if len(tg) == 1 else f"{len(tg)} disks")
        prog.reset(to_copy, label + (" · used space only" if smart else ""))
        done = bad = sent = zeros = 0
        t0 = time.time()
        for off, n in todo:
            prog.check()
            if all(t.error for t in tg):
                raise RuntimeError("Every destination failed: " + "; ".join(t.error for t in tg))
            meta, blob = src.disk_read(path=src_path, offset=off, size=n, zskip=True, compress=over_network)
            for t in tg:
                if not t.error:
                    t.q.put((off, n, meta, blob))
            bad += meta.get("bad", 0)
            zeros += 1 if meta.get("zero") else 0
            sent += len(blob or b"")
            done += n
            prog.update(done)
            if done % (32 * CHUNK) < n:
                prog.set_detail(f"{bad} unreadable sector(s) · " +
                                " · ".join(f"{t.ep.label}: {'FAILED' if t.error else 'ok'}" for t in tg))
        for t in tg:
            t.q.put(None)
        for t in tg:
            t.thread.join()
        whole = start_lba == 0 and length == total_sectors * ss
        for t in tg:
            if not t.error and whole and t.info["usable"] > length:
                try:
                    t.grown = t.ep.disk_fix_gpt(path=t.path)
                except Exception as e:  # noqa: BLE001
                    t.grown = f"Couldn't adjust the partition table to the larger disk: {e}"
    except Cancelled:
        for t in tg:
            if t.thread and t.thread.is_alive():
                t.error = t.error or "cancelled"
                t.q.put(None)
        raise
    finally:
        try:
            src.disk_close(path=src_path)
        finally:
            for t in opened:
                try:
                    t.ep.disk_close(path=t.path)
                except Exception:  # noqa: BLE001
                    pass
    res = {"bytes": length, "copied_bytes": to_copy, "bad_sectors": bad, "sent_bytes": sent,
           "empty_chunks_skipped": zeros, "seconds": round(time.time() - t0, 1), "plan": plan["by_partition"],
           "snapshot": s_info.get("snapshot", []),
           "targets": [{"machine": t.ep.label, "path": t.path, "ok": not t.error and not t.mismatches,
                        "error": t.error, "verify_mismatches": t.mismatches, "grown": t.grown} for t in tg]}
    # single-target convenience fields
    res["verify_mismatches"] = sum(t.mismatches for t in tg)
    res["grown"] = tg[0].grown if len(tg) == 1 else None
    log.info("Clone %s:%s -> %s: %s", src.label, src_path, [t.name for t in tg],
             {k: v for k, v in res.items() if k != "plan"})
    return res


def clone(src, src_path: str, dst, dst_path: str, prog: Progress, start_lba: int = 0, sectors: int | None = None,
          verify: bool = True, snapshot: bool = True, smart: bool = False) -> dict:
    """One-to-one clone (kept for compatibility)."""
    res = clone_many(src, src_path, [(dst, dst_path)], prog, start_lba=start_lba, sectors=sectors, smart=smart,
                     snapshot=snapshot)
    if res["targets"][0]["error"]:
        raise RuntimeError(res["targets"][0]["error"])
    return res
