# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Replay engine frame sequences (tests/fixtures/relay/*.bin) through the relay.

The model_*.bin fixtures are hand-built from the engine code (both load
orders, a failed load, a reconnect inside an rc_1 load window); see
relayfix.py. Live captures (capture_*.bin, plan step R1) are picked up by the
invariant test automatically.
"""

import asyncio
import importlib.util
import time
from pathlib import Path

import pytest

from cuemspowerbridge import osc_relay as R
from cuemspowerbridge.osc_parse import parse_osc_message
from cuemspowerbridge.osc_relay import OscRelay

FIX = Path(__file__).resolve().parent / "fixtures" / "relay"
_spec = importlib.util.spec_from_file_location("relayfix", FIX / "relayfix.py")
relayfix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(relayfix)

S = "/engine/status/"
X1, SH, Y1 = relayfix.X1, relayfix.SH, relayfix.Y1
SCALE = 0.2      # replay 5x faster than recorded


class Sink(asyncio.DatagramProtocol):
    def __init__(self):
        self.got = []

    def datagram_received(self, data, addr):
        a, args = parse_osc_message(data)
        self.got.append((a, args[0] if args else None, data))


@pytest.fixture(autouse=True)
def scaled(monkeypatch):
    monkeypatch.setattr(R, "EDGE_SUPPRESS_S", 2.0 * SCALE)
    monkeypatch.setattr(R, "HEARTBEAT_S", 3600.0)


async def run(name, keys=R.DEFAULT_KEYS):
    recs = relayfix.read(FIX / name)
    loop = asyncio.get_running_loop()
    tr, s = await loop.create_datagram_endpoint(Sink, local_addr=("127.0.0.1", 0))
    port = tr.get_extra_info("sockname")[1]
    r = OscRelay([("127.0.0.1", port)], keys=R.parse_keys(keys))
    await r.start()
    t0 = loop.time()
    for kind, t_ms, data in recs:
        delay = t0 + t_ms * SCALE / 1000 - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        if kind == "C":
            r.on_engine_connect()
        elif kind == "D":
            r.on_engine_disconnect()
        else:
            parsed = parse_osc_message(data)
            if parsed and parsed[0].startswith(S):
                r.on_frame(parsed[0], data)
    deadline = time.monotonic() + 5
    while (r._state or r._heap) and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.3)
    await r.close()
    tr.close()
    return recs, s.got


def edges(got, value=1):
    return sorted(a for a, v, _ in got if a.startswith("/cuems/bridge/cue_") and v == value)


def last(got, address):
    vals = [v for a, v, _ in got if a == address]
    return vals[-1] if vals else None


def started(u):
    return R.STARTED_PREFIX + u


def ended(u):
    return R.ENDED_PREFIX + u


async def test_released_order_show():
    _, got = await run("model_released_show.bin")
    assert edges(got) == sorted([started(X1), ended(X1), started(SH), ended(SH)])
    assert [v for a, v, _ in got if a == S + "load"] == ["", "show_x", "show_y", ""]
    for u in (X1, SH, Y1):                        # all reset by the final unload
        assert last(got, S + f"cue/{u}") == 0


async def test_rc1_order_show():
    _, got = await run("model_rc1_show.bin")
    assert edges(got) == sorted([started(X1), ended(X1), started(SH), ended(SH)])
    loads = [v for a, v, _ in got if a == S + "load"]
    assert loads[-4:] == ["show_x", "", "show_y", ""]
    for u in (X1, SH, Y1):
        assert last(got, S + f"cue/{u}") == 0


async def test_rc1_failed_load_then_reconnect_holds_the_stale_dump():
    _, got = await run("model_rc1_failed_load.bin")
    assert edges(got) == sorted([started(X1), ended(X1)])
    # The reconnect dump replays show_x's cue_status under load "": held, then
    # discarded by the next load's head `load ""`. So after the drop, cue/X1
    # goes out exactly once: the new burst's 0, committed with the load.
    drop = max(i for i, (a, v, _) in enumerate(got) if a == R.LINK_PATH and v == 0
               and any(g[0] == S + "load" and g[1] == "show_x" for g in got[i:]))
    assert [v for a, v, _ in got[drop:] if a == S + f"cue/{X1}"] == [0]
    assert last(got, S + f"cue/{X1}") == 0


async def test_rc1_known_limit_keeps_the_stale_value_without_an_edge():
    _, got = await run("model_rc1_reconnect_in_load_window.bin")
    assert edges(got) == sorted([started(X1), ended(X1)])     # from the play only
    assert last(got, S + f"cue/{X1}") == 100                 # stale, documented
    assert last(got, S + "load") == "show_y"


async def test_live_rc1_capture_from_111():
    """R1 on .111: GO -> natural end -> STOP, then GO -> STOP mid-play. Two
    cue_started, ONE cue_ended (the natural end); no edge from either STOP."""
    cue = "b3c23544-81fa-4884-9380-8cb3bb77d982"
    _, got = await run("capture_rc1_111.bin")
    assert edges(got) == sorted([started(cue), started(cue), ended(cue)])
    assert [v for a, v, _ in got if a == S + "load"][-2:] == ["", "audio-test"]
    assert last(got, S + f"cue/{cue}") == 0


async def test_edges_without_forwarding_cue_status():
    _, got = await run("model_rc1_show.bin", keys="running,load")
    assert not any(a.startswith(S + "cue/") for a, _, _ in got)
    assert edges(got) == sorted([started(X1), ended(X1), started(SH), ended(SH)])


@pytest.mark.parametrize("name", sorted(p.name for p in FIX.glob("*.bin")))
async def test_invariants(name):
    recs, got = await run(name)
    raws = {d for k, _, d in recs if k == "F"}
    live_cues = {parse_osc_message(d)[0][len(S) + 4:]
                 for k, _, d in recs if k == "F"
                 and parse_osc_message(d)[0].startswith(S + "cue/")
                 and parse_osc_message(d)[1] and parse_osc_message(d)[1][0] != 0}
    for a, v, raw in got:
        if a.startswith(S) and not (a.startswith(S + "cue/") and v == 0):
            assert raw in raws, f"{a} not byte-identical to an input frame"
        if a.startswith("/cuems/bridge/cue_"):
            assert a.rsplit("/", 1)[1] in live_cues
    # Every edge path reads 0,1,0 repeated: never a 1 without its 0s.
    paths = {a for a, _, _ in got if a.startswith("/cuems/bridge/cue_")}
    for p in paths:
        vals = [v for a, v, _ in got if a == p]
        assert vals == [0, 1, 0] * (len(vals) // 3) and len(vals) % 3 == 0
