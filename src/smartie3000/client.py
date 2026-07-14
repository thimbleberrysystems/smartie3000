"""Transport to the Artie 3000.

Protocol (verified against real Artie 3000 code in two independent projects):

    ws://<host>:8899/websocket
    -->  {"cmd": "forward", "arg": 100, "id": "abc123"}
    <--  {"status": "accepted", "id": "abc123"}
    <--  {"status": "complete", "id": "abc123", "msg": ...}

Two properties of the robot shape everything here:

* It is **asynchronous**. A movement replies `accepted` immediately and
  `complete` only when the wheels actually stop. We must wait for `complete`,
  or the next command lands while the robot is still moving.

* It is **single-tasking**. Sending a second long command before the first
  finishes returns `error: "Previous command not finished"`. So long commands
  are serialised behind a lock. `stop` deliberately bypasses that lock -- an
  emergency stop that queues behind the motion it is trying to cancel is
  useless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import string
from dataclasses import dataclass

import websockets

log = logging.getLogger(__name__)

# Commands that take physical time: reply `accepted`, then `complete` later.
LONG_COMMANDS = frozenset(
    {"forward", "back", "left", "right", "penup", "pendown", "beep", "arc"}
)


class ArtieError(RuntimeError):
    """The robot rejected a command, or the transport failed."""


@dataclass(frozen=True)
class ArtieConfig:
    host: str = "192.168.4.1"
    port: int = 8899
    dry_run: bool = False
    timeout: float = 30.0

    # Calibration. The units (mm / degrees) are inferred from the Mirobot
    # protocol docs and are NOT confirmed on Artie hardware. If a forward(100)
    # does not measure 100mm, set these rather than editing call sites --
    # every drawing tool multiplies whatever error is here.
    distance_scale: float = 1.0
    turn_scale: float = 1.0

    @classmethod
    def from_env(cls) -> "ArtieConfig":
        return cls(
            host=os.environ.get("ARTIE_HOST", "192.168.4.1"),
            port=int(os.environ.get("ARTIE_PORT", "8899")),
            dry_run=os.environ.get("ARTIE_DRY_RUN", "").lower()
            in ("1", "true", "yes"),
            timeout=float(os.environ.get("ARTIE_TIMEOUT", "30")),
            distance_scale=float(os.environ.get("ARTIE_DISTANCE_SCALE", "1.0")),
            turn_scale=float(os.environ.get("ARTIE_TURN_SCALE", "1.0")),
        )

    @property
    def url(self) -> str:
        return f"ws://{self.host}:{self.port}/websocket"


def _new_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=8))


class ArtieClient:
    """One WebSocket to one robot."""

    def __init__(self, config: ArtieConfig | None = None) -> None:
        self.config = config or ArtieConfig()
        self._ws: websockets.ClientConnection | None = None
        self._reader: asyncio.Task | None = None
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._motion_lock = asyncio.Lock()
        # connect() must be atomic. Without this, two concurrent first commands
        # both see `_ws is None`, both dial, and we end up with two reader tasks
        # calling recv() on one socket -- which websockets rejects outright.
        self._connect_lock = asyncio.Lock()
        self._abort = asyncio.Event()
        # Carried-over rounding error -- see _quantise().
        self._dist_residual = 0.0
        self._turn_residual = 0.0

    # --- abort ---

    @property
    def aborted(self) -> bool:
        """Set by stop(). Long command streams must check this and bail."""
        return self._abort.is_set()

    def clear_abort(self) -> None:
        self._abort.clear()

    # --- connection ---

    async def connect(self) -> None:
        if self._ws is not None:
            return
        async with self._connect_lock:
            if self._ws is not None:  # another coroutine won the race
                return
            try:
                ws = await websockets.connect(self.config.url, open_timeout=10)
            except (OSError, asyncio.TimeoutError, websockets.WebSocketException) as exc:
                raise ArtieError(
                    f"cannot reach Artie at {self.config.url}: {exc}. "
                    "Is the robot on, and are you on its network? "
                    "Run scripts/probe.py to diagnose."
                ) from exc
            self._ws = ws
            self._reader = asyncio.create_task(self._read_loop(ws))
            log.info("connected to %s", self.config.url)

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def _read_loop(self, ws: websockets.ClientConnection) -> None:
        """Pump replies until the socket goes away -- then leave no wreckage.

        The `finally` is load-bearing. A clean close ends `async for` *normally*,
        so an except-only handler never fired: the dead socket stayed installed
        (so every later command failed forever) and in-flight commands sat
        waiting out the full timeout instead of failing at once.
        """
        reason = "connection closed"
        try:
            async for raw in ws:
                self._dispatch(raw if isinstance(raw, str) else raw.decode())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = str(exc)
            log.warning("reader stopped: %s", exc)
        finally:
            # Don't tear down a *newer* connection that replaced this one.
            if self._ws is ws:
                self._ws = None
            self._fail_all(ArtieError(f"connection to Artie lost: {reason}"))

    def _dispatch(self, raw: str) -> None:
        for line in raw.strip().splitlines():
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.debug("ignoring non-JSON frame: %r", line)
                continue

            status = msg.get("status")
            future = self._pending.get(msg.get("id", ""))

            if status == "accepted":
                continue  # motion started; the 'complete' we care about follows
            if status == "notify":
                log.info("notify: %s", msg.get("msg"))
                continue
            if future is None or future.done():
                continue
            if status == "complete":
                future.set_result(str(msg.get("msg", "")))
            elif status == "error":
                future.set_exception(
                    ArtieError(f"robot rejected command: {msg.get('msg')}")
                )

    def _fail_all(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    # --- sending ---

    async def send(self, cmd: str, arg: object | None = None) -> str:
        """Send one command and wait for the robot to report `complete`."""
        await self.connect()

        # Long commands are serialised: the robot is single-tasking and rejects
        # an overlapping motion. `stop` is deliberately NOT in LONG_COMMANDS --
        # an emergency stop that queues behind the motion it is cancelling is
        # worse than useless.
        if cmd in LONG_COMMANDS:
            async with self._motion_lock:
                return await self._send_now(cmd, arg)
        return await self._send_now(cmd, arg)

    async def _send_now(self, cmd: str, arg: object | None) -> str:
        ws = self._ws
        if ws is None:
            raise ArtieError("not connected to Artie")

        msg_id = _new_id()
        payload: dict[str, object] = {"cmd": cmd, "id": msg_id}
        if arg is not None:
            payload["arg"] = arg

        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = future
        try:
            # The Artie Max client appends CRLF; harmless if unrequired.
            await ws.send(json.dumps(payload) + "\r\n")
            return await asyncio.wait_for(future, timeout=self.config.timeout)
        except asyncio.TimeoutError as exc:
            raise ArtieError(
                f"{cmd}({arg}) sent, but the robot never reported completion "
                f"within {self.config.timeout}s. It may be stalled, stuck, or "
                "out of battery."
            ) from exc
        except websockets.WebSocketException as exc:
            # The socket died under us. Surface something the model can act on,
            # not a raw ConnectionClosedOK.
            raise ArtieError(f"lost the connection to Artie while sending {cmd}: {exc}") from exc
        finally:
            self._pending.pop(msg_id, None)

    # --- primitives (calibration and rounding happen here, once) ---
    #
    # The robot takes integers, but the planner works in floats -- a flattened
    # curve is full of moves like `forward 3.7`. Naively rounding each one throws
    # away up to half a unit EVERY command, and those errors accumulate: after
    # the ~140 commands of a detailed drawing the heading had drifted several
    # degrees, so anything drawn next came out visibly rotated.
    #
    # So we carry the remainder. Round to what we can actually send, keep what
    # was lost, and add it to the next command. Cumulative error stays below one
    # unit forever instead of growing without bound. (Same trick as error
    # diffusion in dithering.)

    def _quantise(self, value: float, residual: float) -> tuple[int, float]:
        total = value + residual
        sent = round(total)
        return sent, total - sent

    async def forward(self, mm: float) -> str:
        return await self._move(mm * self.config.distance_scale)

    async def back(self, mm: float) -> str:
        return await self._move(-mm * self.config.distance_scale)

    async def _move(self, signed_mm: float) -> str:
        sent, self._dist_residual = self._quantise(signed_mm, self._dist_residual)
        if sent == 0:
            return "complete"  # rounds to nothing; the remainder is carried
        if sent > 0:
            return await self.send("forward", sent)
        return await self.send("back", -sent)

    async def left(self, degrees: float) -> str:
        return await self._turn(degrees * self.config.turn_scale)

    async def right(self, degrees: float) -> str:
        return await self._turn(-degrees * self.config.turn_scale)

    async def _turn(self, signed_deg: float) -> str:
        sent, self._turn_residual = self._quantise(signed_deg, self._turn_residual)
        if sent == 0:
            return "complete"
        if sent > 0:
            return await self.send("left", sent)
        return await self.send("right", -sent)

    async def pen_up(self) -> str:
        return await self.send("penup")

    async def pen_down(self) -> str:
        return await self.send("pendown")

    async def beep(self, ms: int) -> str:
        return await self.send("beep", int(ms))

    async def stop(self) -> str:
        # Raise the flag FIRST. A long drawing is a stream of ~50 commands, and
        # cancelling only the one currently executing would let the other 49
        # carry on -- an emergency stop that doesn't stop.
        self._abort.set()
        return await self.send("stop")

    async def pause(self) -> str:
        return await self.send("pause")

    async def resume(self) -> str:
        return await self.send("resume")

    async def version(self) -> str:
        return await self.send("version")

    async def uptime(self) -> str:
        return await self.send("uptime")

    async def get_voltage(self) -> str | None:
        """Battery voltage, or None if this firmware doesn't support it.

        `getVoltage` is in the Artie Max library; whether Artie 3000's firmware
        has it is unconfirmed. A missing command must not look like a failure --
        this is diagnostics, not a motion.
        """
        try:
            return await self.send("getVoltage")
        except ArtieError as exc:
            log.info("getVoltage unsupported on this firmware: %s", exc)
            return None


class DryRunClient(ArtieClient):
    """Same interface, no robot.

    Lets the whole stack -- tool schemas, planner, SVG pipeline, and the LLM
    loop itself -- be exercised with no hardware and no batteries. Records
    every command so tests and `artie_preview` can assert on them.
    """

    def __init__(self, config: ArtieConfig | None = None) -> None:
        super().__init__(config)
        self.log: list[tuple[str, object | None]] = []

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send(self, cmd: str, arg: object | None = None) -> str:
        self.log.append((cmd, arg))
        log.info("[dry-run] %s(%s)", cmd, "" if arg is None else arg)
        await asyncio.sleep(0)  # keep it async-shaped, but instant
        if cmd == "version":
            return "dry-run (no robot)"
        if cmd == "uptime":
            return "0"
        if cmd == "getVoltage":
            return "6.0"
        return "complete"

    async def get_voltage(self) -> str | None:
        return await self.send("getVoltage")


def make_client(config: ArtieConfig | None = None) -> ArtieClient:
    config = config or ArtieConfig.from_env()
    return DryRunClient(config) if config.dry_run else ArtieClient(config)
