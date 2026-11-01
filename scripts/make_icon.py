"""Draw the greenproof icon (a white check mark on a green rounded square) as a PNG.

Standard library only, so the icon can be regenerated anywhere without extra packages.

    python scripts/make_icon.py plugins/greenproof/assets/logo.png
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZE = 512
SCALE = 4  # supersampling factor for anti-aliased edges
GREEN = (31, 157, 85)  # #1f9d55, the brandColor in the Codex manifest
WHITE = (255, 255, 255)
RADIUS = 0.22  # corner radius as a fraction of the side
STROKE = 0.085  # check-mark half-width as a fraction of the side
CHECK = ((0.26, 0.52), (0.43, 0.69), (0.75, 0.33))  # the two strokes of the tick


def _in_rounded_square(x: float, y: float) -> bool:
    r = RADIUS
    cx = min(max(x, r), 1 - r)
    cy = min(max(y, r), 1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def _near_segment(x: float, y: float, a: tuple[float, float], b: tuple[float, float]) -> bool:
    (ax, ay), (bx, by) = a, b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    px, py = ax + t * dx, ay + t * dy
    return (x - px) ** 2 + (y - py) ** 2 <= STROKE * STROKE


def _sample(x: float, y: float) -> tuple[int, int, int, int]:
    """The colour of one sub-pixel, as RGBA with alpha 0 or 255."""
    if not _in_rounded_square(x, y):
        return (0, 0, 0, 0)
    if _near_segment(x, y, CHECK[0], CHECK[1]) or _near_segment(x, y, CHECK[1], CHECK[2]):
        return (*WHITE, 255)
    return (*GREEN, 255)


def render() -> bytes:
    rows = bytearray()
    for py in range(SIZE):
        rows.append(0)  # PNG filter type: none
        for px in range(SIZE):
            acc = [0, 0, 0, 0]
            for sy in range(SCALE):
                for sx in range(SCALE):
                    r, g, b, a = _sample(
                        (px + (sx + 0.5) / SCALE) / SIZE, (py + (sy + 0.5) / SCALE) / SIZE
                    )
                    # Premultiplied, so a soft edge does not pick up the transparent black.
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = SCALE * SCALE
            alpha = acc[3] // n
            if acc[3]:
                rows.extend((acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], alpha))
            else:
                rows.extend((0, 0, 0, 0))
    return bytes(rows)


def _chunk(kind: bytes, data: bytes) -> bytes:
    body = kind + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def png() -> bytes:
    header = struct.pack(">IIBBBBB", SIZE, SIZE, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(render(), 9))
        + _chunk(b"IEND", b"")
    )


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: make_icon.py OUTPUT.png [OUTPUT.png ...]", file=sys.stderr)
        return 2
    data = png()
    for target in argv:
        path = Path(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
