# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""cue_name cache in EngineClient and nextcue_name in /status (869fej3ma).

cuems-engine >= 869fedahu sends /engine/status/cue_name/<uuid> once per cue
AFTER /engine/status/load, and again in the late-join dump; nothing on
unload. Pinned here: the frames fill the table; a change of `load` empties
it (the burst that follows refills it); a same-value `load` keeps it;
disconnect empties it; /status resolves nextcue to its name, "" when unknown.
"""

import asyncio

from pythonosc.osc_message_builder import OscMessageBuilder

from cuemspowerbridge.engine_state import UNKNOWN, EngineClient


def _frame(address: str, value) -> bytes:
    b = OscMessageBuilder(address)
    b.add_arg(value)
    return b.build().dgram


class _Ws:
    """A websocket stand-in: iterates the queued frames, then ends."""

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


CUE_A = "8373a4df-fae1-4f22-b399-93844505e3e2"
CUE_B = "05efcae1-3adb-47d6-aa9c-34d221a127b6"


def test_names_fill_the_table_after_load():
    c = EngineClient("ws://unused")
    _consume(c, [
        _frame("/engine/status/load", "show_x"),
        _frame(f"/engine/status/cue_name/{CUE_A}", "R0 CTRL video (trigger)"),
        _frame(f"/engine/status/cue_name/{CUE_B}", "Canción ñ · 2ª"),
        _frame("/engine/status/nextcue", CUE_B),
    ])
    assert c.load == "show_x"
    assert c.cue_names == {CUE_A: "R0 CTRL video (trigger)", CUE_B: "Canción ñ · 2ª"}
    assert c.nextcue == CUE_B


def test_a_change_of_load_empties_the_table_before_the_new_burst():
    c = EngineClient("ws://unused")
    _consume(c, [
        _frame("/engine/status/load", "show_x"),
        _frame(f"/engine/status/cue_name/{CUE_A}", "Intro"),
        _frame("/engine/status/load", "show_y"),       # X -> Y, no unload
        _frame(f"/engine/status/cue_name/{CUE_A}", "Intro v2"),  # shared uuid
    ])
    assert c.cue_names == {CUE_A: "Intro v2"}


def test_unload_empties_the_table():
    c = EngineClient("ws://unused")
    _consume(c, [
        _frame("/engine/status/load", "show_x"),
        _frame(f"/engine/status/cue_name/{CUE_A}", "Intro"),
        _frame("/engine/status/load", ""),
    ])
    assert c.cue_names == {}
    assert c.load == ""


def test_same_load_value_keeps_the_table():
    """A repeated `load show_x` (e.g. a status re-broadcast) is not a new load."""
    c = EngineClient("ws://unused")
    _consume(c, [
        _frame("/engine/status/load", "show_x"),
        _frame(f"/engine/status/cue_name/{CUE_A}", "Intro"),
        _frame("/engine/status/load", "show_x"),
    ])
    assert c.cue_names == {CUE_A: "Intro"}


def test_none_name_becomes_empty_string():
    c = EngineClient("ws://unused")
    _consume(c, [_frame("/engine/status/load", "s"),
                 _frame(f"/engine/status/cue_name/{CUE_A}", "")])
    assert c.cue_names == {CUE_A: ""}


def test_listeners_still_see_every_key():
    c = EngineClient("ws://unused")
    seen = []
    c.on_status(lambda k, v: seen.append(k))
    _consume(c, [_frame("/engine/status/load", "s"), _frame(f"/engine/status/cue_name/{CUE_A}", "n")])
    assert seen == ["load", f"cue_name/{CUE_A}"]


def test_fresh_client_has_no_names():
    c = EngineClient("ws://unused")
    assert c.cue_names == {} and c.nextcue == UNKNOWN
