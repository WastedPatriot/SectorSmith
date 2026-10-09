# SectorSmith developer notes

Portable Windows toolkit for MSP technicians (Python 3.11+, CustomTkinter). Proprietary from 1.4.0, see LICENSE.

## Layout
- `sectorsmith/` engine: device.py (raw I/O, ATA/NVMe pass-through), partitions.py (MBR/GPT, fix_gpt_after_grow),
  wipe.py (overwrite plus the drive's own erase: capability check, ATA Secure Erase, NVMe Sanitize/Format), surface.py
  (scan and imaging, smart/used-space copy), usedmap.py (NTFS/FAT/exFAT/ext used-space maps), ntfs.py, carver.py,
  vss.py (Windows shadow copies), report.py (branded wipe certificate), qr.py (QR encoder for it)
- `sectorsmith/link/` SectorSmith Link: TLS with a pinned cert and token, PowerShell one-liner connector, listen mode
  (USB/WinPE). endpoint.py holds the ops a linked PC exposes. migrate.py moves a user to a new PC.
  netclone.py clones one disk to one or many disks across PCs.
- `sectorsmith/integrations/` optional integrations, off until set up (see docs/INTEGRATIONS.md): screenconnect.py,
  ad.py (domain user lookup via ADSI), dpapi.py (secrets via Windows DPAPI). UI glue in ui/integrations_ui.py
- `sectorsmith/deploy/` SectorSmith Deploy (desired-state software deployment):
  analyze.py (MSI/EXE to silent switches and detection), core.py (Store, Package/Task/Client/Deployment,
  detect, execute, re-check sessions, export of Install/Uninstall/Detect.ps1)
- `sectorsmith/ui/` UI (main.py, screens.py, link_screens.py, deploy_screens.py, guide.py, mascot.py for Mossbit).
  theme.py holds the design tokens; nav.py is the route table behind the rail, sub-nav, breadcrumbs and
  Ctrl+K palette (palette.py); shell.py draws the rail, sub-nav and context bar; workspace.py has the Drives,
  Machines and Jobs pages; settings.py has Settings (Personality Full/Subtle/Off, presentation mode)
- `tools/` make_sprites.py, make_docs.py (README screenshots and GIFs), make_usb.ps1 (WinPE stick)
- `tests/` test_core.py, test_link.py and test_deploy.py (run `python tests/build_test_disk.py <dir>` first),
  test_sanitize.py (hardware erase on fake drives, certificate and QR; no fixture needed, qr_decode.py reads codes back),
  test_integrations.py (ScreenConnect URLs, AD lookup with a fake PowerShell, LDAP escaping, DPAPI; no fixture),
  test_windows.py (CI only), ui_smoke.py, ui_link_smoke.py and ui_deploy_smoke.py (need a display, use xvfb on Linux)

## Rules
- Lint with `ruff`, `bandit -r sectorsmith` and `pyflakes`. Keep tests green before committing.
- Destructive operations always need a typed confirmation in the UI.

## Next up
1. Cancel must stop every long job promptly and report the state it left things in.
2. User migration: to a local drive or NVMe, from an old disk attached by USB, and bringing apps across via Deploy.
3. SSD/NVMe hardware sanitize and the branded wipe certificate.
4. UI refresh: job-grouped navigation, client and ticket bar, Jobs history, indigo accent, Mossbit personality setting.
5. README and screenshots for 1.4.0, version bump, tagged release.
