"""Corner rounding: cap the pivot angle at any single stop so a wet pen doesn't
blot at sharp points (V bottoms, A apexes, W/M zigzags)."""

from __future__ import annotations

import math

import pytest

from smartie3000.strokes import (
    Pose,
    StrokePlan,
    plan_to_commands,
    polygon,
    round_corners,
)
from smartie3000.text import plan_from_text

from .test_strokes import assert_traces


def _turn_at(a, b, c) -> float:
    hi = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))
    ho = math.degrees(math.atan2(c[1] - b[1], c[0] - b[0]))
    return abs((ho - hi + 180) % 360 - 180)


def max_turn(plan: StrokePlan) -> float:
    """The sharpest pivot the robot makes with the pen DOWN -- pen-up
    repositioning turns don't blot, so they don't count."""
    worst = 0.0
    for stroke in plan.strokes:
        for i in range(1, len(stroke) - 1):
            worst = max(worst, _turn_at(stroke[i - 1], stroke[i], stroke[i + 1]))
    return worst


def test_cap_is_enforced_on_a_right_angle():
    sharp = StrokePlan([[(0, 0), (0, 50), (50, 50)]])  # one 90-deg corner
    assert max_turn(sharp) > 80
    rounded = round_corners(sharp, max_turn_deg=45, corner_mm=8)
    assert max_turn(rounded) <= 45 + 1e-6, "a sub-turn still exceeds the cap"


def test_pointy_letters_are_tamed():
    """A/V/W turn ~145 deg at the point; after rounding, nothing over the cap."""
    for ch in "AVWM":
        plan = plan_from_text(ch, 30)
        assert max_turn(plan) > 110, f"{ch} was expected to be sharp"
        rounded = round_corners(plan, max_turn_deg=60, corner_mm=3)
        assert max_turn(rounded) <= 60 + 1e-6, f"{ch} still pivots too hard"


def test_gentle_shapes_are_untouched():
    """Turns already under the cap must not be rounded (keeps commands low and
    crisp corners crisp)."""
    gentle = StrokePlan([[(0, 0), (50, 0), (100, 10)]])  # tiny bend
    assert round_corners(gentle, max_turn_deg=60, corner_mm=3).strokes == gentle.strokes


def test_rounding_never_grows_the_bounding_box():
    """Fillets cut inward only -- a rounded drawing can't run off the page."""
    plan = plan_from_text("WAVY", 40)
    b0 = plan.bbox()
    b1 = round_corners(plan, max_turn_deg=45, corner_mm=4).bbox()
    assert b1[0] >= b0[0] - 1e-6 and b1[1] >= b0[1] - 1e-6
    assert b1[2] <= b0[2] + 1e-6 and b1[3] <= b0[3] + 1e-6


def test_endpoints_are_preserved():
    """Only interior vertices move; a stroke still starts and ends where it did."""
    plan = plan_from_text("V", 30)
    for before, after in zip(plan.strokes,
                             round_corners(plan, 45, 3).strokes):
        assert before[0] == after[0]
        assert before[-1] == after[-1]


def test_short_segments_are_clamped_not_broken():
    """A corner between two tiny segments can't cut back far -- must stay valid."""
    tight = StrokePlan([[(0, 0), (1, 0), (1, 1)]])  # 1mm segments, 90-deg corner
    out = round_corners(tight, max_turn_deg=45, corner_mm=10).strokes[0]
    assert all(math.isfinite(x) and math.isfinite(y) for x, y in out)
    assert out[0] == (0, 0) and out[-1] == (1, 1)


def test_rounded_polygon_still_traces():
    """The rounded shape must survive the plan->commands->replay round trip."""
    assert_traces(round_corners(polygon(3, 60), max_turn_deg=45, corner_mm=5))


def test_zero_corner_is_a_no_op():
    plan = plan_from_text("A", 30)
    assert round_corners(plan, 45, 0).strokes == plan.strokes
