"""Transport tests -- the coverage gap where every audit bug was hiding.

These drive the REAL ArtieClient over a real WebSocket against a fake robot.
Everything before this only ever exercised DryRunClient, which is precisely why
the dead-socket, hang-on-power-off and un-abortable-drawing bugs survived.
"""

from __future__ import annotations

import asyncio

import pytest
import websockets

from artie_mcp.client import ArtieClient, ArtieConfig, ArtieError

from .fake_artie import FakeArtie


@pytest.fixture
async def robot():
    bot = FakeArtie()
    await bot.start()
    yield bot
    await bot.stop()


def client_for(bot: FakeArtie, timeout: float = 2.0) -> ArtieClient:
    return ArtieClient(
        ArtieConfig(host=bot.url_host, port=bot.port, timeout=timeout)
    )


# --- the happy path actually works over a real socket ---


async def test_talks_to_the_robot(robot):
    client = client_for(robot)
    assert await client.version() == "fake-1.0"
    await client.pen_down()
    await client.forward(100)
    assert robot.commands() == ["version", "pendown", "forward"]
    await client.close()


async def test_waits_for_completion_not_just_acceptance(robot):
    """A move must not return until the wheels have actually stopped."""
    robot.motion_time = 0.2
    client = client_for(robot)
    start = asyncio.get_running_loop().time()
    await client.forward(100)
    assert asyncio.get_running_loop().time() - start >= 0.2
    await client.close()


async def test_concurrent_moves_are_serialised(robot):
    """The robot rejects overlapping motions, so the client must not send them."""
    robot.motion_time = 0.05
    client = client_for(robot)
    results = await asyncio.gather(
        client.forward(50), client.forward(60), return_exceptions=True
    )
    assert not [r for r in results if isinstance(r, Exception)], (
        "the motion lock let two long commands overlap"
    )
    await client.close()


# --- BUG 3: a dropped connection permanently broke the session ---


async def test_dead_socket_raises_artie_error_not_a_raw_websocket_error(robot):
    """The tool must return something a model can act on, not ConnectionClosedOK."""
    robot.die_on = "beep"
    client = client_for(robot)
    await client.version()

    with pytest.raises(ArtieError):
        await client.beep(10)  # kills the connection
    await asyncio.sleep(0.1)

    # Now take the robot away completely (batteries out). Every later command
    # must still be a clean ArtieError -- previously a raw ConnectionClosedOK
    # leaked straight out of the MCP tool, and the session never recovered.
    await robot.stop()
    with pytest.raises(ArtieError, match="cannot reach Artie"):
        await client.forward(100)
    await client.close()


async def test_reconnects_after_the_robot_comes_back(robot):
    """Artie runs on AA cells over ESP8266 wifi. Drops are routine, not exotic."""
    robot.die_on = "beep"
    client = client_for(robot)
    await client.version()

    with pytest.raises(ArtieError):
        await client.beep(10)
    await asyncio.sleep(0.1)

    robot.die_on = None  # robot is back
    assert await client.version() == "fake-1.0", "client never re-dialled"
    await client.close()


# --- BUG 4: a clean power-off hung for the full timeout ---


async def test_power_off_fails_in_flight_commands_immediately(robot):
    """`async for` ends *normally* on a clean close, so nothing failed the futures.

    Every in-flight command then waited out the full timeout -- 30s by default,
    per command, mid-drawing.
    """
    robot.die_on = "forward"
    client = client_for(robot, timeout=10.0)  # generous: a hang would blow the budget
    await client.version()

    start = asyncio.get_running_loop().time()
    with pytest.raises(ArtieError):
        await client.forward(100)
    elapsed = asyncio.get_running_loop().time() - start

    assert elapsed < 2.0, (
        f"took {elapsed:.1f}s to notice the robot was gone -- it waited for the "
        "timeout instead of reacting to the closed socket"
    )
    await client.close()


async def test_stalled_motor_times_out_cleanly(robot):
    """Accepted but never completed: a jammed wheel. Must not hang forever."""
    robot.stall = True
    client = client_for(robot, timeout=0.5)
    with pytest.raises(ArtieError, match="never reported completion"):
        await client.forward(100)
    await client.close()


# --- BUG 5: stop could not abort a drawing ---


async def test_stop_raises_the_abort_flag(robot):
    client = client_for(robot)
    await client.version()
    assert not client.aborted
    await client.stop()
    assert client.aborted, "stop must signal callers to abandon the command stream"

    client.clear_abort()
    assert not client.aborted
    await client.close()


async def test_robot_rejection_surfaces_as_artie_error(robot):
    robot.error_on = "forward"
    client = client_for(robot)
    with pytest.raises(ArtieError, match="rejected"):
        await client.forward(100)
    await client.close()


# --- new protocol commands ---


async def test_battery_voltage(robot):
    client = client_for(robot)
    assert await client.get_voltage() == "4.8"
    await client.close()


async def test_unsupported_command_degrades_gracefully(robot):
    """getVoltage is in the Artie Max library; Artie 3000 may not have it."""
    robot.error_on = "getVoltage"
    client = client_for(robot)
    assert await client.get_voltage() is None  # not an exception
    await client.close()


# --- rounding drift: the bug that tilted the text ---


async def test_rounding_error_is_carried_not_discarded(robot):
    """The robot takes integers; the planner emits floats.

    Rounding each command independently throws away up to half a unit every
    time, and it accumulates: after ~140 commands of a detailed drawing the
    heading had drifted several degrees and the next drawing came out visibly
    rotated. The remainder must be carried into the following command.
    """
    client = client_for(robot)

    # 100 turns of 0.4 degrees = 40 degrees of real rotation.
    for _ in range(100):
        await client.left(0.4)

    total = sum(
        arg if cmd == "left" else -arg
        for cmd, arg in robot.received
        if cmd in ("left", "right")
    )
    assert abs(total - 40) <= 1, (
        f"asked for 40 degrees in small steps, robot was told {total} -- "
        "the rounding remainder is being discarded"
    )
    await client.close()


async def test_distance_rounding_is_carried_too(robot):
    client = client_for(robot)
    for _ in range(100):
        await client.forward(1.5)  # 150mm total; naive rounding would send 200

    total = sum(
        arg if cmd == "forward" else -arg
        for cmd, arg in robot.received
        if cmd in ("forward", "back")
    )
    assert abs(total - 150) <= 1, f"asked for 150mm, robot was told {total}mm"
    await client.close()


async def test_a_command_that_rounds_to_zero_is_not_sent(robot):
    """A 0.2mm move is not worth a round-trip that blocks on the wheels."""
    client = client_for(robot)
    await client.forward(0.2)
    assert "forward" not in robot.commands()
    await client.close()
