"""Small QR code encoder (byte mode, error correction level M, versions 1 to 10) with SVG output.

Used for the verification code on wipe certificates, so the portable exe needs no extra dependency.
Follows ISO/IEC 18004: Reed-Solomon over GF(256), interleaved blocks, function patterns, eight masks.
"""
from __future__ import annotations

# level M per version: (EC codewords per block, number of blocks)
_EC_M = {1: (10, 1), 2: (16, 1), 3: (26, 1), 4: (18, 2), 5: (24, 2), 6: (16, 4), 7: (18, 4), 8: (22, 4),
         9: (22, 5), 10: (26, 5)}
_FORMAT_M = 0  # level M is 00 in the format information
MAX_VERSION = 10

_MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data, divisor) -> list[int]:
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(divisor):
            res[i] ^= _gf_mul(coef, factor)
    return res


def raw_modules(ver: int) -> int:
    """Modules left for data and EC after the function patterns."""
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        n -= (25 * na - 10) * na - 55
        if ver >= 7:
            n -= 36
    return n


def data_codewords(ver: int) -> int:
    ec, blocks = _EC_M[ver]
    return raw_modules(ver) // 8 - ec * blocks


def alignment_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    na = ver // 7 + 2
    step = (ver * 8 + na * 3 + 5) // (na * 4 - 4) * 2
    size = ver * 4 + 17
    return [6] + sorted(size - 7 - i * step for i in range(na - 1))


def _encode_data(data: bytes, ver: int) -> list[int]:
    bits: list[int] = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))
    put(0b0100, 4)
    put(len(data), 8 if ver < 10 else 16)
    for b in data:
        put(b, 8)
    cap = data_codewords(ver) * 8
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < cap:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    return [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]


def _interleave(data: list[int], ver: int) -> list[int]:
    ec, nblocks = _EC_M[ver]
    raw = raw_modules(ver) // 8
    nshort = nblocks - raw % nblocks
    short_len = raw // nblocks
    div = _rs_divisor(ec)
    blocks, k = [], 0
    for i in range(nblocks):
        d = data[k:k + short_len - ec + (0 if i < nshort else 1)]
        k += len(d)
        e = _rs_remainder(d, div)
        if i < nshort:
            d = d + [0]  # placeholder so all blocks line up; skipped below
        blocks.append(d + e)
    out = []
    for i in range(len(blocks[0])):
        for j, b in enumerate(blocks):
            if i != short_len - ec or j >= nshort:
                out.append(b[i])
    return out


class _Grid:
    def __init__(self, ver: int):
        self.ver = ver
        self.size = ver * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, on):
        self.dark[y][x] = bool(on)
        self.func[y][x] = True

    def draw_functions(self):
        n = self.size
        for i in range(n):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (n - 4, 3), (3, n - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < n and 0 <= y < n:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = alignment_positions(self.ver)
        last = len(pos) - 1
        for i, ax in enumerate(pos):
            for j, ay in enumerate(pos):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                b = (bits >> i) & 1
                a, c = n - 11 + i % 3, i // 3
                self.set(a, c, b)
                self.set(c, a, b)

    def draw_format(self, mask):
        data = _FORMAT_M << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        n = self.size

        def bit(i):
            return (bits >> i) & 1
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(n - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, n - 15 + i, bit(i))
        self.set(8, n - 8, True)

    def draw_codewords(self, cws):
        n = self.size
        i = 0
        total = len(cws) * 8
        right = n - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(n):
                for j in range(2):
                    x = right - j
                    up = ((right + 1) & 2) == 0
                    y = n - 1 - vert if up else vert
                    if not self.func[y][x] and i < total:
                        self.dark[y][x] = bool((cws[i >> 3] >> (7 - (i & 7))) & 1)
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        fn = _MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.func[y][x] and fn(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self) -> int:
        n = self.size
        score = 0
        lines = [row for row in self.dark] + [[self.dark[y][x] for y in range(n)] for x in range(n)]
        finder_a = [True, False, True, True, True, False, True, False, False, False, False]
        finder_b = finder_a[::-1]
        for line in lines:
            run = 1
            for i in range(1, n + 1):
                if i < n and line[i] == line[i - 1]:
                    run += 1
                    continue
                if run >= 5:
                    score += run - 2
                run = 1
            for i in range(n - 10):
                seg = line[i:i + 11]
                if seg == finder_a or seg == finder_b:
                    score += 40
        for y in range(n - 1):
            for x in range(n - 1):
                c = self.dark[y][x]
                if c == self.dark[y][x + 1] == self.dark[y + 1][x] == self.dark[y + 1][x + 1]:
                    score += 3
        dark = sum(map(sum, self.dark))
        total = n * n
        score += max(0, (abs(dark * 20 - total * 10) + total - 1) // total - 1) * 10  # dark/light balance
        return score


def qr_matrix(text: str | bytes, mask: int | None = None) -> list[list[int]]:
    """QR code for ``text`` as rows of 0/1 (1 is dark), without the quiet zone."""
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    for ver in range(1, MAX_VERSION + 1):
        if 4 + (8 if ver < 10 else 16) + len(data) * 8 <= data_codewords(ver) * 8:
            break
    else:
        raise ValueError(f"QR payload too long ({len(data)} bytes)")
    g = _Grid(ver)
    g.draw_functions()
    g.draw_codewords(_interleave(_encode_data(data, ver), ver))
    if mask is None:
        best = None
        for m in range(8):
            g.apply_mask(m)
            g.draw_format(m)
            p = g.penalty()
            if best is None or p < best[0]:
                best = (p, m)
            g.apply_mask(m)  # undo
        mask = best[1]
    g.apply_mask(mask)
    g.draw_format(mask)
    return [[1 if v else 0 for v in row] for row in g.dark]


def qr_svg(text: str | bytes, module: int = 4, quiet: int = 4, label: str = "QR code") -> str:
    """Standalone SVG: black modules on white, one path, ``quiet`` modules of margin."""
    m = qr_matrix(text)
    n = len(m)
    dim = (n + 2 * quiet) * module
    parts = []
    for y, row in enumerate(m):
        x = 0
        while x < n:
            if row[x]:
                start = x
                while x < n and row[x]:
                    x += 1
                parts.append(f"M{(start + quiet) * module} {(y + quiet) * module}h{(x - start) * module}v{module}"
                             f"h-{(x - start) * module}z")
            else:
                x += 1
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{dim}" height="{dim}" viewBox="0 0 {dim} {dim}" '
            f'shape-rendering="crispEdges" role="img" aria-label="{label}" data-modules="{n}" '
            f'data-quiet="{quiet}" data-module-size="{module}"><rect width="{dim}" height="{dim}" fill="#fff"/>'
            f'<path fill="#000" d="{"".join(parts)}"/></svg>')
