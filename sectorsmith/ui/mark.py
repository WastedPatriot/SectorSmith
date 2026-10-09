"""Geometry of the SectorSmith product mark: a disk with one sector struck out, resting on an anvil, on an indigo
rounded square. Shared by the in-app mark (shell.ProductMark) and tools/make_icon.py, so both stay identical.
Coordinates are fractions of the mark's size. No Mossbit here: the mark is the product, not the mascot."""
from __future__ import annotations

INDIGO = "#5747E0"
INDIGO_DARK = "#3F31C2"   # bottom of the tile's gradient in the icon files
CORNER = .24              # tile corner radius

# Anvil: horn on the left, flat face, waist and foot
ANVIL = [(.15, .615), (.79, .615), (.79, .695), (.68, .715), (.62, .775), (.70, .835), (.70, .875), (.30, .875),
         (.30, .835), (.38, .775), (.34, .715), (.24, .69)]

# Disk above the anvil: ring, the struck sector (12 to 3 o'clock) and the spindle hole
DISK = {"cx": .5, "cy": .355, "r": .205, "ring": .062, "hole": .052, "sector": (90, -90)}

# Small sizes (16 and 24 px) drop the anvil and show a bigger disk, which stays readable
DISK_SMALL = {"cx": .5, "cy": .5, "r": .27, "ring": .085, "hole": .06, "sector": (90, -90)}


def simple(size: int) -> bool:
    return size < 30
