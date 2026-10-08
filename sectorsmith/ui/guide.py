"""In-app how-to guides and the first-run welcome tour."""
from __future__ import annotations

import json

import customtkinter as ctk

from ..util import app_dir
from . import theme
from .widgets import IconBadge

P = theme.PALETTE

GUIDES = {
    "start": {
        "title": "Getting started", "icon": "advanced", "tone": "violet",
        "intro": "SectorSmith turns pro disk tools into simple, guided tasks. Every task follows the same pattern.",
        "steps": [
            ("Run as administrator", "The app asks for admin automatically. Without it, Windows hides your "
                                     "physical drives (disk images still work)."),
            ("Pick a task on Home", "Six big buttons cover the everyday jobs. Each opens a step-by-step wizard."),
            ("Choose a drive", "Drives are listed with their letters, labels, sizes and free space. The disk "
                               "Windows runs from is marked SYSTEM and protected."),
            ("Follow the steps", "The coloured dots at the top right show where you are. Mossbit sweeps the "
                                 "progress bar while work happens — Cancel is always available."),
        ],
        "tips": ["Drop a .img or .vhd file onto the window to work on a disk image like a real drive.",
                 "Switch Light / Dark / System in the bottom-left. Your choice is remembered.",
                 "Logs, backups and settings live in %LOCALAPPDATA%\\SectorSmith."],
    },
    "recover": {
        "title": "Recover deleted files", "icon": "recover", "tone": "accent", "task": "RecoverWizard",
        "intro": "Bring back files that were deleted, emptied from the Recycle Bin, or lost when a drive was "
                 "formatted.",
        "steps": [
            ("Stop using the drive", "Every new file written can overwrite what you want back. Don't install "
                                     "anything onto it — run SectorSmith from another drive or USB stick."),
            ("Pick the partition", "Choose the drive letter the files were on (for example D:). For a drive "
                                   "that shows as RAW or empty, pick the whole disk."),
            ("Choose Quick or Deep", "Quick scan reads the NTFS file table: original names, folders and dates, "
                                     "usually in seconds. Deep scan finds files by their content on any drive "
                                     "(photos, documents, video, archives) — names aren't kept."),
            ("Select and recover", "Filter by type or search by name, tick the rows you need, press Recover, "
                                   "and save them to a different drive."),
        ],
        "tips": ["'Chance: Excellent' means the file's data is still allocated to it; 'Good' means it was "
                 "deleted but not yet overwritten.",
                 "Try Quick scan first. If nothing useful turns up, run Deep scan.",
                 "Always save recovered files to a different drive from the one you're scanning."],
    },
    "partition": {
        "title": "Find lost partitions", "icon": "partition", "tone": "violet", "task": "PartitionWizard",
        "intro": "Use this when a drive suddenly shows as unallocated, RAW or 'needs formatting', or a partition "
                 "was deleted by mistake.",
        "steps": [
            ("Pick the disk", "Choose the whole disk, not a partition."),
            ("Let the quick search run", "It checks every 1 MiB boundary and the classic positions. Lost "
                                         "partitions show a red 'Missing' tag."),
            ("Go deeper if needed", "Deep search reads every sector. It's slower but finds partitions in "
                                    "unusual places and from backup boot sectors."),
            ("Bring them back", "Tick the missing ones and confirm. The partition table is backed up "
                                "automatically before anything is written."),
        ],
        "tips": ["Afterwards, Windows may need a rescan: Disk Management → Action → Rescan Disks.",
                 "Not sure? Use 'Recover files → Deep scan' on the disk to copy data off first.",
                 "The backup (first and last 1 MiB of the disk) can be restored from Expert tools → Overview."],
    },
    "wipe": {
        "title": "Erase and certify", "icon": "wipe", "tone": "danger", "task": "WipeWizard",
        "intro": "Securely erase a whole disk or one partition before it's reused, returned or disposed of.",
        "steps": [
            ("Pick the target", "A whole disk, or a single partition if the rest must be kept. The system disk "
                                "can't be selected."),
            ("Use the drive's own erase", "For a whole SATA or NVMe drive, SectorSmith asks the drive what it "
                                          "supports (ATA Secure Erase, NVMe Sanitize or Format). Leave it on: it "
                                          "counts as NIST 800-88 Purge. If it isn't offered, the overwrite runs."),
            ("Choose how thorough", "Quick = one pass of zeros. Recommended = NIST 800-88 with verification. "
                                    "Thorough = DoD 3-pass with verification. More standards are in the "
                                    "dropdown."),
            ("Type to confirm", "Type the exact phrase shown (for example WIPE DISK 2). This stops accidents."),
            ("Save the certificate", "When it finishes, save the certificate and print it to PDF. It carries your "
                                     "company name and logo (Settings, Branding), client, ticket, drive, method, "
                                     "NIST category, verification and a QR code with its ID and hash."),
        ],
        "tips": ["One verified pass is enough for modern hard drives. Extra passes mostly add time.",
                 "A frozen drive refuses its own erase until it is power-cycled: sleep the PC and wake it, or "
                 "unplug the drive and plug it back in, then try again.",
                 "USB enclosures and RAID controllers usually block the drive's own erase. Connect the drive "
                 "directly, or overwrite it and destroy it if it leaves the business.",
                 "Volumes on the target disk are locked and dismounted while the wipe runs."],
    },
    "shred": {
        "title": "Shred & clean free space", "icon": "shred", "tone": "warn", "task": "ShredWizard",
        "intro": "Destroy specific files so they can't be recovered, or erase the traces of files deleted "
                 "earlier.",
        "steps": [
            ("Drop files in", "Drag files or folders onto the window (from anywhere in the app), or use Add "
                              "files / Add folder."),
            ("Or pick Free space", "Switch to 'Free space on a drive' to overwrite the empty area of a drive. "
                                   "Your existing files are kept."),
            ("Choose strength and confirm", "Pick Quick / Recommended / Thorough and type SHRED for files."),
        ],
        "tips": ["Shredded files are renamed and truncated before deletion, so even their names are gone.",
                 "On SSDs, file-level shredding is best-effort. Wipe the whole drive when disposing of it."],
    },
    "health": {
        "title": "Health check", "icon": "health", "tone": "success", "task": "HealthWizard",
        "intro": "Read every sector and get a plain-English verdict. Nothing is changed during a normal test.",
        "steps": [
            ("Pick a drive or partition", "Testing a partition is quicker when you only care about one area."),
            ("Watch the map", "Each square is a slice of the drive: green is fast, yellow and orange are slow, "
                              "red is unreadable."),
            ("Read the verdict", "Healthy, slow in places, or bad sectors found — with what to do next."),
        ],
        "tips": ["Bad sectors? Back the drive up straight away with Image and clone, then replace it.",
                 "'Try repairing' rewrites unreadable sectors so the drive swaps them for spares. Whatever was "
                 "in them is already lost."],
    },
    "clone": {
        "title": "Image and clone", "icon": "clone", "tone": "violet", "task": "CloneWizard",
        "intro": "Make an exact sector-by-sector copy of a drive or partition — into a file, or onto another "
                 "drive.",
        "steps": [
            ("Pick the source", "A whole drive, or a single partition."),
            ("Choose where it goes", ".vhd image (Windows can mount it in Disk Management), .img raw image, or "
                                     "another drive (everything on it is replaced)."),
            ("Start the copy", "A fast pass runs first, then unreadable areas are retried one sector at a "
                               "time. A SHA-256 hash and a log are saved next to the image."),
        ],
        "tips": ["Imaging a failing drive first, then recovering from the image, is the safest approach.",
                 "Open any image later by dropping it onto the window."],
    },
    "upgrade": {
        "title": "Upgrade to a new SSD / NVMe", "icon": "clone", "tone": "success", "task": "CloneWizard",
        "intro": "Move a whole PC onto a new, bigger drive using a USB enclosure or adapter — no reinstall.",
        "steps": [
            ("Plug in the new drive", "Put the new SSD/NVMe in a USB enclosure and plug it into the PC (or into a "
                                      "linked PC). It shows up marked USB."),
            ("Image and clone → Another drive", "Pick the disk Windows runs from as the source and the USB drive "
                                                 "as the destination. Over the network? Use Clone a disk to another "
                                                 "PC instead."),
            ("Let it snapshot and copy", "Windows keeps running: SectorSmith takes a Volume Shadow Copy snapshot "
                                         "first so every file is copied as it was at that moment."),
            ("Swap the drives", "Fit the new drive inside the PC and boot from it."),
            ("Use the extra space", "A bigger drive's extra space appears as unallocated. In Disk Management "
                                    "extend C: (if a Recovery partition sits in the way, extend into the space "
                                    "after it or move it with Expert tools)."),
        ],
        "tips": ["The partition table is fitted to the bigger disk automatically.",
                 "The new drive must be at least as big as the old one (sector-for-sector copy).",
                 "BitLocker? Suspend protection before swapping drives, then resume after the first boot."],
    },
    "connect": {
        "title": "Connect a machine", "icon": "link", "tone": "accent", "task": "ConnectScreen",
        "intro": "Link another PC on the same network so you can work on both from one window — perfect when "
                 "you're remoted into a new machine.",
        "steps": [
            ("Open Connect a machine", "SectorSmith starts a secure listener on this PC and shows a one-line "
                                       "PowerShell command."),
            ("Run it on the other PC", "Remote in (or use your RMM's PowerShell shell), open PowerShell as "
                                       "administrator, paste, Enter. It downloads SectorSmith from this PC, "
                                       "checks its SHA-256, and links up."),
            ("Work with both", "The PC appears under Machines and in the Working on panel. Use it in Migrate "
                               "user or Network clone."),
        ],
        "tips": ["The link is TLS-encrypted with a one-time certificate pinned in the command, plus a random token. "
                 "A different PC can't impersonate this one.",
                 "Both PCs need to reach each other on the network (same site / VPN). Allow SectorSmith through "
                 "the firewall when Windows asks.",
                 "The other PC disconnects automatically when you close SectorSmith or press Disconnect."],
    },
    "migrate": {
        "title": "Migrate user", "icon": "migrate", "tone": "success", "task": "MigrateWizard",
        "intro": "Copy a user's folders and app data from their old PC to their new one — no USB drives, no "
                 "reboots.",
        "steps": [
            ("Link the new PC", "Use Connect a machine. Ideally run SectorSmith on the old PC and link the new "
                                "one (either way works)."),
            ("Have the user sign in once on the new PC", "That creates their profile folder, so files land with "
                                                         "the right permissions."),
            ("Pick from and to", "Choose the old PC + user, then the new PC + user. Old PC won't start? Plug its "
                                 "disk or NVMe in by USB: its users show under 'On other drives'. To keep a copy "
                                 "on a USB disk instead, pick a drive under New folder."),
            ("Choose what comes across", "Desktop, Documents, Downloads, Pictures and more, plus Chrome, Edge, "
                                         "Firefox, Outlook signatures, Sticky Notes and Office templates. Sizes "
                                         "are shown before you start."),
            ("Bring the apps", "The old PC's apps are listed. Ones in your Deploy library or on winget install "
                               "silently on the new PC; the rest are listed for you to install."),
            ("Run it — then run it again", "The first run copies everything. Later runs copy only new or changed "
                                           "files, so you can pre-stage the day before and do a quick final sync."),
        ],
        "tips": ["Close the user's apps (or sign them out) on the old PC for a clean copy — open files are listed "
                 "in the report so you can re-run.",
                 "Chrome/Edge saved passwords are locked to the old PC by Windows. Export them first (or rely on "
                 "browser sync).",
                 "OneDrive folders are off by default — they re-sync on their own when the user signs in.",
                 "A report of every run is saved in %LOCALAPPDATA%\\SectorSmith\\reports.",
                 "Cancel is safe: what was copied stays, and Run again carries on from there.",
                 ("A disk with no drive letter? 'Drives without a letter' reads its NTFS partition directly "
                  "(read-only). BitLocker disks need unlocking in Windows first.")],
    },
    "netclone": {
        "title": "Clone a disk to another PC", "icon": "clone", "tone": "violet", "task": "NetCloneWizard",
        "intro": "Copy a whole disk, sector by sector, from one PC to another over the network.",
        "steps": [
            ("Boot the destination from USB", "A running Windows PC can't overwrite its own system disk. Boot the "
                                              "new PC from a SectorSmith USB stick: it shows an address and a "
                                              "pairing code."),
            ("Link it", "Connect a machine → PC booted from USB → type the address and code. Check that the "
                        "6-character check code matches on both screens."),
            ("Pick source and destination disks", "Choose the old PC's disk, then the new PC's disk."),
            ("Confirm and clone", "Empty areas are skipped on the wire, data is compressed, and every block is "
                                  "verified with SHA-256 on both ends."),
        ],
        "tips": ["For data disks (not the Windows disk) both PCs can stay running — just link with the command.",
                 "Cloning Windows onto different hardware can need driver updates on first boot. For a new PC, "
                 "Migrate user is usually the better choice.",
                 "Make the USB stick with tools/make_usb.ps1 (needs the free Windows ADK + WinPE add-on)."],
    },
    "deploy": {
        "title": "Deploy software", "icon": "deploy", "tone": "violet", "task": "DeployScreen",
        "intro": "Keep your linked PCs in line: install and update apps, remove ones that shouldn't be there, and "
                 "run upkeep tasks. Deploy is a preview, so try new packages on one PC first.",
        "steps": [
            ("Add a package", "Drop an MSI or EXE on the Library tab. SectorSmith reads it and suggests the silent "
                              "install switches and how to tell it's installed. Check them, change anything, save. "
                              "winget IDs and plain scripts work too."),
            ("Say where it goes", "On Deployments, pick a package or task, then every PC, a client (a named group "
                                  "of PCs) or one PC, and what you want: kept up to date, a set version, or removed."),
            ("Add upkeep tasks", "A task is a check script (exit 0 means the PC is fine) and a fix script that "
                                 "runs when the check fails. Use Check only to report without changing anything."),
            ("Run maintenance", "Pick linked PCs. SectorSmith first checks what each one needs and shows you. "
                                "Nothing changes until you type the confirmation, then it installs, fixes and "
                                "checks again."),
            ("Look back at sessions", "Every run is saved on the Sessions tab with what was found, what changed "
                                      "and the installer output."),
        ],
        "tips": [("Export scripts writes Install.ps1, Uninstall.ps1 and Detect.ps1 (plus the installer) to a "
                  "folder, ready for your RMM or for running by hand."),
                 ("Installer type not recognised? The /S switch is only a guess. Check the vendor's docs and test "
                  "on one PC."),
                 ("The library lives in %LOCALAPPDATA%\\SectorSmith\\deploy. Copy that folder to take it to "
                  "another PC.")],
    },
    "advanced": {
        "title": "Expert tools", "icon": "advanced", "tone": "violet",
        "intro": "The classic tabbed interface for experienced technicians — opens in its own window.",
        "steps": [
            ("Sector editor", "View any sector in hex, decode values in the data inspector, search the disk, and "
                              "edit bytes (typed confirmation required)."),
            ("Partition table tools", "Back up or restore the partition-table area of any disk."),
            ("Everything else", "Wipe, surface test, imaging, partition and file recovery with every option "
                                "exposed."),
        ],
        "tips": ["Launch straight into this view with SectorSmith.exe --classic."],
    },
}
ORDER = ["start", "recover", "partition", "wipe", "shred", "health", "clone", "upgrade", "connect", "migrate",
         "netclone", "deploy", "advanced"]


# ---------------------------------------------------------------------------
def load_settings() -> dict:
    try:
        return json.load(open(app_dir() / "settings.json"))
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    s = load_settings()
    s.update(kw)
    try:
        json.dump(s, open(app_dir() / "settings.json", "w"), indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------------------
from .screens import Screen  # noqa: E402  (screens imports nothing from here at module load)


class GuideScreen(Screen):
    def __init__(self, master, app, topic="start"):
        super().__init__(master, app, "How to use SectorSmith", "Short guides for every task.")
        app.mascot.set_mood("idle", "guide")
        body = self.new_body()
        nav = ctk.CTkFrame(body, fg_color=P["panel"], corner_radius=20, width=250)
        nav.pack(side="left", fill="y", padx=(0, 18))
        nav.pack_propagate(False)
        self.nav_btns = {}
        for key in ORDER:
            g = GUIDES[key]
            b = ctk.CTkButton(nav, text=g["title"], anchor="w", height=40, corner_radius=14,
                              font=theme.font(13, "bold"), fg_color="transparent", hover_color=P["card_hover"],
                              text_color=P["text"], command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=10, pady=(10 if key == "start" else 2, 0))
            self.nav_btns[key] = b
        self.page = ctk.CTkScrollableFrame(body, fg_color="transparent")
        self.page.pack(side="left", fill="both", expand=True)
        self.show(topic)

    def show(self, key):
        for k, b in self.nav_btns.items():
            b.configure(fg_color=P["accent_soft"] if k == key else "transparent",
                        text_color=P["accent"] if k == key else P["text"])
        for w in self.page.winfo_children():
            w.destroy()
        g = GUIDES[key]
        head = ctk.CTkFrame(self.page, fg_color="transparent")
        head.pack(fill="x")
        b = IconBadge(head, g["icon"], 58, g["tone"])
        b.set_bg(theme.c("bg"))
        b.pack(side="left")
        t = ctk.CTkFrame(head, fg_color="transparent")
        t.pack(side="left", padx=14, fill="x", expand=True)
        ctk.CTkLabel(t, text=g["title"], font=theme.font(22, "bold"), text_color=P["text"], anchor="w").pack(
            anchor="w")
        ctk.CTkLabel(t, text=g["intro"], font=theme.font(13), text_color=P["muted"], anchor="w", justify="left",
                     wraplength=540).pack(anchor="w")
        for i, (title, text) in enumerate(g["steps"], 1):
            card = ctk.CTkFrame(self.page, corner_radius=18, fg_color=P["card"])
            card.pack(fill="x", pady=5)
            ctk.CTkLabel(card, text=str(i), width=34, height=34, corner_radius=17, fg_color=P["accent"],
                         text_color="#ffffff", font=theme.font(14, "bold")).pack(side="left", padx=16, pady=14,
                                                                                 anchor="n")
            tt = ctk.CTkFrame(card, fg_color="transparent")
            tt.pack(side="left", fill="x", expand=True, pady=12, padx=(0, 16))
            ctk.CTkLabel(tt, text=title, font=theme.font(15, "bold"), text_color=P["text"], anchor="w").pack(
                anchor="w")
            ctk.CTkLabel(tt, text=text, font=theme.font(13), text_color=P["muted"], anchor="w", justify="left",
                         wraplength=520).pack(anchor="w")
        if g["tips"]:
            tips = ctk.CTkFrame(self.page, corner_radius=18, fg_color=P["violet_soft"])
            tips.pack(fill="x", pady=(10, 4))
            ctk.CTkLabel(tips, text="Good to know", font=theme.font(13, "bold"), text_color=P["violet"]).pack(
                anchor="w", padx=18, pady=(12, 2))
            for tip in g["tips"]:
                ctk.CTkLabel(tips, text="•  " + tip, font=theme.font(13), text_color=P["text"], anchor="w",
                             justify="left", wraplength=580).pack(anchor="w", padx=18, pady=2)
            ctk.CTkFrame(tips, height=8, fg_color="transparent").pack()
        for w in self.footer.winfo_children():
            w.destroy()
        if g.get("task"):
            from . import deploy_screens, link_screens, screens
            mod = next((m for m in (link_screens, deploy_screens) if hasattr(m, g["task"])), screens)
            self.buttons(primary=(f"Start: {g['title']}", lambda: self.app.go(getattr(mod, g["task"]))))
        elif key == "advanced":
            self.buttons(primary=("Open Expert tools", self.app.open_advanced))


WELCOME = [
    ("happy", "Meet Mossbit", "Hi! I'm Mossbit. I'll help you recover files, wipe drives, check drive health and "
                              "more — one simple step at a time."),
    ("sweep", "Pick a task, follow the steps", "Choose a job on the Home screen, pick a drive, and follow the "
                                               "steps. I'll sweep the progress bar while I work."),
    ("warn", "Safe by design", "The Windows system disk is protected, anything permanent asks you to type a "
                               "confirmation, and partition tables are backed up before changes."),
]


class WelcomeScreen(Screen):
    def __init__(self, master, app):
        super().__init__(master, app, "", "", show_back=False)
        self.i = 0
        self._render()

    def _render(self):
        from .mascot import Sprites
        mood, title, text = WELCOME[self.i]
        body = self.new_body()
        card = ctk.CTkFrame(body, corner_radius=28, fg_color=P["card"])
        card.pack(expand=True, fill="both", padx=40, pady=(10, 0))
        import tkinter as tk
        frames, fps, w, h = Sprites.get(self, {"happy": "happy", "sweep": "sweep", "warn": "warn"}[mood], 5)
        cv = tk.Canvas(card, width=w, height=h, highlightthickness=0, bd=0, background=theme.c("card"))
        if theme.effective_personality() == "Off":
            from .shell import ProductMark
            cv, frames = ProductMark(card, 96, bg="surface"), None
        cv.pack(pady=(30, 4))
        start = [0]

        def anim():
            if not frames:
                return
            try:
                cv.delete("all")
                cv.create_image(0, 0, anchor="nw", image=frames[start[0] % len(frames)])
                start[0] += 1
                cv.after(int(1000 / fps), anim)
            except Exception:  # noqa: BLE001
                pass
        anim()
        ctk.CTkLabel(card, text=title, font=theme.font(30, "bold"), text_color=P["text"]).pack()
        ctk.CTkLabel(card, text=text, font=theme.font(15), text_color=P["muted"], wraplength=560,
                     justify="center").pack(pady=(8, 18))
        dots = ctk.CTkFrame(card, fg_color="transparent")
        dots.pack(pady=(0, 30))
        for k in range(len(WELCOME)):
            ctk.CTkFrame(dots, width=28 if k == self.i else 10, height=10, corner_radius=5,
                         fg_color=P["accent"] if k == self.i else P["track"]).pack(side="left", padx=4)
        last = self.i == len(WELCOME) - 1
        self.buttons(primary=("Let's go!" if last else "Next", self._next), secondary=("Skip", self._finish))

    def _next(self):
        if self.i < len(WELCOME) - 1:
            self.i += 1
            self._render()
        else:
            self._finish()

    def _finish(self):
        save_settings(welcomed=True)
        self.app.home()
