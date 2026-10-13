# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""EngineClient.fresh_arm and cluster_warning (plan 869fghmxn §4.3, D15; S2).

Frame orders are the engines' own, from their code:
- every load: `armed no` (_clear_playback_state), [rc_1: `load ""`],
  `load <name>`, [rc_1: `cluster_warning`], later `armed yes`;
- STOP: `running no`, `armed no`, then the nodes' re-arm `armed yes` — no
  `load` frame;
- reconnect dump: running, armed, load, nextcue, [rc_1: cluster_warning]."""

import json

from cuemspowerbridge.engine_state import EngineClient


def _client():
    c = EngineClient("ws://test")
    c.connected = True
    return c


def _feed(c, frames):
    for key, value in frames:
        c._apply_status(key, value)


def _cw(project, unreachable=(), load_id=1):
    return json.dumps({"load_id": load_id, "project": project,
                       "missing": [], "unreachable": list(unreachable)})


RC1_LOAD = [("armed", "no"), ("load", ""), ("load", "show"),
            ("cluster_warning", _cw("show")), ("armed", "yes")]
RELEASED_LOAD = [("armed", "no"), ("load", "show"), ("armed", "yes")]


def test_load_then_arm_is_fresh_on_both_orders():
    for frames in (RC1_LOAD, RELEASED_LOAD):
        c = _client()
        _feed(c, [("running", "no")] + frames)
        assert c.fresh_arm is True


def test_run_clears_fresh():
    c = _client()
    _feed(c, RELEASED_LOAD + [("running", "yes"), ("running", "no")])
    assert c.fresh_arm is False


def test_stop_rearm_is_not_fresh():
    c = _client()
    _feed(c, RELEASED_LOAD + [("running", "yes"), ("running", "no"),
                              ("armed", "no"), ("armed", "yes")])
    assert c.fresh_arm is False


def test_reconnect_dump_is_not_fresh():
    c = _client()
    _feed(c, [("running", "no"), ("armed", "yes"), ("load", "show"),
              ("nextcue", ""), ("cluster_warning", _cw("show"))])
    assert c.fresh_arm is False


def test_another_load_frame_clears_fresh():
    c = _client()
    _feed(c, RELEASED_LOAD + [("load", "show")])
    assert c.fresh_arm is False


def test_cluster_warning_parsed_and_reset_on_load():
    c = _client()
    _feed(c, [("load", "show"), ("cluster_warning", _cw("show", ["n1"]))])
    assert c.cluster_warning_seen is True
    assert c.cluster_warning["unreachable"] == ["n1"]
    _feed(c, [("load", "other")])
    assert c.cluster_warning is None and c.cluster_warning_seen is True


def test_released_engine_never_sees_cluster_warning():
    c = _client()
    _feed(c, RELEASED_LOAD)
    assert c.cluster_warning_seen is False and c.cluster_warning is None


def test_garbage_cluster_warning_is_none():
    c = _client()
    _feed(c, [("cluster_warning", "{not json")])
    assert c.cluster_warning is None and c.cluster_warning_seen is True
