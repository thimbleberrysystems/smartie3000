"""SVG path -> StrokePlan.

LLMs are good at emitting SVG paths, which makes this the tool that turns a
pen-on-wheels into something genuinely useful. We support the geometry of the
`d` attribute -- M/L/H/V/C/S/Q/T/A/Z, absolute and relative -- and flatten every
curve to a polyline, because the robot can only drive in straight lines.

Deliberately NOT supported: fills, strokes, styling, transforms, multiple
elements. It is a pen taped to a wheeled robot; there is nothing to fill with.

Note the y-flip. SVG's y axis points *down*; our plan coordinates point *up*.
Without the flip, everything comes out mirrored.
"""

from __future__ import annotations

import math
import os
import re

from .strokes import Point, Polyline, StrokePlan, simplify

_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_TOKEN = re.compile(r"[MmZzLlHhVvCcSsQqTtAa]|" + _NUMBER.pattern)

# How finely to chop curves.
#
# Resist the urge to make this small. Every point becomes a turn and a move --
# each a round-trip that blocks until the wheels physically stop -- so a finely
# flattened curve takes *minutes* to draw and visibly jitters. Artie is a
# wheeled toy with a ~1mm felt tip, not a precision plotter. We flatten
# coarsely here and then run RDP simplification over the result, which collapses
# the near-straight runs that flattening inevitably over-samples.
FLATTEN_TOLERANCE_MM = 1.0
MAX_SEGMENTS = 60

# How much shape detail to trade away for cleaner ink.
#
# Every surviving vertex is a place where the robot STOPS DEAD, pivots, and
# sets off again -- and a felt tip sitting still bleeds a blob. There is no
# continuous-curve command in the firmware (we checked: `arc` is rejected), so
# the only way to get cleaner lines is to stop fewer times.
#
# Raise this for cleaner, blockier output; lower it for more faithful curves
# and more ink pooling. 0.6mm is under the width of a typical felt tip.
SIMPLIFY_EPSILON_MM = float(os.environ.get("ARTIE_SIMPLIFY_MM", "0.6"))


class SVGParseError(ValueError):
    """The path data could not be understood."""


def _tokenize(d: str) -> list[str]:
    tokens = _TOKEN.findall(d)
    if not tokens:
        raise SVGParseError(f"no drawable commands found in path: {d[:60]!r}")
    # Catch prose early. Otherwise stray letters get read as path commands
    # ('a' as an arc, 't' as a smooth quadratic) and the failure surfaces later
    # as a baffling "bad number" -- an error the caller cannot act on.
    if tokens[0] not in ("M", "m"):
        raise SVGParseError(
            f"an SVG path must start with a moveto (M or m), got {tokens[0]!r}. "
            'Pass only the `d` attribute, e.g. "M 0 0 L 100 0 L 50 80 Z" '
            "-- not a full <svg> document."
        )
    return tokens


def _segments_for(control_points: list[Point]) -> int:
    """Pick a sample count from the control polygon's length."""
    length = sum(
        math.dist(a, b) for a, b in zip(control_points, control_points[1:])
    )
    return max(4, min(MAX_SEGMENTS, int(length / FLATTEN_TOLERANCE_MM) + 1))


def _cubic(p0: Point, p1: Point, p2: Point, p3: Point) -> Polyline:
    n = _segments_for([p0, p1, p2, p3])
    out: Polyline = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        x = u**3 * p0[0] + 3 * u**2 * t * p1[0] + 3 * u * t**2 * p2[0] + t**3 * p3[0]
        y = u**3 * p0[1] + 3 * u**2 * t * p1[1] + 3 * u * t**2 * p2[1] + t**3 * p3[1]
        out.append((x, y))
    return out


def _quadratic(p0: Point, p1: Point, p2: Point) -> Polyline:
    n = _segments_for([p0, p1, p2])
    out: Polyline = []
    for i in range(1, n + 1):
        t = i / n
        u = 1 - t
        x = u**2 * p0[0] + 2 * u * t * p1[0] + t**2 * p2[0]
        y = u**2 * p0[1] + 2 * u * t * p1[1] + t**2 * p2[1]
        out.append((x, y))
    return out


def _arc(
    start: Point,
    rx: float,
    ry: float,
    rotation_deg: float,
    large_arc: bool,
    sweep: bool,
    end: Point,
) -> Polyline:
    """SVG elliptical arc -> polyline (endpoint to centre parameterisation, per spec F.6)."""
    if start == end:
        return []
    if rx == 0 or ry == 0:
        return [end]  # degenerate radii: the spec says treat as a straight line

    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rotation_deg)
    cos_p, sin_p = math.cos(phi), math.sin(phi)

    dx2 = (start[0] - end[0]) / 2
    dy2 = (start[1] - end[1]) / 2
    x1p = cos_p * dx2 + sin_p * dy2
    y1p = -sin_p * dx2 + cos_p * dy2

    # Scale radii up if they are too small to span the endpoints (spec F.6.6).
    lam = (x1p**2) / (rx**2) + (y1p**2) / (ry**2)
    if lam > 1:
        scale = math.sqrt(lam)
        rx *= scale
        ry *= scale

    denom = rx**2 * y1p**2 + ry**2 * x1p**2
    num = rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2
    factor = math.sqrt(max(0.0, num / denom)) if denom else 0.0
    if large_arc == sweep:
        factor = -factor

    cxp = factor * rx * y1p / ry
    cyp = -factor * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (start[0] + end[0]) / 2
    cy = sin_p * cxp + cos_p * cyp + (start[1] + end[1]) / 2

    def angle_of(x: float, y: float) -> float:
        return math.atan2((y - cyp) / ry, (x - cxp) / rx)

    theta1 = angle_of(x1p, y1p)
    theta2 = angle_of(-x1p, -y1p)
    delta = theta2 - theta1
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    radius = max(rx, ry)
    n = max(4, min(MAX_SEGMENTS, int(abs(delta) * radius / FLATTEN_TOLERANCE_MM) + 1))
    out: Polyline = []
    for i in range(1, n + 1):
        theta = theta1 + delta * (i / n)
        px = rx * math.cos(theta)
        py = ry * math.sin(theta)
        out.append((cos_p * px - sin_p * py + cx, sin_p * px + cos_p * py + cy))
    return out


def parse_path(d: str) -> list[Polyline]:
    """Parse an SVG `d` attribute into polylines, still in SVG (y-down) space."""
    tokens = _tokenize(d)
    i = 0

    polylines: list[Polyline] = []
    current: Polyline = []
    cursor: Point = (0.0, 0.0)
    subpath_start: Point = (0.0, 0.0)
    prev_cubic_ctrl: Point | None = None
    prev_quad_ctrl: Point | None = None
    command = ""

    def take(n: int) -> list[float]:
        nonlocal i
        if i + n > len(tokens):
            raise SVGParseError(
                f"command '{command}' is missing arguments (wanted {n})"
            )
        try:
            values = [float(tokens[i + k]) for k in range(n)]
        except ValueError as exc:
            raise SVGParseError(f"bad number near '{tokens[i]}'") from exc
        i += n
        return values

    def flush() -> None:
        nonlocal current
        if len(current) >= 2:
            polylines.append(current)
        current = []

    while i < len(tokens):
        token = tokens[i]

        if token.isalpha():
            command = token
            i += 1
        elif not command:
            raise SVGParseError(f"path starts with a number, not a command: {d[:40]!r}")
        elif command in ("M", "m"):
            command = "L" if command == "M" else "l"  # implicit lineto after moveto

        upper = command.upper()
        relative = command.islower()

        def rel(px: float, py: float) -> Point:
            return (cursor[0] + px, cursor[1] + py) if relative else (px, py)

        if upper == "M":
            flush()
            x, y = take(2)
            cursor = rel(x, y)
            subpath_start = cursor
            current = [cursor]
            prev_cubic_ctrl = prev_quad_ctrl = None

        elif upper == "L":
            x, y = take(2)
            cursor = rel(x, y)
            current.append(cursor)
            prev_cubic_ctrl = prev_quad_ctrl = None

        elif upper == "H":
            (x,) = take(1)
            cursor = (cursor[0] + x, cursor[1]) if relative else (x, cursor[1])
            current.append(cursor)
            prev_cubic_ctrl = prev_quad_ctrl = None

        elif upper == "V":
            (y,) = take(1)
            cursor = (cursor[0], cursor[1] + y) if relative else (cursor[0], y)
            current.append(cursor)
            prev_cubic_ctrl = prev_quad_ctrl = None

        elif upper == "C":
            x1, y1, x2, y2, x, y = take(6)
            c1, c2, end = rel(x1, y1), rel(x2, y2), rel(x, y)
            current.extend(_cubic(cursor, c1, c2, end))
            cursor, prev_cubic_ctrl, prev_quad_ctrl = end, c2, None

        elif upper == "S":  # smooth cubic: reflect the previous control point
            x2, y2, x, y = take(4)
            c2, end = rel(x2, y2), rel(x, y)
            c1 = (
                (2 * cursor[0] - prev_cubic_ctrl[0], 2 * cursor[1] - prev_cubic_ctrl[1])
                if prev_cubic_ctrl
                else cursor
            )
            current.extend(_cubic(cursor, c1, c2, end))
            cursor, prev_cubic_ctrl, prev_quad_ctrl = end, c2, None

        elif upper == "Q":
            x1, y1, x, y = take(4)
            c1, end = rel(x1, y1), rel(x, y)
            current.extend(_quadratic(cursor, c1, end))
            cursor, prev_quad_ctrl, prev_cubic_ctrl = end, c1, None

        elif upper == "T":  # smooth quadratic
            x, y = take(2)
            end = rel(x, y)
            c1 = (
                (2 * cursor[0] - prev_quad_ctrl[0], 2 * cursor[1] - prev_quad_ctrl[1])
                if prev_quad_ctrl
                else cursor
            )
            current.extend(_quadratic(cursor, c1, end))
            cursor, prev_quad_ctrl, prev_cubic_ctrl = end, c1, None

        elif upper == "A":
            rx, ry, rot, large, sweep, x, y = take(7)
            end = rel(x, y)
            current.extend(
                _arc(cursor, rx, ry, rot, bool(large), bool(sweep), end)
            )
            cursor = end
            prev_cubic_ctrl = prev_quad_ctrl = None

        elif upper == "Z":
            if current and current[0] != cursor:
                current.append(subpath_start)
            cursor = subpath_start
            flush()
            current = [cursor]
            prev_cubic_ctrl = prev_quad_ctrl = None

        else:
            raise SVGParseError(f"unsupported path command: {command!r}")

    flush()
    if not polylines:
        raise SVGParseError("path produced no drawable strokes")
    return polylines


def plan_from_svg_path(d: str, width_mm: float) -> StrokePlan:
    """SVG `d` attribute -> StrokePlan, y-flipped and scaled to `width_mm`."""
    if width_mm <= 0:
        raise ValueError("width_mm must be positive")

    # SVG y points down; plan y points up. Without this the drawing is mirrored.
    flipped = [[(x, -y) for x, y in poly] for poly in parse_path(d)]
    plan = StrokePlan(flipped)

    # Normalise to the origin, then scale so the drawing is `width_mm` wide.
    min_x, min_y, _, _ = plan.bbox()
    plan = plan.translated(-min_x, -min_y)

    w, h = plan.size()
    if w <= 0 and h <= 0:
        raise SVGParseError("path has zero extent -- nothing to draw")
    # A purely vertical path has zero width; scale on height instead.
    plan = plan.scaled(width_mm / w) if w > 0 else plan.scaled(width_mm / h)

    # Simplify *after* scaling: the epsilon is a real-world millimetre
    # tolerance on the paper, so it only means anything at final size.
    return simplify(plan, SIMPLIFY_EPSILON_MM)
