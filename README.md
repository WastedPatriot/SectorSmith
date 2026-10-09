<div align="center">

<img src="docs/images/hero.png" alt="SectorSmith, the bench toolkit for MSP technicians" width="100%">

<br>

[![Build & test](https://github.com/WastedPatriot/sectorsmith/actions/workflows/build.yml/badge.svg)](https://github.com/WastedPatriot/sectorsmith/actions/workflows/build.yml)
![Windows 10 / 11](https://img.shields.io/badge/Windows-10%20%7C%2011-0078D4?logo=windows&logoColor=white)
![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Portable](https://img.shields.io/badge/single%20.exe-portable-52C98E)
![Version](https://img.shields.io/badge/version-1.4.0-4F46E5)
![Link](https://img.shields.io/badge/Link-TLS%20encrypted-7656F5)
[![License: Proprietary](https://img.shields.io/badge/license-proprietary-lightgrey)](LICENSE)

**The bench toolkit for MSP technicians.**
<br>Recover files · find lost partitions · erase and certify drives · test drive health · image and clone · migrate users with their apps · deploy software

[**Download**](#-download) · [**Quick start**](#-quick-start) · [**How-to guides**](#-how-to-guides) · [**Safety**](#-safety-rails) · [**FAQ**](#-faq) · [**Build from source**](#-build-from-source)

</div>

---

## ✨ Highlights

<table>
<tr>
<td width="50%" valign="top">

### Built around the job
The menu is grouped by what you're doing: **Recover, Erase, Drives, Machines, Manage, Jobs**. Every wizard follows the same pattern, **pick → choose → confirm → done**, and **Ctrl+K** finds any job, drive or machine from anywhere.

</td>
<td width="50%" valign="top">

### Client on every job
Pick the client in the top bar. Every job, report and certificate is stamped with **client and technician**, and the Jobs page keeps a history of what ran, where and for whom, with filters and CSV export.

</td>
</tr>
<tr>
<td valign="top">

### Migrate users, with their apps
Paste **one PowerShell command** on the new PC (remote session or RMM shell), or plug the old disk into a USB caddy. Pick the user and what comes across, and SectorSmith reinstalls their apps before copying files over an **encrypted link**. Run it again later and only changes are copied.

</td>
<td valign="top">

### Erase and certify
Uses the drive's own **ATA Secure Erase or NVMe Sanitize** when it can (NIST 800-88 Purge), checks the result, and produces a **branded A4 certificate** with your logo, the drive's serial and a QR code.

</td>
</tr>
<tr>
<td valign="top">

### Deploy software (preview)
Drop an installer to build a package with silent switches and detection. Deploy to every PC, one client or one PC, and run maintenance with a **check step before anything changes**.

</td>
<td valign="top">

### Mossbit, on your terms
<img src="docs/images/mossbit.gif" align="right" width="90">

The pixel helper sweeps progress bars and reacts to results. Choose **Full, Subtle (default) or Off** in Settings, and press **Ctrl+Shift+P** for Presentation mode when you're sharing your screen with a client.

</td>
</tr>
</table>

<div align="center">
<img src="docs/images/sweep.gif" alt="Mossbit sweeping the progress bar" width="80%">
<br><sub>Mossbit sweeps the progress bar clean while an erase runs.</sub>
</div>

---

## 🆕 What's new in 1.4.0

* **New look:** a menu grouped by job, a client bar on every job, Ctrl+K search, and an indigo theme. Mossbit has a Subtle mode (the default) and a Presentation mode for screen sharing.
* **Deploy (preview):** drop an installer to build a package, deploy it to every PC, a client or one PC, and run maintenance with a check step before anything changes.
* **Migrate user, now with apps:** start from an old disk or NVMe in a USB caddy (even with no drive letter), copy to a drive in one click, and reinstall the user's apps on the new PC.
* **Erase and certify:** uses the drive's own Secure Erase or NVMe Sanitize when it can (NIST 800-88 Purge), and produces a branded A4 certificate with a QR code.
* **Cancel that really stops:** every long job stops promptly and tells you what state it left things in.

## 📸 Tour

<table>
<tr>
<td><img src="docs/images/home_light.png" alt="Home, light"><p align="center"><sub><b>Home</b>: today's jobs, linked machines, start a job</sub></p></td>
<td><img src="docs/images/home_dark.png" alt="Home, dark"><p align="center"><sub><b>Home</b> · dark</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/drives_light.png" alt="Drives"><p align="center"><sub><b>Drives</b>: pick a drive, then the job</sub></p></td>
<td><img src="docs/images/palette_dark.png" alt="Command palette"><p align="center"><sub><b>Ctrl+K</b> finds any job, drive or machine</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/recover_results.png" alt="Deleted files found"><p align="center"><sub><b>Recover files</b> with original names and folders</sub></p></td>
<td><img src="docs/images/wipe_confirm.png" alt="Erase confirmation"><p align="center"><sub><b>Erase and certify</b>: typed confirmation</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/progress_dark.png" alt="Erase in progress"><p align="center"><sub><b>Live progress</b>: speed, time left, processed</sub></p></td>
<td><img src="docs/images/health_result.png" alt="Health check"><p align="center"><sub><b>Drive health</b> in plain English</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/connect.png" alt="Connect a machine"><p align="center"><sub><b>Connect a machine</b> with one command</sub></p></td>
<td><img src="docs/images/migrate_what.png" alt="Migrate user"><p align="center"><sub><b>Migrate user</b>: pick what comes across</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/machines_dark.png" alt="Machines"><p align="center"><sub><b>Machines</b>: linked PCs, migration and network clone</sub></p></td>
<td><img src="docs/images/jobs_light.png" alt="Jobs"><p align="center"><sub><b>Jobs</b>: every job with client, technician and result</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/manage_light.png" alt="Manage"><p align="center"><sub><b>Manage</b>: software library and deployments</sub></p></td>
<td><img src="docs/images/settings_light.png" alt="Settings"><p align="center"><sub><b>Settings</b>: theme, Mossbit, presentation mode, branding</sub></p></td>
</tr>
<tr>
<td><img src="docs/images/partitions_found.png" alt="Lost partitions found"><p align="center"><sub><b>Lost partitions</b> found and ready to restore</sub></p></td>
<td><img src="docs/images/shred_drop.png" alt="Shred files"><p align="center"><sub><b>Shred files</b>: drop them in</sub></p></td>
</tr>
</table>

---

## 📦 Download

**Easiest:** open the repo's **[Releases](../../releases)** page and download **`SectorSmith.exe`**. It's a single portable file, so copy it to your toolkit USB stick and run it on any Windows 10/11 PC. It asks for administrator rights when it starts.

**Every commit** is also built automatically. Go to **Actions → Build & test → latest run → Artifacts → `SectorSmith-windows`**.

> [!NOTE]
> Windows SmartScreen may warn about an unsigned app the first time. Click **More info → Run anyway**.

---

## 🚀 Quick start

1. **Run `SectorSmith.exe`** and accept the administrator prompt. Without admin rights, Windows hides physical drives (disk images still work).
2. **Follow the 3-step welcome tour.** It covers what the app does and how it keeps you safe.
3. **Pick a task on Home** and follow the steps. If you're unsure, press **? How this works** in the top-right corner of any task.

<div align="center">
<img src="docs/images/welcome.png" alt="Welcome tour" width="80%">
</div>

---

## 📚 How-to guides

<details open>
<summary><b>♻️ Recover deleted files</b></summary>

<br>

Bring back files that were deleted, emptied from the Recycle Bin, or lost when a drive was formatted.

| | Step | What to do |
|:-:|---|---|
| 1 | **Stop using the drive** | Every new file written can overwrite what you want back. Run SectorSmith from a USB stick, not from that drive. |
| 2 | **Pick the partition** | Choose the drive letter the files were on (for example **D:**). If a drive shows as RAW or empty, pick the **whole disk**. |
| 3 | **Quick or Deep** | **Quick scan** reads the NTFS file table, so you get original names, folders and dates, usually in seconds. **Deep scan** finds photos, documents, video and archives by their content on *any* drive, but file names aren't kept. |
| 4 | **Select and recover** | Filter by type or search by name, select the rows you want, press **Recover**, and **save them to a different drive**. |

<img src="docs/images/recover_pick.png" width="49%"> <img src="docs/images/recover_type.png" width="49%">

> [!TIP]
> **Chance: Excellent** means the file's data is still assigned to it. **Good** means it was deleted but not yet overwritten. Try Quick first, then Deep if nothing useful turns up.

**Deep scan finds:** JPG · PNG · GIF · BMP · WebP · HEIC · MP4 · MOV · M4A · 3GP · AVI · WAV · MP3 · PDF · DOCX · XLSX · PPTX · ODT · ZIP · JAR · APK · DOC · XLS · PPT · MSG · 7Z · SQLite. Each file's exact length is worked out from its internal structure, so carved files open properly.

</details>

<details>
<summary><b>🧩 Find lost partitions</b></summary>

<br>

Use this when a drive suddenly shows as unallocated, RAW or "needs formatting", or a partition was deleted by mistake.

1. **Pick the disk** (the whole disk, not a partition).
2. **Quick search** checks the standard partition positions. Lost partitions show a red **Missing** tag.
3. **Deep search** reads every sector. It also uses NTFS backup boot sectors and ext backup superblocks.
4. **Bring them back.** Tick the ones you want, type `RESTORE`, and the GPT or MBR entries are rewritten. A backup of the table area is saved first.

<img src="docs/images/partitions_found.png" width="70%">

> [!TIP]
> Afterwards, Windows may need a rescan: **Disk Management → Action → Rescan Disks**. To undo, restore the saved backup from **Drives → Expert tools → Overview**.

</details>

<details>
<summary><b>🧽 Erase and certify a drive</b></summary>

<br>

Securely erase a whole disk or a single partition before it's reused, returned or disposed of.

1. **Pick the target.** The disk Windows runs from can't be selected.
2. **Use the drive's own erase.** For a whole SATA or NVMe drive, SectorSmith asks the drive what it supports and offers ATA Secure Erase, NVMe Sanitize (crypto or block erase) or NVMe Format. That counts as NIST 800-88 **Purge** and reaches spare cells an overwrite can't. A frozen drive is reported (sleep the PC or replug the drive, then try again). If the drive can't do it, the overwrite below runs and the certificate says so.
3. **Choose how thorough** (the overwrite, NIST 800-88 **Clear**):

   | Option | Method | When |
   |---|---|---|
   | **Quick** | 1 pass of zeros | Reusing the drive inside the business |
   | **Recommended** | NIST 800-88 Clear + read-back verify | Most situations |
   | **Thorough** | DoD 5220.22-M 3-pass + verify | Policy requires multi-pass |
   | *More…* | HMG IS5, DoD 7-pass, Schneier, Gutmann 35, custom pattern | Special requirements |

4. **Type to confirm**, e.g. `ERASE DISK 2 A1B2C3` (the last 6 characters of the serial) or `ERASE PARTITION 1`.
5. **Save the certificate.** A sober A4 page (print it to PDF) with your company name and logo (Settings, Branding), client, technician, machine, drive model, serial and capacity, method, hardware or software erase, NIST category, verification result, times, a certificate ID and a QR code holding the ID and a SHA-256 of the record.

<img src="docs/images/wipe_strength.png" width="49%"> <img src="docs/images/wipe_confirm.png" width="49%">

> [!WARNING]
> SSDs and USB flash keep spare cells the computer can't reach. Use the drive's own erase (step 2) on a whole drive connected by SATA or NVMe. USB enclosures and RAID controllers usually block it: connect the drive directly, or destroy it physically.

</details>

<details>
<summary><b>✂️ Shred files & clean free space</b></summary>

<br>

* **Files & folders:** drag them onto the window (or use **Add files / Add folder**), choose a strength, then type `SHRED`. Each file is overwritten, renamed, truncated and then deleted.
* **Free space:** overwrites a drive's empty area, which destroys traces of files deleted earlier. Your existing files stay.

<img src="docs/images/shred_drop.png" width="70%">

</details>

<details>
<summary><b>💓 Check drive health</b></summary>

<br>

Reads every sector (read-only) and shows a live map: green is fast, yellow and orange are slow, red is unreadable. You then get a plain-English verdict, such as *healthy*, *slow in places* or *bad sectors found*, with the next step to take.

<img src="docs/images/health_result.png" width="70%">

> [!TIP]
> Bad sectors? Back the drive up straight away with **Back up / clone**. **Try repairing** rewrites unreadable sectors so the drive swaps them for spares.

</details>

<details>
<summary><b>🪞 Back up & clone</b></summary>

<br>

Make an exact sector-by-sector copy of a drive or a partition.

* **`.vhd` image:** Windows can mount it straight from Disk Management.
* **`.img` raw image:** works with any recovery tool.
* **Another drive:** a direct clone. Everything on the target is replaced, and you confirm by typing `CLONE TO DISK n`.

Copying works like ddrescue. A fast pass skips problem areas, then they're retried one sector at a time. A **SHA-256** hash and a log are written next to the image.

<img src="docs/images/clone_dest.png" width="70%">

</details>

<details>
<summary><b>🔗 Connect a machine</b></summary>

<br>

Link another PC on the same network (same site or over a VPN) so you can work on both from one window. This is ideal when you're remoted into a new machine.

1. Open **Machines → Connect a machine** (or **Connect a machine** on Home). SectorSmith starts a secure listener and shows a one-line PowerShell command.
2. On the other PC, either remote in or use your RMM's PowerShell shell (Datto RMM works). Open PowerShell **as administrator**, paste the command and press **Enter**.
3. The command downloads SectorSmith **from your PC**, checks its **SHA-256**, and links up. The other PC then appears on the **Machines** page and under **Working on** in the side panel.

<img src="docs/images/connect.png" width="70%">

> [!NOTE]
> **Security:** the link uses TLS 1.2+ with a one-time certificate. That certificate's fingerprint, plus a random 128-bit token, is pinned inside the command, so no other PC can stand in for yours. The link ends when you close SectorSmith or press **Disconnect**. Windows may ask to allow SectorSmith through the firewall; choose **Allow** (private networks).

</details>

<details>
<summary><b>🧳 Move a user to a new PC</b></summary>

<br>

Copy a user's folders and app data from their old PC to their new one, with no USB drives and no reboots.

| | Step | What to do |
|:-:|---|---|
| 1 | **Link the new PC** | Use *Connect a machine*. Run SectorSmith on either PC; both directions work. |
| 2 | **Sign the user in once on the new PC** | This creates their profile folder, so files land with the right permissions. |
| 3 | **From → To** | Pick the old PC and user, then the new PC and user (or type a folder path). |
| 4 | **Choose what comes across** | Desktop, Documents, Downloads, Pictures, Videos, Music, Favorites, plus **Chrome, Edge, Firefox, Outlook signatures, Sticky Notes, Office templates and dictionaries**. Sizes are shown before you start. Caches and temp files are skipped. |
| 5 | **Run it, then run it again** | Later runs copy **only new or changed files**, so you can pre-stage the day before and do a quick final sync at handover. |

<img src="docs/images/migrate_what.png" width="49%"> <img src="docs/images/migrate_done.png" width="49%">

> [!TIP]
> Close the user's apps on the old PC (or sign them out) for a clean copy. Anything that was open is listed in the run report so you can re-run. Chrome/Edge **saved passwords** are locked to the old PC by Windows, so export them first or rely on browser sync. OneDrive folders are **off by default** because they re-sync on their own.

</details>

<details>
<summary><b>🖧 Clone a disk to another PC</b></summary>

<br>

Copy a whole disk, sector by sector, from one PC to another over the network. Empty space isn't sent, data is compressed, and every 4 MiB block is **SHA-256-verified on both ends**.

* **Data disks:** both PCs can keep running. Link them with the command and pick the disks.
* **The PC's own Windows disk:** a running Windows can't overwrite itself. Boot the destination PC from a **SectorSmith USB stick**. It shows an address, a pairing code and a check code. In SectorSmith, go to **Connect a machine → PC booted from USB** and type them in.

<img src="docs/images/netclone_dest.png" width="49%"> <img src="docs/images/netclone_running.png" width="49%">

**Make the USB stick** (one-time). First install the free [Windows ADK + WinPE add-on](https://learn.microsoft.com/windows-hardware/get-started/adk-install), then run:

```powershell
.\tools\make_usb.ps1 -UsbDrive E:     # erases E:, adds SectorSmith in "waiting" mode
```

> [!WARNING]
> Windows cloned onto **different hardware** may need driver updates on first boot. For a user getting a new PC, **Move a user** is usually the better choice.

</details>

<details>
<summary><b>🛠 Expert tools</b></summary>

<br>

The classic tabbed interface opens in its own window, with every option exposed:

* **Sector editor:** a hex/ASCII view of any sector, a data inspector (int8–64, LE/BE), structure recognition (MBR, GPT, boot sectors, MFT records), whole-disk search, and byte editing with typed confirmation.
* **Partition-table backup and restore**, plus every task with all its options.

Run `SectorSmith.exe --classic` to open straight into this view.

Open it from **Drives → Expert tools**, or with Ctrl+K.

</details>

These guides are built in too: **Settings → How to use**, or the **?** in the top bar.

<img src="docs/images/guide_dark.png" width="70%">

---

## 🛡 Safety rails

| | |
|---|---|
| 🔒 **System disk protected** | The disk Windows is running from can't be wiped, cloned over, restored over or edited. To wipe it, boot from USB. |
| ⌨️ **Typed confirmations** | Anything permanent asks you to type a phrase like `ERASE DISK 2 A1B2C3`, so a stray click can't destroy data. |
| 💾 **Automatic backups** | The partition-table area (first and last 1 MiB of the disk) is saved before any table change. |
| 🔐 **Safe raw writes** | Volumes on the target disk are locked and dismounted before raw writes, then released. |
| 📝 **Logs** | Every operation is logged to `%LOCALAPPDATA%\SectorSmith\sectorsmith.log`. |

---

## ❓ FAQ

<details><summary><b>Why can't I see my drives?</b></summary>
SectorSmith needs administrator rights to open physical disks. Accept the UAC prompt, or right-click → <i>Run as administrator</i>. The top bar shows <b>Admin</b> when it's elevated.
</details>

<details><summary><b>Quick scan is greyed out</b></summary>
Quick scan reads the NTFS file table, so it needs an NTFS partition. Pick one, or use Deep scan, which works on any filesystem (and on RAW drives).
</details>

<details><summary><b>How many wipe passes do I need?</b></summary>
For modern hard drives, one verified pass (NIST 800-88 Clear) is the accepted standard. Extra passes add time, not security. For SSDs and NVMe drives, use the drive's own erase (see the Erase guide).
</details>

<details><summary><b>Can I recover files from a drive that's failing?</b></summary>
Yes. Image it first with <b>Back up / clone → .img</b>, which handles bad sectors. Then drop the image onto SectorSmith and recover from the image, so the failing drive is only read once.
</details>

<details><summary><b>Where are settings, logs and backups?</b></summary>
<code>%LOCALAPPDATA%\SectorSmith</code>
</details>

---

## 💬 Support & issues

Found a problem or have an idea? Open an **[issue](../../issues/new/choose)**. The bug template asks for the version, the task and the log, which makes fixes quick. Security reports go through [SECURITY.md](SECURITY.md).

---

## 🧑‍💻 Build from source

```powershell
# Python 3.10+ from python.org (tick "Add to PATH")
git clone https://github.com/WastedPatriot/sectorsmith.git
cd sectorsmith
.\build.bat            # -> dist\SectorSmith.exe
# or run without building:
.\run.bat
```

<details><summary><b>Project layout</b></summary>

```
sectorsmith/
├─ device.py       raw disk I/O (Win32 + Linux + image files), volume lock/dismount
├─ partitions.py   MBR/GPT parsing, filesystem detection, safe table writes + backups
├─ partscan.py     lost-partition search (NTFS/FAT/exFAT/ext, backup boot sectors)
├─ ntfs.py         MFT parser for undelete with names, folders and dates
├─ carver.py       signature carving for 25+ file types with structural length detection
├─ wipe.py         drive's own erase (ATA Secure Erase, NVMe Sanitize), wipe methods, verification, shredding
├─ surface.py      surface scan, ddrescue-style imaging/cloning, fixed VHD writer
├─ report.py       branded wipe certificates (A4 HTML)
├─ qr.py           small QR encoder for the certificate
├─ link/           SectorSmith Link: TLS pairing, agent, user migration, network clone
├─ app.py          classic expert UI (Advanced tools)
└─ ui/             new UI: wizards, Mossbit, sweep bar, guides, themes
tools/
├─ make_sprites.py generates the pixel-art sprite sheets + icon
├─ make_docs.py    regenerates every image in this README
└─ make_usb.ps1    builds the bootable SectorSmith USB stick (WinPE)
tests/             engine tests, real-Windows integration test, headless UI tests
```
</details>

<details><summary><b>Tests & CI</b></summary>

Every push runs [GitHub Actions](.github/workflows/build.yml):

* **Engine tests (Linux):** builds a real GPT disk image with FAT32 + NTFS + ext4 partitions, then checks partition search and restore (validated with `sgdisk`/`sfdisk`), byte-exact NTFS undelete, carving, wipe verification, VHD output (validated with `qemu-img`), and simulated bad sectors.
* **Hardware erase tests:** fake SATA and NVMe drives check IDENTIFY parsing, method choice, frozen drives, the system-disk refusal, fallback to overwrite and verification by sampling; the certificate's QR code is decoded back.
* **Link tests:** a controller and real agent processes over TLS. They check certificate pinning, token rejection, migrating a profile byte-for-byte (including delta re-runs), clones in both directions, and USB/listen-mode pairing.
* **Lint & security:** `ruff` and `bandit` on every push. CodeQL also runs once the repo is public, and Dependabot watches dependencies.
* **Windows integration:** creates and attaches a real virtual disk with `diskpart`, then exercises raw I/O, volume locking, partition restore that Windows re-mounts, NTFS undelete on a live volume, and wipes.
* **Build:** produces `SectorSmith.exe`. Tagging `v*` publishes a Release.

</details>

---

## 🗺 Roadmap

- [x] **Remote link:** pair machines with a copy-paste command, move users, and clone disks between them
- [ ] Map network drives & printers for migrated users
- [x] Volume Shadow Copy for copying files that are open on the old PC
- [x] Deploy: packages, deployments and maintenance runs on linked PCs (preview)
- [ ] Package catalogue from winget and Chocolatey, onboarding and schedules
- [ ] ScreenConnect connect button, Microsoft 365 and Active Directory
- [ ] Server migration (shares, permissions, printers, tasks)
- [ ] FAT/exFAT undelete with names
- [ ] BitLocker unlock, ext4 write, LVM, RAID reconstruction
- [x] ATA Secure Erase / NVMe Sanitize passthrough

---

<div align="center">
<img src="docs/images/mossbit_moods.png" width="70%">
<br><sub>SectorSmith © 2026 WastedPatriot · <a href="LICENSE">All rights reserved</a> · Built with Python, CustomTkinter and a lot of tiny pixels.</sub>
</div>
