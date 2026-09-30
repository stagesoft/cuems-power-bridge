# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""POST /poweroff (displays only), the ON/OFF last-wins lock, the query-first
power-off, and the `?wait=1` result mode of /poweron and /poweroff."""

import asyncio
import json
import socket

from aiohttp.test_utils import make_mocked_request

import cuemspowerbridge.displays.pjlink as pjlink_mod
from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.displays.base import (
    DeviceDef, DisplayDriver, DisplayError, DisplayUnconfirmed, PowerState,
)
from cuemspowerbridge.displays.manager import DisplayManager


# ---------------------------------------------------------------- stubs ----

class _StubDisplays:
    """Stand-in for DisplayManager. Each power call can block on a gate, so
    the in-flight / cancel paths can be observed, and can make its
    cancellation slow (`cancel_delay_s`) to widen the ON/OFF interleave window
    the lock exists for. `active`/`max_active` count ON+OFF tasks running at
    once."""

    def __init__(self, *, configured: bool = True, gate: asyncio.Event | None = None,
                 cancel_delay_s: float = 0.0):
        self._configured = configured
        self.gate = gate
        self.cancel_delay_s = cancel_delay_s
        self.power_on_calls = 0
        self.power_off_calls = 0
        self.cancelled: list[str] = []
        self.active = 0
        self.max_active = 0

    @property
    def configured(self) -> bool:
        return self._configured

    async def _run(self, action: str) -> dict:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            return {"results": [{"name": "p1", "ok": True, "state": action}],
                    "succeeded": 1, "unconfirmed": 0, "failed": 0}
        except asyncio.CancelledError:
            self.cancelled.append(action)
            if self.cancel_delay_s:
                await asyncio.sleep(self.cancel_delay_s)
            raise
        finally:
            self.active -= 1

    async def power_on_all(self) -> dict:
        self.power_on_calls += 1
        return await self._run("on")

    async def power_off_where_needed(self) -> dict:
        self.power_off_calls += 1
        return await self._run("off")


class _Drv(DisplayDriver):
    """Driver stub for a REAL DisplayManager: fixed status, scripted outcome."""

    def __init__(self, name, *, status=PowerState.ON, exc=None, hang=False):
        super().__init__(DeviceDef(name=name, host=name))
        self.status = status
        self.exc = exc
        self.hang = hang
        self.calls: list[str] = []

    async def _act(self, what):
        self.calls.append(what)
        if self.hang:
            await asyncio.sleep(3600)
        if self.exc:
            raise self.exc

    async def power_on(self):
        await self._act("on")

    async def power_off(self):
        await self._act("off")

    async def power_status(self):
        self.calls.append("status")
        return self.status


def _manager(drivers) -> DisplayManager:
    m = DisplayManager([])
    m._drivers = list(drivers)
    m._states = [PowerState.UNKNOWN] * len(drivers)
    return m


def _bridge(displays=None, *, token: str = "", no_rate_limit: bool = False) -> Bridge:
    cfg = Config()
    cfg.shared_token = token
    b = Bridge(cfg)
    b.displays = displays if displays is not None else _StubDisplays()
    if no_rate_limit:
        b._rate.min_interval = 0.0
    return b


def _req(path: str, token: str | None = None):
    headers = {"X-Auth-Token": token} if token is not None else {}
    return make_mocked_request("POST", path, headers=headers)


def _body(resp) -> dict:
    return json.loads(resp.body)


# ------------------------------------------------------- basic contract ----

async def test_poweroff_ok_schedules_power_off():
    d = _StubDisplays()
    b = _bridge(d)
    resp = await b.handle_poweroff(_req("/poweroff"))
    assert resp.status == 200
    assert _body(resp) == {"ok": True}
    await asyncio.sleep(0)
    assert d.power_off_calls == 1
    assert d.power_on_calls == 0


async def test_poweroff_without_wait_returns_before_the_task_finishes():
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d)
    resp = await b.handle_poweroff(_req("/poweroff"))
    assert resp.status == 200                 # fire-and-forget: not held open
    assert not b._projector_off_task.done()
    gate.set()
    await b._projector_off_task


async def test_poweroff_bad_token():
    d = _StubDisplays()
    b = _bridge(d, token="secret")
    resp = await b.handle_poweroff(_req("/poweroff"))
    assert resp.status == 401
    assert _body(resp)["reason"] == "bad_token"
    resp = await b.handle_poweroff(_req("/poweroff", token="wrong"))
    assert resp.status == 401
    await asyncio.sleep(0)
    assert d.power_off_calls == 0
    ok = await b.handle_poweroff(_req("/poweroff", token="secret"))
    assert ok.status == 200


async def test_poweroff_no_displays():
    d = _StubDisplays(configured=False)
    b = _bridge(d)
    resp = await b.handle_poweroff(_req("/poweroff"))
    assert resp.status == 503
    assert _body(resp)["reason"] == "no_displays"
    assert d.power_off_calls == 0


async def test_poweroff_rate_limited_on_rapid_repeat():
    b = _bridge()
    r1 = await b.handle_poweroff(_req("/poweroff"))
    r2 = await b.handle_poweroff(_req("/poweroff"))
    assert r1.status == 200
    assert r2.status == 429
    assert _body(r2)["reason"] == "rate_limited"


async def test_poweroff_and_poweron_rate_limits_are_independent():
    b = _bridge()
    assert (await b.handle_poweroff(_req("/poweroff"))).status == 200
    assert (await b.handle_poweron(_req("/poweron"))).status == 200


async def test_route_registered():
    # Build the app the way start() does, without its network side effects.
    from aiohttp import web
    b = _bridge()
    app = web.Application()
    app.router.add_post("/poweroff", b.handle_poweroff)
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    assert ("POST", "/poweroff") in routes
    # And start() really wires it (source check: start() needs live sockets).
    import inspect
    assert 'add_post("/poweroff", self.handle_poweroff)' in inspect.getsource(Bridge.start)


# ------------------------------------------------------------- dedup ----

async def test_poweroff_dedups_while_in_flight(caplog):
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d, no_rate_limit=True)
    caplog.set_level("INFO", logger="cuemspowerbridge.bridge")
    assert (await b.handle_poweroff(_req("/poweroff"))).status == 200
    first = b._projector_off_task
    assert (await b.handle_poweroff(_req("/poweroff"))).status == 200
    assert b._projector_off_task is first      # joined, not re-spawned
    await asyncio.sleep(0)
    assert d.power_off_calls == 1
    assert any("already in flight" in r.getMessage() and r.levelname == "INFO"
               for r in caplog.records)
    gate.set()
    await first


async def test_poweron_http_dedup_logs_info(caplog):
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d, no_rate_limit=True)
    caplog.set_level("INFO", logger="cuemspowerbridge.bridge")
    b._spawn_projector_power_on("startup")      # e.g. the startup power-on
    assert (await b.handle_poweron(_req("/poweron"))).status == 200
    await asyncio.sleep(0)
    assert d.power_on_calls == 1
    assert any("/poweron: displays power-on already in flight" in r.getMessage()
               and r.levelname == "INFO" for r in caplog.records)
    gate.set()
    await b._projector_on_task


# ------------------------------------------------ cancel, last one wins ----

async def test_poweroff_cancels_in_flight_poweron(caplog):
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d)
    caplog.set_level("INFO", logger="cuemspowerbridge.bridge")
    b._spawn_projector_power_on("startup")
    on_task = b._projector_on_task
    await asyncio.sleep(0)
    assert (await b.handle_poweroff(_req("/poweroff"))).status == 200
    assert on_task.cancelled()
    assert d.cancelled == ["on"]
    assert b._projector_on_task is None
    await asyncio.sleep(0)
    assert d.power_off_calls == 1
    assert any("cancelling in-flight displays power-on" in r.getMessage()
               and r.levelname == "INFO" for r in caplog.records)
    gate.set()
    await b._projector_off_task


async def test_poweron_cancels_in_flight_poweroff(caplog):
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d)
    caplog.set_level("INFO", logger="cuemspowerbridge.bridge")
    assert (await b.handle_poweroff(_req("/poweroff"))).status == 200
    off_task = b._projector_off_task
    await asyncio.sleep(0)
    assert (await b.handle_poweron(_req("/poweron"))).status == 200
    assert off_task.cancelled()
    assert d.cancelled == ["off"]
    assert b._projector_off_task is None
    await asyncio.sleep(0)
    assert d.power_on_calls == 1
    assert any("cancelling in-flight displays power-off" in r.getMessage()
               and r.levelname == "INFO" for r in caplog.records)
    gate.set()
    await b._projector_on_task


async def test_interleaved_on_off_only_the_last_survives():
    # A slow cancellation of the in-flight ON opens the window F9 describes:
    # without the lock, /poweron would slip in while /poweroff awaits that
    # cancel, and both an ON and an OFF task would end up running.
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate, cancel_delay_s=0.05)
    b = _bridge(d, no_rate_limit=True)
    b._spawn_projector_power_on("startup")
    await asyncio.sleep(0)
    r_off, r_on = await asyncio.gather(
        b.handle_poweroff(_req("/poweroff")),
        b.handle_poweron(_req("/poweron")),
    )
    assert (r_off.status, r_on.status) == (200, 200)
    await asyncio.sleep(0)
    assert b._projector_off_task is None                 # OFF was superseded
    assert b._projector_on_task is not None and not b._projector_on_task.done()
    assert d.max_active == 1                             # never ON and OFF at once
    assert d.cancelled == ["on", "off"]
    gate.set()
    await b._projector_on_task


async def test_interleaved_off_on_off_last_off_wins():
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate, cancel_delay_s=0.02)
    b = _bridge(d, no_rate_limit=True)
    await asyncio.gather(
        b.handle_poweroff(_req("/poweroff")),
        b.handle_poweron(_req("/poweron")),
        b.handle_poweroff(_req("/poweroff")),
    )
    await asyncio.sleep(0)
    assert b._projector_on_task is None
    assert b._projector_off_task is not None and not b._projector_off_task.done()
    assert d.max_active == 1
    gate.set()
    await b._projector_off_task


# ------------------------------------------------------- query-first ----

async def test_power_off_where_needed_skips_off_and_cooling():
    on = _Drv("on", status=PowerState.ON)
    off = _Drv("off", status=PowerState.OFF)
    cool = _Drv("cool", status=PowerState.COOLDOWN)
    warm = _Drv("warm", status=PowerState.WARMUP)
    unk = _Drv("unk", status=PowerState.UNKNOWN)
    m = _manager([on, off, cool, warm, unk])
    summary = await m.power_off_where_needed()
    assert on.calls == ["status", "off"]
    assert off.calls == ["status"]            # no POWR 0
    assert cool.calls == ["status"]           # no POWR 0
    assert warm.calls == ["status", "off"]    # warming still gets POWR 0
    assert unk.calls == ["status", "off"]     # unanswered query still gets POWR 0
    by = {r["name"]: r for r in summary["results"]}
    assert [r["name"] for r in summary["results"]] == ["on", "off", "cool", "warm", "unk"]
    assert by["off"] == {"name": "off", "ok": True, "state": "already_off", "power": "off"}
    assert by["cool"] == {"name": "cool", "ok": True, "state": "already_off",
                          "power": "cooldown"}
    assert by["on"] == {"name": "on", "ok": True, "state": "off"}
    assert (summary["succeeded"], summary["unconfirmed"], summary["failed"]) == (5, 0, 0)
    # cache: skipped devices keep their queried state, commanded ones go OFF
    assert [s["power"] for s in m.snapshot()] == ["off", "off", "cooldown", "off", "off"]


async def test_poweroff_all_already_off_sends_nothing():
    a = _Drv("a", status=PowerState.OFF)
    b_ = _Drv("b", status=PowerState.COOLDOWN)
    b = _bridge(_manager([a, b_]))
    resp = await b.handle_poweroff(_req("/poweroff?wait=1"))
    assert resp.status == 200
    body = _body(resp)
    assert [r["state"] for r in body["results"]] == ["already_off", "already_off"]
    assert a.calls == ["status"] and b_.calls == ["status"]


async def test_power_on_all_and_off_all_return_results():
    # _run_shutdown ignores the return value; the shape is additive.
    a = _Drv("a")
    bad = _Drv("bad", exc=DisplayError("boom"))
    m = _manager([a, bad])
    s = await m.power_on_all()
    assert s["results"] == [{"name": "a", "ok": True, "state": "on"},
                            {"name": "bad", "ok": False, "error": "boom"}]
    assert (s["succeeded"], s["unconfirmed"], s["failed"]) == (1, 0, 1)
    s = await m.power_off_all()
    assert s["results"][0] == {"name": "a", "ok": True, "state": "off"}


# ------------------------------------------------------------ ?wait=1 ----

async def test_wait_200_all_ok():
    a, c = _Drv("a"), _Drv("c")
    b = _bridge(_manager([a, c]))
    resp = await b.handle_poweroff(_req("/poweroff?wait=1"))
    assert resp.status == 200
    body = _body(resp)
    assert body["ok"] is True and body["action"] == "off"
    assert body["results"] == [{"name": "a", "ok": True, "state": "off"},
                               {"name": "c", "ok": True, "state": "off"}]
    assert "reason" not in body


async def test_wait_207_partial():
    a = _Drv("a")
    bad = _Drv("bad", exc=DisplayError("no route"))
    b = _bridge(_manager([a, bad]))
    resp = await b.handle_poweron(_req("/poweron?wait=1"))
    assert resp.status == 207
    body = _body(resp)
    assert body["ok"] is False and body["reason"] == "partial"
    assert body["results"][1] == {"name": "bad", "ok": False, "error": "no route"}
    assert (body["succeeded"], body["failed"]) == (1, 1)


async def test_wait_207_unconfirmed():
    warm = _Drv("warm", exc=DisplayUnconfirmed("ERR3"))
    b = _bridge(_manager([warm]))
    resp = await b.handle_poweron(_req("/poweron?wait=1"))
    assert resp.status == 207
    body = _body(resp)
    assert body["reason"] == "unconfirmed"
    assert body["results"] == [{"name": "warm", "ok": False, "state": "unconfirmed",
                                "error": "ERR3"}]


async def test_wait_502_all_failed():
    x = _Drv("x", exc=DisplayError("down"))
    y = _Drv("y", exc=DisplayError("down"))
    b = _bridge(_manager([x, y]))
    resp = await b.handle_poweroff(_req("/poweroff?wait=1"))
    assert resp.status == 502
    body = _body(resp)
    assert body["reason"] == "all_failed"
    assert [r["ok"] for r in body["results"]] == [False, False]


async def test_wait_504_on_timeout_task_keeps_running():
    hung = _Drv("hung", hang=True)
    b = _bridge(_manager([hung]))
    b._power_wait_budget_s = lambda **kw: 0.05
    resp = await b.handle_poweron(_req("/poweron?wait=1"))
    assert resp.status == 504
    assert _body(resp)["reason"] == "timeout"
    task = b._projector_on_task
    assert task is not None and not task.done()   # not cancelled by the 504
    await b._cancel_projector_on_task()


async def test_wait_budget():
    b = _bridge()
    b.cfg.projector_command_timeout_s = 5
    assert b._power_wait_budget_s(query_first=False) == 20   # 3T+5, as shutdown
    assert b._power_wait_budget_s(query_first=True) == 25    # + the status query


async def test_wait_409_when_superseded():
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d)
    waiting = asyncio.create_task(b.handle_poweroff(_req("/poweroff?wait=1")))
    await asyncio.sleep(0.01)
    assert (await b.handle_poweron(_req("/poweron"))).status == 200
    resp = await waiting
    assert resp.status == 409
    assert _body(resp)["reason"] == "superseded"
    gate.set()
    await b._projector_on_task


async def test_wait_joins_in_flight_task_result():
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d, no_rate_limit=True)
    w1 = asyncio.create_task(b.handle_poweroff(_req("/poweroff?wait=1")))
    w2 = asyncio.create_task(b.handle_poweroff(_req("/poweroff?wait=1")))
    await asyncio.sleep(0.01)
    gate.set()
    r1, r2 = await asyncio.gather(w1, w2)
    assert (r1.status, r2.status) == (200, 200)
    assert d.power_off_calls == 1


async def test_poweron_without_wait_unchanged():
    # cuems-displays-on's contract: plain POST /poweron → 200 {"ok": true} at
    # once, whatever the projector is doing.
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d)
    resp = await b.handle_poweron(_req("/poweron"))
    assert resp.status == 200
    assert _body(resp) == {"ok": True}
    assert not b._projector_on_task.done()
    gate.set()
    await b._projector_on_task


# ------------------------------------------------------------- stop() ----

async def test_stop_cancels_off_task():
    gate = asyncio.Event()
    d = _StubDisplays(gate=gate)
    b = _bridge(d)
    assert (await b.handle_poweroff(_req("/poweroff"))).status == 200
    off_task = b._projector_off_task
    await asyncio.sleep(0)
    await asyncio.wait_for(b.stop(), timeout=5)
    assert off_task.cancelled()
    assert b._projector_off_task is None
    assert d.cancelled == ["off"]


# ---------------------------------- integration: fake PJLink server ----

async def _pjlink_server(power_state: str, set_reply: str):
    """Fake PJLink projector: answers `POWR ?` with `power_state` and any
    POWR set with `set_reply`. Records every command it receives."""
    seen: list[str] = []

    async def handler(reader, writer):
        writer.write(b"PJLINK 0\r")
        await writer.drain()
        cmd = (await reader.readuntil(b"\r")).decode().strip()
        seen.append(cmd)
        if cmd == "%1POWR ?":
            writer.write(f"%1POWR={power_state}\r".encode())
        else:
            writer.write(f"%1POWR={set_reply}\r".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], seen


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()          # nothing listens here now → connection refused
    return port


def _pjlink_bridge(ports: list[int]) -> Bridge:
    devs = [DeviceDef(name=f"p{i + 1}", host="127.0.0.1", driver="pjlink", port=p)
            for i, p in enumerate(ports)]
    return _bridge(DisplayManager(devs, timeout_s=1.0), no_rate_limit=True)


async def test_integration_err3_is_207_unconfirmed(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", (0, 0))
    # ON while the projector is cooling down: it answers ERR3 to every POWR 1.
    server, port, seen = await _pjlink_server(power_state="2", set_reply="ERR3")
    async with server:
        b = _pjlink_bridge([port])
        resp = await b.handle_poweron(_req("/poweron?wait=1"))
    assert resp.status == 207
    body = _body(resp)
    assert body["reason"] == "unconfirmed"
    assert body["results"][0]["state"] == "unconfirmed"
    assert body["results"][0]["ok"] is False
    assert seen == ["%1POWR 1"] * 3               # initial + 2 retries


async def test_integration_poweroff_query_first_and_err3(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", (0, 0))
    # p1 cooling → skipped, no POWR 0; p2 warming → POWR 0 answered ERR3.
    s1, port1, seen1 = await _pjlink_server(power_state="2", set_reply="OK")
    s2, port2, seen2 = await _pjlink_server(power_state="3", set_reply="ERR3")
    async with s1, s2:
        b = _pjlink_bridge([port1, port2])
        resp = await b.handle_poweroff(_req("/poweroff?wait=1"))
    assert resp.status == 207
    body = _body(resp)
    assert body["results"][0] == {"name": "p1", "ok": True, "state": "already_off",
                                  "power": "cooldown"}
    assert body["results"][1]["state"] == "unconfirmed"
    assert seen1 == ["%1POWR ?"]
    assert seen2 == ["%1POWR ?"] + ["%1POWR 0"] * 3


async def test_integration_poweroff_on_projector_200(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", (0, 0))
    server, port, seen = await _pjlink_server(power_state="1", set_reply="OK")
    async with server:
        b = _pjlink_bridge([port])
        resp = await b.handle_poweroff(_req("/poweroff?wait=1"))
    assert resp.status == 200
    assert _body(resp)["results"] == [{"name": "p1", "ok": True, "state": "off"}]
    assert seen == ["%1POWR ?", "%1POWR 0"]


async def test_integration_unreachable_is_502(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", (0, 0))
    b = _pjlink_bridge([_free_port(), _free_port()])
    for path in ("/poweroff?wait=1", "/poweron?wait=1"):
        resp = await b.handle_poweroff(_req(path)) if "off" in path \
            else await b.handle_poweron(_req(path))
        assert resp.status == 502
        body = _body(resp)
        assert body["reason"] == "all_failed"
        assert all(r["ok"] is False and "error" in r for r in body["results"])


async def test_integration_one_unreachable_is_207(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", (0, 0))
    server, port, _ = await _pjlink_server(power_state="1", set_reply="OK")
    async with server:
        b = _pjlink_bridge([port, _free_port()])
        resp = await b.handle_poweroff(_req("/poweroff?wait=1"))
    assert resp.status == 207
    body = _body(resp)
    assert body["reason"] == "partial"
    assert body["results"][0]["ok"] is True
    assert body["results"][1]["ok"] is False and "error" in body["results"][1]
