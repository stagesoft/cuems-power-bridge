# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The topology adapter: selections carry their reasons, failures are named.

Feature 001. Two properties are load-bearing and are asserted here rather than
assumed anywhere else:

* a selection that is empty or short always says WHY (Selection.skipped);
* a topology that cannot be read RAISES, and is never turned into an empty
  selection — the two outcomes are mutually exclusive.
"""

from __future__ import annotations

import logging

import pytest

from cuemspowerbridge.network_map import (
    NodeView,
    SkipReason,
    TopologyError,
    TopologyErrorKind,
    load_nodes,
    readiness_peers,
    shutdown_targets,
)


def _paths(d):
    return str(d / "settings.xml"), str(d / "network_map.xml")


# --------------------------------------------------------------------------
# Selection — the five shutdown shapes
# --------------------------------------------------------------------------


def test_controller_only_is_an_answer_not_an_anomaly(conf_dir):
    s, m = _paths(conf_dir("map-controller-only"))
    sel = shutdown_targets(s, m)
    assert sel.mode == "controller_only"
    assert sel.found == 0 and sel.targets == [] and sel.skipped == []


def test_adopted_nodes_are_targeted_by_name(conf_dir):
    s, m = _paths(conf_dir("map-two-adopted"))
    sel = shutdown_targets(s, m)
    assert sel.mode == "adopted"
    assert sel.targets == ["node01.local", "node02.local"]
    assert sel.found == 2 and sel.adopted_count == 2
    assert sel.skipped == [] and sel.partial is False


def test_unadopted_nodes_are_skipped_and_named(conf_dir):
    s, m = _paths(conf_dir("map-mixed"))
    sel = shutdown_targets(s, m)
    assert sel.targets == ["node01.local"]
    assert [(k.label, k.reason) for k in sel.skipped] == [("node02", SkipReason.UNADOPTED)]


def test_nothing_adopted_yields_no_targets_but_says_why(conf_dir):
    """Case 3: the caller refuses on this; the adapter never silently empties."""
    s, m = _paths(conf_dir("map-none-adopted"))
    sel = shutdown_targets(s, m)
    assert sel.targets == []
    assert sel.found == 2 and sel.adopted_count == 0
    assert {k.reason for k in sel.skipped} == {SkipReason.UNADOPTED}


def test_absent_adopted_element_counts_as_not_adopted(conf_dir):
    """map-none-adopted's second node carries NO <adopted> at all (research R3)."""
    s, m = _paths(conf_dir("map-none-adopted"))
    assert all(not v.adopted for v in load_nodes(s, m) if not v.is_self)


def test_force_targets_unadopted_nodes_too(conf_dir):
    s, m = _paths(conf_dir("map-none-adopted"))
    sel = shutdown_targets(s, m, include_unadopted=True)
    assert sel.mode == "forced_all"
    assert sel.targets == ["node01.local", "node02.local"]
    assert sel.skipped == []


def test_all_unresolvable_yields_no_targets_with_adopted_present(conf_dir):
    """Case 3b: adopted nodes exist, none addressable -> caller must refuse."""
    s, m = _paths(conf_dir("map-unresolvable"))
    sel = shutdown_targets(s, m)
    assert sel.targets == []
    assert sel.adopted_count == 2
    assert {k.reason for k in sel.skipped} == {SkipReason.UNRESOLVABLE}


def test_partial_resolution_proceeds_and_is_marked(conf_dir):
    s, m = _paths(conf_dir("map-partial-resolve"))
    sel = shutdown_targets(s, m)
    assert sel.targets == ["node01.local"]
    assert sel.partial is True
    assert len(sel.unresolvable) == 1


# --------------------------------------------------------------------------
# The two field-learned resolution policies
# --------------------------------------------------------------------------


def _view(**kw):
    base = dict(uuid="u-1", role=None, adopted=True, role_id=None, alias=None,
                hostname=None, ip="10.0.0.9", is_self=False)
    base.update(kw)
    return NodeView(**base)


@pytest.mark.parametrize(
    "kw,expected",
    [
        (dict(role_id="node01", alias="a", hostname="h"), "node01.local"),
        (dict(alias="a", hostname="h"), "a.local"),
        (dict(hostname="h"), "h.local"),
        (dict(), None),
    ],
)
def test_avahi_resolution_order_and_never_ip(kw, expected):
    """role_id -> alias -> hostname. NEVER <ip>, however tempting."""
    v = _view(**kw)
    assert v.avahi == expected
    if expected is None:
        assert v.ip  # an address exists and is still not used


def test_readiness_trusts_ip_and_skips_unadopted(conf_dir):
    s, m = _paths(conf_dir("map-mixed"))
    sel = readiness_peers(s, m)
    assert sel.targets == [("192.168.1.102", "node01")]
    assert [(k.label, k.reason) for k in sel.skipped] == [("node02", SkipReason.UNADOPTED)]


def test_readiness_skips_adopted_node_without_ip_defensively():
    """Defensive branch: the schema makes <ip> mandatory (research R11), so this
    state cannot come from a valid document — it is constructed directly."""
    nodes = [
        _view(uuid="u-self", is_self=True),
        _view(uuid="u-a", role_id="node01", ip=""),
        _view(uuid="u-b", role_id="node02", ip="192.168.1.9"),
    ]
    from cuemsutils.tools.NodeList import NodeRole

    nodes = [NodeView(**{**n.__dict__, "role": NodeRole.node}) for n in nodes[1:]]
    sel = readiness_peers("x/settings.xml", "x/network_map.xml", nodes=nodes)
    assert sel.targets == [("192.168.1.9", "node02")]
    assert [(k.label, k.reason) for k in sel.skipped] == [("node01", SkipReason.NO_IP)]
    assert sel.partial is True


def test_controller_is_never_its_own_target(conf_dir):
    s, m = _paths(conf_dir("map-two-adopted"))
    assert "controller.local" not in shutdown_targets(s, m).targets
    assert any(v.is_self for v in load_nodes(s, m))


# --------------------------------------------------------------------------
# TopologyError — every kind, and its message content
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case,kind",
    [
        ("map-pre007", TopologyErrorKind.NETWORK_MAP_RETIRED_VOCABULARY),
        ("map-no-self", TopologyErrorKind.SELF_ENTRY_MISSING),
        ("map-incomplete", TopologyErrorKind.NETWORK_MAP_INVALID),
        ("map-no-settings", TopologyErrorKind.SETTINGS_XML_MISSING),
    ],
)
def test_unreadable_topology_raises_classified(conf_dir, case, kind):
    s, m = _paths(conf_dir(case))
    with pytest.raises(TopologyError) as exc:
        shutdown_targets(s, m)
    assert exc.value.kind is kind


def test_missing_network_map_raises(conf_dir, tmp_path):
    """Also proves the adapter pins CUEMS_CONF_PATH: conf_dir points the
    environment at a complete fixture, and the read must still obey the
    configured paths (research R2)."""
    d = conf_dir("map-two-adopted")
    (tmp_path / "settings.xml").write_text((d / "settings.xml").read_text())
    with pytest.raises(TopologyError) as exc:
        shutdown_targets(str(tmp_path / "settings.xml"), str(tmp_path / "network_map.xml"))
    assert exc.value.kind is TopologyErrorKind.NETWORK_MAP_MISSING


def test_config_dir_mismatch_is_reported_not_papered_over():
    with pytest.raises(TopologyError) as exc:
        shutdown_targets("/etc/cuems/settings.xml", "/var/lib/other/network_map.xml")
    assert exc.value.kind is TopologyErrorKind.CONFIG_DIR_MISMATCH


@pytest.mark.parametrize(
    "case,must_contain",
    [
        ("map-pre007", ["cuems-migrate-network-map", "network_map.xml"]),
        ("map-no-settings", ["cuems-utils", "settings.xml"]),
        ("map-no-self", ["cuems-nodeconf", "network_map.xml"]),
        ("map-incomplete", ["node_role", "network_map.xml"]),
    ],
)
def test_error_messages_name_the_fault_and_a_remedy(conf_dir, case, must_contain):
    """An operator reading the journal of a machine that then powered off has
    only this line to work from (FR-026, SC-014)."""
    s, m = _paths(conf_dir(case))
    with pytest.raises(TopologyError) as exc:
        shutdown_targets(s, m)
    text = str(exc.value)
    for token in must_contain:
        assert token in text, f"{case}: message does not mention {token!r}: {text}"


def test_empty_node_list_raises_rather_than_looking_like_one_machine(conf_dir, tmp_path):
    """Case 1 must not be counterfeitable by a document that says nothing."""
    d = conf_dir("map-two-adopted")  # env points here; the adapter must not follow it
    (tmp_path / "settings.xml").write_text((d / "settings.xml").read_text())
    (tmp_path / "network_map.xml").write_text(
        "<?xml version='1.0' encoding='utf-8'?>\n"
        '<cms:CuemsNetworkMap xmlns:cms="https://stagelab.coop/cuems/">'
        "<node_list></node_list></cms:CuemsNetworkMap>\n"
    )
    with pytest.raises(TopologyError):
        shutdown_targets(str(tmp_path / "settings.xml"), str(tmp_path / "network_map.xml"))


# --------------------------------------------------------------------------
# The library's own logging must not flood the controller's journal
# --------------------------------------------------------------------------


def test_cuemsutils_logger_is_bounded():
    """cuemsutils DEBUG-logs every internal call; topology is read on the
    auto-load retry loop as well as at shutdown (research R2a)."""
    import cuemspowerbridge.network_map  # noqa: F401  (import applies the bound)

    assert logging.getLogger("cuemsutils").level >= logging.WARNING
