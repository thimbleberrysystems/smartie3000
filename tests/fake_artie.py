"""An in-process Artie, speaking the real protocol.

This is the piece that should have existed from the start. `client.py` was the
one module with no test coverage, and every transport bug the audit found --
the dead socket, the hang on power-off, the un-abortable drawing -- lived in
exactly that gap.

It mimics the behaviour that actually shapes the client design:

* async: `accepted` immediately, `complete` only after the motion "finishes"
* single-tasking: a second long command while one is running is an error
* and it can be told to misbehave -- stall, drop, or reject -- because that is
  what a robot on four AA batteries actually does.
"""

from __future__ import annotations

import asyncio
import json

import websockets

LONG = {"forward", "back", "left", "right", "penup", "pendown", "beep", "arc"}


class FakeArtie:
    """A robot you can make fail on purpose."""

    def __init__(self, motion_time: float = 0.01) -> None:
        self.motion_time = motion_time
        self.received: list[tuple[str, object]] = []

        # Misbehaviour switches.
        self.stall = False  # accept, then never complete (jammed motor)
        self.die_on: str | None = None  # hang up when this command arrives
        self.error_on: str | None = None  # reject this command

        self._busy = False
        self._server: websockets.Server | None = None
        self.port = 0

    @property
    def url_host(self) -> str:
        return "127.0.0.1"

    async def start(self) -> None:
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def commands(self) -> list[str]:
        return [cmd for cmd, _ in self.received]

    async def _handle(self, ws) -> None:
        try:
            async for raw in ws:
                for line in raw.strip().splitlines():
                    if not line.strip():
                        continue
                    await self._one(ws, json.loads(line))
        except websockets.ConnectionClosed:
            pass

    async def _one(self, ws, msg: dict) -> None:
        cmd = msg.get("cmd", "")
        msg_id = msg.get("id")
        self.received.append((cmd, msg.get("arg")))

        if cmd == self.die_on:
            await ws.close()  # battery died / robot switched off
            return

        if cmd == self.error_on:
            await self._send(ws, {"status": "error", "id": msg_id, "msg": "nope"})
            return

        if cmd == "stop":
            self._busy = False
            await self._send(ws, {"status": "complete", "id": msg_id, "msg": ""})
            return

        if cmd in LONG:
            if self._busy:
                # The real robot's exact rejection when a motion overlaps.
                await self._send(
                    ws,
                    {
                        "status": "error",
                        "id": msg_id,
                        "msg": "Previous command not finished",
                    },
                )
                return

            self._busy = True
            await self._send(ws, {"status": "accepted", "id": msg_id})
            if self.stall:
                return  # jammed: accepted, but never completes
            await asyncio.sleep(self.motion_time)
            self._busy = False
            await self._send(ws, {"status": "complete", "id": msg_id, "msg": ""})
            return

        # Short commands complete straight away.
        replies = {"version": "fake-1.0", "uptime": "1234", "getVoltage": "4.8"}
        await self._send(
            ws, {"status": "complete", "id": msg_id, "msg": replies.get(cmd, "")}
        )

    async def _send(self, ws, payload: dict) -> None:
        try:
            await ws.send(json.dumps(payload))
        except websockets.ConnectionClosed:
            pass
