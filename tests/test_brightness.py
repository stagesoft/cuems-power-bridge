# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Brightness: config parsing, DisplayManager fan-out/validation, /brightness."""

import json

import pytest

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.displays.base import DeviceDef
from cuemspowerbridge.displays.escvp import EscVpError
from cuemspowerbridge.displays.manager import (
    DisplayManager,
    parse_brightness_levels,
    parse_devices,
)


# ---------------- config parsing ----------------

def test_parse_brightness_levels():
    extras = {
        "brightness.standard.command": "BRIGHT 0",
        "brightness.full.command": "BRIGHT 50",
        "projector.1.host": "10.0.0.1",   # must be ignored here
    }
    assert parse_brightness_levels(extras) == {
        "standard": "BRIGHT 0", "full": "BRIGHT 50",
    }


def test_parse_devices_brightness_fields():
    extras = {
        "projector.1.host": "10.0.0.1",
        "projector.1.brightness": "true",
        "projector.1.escvp_password": "sekret",
        "projector.1.escvp_port": "3629",
        "projector.2.host": "10.0.0.2",   # no brightness opt-in
    }
    devs = parse_devices(extras)
    by_host = {d.host: d for d in devs}
    assert by_host["10.0.0.1"].brightness is True
    assert by_host["10.0.0.1"].escvp_password == "sekret"
    assert by_host["10.0.0.1"].escvp_port == 3629
    assert by_host["10.0.0.2"].brightness is False


# ---------------- manager validation ----------------

def _dev(host, **kw):
    return DeviceDef(name=host, host=host, **kw)


def test_brightness_optin_without_levels_raises():
    with pytest.raises(ValueError):
        DisplayManager([_dev("h", brightness=True)], brightness_levels={})


def test_empty_level_command_raises():
    with pytest.raises(ValueError):
        DisplayManager([_dev("h", brightness=True)],
                       brightness_levels={"full": ""})


def test_crlf_level_command_raises():
    with pytest.raises(ValueError):
        DisplayManager([_dev("h", brightness=True)],
                       brightness_levels={"full": "BRIGHT 0\rPWR OFF"})


def test_levels_without_optin_devices_is_allowed():
    # Levels defined but no device opted in → not an error; just no-op fleet.
    m = DisplayManager([_dev("h")], brightness_levels={"full": "BRIGHT 50"})
    assert m.brightness_configured is False
    assert m.brightness_count == 0


# ---------------- set_brightness fan-out ----------------

async def test_set_brightness_targets_only_flagged_and_leaves_states():
    # 3 devices, #1 and #3 opted in, #2 not. dry_run → no network.
    devs = [_dev("p1", brightness=True), _dev("p2"), _dev("p3", brightness=True)]
    m = DisplayManager(devs, brightness_levels={"full": "BRIGHT 50"},
                       dry_run=True)
    assert m.brightness_count == 2
    res = await m.set_brightness("full")
    assert res["applied"] == 2 and res["failed"] == 0
    assert {r["name"] for r in res["results"]} == {"p1", "p3"}
    assert m.last_brightness_level == "full"
    # The power-state cache (index-aligned with the POWER fleet) is untouched.
    assert m._states == m._states[:3]  # length matches power fleet
    assert len(m._states) == 3


async def test_set_brightness_partial(monkeypatch):
    devs = [_dev("p1", brightness=True), _dev("p2", brightness=True)]
    m = DisplayManager(devs, brightness_levels={"full": "BRIGHT 50"})

    async def ok(_cmd):
        return None

    async def boom(_cmd):
        raise EscVpError("standby ERR", retryable=False)

    monkeypatch.setattr(m._brightness_drivers[0], "send_command", ok)
    monkeypatch.setattr(m._brightness_drivers[1], "send_command", boom)
    res = await m.set_brightness("full")
    assert res["applied"] == 1 and res["failed"] == 1
    # Partial apply must NOT record a coherent fleet level.
    assert m.last_brightness_level is None


async def test_set_brightness_all_failed(monkeypatch):
    devs = [_dev("p1", brightness=True)]
    m = DisplayManager(devs, brightness_levels={"full": "BRIGHT 50"})

    async def boom(_cmd):
        raise EscVpError("nope")

    monkeypatch.setattr(m._brightness_drivers[0], "send_command", boom)
    res = await m.set_brightness("full")
    assert res["applied"] == 0 and res["failed"] == 1


# ---------------- /brightness endpoint ----------------

class _StubBright:
    def __init__(self, *, configured=True, levels=("full", "standard"), result=None):
        self._configured = configured
        self._levels = sorted(levels)
        self._result = result or {"results": [{"name": "P1", "ok": True}],
                                  "applied": 1, "failed": 0}
        self.calls = []

    @property
    def brightness_configured(self):
        return self._configured

    @property
    def brightness_levels(self):
        return self._levels

    async def set_brightness(self, level):
        self.calls.append(level)
        return self._result


class _Req:
    """Minimal duck-typed request: headers, query, json()."""

    def __init__(self, headers=None, query=None, json_body=None):
        self.headers = headers or {}
        self.query = query or {}
        self._json = json_body

    async def json(self):
        if self._json is None:
            raise ValueError("no body")
        return self._json


def _bridge(displays, token=""):
    cfg = Config()
    cfg.shared_token = token
    b = Bridge(cfg)
    b.displays = displays
    return b


async def test_brightness_bad_token():
    b = _bridge(_StubBright(), token="secret")
    resp = await b.handle_brightness(_Req(query={"level": "full"}))
    assert resp.status == 401


async def test_brightness_no_devices():
    b = _bridge(_StubBright(configured=False))
    resp = await b.handle_brightness(_Req(query={"level": "full"}))
    assert resp.status == 503


async def test_brightness_missing_level():
    b = _bridge(_StubBright())
    resp = await b.handle_brightness(_Req())
    assert resp.status == 400
    assert json.loads(resp.text)["reason"] == "missing_level"


async def test_brightness_unknown_level():
    b = _bridge(_StubBright())
    resp = await b.handle_brightness(_Req(query={"level": "ludicrous"}))
    assert resp.status == 400
    body = json.loads(resp.text)
    assert body["reason"] == "unknown_level"
    assert body["levels"] == ["full", "standard"]


async def test_brightness_query_string_ok():
    d = _StubBright()
    b = _bridge(d)
    resp = await b.handle_brightness(_Req(query={"level": "full"}))
    assert resp.status == 200
    assert d.calls == ["full"]
    assert json.loads(resp.text)["ok"] is True


async def test_brightness_json_body_ok():
    d = _StubBright()
    b = _bridge(d)
    resp = await b.handle_brightness(_Req(json_body={"level": "standard"}))
    assert resp.status == 200
    assert d.calls == ["standard"]


async def test_brightness_partial_207():
    d = _StubBright(result={"results": [{"name": "P1", "ok": True},
                                        {"name": "P2", "ok": False, "error": "ERR"}],
                            "applied": 1, "failed": 1})
    b = _bridge(d)
    resp = await b.handle_brightness(_Req(query={"level": "full"}))
    assert resp.status == 207
    assert json.loads(resp.text)["reason"] == "partial"


async def test_brightness_all_failed_502():
    d = _StubBright(result={"results": [{"name": "P1", "ok": False, "error": "ERR"}],
                            "applied": 0, "failed": 1})
    b = _bridge(d)
    resp = await b.handle_brightness(_Req(query={"level": "full"}))
    assert resp.status == 502
    assert json.loads(resp.text)["reason"] == "all_failed"
