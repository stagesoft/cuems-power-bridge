# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""POST /shutdown: the six cases, end to end through the HTTP handler.

Feature 001, spec § "Shutdown target selection". The properties under test are
the ones that failed silently in the field:

* a refusal NEVER reaches the Shelly — mains is not armed on a cluster the
  bridge could not account for;
* the reachability poll runs whenever the sequence proceeds with targets;
* `force` overrides policy (a running project, the adoption filter) and never
  evidence (an unreadable map, an unaddressable target set).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config

FIXTURES = Path(__file__).parent / "fixtures" / "network_map"


class _FakeEngine:
    """Engine cache: known, loaded, not playing — a shutdown may proceed."""

    armed = "yes"
    load = "show"
    nextcue = None

    def is_known(self) -> bool:
        return True

    def project_running(self) -> bool:
        return False

    def project_loaded(self) -> bool:
        return True


class _RecordingShelly:
    def __init__(self) -> None:
        self.armed_for: int | None = None
        self.status_calls = 0

    async def get_status(self) -> dict:
        self.status_calls += 1
        return {"output": True}

    async def arm_timer(self, seconds: int) -> None:
        self.armed_for = seconds


def _bridge(case: str, *, dry_run: bool = True):
    """dry_run stays TRUE everywhere: with it False, step 10 of the sequence
    executes `sudo systemctl poweroff` for real. The state machine runs
    identically either way (that parity is a design goal), so "the sequence
    reached the irreversible part" is asserted as state, not as a Shelly call."""
    cfg = Config()
    cfg.network_map_path = str(FIXTURES / case / "network_map.xml")
    cfg.settings_xml_path = str(FIXTURES / case / "settings.xml")
    cfg.dry_run = dry_run
    cfg.shared_token = ""
    cfg.auto_load_project = ""
    b = Bridge(cfg)
    b.engine = _FakeEngine()
    b.shelly = _RecordingShelly()
    return b


def _post(b: Bridge, *, force: bool = False):
    query = "?force=1" if force else ""
    req = make_mocked_request("POST", f"/shutdown{query}")
    resp = asyncio.run(b.handle_shutdown(req))
    return resp, json.loads(resp.body.decode())


def _poweroff_calls(monkeypatch):
    """Record SSH fan-out targets without touching the network.

    Patched on `cluster_shutdown`, not `bridge`: feature 002 moved the two
    stage bodies there. The assertions below are unchanged — only where the
    seam sits moved."""
    seen: list[list] = []

    async def fake_poweroff_all(targets, dry_run=False):
        seen.append([t.host for t in targets])

    monkeypatch.setattr("cuemspowerbridge.cluster_shutdown.poweroff_all", fake_poweroff_all)
    return seen


def _no_poll(monkeypatch):
    """Record reachability polls; never actually ping anything."""
    polled: list[list[str]] = []

    class _Result:
        stuck_hosts: list[str] = []
        timed_out = False
        elapsed_s = 0.0

    async def fake_wait(hosts, **kw):
        polled.append(list(hosts))
        return _Result()

    monkeypatch.setattr("cuemspowerbridge.cluster_shutdown.wait_until_all_down", fake_wait)
    return polled


# --------------------------------------------------------------------------
# Case 1 — controller-only cluster: an answer, not an anomaly
# --------------------------------------------------------------------------


def test_case1_controller_only_proceeds(monkeypatch):
    b = _bridge("map-controller-only")
    _poweroff_calls(monkeypatch)
    polled = _no_poll(monkeypatch)
    resp, body = _post(b)
    assert resp.status == 200 and body["ok"] is True
    assert b._state in ("poweroff-issued", "done")  # reached the irreversible part
    assert polled == []                         # nothing to poll: Case 1
    assert b._node_selection["mode"] == "controller_only"
    assert b._node_selection["read_ok"] is True


# --------------------------------------------------------------------------
# Case 2 — normal cluster: adopted only
# --------------------------------------------------------------------------


def test_case2_targets_adopted_nodes_and_polls_them(monkeypatch):
    b = _bridge("map-two-adopted")
    seen = _poweroff_calls(monkeypatch)
    polled = _no_poll(monkeypatch)
    resp, body = _post(b)
    assert resp.status == 200 and body["ok"] is True
    assert seen == [["node01.local", "node02.local"]]
    assert polled == [["node01.local", "node02.local"]]        # FR-012
    assert b._state in ("poweroff-issued", "done")
    assert b._node_selection["mode"] == "adopted"
    assert b._node_selection["targeted"] == 2


def test_case2_skips_unadopted_and_says_so(monkeypatch):
    b = _bridge("map-mixed")
    seen = _poweroff_calls(monkeypatch)
    _no_poll(monkeypatch)
    resp, _ = _post(b)
    assert resp.status == 200
    assert seen == [["node01.local"]]
    assert b._node_selection["skipped"] == [{"node": "node02", "reason": "unadopted"}]


# --------------------------------------------------------------------------
# Case 3 — nothing adopted: refuse
# --------------------------------------------------------------------------


def test_case3_no_adopted_nodes_refuses_without_arming(monkeypatch):
    b = _bridge("map-none-adopted")
    seen = _poweroff_calls(monkeypatch)
    polled = _no_poll(monkeypatch)
    resp, body = _post(b)
    assert resp.status == 409
    assert body["reason"] == "no_adopted_nodes"
    assert body["found"] == 2 and sorted(body["skipped"]) == ["node01", "node02"]
    assert b.shelly.armed_for is None and b.shelly.status_calls == 0  # mains untouched
    assert b._state not in ("poweroff-issued", "done")
    assert seen == [] and polled == []
    assert b._state == "idle"


def test_case4_force_targets_unadopted_too(monkeypatch):
    b = _bridge("map-none-adopted")
    seen = _poweroff_calls(monkeypatch)
    polled = _no_poll(monkeypatch)
    resp, body = _post(b, force=True)
    assert resp.status == 200 and body["ok"] is True
    assert seen == [["node01.local", "node02.local"]]
    assert polled == [["node01.local", "node02.local"]]
    assert b._node_selection["mode"] == "forced_all"


# --------------------------------------------------------------------------
# Case 3b — nothing addressable: refuse, and force does NOT help
# --------------------------------------------------------------------------


def test_case3b_no_resolvable_nodes_refuses(monkeypatch):
    b = _bridge("map-unresolvable")
    seen = _poweroff_calls(monkeypatch)
    resp, body = _post(b)
    assert resp.status == 409
    assert body["reason"] == "no_resolvable_nodes"
    assert body["found"] == 2 and len(body["unresolvable"]) == 2
    assert b.shelly.armed_for is None and seen == []


def test_case3b_force_does_not_override_unaddressable(monkeypatch):
    """An unreachable node cannot be commanded off by asserting harder."""
    b = _bridge("map-unresolvable")
    seen = _poweroff_calls(monkeypatch)
    resp, body = _post(b, force=True)
    assert resp.status == 409
    assert body["reason"] == "no_resolvable_nodes"
    assert b.shelly.armed_for is None and seen == []


# --------------------------------------------------------------------------
# Partial resolution — proceeds, loudly
# --------------------------------------------------------------------------


def test_partial_resolution_proceeds_and_is_marked(monkeypatch, caplog):
    b = _bridge("map-partial-resolve")
    seen = _poweroff_calls(monkeypatch)
    polled = _no_poll(monkeypatch)
    with caplog.at_level("ERROR"):
        resp, _ = _post(b)
    assert resp.status == 200
    assert seen == [["node01.local"]]
    assert polled == [["node01.local"]]
    assert b._node_selection["partial"] is True
    assert "PARTIAL" in caplog.text or "cannot be commanded off" in caplog.text


# --------------------------------------------------------------------------
# Case 5 — unreadable topology: refuse, with and without force
# --------------------------------------------------------------------------


def test_case5_retired_vocabulary_refuses(monkeypatch):
    b = _bridge("map-pre007")
    seen = _poweroff_calls(monkeypatch)
    resp, body = _post(b)
    assert resp.status == 503
    assert body["reason"] == "topology_unreadable"
    assert body["detail"] == "network_map_retired_vocabulary"
    assert b.shelly.armed_for is None and seen == []
    assert b._node_selection["read_ok"] is False


def test_case5_force_does_not_override_unreadable(monkeypatch):
    """force overrides policy, never evidence."""
    b = _bridge("map-pre007")
    seen = _poweroff_calls(monkeypatch)
    resp, body = _post(b, force=True)
    assert resp.status == 503
    assert body["reason"] == "topology_unreadable"
    assert b.shelly.armed_for is None and seen == []


def test_case5_missing_settings_refuses_naming_the_package(monkeypatch, caplog):
    b = _bridge("map-no-settings")
    with caplog.at_level("ERROR"):
        resp, body = _post(b)
    assert resp.status == 503
    assert body["detail"] == "settings_xml_missing"
    assert "cuems-utils" in caplog.text


def test_case5_self_entry_missing_refuses(monkeypatch):
    b = _bridge("map-no-self")
    resp, body = _post(b)
    assert resp.status == 503
    assert body["detail"] == "self_entry_missing"
    assert b.shelly.armed_for is None


# --------------------------------------------------------------------------
# Case 1 and Case 5 must never look alike in /status
# --------------------------------------------------------------------------


def test_controller_only_and_unreadable_are_distinguishable(monkeypatch):
    _poweroff_calls(monkeypatch)
    _no_poll(monkeypatch)

    ok = _bridge("map-controller-only")
    _post(ok)
    bad = _bridge("map-pre007")
    _post(bad)

    a, z = ok._node_selection, bad._node_selection
    assert (a["mode"], a["read_ok"]) == ("controller_only", True)
    assert (z["mode"], z["read_ok"]) == ("none", False)
    assert a != z
