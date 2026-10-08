# SectorSmith — notes for Claude Code

Portable Windows disk toolkit (Python 3.11+, CustomTkinter). Owner: WastedPatriot. MIT.
User prefers short, ADHD-friendly answers: next action first, numbered steps, ≤5 items.

## Layout
- `sectorsmith/` engine: device.py (raw I/O), partitions.py (MBR/GPT, fix_gpt_after_grow), wipe.py, surface.py
  (scan + imaging, smart/used-space copy), usedmap.py (NTFS/FAT/exFAT/ext used-space maps), ntfs.py, carve.py,
  vss.py (Windows shadow copies), report.py
- `sectorsmith/link/` SectorSmith Link — TLS + pinned cert + token, PowerShell one-liner connector, listen mode
  (USB/WinPE). endpoint.py = ops a linked PC exposes. migrate.py = move a user to a new PC.
  netclone.py = one disk → one or many disks across PCs.
- `sectorsmith/deploy/` SectorSmith Deploy (our own software-deployment module, desired-state):
  analyze.py (MSI/EXE → silent switches, detection), core.py (Store, Package/Task/Client/Deployment,
  detect → execute → re-check sessions, export of Install/Uninstall/Detect.ps1)
- `sectorsmith/ui/` new UI (main.py, screens.py, link_screens.py, guide.py, mascot.py = Mossbit sprite)
- `tools/` make_sprites.py, make_docs.py (README screenshots/GIFs), make_usb.ps1 (WinPE stick)
- `tests/` test_core.py, test_link.py (needs `python tests/build_test_disk.py <dir>` first), test_windows.py
  (CI only), ui_smoke.py / ui_link_smoke.py (xvfb)

## Rules
- Commit author: Claude <noreply@anthropic.com>.
- Lint: `ruff`, `bandit -r sectorsmith`, `pyflakes`. Keep tests green before committing.
- Destructive ops always need typed confirmation in the UI.

## Status / next up
1. Push this branch and confirm GitHub Actions is green (Windows job tests VSS + diskpart).
2. Deploy engine (deploy/core.py) is written but NOT tested yet → write tests/test_deploy.py
   (shell-language packages, SECTORSMITH_FAKE_SOFTWARE fixture, run against a real agent over Link).
3. Deploy UI: screen with tabs Library | Deployments | Clients | Tasks | Sessions, drop-an-installer package
   builder, run maintenance on linked PCs, Home card + guide topic.
4. tests/ui_link_smoke.py clone section hangs after the NetCloneWizard rewrite (multi-select) — fix it.
5. README sections for smart clone, one → many, Deploy, migration "New folder"; bump to 1.4.0.
6. Set up a scheduled repo-care agent (triage issues/PRs, check CI).
