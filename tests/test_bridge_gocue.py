# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Cue triggering from the bridge: POST /setnextcue and POST /gocue.

/setnextcue selects a cue (engine setnextcue <uuid>); /gocue selects then fires
(setnextcue <uuid> + go). Cues are UUID-only. Mirrors the handle_go gating plus
a project-loaded gate, and surfaces nextcue in /status.
"""

import json
from typing import Any

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.engine_state import UNKNOWN

_UUID = "0123abcd-4567-89ab-cdef-0123456789ab"


class _FakeEngine:
    """Stand-in for EngineClient with a send_osc recorder and per-call result
    injection. `send_results` is consumed one entry per send_osc call (defaults
    to True forever once exhausted), so the partial-failure path (setnextcue ok,
    go fails) can be exercised deterministically."""

    def __init__(self, *, connected: bool = True, running: str = "no",
                 armed: str = "yes", load: str = "proj", nextcue: str = UNKNOWN,
                 send_results: list[bool] | None = None):
        self.connected = connected
        self.running = running
        self.armed = armed
        self.load = load
        self.nextcue = nextcue
        self._send_results = list(send_results) if send_results else []
        self.sent: list[tuple[str, Any]] = []

    def is_known(self) -> bool:
        return self.connected and self.running != UNKNOWN

    def project_running(self) -> bool:
        return self.running == "yes"

    def project_loaded(self) -> bool:
        return self.load not in ("", UNKNOWN)

    def fully_ready(self) -> bool:
        return self.project_loaded() and self.armed == "yes"

    async def send_osc(self, address: str, value: Any = None) -> bool:
        self.sent.append((address, value))
        if self._send_results:
            return self._send_results.pop(0)
        return True


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


def _bridge(engine: _FakeEngine | None = None, token: str = "") -> Bridge:
    cfg = Config()
    cfg.shared_token = token
    b = Bridge(cfg)
    b.engine = engine if engine is not None else _FakeEngine()
    return b


def _addrs(engine: _FakeEngine) -> list[str]:
    return [addr for addr, _ in engine.sent]


# ---------------- /setnextcue ----------------

async def test_setnextcue_happy_path():
    eng = _FakeEngine()
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(query={"cue": _UUID}))
    assert resp.status == 200
    assert json.loads(resp.text) == {"ok": True, "cue": _UUID}
    assert eng.sent == [("/engine/command/setnextcue", _UUID)]


async def test_setnextcue_json_body():
    eng = _FakeEngine()
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(json_body={"cue": _UUID}))
    assert resp.status == 200
    assert eng.sent == [("/engine/command/setnextcue", _UUID)]


async def test_setnextcue_does_not_require_armed():
    eng = _FakeEngine(armed="no")
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(query={"cue": _UUID}))
    assert resp.status == 200
    assert eng.sent == [("/engine/command/setnextcue", _UUID)]


async def test_setnextcue_missing_cue():
    b = _bridge()
    resp = await b.handle_setnextcue(_Req())
    assert resp.status == 400
    assert json.loads(resp.text)["reason"] == "missing_cue"


async def test_setnextcue_invalid_cue():
    eng = _FakeEngine()
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(query={"cue": "not a uuid"}))
    assert resp.status == 400
    assert json.loads(resp.text)["reason"] == "invalid_cue"
    assert eng.sent == []  # never reached the engine


async def test_setnextcue_no_project_loaded():
    eng = _FakeEngine(load="")
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(query={"cue": _UUID}))
    assert resp.status == 409
    assert json.loads(resp.text)["reason"] == "no_project_loaded"
    assert eng.sent == []


async def test_setnextcue_engine_unknown():
    eng = _FakeEngine(running=UNKNOWN)
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(query={"cue": _UUID}))
    assert resp.status == 503


async def test_setnextcue_bad_token():
    b = _bridge(token="secret")
    resp = await b.handle_setnextcue(_Req(query={"cue": _UUID}))
    assert resp.status == 401


async def test_setnextcue_engine_send_failed():
    eng = _FakeEngine(send_results=[False])
    b = _bridge(eng)
    resp = await b.handle_setnextcue(_Req(query={"cue": _UUID}))
    assert resp.status == 502


# ---------------- /gocue ----------------

async def test_gocue_happy_path_order():
    eng = _FakeEngine()
    b = _bridge(eng)
    resp = await b.handle_gocue(_Req(query={"cue": _UUID}))
    assert resp.status == 200
    assert json.loads(resp.text) == {"ok": True, "cue": _UUID}
    # setnextcue THEN go, in that order
    assert eng.sent == [
        ("/engine/command/setnextcue", _UUID),
        ("/engine/command/go", None),
    ]


async def test_gocue_not_armed():
    eng = _FakeEngine(armed="no")
    b = _bridge(eng)
    resp = await b.handle_gocue(_Req(query={"cue": _UUID}))
    assert resp.status == 409
    assert json.loads(resp.text)["reason"] == "not_armed"
    assert eng.sent == []  # nothing sent when not armed


async def test_gocue_allowed_mid_show():
    # running == "yes" must NOT block (parity with /go: advance, never stop)
    eng = _FakeEngine(running="yes", armed="yes")
    b = _bridge(eng)
    resp = await b.handle_gocue(_Req(query={"cue": _UUID}))
    assert resp.status == 200
    assert _addrs(eng) == [
        "/engine/command/setnextcue", "/engine/command/go",
    ]


async def test_gocue_partial_failure_setnextcue_ok_go_fails():
    # setnextcue succeeds, go fails -> 502, but go WAS attempted
    eng = _FakeEngine(send_results=[True, False])
    b = _bridge(eng)
    resp = await b.handle_gocue(_Req(query={"cue": _UUID}))
    assert resp.status == 502
    assert json.loads(resp.text)["reason"] == "engine_send_failed"
    assert _addrs(eng) == [
        "/engine/command/setnextcue", "/engine/command/go",
    ]


async def test_gocue_setnextcue_fails_skips_go():
    # first send fails -> go is never attempted
    eng = _FakeEngine(send_results=[False])
    b = _bridge(eng)
    resp = await b.handle_gocue(_Req(query={"cue": _UUID}))
    assert resp.status == 502
    assert _addrs(eng) == ["/engine/command/setnextcue"]


async def test_gocue_missing_cue():
    b = _bridge()
    resp = await b.handle_gocue(_Req())
    assert resp.status == 400
    assert json.loads(resp.text)["reason"] == "missing_cue"


async def test_gocue_no_project_loaded():
    eng = _FakeEngine(load="")
    b = _bridge(eng)
    resp = await b.handle_gocue(_Req(query={"cue": _UUID}))
    assert resp.status == 409
    assert json.loads(resp.text)["reason"] == "no_project_loaded"


# ---------------- /status nextcue ----------------

def test_status_payload_includes_nextcue():
    eng = _FakeEngine(nextcue=_UUID)
    b = _bridge(eng)
    payload = b._status_payload()
    assert payload["nextcue"] == _UUID
