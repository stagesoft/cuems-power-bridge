# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Auto-play: send GO automatically after the bridge's OWN auto-load.

Fires only on auto-load success (never a manual load), waits (bounded) for the
engine to arm, never sends GO if the project is already running, and is
cancellable so a pending GO can't fire mid-shutdown.
"""

from typing import Any

from cuemspowerbridge import bridge as bridge_mod
from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config, _parse
from cuemspowerbridge.engine_state import UNKNOWN


class _FakeEngine:
    """Stand-in for EngineClient: settable status fields + a send_osc recorder.

    `arm_after_reads=N` makes `armed` report "no" for the first N-1 reads then
    "yes", simulating the load->arm delay deterministically (one read per
    auto-play poll iteration)."""

    def __init__(self, *, connected: bool = True, running: str = "no",
                 armed: str = "yes", load: str = "proj",
                 arm_after_reads: int = 0, send_ok: bool = True):
        self.connected = connected
        self.running = running
        self._armed = armed
        self.load = load
        self.arm_after_reads = arm_after_reads
        self.send_ok = send_ok
        self._armed_reads = 0
        self.sent: list[tuple[str, Any]] = []

    @property
    def armed(self) -> str:
        self._armed_reads += 1
        if self.arm_after_reads and self._armed_reads < self.arm_after_reads:
            return "no"
        return self._armed

    def is_known(self) -> bool:
        return self.connected and self.running != UNKNOWN

    def project_running(self) -> bool:
        return self.running == "yes"

    def project_loaded(self) -> bool:
        return self.load not in ("", UNKNOWN)

    async def send_osc(self, address: str, value: Any = None) -> bool:
        self.sent.append((address, value))
        return self.send_ok


def _bridge(*, auto_play: bool, engine: _FakeEngine | None = None) -> Bridge:
    cfg = Config()
    cfg.auto_play = auto_play
    b = Bridge(cfg)
    b.engine = engine if engine is not None else _FakeEngine()
    return b


def _fast(monkeypatch, *, polls: int = 10) -> None:
    """Shrink the arm-wait budget so tests don't sleep for real."""
    monkeypatch.setattr(bridge_mod, "_AUTO_PLAY_POLL_S", 0.0)
    monkeypatch.setattr(bridge_mod, "_AUTO_PLAY_ARM_POLLS", polls)


def _gos(engine: _FakeEngine) -> list[str]:
    return [addr for addr, _ in engine.sent if addr == "/engine/command/go"]


# ---- config flag ----

def test_auto_play_parses_and_defaults():
    cfg = Config()
    assert cfg.auto_play is False              # default off
    _parse("auto_play = true\n", cfg)
    assert cfg.auto_play is True


# ---- auto-play behaviour ----

async def test_go_sent_when_already_armed(monkeypatch):
    _fast(monkeypatch)
    eng = _FakeEngine(armed="yes")
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    await b._auto_play_task
    assert _gos(eng) == ["/engine/command/go"]


async def test_no_go_when_auto_play_disabled(monkeypatch):
    _fast(monkeypatch)
    eng = _FakeEngine(armed="yes")
    b = _bridge(auto_play=False, engine=eng)
    b._maybe_auto_play()
    assert b._auto_play_task is None           # never spawned
    assert _gos(eng) == []


async def test_waits_for_armed_then_sends_go(monkeypatch):
    _fast(monkeypatch)
    eng = _FakeEngine(armed="yes", arm_after_reads=3)   # "no","no","yes"
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    await b._auto_play_task
    assert _gos(eng) == ["/engine/command/go"]
    assert eng._armed_reads == 3               # waited, didn't fire early


async def test_no_go_when_already_running(monkeypatch):
    _fast(monkeypatch)
    eng = _FakeEngine(running="yes", armed="yes")
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    await b._auto_play_task
    assert _gos(eng) == []                      # never double-GO a running show


async def test_no_go_when_engine_never_arms(monkeypatch):
    _fast(monkeypatch, polls=3)
    eng = _FakeEngine(armed="no")               # stays unarmed
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    await b._auto_play_task
    assert _gos(eng) == []


async def test_go_send_failure_is_final_no_retry(monkeypatch):
    _fast(monkeypatch)
    eng = _FakeEngine(armed="yes", send_ok=False)
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    await b._auto_play_task                      # must not raise
    assert _gos(eng) == ["/engine/command/go"]   # tried exactly once


async def test_maybe_auto_play_dedups_in_flight(monkeypatch):
    _fast(monkeypatch, polls=1000)
    eng = _FakeEngine(armed="no")               # keeps the first task waiting
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    first = b._auto_play_task
    b._maybe_auto_play()                         # in-flight → no new task
    assert b._auto_play_task is first
    await b._cancel_auto_play_task()


async def test_cancel_unwinds_pending_auto_play(monkeypatch):
    # Safety-critical: the shutdown path cancels this so GO can't fire mid-off.
    _fast(monkeypatch, polls=1000)
    eng = _FakeEngine(armed="no")               # never arms → task stays waiting
    b = _bridge(auto_play=True, engine=eng)
    b._maybe_auto_play()
    t = b._auto_play_task
    assert t is not None and not t.done()
    await b._cancel_auto_play_task()
    assert t.done()
    assert _gos(eng) == []                       # cancelled before GO
    assert b._auto_play_task is None
