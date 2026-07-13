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

    def travel_to(target: Point) -> None:
        nonlocal x, y, heading
        dx, dy = target[0] - x, target[1] - y
        distance = math.hypot(dx, dy)
        if distance < EPS_DIST:
            return

        turn = _normalise_angle(math.degrees(math.atan2(dy, dx)) - heading)
        if abs(turn) >= EPS_ANGLE:
            commands.append(("left", turn) if turn > 0 else ("right", -turn))
            heading = _normalise_angle(heading + turn)

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

        # Draw it.
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
