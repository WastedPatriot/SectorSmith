"""Printable data-erasure certificate: an A4 HTML page (print it to PDF) branded with the MSP's name and logo."""
from __future__ import annotations

import base64
import getpass
import hashlib
import html
import json
import os
import platform
import re
import time
import uuid

from .qr import qr_svg
from .util import APP_NAME, APP_VERSION, human_size, human_time

LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
              ".svg": "image/svg+xml", ".webp": "image/webp", ".bmp": "image/bmp"}
LOGO_MAX = 3 * 1024 * 1024
QR_PREFIX = "SECTORSMITH-WIPE"
NIST_NOTES = {
    "Purge": "NIST SP 800-88 Purge: the drive's own sanitize command, which also reaches spare and remapped areas "
             "the computer cannot address.",
    "Clear": "NIST SP 800-88 Clear: data overwritten through the normal interface. On flash drives this cannot reach "
             "spare cells; the drive's own sanitize (Purge) or destruction is stronger.",
}


def new_certificate_id() -> str:
    h = uuid.uuid4().hex[:12].upper()
    return f"{h[:4]}-{h[4:8]}-{h[8:]}"


def _stamp(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t)).strip() if t else "-"


def certificate_record(info: dict, branding: dict | None = None) -> dict:
    """The facts the certificate states, as plain values. Its hash is printed and put in the QR code."""
    b = branding or {}
    ok = info.get("write_errors", 0) == 0 and info.get("verify_mismatched_blocks", 0) == 0
    if ok and info.get("verified"):
        result = "Passed: erased and verified"
    elif ok:
        result = "Passed: erased, not verified"
    else:
        result = "Completed with errors"
    hw = bool(info.get("hardware"))
    return {
        "certificate_id": info.get("cert_id") or new_certificate_id(),
        "issuer": b.get("company") or "",
        "client": info.get("client") or "",
        "ticket": info.get("ticket") or "",
        "technician": info.get("technician") or getpass.getuser(),
        "machine": info.get("machine") or platform.node(),
        "drive_model": info.get("model") or "",
        "drive_serial": info.get("serial") or "",
        "drive_firmware": info.get("drive_firmware") or "",
        "capacity_bytes": int(info.get("size") or 0),
        "interface": info.get("bus") or "",
        "scope": info.get("scope") or "",
        "method": info.get("method") or "",
        "erase_type": "Hardware (drive's own erase)" if hw else "Software overwrite",
        "nist_category": info.get("nist") or ("Purge" if hw else "Clear"),
        "passes": int(info.get("passes") or 0),
        "verification": info.get("verification") or ("Read-back verified" if info.get("verified") else
                                                     "Not verified"),
        "write_errors": int(info.get("write_errors") or 0),
        "verify_mismatches": int(info.get("verify_mismatched_blocks") or 0),
        "note": info.get("fallback_reason") or "",
        "result": result,
        "started": round(float(info.get("start") or 0), 3),
        "finished": round(float(info.get("end") or 0), 3),
        "software": f"{APP_NAME} {APP_VERSION}",
    }


def record_hash(record: dict) -> str:
    """SHA-256 of the record as compact, key-sorted UTF-8 JSON (the same text embedded in the page)."""
    return hashlib.sha256(_canonical(record).encode("utf-8")).hexdigest()


def _canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def qr_payload(cert_id: str, digest: str) -> str:
    return f"{QR_PREFIX} {cert_id} SHA256:{digest}"


def _logo_tag(path: str | None) -> str:
    if not path or not os.path.isfile(path):
        return ""
    mime = LOGO_TYPES.get(os.path.splitext(path)[1].lower())
    if not mime or os.path.getsize(path) > LOGO_MAX:
        return ""
    with open(path, "rb") as f:
        data = base64.b64encode(f.read()).decode("ascii")
    return f'<img class="logo" alt="" src="data:{mime};base64,{data}">'


def render_certificate(info: dict, branding: dict | None = None) -> tuple[str, str, str]:
    """(html, certificate id, record hash). ``branding``: {"company": name, "logo": image path}."""
    b = branding or {}
    rec = certificate_record(info, b)
    digest = record_hash(rec)
    cid = rec["certificate_id"]
    e = html.escape
    company = rec["issuer"]

    def rows(pairs):
        return "\n".join(f"<tr><th>{e(k)}</th><td>{e(str(v)) if v not in ('', None) else '-'}</td></tr>"
                         for k, v in pairs)
    job = rows([("Client", rec["client"]), ("Ticket", rec["ticket"]), ("Technician", rec["technician"]),
                ("Machine", rec["machine"])])
    drive = rows([("Model", rec["drive_model"]), ("Serial number", rec["drive_serial"]),
                  ("Capacity", f"{human_size(rec['capacity_bytes'])} ({rec['capacity_bytes']:,} bytes)"),
                  ("Interface", rec["interface"]), ("Firmware", rec["drive_firmware"]), ("Scope", rec["scope"])])
    erasure = rows([("Method", rec["method"]), ("Type", rec["erase_type"]),
                    ("NIST SP 800-88", rec["nist_category"]), ("Passes", rec["passes"] or "-"),
                    ("Verification", rec["verification"]),
                    ("Errors", f"{rec['write_errors']} unwritable sector(s), {rec['verify_mismatches']} verify "
                               "mismatch(es)")]
                   + ([("Note", rec["note"])] if rec["note"] else []))
    timing = rows([("Started", _stamp(rec["started"])), ("Finished", _stamp(rec["finished"])),
                   ("Duration", human_time(rec["finished"] - rec["started"]) if rec["started"] else "-")])
    qr = qr_svg(qr_payload(cid, digest), module=3, quiet=4, label=f"Certificate {cid}")
    record_json = _canonical(rec).replace("</", "<\\/")
    issuer = (f'{_logo_tag(b.get("logo"))}<div class="company">{e(company)}</div>' if company or b.get("logo")
              else "")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Certificate of Data Erasure {e(cid)}</title>
<style>
@page {{ size: A4; margin: 16mm 16mm 18mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family: "Segoe UI", Arial, Helvetica, sans-serif; color: #111; background: #fff; margin: 0 auto;
  max-width: 178mm; font-size: 10.5pt; line-height: 1.35; padding: 10mm 0; }}
header {{ display: flex; justify-content: space-between; align-items: flex-start; gap: 12mm;
  border-bottom: 1.5pt solid #111; padding-bottom: 5mm; }}
.issuer {{ display: flex; align-items: center; gap: 4mm; min-height: 14mm; }}
.logo {{ max-height: 16mm; max-width: 55mm; }}
.company {{ font-size: 13pt; font-weight: 600; }}
.title {{ text-align: right; }}
h1 {{ font-size: 17pt; margin: 0; font-weight: 600; letter-spacing: .2pt; }}
.cid {{ font-family: Consolas, "Courier New", monospace; font-size: 10pt; margin-top: 1.5mm; }}
.result {{ margin: 5mm 0 2mm; padding: 3mm 4mm; border: 1pt solid #111; font-weight: 600; font-size: 11.5pt; }}
h2 {{ font-size: 10pt; text-transform: uppercase; letter-spacing: .8pt; color: #444; margin: 5mm 0 1mm;
  font-weight: 600; }}
table {{ width: 100%; border-collapse: collapse; }}
th, td {{ text-align: left; vertical-align: top; padding: 1.4mm 0; border-bottom: .5pt solid #ccc; }}
th {{ width: 34%; font-weight: 400; color: #555; padding-right: 4mm; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 0 10mm; }}
.verify {{ display: flex; gap: 6mm; align-items: center; margin-top: 6mm; page-break-inside: avoid; }}
.verify svg {{ width: 34mm; height: 34mm; flex: none; }}
.mono {{ font-family: Consolas, "Courier New", monospace; font-size: 8.5pt; word-break: break-all; }}
.small {{ font-size: 8.5pt; color: #444; }}
.sig {{ display: flex; gap: 12mm; margin-top: 12mm; page-break-inside: avoid; }}
.sig div {{ flex: 1; border-top: .75pt solid #111; padding-top: 1.5mm; font-size: 9pt; color: #444; }}
footer {{ margin-top: 8mm; font-size: 8pt; color: #666; }}
@media print {{ body {{ padding: 0; }} }}
</style></head><body>
<header><div class="issuer">{issuer}</div>
<div class="title"><h1>Certificate of Data Erasure</h1><div class="cid">Certificate ID {e(cid)}</div></div></header>
<div class="result">Result: {e(rec["result"])}</div>
<div class="grid"><section><h2>Job</h2><table>{job}</table></section>
<section><h2>Time</h2><table>{timing}</table></section></div>
<h2>Drive</h2><table>{drive}</table>
<h2>Erasure</h2><table>{erasure}</table>
<p class="small">{e(NIST_NOTES.get(rec["nist_category"], ""))}</p>
<div class="verify">{qr}<div><div class="small">Certificate ID</div><div class="mono">{e(cid)}</div>
<div class="small" style="margin-top:2mm">SHA-256 of the erasure record</div><div class="mono">{digest}</div>
<div class="small" style="margin-top:2mm">The QR code holds the certificate ID and this hash. The record is
embedded in this file, so the hash can be recalculated to show the details have not been changed.</div></div></div>
<div class="sig"><div>Technician signature</div><div>Name</div><div>Date</div></div>
<footer>Issued{(" by " + e(company)) if company else ""}. Erasure performed with {e(rec["software"])}.</footer>
<script type="application/json" id="erasure-record">{record_json}</script>
</body></html>
"""
    return doc, cid, digest


def wipe_certificate(path: str, info: dict, branding: dict | None = None) -> str:
    """Write the certificate to ``path`` and return its ID."""
    doc, cid, _digest = render_certificate(info, branding)
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return cid


def verify_certificate(doc: str) -> tuple[bool, str]:
    """Recalculate the hash of the record embedded in a certificate and compare it with the printed one."""
    m = re.search(r'<script type="application/json" id="erasure-record">(.*?)</script>', doc, re.S)
    printed = re.search(r'SHA-256 of the erasure record</div><div class="mono">([0-9a-f]{64})<', doc)
    if not m or not printed:
        return False, ""
    rec = json.loads(m.group(1))  # "<\/" is a valid JSON escape for "</"
    digest = record_hash(rec)
    return digest == printed.group(1), digest
