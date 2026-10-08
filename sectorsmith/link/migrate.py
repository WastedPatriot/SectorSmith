"""Move a user's files, app settings and apps from one PC to another, between profiles, from an old disk attached
by USB, or onto a drive / folder."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..offline import describe
from ..util import Cancelled, Progress, app_dir, cancel_scope, get_logger, human_size

log = get_logger()

SMALL = 1024 * 1024
CHUNK = 8 * 1024 * 1024


@dataclass
class Item:
    key: str
    label: str
    rel: str
    group: str
    default: bool = True
    note: str = ""


ITEMS = [
    Item("desktop", "Desktop", "Desktop", "Folders"),
    Item("documents", "Documents", "Documents", "Folders"),
    Item("downloads", "Downloads", "Downloads", "Folders"),
    Item("pictures", "Pictures", "Pictures", "Folders"),
    Item("videos", "Videos", "Videos", "Folders"),
    Item("music", "Music", "Music", "Folders"),
    Item("favorites", "Favorites", "Favorites", "Folders"),
    Item("links", "Links", "Links", "Folders", False),
    Item("contacts", "Contacts", "Contacts", "Folders", False),
    Item("savedgames", "Saved Games", "Saved Games", "Folders", False),
    Item("chrome", "Google Chrome", "AppData/Local/Google/Chrome/User Data", "Apps",
         note="Bookmarks, history, extensions. Saved passwords are locked to the old PC — export them first."),
    Item("edge", "Microsoft Edge", "AppData/Local/Microsoft/Edge/User Data", "Apps",
         note="Bookmarks, history, extensions. Signed-in Edge also syncs by itself."),
    Item("firefox", "Mozilla Firefox", "AppData/Roaming/Mozilla/Firefox", "Apps",
         note="Whole profile including saved passwords."),
    Item("signatures", "Outlook signatures", "AppData/Roaming/Microsoft/Signatures", "Apps"),
    Item("sticky", "Sticky Notes", "AppData/Local/Packages/Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe/LocalState",
         "Apps", note="Close Sticky Notes on both PCs first."),
    Item("templates", "Office templates", "AppData/Roaming/Microsoft/Templates", "Apps"),
    Item("dictionaries", "Office custom dictionaries", "AppData/Roaming/Microsoft/UProof", "Apps"),
    Item("quickaccess", "Quick Access & jump lists", "AppData/Roaming/Microsoft/Windows/Recent/AutomaticDestinations",
         "Apps", False),
]


def onedrive_items(src_ep, profile_path) -> list[Item]:
    """OneDrive folders in the profile (off by default — they sync on their own)."""
    try:
        names = src_ep.list_dir(path=profile_path)
    except Exception:  # noqa: BLE001
        return []
    return [Item(f"od:{n}", n, n, "OneDrive", False,
                 "Syncs by itself when the user signs in to OneDrive on the new PC. Only copy if it won't.")
            for n in names if n.lower().startswith("onedrive")]


@dataclass
class Plan:
    src: object
    src_root: str
    dst: object
    dst_root: str
    items: list[Item]
    only_changed: bool = True
    result: dict = field(default_factory=dict)
    apps: list = field(default_factory=list)      # rows from apps.match(); library/winget ones get installed
    store: object = None                          # Deploy library (packages for 'library' rows)
    apps_first: bool = True                       # install apps before copying, so copied settings win
    install_apps: bool = True                     # False: only list them (e.g. copying to a drive)


def measure(src_ep, root: str, items: list[Item]) -> dict:
    sizes = {}
    for it in items:
        try:
            r = src_ep.dir_size(root=root, rel=it.rel)
            sizes[it.key] = (r["bytes"], r["files"])
        except Exception:  # noqa: BLE001
            sizes[it.key] = (0, 0)
    return sizes


def run(plan: Plan, prog: Progress) -> dict:
    """Copy the plan. Cancel stops it within a moment (on linked PCs too); files already copied stay and a report
    is written, so 'Run again' carries on where it stopped."""
    with cancel_scope(prog.check):
        state = {"copied": 0, "bytes": 0, "skipped_unchanged": 0, "failed": [], "scan_errors": 0, "apps": [],
                 "t0": time.time()}
        try:
            return _run(plan, prog, state)
        except Cancelled as c:
            res = _result(plan, state)
            res["cancelled"] = True
            res["report"] = write_report(plan, res)
            c.info.update(res)
            log.info("Migration cancelled: %s", {k: v for k, v in res.items() if k != "failed"})
            raise


def _install_apps(plan: Plan, prog: Progress, state: dict):
    from . import apps
    if not plan.apps or not plan.install_apps:
        return
    r = apps.install(plan.dst, plan.apps, plan.store, prog)
    state["apps"] = r["apps"]
    state["apps_session"] = r["session"]


def _result(plan: Plan, st: dict) -> dict:
    res = {"copied": st["copied"], "bytes": st["bytes"], "skipped_unchanged": st["skipped_unchanged"],
           "failed": st["failed"], "scan_errors": st["scan_errors"], "seconds": round(time.time() - st["t0"], 1),
           "apps": st["apps"], "manual_apps": [a["name"] for a in plan.apps if a["source"] == "manual"]}
    res["hints"] = explain_failures(st["failed"], plan.src.label)
    return res


def _run(plan: Plan, prog: Progress, state: dict) -> dict:
    src, dst = plan.src, plan.dst
    if plan.apps_first:
        _install_apps(plan, prog, state)
    prog.reset(1, "Scanning what to copy…")
    todo = []  # (rel, size, mtime)
    skipped = 0
    scan_errors = 0
    for it in plan.items:
        prog.check()
        prog.set_detail(f"Scanning {it.label}")
        s = src.scan_tree(root=plan.src_root, rel=it.rel)
        scan_errors += s["errors"]
        if not s["files"]:
            continue
        existing = {}
        if plan.only_changed:
            try:
                d = dst.scan_tree(root=plan.dst_root, rel=it.rel)
                existing = {f[0]: (f[1], f[2]) for f in d["files"]}
            except Cancelled:
                raise
            except Exception:  # noqa: BLE001
                existing = {}
        for rel, size, mtime in s["files"]:
            ex = existing.get(rel)
            if ex and ex[0] == size and abs(ex[1] - mtime) <= 2:
                skipped += 1
                continue
            todo.append((rel, size, mtime))

    total = sum(t[1] for t in todo)
    prog.reset(max(1, total), f"Copying {len(todo):,} files ({human_size(total)})")
    done = 0
    copied, failed = 0, state["failed"]
    state["skipped_unchanged"], state["scan_errors"] = skipped, scan_errors

    small = [t for t in todo if t[1] <= SMALL]
    large = [t for t in todo if t[1] > SMALL]

    # small files in batches
    batch, bsize = [], 0
    def flush():
        nonlocal done, copied, batch, bsize
        if not batch:
            return
        rels = [b[0] for b in batch]
        try:
            meta, blob = src.read_files(root=plan.src_root, rels=rels)
        except Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            failed.extend(f"{r}: {e}" for r in rels)
            batch, bsize = [], 0
            return
        items, parts, pos = [], [], 0
        for (rel, size, mtime), n in zip(batch, meta["sizes"]):
            if n < 0:
                failed.append(f"{rel}: {meta['errors'].get(rel, 'read error')}")
                continue
            items.append({"rel": rel, "size": n, "mtime": mtime})
            parts.append(blob[pos: pos + n])
            pos += n
        if items:
            try:
                w = dst.write_files(root=plan.dst_root, items=items, blob=b"".join(parts))
                errs = w.get("errors", {})
                failed.extend(f"{k}: {v}" for k, v in errs.items())
                copied += len(items) - len(errs)
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                failed.extend(f"{i['rel']}: {e}" for i in items)
        done += sum(b[1] for b in batch)
        state["copied"], state["bytes"] = copied, done
        prog.update(done)
        prog.set_detail(batch[-1][0])
        batch, bsize = [], 0

    for t in small:
        prog.check()
        batch.append(t)
        bsize += t[1]
        if bsize >= CHUNK or len(batch) >= 256:
            flush()
    flush()

    for rel, size, mtime in large:
        prog.check()
        prog.set_detail(rel)
        off = 0
        try:
            while off < size:
                prog.check()
                meta, data = src.read_file(root=plan.src_root, rel=rel, offset=off, size=min(CHUNK, size - off))
                if not data:
                    raise OSError("file shrank while copying")
                dst.write_file(root=plan.dst_root, rel=rel, offset=off, total=size, mtime=mtime, blob=data)
                off += len(data)
                prog.update(done + off)
            copied += 1
        except Cancelled:
            # the half-written file has the old date, so 'Run again' copies it again from the start
            failed.append(f"{rel}: stopped part-way (Cancel)")
            raise
        except Exception as e:  # noqa: BLE001
            failed.append(f"{rel}: {e}")
        done += size
        state["copied"], state["bytes"] = copied, done
        prog.update(done)

    if not plan.apps_first:
        _install_apps(plan, prog, state)
    if plan.apps and not plan.install_apps:
        _save_app_list(plan)
    res = _result(plan, state)
    res["bytes"] = total
    res["report"] = write_report(plan, res)
    log.info("Migration done: %s", {k: v for k, v in res.items() if k != "failed"})
    return res


APPS_IN_USE = [("/microsoft/edge/", "Microsoft Edge"), ("/google/chrome/", "Google Chrome"),
               ("/mozilla/firefox/", "Firefox"), ("/microsoft/outlook/", "Outlook"),
               ("/microsoft/teams/", "Teams"), ("/onenote/", "OneNote")]


def explain_failures(failed: list[str], src_label: str) -> list[str]:
    """Plain-English hints for the usual causes, so the report says what to do next."""
    apps, denied = set(), 0
    for f in failed:
        low = f.lower().replace("\\", "/")
        if "permission denied" in low or "being used by another process" in low or "access is denied" in low:
            denied += 1
            for key, app in APPS_IN_USE:
                if key in low:
                    apps.add(app)
    hints = []
    if apps:
        hints.append(f"{', '.join(sorted(apps))} was still open on {src_label}, so some of its files were locked. "
                     "Close it there (check the system tray too) and click 'Run again' — only the missing files copy.")
    if denied and not apps:
        hints.append("Some files were locked or protected. Close open programs on both PCs and run again.")
    return hints


def _save_app_list(plan: Plan):
    """Copying to a drive or folder: leave the old PC's app list next to the files."""
    from .apps import app_list_text
    data = app_list_text(plan.apps).encode("utf-8")
    try:
        plan.dst.write_files(root=plan.dst_root, items=[{"rel": "SectorSmith - apps on the old PC.txt",
                                                         "size": len(data), "mtime": None}], blob=data)
    except Exception as e:  # noqa: BLE001
        log.warning("Couldn't save the app list: %s", e)


def write_report(plan: Plan, res: dict) -> str:
    folder = app_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(plan.src_root.rstrip("/\\")) or "user"
    path = folder / f"Migration {name} {time.strftime('%Y-%m-%d %H%M')}.txt"
    lines = [
        "SectorSmith — user migration report",
        f"Date:        {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"From:        {plan.src.label}  {describe(plan.src_root)}",
        f"To:          {plan.dst.label}  {describe(plan.dst_root)}",
        f"Items:       {', '.join(i.label for i in plan.items)}",
        f"Mode:        {'only new/changed files' if plan.only_changed else 'copy everything'}",
        f"Copied:      {res['copied']:,} files, {human_size(res['bytes'])}",
        f"Unchanged:   {res['skipped_unchanged']:,} files skipped",
        f"Failed:      {len(res['failed'])}",
        f"Duration:    {res['seconds']} s",
    ] + (["Status:      STOPPED (Cancel). Files copied so far are kept; Run again copies the rest."]
         if res.get("cancelled") else []) + [""]
    if plan.apps:
        from .apps import app_list_text
        lines += app_list_text(plan.apps, res.get("apps")).splitlines() + [""]
    lines += [f"  TIP: {h}" for h in res.get("hints", [])] + ([""] if res.get("hints") else []) \
        + [f"  FAILED {f}" for f in res["failed"]]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
