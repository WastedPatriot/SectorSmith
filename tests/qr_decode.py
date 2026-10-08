"""Independent QR reader for tests: reads a module matrix (or the SVG made by sectorsmith.qr) back to bytes.

Written from the ISO/IEC 18004 layout rather than from the encoder, so a round trip proves the code is readable:
it checks the format information BCH code, removes the mask, walks the data zigzag, de-interleaves the blocks,
requires every Reed-Solomon syndrome to be zero and parses the byte-mode segment.
"""
import re

# level -> version -> (EC codewords per block, [data codewords per block...]), versions 1 to 10
EC_TABLE = {
    "M": {1: (10, [16]), 2: (16, [28]), 3: (26, [44]), 4: (18, [32, 32]), 5: (24, [43, 43]),
          6: (16, [27] * 4), 7: (18, [31] * 4), 8: (22, [38, 38, 39, 39]), 9: (22, [36] * 3 + [37] * 2),
          10: (26, [43] * 4 + [44])},
}
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42],
         9: [6, 26, 46], 10: [6, 28, 50]}
LEVELS = {1: "L", 0: "M", 3: "Q", 2: "H"}
MASKS = [lambda r, c: (r + c) % 2 == 0, lambda r, c: r % 2 == 0, lambda r, c: c % 3 == 0,
         lambda r, c: (r + c) % 3 == 0, lambda r, c: (r // 2 + c // 3) % 2 == 0,
         lambda r, c: (r * c) % 2 + (r * c) % 3 == 0, lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
         lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0]

EXP, LOG = [0] * 512, [0] * 256
_v = 1
for _i in range(255):
    EXP[_i], LOG[_v] = _v, _i
    _v = (_v << 1) ^ (0x11D if _v & 0x80 else 0)
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


class QRError(Exception):
    pass


def _bch_ok(word15):
    """Format info: 5 data bits + 10 BCH bits, valid when the remainder against 0x537 is zero."""
    v = word15
    for i in range(14, 9, -1):
        if v & (1 << i):
            v ^= 0x537 << (i - 10)
    return v == 0


def _is_function(r, c, n, ver):
    if (r < 9 and c < 9) or (r < 9 and c >= n - 8) or (r >= n - 8 and c < 9):
        return True  # finders, separators and format areas
    if r == 6 or c == 6:
        return True
    pos = ALIGN[ver]
    for ar in pos:
        for ac in pos:
            if (ar, ac) in ((6, 6), (6, pos[-1]), (pos[-1], 6)):
                continue
            if abs(r - ar) <= 2 and abs(c - ac) <= 2:
                return True
    return ver >= 7 and ((r < 6 and c >= n - 11) or (c < 6 and r >= n - 11))  # version information


def _syndromes_zero(block, ec):
    for i in range(ec):
        s = 0
        for b in block:
            s = (EXP[LOG[s] + i] if s else 0) ^ b
        if s:
            return False
    return True


def decode_matrix(m):
    n = len(m)
    if any(len(row) != n for row in m) or (n - 17) % 4:
        raise QRError("not a square QR matrix")
    ver = (n - 17) // 4
    if ver not in ALIGN:
        raise QRError(f"version {ver} not handled")
    # first format copy: row 8 columns 0-5,7,8 then column 8 rows 7,5..0 (MSB first)
    coords = [(8, 0), (8, 1), (8, 2), (8, 3), (8, 4), (8, 5), (8, 7), (8, 8), (7, 8), (5, 8), (4, 8), (3, 8),
              (2, 8), (1, 8), (0, 8)]
    word = 0
    for r, c in coords:
        word = word << 1 | m[r][c]
    # second copy: column 8 bottom up, then row 8 right part
    coords2 = [(n - 1 - i, 8) for i in range(7)] + [(8, n - 8 + i) for i in range(8)]
    word2 = 0
    for r, c in coords2:
        word2 = word2 << 1 | m[r][c]
    if word != word2:
        raise QRError("format copies disagree")
    if not _bch_ok(word ^ 0x5412):
        raise QRError("format information fails its BCH check")
    fmt = (word ^ 0x5412) >> 10
    level, mask = LEVELS[fmt >> 3], fmt & 7
    if m[n - 8][8] != 1:
        raise QRError("dark module missing")
    for i in range(n):  # timing patterns
        if m[6][i] != (i % 2 == 0) and 8 <= i < n - 8:
            raise QRError("bad timing pattern")
    bits = []
    col = n - 1
    upward = True
    while col > 0:
        if col == 6:
            col -= 1
        rows = range(n - 1, -1, -1) if upward else range(n)
        for r in rows:
            for c in (col, col - 1):
                if not _is_function(r, c, n, ver):
                    bits.append(m[r][c] ^ (1 if MASKS[mask](r, c) else 0))
        upward = not upward
        col -= 2
    ec, dlens = EC_TABLE[level][ver]
    total = sum(dlens) + ec * len(dlens)
    cws = [int("".join(map(str, bits[i * 8:i * 8 + 8])), 2) for i in range(total)]
    blocks = [[] for _ in dlens]
    k = 0
    for i in range(max(dlens)):
        for b, dl in enumerate(dlens):
            if i < dl:
                blocks[b].append(cws[k])
                k += 1
    for i in range(ec):
        for b in range(len(dlens)):
            blocks[b].append(cws[k])
            k += 1
    data = []
    for b, dl in zip(blocks, dlens):
        if not _syndromes_zero(b, ec):
            raise QRError("Reed-Solomon check failed")
        data += b[:dl]
    stream = "".join(f"{x:08b}" for x in data)
    if stream[:4] != "0100":
        raise QRError(f"unexpected mode {stream[:4]}")
    nlen = 8 if ver < 10 else 16
    length = int(stream[4:4 + nlen], 2)
    body = stream[4 + nlen:4 + nlen + length * 8]
    return bytes(int(body[i:i + 8], 2) for i in range(0, len(body), 8)), {"version": ver, "level": level,
                                                                           "mask": mask, "data": data}


def matrix_from_svg(svg):
    """Rebuild the module matrix from the single-path SVG that sectorsmith.qr.qr_svg writes."""
    n = int(re.search(r'data-modules="(\d+)"', svg).group(1))
    quiet = int(re.search(r'data-quiet="(\d+)"', svg).group(1))
    size = int(re.search(r'data-module-size="(\d+)"', svg).group(1))
    m = [[0] * n for _ in range(n)]
    path = re.search(r'<path[^>]* d="([^"]*)"', svg).group(1)
    for x, y, w in re.findall(r"M(\d+) (\d+)h(\d+)v\d+h-\d+z", path):
        r = int(y) // size - quiet
        c0 = int(x) // size - quiet
        for c in range(c0, c0 + int(w) // size):
            m[r][c] = 1
    return m
