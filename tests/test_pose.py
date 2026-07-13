"""Regression tests for the two silent bugs the audit found.

Both were invisible: no error, no crash. The drawing just came out wrong.
"""

from __future__ import annotations

import math

import pytest

from artie_mcp.strokes import (
    OutOfBounds,
    Page,
    Pose,
    StrokePlan,
    check_fits,
    optimise_stroke_order,
    path,
    plan_to_commands,
    polygon,
)

from .test_strokes import replay


# --- BUG 1: the second drawing came out rotated ---


def test_a_square_leaves_the_robot_facing_the_other_way():
    """The fact that made bug 1 possible. Pin it down so nobody 'simplifies' it."""
    _, end = plan_to_commands(polygon(4, 100), Pose(0, 0, 90))
    assert end.heading % 360 == pytest.approx(270, abs=1)


def test_drawing_the_same_shape_twice_draws_it_twice_in_the_same_place():
    """THE headline bug: draw a square, draw it again -- the second came out
    rotated 180 degrees, because the planner assumed heading=90 every time."""
    square = polygon(4, 100)

    cmds1, pose1 = plan_to_commands(square, Pose(0, 0, 90))
    cmds2, _ = plan_to_commands(square, pose1)  # continue from where it ended

    drawn1 = replay(cmds1, start=(0, 0), heading=90)
    drawn2 = replay(cmds2, start=(pose1.x, pose1.y), heading=pose1.heading)

    for a, b in zip(drawn1[0], drawn2[0]):
        assert math.dist(a, b) < 1.0, (
            f"second square landed at {b} instead of {a} -- it is rotated/offset"
        )


def test_ignoring_the_returned_pose_reproduces_the_bug():
    """Guard the guard: if this passed, the test above would prove nothing."""
    square = polygon(4, 100)
    cmds1, pose1 = plan_to_commands(square, Pose(0, 0, 90))
    # The OLD behaviour: assume heading 90 again instead of using pose1.
    cmds2, _ = plan_to_commands(square, Pose(pose1.x, pose1.y, 90))

    drawn1 = replay(cmds1, start=(0, 0), heading=90)
    drawn2 = replay(cmds2, start=(pose1.x, pose1.y), heading=pose1.heading)

    worst = max(math.dist(a, b) for a, b in zip(drawn1[0], drawn2[0]))
    assert worst > 50, "expected the old approach to be badly wrong, and it isn't"


# --- BUG 2: 'absolute coordinates' were ignored ---


def test_the_same_shape_at_different_places_is_drawn_differently():
    """A triangle at (0,0) and at (120,150) used to emit IDENTICAL commands."""
    here = path([(0, 0), (100, 0), (50, 80)], close=True)
    there = path([(60, 100), (160, 100), (110, 180)], close=True)

    pose = Pose(0, 0, 90)
    cmds_here, _ = plan_to_commands(here, pose)
    cmds_there, _ = plan_to_commands(there, pose)

    assert cmds_here != cmds_there, "absolute page coordinates are being ignored"


def test_the_robot_travels_to_the_start_of_the_drawing():
    """It must drive to the shape with the pen UP, not assume it's already there."""
    plan = path([(100, 100), (150, 100), (125, 150)], close=True)
    commands, _ = plan_to_commands(plan, Pose(0, 0, 90))

    first_down = [c for c, _ in commands].index("pendown")
    travel = [c for c, _ in commands[:first_down]]
    assert "forward" in travel, "no pen-up travel to reach the drawing"

    # And it must arrive at the right spot.
    drawn = replay(commands, start=(0, 0), heading=90)
    assert math.dist(drawn[0][0], (100, 100)) < 1.0


def test_pose_after_drawing_is_where_the_pen_finished():
    plan = path([(10, 10), (60, 10)], close=False)
    commands, end = plan_to_commands(plan, Pose(0, 0, 90))
    replay(commands, start=(0, 0), heading=90)
    assert (end.x, end.y) == pytest.approx((60, 10), abs=1.0)


# --- bounds are now positional, not just size ---


def test_a_drawing_that_fits_but_hangs_off_the_edge_is_rejected():
    """Small enough for the page, but placed past its edge. Previously allowed."""
    off_the_edge = path([(190, 10), (290, 10), (240, 90)], close=True)
    with pytest.raises(OutOfBounds, match="run off the paper"):
        check_fits(off_the_edge, Page(210, 297))


def test_a_drawing_inside_the_page_is_allowed():
    check_fits(path([(10, 10), (110, 10), (60, 90)], close=True), Page(210, 297))


def test_negative_coordinates_are_rejected():
    with pytest.raises(OutOfBounds):
        check_fits(path([(-20, 10), (50, 10)], close=False), Page(210, 297))


# --- stroke ordering ---


def test_stroke_order_reduces_pen_travel():
    """Text is many separate strokes; drawing them in emission order criss-crosses."""

    def pen_up_travel(plan: StrokePlan, pose: Pose) -> float:
        total, (x, y) = 0.0, (pose.x, pose.y)
        for stroke in plan.strokes:
            total += math.dist((x, y), stroke[0])
            x, y = stroke[-1]
        return total

    pose = Pose(0, 0, 90)
    # Deliberately pessimal order: alternate near and far.
    bad = StrokePlan(
        [
            [(0, 0), (10, 0)],
            [(200, 0), (210, 0)],
            [(20, 0), (30, 0)],
            [(220, 0), (230, 0)],
            [(40, 0), (50, 0)],
        ]
    )
    good = optimise_stroke_order(bad, pose)
    assert pen_up_travel(good, pose) < pen_up_travel(bad, pose)
    assert len(good.strokes) == len(bad.strokes), "a stroke went missing"
