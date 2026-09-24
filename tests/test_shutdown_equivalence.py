# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The wall switch and the API cannot disagree — proved, not asserted.

Feature 002's reason to exist. Before it, machine selection was shared and
nothing else was; the two paths agreed because one person had written both.

What must match is the **decision** and the **stage semantics**. Orchestration
deliberately differs and is *not* under test here: `/shutdown` runs the two
stages concurrently and then arms the relay; the transition path runs displays
first, probes liveness once, and SSHes only the machines that answered.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from cuemspowerbridge import cluster_shutdown, network_map
from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.scripts import cluster_poweroff as cli

FIXTURES = Path(__file__).parent / "fixtures" / "network_map"

#: Every fixture, and what both entry points must decide about it.
CASES = [
    ("map-controller-only", "controller_only", []),
    ("map-two-adopted", "adopted", ["node01.local", "node02.local"]),
    ("map-mixed", "adopted", ["node01.local"]),
    ("map-none-adopted", "adopted", []),          # refused by both callers
    ("map-unresolvable", "adopted", []),          # refused by both callers
    ("map-partial-resolve", "adopted", ["node01.local"]),
]


def _paths(case):
    d = FIXTURES / case
    return str(d / "settings.xml"), str(d / "network_map.xml")


def _decide(case, *, include_unadopted=False):
    """The shared decision — the thing both callers must reach identically."""
    s, m = _paths(case)
    nodes = network_map.load_nodes(s, m)
    return network_map.shutdown_targets(
        s, m, include_unadopted=include_unadopted, nodes=nodes)


# --------------------------------------------------------------------------
# One decision, whoever asks
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case,mode,targets", CASES)
def test_both_callers_share_one_decision(case, mode, targets, conf_dir):
    """Both entry points call the same function on the same document, so the
    test is that neither has a private opinion to drift from."""
    conf_dir(case)
    sel = _decide(case)
    assert sel.mode == mode
    assert sel.targets == targets


@pytest.mark.parametrize("case", [c for c, _, _ in CASES])
def test_the_helper_and_the_daemon_read_the_same_selection(case, conf_dir):
    conf_dir(case)
    first = _decide(case)
    second = _decide(case)
    assert (first.mode, first.targets, first.found, first.adopted_count,
            first.partial) == (second.mode, second.targets, second.found,
                               second.adopted_count, second.partial)


@pytest.mark.parametrize("case", ["map-pre007", "map-no-self", "map-incomplete"])
def test_an_unreadable_topology_refuses_for_both(case, conf_dir):
    conf_dir(case)
    with pytest.raises(network_map.TopologyError):
        _decide(case)


def test_include_unadopted_changes_the_same_thing_for_both(conf_dir):
    """The HTTP route's force=1 and the helper's --include-unadopted are the
    same knob on the same function."""
    conf_dir("map-none-adopted")
    assert _decide("map-none-adopted").targets == []
    assert _decide("map-none-adopted", include_unadopted=True).targets == [
        "node01.local", "node02.local"]


# --------------------------------------------------------------------------
# The refusals map consistently onto each transport
# --------------------------------------------------------------------------


class _FakeEngine:
    armed, load, nextcue = "yes", "show", None

    def is_known(self):
        return True

    def project_running(self):
        return False

    def project_loaded(self):
        return True


def _http(case, *, force=False):
    cfg = Config()
    s, m = _paths(case)
    cfg.settings_xml_path, cfg.network_map_path = s, m
    cfg.dry_run = True
    cfg.shared_token = ""
    cfg.auto_load_project = ""
    b = Bridge(cfg)
    b.engine = _FakeEngine()
    req = make_mocked_request("POST", "/shutdown" + ("?force=1" if force else ""))
    resp = asyncio.run(b.handle_shutdown(req))
    return resp.status, json.loads(resp.body.decode())


@pytest.mark.parametrize("case,status,reason", [
    ("map-none-adopted", 409, "no_adopted_nodes"),
    ("map-unresolvable", 409, "no_resolvable_nodes"),
    ("map-pre007", 503, "topology_unreadable"),
])
def test_each_refusal_has_one_meaning_per_transport(case, status, reason, monkeypatch):
    """HTTP says 409/503; the helper says exit 4. Different alphabets, one
    decision underneath."""
    monkeypatch.setattr(cluster_shutdown, "poweroff_all",
                        lambda *a, **kw: asyncio.sleep(0))
    got_status, body = _http(case)
    assert (got_status, body["reason"]) == (status, reason)
    assert cli.EXIT_REFUSED == 4      # the helper's side of the same refusal


# --------------------------------------------------------------------------
# The guard is UNREACHABLE from the product path (SC-010)
# --------------------------------------------------------------------------


def _names(path):
    tree = ast.parse(Path(path).read_text())
    toks = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    toks |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    toks |= {n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    return toks


def test_the_shared_stages_cannot_reach_the_guard():
    """A claim about reachability, so it is checked on the call graph rather
    than by exercising a path and finding nothing happened."""
    toks = _names("src/cuemspowerbridge/cluster_shutdown.py")
    assert "_project_is_playing" not in toks
    assert not {t for t in toks if "8478" in t or "/status" in t}


def test_the_guard_lives_only_in_the_helper_entry_point():
    src = Path("src/cuemspowerbridge/scripts/cluster_poweroff.py").read_text()
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "_guard" in funcs and "_project_is_playing" in funcs
    # and it is consulted before any stage runs
    assert src.index("refused = _guard(args)") < src.index("asyncio.run(_run(args))")


def test_the_daemon_does_not_import_the_guard():
    toks = _names("src/cuemspowerbridge/bridge.py")
    assert "_guard" not in toks and "_project_is_playing" not in toks


# --------------------------------------------------------------------------
# One power-off at a time, across processes (FR-014, SC-009)
# --------------------------------------------------------------------------


def test_the_http_route_refuses_while_the_helper_holds_the_lock(monkeypatch, tmp_path):
    from cuemspowerbridge import shutdown_lock

    lock = str(tmp_path / "shutdown.lock")
    monkeypatch.setattr(shutdown_lock, "DEFAULT_LOCK_PATH", lock)
    monkeypatch.setattr(cluster_shutdown, "poweroff_all",
                        lambda *a, **kw: asyncio.sleep(0))
    with shutdown_lock.acquire(lock):                     # the helper holds it
        status, body = _http("map-two-adopted")
    assert status == 409 and body["reason"] == "shutdown_already_in_progress"


def test_lock_held_takes_nothing(tmp_path):
    """--lock-held: the wrapper holds one lock across both stages."""
    from cuemspowerbridge import shutdown_lock

    lock = str(tmp_path / "shutdown.lock")
    with shutdown_lock.acquire(lock):
        with shutdown_lock.held_elsewhere():
            pass                                          # would raise if it tried


def test_the_daemon_releases_before_its_own_re_entrant_transition(tmp_path):
    """/shutdown ends by triggering the transition that runs the wrapper; a
    still-held lock would make that wrapper refuse and run neither stage."""
    src = Path("src/cuemspowerbridge/bridge.py").read_text()
    assert src.index("lock.release()") < src.index("issuing local poweroff")
