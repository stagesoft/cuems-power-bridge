# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""OscRelay on real localhost UDP sockets (every port ephemeral).

Plan cuems-RELATIONS/Plans/2026-10-09-power-bridge-companion-cue-status-relay.md
§4.2-§4.6 / step S2: filter and byte identity, the hold/commit rules, resets,
link state + heartbeat, edge pulses (shape, timing, 1-99 = playing, when not
to fire), lanes and pacing, dump, target errors, supervision, close().
"""

import asyncio
import socket
import time

import pytest
from pythonosc.osc_message_builder import OscMessageBuilder

from cuemspowerbridge import osc_relay as R
from cuemspowerbridge.osc_parse import parse_osc_message
from cuemspowerbridge.osc_relay import OscRelay

S = "/engine/status/"
A = "8373a4df-fae1-4f22-b399-93844505e3e2"
B = "05efcae1-3adb-47d6-aa9c-34d221a127b6"
C = "c0ffee00-0000-4000-8000-000000000001"


def osc(address, *args):
    b = OscMessageBuilder(address)
    for a in args:
        b.add_arg(a)
    return b.build().dgram


class Sink(asyncio.DatagramProtocol):
    def __init__(self):
        self.got = []                     # (monotonic, address, args, raw)
        self.event = asyncio.Event()

    def datagram_received(self, data, addr):
        a, args = parse_osc_message(data)
        self.got.append((time.monotonic(), a, args, data))
        self.event.set()

    # helpers
    def addrs(self):
        return [g[1] for g in self.got]

    def values(self, address):
        return [g[2][0] if g[2] else None for g in self.got if g[1] == address]

    def edges(self):
        return [(g[1], g[2][0]) for g in self.got if g[1].startswith("/cuems/bridge/cue_")]

    def clear(self):
        self.got.clear()

    async def wait_for(self, pred, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise AssertionError(f"timeout; got {self.addrs()[-20:]}")
            self.event.clear()
            try:
                await asyncio.wait_for(self.event.wait(), 0.05)
            except asyncio.TimeoutError:
                pass


async def sink():
    loop = asyncio.get_running_loop()
    tr, proto = await loop.create_datagram_endpoint(Sink, local_addr=("127.0.0.1", 0))
    return tr, proto, tr.get_extra_info("sockname")[1]


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    # No edge-suppression window unless a test asks for it; slow heartbeat so
    # it does not pollute captures.
    monkeypatch.setattr(R, "EDGE_SUPPRESS_S", 0.0)
    monkeypatch.setattr(R, "HEARTBEAT_S", 3600.0)
    R._config._warned.clear()


async def relay_to_sink(**kw):
    tr, s, port = await sink()
    kw.setdefault("keys", R.parse_keys(R.DEFAULT_KEYS))
    r = OscRelay([("127.0.0.1", port)], **kw)
    await r.start()
    await s.wait_for(lambda: R.LINK_PATH in s.addrs())
    s.clear()
    return r, s, tr


def feed(r, *frames):
    for address, value in frames:
        r.on_frame(address, osc(address, value))


async def settle(r, s, quiet=0.15):
    """Wait until the relay has nothing queued and the sink went quiet."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not r._state and not r._heap:
            n = len(s.got)
            await asyncio.sleep(quiet)
            if len(s.got) == n and not r._state and not r._heap:
                return
        await asyncio.sleep(0.01)
    raise AssertionError("relay did not settle")


# --------------------------------------------------------------- filtering

async def test_filter_prefix_exact_and_byte_identity():
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    frames = [
        (S + "load", "show_x"),
        (S + f"cue/{A}", 0),
        (S + f"cue_enabled/{A}", 1),        # opt-in, not default
        (S + "running", "no"),
        (S + "running_x", "zzz"),           # exact "running" must not match
        (S + "timecode", 0),
        (S + "nextcue", A),
    ]
    raws = {a: osc(a, v) for a, v in frames}
    for a, v in frames:
        r.on_frame(a, raws[a])
    await settle(r, s)
    sent = {g[1]: g[3] for g in s.got if g[1].startswith(S)}
    assert set(sent) == {S + "load", S + f"cue/{A}", S + "running", S + "nextcue"}
    for a, raw in sent.items():
        assert raw == raws[a]                # byte for byte
    await r.close()
    tr.close()


async def test_opt_in_keys():
    r, s, tr = await relay_to_sink(keys=R.parse_keys("cue_enabled/,cue_name/,load"))
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 1), (S + f"cue_enabled/{A}", 0),
         (S + f"cue_name/{A}", "Intro"))
    await settle(r, s)
    assert {a for a in s.addrs() if a.startswith(S)} == {
        S + "load", S + f"cue_enabled/{A}", S + f"cue_name/{A}"}
    assert (R.STARTED_PREFIX + A) in s.addrs()      # edges don't need cue/ forwarded
    await r.close()
    tr.close()


# --------------------------------------------------------- hold / commit

async def test_rc1_order_holds_until_load_then_commits_in_order():
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    feed(r, (S + "armed", "no"), (S + "nextcue", ""), (S + "load", ""),
         (S + f"cue/{A}", 0), (S + f"cue/{B}", 0))
    await asyncio.sleep(0.1)
    assert S + f"cue/{A}" not in s.addrs()           # held
    assert r.stats()["held"] == 2
    feed(r, (S + "load", "show_x"))
    await settle(r, s)
    order = [a for a in s.addrs() if a.startswith(S + "cue/") or a == S + "load"]
    assert order[-3:] == [S + f"cue/{A}", S + f"cue/{B}", S + "load"]
    assert s.values(S + "load")[-1] == "show_x"
    assert s.edges() == []
    await r.close()
    tr.close()


async def test_released_order_holds_from_unknown_load():
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    # <= rc6-3: cue/*=0, cue_enabled, load X, names; first frames see `load`
    # as unknown.
    feed(r, (S + f"cue/{A}", 0), (S + f"cue_enabled/{A}", 1), (S + "load", "show_x"),
         (S + f"cue_name/{A}", "Intro"))
    await settle(r, s)
    assert s.addrs().index(S + f"cue/{A}") < s.addrs().index(S + "load")
    await r.close()
    tr.close()


async def test_unload_synthesises_resets_and_drops_per_cue_cache():
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    feed(r, (S + "load", "show_x"), (S + f"cue/{A}", 1), (S + f"cue/{B}", 100),
         (S + "running", "no"))
    await settle(r, s)
    s.clear()
    feed(r, (S + "load", ""))
    await settle(r, s)
    assert s.values(S + f"cue/{A}") == [0]
    assert s.values(S + f"cue/{B}") == [0]
    assert s.values(S + "load") == [""]
    assert s.edges() == []                          # a reset is no edge
    assert not any(a.startswith(S + "cue/") for a in r._cache)
    s.clear()
    r.dump()
    await settle(r, s)
    assert set(s.addrs()) == {R.LINK_PATH, S + "load", S + "running"}
    await r.close()
    tr.close()


async def test_no_resets_when_cue_is_not_forwarded():
    r, s, tr = await relay_to_sink(keys=R.parse_keys("load"))
    feed(r, (S + "load", "show_x"), (S + f"cue/{A}", 1), (S + "load", ""))
    await settle(r, s)
    assert S + f"cue/{A}" not in s.addrs()
    await r.close()
    tr.close()


async def test_disconnect_resets_link_zero_first_and_clears():
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    feed(r, (S + "load", "show_x"), (S + f"cue/{A}", 1), (S + "running", "yes"))
    await settle(r, s)
    s.clear()
    feed(r, (S + "load", ""), (S + f"cue/{B}", 0))   # B held
    r.on_engine_disconnect()
    await settle(r, s)
    assert s.got[0][1] == R.LINK_PATH and s.got[0][2] == [0]
    assert s.values(S + f"cue/{A}") == [0]
    assert S + f"cue/{B}" not in s.addrs()           # held frames discarded
    assert r.stats()["held"] == 0 and r.stats()["cached"] == 0
    # After a reconnect the view of load is unknown again: cue frames hold.
    r.on_engine_connect()
    feed(r, (S + f"cue/{C}", 0))
    await asyncio.sleep(0.05)
    assert r.stats()["held"] == 1
    await r.close()
    tr.close()


async def test_known_limit_reconnect_inside_rc1_load_window():
    """A reconnect between the head `load ""` and the reassignment of
    cue_status replays the OLD project's cues as held frames; they commit with
    the new burst (stale X-only values, no edge). Documented, not fixed."""
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    feed(r, (S + "load", "show_x"), (S + f"cue/{A}", 100))
    await settle(r, s)
    r.on_engine_disconnect()
    r.on_engine_connect()
    # Dump inside the window: load "" and X's cue_status still in place.
    feed(r, (S + "running", "no"), (S + "load", ""), (S + f"cue/{A}", 100))
    # The new project's burst, then its load.
    feed(r, (S + f"cue/{B}", 0), (S + "load", "show_y"))
    await settle(r, s)
    assert s.values(S + f"cue/{A}")[-1] == 100       # stale X-only value
    assert s.edges() == []
    await r.close()
    tr.close()


async def test_hold_cap(monkeypatch):
    monkeypatch.setattr(R, "HOLD_CAP", 3)
    r = OscRelay([("127.0.0.1", 9)])
    feed(r, *[(S + f"cue/{i}", 0) for i in range(5)])
    assert r.stats()["held"] == 3 and r.stats()["held_dropped"] == 2


# ------------------------------------------------------------------ edges

async def test_edge_pulse_shape_and_timing():
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    feed(r, (S + "load", "show_x"), (S + f"cue/{A}", 0))
    await settle(r, s)
    s.clear()
    feed(r, (S + f"cue/{A}", 1))
    await settle(r, s, quiet=0.3)
    p = [g for g in s.got if g[1] == R.STARTED_PREFIX + A]
    assert [g[2][0] for g in p] == [0, 1, 0]
    assert p[1][0] - p[0][0] >= R.PULSE_GAP_S - 0.002
    assert p[2][0] - p[1][0] >= R.PULSE_HOLD_S - 0.002
    assert r.stats()["edges_sent"] == 1
    s.clear()
    feed(r, (S + f"cue/{A}", 100))
    await settle(r, s, quiet=0.3)
    assert s.edges() == [(R.ENDED_PREFIX + A, 0), (R.ENDED_PREFIX + A, 1),
                         (R.ENDED_PREFIX + A, 0)]
    await r.close()
    tr.close()


async def test_progress_values_count_as_playing():
    r, s, tr = await relay_to_sink()
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 0),
         (S + f"cue/{A}", 50),       # started
         (S + f"cue/{A}", 1),        # still playing: nothing
         (S + f"cue/{A}", 100),      # ended
         (S + f"cue/{A}", 0),        # STOP reset: nothing
         (S + f"cue/{B}", 1),        # unseen cue → from 0: started
         (S + f"cue/{B}", 0))        # stopped while playing: nothing
    await settle(r, s, quiet=0.4)
    ones = [a for a, v in s.edges() if v == 1]
    assert sorted(ones) == sorted([R.STARTED_PREFIX + A, R.ENDED_PREFIX + A,
                                   R.STARTED_PREFIX + B])
    await r.close()
    tr.close()


async def test_no_edges_from_hold_commits():
    r, s, tr = await relay_to_sink()
    feed(r, (S + f"cue/{A}", 1), (S + f"cue/{B}", 100), (S + "load", "x"))
    await settle(r, s, quiet=0.3)
    assert s.edges() == []
    # ...but the committed value is the baseline for the next live change.
    feed(r, (S + f"cue/{A}", 100))
    await settle(r, s, quiet=0.3)
    assert [a for a, v in s.edges() if v == 1] == [R.ENDED_PREFIX + A]
    await r.close()
    tr.close()


async def test_no_edges_inside_the_connect_window(monkeypatch):
    monkeypatch.setattr(R, "EDGE_SUPPRESS_S", 0.3)
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 1))   # interleaved live frame
    await asyncio.sleep(0.35)
    feed(r, (S + f"cue/{A}", 100))
    await settle(r, s, quiet=0.3)
    assert [a for a, v in s.edges() if v == 1] == [R.ENDED_PREFIX + A]
    await r.close()
    tr.close()


async def test_edges_without_forwarding_cue():
    r, s, tr = await relay_to_sink(keys=R.parse_keys("running"))
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 0), (S + f"cue/{A}", 1))
    await settle(r, s, quiet=0.3)
    assert S + f"cue/{A}" not in s.addrs()
    assert [a for a, v in s.edges() if v == 1] == [R.STARTED_PREFIX + A]
    await r.close()
    tr.close()


async def test_pulses_on_one_path_are_serialised():
    r, s, tr = await relay_to_sink()
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 1), (S + f"cue/{A}", 0),
         (S + f"cue/{A}", 1))                          # two started edges
    await settle(r, s, quiet=0.5)
    vals = [v for a, v in s.edges() if a == R.STARTED_PREFIX + A]
    assert vals == [0, 1, 0, 0, 1, 0]                  # never interleaved
    await r.close()
    tr.close()


def test_event_cap_drops_whole_pending_pulses_oldest_first(monkeypatch):
    monkeypatch.setattr(R, "EVENT_CAP", 9)
    r = OscRelay([("127.0.0.1", 9)])
    for u in ("p1", "p2", "p3"):
        r._add_pulse(u)
    assert r._pending_sends == 9
    first = r._order[min(r._order)]
    first.stage = 2                       # its 1 is out: owes a trailing 0
    r._pending_sends -= 2
    r._add_pulse("p4")
    assert [p.path for p in r._order.values()] == ["p1", "p3", "p4"]
    assert r.events_dropped == 1
    r._add_pulse("p5")                    # drops p3 (p1 is protected)
    assert [p.path for p in r._order.values()] == ["p1", "p4", "p5"]
    assert r._pending_sends == 1 + 3 + 3


# ------------------------------------------------------ link and heartbeat

async def test_heartbeat(monkeypatch):
    monkeypatch.setattr(R, "HEARTBEAT_S", 0.1)
    r, s, tr = await relay_to_sink()
    r.on_engine_connect()
    await asyncio.sleep(0.55)
    vals = s.values(R.LINK_PATH)
    assert len(vals) >= 4 and vals[-1] == 1
    await r.close()
    tr.close()


async def test_link_zero_jumps_ahead_of_300_resets_at_rate_100():
    r, s, tr = await relay_to_sink(rate=100)
    r.on_engine_connect()
    uuids = [f"u{i:03d}" for i in range(300)]
    feed(r, *[(S + f"cue/{u}", 0) for u in uuids])
    feed(r, (S + "load", "x"))           # 301 frames queued, ~3 s at 100/s
    await asyncio.sleep(0.1)
    s.clear()
    t0 = time.monotonic()
    r.on_engine_disconnect()
    await s.wait_for(lambda: R.LINK_PATH in s.addrs(), timeout=0.5)
    assert s.got[0][1] == R.LINK_PATH and s.got[0][2] == [0]
    assert s.got[0][0] - t0 < 0.05
    assert r.stats()["queue_state"] > 250
    await r.close()
    tr.close()


async def test_state_frame_goes_out_while_only_future_pulses_wait():
    r, s, tr = await relay_to_sink()
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 1))
    await s.wait_for(lambda: (R.STARTED_PREFIX + A) in s.addrs())  # leading 0 out
    await asyncio.sleep(0.01)
    t0 = time.monotonic()
    feed(r, (S + "running", "yes"))
    await s.wait_for(lambda: (S + "running") in s.addrs(), timeout=0.5)
    t_run = [g[0] for g in s.got if g[1] == S + "running"][0]
    ones = [g[0] for g in s.got if g[1] == R.STARTED_PREFIX + A and g[2] == [1]]
    assert t_run - t0 < 0.02
    assert not ones or t_run < ones[0]
    await r.close()
    tr.close()


# ------------------------------------------------------ lanes and pacing

async def test_rate_paces_and_coalesces():
    r, s, tr = await relay_to_sink(rate=1000)
    feed(r, (S + "load", "x"))
    feed(r, *[(S + f"cue/u{i}", 0) for i in range(200)])
    feed(r, (S + "running", "yes"), (S + "running", "no"))
    t0 = time.monotonic()
    await settle(r, s)
    times = [g[0] for g in s.got]
    assert len([a for a in s.addrs() if a.startswith(S + "cue/")]) == 200
    assert s.values(S + "running") == ["no"]          # coalesced, latest wins
    assert times[-1] - t0 >= 0.18                      # ~200 at 1000/s
    await r.close()
    tr.close()


async def test_low_rate_is_really_slow():
    r, s, tr = await relay_to_sink(rate=100)
    feed(r, (S + "load", "x"))
    feed(r, *[(S + f"cue/u{i}", 0) for i in range(20)])
    t0 = time.monotonic()
    await s.wait_for(lambda: len(s.got) >= 21, timeout=2)
    assert s.got[-1][0] - t0 >= 0.18                   # 21 at 100/s ≈ 0.2 s
    await r.close()
    tr.close()


async def test_events_go_before_state():
    r, s, tr = await relay_to_sink(rate=100)
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 0))
    await settle(r, s)
    s.clear()
    feed(r, *[(S + f"cue/u{i}", 0) for i in range(50)])
    feed(r, (S + f"cue/{A}", 1))
    await s.wait_for(lambda: (R.STARTED_PREFIX + A) in s.addrs(), timeout=1)
    assert s.addrs().index(R.STARTED_PREFIX + A) <= 2
    await r.close()
    tr.close()


async def test_no_wakeups_when_idle(monkeypatch):
    r, s, tr = await relay_to_sink()
    await settle(r, s)

    class Clock:
        calls = 0

        def monotonic(self):
            Clock.calls += 1
            return time.monotonic()

        def time(self):
            return time.time()

    monkeypatch.setattr(R, "time", Clock())
    await asyncio.sleep(0.5)
    assert Clock.calls == 0
    monkeypatch.setattr(R, "time", time)
    await r.close()
    tr.close()


# ------------------------------------------------------------------- dump

async def test_dump_listener_targets_only_rate_limited_and_filtered():
    class Eng:
        cue_names = {A: "Intro"}

    tr_s, s, port = await sink()
    r = OscRelay([("127.0.0.1", port)], listen=("127.0.0.1", 0), engine=Eng())
    await r.start()
    lport = r._listener_tr.get_extra_info("sockname")[1]
    r.on_engine_connect()
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 0), (S + f"cue/{B}", 0),
         (S + "running", "no"))
    await settle(r, s)
    s.clear()

    asker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    asker.bind(("127.0.0.1", 0))
    asker.setblocking(False)
    asker.sendto(osc(R.DUMP_PATH), ("127.0.0.1", lport))
    await settle(r, s)
    assert set(s.addrs()) == {R.LINK_PATH, S + "load", S + f"cue/{A}", S + "running"}
    with pytest.raises(BlockingIOError):
        asker.recv(2048)                               # never reflected

    s.clear()
    asker.sendto(osc(R.DUMP_PATH), ("127.0.0.1", lport))          # < 1 s: ignored
    asker.sendto(osc("/cuems/bridge/other"), ("127.0.0.1", lport))
    asker.sendto(osc(R.DUMP_PATH, 1), ("127.0.0.1", lport))       # has an arg
    await asyncio.sleep(0.2)
    assert s.got == []
    st = r.stats()
    assert st["dumps_served"] == 1 and st["dumps_ignored"] == 3
    asker.close()
    await r.close()
    tr_s.close()


async def test_listen_port_in_use_disables_only_the_listener():
    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("127.0.0.1", 0))
    bport = busy.getsockname()[1]
    tr_s, s, port = await sink()
    r = OscRelay([("127.0.0.1", port)], listen=("127.0.0.1", bport))
    await r.start()
    st = r.stats()
    assert st["enabled"] and st["listen"] is None and "listener disabled" in st["last_error"]
    feed(r, (S + "running", "no"))
    await s.wait_for(lambda: S + "running" in s.addrs())
    busy.close()
    await r.close()
    tr_s.close()


async def test_refresh_resends_the_cache(monkeypatch):
    r, s, tr = await relay_to_sink(refresh_s=0.2)
    feed(r, (S + "running", "no"))
    await asyncio.sleep(0.5)
    assert len(s.values(S + "running")) >= 2
    await r.close()
    tr.close()


# --------------------------------------------------- errors and supervision

async def test_closed_port_target_errors_logged_once_then_recovers(monkeypatch, caplog):
    monkeypatch.setattr(R, "TARGET_RECOVER_S", 0.3)
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()                                       # nothing listens now
    r = OscRelay([("127.0.0.1", port)])
    await r.start()
    for i in range(10):
        feed(r, (S + "running", f"v{i}"))
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.1)
    t = r.stats()["targets"][f"127.0.0.1:{port}"]
    assert t["errors"] >= 1 and t["state"] == "error" and t["last_error"]
    assert sum("osc relay: target" in m for m in caplog.messages) == 1
    # The target appears; after TARGET_RECOVER_S without errors it is ok.
    loop = asyncio.get_running_loop()
    tr, s = await loop.create_datagram_endpoint(Sink, local_addr=("127.0.0.1", port))
    await asyncio.sleep(0.35)
    feed(r, (S + "running", "back"))
    await s.wait_for(lambda: S + "running" in s.addrs())
    assert r.stats()["targets"][f"127.0.0.1:{port}"]["state"] == "ok"
    await r.close()
    tr.close()


async def test_a_killed_drain_marks_degraded():
    r, s, tr = await relay_to_sink()
    drain = r._tasks[0]
    drain.cancel()
    await asyncio.sleep(0.05)
    st = r.stats()
    assert st["degraded"] is True and "osc-relay-drain" in st["last_error"]
    await r.close()
    tr.close()


async def test_close_link_first_trailing_zero_and_deadline():
    r, s, tr = await relay_to_sink(rate=1000)
    r.on_engine_connect()
    feed(r, (S + "load", "x"), (S + f"cue/{A}", 0))
    await settle(r, s)
    feed(r, (S + f"cue/{A}", 1))
    one_out = (R.STARTED_PREFIX + A, 1)
    await s.wait_for(lambda: one_out in [(g[1], g[2][0]) for g in s.got])  # its 1 is out
    feed(r, *[(S + f"cue/u{i}", 0) for i in range(3000)])  # 3 s of state
    r.on_engine_disconnect()
    s.clear()
    t0 = time.monotonic()
    await r.close()
    took = time.monotonic() - t0
    assert took < R.CLOSE_DEADLINE_S + 0.2
    await asyncio.sleep(0.05)
    assert s.got[0][1] == R.LINK_PATH and s.got[0][2] == [0]
    assert (R.STARTED_PREFIX + A, 0) in s.edges()[:3]
    tr.close()


async def test_stats_shape():
    r = OscRelay([("127.0.0.1", 9)])
    st = r.stats()
    for k in ("enabled", "degraded", "last_error", "unknown_keys", "rate",
              "targets", "queue_state", "queue_events", "events_dropped",
              "held", "cached", "dumps_served", "dumps_ignored", "edges_sent"):
        assert k in st
    assert set(st["targets"]["127.0.0.1:9"]) == {
        "sent", "errors", "last_error", "last_send", "state"}
