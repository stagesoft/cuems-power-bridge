# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The two vocabularies, and the fixture that must FAIL against the old reader.

This file used to pin the defect rather than the behaviour: its fixtures were
written in the retired `<node_type>` vocabulary, so the suite stayed green
BECAUSE it agreed with a parser that selected nothing from a converted
document. Feature 001 replaces it with two fixtures that discriminate.

The pre-migration run is recorded in
`specs/001-node-role-parser/evidence/pre-migration-parser-failure.txt`; it
cannot be reproduced here, because the reader it exercised is gone. What is
testable here is the other half of the guarantee: the retired document is now
REFUSED rather than silently answered.
"""

from __future__ import annotations

import pytest

from cuemspowerbridge.network_map import (
    TopologyError,
    TopologyErrorKind,
    readiness_peers,
    shutdown_targets,
)


def _paths(d):
    return str(d / "settings.xml"), str(d / "network_map.xml")


# --------------------------------------------------------------------------
# post-007 — the current vocabulary resolves correctly
# --------------------------------------------------------------------------


def test_post007_map_selects_its_adopted_nodes(conf_dir):
    """The regression this feature exists to fix: two adopted nodes in the
    current `<node_role>` vocabulary must select as TWO, not as zero."""
    s, m = _paths(conf_dir("map-two-adopted"))
    assert shutdown_targets(s, m).targets == ["node01.local", "node02.local"]


def test_post007_map_yields_readiness_peers_by_ip(conf_dir):
    """The readiness gate trusts <ip>, and used to come back empty too."""
    s, m = _paths(conf_dir("map-two-adopted"))
    assert readiness_peers(s, m).targets == [
        ("192.168.1.102", "node01"),
        ("192.168.1.103", "node02"),
    ]


def test_readiness_label_falls_back_to_uuid(conf_dir):
    """A node with no role_id/alias/hostname is still a bus peer (matched by
    IP); its label degrades to the uuid rather than disappearing."""
    s, m = _paths(conf_dir("map-unresolvable"))
    labels = [label for _ip, label in readiness_peers(s, m).targets]
    assert all(label.startswith("0367f391-") for label in labels)


# --------------------------------------------------------------------------
# pre-007 — the retired vocabulary is refused, loudly
# --------------------------------------------------------------------------


@pytest.mark.parametrize("selector", [shutdown_targets, readiness_peers])
def test_pre007_map_is_refused_not_silently_empty(conf_dir, selector):
    """The whole class of defect in one assertion: a document this bridge can
    no longer understand produces an ERROR, never an empty answer."""
    s, m = _paths(conf_dir("map-pre007"))
    with pytest.raises(TopologyError) as exc:
        selector(s, m)
    assert exc.value.kind is TopologyErrorKind.NETWORK_MAP_RETIRED_VOCABULARY


def test_pre007_refusal_names_the_conversion_tool(conf_dir):
    """An operator must be able to act on the message alone."""
    s, m = _paths(conf_dir("map-pre007"))
    with pytest.raises(TopologyError) as exc:
        shutdown_targets(s, m)
    assert "cuems-migrate-network-map" in str(exc.value)
