"""Text -> StrokePlan, via a single-stroke font.

A pen on a wheeled robot cannot fill an outline, so ordinary fonts are useless
here: their glyphs are closed shapes meant to be filled. What we need is a
*stroke* font, where each letter is the path the pen actually walks -- the same
idea as engraving and plotter fonts (Hershey being the classic).

Glyphs are defined on a 4-wide grid, y-up, baseline at y=0. Capitals span
0..6; lowercase has an x-height of 4, ascenders (b d f h k l t) reaching the
full 6, and descenders (g j p q y) dipping to -2. Everything is scaled from
the CAP height, so `height_mm` means what you'd expect for capitals and
lowercase comes out proportionally smaller.
"""

from __future__ import annotations

from .strokes import Polyline, StrokePlan

GLYPH_WIDTH = 4.0
GLYPH_HEIGHT = 6.0  # cap height
LINE_SPACING = 1.6  # multiples of cap height

# Proportional spacing. The font used to be monospaced -- every glyph advanced
# a fixed amount regardless of width -- so a narrow `l` or `i` got the same wide
# cell as a fat `m`, leaving lop-sided gaps. Instead we advance by each glyph's
# actual ink width plus a constant SIDE_BEARING on each side, so the *gap*
# between letters is even (which is what the eye reads as evenly spaced).
SIDE_BEARING = 0.8  # blank space on each side of a glyph's ink
SPACE_ADVANCE = 3.0  # width of a space character
AVG_ADVANCE = 4.6  # rough per-char advance, for the wrap estimate only

# Each glyph is a list of pen-down strokes.
_GLYPHS: dict[str, list[Polyline]] = {
    " ": [],
    "A": [[(0, 0), (2, 6), (4, 0)], [(1, 2), (3, 2)]],
    "B": [
        [(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)],
        [(3, 3), (4, 2), (4, 1), (3, 0), (0, 0)],
    ],
    "C": [[(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1)]],
    "D": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 1), (3, 0), (0, 0)]],
    "E": [[(4, 6), (0, 6), (0, 0), (4, 0)], [(0, 3), (3, 3)]],
    "F": [[(4, 6), (0, 6), (0, 0)], [(0, 3), (3, 3)]],
    "G": [
        [(4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0), (3, 0), (4, 1), (4, 3), (2, 3)]
    ],
    "H": [[(0, 0), (0, 6)], [(4, 0), (4, 6)], [(0, 3), (4, 3)]],
    "I": [[(1, 0), (3, 0)], [(2, 0), (2, 6)], [(1, 6), (3, 6)]],
    "J": [[(3, 6), (3, 1), (2, 0), (1, 0), (0, 1)]],
    "K": [[(0, 0), (0, 6)], [(4, 6), (0, 3), (4, 0)]],
    "L": [[(0, 6), (0, 0), (4, 0)]],
    "M": [[(0, 0), (0, 6), (2, 3), (4, 6), (4, 0)]],
    "N": [[(0, 0), (0, 6), (4, 0), (4, 6)]],
    "O": [[(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)]],
    "P": [[(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)]],
    "Q": [
        [(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)],
        [(2, 2), (4, 0)],
    ],
    "R": [
        [(0, 0), (0, 6), (3, 6), (4, 5), (4, 4), (3, 3), (0, 3)],
        [(2, 3), (4, 0)],
    ],
    "S": [
        [
            (4, 5), (3, 6), (1, 6), (0, 5), (0, 4), (1, 3),
            (3, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1),
        ]
    ],
    "T": [[(0, 6), (4, 6)], [(2, 6), (2, 0)]],
    "U": [[(0, 6), (0, 1), (1, 0), (3, 0), (4, 1), (4, 6)]],
    "V": [[(0, 6), (2, 0), (4, 6)]],
    "W": [[(0, 6), (1, 0), (2, 4), (3, 0), (4, 6)]],
    "X": [[(0, 0), (4, 6)], [(0, 6), (4, 0)]],
    "Y": [[(0, 6), (2, 3), (4, 6)], [(2, 3), (2, 0)]],
    "Z": [[(0, 6), (4, 6), (0, 0), (4, 0)]],
    # --- lowercase: x-height 4, ascenders to 6, descenders to -2 ---
    "a": [
        [(4, 4), (4, 0)],
        [(4, 3), (3, 4), (1, 4), (0, 3), (0, 1), (1, 0), (3, 0), (4, 1)],
    ],
    "b": [
        [(0, 6), (0, 0)],
        [(0, 3), (1, 4), (3, 4), (4, 3), (4, 1), (3, 0), (0, 0)],
    ],
    "c": [[(4, 3), (3, 4), (1, 4), (0, 3), (0, 1), (1, 0), (3, 0), (4, 1)]],
    "d": [
        [(4, 6), (4, 0)],
        [(4, 3), (3, 4), (1, 4), (0, 3), (0, 1), (1, 0), (4, 0)],
    ],
    "e": [
        [(0, 2), (4, 2), (4, 3), (3, 4), (1, 4), (0, 3), (0, 1), (1, 0), (3, 0), (4, 1)]
    ],
    "f": [[(3, 6), (2, 6), (1, 5), (1, 0)], [(0, 4), (3, 4)]],
    "g": [
        [(4, 4), (4, -1), (3, -2), (1, -2), (0, -1)],
        [(4, 3), (3, 4), (1, 4), (0, 3), (0, 1), (1, 0), (3, 0), (4, 1)],
    ],
    "h": [[(0, 6), (0, 0)], [(0, 3), (1, 4), (3, 4), (4, 3), (4, 0)]],
    "i": [[(2, 4), (2, 0)], [(2, 5), (2, 5.4)]],
    "j": [[(3, 4), (3, -1), (2, -2), (1, -2), (0, -1)], [(3, 5), (3, 5.4)]],
    "k": [[(0, 6), (0, 0)], [(3, 4), (0, 2), (3, 0)]],
    "l": [[(2, 6), (2, 0)]],
    "m": [
        [(0, 4), (0, 0)],
        [(0, 3), (1, 4), (1.5, 4), (2, 3), (2, 0)],
        [(2, 3), (3, 4), (3.5, 4), (4, 3), (4, 0)],
    ],
    "n": [[(0, 4), (0, 0)], [(0, 3), (1, 4), (3, 4), (4, 3), (4, 0)]],
    "o": [[(1, 0), (0, 1), (0, 3), (1, 4), (3, 4), (4, 3), (4, 1), (3, 0), (1, 0)]],
    "p": [
        [(0, 4), (0, -2)],
        [(0, 3), (1, 4), (3, 4), (4, 3), (4, 1), (3, 0), (0, 0)],
    ],
    "q": [
        [(4, 4), (4, -2)],
        [(4, 3), (3, 4), (1, 4), (0, 3), (0, 1), (1, 0), (4, 0)],
    ],
    "r": [[(0, 4), (0, 0)], [(0, 3), (1, 4), (3, 4), (4, 3)]],
    "s": [
        [(4, 3), (3, 4), (1, 4), (0, 3), (1, 2), (3, 2), (4, 1), (3, 0), (1, 0), (0, 1)]
    ],
    "t": [[(1, 6), (1, 1), (2, 0), (3, 0)], [(0, 4), (3, 4)]],
    "u": [[(0, 4), (0, 1), (1, 0), (3, 0), (4, 1), (4, 4)]],
    "v": [[(0, 4), (2, 0), (4, 4)]],
    "w": [[(0, 4), (1, 0), (2, 3), (3, 0), (4, 4)]],
    "x": [[(0, 4), (4, 0)], [(0, 0), (4, 4)]],
    "y": [[(0, 4), (2, 0)], [(4, 4), (1, -2)]],
    "z": [[(0, 4), (4, 4), (0, 0), (4, 0)]],
    "0": [[(1, 0), (0, 1), (0, 5), (1, 6), (3, 6), (4, 5), (4, 1), (3, 0), (1, 0)]],
    "1": [[(1, 5), (2, 6), (2, 0)], [(1, 0), (3, 0)]],
    "2": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (0, 0), (4, 0)]],
    "3": [[(0, 6), (4, 6), (2, 3), (4, 2), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "4": [[(3, 0), (3, 6), (0, 2), (4, 2)]],
    "5": [[(4, 6), (0, 6), (0, 4), (3, 4), (4, 3), (4, 1), (3, 0), (1, 0), (0, 1)]],
    "6": [
        [
            (4, 5), (3, 6), (1, 6), (0, 5), (0, 1), (1, 0),
            (3, 0), (4, 1), (4, 2), (3, 3), (1, 3), (0, 2),
        ]
    ],
    "7": [[(0, 6), (4, 6), (1, 0)]],
    "8": [
        [
            (1, 3), (0, 4), (0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (3, 3),
            (1, 3), (0, 2), (0, 1), (1, 0), (3, 0), (4, 1), (4, 2), (3, 3),
        ]
    ],
    "9": [
        [
            (0, 1), (1, 0), (3, 0), (4, 1), (4, 5), (3, 6),
            (1, 6), (0, 5), (0, 4), (1, 3), (3, 3), (4, 4),
        ]
    ],
    ".": [[(2, 0), (2, 0.4)]],
    ",": [[(2, 0.6), (1.4, -0.6)]],
    "-": [[(1, 3), (3, 3)]],
    "+": [[(1, 3), (3, 3)], [(2, 2), (2, 4)]],
    "=": [[(1, 4), (3, 4)], [(1, 2), (3, 2)]],
    "!": [[(2, 6), (2, 2)], [(2, 0), (2, 0.4)]],
    "?": [[(0, 5), (1, 6), (3, 6), (4, 5), (4, 4), (2, 2)], [(2, 0), (2, 0.4)]],
    ":": [[(2, 4), (2, 4.4)], [(2, 1), (2, 1.4)]],
    "'": [[(2, 6), (2, 5)]],
    "/": [[(0, 0), (4, 6)]],
    "(": [[(3, 6), (1, 4), (1, 2), (3, 0)]],
    ")": [[(1, 6), (3, 4), (3, 2), (1, 0)]],
}


class UnsupportedCharacter(ValueError):
    """The stroke font has no glyph for this character."""


def supported_characters() -> str:
    return "".join(sorted(_GLYPHS))


def _wrap(text: str, chars_per_line: int) -> str:
    """Break on word boundaries so text doesn't march off the edge of the paper."""
    if chars_per_line < 1:
        return text
    out: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}".strip()
            if len(candidate) <= chars_per_line:
                line = candidate
            else:
                if line:
                    out.append(line)
                # A single word longer than the line: hard-break it rather than
                # silently overflowing.
                while len(word) > chars_per_line:
                    out.append(word[:chars_per_line])
                    word = word[chars_per_line:]
                line = word
        out.append(line)
    return "\n".join(out)


def plan_from_text(
    text: str, height_mm: float, max_width_mm: float | None = None
) -> StrokePlan:
    """Render text as pen strokes.

    `height_mm` is the CAP height: capitals come out exactly this tall, and
    lowercase proportionally smaller (x-height is 2/3 of it), with ascenders
    reaching cap height and descenders dipping below the baseline.

    If `max_width_mm` is given, text wraps at word boundaries to fit it.
    """
    if height_mm <= 0:
        raise ValueError("height_mm must be positive")
    if not text.strip():
        raise ValueError("nothing to write")

    scale = height_mm / GLYPH_HEIGHT

    if max_width_mm:
        # A rough estimate, slightly generous so lines never overflow the page
        # (check_fits would reject a line that did). Proportional widths make
        # exact char-count wrapping impossible anyway.
        per_char = AVG_ADVANCE * scale
        text = _wrap(text, int(max_width_mm // per_char))

    strokes: list[Polyline] = []

    cursor_x = 0.0
    cursor_y = 0.0
    for char in text:
        if char == "\n":
            cursor_x = 0.0
            cursor_y -= GLYPH_HEIGHT * LINE_SPACING
            continue
        if char == " ":
            cursor_x += SPACE_ADVANCE
            continue

        glyph = _GLYPHS.get(char)
        if glyph is None:
            raise UnsupportedCharacter(
                f"the stroke font has no glyph for {char!r}. "
                f"Supported: {supported_characters()!r}"
            )

        # Advance by the glyph's own ink width, not a fixed cell. Place its ink
        # SIDE_BEARING in from the cursor, so the blank gap to the next letter is
        # always exactly 2*SIDE_BEARING -- constant, whatever the letters' widths.
        xs = [x for stroke in glyph for x, _ in stroke]
        ink_min, ink_max = min(xs), max(xs)
        ink_w = ink_max - ink_min
        shift = cursor_x + SIDE_BEARING - ink_min

        for stroke in glyph:
            strokes.append(
                [
                    ((x + shift) * scale, (y + cursor_y) * scale)
                    for x, y in stroke
                ]
            )
        cursor_x += ink_w + 2 * SIDE_BEARING

    if not strokes:
        raise ValueError("nothing to write (only spaces?)")

    plan = StrokePlan(strokes)
    min_x, min_y, _, _ = plan.bbox()
    return plan.translated(-min_x, -min_y)
