from __future__ import annotations

import math

import pytest

from smartie3000.strokes import plan_to_commands
from smartie3000.svg import SVGParseError, parse_path, plan_from_svg_path
from smartie3000.text import UnsupportedCharacter, plan_from_text

from .test_strokes import assert_traces


# --- SVG geometry ---


def test_triangle_path():
    plan = plan_from_svg_path("M 0 0 L 100 0 L 50 80 Z", width_mm=100)
    assert len(plan.strokes) == 1
    assert plan.strokes[0][0] == plan.strokes[0][-1]  # Z closed it
    assert_traces(plan)


def test_scaled_to_requested_width():
    plan = plan_from_svg_path("M 0 0 L 10 0 L 5 8 Z", width_mm=120)
    width, _ = plan.size()
    assert width == pytest.approx(120, abs=0.5)


def test_y_axis_is_flipped():
    """SVG y grows downward; ours grows up. Without the flip, art is mirrored."""
    # In SVG this goes DOWN from the origin, so in plan space it must go UP.
    plan = plan_from_svg_path("M 0 0 L 0 10 L 10 10", width_mm=10)
    (x0, y0), (x1, y1) = plan.strokes[0][0], plan.strokes[0][1]
    assert y1 > y0 or y0 > y1  # non-degenerate
    # After normalising to the origin, the SVG start point (y=0, topmost) must
    # be the HIGHEST point in plan space.
    ys = [p[1] for p in plan.strokes[0]]
    assert plan.strokes[0][0][1] == pytest.approx(max(ys))


def test_relative_commands():
    absolute = parse_path("M 0 0 L 50 0 L 50 50")
    relative = parse_path("m 0 0 l 50 0 l 0 50")
    assert absolute[0] == relative[0]


def test_horizontal_and_vertical_shortcuts():
    strokes = parse_path("M 0 0 H 50 V 50 Z")
    assert strokes[0][:3] == [(0.0, 0.0), (50.0, 0.0), (50.0, 50.0)]


def test_curves_are_flattened_to_many_short_segments():
    strokes = parse_path("M 0 0 C 0 50, 100 50, 100 0")
    assert len(strokes[0]) > 5  # a curve, not one straight line
    # The flattened curve must actually end where the curve ends.
    assert strokes[0][-1] == pytest.approx((100.0, 0.0))


def test_quadratic_curve():
    strokes = parse_path("M 0 0 Q 50 50, 100 0")
    assert len(strokes[0]) > 5
    assert strokes[0][-1] == pytest.approx((100.0, 0.0))


def test_arc_reaches_its_endpoint():
    strokes = parse_path("M 0 0 A 50 50 0 0 1 100 0")
    assert strokes[0][-1] == pytest.approx((100.0, 0.0), abs=0.5)


def test_arc_bulges_the_right_way():
    """sweep=1 and sweep=0 must curve to opposite sides."""
    up = parse_path("M 0 0 A 50 50 0 0 1 100 0")[0]
    down = parse_path("M 0 0 A 50 50 0 0 0 100 0")[0]
    mid_up = up[len(up) // 2][1]
    mid_down = down[len(down) // 2][1]
    assert (mid_up > 0) != (mid_down > 0)


def test_multiple_subpaths_become_multiple_strokes():
    plan = plan_from_svg_path("M 0 0 L 10 0 M 0 10 L 10 10", width_mm=50)
    assert len(plan.strokes) == 2
    # The pen must lift between them.
    verbs = [c for c, _ in plan_to_commands(plan)[0]]
    assert verbs.count("pendown") == 2


def test_curves_do_not_explode_into_hundreds_of_commands():
    """Each point costs a turn AND a move, each blocking until the wheels stop.

    An unsimplified curve took 646 commands -- minutes of jittering for a shape
    a felt tip cannot resolve anyway. Guard against regressing to that.
    """
    heart = (
        "M 50 30 C 50 27, 45 20, 35 20 C 20 20, 20 38, 20 38 "
        "C 20 50, 35 62, 50 72 C 65 62, 80 50, 80 38 "
        "C 80 38, 80 20, 65 20 C 55 20, 50 27, 50 30 Z"
    )
    commands, _ = plan_to_commands(plan_from_svg_path(heart, width_mm=80))
    assert len(commands) < 120, f"{len(commands)} commands is far too many"

    # ...but it must still be recognisably curved, not collapsed to a polygon.
    assert len(commands) > 20


def test_simplification_keeps_the_shape():
    """A circle must still look like a circle after simplification."""
    plan = plan_from_svg_path("M 0 50 A 50 50 0 1 1 100 50 A 50 50 0 1 1 0 50", 100)
    points = plan.strokes[0]
    cx, cy = 50.0, 50.0
    radii = [math.dist((x, y), (cx, cy)) for x, y in points]
    assert all(abs(r - 50) < 2.0 for r in radii), "simplification distorted the circle"


def test_garbage_is_rejected_with_an_actionable_message():
    # Prose contains 'a' and 't', which ARE valid path commands -- without a
    # guard this fails later with a baffling "bad number" error.
    with pytest.raises(SVGParseError, match="must start with a moveto"):
        parse_path("this is not a path")
    with pytest.raises(SVGParseError, match="must start with a moveto"):
        parse_path("L 10 10")  # no initial moveto


def test_whole_svg_document_is_rejected_with_a_hint():
    with pytest.raises(SVGParseError, match="`d` attribute"):
        parse_path('<svg><path d="M 0 0 L 10 10"/></svg>')


def test_truncated_path_is_rejected():
    with pytest.raises(SVGParseError, match="missing arguments"):
        parse_path("M 0 0 L 50")


# --- text ---


def test_text_produces_strokes():
    plan = plan_from_text("HI", height_mm=20)
    assert len(plan.strokes) > 2
    _, height = plan.size()
    assert height == pytest.approx(20, abs=0.5)


def test_text_traces_correctly():
    assert_traces(plan_from_text("AT", height_mm=30))


def test_lowercase_is_a_real_alphabet_not_a_fold():
    """The font used to fold lowercase to uppercase; now it has real glyphs."""
    assert plan_from_text("hi", 20).strokes != plan_from_text("HI", 20).strokes


def test_the_whole_lowercase_alphabet_traces_correctly():
    """Every glyph must survive the plan->commands->replay round trip -- the
    harness that catches sign errors and heading drift in letterforms."""
    assert_traces(plan_from_text("abcdefghijklm", 30))
    assert_traces(plan_from_text("nopqrstuvwxyz", 30))


def test_descenders_dip_below_the_baseline():
    """g j p q y must reach below the line, or they read as 9 i b o v."""
    # 'v' spans the x-height only (4 grid units); 'y' adds a 2-unit descender.
    _, v_height = plan_from_text("v", 18).size()
    _, y_height = plan_from_text("y", 18).size()
    assert v_height == pytest.approx(12, abs=0.5)  # 4/6 of cap height
    assert y_height == pytest.approx(18, abs=0.5)  # (4+2)/6 of cap height


def test_ascenders_reach_cap_height():
    _, b_height = plan_from_text("b", 18).size()
    _, cap_height = plan_from_text("B", 18).size()
    assert b_height == pytest.approx(cap_height, abs=0.1)


def test_mixed_case_line_keeps_the_baseline():
    """In 'vy', the v's lowest point must sit ABOVE the plan's bottom edge --
    the y's descender defines the bottom, the v sits on the baseline."""
    plan = plan_from_text("vy", 18)
    v_bottom = min(p[1] for p in plan.strokes[0])  # first stroke belongs to v
    assert v_bottom == pytest.approx(6, abs=0.5)  # 2 grid units above the descender


def test_text_grows_wider_with_more_characters():
    one, _ = plan_from_text("I", 20).size()
    many, _ = plan_from_text("IIII", 20).size()
    assert many > one * 3


def test_unsupported_character_says_what_is_supported():
    with pytest.raises(UnsupportedCharacter, match="Supported"):
        plan_from_text("hello~", 20)


def test_empty_text_is_rejected():
    with pytest.raises(ValueError):
        plan_from_text("   ", 20)


def test_narrow_letters_advance_less_than_wide_ones():
    """Proportional spacing: 'lll' must be narrower than 'mmm' (was monospaced,
    so they used to be identical)."""
    narrow, _ = plan_from_text("lll", 30).size()
    wide, _ = plan_from_text("mmm", 30).size()
    assert narrow < wide * 0.7, "letters are still monospaced"


def test_advance_is_constant_for_repeated_letters():
    """Each added copy of a letter must widen the word by the same amount --
    i.e. the per-letter advance is stable (the basis of even spacing)."""
    w1, _ = plan_from_text("m", 30).size()
    w2, _ = plan_from_text("mm", 30).size()
    w3, _ = plan_from_text("mmm", 30).size()
    assert (w3 - w2) == pytest.approx(w2 - w1, abs=0.01)


def test_gap_between_letters_is_constant_regardless_of_widths():
    """The blank GAP between two letters is 2*SIDE_BEARING no matter how wide
    they are -- so 'l m' and 'm l' leave the same gap. That even gap is what
    reads as even spacing; the fixed-cell font failed this."""
    # advance(X) = width(XX) - width(X). gap = advance(X) - ink_width(X).
    def advance(ch):
        one, _ = plan_from_text(ch, 30).size()
        two, _ = plan_from_text(ch + ch, 30).size()
        return two - one

    def ink_width(ch):
        return plan_from_text(ch, 30).size()[0]

    gap_l = advance("l") - ink_width("l")
    gap_m = advance("m") - ink_width("m")
    assert gap_l == pytest.approx(gap_m, abs=0.01), "gaps differ by letter width"
