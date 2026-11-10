"""Friday's mark: a coin with two faces. One is System 1 (the bolt), one is System 2 (the F).

Drawn in Braille dots - each terminal cell is a 2x4 dot grid - as a real disc with thickness,
turning on its vertical axis, so the rim shows as it passes edge-on.
"""

import math
import time

from rich.console import Group
from rich.live import Live
from rich.text import Text

THICKNESS = 0.34
BOLT = [(0.20, -0.60), (-0.38, 0.10), (-0.06, 0.10), (-0.20, 0.60), (0.38, -0.12), (0.06, -0.12)]
F_FACE, BOLT_FACE, RIM = (142, 162, 255), (245, 180, 84), (96, 106, 140)
_DOTS = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # braille bit for (row, col)


def _in_polygon(x: float, y: float, poly) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _f(u: float, v: float) -> bool:
    stem = -0.30 <= u <= -0.06 and -0.5 <= v <= 0.5
    top = -0.30 <= u <= 0.34 and -0.5 <= v <= -0.30
    mid = -0.30 <= u <= 0.20 and -0.10 <= v <= 0.10
    return stem or top or mid


def _sample(x: float, y: float, theta: float, parity: int) -> int:
    """0 = empty, 1 = lit dot on the visible face, 2 = lit dot on the rim."""
    if abs(y) > 1:
        return 0
    cos, sin = math.cos(theta), math.sin(theta)
    half_chord = math.sqrt(1 - y * y) * abs(cos)
    offset = THICKNESS / 2 * sin
    face_x = offset if cos > 0 else -offset  # centre of whichever face points at the viewer
    if abs(cos) > 1e-3 and abs(x - face_x) <= half_chord:
        u = (x - face_x) / abs(cos)
        ring = u * u + y * y >= 0.80**2
        glyph = _f(u, y) if cos > 0 else _in_polygon(u, y, BOLT)
        return 1 if ring or glyph else 0
    if abs(x) <= abs(offset) + half_chord:
        return 2 if parity else 0  # the rim is dithered so it reads as a different surface
    return 0


def frame(theta: float, rows: int = 12) -> list[Text]:
    """The coin at rotation `theta` (0 shows the F, pi shows the bolt) as `rows` lines."""
    size = rows * 4
    scale = (size / 2) / 1.06
    cos, sin = math.cos(theta), math.sin(theta)
    face = F_FACE if cos > 0 else BOLT_FACE
    face_rgb = tuple(int(c * (0.4 + 0.6 * abs(cos))) for c in face)
    rim_rgb = tuple(int(c * (0.55 + 0.45 * abs(sin))) for c in RIM)
    lines = []
    for row in range(rows):
        line = Text()
        for col in range(size // 2):
            bits = rim = lit = 0
            for dy in range(4):
                for dx in range(2):
                    px, py = col * 2 + dx, row * 4 + dy
                    kind = _sample((px + 0.5 - size / 2) / scale, (py + 0.5 - size / 2) / scale, theta, (px + py) % 2)
                    if kind:
                        bits |= _DOTS[dy][dx]
                        lit += 1
                        rim += kind == 2
            r, g, b = rim_rgb if rim * 2 > lit else face_rgb
            line.append(chr(0x2800 + bits), style=f"rgb({r},{g},{b})")
        lines.append(line)
    return lines


def spin(console, beside=None, seconds: float = 1.7, turns: int = 2) -> None:
    """Flip the coin and let it settle on the F. `beside` is a renderable shown to its right."""
    from rich.table import Table

    def view(theta: float):
        if beside is None:
            return Group(*frame(theta))
        grid = Table.grid(padding=(0, 3))
        grid.add_row(Group(*frame(theta)), beside)
        return grid

    if not console.is_terminal or seconds <= 0:
        console.print(view(0.0))
        return
    start = time.monotonic()
    with Live(view(0.0), console=console, refresh_per_second=30, transient=False) as live:
        while (t := (time.monotonic() - start) / seconds) < 1:
            live.update(view(turns * 2 * math.pi * (1 - (1 - t) ** 3)))  # ease out
            time.sleep(1 / 30)
        live.update(view(0.0))
