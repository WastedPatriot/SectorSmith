"""Move a user's files and app data from one PC to another (or between profiles)."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from ..util import Cancelled, Progress, app_dir, get_logger, human_size

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
    src, dst = plan.src, plan.dst
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
    copied, failed = 0, []
    t0 = time.time()

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
            except Exception as e:  # noqa: BLE001
                failed.extend(f"{i['rel']}: {e}" for i in items)
        done += sum(b[1] for b in batch)
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
            raise
        except Exception as e:  # noqa: BLE001
            failed.append(f"{rel}: {e}")
        done += size
        prog.update(done)

    res = {"copied": copied, "bytes": total, "skipped_unchanged": skipped, "failed": failed,
           "scan_errors": scan_errors, "seconds": round(time.time() - t0, 1)}
    res["report"] = write_report(plan, res)
    log.info("Migration done: %s", {k: v for k, v in res.items() if k != "failed"})
    return res


def write_report(plan: Plan, res: dict) -> str:
    folder = app_dir() / "reports"
    folder.mkdir(parents=True, exist_ok=True)
    name = os.path.basename(plan.src_root.rstrip("/\\")) or "user"
    path = folder / f"Migration {name} {time.strftime('%Y-%m-%d %H%M')}.txt"
    lines = [
        "SectorSmith — user migration report",
        f"Date:        {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"From:        {plan.src.label}  {plan.src_root}",
        f"To:          {plan.dst.label}  {plan.dst_root}",
        f"Items:       {', '.join(i.label for i in plan.items)}",
        f"Mode:        {'only new/changed files' if plan.only_changed else 'copy everything'}",
        f"Copied:      {res['copied']:,} files, {human_size(res['bytes'])}",
        f"Unchanged:   {res['skipped_unchanged']:,} files skipped",
        f"Failed:      {len(res['failed'])}",
        f"Duration:    {res['seconds']} s",
        "",
    ] + [f"  FAILED {f}" for f in res["failed"]]
    path.write_text("\n".join(lines), encoding="utf-8")
    return str(path)
