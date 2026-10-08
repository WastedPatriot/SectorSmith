"""Apps for a user migration: what the old PC has installed, what can be put on the new one automatically (a package
in the Deploy library, or a winget package), and installing the chosen ones with the Deploy engine."""
from __future__ import annotations

import os
import re

from ..deploy import core
from ..util import Cancelled, get_logger

log = get_logger()

# (pattern on the Add/Remove Programs name, winget ID, name to detect it by, folders under Program Files or the
# user's AppData that show it is on an old disk). winget ID "" = can't be installed automatically (note says why).
CATALOG = [
    (r"^Google Chrome\b", "Google.Chrome", "Google Chrome", ("Google/Chrome/Application",)),
    (r"^Mozilla Firefox\b", "Mozilla.Firefox", "Mozilla Firefox", ("Mozilla Firefox",)),
    (r"^Mozilla Thunderbird\b", "Mozilla.Thunderbird", "Mozilla Thunderbird", ("Mozilla Thunderbird",)),
    (r"^Brave\b", "Brave.Brave", "Brave", ("BraveSoftware/Brave-Browser",)),
    (r"^Opera( Stable)?\b", "Opera.Opera", "Opera", ("AppData/Local/Programs/Opera",)),
    (r"^Vivaldi\b", "Vivaldi.Vivaldi", "Vivaldi", ("AppData/Local/Vivaldi",)),
    (r"^7-Zip\b", "7zip.7zip", "7-Zip", ("7-Zip",)),
    (r"^WinRAR\b", "RARLab.WinRAR", "WinRAR", ("WinRAR",)),
    (r"^Notepad\+\+", "Notepad++.Notepad++", "Notepad++", ("Notepad++",)),
    (r"^VLC media player\b", "VideoLAN.VLC", "VLC media player", ("VideoLAN/VLC",)),
    (r"^Adobe (Acrobat )?Reader\b|^Adobe Acrobat \(64-bit\)", "Adobe.Acrobat.Reader.64-bit", "Adobe Acrobat",
     ("Adobe/Acrobat DC", "Adobe/Acrobat Reader DC")),
    (r"^Foxit PDF Reader\b", "Foxit.FoxitReader", "Foxit PDF Reader", ("Foxit Software/Foxit PDF Reader",)),
    (r"^Zoom( Workplace)?\b", "Zoom.Zoom", "Zoom", ("Zoom/bin", "AppData/Roaming/Zoom/bin")),
    (r"^Slack\b", "SlackTechnologies.Slack", "Slack", ("Slack", "AppData/Local/slack")),
    (r"^Microsoft Teams\b", "Microsoft.Teams", "Microsoft Teams", ()),
    (r"^Spotify\b", "Spotify.Spotify", "Spotify", ("AppData/Roaming/Spotify",)),
    (r"^Dropbox\b", "Dropbox.Dropbox", "Dropbox", ("Dropbox/Client",)),
    (r"^Google Drive\b", "Google.GoogleDrive", "Google Drive", ("Google/Drive File Stream",)),
    (r"^Microsoft Visual Studio Code\b", "Microsoft.VisualStudioCode", "Microsoft Visual Studio Code",
     ("Microsoft VS Code", "AppData/Local/Programs/Microsoft VS Code")),
    (r"^Git( version [\d.]+)?$", "Git.Git", "Git", ("Git/cmd",)),
    (r"^PuTTY\b", "PuTTY.PuTTY", "PuTTY", ("PuTTY",)),
    (r"^WinSCP\b", "WinSCP.WinSCP", "WinSCP", ("WinSCP",)),
    (r"^FileZilla\b", "TimKosse.FileZilla.Client", "FileZilla", ("FileZilla FTP Client",)),
    (r"^KeePass Password Safe\b", "DominikReichl.KeePass", "KeePass", ("KeePass Password Safe 2",)),
    (r"^Bitwarden\b", "Bitwarden.Bitwarden", "Bitwarden", ("Bitwarden", "AppData/Local/Programs/Bitwarden")),
    (r"^Audacity\b", "Audacity.Audacity", "Audacity", ("Audacity",)),
    (r"^GIMP\b", "GIMP.GIMP", "GIMP", ("GIMP 2", "GIMP 3")),
    (r"^paint\.net\b", "dotPDN.PaintDotNet", "paint.net", ("paint.net",)),
    (r"^LibreOffice\b", "TheDocumentFoundation.LibreOffice", "LibreOffice", ("LibreOffice",)),
    (r"^Signal\b", "OpenWhisperSystems.Signal", "Signal", ("AppData/Local/Programs/signal-desktop",)),
    (r"^Telegram Desktop\b", "Telegram.TelegramDesktop", "Telegram Desktop", ("AppData/Roaming/Telegram Desktop",)),
    (r"^Discord\b", "Discord.Discord", "Discord", ("AppData/Local/Discord",)),
    (r"^Steam$", "Valve.Steam", "Steam", ("Steam",)),
    (r"^OBS Studio\b", "OBSProject.OBSStudio", "OBS Studio", ("obs-studio",)),
    (r"^Greenshot\b", "Greenshot.Greenshot", "Greenshot", ("Greenshot",)),
    (r"^ShareX\b", "ShareX.ShareX", "ShareX", ("ShareX",)),
    (r"^PowerToys\b", "Microsoft.PowerToys", "PowerToys", ("PowerToys",)),
    (r"^TeamViewer\b", "TeamViewer.TeamViewer", "TeamViewer", ("TeamViewer",)),
    (r"^AnyDesk\b", "AnyDeskSoftwareGmbH.AnyDesk", "AnyDesk", ("AnyDesk",)),
    (r"^Everything\b", "voidtools.Everything", "Everything", ("Everything",)),
    (r"^Malwarebytes\b", "Malwarebytes.Malwarebytes", "Malwarebytes", ("Malwarebytes/Anti-Malware",)),
    (r"^Node\.js\b", "OpenJS.NodeJS.LTS", "Node.js", ("nodejs",)),
    (r"^PowerShell 7\b", "Microsoft.PowerShell", "PowerShell 7", ("PowerShell/7",)),
    (r"^WireGuard\b", "WireGuard.WireGuard", "WireGuard", ("WireGuard",)),
    (r"^OpenVPN\b", "OpenVPNTechnologies.OpenVPN", "OpenVPN", ("OpenVPN",)),
    (r"^Obsidian\b", "Obsidian.Obsidian", "Obsidian", ("AppData/Local/Programs/Obsidian",)),
    (r"^Postman\b", "Postman.Postman", "Postman", ("AppData/Local/Postman",)),
    (r"^Microsoft (365|Office)\b", "", "Microsoft 365",
     ("Microsoft Office/root/Office16", "Microsoft Office/Office16")),
]
NOTES = {"Microsoft 365": "Install from office.com (or your RMM), then sign in to activate."}
# runtimes, drivers and Windows parts: the new PC has its own, or they come with the apps that need them
NOISE = re.compile(r"^(Microsoft Visual C\+\+|Microsoft \.NET|Microsoft Windows Desktop Runtime|Microsoft ASP\.NET|"
                   r"Windows Software Development Kit|Windows SDK|Microsoft Update Health|Windows PC Health|"
                   r"Update for|Security Update|Hotfix|Microsoft Edge|Microsoft OneDrive|Microsoft GameInput|"
                   r"Teams Machine-Wide Installer|Office 16 Click-to-Run|Microsoft Office \d+ Click-to-Run|"
                   r"Intel\(R\)|Intel® |Realtek|NVIDIA (Graphics Driver|PhysX|HD Audio|FrameView|USBC)|AMD |"
                   r"Dolby|Synaptics|ELAN|Conexant|Microsoft Intune|Windows Driver Package|Microsoft Policy|"
                   r"vs_|Microsoft Visual Studio Installer|Python Launcher|Java Auto Updater)", re.I)
# Program Files folders that are Windows or driver parts, not apps a person uses
FOLDER_NOISE = {"common files", "windows defender", "windows defender advanced threat protection", "windows mail",
                "windows media player", "windows multimedia platform", "windows nt", "windows photo viewer",
                "windows portable devices", "windows security", "windows sidebar", "windowsapps",
                "windowspowershell", "internet explorer", "microsoft update health tools", "modifiablewindowsapps",
                "reference assemblies", "msbuild", "uninstall information", "dotnet", "microsoft.net",
                "windows kits", "windows photo viewer", "microsoft", "microsoft analysis services", "package cache",
                "microsoft sql server", "iis", "iis express", "intel", "nvidia corporation", "realtek", "amd",
                "dell", "hp", "lenovo", "microsoft office", "microsoft onedrive"}


def _catalog(name: str):
    for pat, wid, det, folders in CATALOG:
        if re.search(pat, name, re.I):
            return pat, wid, det, folders
    return None


def _norm(name: str) -> str:
    """Name without version / architecture noise, for 'already on the new PC' matching."""
    n = re.sub(r"\(.*?\)|\b(x64|x86|64-bit|32-bit|version)\b|v?\d+(\.\d+)+", " ", name, flags=re.I)
    return re.sub(r"\s+", " ", n).strip().lower()


def clean_inventory(inv: list[dict]) -> list[dict]:
    """Add/Remove Programs entries a person would recognise as apps: no system parts, updates, runtimes, drivers
    or duplicates (the newest version wins)."""
    best: dict[str, dict] = {}
    for e in inv:
        name = (e.get("name") or "").strip()
        if not name or e.get("system_component") or NOISE.search(name) or re.search(r"\bKB\d{6,}\b", name):
            continue
        k = _norm(name)
        if k not in best or core.vtuple(e.get("version")) > core.vtuple(best[k].get("version")):
            best[k] = e
    return sorted(best.values(), key=lambda e: e["name"].lower())


def apps_on_disk(list_dir, path_exists, join, volume: str, profile: str) -> list[dict]:
    """Apps of an old Windows that isn't running, found by their folders (its registry isn't read).
    ``list_dir``/``path_exists``/``join`` work on the endpoint's paths, so raw NTFS paths work too."""
    out, seen = [], set()
    roots = [join(volume, "Program Files"), join(volume, "Program Files (x86)")]
    for pat, wid, det, folders in CATALOG:
        for f in folders:
            base = profile if f.startswith("AppData/") else None
            cands = [join(base, *f.split("/"))] if base else [join(r, *f.split("/")) for r in roots]
            if any(path_exists(c) for c in cands):
                out.append({"key": det, "name": det, "version": "", "publisher": "", "scope": "disk",
                            "found": "folder"})
                seen.add(f.split("/")[0].lower())
                break
    for r in roots + [join(profile, "AppData", "Local", "Programs")]:
        for n in list_dir(r):
            low = n.lower()
            if low in seen or low in FOLDER_NOISE or low.startswith(("microsoft", "windows", "{")):
                continue
            seen.add(low)
            out.append({"key": n, "name": n, "version": "", "publisher": "", "scope": "disk", "found": "folder"})
    return clean_inventory(out)


def match(apps: list[dict], store=None, dst_inv: list[dict] | None = None) -> list[dict]:
    """For each app: how it gets onto the new PC.
    source = installed (already there) | library (Deploy package) | winget | manual"""
    have = {_norm(e["name"]) for e in (dst_inv or [])}
    have_names = [e["name"].lower() for e in (dst_inv or [])]
    pkgs = list(store.packages) if store is not None else []
    out = []
    for e in apps:
        cat = _catalog(e["name"])
        det = cat[2] if cat else e["name"]
        row = {"name": e["name"], "version": e.get("version", ""), "publisher": e.get("publisher", ""),
               "source": "manual", "package_id": "", "winget_id": "", "detect": det, "note": ""}
        lib = next((p for p in pkgs if core._match_inventory(p, [e])), None)
        if _norm(e["name"]) in have or (cat and any(n.startswith(det.lower()) for n in have_names)):
            row.update(source="installed", note="Already on the new PC")
        elif lib is not None:
            row.update(source="library", package_id=lib.id, note=f"Deploy library: {lib.name} {lib.version}".strip())
        elif cat and cat[1]:
            row.update(source="winget", winget_id=cat[1], note=f"winget: {cat[1]}")
        else:
            row["note"] = NOTES.get(det, "Install it yourself (no package or winget ID known)")
        out.append(row)
    order = {"library": 0, "winget": 1, "manual": 2, "installed": 3}
    out.sort(key=lambda r: (order[r["source"]], r["name"].lower()))
    return out


class MigrationStore(core.Store):
    """The Deploy library plus one-off winget packages and 'install this' deployments for one PC, kept in memory.
    Sessions are saved to the real library, so the install shows on the Deploy Sessions tab."""

    def __init__(self, base: core.Store, packages: list, deployments: list):
        super().__init__(base.root)
        self.base = base
        # only this migration's packages and deployments, not the whole library the base just loaded
        self.data.clear()
        self.data.update(packages=packages, tasks=[], clients=[], deployments=deployments)

    def save(self, kind=None):
        pass


def build(rows: list[dict], store, hostname: str) -> MigrationStore:
    pkgs, deps = [], []
    for r in rows:
        if r["source"] == "library":
            p = store.get("packages", r["package_id"])
        elif r["source"] == "winget":
            p = core.Package(name=r["detect"], kind="winget", winget_id=r["winget_id"], install=core.WINGET_INSTALL,
                             uninstall=core.WINGET_UNINSTALL, detection={"method": "registry", "value": r["detect"]})
        else:
            continue
        if p is None:
            continue
        if all(x.id != p.id for x in pkgs):
            pkgs.append(p)
            deps.append(core.Deployment("software", p.id, "installed", target_kind="machine", target_value=hostname))
    for p in list(pkgs):  # prerequisites of library packages come along
        for pre in p.prerequisites:
            q = store.get("packages", pre)
            if q is not None and all(x.id != q.id for x in pkgs):
                pkgs.append(q)
    return MigrationStore(store, pkgs, deps)


def install(dst, rows: list[dict], store, prog) -> dict:
    """Install the chosen apps on the destination PC with the Deploy engine (detect, install, check again).
    Returns {"apps": [{name, status, result}], "session": path}. Problems are reported, not raised."""
    host = dst.info()["hostname"]
    chosen = [r for r in rows if r["source"] in ("library", "winget")]
    if not chosen:
        return {"apps": [], "session": None}
    if store is None:
        import tempfile
        store = core.Store(tempfile.mkdtemp(prefix="ssmigrate_"))
    ms = build(chosen, store, host)
    prog.set_label(f"Installing {len(chosen)} app(s) on {dst.label}…")
    try:
        sess = core.run_session(dst, ms, prog, mode="full")
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001  (the files still get copied)
        log.warning("Installing apps on %s failed: %s", dst.label, e)
        return {"apps": [{"name": r["name"], "status": "failed", "result": str(e)} for r in chosen], "session": None}
    apps = [{"name": a["name"], "status": a["status"],
             "result": a.get("result") or ("already installed" if a.get("action") == "none" else "")}
            for a in sess["actions"]]
    return {"apps": apps, "session": sess.get("path")}


def app_list_text(rows: list[dict], results: list[dict] | None = None) -> str:
    done = {a["name"].lower(): a for a in (results or [])}
    lines = ["Apps from the old PC", ""]
    for r in rows:
        a = done.get(r["detect"].lower()) or done.get(r["name"].lower())
        state = (f"{a['status']}: {a.get('result', '')}" if a else
                 {"installed": "already on the new PC", "manual": "install it yourself"}.get(r["source"], "not chosen"))
        lines.append(f"  {r['name']} {r.get('version', '')}".rstrip() + f"  ->  {state}"
                     + (f"  ({r['note']})" if r.get("note") and r["source"] == "manual" else ""))
    return "\n".join(lines) + "\n"


def volume_of(profile: str) -> str:
    """Drive a profile folder lives on (two levels above C:\\Users\\name)."""
    from .. import offline
    if offline.is_raw(profile):
        disk, lba, inner = offline.parse_raw(profile)
        parts = inner.split("/")
        return offline.raw_root(disk, lba, "/".join(parts[:-2]))
    p = profile.rstrip("\\/")
    return os.path.dirname(os.path.dirname(p)) or p
