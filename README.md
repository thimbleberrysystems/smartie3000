# artie-mcp

An [MCP](https://modelcontextprotocol.io) server that lets an LLM drive an **Artie 3000** drawing robot — from raw motion up to SVG paths and handwriting.

![Heart and text drawn by Artie](docs/heart.png)

*Above: exactly what the robot draws. Not a mock-up — this is the actual command stream (138 moves) replayed through a simulated turtle.*

---

## What this is

Artie 3000 is a £60 kids' toy: a wheeled robot that holds a felt pen. It ships with a Blockly editor for children. This turns it into something an LLM can drive, with tools that go from `forward 100mm` all the way up to *"draw this SVG path"* and *"write this word"*.

```
"draw a heart, and write ARTIE above it"
        |
        v
  artie_draw_svg(...)      -- LLM emits an SVG path
  artie_draw_text("ARTIE") -- single-stroke font
        |
        v
   pen on paper
```

## How it talks to the robot

Artie speaks the **[Mirobot protocol](https://learn.mime.co.uk/docs/understanding-the-mirobot-protocol/)** — a JSON WebSocket protocol from an unrelated open-source robot. That was the key discovery: the toy isn't a black box, it implements a documented spec.

```
ws://<artie>:8899/websocket
-->  {"cmd": "forward", "arg": 100, "id": "abc123"}
<--  {"status": "accepted", "id": "abc123"}      # motion started
<--  {"status": "complete", "id": "abc123"}      # wheels have STOPPED
```

**Confirmed on real hardware** (firmware `3.1.21`). Two properties of the robot shape the entire design:

- **It's asynchronous.** A move reports `complete` only when the wheels physically stop — a 100mm move takes ~2.5 seconds. The client waits for it.
- **It's single-tasking.** Send a second move before the first finishes and it returns `error: "Previous command not finished"`. Commands are serialised behind a lock.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

# 1. Find the robot and check it speaks the protocol (stdlib only, no install needed)
python3 scripts/probe.py

# 2. Point the server at it
export ARTIE_HOST=192.168.1.123

# 3. Register with Claude Code
claude mcp add artie -- uv run --directory $(pwd) python -m artie_mcp
```

Then just ask: *"draw a hexagon"*, *"write HELLO"*, *"draw this SVG path"*.

### No robot? No problem.

```bash
ARTIE_DRY_RUN=1 uv run mcp dev src/artie_mcp/server.py
```

Dry-run mode exercises the whole stack — tool schemas, planner, SVG pipeline — with no hardware and no batteries.

---

## ⚠️ Calibrate before you trust a single drawing

**The units are inferred, not documented.** Millimetres and degrees come from the *Mirobot* spec, not from Artie's own docs. If they're wrong, **nothing errors** — your drawings just come out silently the wrong size, and every drawing tool multiplies the error.

```
artie_calibrate()            # draws a line that SHOULD be 100mm
                             # and a corner that SHOULD be 90 degrees
# measure them with a ruler, then:
artie_calibrate(measured_line_mm=94, measured_turn_deg=87)
# -> ARTIE_DISTANCE_SCALE=1.0638
#    ARTIE_TURN_SCALE=1.0345
```

Set those in the environment, restart, and draw a square. **If the square doesn't close, the turn scale is still off** — that's the classic symptom.

---

## Getting Artie onto your home WiFi

Out of the box Artie broadcasts its own hotspot (`Artie-XXXX`, robot at `192.168.4.1`), which means your computer has to leave your network — and lose internet — to talk to it.

You can avoid that. Artie's firmware has a **WiFi config page that nothing links to**:

```
http://192.168.4.1/admin/wifi.html
```

Join the hotspot **from a phone**, open that URL, pick your home network, and save. Artie joins your LAN *and keeps its own hotspot running*. Your computer never leaves your network. Set `ARTIE_HOST` to its new address and you're done — no network switching, ever again.

> Verified working: Artie joined a home LAN and has been driven over it since. The page appears to be served only on the hotspot side, so configure it there.

## Tools

| | |
|---|---|
| **Position** | `artie_set_origin` · `artie_where` · `artie_status` |
| **Primitives** | `artie_forward` `artie_back` (mm) · `artie_left` `artie_right` (deg) · `artie_pen_up` `artie_pen_down` · `artie_beep` · `artie_stop` · `artie_pause` `artie_resume` |
| **Batch** | `artie_run_sequence(["pendown", "forward 100", "left 90", ...])` — one call, not nine round-trips |
| **Drawing** | `artie_draw_polygon` · `artie_draw_path` · `artie_draw_svg` · `artie_draw_text` |
| **Setup** | `artie_calibrate` · `artie_battery` |

![Shapes, text, and an SVG curve placed on one page](docs/gallery.png)

*A square, a caption, a triangle and an SVG curve — each placed at absolute page coordinates.*

### Preview: let the model see its own work

Every drawing tool takes `preview_only=true`, which returns **an actual image** of what it's about to draw, plus the size and command count — and the robot doesn't move. The model can look at its drawing and fix it *before* committing ink to paper.

---

## The robot has no idea where it is

Artie has **no encoders**. It cannot sense its position or heading. The server **dead-reckons** from the commands it sent, which is the only reason absolute page coordinates mean anything at all.

This matters more than it sounds. Without pose tracking, every drawing assumed the robot faced "up the page" — but a square leaves it facing the *opposite* way, so anything drawn afterwards came out **rotated 180°**, silently. Tracking pose is what makes a shape and its caption line up.

But dead reckoning **drifts**: wheels slip, and nothing ever corrects it. If a drawing lands somewhere unexpected, physically reposition the robot and call `artie_set_origin` to re-sync.

## Gotchas worth knowing

**No battery telemetry.** `getVoltage` exists in the Artie *Max* library but not on Artie 3000's firmware. A flat battery makes the motors weak, so lines come out short and squares don't close — symptoms that look **exactly** like a calibration fault and get misdiagnosed as one. Fresh AAs are a blind first move.

**The robot takes integers, the planner emits floats.** A flattened curve is full of moves like `forward 3.7`. Rounding each one independently discards up to half a unit *every command*, and it accumulates: after ~140 commands the heading had drifted several degrees and text came out visibly slanted. The client carries the rounding remainder into the next command (error diffusion), so cumulative error stays bounded.

**Drawing is slow.** ~40mm/s, and every command blocks until the wheels stop. A detailed SVG is minutes, not seconds. Curves are simplified aggressively for this reason — an unsimplified heart was 646 commands; it's now 54, with the same shape.

## Design

Every input format lowers into one intermediate representation, so all the drawing maths is testable with no robot and no socket:

```
SVG path ─┐
text      ├─→  StrokePlan  ─→  turtle planner  ─→  primitives  ─→  robot
polygon   │   (polylines, mm)   (x, y, heading,     (forward/
path      ┘                      pen state)          left/...)
```

Add a new input format (DXF, G-code) and nothing downstream changes.

| file | |
|---|---|
| `client.py` | WebSocket transport: serialised motion, reconnect, abort, rounding carry |
| `strokes.py` | `StrokePlan`, pose, turtle planner, simplification, bounds |
| `svg.py` | SVG `d` → polylines (M/L/H/V/C/S/Q/T/A/Z, curves flattened) |
| `text.py` | single-stroke font (a pen can't fill an outline) |
| `server.py` | MCP tools |
| `scripts/probe.py` | standalone diagnostic, stdlib only |

## Configuration

| Variable | Default | |
|---|---|---|
| `ARTIE_HOST` | `192.168.4.1` | robot address (hotspot, or its LAN IP) |
| `ARTIE_PORT` | `8899` | |
| `ARTIE_DRY_RUN` | off | simulate; no robot needed |
| `ARTIE_TIMEOUT` | `30` | seconds to wait for a move to complete |
| `ARTIE_DISTANCE_SCALE` | `1.0` | **calibration** — see above |
| `ARTIE_TURN_SCALE` | `1.0` | **calibration** |
| `ARTIE_PAGE_WIDTH_MM` / `ARTIE_PAGE_HEIGHT_MM` | `210` / `297` | oversized drawings are rejected before the pen moves |

## Tests

```bash
uv run pytest        # 71 tests
```

Two carry the most weight:

- **`tests/fake_artie.py`** — an in-process robot speaking the real protocol, which can be told to stall, drop the connection, or reject commands on cue. The transport was once the only untested module, and it was where every serious bug lived.
- **`replay()`** — takes the commands actually issued, drives a simulated turtle with them, and checks the pen visits the planned points. It catches sign errors and heading drift that eyeballing a command list never would.

## Credits

Protocol reverse-engineering rests on [Mirobot](https://github.com/mirobot) (Mime Industries), and on two projects that proved an Artie 3000 speaks it: [`Artie3000_WiiRemote`](https://github.com/majki09/Artie3000_WiiRemote) and [`rogerhoward/artie3000`](https://github.com/rogerhoward/artie3000). The stroke-font idea comes from [`writing-with-artie`](https://github.com/tomhannen/writing-with-artie).

Artie 3000 is a product of Educational Insights. This project is unaffiliated.
