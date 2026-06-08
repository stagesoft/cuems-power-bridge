# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Startup projector power-on + /poweron route (decoupled from project load)."""

import asyncio

from aiohttp.test_utils import make_mocked_request

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config


class _StubDisplays:
    """Stand-in for DisplayManager: records power_on_all calls; can block on a
    gate so we can observe the in-flight dedup guard."""

    def __init__(self, *, configured: bool = True, gate: asyncio.Event | None = None):
        self._configured = configured
        self.gate = gate
        self.power_on_calls = 0

    @property
    def configured(self) -> bool:
        return self._configured

    async def power_on_all(self) -> None:
        self.power_on_calls += 1
        if self.gate is not None:
            await self.gate.wait()


def _bridge(*, on_start: bool, on_load: bool = True, displays: _StubDisplays | None = None,
            token: str = "") -> Bridge:
    cfg = Config()
    cfg.projector_power_on_on_start = on_start
    cfg.projector_power_on_on_load = on_load
    cfg.shared_token = token
    b = Bridge(cfg)
    b.displays = displays if displays is not None else _StubDisplays()
    return b


# ---- config flag ----

def test_config_flag_parses_and_defaults():
    cfg = Config()
    assert cfg.projector_power_on_on_start is False          # default off
    from cuemspowerbridge.config import _parse
    _parse("projector_power_on_on_start = true\n", cfg)
    assert cfg.projector_power_on_on_start is True


# ---- startup power-on (_maybe_power_on_at_start) ----

async def test_startup_fires_when_enabled():
    d = _StubDisplays()
    b = _bridge(on_start=True, displays=d)
    b._maybe_power_on_at_start()
    await asyncio.sleep(0)            # let the spawned task run
    assert d.power_on_calls == 1


async def test_startup_fires_even_when_on_load_false():
    # Regression guard (review blocker #2): startup power-on must be gated
    # ONLY by projector_power_on_on_start, never nested under on_load.
    d = _StubDisplays()
    b = _bridge(on_start=True, on_load=False, displays=d)
    b._maybe_power_on_at_start()
    await asyncio.sleep(0)
    assert d.power_on_calls == 1


async def test_startup_noop_when_disabled():
    d = _StubDisplays()
    b = _bridge(on_start=False, displays=d)
    b._maybe_power_on_at_start()
    await asyncio.sleep(0)
    assert d.power_on_calls == 0


async def test_startup_noop_when_no_displays():
    d = _StubDisplays(configured=False)
    b = _bridge(on_start=True, displays=d)
    b._maybe_power_on_at_start()
    await asyncio.sleep(0)
    assert d.power_on_calls == 0


# ---- dedup guard ----

async def test_spawn_dedups_while_in_flight():
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(on_start=True, displays=d)
    b._spawn_projector_power_on()        # task 1 created (will block on gate)
    b._spawn_projector_power_on()        # in-flight → must be skipped
    await asyncio.sleep(0)               # task 1 runs power_on_all, then blocks
    assert d.power_on_calls == 1
    gate.set()
    await b._projector_on_task           # let it finish cleanly


# ---- /poweron route ----

async def test_poweron_ok_schedules_power_on():
    d = _StubDisplays()
    b = _bridge(on_start=False, displays=d)   # route works regardless of startup flag
    resp = await b.handle_poweron(make_mocked_request("POST", "/poweron"))
    assert resp.status == 200
    await asyncio.sleep(0)
    assert d.power_on_calls == 1


async def test_poweron_bad_token():
    b = _bridge(on_start=False, token="secret")
    resp = await b.handle_poweron(make_mocked_request("POST", "/poweron"))
    assert resp.status == 401


async def test_poweron_no_displays():
    d = _StubDisplays(configured=False)
    b = _bridge(on_start=False, displays=d)
    resp = await b.handle_poweron(make_mocked_request("POST", "/poweron"))
    assert resp.status == 503
    assert d.power_on_calls == 0


async def test_poweron_rate_limited_on_rapid_repeat():
    d = _StubDisplays()
    b = _bridge(on_start=False, displays=d)
    r1 = await b.handle_poweron(make_mocked_request("POST", "/poweron"))
    r2 = await b.handle_poweron(make_mocked_request("POST", "/poweron"))
    assert r1.status == 200
    assert r2.status == 429          # within the 200ms min-interval
