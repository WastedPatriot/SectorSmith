"""Printable clone report for "Clone this PC to a new drive": an A4 HTML page in the style of the wipe certificate,
with the layout before and after, timings, every check and the verdict."""
from __future__ import annotations

import getpass
import html
import json
import platform
import time

from . import verify as V
from .util import APP_NAME, APP_VERSION, human_size, human_time

ROLE_NAMES = {"esp": "EFI System", "msr": "Microsoft Reserved", "windows": "Windows", "recovery": "Recovery",
              "data": "Data", "other": "Other"}
ROLE_FILL = {"esp": "#7a7a7a", "msr": "#b5b5b5", "windows": "#222", "recovery": "#9a9a9a", "data": "#555",
             "other": "#cfcfcf"}
STATUS_TEXT = {"passed": "Passed", "problems": "Problems", "failed": "Failed", "skipped": "Skipped"}


def _stamp(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t)).strip() if t else "-"


def _bar(rows, total) -> str:
    """Layout as one horizontal bar, partitions proportional (with a minimum width so small ones stay visible)."""
    w = 600
    total = max(total, 1)
    widths = [max(6.0, w * r["size"] / total) for r in rows]
    scale = min(1.0, w / max(sum(widths), 1))
    x = 0.0
    out = [f'<svg viewBox="0 0 {w} 26" width="100%" height="26" role="img" aria-label="partition layout">',
           f'<rect x="0" y="0" width="{w}" height="26" fill="#f2f2f2" stroke="#999" stroke-width=".5"/>']
    for r, rw in zip(rows, widths):
        rw *= scale
        out.append(f'<rect x="{x:.1f}" y="0" width="{rw:.1f}" height="26" fill="{ROLE_FILL.get(r["role"], "#ccc")}"'
                   f' stroke="#fff" stroke-width="1"/>')
        x += rw
    out.append("</svg>")
    return "".join(out)


def render_report(res: dict, info: dict | None = None) -> str:
    info = info or {}
    e = html.escape
    v = res.get("verify") or V.not_run()
    label = V.label(v)
    src, dst = res.get("source", {}), res.get("target", {})
    ss = (res.get("plan") or {}).get("sector_size", 512)
    before, after = res.get("layout_before", []), res.get("layout_after", [])

    def rows(pairs):
        return "\n".join(f"<tr><th>{e(k)}</th><td>{e(str(x)) if x not in ('', None) else '-'}</td></tr>"
                         for k, x in pairs)

    job = rows([("Client", info.get("client", "")), ("Ticket", info.get("ticket", "")),
                ("Technician", info.get("technician") or getpass.getuser()),
                ("Machine", info.get("machine") or platform.node())])
    timing = rows([("Started", _stamp(res.get("started"))), ("Finished", _stamp(res.get("ended"))),
                   ("Duration", human_time((res.get("ended") or 0) - (res.get("started") or 0)))] +
                  [(f"  {k.capitalize()}", human_time(t)) for k, t in (res.get("timings") or {}).items()])
    drives = rows([("Source", f"{src.get('name', '')} {src.get('model', '')} ({human_size(src.get('size', 0))})"),
                   ("Source serial", src.get("serial", "")),
                   ("Target", f"{dst.get('name', '')} {dst.get('model', '')} ({human_size(dst.get('size', 0))})"),
                   ("Target serial", dst.get("serial", "")), ("Sector size", f"{ss} bytes"),
                   ("Partition style", (res.get("plan") or {}).get("scheme", "")),
                   ("Snapshot", ", ".join(res.get("snapshot") or []) or "-"),
                   ("Snapshot set", res.get("snapshot_mode") or "-")])
    data = rows([("Copied", f"{human_size(res.get('bytes_copied', 0))} ({res.get('bytes_copied', 0):,} bytes) in "
                            f"{res.get('chunks', 0)} blocks"),
                 ("Unreadable source sectors", f"{res.get('bad_sectors', 0)} (copied as zeros)"),
                 ("Target left as", res.get("target_state", ""))])

    def layout_table(parts, after_side):
        out = ["<table class='lay'><tr><th>Partition</th><th>File system</th><th>Starts at</th><th>Size</th>"
               + ("<th>What happened</th>" if after_side else "") + "</tr>"]
        for p in parts:
            what = ""
            if after_side:
                what = {"used": "Used space copied from the snapshot", "raw": "Copied block for block",
                        "zero": "Rebuilt empty"}.get(p.get("action"), "")
                if p.get("resize"):
                    what += f", {p['resize'].replace('grow', 'grown').replace('shrink', 'shrunk')}"
            out.append(f"<tr><td>{e(ROLE_NAMES.get(p['role'], p['role']))}</td><td>{e(p.get('fs') or '-')}</td>"
                       f"<td>{p['start'] // (1024 * 1024):,} MiB</td><td>{e(human_size(p['size']))}</td>"
                       + (f"<td>{e(what)}</td>" if after_side else "") + "</tr>")
        out.append("</table>")
        return "".join(out)
    checks = "".join(f"<tr class='{e(c['status'])}'><td>{e(STATUS_TEXT.get(c['status'], c['status']))}</td>"
                     f"<td>{e(c['name'])}</td><td>{e(c.get('detail', ''))}</td></tr>" for c in res.get("checks", []))
    bad = res.get("bad_lbas") or []
    bad_html = ""
    if bad:
        items = ", ".join(f"{b['source']}&rarr;{b['target']}" for b in bad[:500])
        bad_html = (f"<h2>Unreadable sectors (source LBA &rarr; target LBA)</h2><p class='mono'>{items}"
                    f"{' and more' if len(bad) > 500 else ''}</p>")
    notes = "".join(f"<li>{e(n)}</li>" for n in res.get("notes") or [])
    error = f"<p class='err'>{e(res['error'])}</p>" if res.get("error") else ""
    after_total = (res.get("target") or {}).get("size", 0)
    before_total = (res.get("source") or {}).get("size", 0)
    record = json.dumps({k: res.get(k) for k in ("source", "target", "layout_before", "layout_after", "bytes_copied",
                                                 "bad_sectors", "checks", "verify", "started", "ended")},
                        sort_keys=True, default=str).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Clone report</title>
<style>
@page {{ size: A4; margin: 16mm 16mm 18mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Segoe UI", Arial, Helvetica, sans-serif; color: #111; background: #fff; margin: 0 auto;
  max-width: 178mm; font-size: 10.5pt; line-height: 1.35; padding: 10mm 0; }}
header {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1.5pt solid #111;
  padding-bottom: 4mm; }}
h1 {{ font-size: 17pt; margin: 0; font-weight: 600; }}
.sub {{ color: #555; font-size: 9.5pt; }}
.result {{ margin: 5mm 0 2mm; padding: 3mm 4mm; border: 1pt solid #111; font-weight: 600; font-size: 12pt; }}
.result small {{ display: block; font-weight: 400; font-size: 9.5pt; color: #333; margin-top: 1mm; }}
h2 {{ font-size: 10pt; text-transform: uppercase; letter-spacing: .8pt; color: #444; margin: 5mm 0 1mm;
  font-weight: 600; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; vertical-align: top; padding: 1.3mm 2mm 1.3mm 0; border-bottom: .5pt solid #ccc; }}
th {{ font-weight: 400; color: #555; }}
.kv th {{ width: 34%; }}
.lay th {{ font-size: 9pt; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0 10mm; }}
.checks td:first-child {{ width: 18mm; font-weight: 600; }}
.checks tr.failed td:first-child {{ text-decoration: underline; }}
.mono {{ font-family: Consolas, "Courier New", monospace; font-size: 8.5pt; word-break: break-all; }}
.err {{ border-left: 2pt solid #111; padding-left: 3mm; }}
.legend span {{ display: inline-block; margin-right: 4mm; font-size: 8.5pt; color: #444; }}
.legend i {{ display: inline-block; width: 3mm; height: 3mm; margin-right: 1mm; vertical-align: -0.3mm; }}
footer {{ margin-top: 8mm; font-size: 8pt; color: #666; }}
@media print {{ body {{ padding: 0; }} }}
</style></head><body>
<header><div><h1>Clone report</h1><div class="sub">Clone this PC to a new drive</div></div>
<div class="sub">{e(_stamp(res.get("ended")))}</div></header>
<div class="result">Verdict: {e(label)}<small>{e(v.get("summary", ""))}</small></div>
{error}
<div class="grid"><section><h2>Job</h2><table class="kv">{job}</table></section>
<section><h2>Time</h2><table class="kv">{timing}</table></section></div>
<h2>Drives</h2><table class="kv">{drives}</table>
<h2>Layout before (source)</h2>{_bar(before, before_total)}{layout_table(before, False)}
<h2>Layout after (new drive)</h2>{_bar(after, after_total)}{layout_table(after, True)}
<p class="legend">{"".join(f'<span><i style="background:{ROLE_FILL[k]}"></i>{ROLE_NAMES[k]}</span>'
                          for k in ("esp", "msr", "windows", "recovery", "data"))}</p>
<h2>Data</h2><table class="kv">{data}</table>
<h2>Checks</h2><table class="checks">{checks}</table>
{bad_html}
{"<h2>Notes</h2><ul>" + notes + "</ul>" if notes else ""}
<h2>After the first start from the new drive</h2>
<ul><li>Disconnect the old drive (or change the boot order) and start the PC from the new one.</li>
<li>Check that C: has the expected size in Disk Management.</li>
<li>Run <span class="mono">reagentc /info</span> in an administrator command prompt. If Windows RE shows Disabled, run
<span class="mono">reagentc /disable</span> then <span class="mono">reagentc /enable</span>.</li></ul>
<footer>Made with {e(APP_NAME)} {e(APP_VERSION)}.</footer>
<script type="application/json" id="clone-record">{record}</script>
</body></html>
"""


def write_report(path: str, res: dict, info: dict | None = None) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(render_report(res, info))
    return path
