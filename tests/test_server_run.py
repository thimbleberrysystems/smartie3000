"""Tests for _run(): the abort path and the pen-up guarantee.

These are bugs 5 and 6 from the audit, and both live in the server, not the
client:

  * artie_stop cancelled the move in flight, then the remaining ~50 commands
    carried on regardless -- an emergency stop that doesn't stop.
  * any failure mid-stroke left the pen DOWN, so the next move dragged a line
    right across the drawing.
"""

from __future__ import annotations

import pytest

from artie_mcp import server
from artie_mcp.client import ArtieError, ArtieConfig, DryRunClient


class FlakyClient(DryRunClient):
    """A dry-run client that can fail, or abort, on cue."""

    def __init__(self, fail_on: int | None = None, abort_after: int | None = None):
        super().__init__(ArtieConfig(dry_run=True))
        self.fail_on = fail_on
        self.abort_after = abort_after
        self.sent = 0

    async def send(self, cmd: str, arg: object | None = None) -> str:
        self.sent += 1
        if self.fail_on is not None and self.sent == self.fail_on:
            raise ArtieError("battery died")
        if self.abort_after is not None and self.sent == self.abort_after:
            self._abort.set()  # as if the user called artie_stop
        return await super().send(cmd, arg)


@pytest.fixture(autouse=True)
def restore_client():
    original = server._client
    yield
    server._client = original


def install(client) -> None:
    server._client = client


LONG_DRAWING = [
    ("pendown", None),
    *[("forward", 10.0) for _ in range(20)],
    ("penup", None),
]


async def test_stop_abandons_the_rest_of_the_drawing():
    client = FlakyClient(abort_after=3)
    install(client)

    with pytest.raises(ArtieError, match="abandoned"):
        await server._run(LONG_DRAWING)

    # It must NOT have ploughed through all 22 commands.
    assert client.sent < 10, (
        f"sent {client.sent} commands after stop -- the abort was ignored"
    )


async def test_pen_is_lifted_when_a_drawing_fails_partway():
    client = FlakyClient(fail_on=5)  # dies mid-stroke, pen down
    install(client)

    with pytest.raises(ArtieError, match="battery died"):
        await server._run(LONG_DRAWING)

    assert client.log[-1][0] == "penup", (
        "the pen was left DOWN after a failure -- the next move will drag ink "
        f"across the page (last command was {client.log[-1][0]})"
    )


async def test_pen_is_lifted_when_a_drawing_is_aborted():
    client = FlakyClient(abort_after=3)
    install(client)

    with pytest.raises(ArtieError):
        await server._run(LONG_DRAWING)

    assert client.log[-1][0] == "penup"


async def test_a_clean_drawing_still_ends_pen_up():
    client = FlakyClient()
    install(client)
    await server._run(LONG_DRAWING)
    assert client.log[-1][0] == "penup"


async def test_abort_flag_is_cleared_at_the_start_of_each_drawing():
    """Otherwise one stop would poison every drawing for the rest of the session."""
    client = FlakyClient(abort_after=3)
    install(client)
    with pytest.raises(ArtieError):
        await server._run(LONG_DRAWING)

    client.abort_after = None  # user is done stopping
    await server._run(LONG_DRAWING)  # must not immediately abort
    assert client.log[-1][0] == "penup"
