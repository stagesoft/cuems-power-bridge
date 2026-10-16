# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The relay inside Bridge: off by default (only osc_relay.enabled=false in
/status), hooks registered before the engine client starts, and a failure
in from_config / start / stats never costs /status its 200.
"""

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.osc_relay import OscRelay


def _cfg(**kw):
    c = Config()
    c.engine_ws_url = "ws://127.0.0.1:9"
    c.editor_ws_url = "ws://127.0.0.1:9"
    c.listen_host, c.listen_port = "127.0.0.1", 0
    c.projector_power_on_on_load = False
    if "osc_relay_targets" in kw:          # tests that want the relay: switch it on
        kw.setdefault("osc_relay_enabled", "true")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


async def _status(b: Bridge):
    app = web.Application()
    app.router.add_get("/status", b.handle_status)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/status")
        return resp.status, await resp.json()


async def test_relay_off_adds_only_enabled_false():
    b = Bridge(_cfg())
    assert b.osc_relay is None
    code, body = await _status(b)
    assert code == 200 and body["osc_relay"] == {"enabled": False}


async def test_hooks_are_registered_before_the_engine_starts(monkeypatch):
    b = Bridge(_cfg(osc_relay_targets="127.0.0.1:9", osc_relay_listen=""))
    seen = {}
    real_start = b.engine.start

    async def start():
        seen["frame"] = len(b.engine._frame_listeners)
        seen["connect"] = len(b.engine._connect_listeners)
        seen["disconnect"] = len(b.engine._disconnect_listeners)
        seen["relay_started"] = b.osc_relay._started
        await real_start()

    monkeypatch.setattr(b.engine, "start", start)
    runner = await b.start()
    try:
        assert seen == {"frame": 1, "connect": 1, "disconnect": 1, "relay_started": True}
        code, body = await _status(b)
        assert code == 200 and body["osc_relay"]["enabled"] is True
    finally:
        await b.stop()
        await runner.cleanup()


async def test_from_config_exception_leaves_status_200(monkeypatch):
    def boom(cfg, engine=None):
        raise RuntimeError("kaput")

    monkeypatch.setattr(OscRelay, "from_config", staticmethod(boom))
    b = Bridge(_cfg(osc_relay_targets="127.0.0.1:9"))
    code, body = await _status(b)
    assert code == 200
    assert body["osc_relay"]["enabled"] is False
    assert "kaput" in body["osc_relay"]["last_error"]


async def test_start_exception_disables_the_relay_not_the_bridge(monkeypatch):
    async def boom(self):
        self._started = True
        raise RuntimeError("no sockets")

    monkeypatch.setattr(OscRelay, "start", boom)
    b = Bridge(_cfg(osc_relay_targets="127.0.0.1:9", osc_relay_listen=""))
    runner = await b.start()
    try:
        assert b.engine._frame_listeners == []          # hooks not registered
        code, body = await _status(b)
        assert code == 200
        assert body["osc_relay"]["enabled"] is False
        assert "no sockets" in body["osc_relay"]["last_error"]
    finally:
        await b.stop()
        await runner.cleanup()


async def test_stats_exception_is_isolated(monkeypatch):
    b = Bridge(_cfg(osc_relay_targets="127.0.0.1:9"))

    def boom(self):
        raise KeyError("x")

    monkeypatch.setattr(OscRelay, "stats", boom)
    code, body = await _status(b)
    assert code == 200
    assert body["osc_relay"] == {"enabled": True, "error": "KeyError"}
    assert body["state"] == "idle"


async def test_stop_closes_the_relay_after_the_engine():
    b = Bridge(_cfg(osc_relay_targets="127.0.0.1:9", osc_relay_listen=""))
    order = []
    real_engine_stop, real_close = b.engine.stop, b.osc_relay.close

    async def engine_stop():
        order.append("engine")
        await real_engine_stop()

    async def close():
        order.append("relay")
        await real_close()

    b.engine.stop = engine_stop
    b.osc_relay.close = close
    runner = await b.start()
    await asyncio.sleep(0.05)
    await b.stop()
    await runner.cleanup()
    assert order == ["engine", "relay"]
