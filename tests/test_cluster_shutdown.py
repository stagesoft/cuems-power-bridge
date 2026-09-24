# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The two shared stage bodies, in isolation — no HTTP, no Bridge, no hardware.

This is the half of feature 002 that the suite could never reach before: 221
lines of it lived inside another package's shell script, executed only during a
real poweroff transition on a controller.
"""

from __future__ import annotations

import asyncio
import ast
from pathlib import Path

import pytest

from cuemspowerbridge import cluster_shutdown, network_map
from cuemspowerbridge.cluster_shutdown import (
    StageContext,
    exclude_self,
    run_display_stage,
    run_node_stage,
)


class _Cfg:
    projector_power_off_on_shutdown = True
    network_map_path = "/etc/cuems/network_map.xml"
    ssh_user = "cuems-admin"
    ssh_key = "/etc/cuems/power-bridge.key"
    poweroff_cmd = "sudo /sbin/poweroff"
    dry_run = False


class _Displays:
    def __init__(self, states, configured=True):
        self._states = states
        self.configured = configured
        self.powered_off = False

    async def status_all(self):
        return self._states

    async def power_off_all(self):
        self.powered_off = True

    def snapshot(self):
        return [{"name": d["name"], "power": "off"} for d in self._states]


def _ctx(cfg=None, displays=None, events=None):
    def progress(event, detail):
        if events is not None:
            events.append(event)

    return StageContext(cfg=cfg or _Cfg(), displays=displays or _Displays([]),
                        progress=progress)


def _sel(targets, *, mode="adopted", nodes=(), **kw):
    s = network_map.Selection(mode=mode, targets=list(targets), nodes=list(nodes))
    for k, v in kw.items():
        setattr(s, k, v)
    return s


# --------------------------------------------------------------------------
# Stage 1 — the three gates (FR-023)
# --------------------------------------------------------------------------


def test_disabled_gate_leaves_the_lamps_alone():
    """Venues keep configured fleets dark on purpose."""
    cfg = _Cfg()
    cfg.projector_power_off_on_shutdown = False
    d = _Displays([{"name": "p1", "power": "on"}])
    out = asyncio.run(run_display_stage(_ctx(cfg=cfg, displays=d)))
    assert out.detail == "disabled" and out.ok
    assert d.powered_off is False


def test_no_displays_configured_is_success_not_failure():
    d = _Displays([], configured=False)
    out = asyncio.run(run_display_stage(_ctx(displays=d)))
    assert out.detail == "not-configured" and out.ok


def test_a_fleet_already_off_is_a_fast_no_op():
    """The caller's own local poweroff re-enters this path; the second pass
    must not be a second full PJLink cycle."""
    d = _Displays([{"name": "p1", "power": "off"}, {"name": "p2", "power": "cooldown"}])
    out = asyncio.run(run_display_stage(_ctx(displays=d)))
    assert out.detail == "already-off"
    assert d.powered_off is False


def test_an_unreachable_fleet_is_skipped_loudly():
    d = _Displays([{"name": "p1", "power": "unknown"}, {"name": "p2", "power": "unknown"}])
    out = asyncio.run(run_display_stage(_ctx(displays=d)))
    assert out.detail == "unreachable"
    assert d.powered_off is False


def test_one_answering_device_still_commands_the_fleet():
    d = _Displays([{"name": "p1", "power": "unknown"}, {"name": "p2", "power": "on"}])
    out = asyncio.run(run_display_stage(_ctx(displays=d)))
    assert out.detail == "powered-off"
    assert d.powered_off is True


# --------------------------------------------------------------------------
# Self-exclusion (FR-019) — including the duplicate-entry case it exists for
# --------------------------------------------------------------------------


def _view(uuid, avahi, *, is_self=False, role_id=None):
    return network_map.NodeView(
        uuid=uuid, role=None, adopted=True, role_id=role_id or avahi.split(".")[0],
        alias=None, hostname=None, ip="10.0.0.9", is_self=is_self,
    )


def test_self_is_excluded_by_uuid(monkeypatch):
    monkeypatch.setattr(cluster_shutdown, "_local_addresses", lambda: set())
    monkeypatch.setattr(cluster_shutdown, "_resolve", lambda n: {"10.0.0.9"})
    nodes = [_view("u-self", "controller.local", is_self=True), _view("u-1", "node01.local")]
    kept, excluded = exclude_self(["controller.local", "node01.local"], nodes)
    assert kept == ["node01.local"]
    assert excluded[0][0] == "controller.local" and "uuid" in excluded[0][1]


def test_a_duplicate_entry_for_this_host_is_still_excluded(monkeypatch):
    """The reason the address and name tests are kept: a SECOND entry
    describing this host under a different uuid — the shape cuems-nodeconf's
    MAC-keyed merge once produced."""
    monkeypatch.setattr(cluster_shutdown, "_local_addresses", lambda: {"10.16.10.1"})
    monkeypatch.setattr(
        cluster_shutdown, "_resolve",
        lambda n: {"10.16.10.1"} if n == "ghost.local" else {"10.16.10.50"})
    nodes = [_view("u-ghost", "ghost.local"), _view("u-1", "node01.local")]
    kept, excluded = exclude_self(["ghost.local", "node01.local"], nodes)
    assert kept == ["node01.local"]
    assert "local address" in excluded[0][1]


def test_address_matching_is_exact_never_a_substring(monkeypatch):
    """10.16.10.1 is a substring of 10.16.10.10."""
    monkeypatch.setattr(cluster_shutdown, "_local_addresses", lambda: {"10.16.10.1"})
    monkeypatch.setattr(cluster_shutdown, "_resolve", lambda n: {"10.16.10.10"})
    kept, excluded = exclude_self(["node10.local"], [_view("u-10", "node10.local")])
    assert kept == ["node10.local"] and excluded == []


def test_loopback_resolution_is_excluded(monkeypatch):
    monkeypatch.setattr(cluster_shutdown, "_local_addresses", lambda: set())
    monkeypatch.setattr(cluster_shutdown, "_resolve", lambda n: {"127.0.0.1"})
    kept, excluded = exclude_self(["me.local"], [_view("u-me", "me.local")])
    assert kept == [] and "loopback" in excluded[0][1]


def test_an_unresolvable_target_is_kept(monkeypatch):
    """Not resolving is not evidence of being this host."""
    monkeypatch.setattr(cluster_shutdown, "_local_addresses", lambda: {"10.0.0.1"})
    monkeypatch.setattr(cluster_shutdown, "_resolve", lambda n: set())
    kept, _ = exclude_self(["node01.local"], [_view("u-1", "node01.local")])
    assert kept == ["node01.local"]


# --------------------------------------------------------------------------
# Stage 2 — the pre-pass, the policies, and the rehearsal split
# --------------------------------------------------------------------------


class _Reach:
    """Records reachability calls and replays scripted answers."""

    def __init__(self, *answers):
        self.calls: list[dict] = []
        self._answers = list(answers)

    async def __call__(self, hosts, **kw):
        self.calls.append({"hosts": list(hosts), **kw})
        stuck = self._answers.pop(0) if self._answers else []

        class _R:
            stuck_hosts = stuck
            timed_out = bool(stuck)
            elapsed_s = 0.0

        return _R()


def _no_ssh(monkeypatch):
    seen: list[list[str]] = []

    async def fake(targets, dry_run=False):
        seen.append([t.host for t in targets])

    monkeypatch.setattr(cluster_shutdown, "poweroff_all", fake)
    return seen


def _no_self(monkeypatch):
    monkeypatch.setattr(cluster_shutdown, "exclude_self", lambda t, n: (t, []))


def test_pre_pass_ssh_only_hosts_that_answered_alive(monkeypatch):
    _no_self(monkeypatch)
    seen = _no_ssh(monkeypatch)
    reach = _Reach(["node01.local"], [])          # probe: only node01 alive
    monkeypatch.setattr(cluster_shutdown, "wait_until_all_down", reach)
    out = asyncio.run(run_node_stage(
        _ctx(), _sel(["node01.local", "node02.local"]), pre_pass=True, max_wait_s=30))
    assert seen == [["node01.local"]]
    assert reach.calls[0]["max_wait_s"] == 0 and reach.calls[0]["confirm_failures"] == 1
    assert out.stuck_hosts == []


def test_pre_pass_finding_everything_down_skips_the_fan_out(monkeypatch):
    _no_self(monkeypatch)
    seen = _no_ssh(monkeypatch)
    monkeypatch.setattr(cluster_shutdown, "wait_until_all_down", _Reach([]))
    asyncio.run(run_node_stage(_ctx(), _sel(["node01.local"]), pre_pass=True, max_wait_s=30))
    assert seen == []


def test_without_pre_pass_every_target_is_sshed(monkeypatch):
    _no_self(monkeypatch)
    seen = _no_ssh(monkeypatch)
    reach = _Reach([])
    monkeypatch.setattr(cluster_shutdown, "wait_until_all_down", reach)
    asyncio.run(run_node_stage(
        _ctx(), _sel(["node01.local", "node02.local"]), pre_pass=False, max_wait_s=30))
    assert seen == [["node01.local", "node02.local"]]
    assert reach.calls[0]["max_wait_s"] == 30      # the real wait, no probe


def test_controller_only_says_so_and_polls_nothing(monkeypatch):
    _no_self(monkeypatch)
    seen = _no_ssh(monkeypatch)
    events: list[str] = []
    monkeypatch.setattr(cluster_shutdown, "wait_until_all_down", _Reach())
    asyncio.run(run_node_stage(
        _ctx(events=events), _sel([], mode="controller_only"), max_wait_s=30))
    assert seen == [] and "nothing-to-poll" in events


@pytest.mark.parametrize("skips,expected_calls", [(True, 0), (False, 1)])
def test_dry_run_wait_differs_per_caller_and_both_are_preserved(
        monkeypatch, skips, expected_calls):
    """The hook skips the wait under dry_run; /shutdown has always polled.
    Merging them silently changed one — this pins both."""
    _no_self(monkeypatch)
    _no_ssh(monkeypatch)
    cfg = _Cfg()
    cfg.dry_run = True
    reach = _Reach([])
    monkeypatch.setattr(cluster_shutdown, "wait_until_all_down", reach)
    asyncio.run(run_node_stage(_ctx(cfg=cfg), _sel(["node01.local"]),
                               dry_run_skips_wait=skips, max_wait_s=30))
    assert len(reach.calls) == expected_calls


def test_a_stuck_host_is_reported_not_hidden(monkeypatch):
    _no_self(monkeypatch)
    _no_ssh(monkeypatch)
    monkeypatch.setattr(cluster_shutdown, "wait_until_all_down", _Reach(["node02.local"]))
    out = asyncio.run(run_node_stage(
        _ctx(), _sel(["node01.local", "node02.local"]), max_wait_s=30))
    assert out.stuck_hosts == ["node02.local"] and out.timed_out is True


# --------------------------------------------------------------------------
# The carried-over defects (FR-017, FR-018) and the module's boundaries
# --------------------------------------------------------------------------


def _code_tokens(path="src/cuemspowerbridge/cluster_shutdown.py") -> set[str]:
    """Every name the module's CODE references — imports, attributes, literals.

    Parsed, not grepped: the module's docstring explains at length what it must
    not touch, and a grep would fail on the explanation itself.
    """
    tree = ast.parse(Path(path).read_text())
    toks: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            toks.add(node.id)
        elif isinstance(node, ast.Attribute):
            toks.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            toks.add(node.value)
        elif isinstance(node, ast.alias):
            toks.add(node.name.split(".")[-1])
            if node.asname:
                toks.add(node.asname)
        elif isinstance(node, ast.ImportFrom) and node.module:
            toks.add(node.module.split(".")[-1])
    return toks


def test_identity_comes_from_the_library_not_a_private_read():
    """FR-017/FR-018: the fifth copy of the node-identity model is gone, and
    with it the read that swallowed every exception."""
    toks = _code_tokens()
    assert not {t for t in toks if "settings.xml" in t}
    assert "ElementTree" not in toks and "etree" not in toks


def test_the_shared_module_cannot_arm_the_relay_or_power_this_host_off():
    """FR-001a — the hook must never do either, and /shutdown ends by
    triggering the transition that runs the hook."""
    toks = _code_tokens()
    for forbidden in ("shelly", "ShellyClient", "arm_timer", "get_status",
                      "controller_poweroff_cmd", "create_subprocess_exec"):
        assert forbidden not in toks, f"the shared stages must not reference {forbidden!r}"
    assert not {t for t in toks if "systemctl" in t}


def test_the_shared_module_never_consults_the_engine():
    """The running-show guard is the caller's business, requested explicitly
    (SC-010): a transaction cannot reach it even by accident."""
    toks = _code_tokens()
    for forbidden in ("engine", "EngineClient", "project_running", "project_loaded"):
        assert forbidden not in toks, f"the shared stages must not reference {forbidden!r}"
    assert not {t for t in toks if "8478" in t or "/status" in t}
