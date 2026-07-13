"""The planner is the part most likely to be silently wrong, so test it hardest.

The key test is `replay`: take the robot commands we emit, drive a simulated
turtle with them, and check the pen actually visits the points we planned. That
catches sign errors, heading drift and unit slips that eyeballing the command
list would not.
"""

from __future__ import annotations

import math

import pytest

from artie_mcp.strokes import (
    OutOfBounds,
    Page,
    START_HEADING,
    StrokePlan,
    check_fits,
    path,
    plan_to_commands,
    polygon,
    to_svg,
)


def replay(commands, start=(0.0, 0.0), heading=START_HEADING):
    """Drive a simulated turtle and record the pen-down polylines it draws."""
    x, y = start
    pen_down = False
    strokes: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    for cmd, arg in commands:
        if cmd == "pendown":
            pen_down = True
            current = [(x, y)]
        elif cmd == "penup":
            if pen_down and len(current) >= 2:
                strokes.append(current)
            pen_down = False
            current = []
        elif cmd == "left":
            heading += arg
        elif cmd == "right":
            heading -= arg
        elif cmd in ("forward", "back"):
            sign = 1 if cmd == "forward" else -1
            x += sign * arg * math.cos(math.radians(heading))
            y += sign * arg * math.sin(math.radians(heading))
            if pen_down:
                current.append((x, y))
    if pen_down and len(current) >= 2:
        strokes.append(current)
    return strokes


def assert_traces(plan: StrokePlan, tol: float = 1.0) -> None:
    """The robot's actual pen path must match the plan it was built from.

    The robot starts at the plan's first point (that is what plan_to_commands
    assumes), so the simulator must start there too.
    """
    start = plan.strokes[0][0] if plan.strokes else (0.0, 0.0)
    commands, _ = plan_to_commands(plan)
    drawn = replay(commands, start=start)
    assert len(drawn) == len([s for s in plan.strokes if len(s) >= 2])
    for expected, actual in zip(plan.strokes, drawn):
        assert len(actual) == len(expected)
        for (ex, ey), (ax, ay) in zip(expected, actual):
            assert math.dist((ex, ey), (ax, ay)) < tol, (
                f"planned {(ex, ey)} but robot reached {(ax, ay)}"
            )


# --- polygons ---


def test_square_has_four_sides_and_four_right_angles():
    commands, _ = plan_to_commands(polygon(4, 100))
    forwards = [a for c, a in commands if c == "forward"]
    turns = [a for c, a in commands if c in ("left", "right")]

    # 4 sides drawn, each 100mm (a 5th tiny move to close is possible; allow it)
    sides = [f for f in forwards if f > 1.0]
    assert len(sides) == 4
    assert all(abs(s - 100) < 1.0 for s in sides)

    # The interior turns are 90 degrees.
    assert sum(1 for t in turns if abs(t - 90) < 1.0) >= 3


def test_square_closes_rather_than_spiralling():
    """The classic symptom of turn-scaling drift is a square that never closes."""
    plan = polygon(4, 100)
    start, end = plan.strokes[0][0], plan.strokes[0][-1]
    assert math.dist(start, end) < 0.01


@pytest.mark.parametrize("sides", [3, 4, 5, 6, 8, 12])
def test_polygons_trace_correctly(sides):
    assert_traces(polygon(sides, 60))


def test_polygon_rejects_degenerate_input():
    with pytest.raises(ValueError):
        polygon(2, 100)
    with pytest.raises(ValueError):
        polygon(4, 0)


# --- paths ---


def test_path_traces_correctly():
    assert_traces(path([(0, 0), (100, 0), (50, 80)], close=True))


def test_closed_path_returns_to_start():
    plan = path([(0, 0), (100, 0), (50, 80)], close=True)
    assert plan.strokes[0][0] == plan.strokes[0][-1]


def test_path_needs_two_points():
    with pytest.raises(ValueError):
        path([(0, 0)])


# --- pen handling ---


def test_pen_lifts_between_separate_strokes():
    plan = StrokePlan([[(0, 0), (50, 0)], [(0, 50), (50, 50)]])
    commands, _ = plan_to_commands(plan)
    verbs = [c for c, _ in commands]

    # Must lift before travelling to the second stroke, and end with the pen up.
    assert verbs.count("pendown") == 2
    assert verbs.count("penup") >= 2
    assert verbs[-1] == "penup"
    # The travel move must happen while the pen is up.
    first_down = verbs.index("pendown")
    assert "forward" not in verbs[:first_down] or verbs[0] != "pendown"


def test_two_strokes_both_trace_correctly():
    assert_traces(StrokePlan([[(0, 0), (50, 0)], [(0, 50), (50, 50)]]))


def test_single_point_stroke_draws_nothing():
    assert plan_to_commands(StrokePlan([[(10, 10)]]))[0] == []


def test_empty_plan_is_safe():
    assert plan_to_commands(StrokePlan([]))[0] == []


# --- turns take the short way round ---


def test_turn_never_exceeds_180_degrees():
    plan = StrokePlan([[(0, 0), (0, 50), (0, 0)]])  # forward then straight back
    for cmd, arg in plan_to_commands(plan)[0]:
        if cmd in ("left", "right"):
            assert abs(arg) <= 180.0 + 1e-6


# --- bounds ---


def test_oversized_drawing_is_rejected_before_the_pen_moves():
    with pytest.raises(OutOfBounds, match="page"):
        check_fits(polygon(4, 250), Page(210, 297))


def test_drawing_that_fits_is_allowed():
    check_fits(polygon(4, 50), Page(210, 297))  # must not raise


# --- reporting ---


def test_drawn_length_of_square_is_its_perimeter():
    assert polygon(4, 100).drawn_length_mm() == pytest.approx(400, abs=1)


def test_preview_svg_is_wellformed():
    svg = to_svg(polygon(4, 100))
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    assert "<path" in svg
