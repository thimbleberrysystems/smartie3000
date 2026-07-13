# artie-mcp — giving a language model a hand that can hold a pen

![Heart and text drawn by Artie](docs/heart.png)

Language models are very good at deciding what to draw and extremely bad at knowing whether they actually drew it. This project closes the first half of that gap: it gives an LLM a **body** — a £60 wheeled robot with a felt pen — and a set of tools that run from *"turn left 90 degrees"* all the way up to *"draw this SVG path"* and *"write this word."*

Then it spends most of its effort on the part that turns out to be hard: **the physical world does not do what you told it to.**

---

## Physical AI, in miniature

An Artie 3000 is a toy. But it is a startlingly complete model of the problems that show up whenever a model acts on the world instead of on text:

**The body has no senses.** We probed every sensor and introspection command in the protocol — `collideState`, `followState`, `getSettings`, encoders, battery voltage. Artie **rejects all of them**. It cannot perceive its position, its heading, whether it hit something, or whether its battery is dying. Everything it does is *open loop*: you command, and you hope.

**So the model must maintain a belief about reality.** The server dead-reckons the robot's pose from the commands it issued, because the robot cannot tell us. That belief is the only reason "draw a square, then write a caption under it" works at all. It is also, inevitably, **wrong** — wheels slip, and nothing ever corrects it.

**Actions are slow and irreversible.** Every command blocks until the wheels physically stop (~40mm/s). Ink on paper cannot be undone. A bug doesn't throw an exception; it draws a crooked line and you find out afterwards.

**The interface lies unless you make it honest.** Half the work here was discovering that the abstractions were *quietly* wrong — a planner that assumed the robot always faced "up," coordinates that claimed to be absolute but weren't, rounding that accumulated into a visible tilt. None of it errored. It just came out wrong on the paper.

That is the whole discipline of physical AI, at desk scale: *your model of the world is not the world, and the world will not tell you when you've drifted.*

---

## What the model can actually do

```
        "draw a heart, and write ARTIE above it"
                        |
                        v
   artie_draw_svg(...)        <- the model emits an SVG path
   artie_draw_text("ARTIE")   <- rendered in a single-stroke font
                        |
                        v
                  ink on paper
```

| | |
|---|---|
| **Sense of place** | `artie_set_origin` · `artie_where` · `artie_status` |
| **Motion** | `artie_forward` `artie_back` (mm) · `artie_left` `artie_right` (deg) · `artie_pen_up` `artie_pen_down` · `artie_beep` · `artie_stop` · `artie_pause` `artie_resume` |
| **Batch** | `artie_run_sequence(["pendown", "forward 100", "left 90", ...])` — one call, not nine round-trips |
| **Drawing** | `artie_draw_polygon` · `artie_draw_path` · `artie_draw_svg` · `artie_draw_text` |
| **Setup** | `artie_calibrate` · `artie_battery` |

![Shapes, text, and an SVG curve placed on one page](docs/gallery.png)

*A square, a caption, a triangle and an SVG curve — each placed at absolute page coordinates. This is the real command stream, replayed.*

### Letting the model see before it commits

Every drawing tool takes `preview_only=true`, which returns **an actual image** of what is about to be drawn — and the robot doesn't move. The model can look at its own plan and fix it *before* the pen touches paper.

This matters more for a body than for a chatbot. A wrong sentence can be retracted. A wrong line cannot.

---

## The robot is blind. Plan accordingly.

Artie has **no encoders and no sensors of any kind.** The server dead-reckons its position from what it commanded. This is what makes absolute page coordinates meaningful — and it is what makes them fragile.

If a drawing lands somewhere unexpected, the belief has drifted from reality. Physically reposition the robot and call `artie_set_origin` to re-sync. That is the only correction mechanism, because the robot cannot sense that anything is wrong.

**Closing this loop properly needs a camera** — and the paper itself is the ruler (A4 is exactly 210×297mm, so a photo can be rectified against the sheet's own corners and measured in real millimetres). That is the natural next step, and it would make calibration and drift-correction fully autonomous.

---

## ⚠️ Calibrate before you trust a single drawing

**The units are inferred, not documented.** Millimetres and degrees come from the *Mirobot* spec, not from Artie's own. If they're wrong, **nothing errors** — drawings just come out silently the wrong size, and every tool multiplies the error.

```
artie_calibrate()            # draws a line that SHOULD be 100mm
                             # and a corner that SHOULD be 90 degrees
# measure them, then:
artie_calibrate(measured_line_mm=94, measured_turn_deg=87)
# -> ARTIE_DISTANCE_SCALE=1.0638
#    ARTIE_TURN_SCALE=1.0345
```

**A shape that doesn't close is the tell.** Draw a square (or better, a pentagon — five turns compound the error further). If the last corner doesn't meet the first, the *turn* scale is off.

---

## How it talks to the robot

Artie turned out to speak the **[Mirobot protocol](https://learn.mime.co.uk/docs/understanding-the-mirobot-protocol/)** — a documented JSON-over-WebSocket protocol from an unrelated open-source robot. The toy is not a black box.

```
ws://<artie>:8899/websocket
-->  {"cmd": "forward", "arg": 100, "id": "abc123"}
<--  {"status": "accepted", "id": "abc123"}      # motion started
<--  {"status": "complete", "id": "abc123"}      # the wheels have STOPPED
```

**Confirmed on real hardware** (firmware `3.1.21`). Two properties shape everything:

- **Asynchronous.** `complete` arrives only when the wheels physically stop. A 100mm move takes ~2.5 seconds; the client waits for it.
- **Single-tasking.** A second move sent before the first finishes is rejected outright. Motion is serialised behind a lock.

## Quick start

```bash
uv venv && uv pip install -e ".[dev]"

python3 scripts/probe.py          # find the robot, verify the protocol (stdlib only)
export ARTIE_HOST=192.168.1.123

claude mcp add artie -- uv run --directory $(pwd) python -m artie_mcp
```

Then just ask: *"draw a hexagon"*, *"write HELLO"*, *"draw this SVG path"*.

**No robot?** `ARTIE_DRY_RUN=1 uv run mcp dev src/artie_mcp/server.py` exercises the entire stack — tools, planner, SVG pipeline — with no hardware and no batteries.

### Get Artie onto your home WiFi (do this first)

Out of the box, Artie broadcasts its own hotspot (`Artie-XXXX`), which means your computer has to *leave* your network — and lose internet — to talk to it. You don't have to live like that.

1. Join the `Artie-XXXX` hotspot **from your phone**.
2. Open **`http://192.168.4.1`** — Artie's own interface. The WiFi setting is in there.
3. Point it at your home network and save.

Artie joins your LAN and keeps its own hotspot running as a fallback. Find its new address (`scripts/probe.py`, or your router's DHCP table), set `ARTIE_HOST`, and you never switch networks again.

> **Note:** Artie's firmware is *derived* from Mirobot's, but it is not identical. Mirobot's admin pages (`/admin/wifi.html`) are **not present** on Artie — use `http://192.168.4.1` itself. Likewise, all of Mirobot's sensor and introspection commands are rejected. Assume the protocol's *motion* subset, and nothing more.

---

## Design

Every input format lowers into one intermediate representation, so all the drawing maths is testable with no robot and no socket:

```
SVG path ─┐
text      ├─→  StrokePlan  ─→  turtle planner  ─→  primitives  ─→  robot
polygon   │   (polylines, mm)   (x, y, heading,     (forward/
path      ┘                      pen state)          left/...)
```

| file | |
|---|---|
| `client.py` | WebSocket transport: serialised motion, reconnect, abort, rounding carry |
| `strokes.py` | `StrokePlan`, pose, turtle planner, simplification, page bounds |
| `svg.py` | SVG `d` → polylines (M/L/H/V/C/S/Q/T/A/Z; curves flattened) |
| `text.py` | single-stroke font — a pen cannot fill an outline |
| `server.py` | MCP tools |
| `scripts/probe.py` | standalone diagnostic, stdlib only |

## Three bugs the physical world taught us

None of these threw an exception. All of them just came out **wrong on the paper**.

**The second drawing was rotated 180°.** The planner assumed the robot started every drawing facing "up the page." But a square leaves it facing the *opposite* way — so a heart followed by a caption produced an upside-down caption. Silent. Fixed by tracking pose across drawings.

**"Absolute" coordinates were ignored.** A triangle at `(0,0)` and the same triangle at `(500,500)` emitted *byte-identical* commands. The docstring promised page positioning; the code delivered "wherever the robot happens to be."

**Rounding accumulated into a visible tilt.** The robot takes integers; the planner emits floats (`forward 3.7`). Rounding each command independently discards up to half a unit *every time*, and over ~140 commands the heading drifted several degrees — text came out slanted. The client now carries the remainder into the next command (error diffusion), so cumulative error stays bounded.

The third one was caught only by **rendering the output and looking at it**. The test suite passed 68/68 with the bug present.

## Other things worth knowing

**No battery telemetry.** A flat battery makes the motors weak, so lines come out short and shapes don't close — symptoms indistinguishable from a calibration fault, and routinely misdiagnosed as one. Fresh AAs are a blind first move.

**Distance costs time; complexity is nearly free.** A 33-segment circle and a 4-segment square both took 18 seconds, because both drew ~350mm of line. Curves are simplified hard for this reason: an unsimplified heart compiled to 646 blocking commands; it's now 54, with the same shape.

## Configuration

| Variable | Default | |
|---|---|---|
| `ARTIE_HOST` | `192.168.4.1` | robot address (hotspot, or its LAN IP) |
| `ARTIE_PORT` | `8899` | |
| `ARTIE_DRY_RUN` | off | simulate; no robot needed |
| `ARTIE_TIMEOUT` | `30` | seconds to wait for a move to complete |
| `ARTIE_DISTANCE_SCALE` | `1.0` | **calibration** |
| `ARTIE_TURN_SCALE` | `1.0` | **calibration** |
| `ARTIE_PAGE_WIDTH_MM` / `ARTIE_PAGE_HEIGHT_MM` | `210` / `297` | oversized drawings are rejected before the pen moves |

## Tests

```bash
uv run pytest        # 71 tests
```

Two carry the weight:

- **`tests/fake_artie.py`** — an in-process robot speaking the real protocol, which can be told to stall, drop the connection, or reject commands on cue. The transport was once the only untested module, and that is exactly where every serious bug was hiding.
- **`replay()`** — takes the commands *actually issued*, drives a simulated turtle with them, and checks the pen visits the planned points. It catches sign errors and heading drift that reading a command list never would.

## Credits

The protocol work rests on [Mirobot](https://github.com/mirobot) (Mime Industries), and on two projects that proved an Artie 3000 speaks it: [`Artie3000_WiiRemote`](https://github.com/majki09/Artie3000_WiiRemote) and [`rogerhoward/artie3000`](https://github.com/rogerhoward/artie3000). The stroke-font idea comes from [`writing-with-artie`](https://github.com/tomhannen/writing-with-artie).

Artie 3000 is a product of Educational Insights. This project is unaffiliated.
