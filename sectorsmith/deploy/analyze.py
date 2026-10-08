"""Installer analysis: read an MSI/EXE/MSIX and suggest name, version, silent switches and detection."""
from __future__ import annotations

import os
import re
import struct

# --------------------------------------------------------------------------- MSI (pure Python, via olefile)
_B64 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._"


def _msi_name(raw: str) -> str:
    out = []
    for ch in raw:
        c = ord(ch)
        if c == 0x4840:
            out.append("!")
        elif 0x3800 <= c < 0x4800:
            c -= 0x3800
            out.append(_B64[c & 0x3F])
            out.append(_B64[(c >> 6) & 0x3F])
        elif 0x4800 <= c < 0x4840:
            out.append(_B64[c - 0x4800])
        else:
            out.append(ch)
    return "".join(out)


def msi_properties(path: str) -> dict:
    """Return the MSI Property table as a dict (ProductName, ProductVersion, ProductCode, …)."""
    import olefile
    ole = olefile.OleFileIO(path)
    try:
        streams = {_msi_name(e[0]): e for e in ole.listdir(streams=True, storages=False) if len(e) == 1}
        pool = ole.openstream(streams["!_StringPool"]).read()
        data = ole.openstream(streams["!_StringData"]).read()
        codepage, flags = struct.unpack_from("<HH", pool, 0)
        long_refs = bool(flags & 0x8000)
        enc = "utf-8" if codepage == 65001 else (f"cp{codepage}" if codepage else "cp1252")
        strings = [""]
        pos = 0
        i = 4
        while i + 4 <= len(pool):
            ln, refs = struct.unpack_from("<HH", pool, i)
            i += 4
            if ln == 0 and refs:  # string longer than 64 KiB: length in the next entry
                ln = struct.unpack_from("<I", pool, i)[0]
                i += 4
            s = data[pos:pos + ln]
            pos += ln
            try:
                strings.append(s.decode(enc))
            except (UnicodeDecodeError, LookupError):
                strings.append(s.decode("latin-1"))
        tbl = ole.openstream(streams["!Property"]).read()
        ref = 3 if long_refs else 2
        rows = len(tbl) // (2 * ref)

        def sref(off):
            v = int.from_bytes(tbl[off:off + ref], "little")
            return strings[v] if v < len(strings) else ""
        return {sref(r * ref): sref(rows * ref + r * ref) for r in range(rows)}
    finally:
        ole.close()


# --------------------------------------------------------------------------- EXE
FRAMEWORKS = [  # (marker bytes, name, silent install args, silent uninstall args)
    (b"Inno Setup Setup Data", "Inno Setup", "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-", "/VERYSILENT /NORESTART"),
    (b"InnoSetupLdrWindow", "Inno Setup", "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-", "/VERYSILENT /NORESTART"),
    (b"Nullsoft.NSIS", "NSIS", "/S", "/S"),
    (b"NullsoftInst", "NSIS", "/S", "/S"),
    (b".wixburn", "WiX Burn bundle", "/quiet /norestart", "/uninstall /quiet /norestart"),
    (b"InstallShield", "InstallShield", '/s /v"/qn /norestart"', '/s /x /v"/qn"'),
    (b"Advanced Installer", "Advanced Installer", "/exenoui /qn /norestart", "/exenoui /qn"),
    (b"Squirrel", "Squirrel", "--silent", "--uninstall -s"),
    (b"7-Zip", "7-Zip SFX", "-y", ""),
]
_VER_KEYS = ["ProductName", "CompanyName", "FileVersion", "ProductVersion", "FileDescription"]


def _ver_string(blob: bytes, key: str) -> str | None:
    k = key.encode("utf-16-le") + b"\0\0"
    i = blob.find(k)
    while i != -1:
        j = i + len(k)
        while j < len(blob) - 1 and blob[j:j + 2] == b"\0\0":
            j += 2
        if j % 2:
            j += 1
        end = j
        while end < len(blob) - 1 and blob[end:end + 2] != b"\0\0":
            end += 2
        try:
            val = blob[j:end].decode("utf-16-le").strip()
        except UnicodeDecodeError:
            val = ""
        if val and len(val) < 200 and val.isprintable():
            return val
        i = blob.find(k, i + 1)
    return None


def exe_info(path: str) -> dict:
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(min(size, 24 * 1024 * 1024))
        tail = b""
        if size > len(head):
            f.seek(max(len(head), size - 4 * 1024 * 1024))
            tail = f.read()
    blob = head + tail
    info = {k: _ver_string(blob, k) for k in _VER_KEYS}
    fw = next(((n, a, u) for m, n, a, u in FRAMEWORKS if m in blob), None)
    info["framework"], info["silent"], info["uninstall_args"] = fw if fw else (None, None, None)
    info["is_pe"] = head[:2] == b"MZ"
    return info


# --------------------------------------------------------------------------- suggestions
def clean_version(v: str | None) -> str:
    if not v:
        return ""
    m = re.search(r"\d+(?:[.,]\d+){0,3}", v)
    return m.group(0).replace(",", ".") if m else v.strip()


def analyze(path: str) -> dict:
    """Suggest a package definition for an installer file."""
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    s = {"installer": name, "kind": "exe", "name": os.path.splitext(name)[0], "publisher": "", "version": "",
         "install": "", "uninstall": "", "detection": {"method": "registry", "value": ""}, "notes": [],
         "success_codes": [0, 3010, 1641], "language": "powershell"}
    if ext == ".msi":
        p = msi_properties(path)
        s.update(kind="msi", name=p.get("ProductName", s["name"]), publisher=p.get("Manufacturer", ""),
                 version=p.get("ProductVersion", ""), product_code=p.get("ProductCode", ""),
                 upgrade_code=p.get("UpgradeCode", ""))
        s["install"] = 'msiexec.exe /i "{installer}" /qn /norestart ALLUSERS=1'
        s["uninstall"] = "msiexec.exe /x {product_code} /qn /norestart"
        s["detection"] = {"method": "registry", "value": s["name"]}
        s["notes"].append("MSI: silent switches are standard (/qn). Detection matches the Add/Remove Programs name.")
    elif ext in (".msix", ".msixbundle", ".appx", ".appxbundle"):
        s.update(kind="msix")
        s["install"] = 'powershell -NoProfile -Command "Add-AppxProvisionedPackage -Online -PackagePath \'{installer}\' -SkipLicense"'
        s["uninstall"] = ""
        s["detection"] = {"method": "script", "value": ""}
        s["notes"].append("MSIX is provisioned for all users. Add a detection script (Get-AppxPackage).")
    else:
        e = exe_info(path)
        s["name"] = e.get("ProductName") or e.get("FileDescription") or s["name"]
        s["publisher"] = e.get("CompanyName") or ""
        s["version"] = clean_version(e.get("ProductVersion") or e.get("FileVersion"))
        if e["silent"]:
            s["install"] = f'"{{installer}}" {e["silent"]}'
            s["uninstall"] = "{registry_uninstall} " + (e["uninstall_args"] or "")
            s["notes"].append(f"Detected installer type: {e['framework']} — standard silent switches applied.")
        else:
            s["install"] = '"{installer}" /S'
            s["uninstall"] = "{registry_uninstall}"
            s["notes"].append("Installer type not recognised — '/S' is a guess. Test it on one machine first, or check "
                              "the vendor's docs for silent switches.")
        s["detection"] = {"method": "registry", "value": s["name"]}
    s["version"] = clean_version(s["version"])
    return s
