"""StrokePlan -- the one intermediate representation everything lowers into.

    SVG path --.
    text       |--> StrokePlan --> plan_to_commands() --> robot primitives
    polygon    |    (polylines,
    path    --'      in mm)

Keeping this layer pure (no sockets, no MCP, no I/O) is what makes the drawing
maths testable without a robot. It is also where a new input format plugs in:
produce a StrokePlan and everything downstream already works.

Coordinate system: millimetres, x to the right, y **up** (normal maths
convention, not SVG's y-down -- svg.py flips it on the way in). The robot is
assumed to start at the plan's first point, facing +y ("up the page").
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

Point = tuple[float, float]
Polyline = list[Point]
Command = tuple[str, float | None]  # e.g. ("forward", 100.0), ("penup", None)

EPS_DIST = 0.5  # mm  -- below this, a move is not worth a command
EPS_ANGLE = 0.5  # deg -- below this, a turn is not worth a command

# The robot starts facing "up the page".
START_HEADING = 90.0


@dataclass
class Page:
    """Paper the drawing must fit on."""

    width_mm: float = 210.0  # A4 portrait
    height_mm: float = 297.0


@dataclass
class Pose:
    """Where we believe the robot is, in page coordinates.

    This is DEAD RECKONING. It tracks what we *commanded*, not where the robot
    physically is -- wheel slip accumulates and nothing ever corrects it. It is
    still far better than the alternative, which was assuming the robot faced
    "up the page" at the start of every drawing: after a square it faces the
    opposite way, so the next drawing came out rotated 180 degrees.

    `artie_set_origin` is the manual re-sync when belief and reality drift apart.
    """

    x: float = 0.0
    y: float = 0.0
    heading: float = START_HEADING  # degrees; 90 = up the page


@dataclass
class StrokePlan:
    """A drawing: a list of polylines, each drawn with the pen down.

    Between polylines the pen lifts and the robot travels.
    """

    strokes: list[Polyline] = field(default_factory=list)

    def bbox(self) -> tuple[float, float, float, float]:
        """(min_x, min_y, max_x, max_y). Raises if the plan is empty."""
        points = [p for stroke in self.strokes for p in stroke]
        if not points:
            raise ValueError("empty plan has no bounding box")
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        return min(xs), min(ys), max(xs), max(ys)

    def size(self) -> tuple[float, float]:
        min_x, min_y, max_x, max_y = self.bbox()
        return max_x - min_x, max_y - min_y

    def drawn_length_mm(self) -> float:
        """Total pen-down distance -- i.e. how much ink this will use."""
        total = 0.0
        for stroke in self.strokes:
            for a, b in zip(stroke, stroke[1:]):
                total += math.dist(a, b)
        return total

    def translated(self, dx: float, dy: float) -> "StrokePlan":
        return StrokePlan(
            [[(x + dx, y + dy) for x, y in stroke] for stroke in self.strokes]
        )

    def scaled(self, factor: float) -> "StrokePlan":
        return StrokePlan(
            [[(x * factor, y * factor) for x, y in stroke] for stroke in self.strokes]
        )

    def scaled_to_width(self, width_mm: float) -> "StrokePlan":
        w, _ = self.size()
        if w <= 0:
            return self
        return self.scaled(width_mm / w)


def _rdp(points: Polyline, epsilon: float) -> Polyline:
    """Ramer-Douglas-Peucker: drop points that don't change the shape."""
    if len(points) < 3:
        return list(points)

    start, end = points[0], points[-1]
    line_len = math.dist(start, end)

    # Find the point furthest from the straight line start->end.
    worst_i, worst_d = 0, -1.0
    for i, p in enumerate(points[1:-1], start=1):
        if line_len < 1e-9:
            d = math.dist(p, start)
        else:
            d = abs(
                (end[0] - start[0]) * (start[1] - p[1])
                - (start[0] - p[0]) * (end[1] - start[1])
            ) / line_len
        if d > worst_d:
            worst_i, worst_d = i, d

    if worst_d <= epsilon:
        return [start, end]  # the whole run is straight enough; collapse it
    return (
        _rdp(points[: worst_i + 1], epsilon)[:-1]
        + _rdp(points[worst_i:], epsilon)
    )


def simplify(plan: StrokePlan, epsilon_mm: float = 0.5) -> StrokePlan:
    """Drop points that don't visibly change the drawing.

    Curve flattening produces far more points than a wheeled robot can honour.
    Every surviving point costs a real command -- a turn and a move, each a
    round-trip that waits for the wheels to stop -- so an unsimplified curve
    takes minutes to draw and visibly jitters. Artie's pen is about 1mm wide,
    so collapsing anything straighter than `epsilon_mm` is free, visually.
    """
    return StrokePlan([_rdp(stroke, epsilon_mm) for stroke in plan.strokes])


def _quad_bezier(p0: Point, p1: Point, p2: Point, t: float) -> Point:
    u = 1.0 - t
    return (
        u * u * p0[0] + 2 * u * t * p1[0] + t * t * p2[0],
        u * u * p0[1] + 2 * u * t * p1[1] + t * t * p2[1],
    )


def _fillet(a: Point, b: Point, c: Point, max_turn: float, corner: float) -> Polyline:
    """Replace a sharp vertex `b` with a rounded arc; return the points that
    stand in for `b` (its neighbours `a`, `c` stay put)."""
    v1 = (a[0] - b[0], a[1] - b[1])  # b -> a
    v2 = (c[0] - b[0], c[1] - b[1])  # b -> c
    l1 = math.hypot(*v1)
    l2 = math.hypot(*v2)
    if l1 < EPS_DIST or l2 < EPS_DIST:
        return [b]

    # The angle the robot pivots through at b (180 - interior angle).
    heading_in = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    heading_out = math.degrees(math.atan2(c[1] - b[1], c[0] - b[0]))
    turn = abs(_normalise_angle(heading_out - heading_in))
    if turn <= max_turn:
        return [b]  # gentle enough -- leave it crisp

    # Cut back along each segment, but never more than ~45% of the shorter one
    # (so adjacent fillets can't overlap: two corners share a segment and each
    # eats at most 45% of it).
    d = min(corner, 0.45 * l1, 0.45 * l2)
    if d < EPS_DIST:
        return [b]

    u1 = (v1[0] / l1, v1[1] / l1)
    u2 = (v2[0] / l2, v2[1] / l2)
    p1 = (b[0] + u1[0] * d, b[1] + u1[1] * d)
    p2 = (b[0] + u2[0] * d, b[1] + u2[1] * d)

    # A quadratic Bezier through p1, (control=b), p2 is tangent to both segments,
    # stays inside the corner, and never overshoots. Its bend concentrates near
    # the middle, so uniform-parameter samples don't turn evenly -- rather than
    # guess a count, add points until the worst sub-turn is actually under the
    # cap. `a` and `c` are included only to measure the junction turns.
    n = max(2, math.ceil(turn / max_turn))
    while n < 64:
        arc = [_quad_bezier(p1, b, p2, i / n) for i in range(0, n + 1)]
        chain = [a] + arc + [c]
        worst = max(
            abs(_normalise_angle(
                math.degrees(math.atan2(chain[i + 1][1] - chain[i][1],
                                        chain[i + 1][0] - chain[i][0]))
                - math.degrees(math.atan2(chain[i][1] - chain[i - 1][1],
                                          chain[i][0] - chain[i - 1][0]))))
            for i in range(1, len(chain) - 1)
        )
        if worst <= max_turn + 1e-9:
            return arc
        n += 1
    return arc


def round_corners(
    plan: StrokePlan, max_turn_deg: float = 60.0, corner_mm: float = 3.0
) -> StrokePlan:
    """Round off sharp corners so the robot never pivots hard on a wet pen.

    At a sharp vertex the robot must nearly reverse -- decelerate to a near-stop,
    pivot, accelerate away -- and it's slowest right AT the point, so ink pools
    there. The apex of every A and the bottom of every V picks up a blob.

    This replaces any vertex turning more than `max_turn_deg` with a short arc
    (cut back `corner_mm` along each side), split so the robot never turns more
    than the cap at one stop. Fillets only cut inward, so a drawing never grows
    past its bounds. Applies to text, SVG, and polygons alike.

    Note: on a CLOSED stroke (first point == last), the seam vertex is an
    endpoint and stays sharp -- a minor cost that only affects closed shapes,
    not the open strokes that make up letters.
    """
    if corner_mm <= 0 or max_turn_deg <= 0:
        return plan
    out: list[Polyline] = []
    for stroke in plan.strokes:
        if len(stroke) < 3:
            out.append(list(stroke))
            continue
        rounded: Polyline = [stroke[0]]
        for i in range(1, len(stroke) - 1):
            rounded.extend(_fillet(stroke[i - 1], stroke[i], stroke[i + 1],
                                   max_turn_deg, corner_mm))
        rounded.append(stroke[-1])
        out.append(rounded)
    return StrokePlan(out)


class OutOfBounds(ValueError):
    """The drawing is bigger than the paper."""


def check_fits(plan: StrokePlan, page: Page) -> None:
    """Fail *before* the pen moves, not when Artie drives off the desk.

    Plan coordinates are absolute page coordinates (origin at the bottom-left),
    so this checks POSITION as well as size. It could previously only compare
    sizes, because nothing knew where on the paper the drawing actually sat.
    """
    if not plan.strokes:
        return

    w, h = plan.size()
    if w > page.width_mm or h > page.height_mm:
        raise OutOfBounds(
            f"drawing is {w:.0f}x{h:.0f}mm but the page is "
            f"{page.width_mm:.0f}x{page.height_mm:.0f}mm. "
            "Scale it down or use a bigger page."
        )

    min_x, min_y, max_x, max_y = plan.bbox()
    if min_x < -EPS_DIST or min_y < -EPS_DIST or max_x > page.width_mm or max_y > page.height_mm:
        raise OutOfBounds(
            f"drawing would run off the paper: it spans x {min_x:.0f}..{max_x:.0f}mm, "
            f"y {min_y:.0f}..{max_y:.0f}mm, but the page is "
            f"{page.width_mm:.0f}x{page.height_mm:.0f}mm with the origin at the "
            "bottom-left. Move it, shrink it, or check artie_where."
        )


def _normalise_angle(deg: float) -> float:
    """Fold into (-180, 180] so we always turn the short way round."""
    deg = (deg + 180.0) % 360.0 - 180.0
    return deg + 360.0 if deg <= -180.0 else deg


def plan_to_commands(
    plan: StrokePlan, pose: Pose | None = None
) -> tuple[list[Command], Pose]:
    """Lower a StrokePlan into robot primitives, starting from where the robot is.

    Walks each polyline, lifting the pen to travel between strokes and putting
    it down to draw along them. Turns are computed from the heading change and
    always taken the short way round.

    Returns the commands AND the resulting pose. The caller must keep that pose:
    it is the only thing that stops the next drawing from being rotated. (This
    used to assume heading=90 every time -- so anything drawn after a square,
    which leaves the robot facing 270, came out upside down.)
    """
    if pose is None:  # no belief about the robot: assume it stands at the start
        first = plan.strokes[0][0] if plan.strokes else (0.0, 0.0)
        pose = Pose(first[0], first[1], START_HEADING)

    commands: list[Command] = []
    if not plan.strokes:
        return commands, pose

    x, y, heading = pose.x, pose.y, pose.heading
    pen_is_down = False

    def turn_towards(target: Point) -> None:
        """Rotate to face `target` without moving."""
        nonlocal heading
        dx, dy = target[0] - x, target[1] - y
        if math.hypot(dx, dy) < EPS_DIST:
            return
        turn = _normalise_angle(math.degrees(math.atan2(dy, dx)) - heading)
        if abs(turn) >= EPS_ANGLE:
            commands.append(("left", turn) if turn > 0 else ("right", -turn))
            heading = _normalise_angle(heading + turn)

    def travel_to(target: Point) -> None:
        nonlocal x, y, heading
        dx, dy = target[0] - x, target[1] - y
        distance = math.hypot(dx, dy)
        if distance < EPS_DIST:
            return

        turn_towards(target)

        commands.append(("forward", distance))

        # Integrate the move we ACTUALLY commanded -- do not teleport to
        # `target`. When a sub-threshold turn is skipped above, the robot drives
        # slightly off-bearing; assuming it landed on the target would hide that
        # error and let it compound across a drawing (it showed up as text that
        # slowly tilted). Tracking the true heading instead makes every later
        # segment steer back towards its own target, so the error self-corrects.
        x += distance * math.cos(math.radians(heading))
        y += distance * math.sin(math.radians(heading))

    for stroke in plan.strokes:
        if len(stroke) < 2:
            continue  # a single point draws nothing

        # Travel to the start of the stroke with the pen up.
        if pen_is_down:
            commands.append(("penup", None))
            pen_is_down = False
        travel_to(stroke[0])

        # Aim BEFORE lowering the pen. Otherwise the robot pivots on the spot
        # with the tip already touching, grinding a blob into the paper at the
        # start of every single stroke -- 17 of them in a word like "HI ARTIE".
        turn_towards(stroke[1])

        commands.append(("pendown", None))
        pen_is_down = True
        for point in stroke[1:]:
            travel_to(point)

    if pen_is_down:
        commands.append(("penup", None))

    # Normalise to [0, 360). Internally headings wander into negatives (turns are
    # taken the short way round), and reporting "facing -90deg" to a model is
    # needlessly confusing when it means 270.
    return commands, Pose(x, y, heading % 360)


def optimise_stroke_order(plan: StrokePlan, pose: Pose | None = None) -> StrokePlan:
    """Reorder strokes so the pen travels less between them (nearest neighbour).

    Text is a lot of separate strokes -- 'HI ARTIE' is 17 -- and drawing them in
    emission order means criss-crossing the page with the pen up. Greedy is
    plenty here: the robot's travel is slow, but so is thinking about it.
    """
    remaining = [s for s in plan.strokes if len(s) >= 2]
    if len(remaining) < 3:
        return StrokePlan(list(remaining))

    x, y = (pose.x, pose.y) if pose else remaining[0][0]
    ordered: list[Polyline] = []
    while remaining:
        # Nearest start point wins. (We don't consider drawing a stroke
        # backwards -- for letterforms that would look wrong to a human.)
        best = min(remaining, key=lambda s: math.dist((x, y), s[0]))
        remaining.remove(best)
        ordered.append(best)
        x, y = best[-1]
    return StrokePlan(ordered)


# --- shape constructors ---


def polygon(sides: int, side_length_mm: float) -> StrokePlan:
    """Regular polygon, drawn counter-clockwise from the robot's start point."""
    if sides < 3:
        raise ValueError("a polygon needs at least 3 sides")
    if side_length_mm <= 0:
        raise ValueError("side length must be positive")

    points: Polyline = [(0.0, 0.0)]
    x = y = 0.0
    heading = 0.0  # start heading east, in plan coordinates
    exterior = 360.0 / sides
    for _ in range(sides):
        x += side_length_mm * math.cos(math.radians(heading))
        y += side_length_mm * math.sin(math.radians(heading))
        points.append((x, y))
        heading += exterior
    return StrokePlan([points])


def path(points: list[Point], close: bool = False) -> StrokePlan:
    """A single open or closed polyline through absolute mm coordinates."""
    if len(points) < 2:
        raise ValueError("a path needs at least 2 points")
    pts = list(points)
    if close and pts[0] != pts[-1]:
        pts.append(pts[0])
    return StrokePlan([pts])


def to_svg(plan: StrokePlan, stroke_width: float = 0.6) -> str:
    """Render the plan as an SVG document -- a preview, without moving anything.

    Note the y-flip: plan coords are y-up, SVG is y-down.
    """
    if not plan.strokes:
        return '<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0"/>'

    min_x, min_y, max_x, max_y = plan.bbox()
    w = max(max_x - min_x, 1.0)
    h = max(max_y - min_y, 1.0)
    pad = 5.0

    paths = []
    for stroke in plan.strokes:
        d = " ".join(
            f"{'M' if i == 0 else 'L'} {px - min_x:.2f} {max_y - py:.2f}"
            for i, (px, py) in enumerate(stroke)
        )
        paths.append(f'<path d="{d}" fill="none" stroke="black" '
                     f'stroke-width="{stroke_width}"/>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{w + 2 * pad:.1f}mm" height="{h + 2 * pad:.1f}mm" '
        f'viewBox="{-pad:.1f} {-pad:.1f} {w + 2 * pad:.1f} {h + 2 * pad:.1f}">'
        + "".join(paths)
        + "</svg>"
    )
