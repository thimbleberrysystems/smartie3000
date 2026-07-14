"""MCP tools for the Artie 3000.

Every docstring here is a prompt. The model cannot see the robot, cannot see the
paper, and has no way to know that `forward(100)` means 10 centimetres rather
than 100 steps -- so units and pen semantics are spelled out in each one.

Two things the server owns, which the robot cannot tell us:

* **Pose.** Artie has no encoders and no idea where it is. We dead-reckon from
  the commands we sent. Without this, every drawing assumed the robot faced "up
  the page" -- and since a square leaves it facing the opposite way, the *next*
  drawing came out rotated 180 degrees, silently.

* **Page.** Coordinates are absolute millimetres from the bottom-left of the
  paper, which is only meaningful because we track where the robot is standing.
"""

from __future__ import annotations

import io
import logging
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP, Image

from .client import ArtieClient, ArtieConfig, ArtieError, make_client
from .strokes import (
    Command,
    OutOfBounds,
    Page,
    Pose,
    StrokePlan,
    check_fits,
    optimise_stroke_order,
    path as make_path,
    plan_to_commands,
    polygon as make_polygon,
    to_svg,
)
from .svg import plan_from_svg_path
from .text import plan_from_text

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

mcp = FastMCP("artie")

_config = ArtieConfig.from_env()
_client: ArtieClient | None = None

MAX_DISTANCE_MM = 1000.0  # one command should not send it across the room
PAGE = Page(
    width_mm=float(os.environ.get("ARTIE_PAGE_WIDTH_MM", "210")),
    height_mm=float(os.environ.get("ARTIE_PAGE_HEIGHT_MM", "297")),
)
PREVIEW_PATH = Path(os.environ.get("ARTIE_PREVIEW_PATH", "artie_preview.svg"))

# Where we believe the robot is. Dead reckoning -- see the module docstring.
_pose = Pose(0.0, 0.0)


def _get_client() -> ArtieClient:
    global _client
    if _client is None:
        _client = make_client(_config)
    return _client


def _check_distance(mm: float) -> None:
    if mm <= 0:
        raise ValueError("distance must be positive")
    if mm > MAX_DISTANCE_MM:
        raise ValueError(
            f"{mm}mm exceeds the {MAX_DISTANCE_MM:.0f}mm per-command limit. "
            "Break it into several moves."
        )


def _advance(cmd: str, arg: float | None) -> None:
    """Keep our belief about the robot in step with what we just told it to do."""
    import math

    global _pose
    if cmd == "left":
        _pose.heading = (_pose.heading + (arg or 0)) % 360
    elif cmd == "right":
        _pose.heading = (_pose.heading - (arg or 0)) % 360
    elif cmd in ("forward", "back"):
        sign = 1 if cmd == "forward" else -1
        _pose.x += sign * (arg or 0) * math.cos(math.radians(_pose.heading))
        _pose.y += sign * (arg or 0) * math.sin(math.radians(_pose.heading))


async def _run(commands: list[Command]) -> None:
    """Execute a planned command stream, abortably, always leaving the pen up.

    Two failures this guards against:
      * `artie_stop` used to cancel only the command in flight -- the remaining
        ~50 kept going. An emergency stop that doesn't stop.
      * any error mid-stroke left the pen DOWN, so the next move dragged a line
        across the paper.
    """
    client = _get_client()
    client.clear_abort()
    pen_down = False
    try:
        for cmd, arg in commands:
            if client.aborted:
                raise ArtieError("stopped: drawing abandoned partway through")
            if cmd == "forward":
                await client.forward(float(arg or 0))
            elif cmd == "back":
                await client.back(float(arg or 0))
            elif cmd == "left":
                await client.left(float(arg or 0))
            elif cmd == "right":
                await client.right(float(arg or 0))
            elif cmd == "penup":
                await client.pen_up()
                pen_down = False
            elif cmd == "pendown":
                await client.pen_down()
                pen_down = True
            elif cmd == "beep":
                await client.beep(int(arg or 0))
            else:
                raise ArtieError(f"unknown planned command: {cmd}")
            _advance(cmd, arg)
    finally:
        if pen_down:
            # Never leave the pen on the paper. Best effort: if the robot is
            # gone, there is nothing we can do, and the original error matters
            # more than this one.
            try:
                await client.pen_up()
            except ArtieError:
                log.warning("could not lift the pen -- robot unreachable")


def _summarise(plan: StrokePlan, commands: list[Command], what: str) -> str:
    w, h = plan.size()
    min_x, min_y, _, _ = plan.bbox()
    return (
        f"{what}: {len(plan.strokes)} stroke(s), "
        f"{plan.drawn_length_mm():.0f}mm of pen-down line, "
        f"{w:.0f}x{h:.0f}mm at ({min_x:.0f}, {min_y:.0f}), "
        f"{len(commands)} robot commands"
    )


def _render_png(plan: StrokePlan) -> bytes | None:
    """Rasterise the preview so the model can actually LOOK at it."""
    try:
        import cairosvg

        return cairosvg.svg2png(
            bytestring=to_svg(plan).encode(), scale=2, background_color="white"
        )
    except Exception as exc:  # cairo's native deps are easy to miss
        log.warning("cannot rasterise preview (%s); falling back to the SVG file", exc)
        return None


def _place(plan: StrokePlan, at_x: float | None, at_y: float | None) -> StrokePlan:
    """Put the drawing's bottom-left corner where the caller asked -- or where
    the robot is currently standing."""
    min_x, min_y, _, _ = plan.bbox()
    target_x = _pose.x if at_x is None else at_x
    target_y = _pose.y if at_y is None else at_y
    return plan.translated(target_x - min_x, target_y - min_y)


async def _draw_or_preview(
    plan: StrokePlan, what: str, preview_only: bool
) -> str | list:
    global _pose
    plan = optimise_stroke_order(plan, _pose)
    check_fits(plan, PAGE)
    commands, end_pose = plan_to_commands(plan, _pose)
    summary = _summarise(plan, commands, what)

    if preview_only:
        PREVIEW_PATH.write_text(to_svg(plan))
        png = _render_png(plan)
        text = (
            f"PREVIEW ONLY -- the robot did not move.\n{summary}\n"
            f"Also written to {PREVIEW_PATH.resolve()}\n"
            "Look at the image, then call again with preview_only=false to draw it."
        )
        return [Image(data=png, format="png"), text] if png else text

    await _run(commands)
    _pose = end_pose  # crucial: without this the next drawing is rotated
    return f"Drawn.\n{summary}\nRobot is now at {_describe_pose()}."


def _describe_pose() -> str:
    compass = {0: "right", 90: "up", 180: "left", 270: "down"}
    nearest = min(compass, key=lambda a: abs(((_pose.heading - a + 180) % 360) - 180))
    facing = compass[nearest] if abs(((_pose.heading - nearest + 180) % 360) - 180) < 15 else ""
    return (
        f"({_pose.x:.0f}, {_pose.y:.0f})mm facing {_pose.heading:.0f}deg"
        + (f" ({facing})" if facing else "")
    )


# --- status and pose ---


@mcp.tool()
async def artie_status() -> str:
    """Check whether Artie is reachable; report firmware, battery and position.

    Call this first if anything seems wrong. In dry-run mode it says so
    explicitly -- if you see that, no physical robot is being driven.
    """
    client = _get_client()
    mode = "DRY RUN (no physical robot)" if _config.dry_run else "live"
    try:
        version = await client.version()
        uptime = await client.uptime()
        voltage = await client.get_voltage()
    except ArtieError as exc:
        return f"Artie is NOT reachable at {_config.url} ({mode}).\n{exc}"

    battery = f"{voltage}V" if voltage else "not reported by this firmware"
    return (
        f"Artie is reachable at {_config.url} ({mode}).\n"
        f"Firmware: {version}\nUptime: {uptime}ms\nBattery: {battery}\n"
        f"Page: {PAGE.width_mm:.0f}x{PAGE.height_mm:.0f}mm\n"
        f"Believed position: {_describe_pose()}\n"
        f"Calibration: distance x{_config.distance_scale}, turn x{_config.turn_scale}"
    )


@mcp.tool()
async def artie_where() -> str:
    """Report where Artie is believed to be on the page, and what room is left.

    IMPORTANT: this is dead reckoning, not a sensor. Artie has no idea where it
    actually is -- we only track what we told it to do, and wheels slip. If the
    drawing is drifting from where you expect, physically reposition the robot
    and call artie_set_origin to re-sync.
    """
    return (
        f"Believed position: {_describe_pose()}\n"
        f"Page is {PAGE.width_mm:.0f}x{PAGE.height_mm:.0f}mm, origin bottom-left.\n"
        f"Room to the right: {PAGE.width_mm - _pose.x:.0f}mm; "
        f"above: {PAGE.height_mm - _pose.y:.0f}mm.\n"
        "(Dead reckoning -- re-sync with artie_set_origin if it has drifted.)"
    )


@mcp.tool()
async def artie_set_origin(
    x_mm: float = 0.0, y_mm: float = 0.0, heading_deg: float = 90.0
) -> str:
    """Tell Artie where it is standing. Call this after physically moving it.

    Coordinates are millimetres from the BOTTOM-LEFT of the paper; heading is
    degrees, 90 = facing up the page (the default, and what you get if you set
    the robot down square at the bottom-left corner).

    This is the only way to correct accumulated drift -- Artie cannot sense its
    own position.
    """
    global _pose
    _pose = Pose(x_mm, y_mm, heading_deg % 360)
    return f"Origin set. Artie is now assumed to be at {_describe_pose()}."


# --- primitives ---


@mcp.tool()
async def artie_forward(distance_mm: float) -> str:
    """Drive Artie forward. Distance is in MILLIMETRES (100 = 10cm).

    If the pen is down this draws a line; if it is up, the robot just moves.
    Returns once the robot has physically stopped.
    """
    _check_distance(distance_mm)
    await _get_client().forward(distance_mm)
    _advance("forward", distance_mm)
    return f"Moved forward {distance_mm:.0f}mm. Now at {_describe_pose()}."


@mcp.tool()
async def artie_back(distance_mm: float) -> str:
    """Drive Artie backward. Distance is in MILLIMETRES (100 = 10cm)."""
    _check_distance(distance_mm)
    await _get_client().back(distance_mm)
    _advance("back", distance_mm)
    return f"Moved back {distance_mm:.0f}mm. Now at {_describe_pose()}."


@mcp.tool()
async def artie_left(degrees: float) -> str:
    """Turn Artie left (counter-clockwise), in DEGREES. Turns in place."""
    await _get_client().left(degrees)
    _advance("left", degrees)
    return f"Turned left {degrees:.0f}deg. Now facing {_pose.heading:.0f}deg."


@mcp.tool()
async def artie_right(degrees: float) -> str:
    """Turn Artie right (clockwise), in DEGREES. Turns in place."""
    await _get_client().right(degrees)
    _advance("right", degrees)
    return f"Turned right {degrees:.0f}deg. Now facing {_pose.heading:.0f}deg."


@mcp.tool()
async def artie_pen_down() -> str:
    """Lower the pen. Everything Artie drives after this DRAWS ON THE PAPER."""
    await _get_client().pen_down()
    return "Pen down -- movement now draws."


@mcp.tool()
async def artie_pen_up() -> str:
    """Raise the pen, so Artie can move without drawing."""
    await _get_client().pen_up()
    return "Pen up -- movement no longer draws."


@mcp.tool()
async def artie_beep(duration_ms: int = 500) -> str:
    """Beep, for the stated number of MILLISECONDS. Handy to confirm it's alive."""
    await _get_client().beep(duration_ms)
    return f"Beeped for {duration_ms}ms."


@mcp.tool()
async def artie_stop() -> str:
    """Stop Artie immediately and ABANDON any drawing in progress.

    Use this if a drawing is going wrong. It cancels the current movement and
    tells the remaining planned commands not to run.
    """
    await _get_client().stop()
    return "Stopped. Any drawing in progress has been abandoned."


@mcp.tool()
async def artie_pause() -> str:
    """Pause the current movement. Resume it with artie_resume."""
    await _get_client().pause()
    return "Paused."


@mcp.tool()
async def artie_resume() -> str:
    """Resume a movement paused with artie_pause."""
    await _get_client().resume()
    return "Resumed."


@mcp.tool()
async def artie_battery() -> str:
    """Report battery voltage.

    Worth checking when drawings go wrong. A flat battery makes the motors weak,
    so lines come out short and squares don't close -- symptoms that look exactly
    like a calibration problem, and get misdiagnosed as one.
    """
    voltage = await _get_client().get_voltage()
    if voltage is None:
        return (
            "This firmware does not report battery voltage. If lines are coming "
            "out short or squares won't close, try fresh batteries anyway -- it "
            "looks just like a calibration fault."
        )
    return f"Battery: {voltage}V (4x AA; fresh alkalines read about 6V)."


# --- calibration ---


@mcp.tool()
async def artie_calibrate(
    measured_line_mm: float | None = None, measured_turn_deg: float | None = None
) -> str:
    """Work out Artie's true distance and turn scaling. Do this FIRST, once.

    Call with no arguments and it draws a test pattern: a line it *believes* is
    100mm, then a turn it believes is 90 degrees, then another 100mm line.

    Measure the two lines and the angle between them with a ruler and protractor,
    then call again passing what you actually measured. It returns the scale
    factors to set.

    Why this matters: Artie's units are inferred from the Mirobot protocol docs,
    not confirmed on this hardware. If they are wrong, nothing errors -- every
    drawing just comes out the wrong size, and every drawing tool multiplies the
    error. A square that spirals instead of closing means the TURN scale is off.
    """
    if measured_line_mm is None and measured_turn_deg is None:
        client = _get_client()
        client.clear_abort()
        await client.pen_down()
        await client.forward(100)
        await client.left(90)
        await client.forward(100)
        await client.pen_up()
        return (
            "Drew the calibration pattern: two lines that SHOULD each be 100mm, "
            "with a corner that SHOULD be 90 degrees.\n\n"
            "Measure them, then call artie_calibrate again with:\n"
            "  measured_line_mm = the length of one line (mm)\n"
            "  measured_turn_deg = the actual angle at the corner (degrees)"
        )

    lines = []
    if measured_line_mm:
        if measured_line_mm <= 0:
            raise ValueError("measured length must be positive")
        # We asked for 100mm and got `measured`. To get a true 100mm we must ask
        # for 100 * (100 / measured).
        scale = 100.0 / measured_line_mm * _config.distance_scale
        lines.append(f"  ARTIE_DISTANCE_SCALE={scale:.4f}   (asked 100mm, drew {measured_line_mm:.1f}mm)")
    if measured_turn_deg:
        if measured_turn_deg <= 0:
            raise ValueError("measured angle must be positive")
        scale = 90.0 / measured_turn_deg * _config.turn_scale
        lines.append(f"  ARTIE_TURN_SCALE={scale:.4f}   (asked 90deg, turned {measured_turn_deg:.1f}deg)")

    return (
        "Set these in the environment and restart the MCP server:\n\n"
        + "\n".join(lines)
        + "\n\nThen draw a square and check that it closes."
    )


# --- drawing ---


# structured_output=False: these can return an Image, which FastMCP's
# pydantic output model cannot serialize.
@mcp.tool(structured_output=False)
async def artie_draw_polygon(
    sides: int,
    side_length_mm: float,
    at_x_mm: float | None = None,
    at_y_mm: float | None = None,
    preview_only: bool = False,
) -> str | list:
    """Draw a regular polygon (3 = triangle, 4 = square, 6 = hexagon...).

    `side_length_mm` is the length of ONE side, in millimetres. The pen is
    lowered and raised automatically.

    By default the shape is placed where the robot currently stands. Pass
    at_x_mm / at_y_mm to place its bottom-left corner at a specific spot on the
    page (millimetres from the bottom-left of the paper).

    Set preview_only=true to SEE an image of it first, without moving the robot.
    """
    _check_distance(side_length_mm)
    plan = _place(make_polygon(sides, side_length_mm), at_x_mm, at_y_mm)
    return await _draw_or_preview(plan, f"{sides}-sided polygon", preview_only)


# structured_output=False: these can return an Image, which FastMCP's
# pydantic output model cannot serialize.
@mcp.tool(structured_output=False)
async def artie_draw_path(
    points: list[list[float]], close: bool = False, preview_only: bool = False
) -> str | list:
    """Draw straight lines through a list of [x, y] points.

    Coordinates are ABSOLUTE page positions in MILLIMETRES from the BOTTOM-LEFT
    corner of the paper: x is right, y is UP. The robot travels to the first
    point with the pen up, then draws.

    Set close=true to join the last point back to the first.
    Example: [[10,10],[110,10],[60,90]] with close=true draws a triangle.

    Set preview_only=true to SEE an image of it first, without moving the robot.
    """
    pts = [(float(p[0]), float(p[1])) for p in points if len(p) >= 2]
    if len(pts) != len(points):
        raise ValueError("every point must be a pair like [x, y]")
    plan = make_path(pts, close=close)
    return await _draw_or_preview(plan, f"path of {len(pts)} points", preview_only)


# structured_output=False: these can return an Image, which FastMCP's
# pydantic output model cannot serialize.
@mcp.tool(structured_output=False)
async def artie_draw_svg(
    svg_path_d: str,
    width_mm: float = 100.0,
    at_x_mm: float | None = None,
    at_y_mm: float | None = None,
    preview_only: bool = False,
) -> str | list:
    """Draw an SVG path. Pass the `d` attribute only -- not a whole SVG document.

    The drawing is scaled so its total width is `width_mm` MILLIMETRES. Curves
    are flattened to short straight lines, since the robot can only drive
    straight. Supports M, L, H, V, C, S, Q, T, A and Z (absolute and relative).

    Fills, colours and stroke styles are ignored: this is one pen, and it draws
    OUTLINES only. Design accordingly -- line art, not filled shapes.

    By default it is placed where the robot stands; at_x_mm / at_y_mm place its
    bottom-left corner on the page instead.

    Example: "M 0 0 L 100 0 L 50 80 Z" draws a triangle.
    Set preview_only=true to SEE an image of it first, without moving the robot.
    """
    plan = _place(plan_from_svg_path(svg_path_d, width_mm), at_x_mm, at_y_mm)
    return await _draw_or_preview(plan, "SVG path", preview_only)


# structured_output=False: these can return an Image, which FastMCP's
# pydantic output model cannot serialize.
@mcp.tool(structured_output=False)
async def artie_draw_text(
    text: str,
    height_mm: float = 20.0,
    at_x_mm: float | None = None,
    at_y_mm: float | None = None,
    wrap: bool = True,
    preview_only: bool = False,
) -> str | list:
    """Write text using a single-stroke font.

    `height_mm` is the capital-letter height in MILLIMETRES. Letters are drawn as
    pen strokes, not filled outlines.

    Only uppercase letters, digits and basic punctuation exist in the font;
    lowercase input is written as uppercase. Newlines start a new line, and by
    default long text wraps to fit the paper.

    Text gets wide fast -- roughly (height_mm * 0.83) per character -- so preview
    first if you are near the edge.

    Set preview_only=true to SEE an image of it first, without moving the robot.
    """
    max_width = PAGE.width_mm if wrap else None
    plan = _place(plan_from_text(text, height_mm, max_width_mm=max_width), at_x_mm, at_y_mm)
    return await _draw_or_preview(plan, f"text {text!r}", preview_only)


@mcp.tool()
async def artie_run_sequence(steps: list[str]) -> str:
    """Run several primitive commands in order, in ONE call.

    Each step is a string: "forward 100", "left 90", "back 50", "right 45",
    "pendown", "penup", "beep 200". Distances are MILLIMETRES, angles DEGREES.

    Use this instead of many separate tool calls -- drawing a square is one
    sequence, not nine round-trips.

    Example: ["pendown", "forward 100", "left 90", "forward 100", "penup"]
    """
    commands: list[Command] = []
    for step in steps:
        parts = step.strip().split()
        if not parts:
            continue
        verb = parts[0].lower()
        if verb in ("penup", "pendown"):
            commands.append((verb, None))
        elif verb in ("forward", "back", "left", "right", "beep"):
            if len(parts) < 2:
                raise ValueError(f"'{step}' needs a number, e.g. '{verb} 100'")
            value = float(parts[1])
            if verb in ("forward", "back"):
                _check_distance(value)
            commands.append((verb, value))
        else:
            raise ValueError(
                f"unknown step {step!r}. Use forward/back/left/right/"
                "pendown/penup/beep."
            )

    await _run(commands)  # ordered, abortable, and always leaves the pen up
    return f"Ran {len(commands)} commands. Now at {_describe_pose()}."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
