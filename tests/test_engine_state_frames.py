# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""EngineClient.on_frame / on_connect, the hooks the Companion relay uses.

Pinned: every /engine/status/* frame reaches on_frame with its exact bytes, in
arrival order, BEFORE the cache changes and before on_status; other addresses
don't; a crashing listener is isolated; on_connect fires once per real
connection, before that connection's first frame, and never on a failed
connect attempt.
"""

import asyncio

import websockets
from pythonosc.osc_message_builder import OscMessageBuilder

from cuemspowerbridge.engine_state import EngineClient


def _frame(address: str, value) -> bytes:
    b = OscMessageBuilder(address)
    b.add_arg(value)
    return b.build().dgram


class _Ws:
    def __init__(self, frames):
        self._frames = list(frames)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


def _consume(client, frames):
    asyncio.run(client._consume(_Ws(frames)))


def test_frames_arrive_raw_in_order_and_before_the_cache():
    c = EngineClient("ws://unused")
    seen = []
    frames = [
        _frame("/engine/status/load", "show_x"),
        _frame("/engine/status/cue/A", 1),
        _frame("/engine/command/go", 1),            # not a status frame
        _frame("/engine/status/running", "yes"),
        _frame("/engine/status/cue_name/A", "Canción ñ"),
    ]

    def on_frame(address, raw):
        # The cache must still hold the PREVIOUS value when we are called.
        seen.append(("frame", address, raw, c.load, c.running))

    c.on_frame(on_frame)
    c.on_status(lambda k, v: seen.append(("status", k)))
    _consume(c, frames)

    frame_calls = [s for s in seen if s[0] == "frame"]
    assert [s[1] for s in frame_calls] == [
        "/engine/status/load", "/engine/status/cue/A",
        "/engine/status/running", "/engine/status/cue_name/A",
    ]
    assert [s[2] for s in frame_calls] == [frames[0], frames[1], frames[3], frames[4]]
    assert frame_calls[0][3] == "unknown"            # load not yet applied
    assert frame_calls[2][4] == "unknown"            # running not yet applied
    # Each frame hook runs before its own on_status.
    order = [s[0] if s[0] == "frame" else s[1] for s in seen]
    assert order == ["frame", "load", "frame", "cue/A", "frame", "running",
                     "frame", "cue_name/A"]


def test_a_crashing_frame_listener_is_isolated():
    c = EngineClient("ws://unused")
    got = []

    def boom(address, raw):
        raise RuntimeError("x")

    c.on_frame(boom)
    c.on_frame(lambda a, r: got.append(a))
    c.on_status(lambda k, v: got.append(k))
    _consume(c, [_frame("/engine/status/load", "show_x")])
    assert got == ["/engine/status/load", "load"]
    assert c.load == "show_x"


async def test_on_connect_once_per_real_connection_before_its_frames():
    connections = 0

    async def handler(ws, *args):
        nonlocal connections
        connections += 1
        await ws.send(_frame("/engine/status/running", "no"))
        await asyncio.sleep(0.05)
        await ws.close()

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        c = EngineClient(f"ws://127.0.0.1:{port}")
        events = []
        c.on_connect(lambda: events.append("connect"))
        c.on_frame(lambda a, r: events.append("frame"))
        c.on_disconnect(lambda: events.append("disconnect"))
        await c.start()
        for _ in range(60):                       # backoff 1 s between tries
            if events.count("frame") >= 2:
                break
            await asyncio.sleep(0.05)
        await c.stop()
    assert events[:6] == ["connect", "frame", "disconnect",
                          "connect", "frame", "disconnect"]
    assert events.count("connect") == connections


async def test_no_on_connect_for_a_failed_attempt():
    c = EngineClient("ws://127.0.0.1:9")       # nothing listens
    events = []
    c.on_connect(lambda: events.append("connect"))
    c.on_disconnect(lambda: events.append("disconnect"))
    await c.start()
    await asyncio.sleep(0.3)
    await c.stop()
    assert events == []


async def test_a_crashing_connect_listener_does_not_drop_the_connection():
    async def handler(ws, *args):
        await ws.send(_frame("/engine/status/load", "show_x"))
        await asyncio.sleep(0.5)

    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        c = EngineClient(f"ws://127.0.0.1:{port}")

        def boom():
            raise RuntimeError("x")

        c.on_connect(boom)
        await c.start()
        for _ in range(40):
            if c.load == "show_x":
                break
            await asyncio.sleep(0.025)
        assert c.load == "show_x"
        await c.stop()
