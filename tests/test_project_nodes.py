# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Which nodes a project needs, and are they on the bus (plan 869fghmxn
check 12, D6/D11/D13; S2).

The script fixture is a REDUCED copy of the Medina sala1 structure: cue →
outputs → *CueOutput → output_name, with its two node UUIDs and nothing else
(no media names — the real script stays out of this public repo)."""

import asyncio

from cuemspowerbridge import cluster_bus, network_map, project_nodes

CTRL = "9cf04acb-f784-470e-a7f8-c14d77003c2f"   # sala1's controller
NODE1 = "e1684ce4-d3f1-447d-bb01-c53af3a3d66d"
SPARE = "11111111-2222-3333-4444-555555555555"
GHOST = "0367f391-ebf4-48b2-9f26-000000000001"  # a template uuid, never adopted

SCRIPT = f"""<?xml version='1.0' encoding='utf-8'?>
<cms:CuemsProject xmlns:cms="https://stagelab.coop/cuems/">
  <CueList>
    <contents>
      <VideoCue><outputs>
        <VideoCueOutput><output_name>{CTRL}_0</output_name></VideoCueOutput>
        <VideoCueOutput><output_name>{NODE1}_1</output_name></VideoCueOutput>
      </outputs></VideoCue>
      <AudioCue enabled="False"><outputs>
        <AudioCueOutput><output_name>{NODE1}_0</output_name></AudioCueOutput>
      </outputs></AudioCue>
      <CueList><contents>
        <DmxCue><outputs>
          <DmxCueOutput><output_name>{GHOST}</output_name></DmxCueOutput>
        </outputs></DmxCue>
      </contents></CueList>
      <ActionCue>
        <ui_properties><output_name>{SPARE}_0</output_name></ui_properties>
        <outputs><ActionCueOutput><output_name>garbage</output_name></ActionCueOutput></outputs>
      </ActionCue>
    </contents>
  </CueList>
</cms:CuemsProject>
"""

SETTINGS = f"""<?xml version='1.0' encoding='utf-8'?>
<cms:Settings xmlns:cms="https://stagelab.coop/cuems/">
  <library_path>{{lib}}</library_path>
  <nng_hub_port>9093</nng_hub_port>
  <node><uuid>{CTRL.upper()}</uuid></node>
</cms:Settings>
"""


def _map(entries):
    rows = []
    for uuid, ntype, ip, adopted in entries:
        ip_el = f"<ip>{ip}</ip>" if ip else ""
        ad_el = f"<adopted>{adopted}</adopted>" if adopted is not None else ""
        rows.append(f"<node><uuid>{uuid}</uuid><node_type>{ntype}</node_type>"
                    f"<role_id>{uuid[:4]}</role_id>{ip_el}{ad_el}</node>")
    return ("<cms:CuemsNetworkMap xmlns:cms=\"https://stagelab.coop/cuems/\">"
            f"<CuemsNodeDict>{''.join(rows)}</CuemsNodeDict></cms:CuemsNetworkMap>")


def _files(tmp_path, map_entries):
    lib = tmp_path / "lib"
    (lib / "projects" / "show").mkdir(parents=True)
    (lib / "projects" / "show" / "script.xml").write_text(SCRIPT)
    settings = tmp_path / "settings.xml"
    settings.write_text(SETTINGS.format(lib=lib))
    nmap = tmp_path / "network_map.xml"
    nmap.write_text(_map(map_entries))
    return str(settings), str(nmap)


def test_project_scan_matches_the_engine_rule(tmp_path):
    _files(tmp_path, [])
    found = project_nodes.project_node_uuids(
        str(tmp_path / "lib" / "projects" / "show" / "script.xml"))
    # outputs only (ui_properties ignored), disabled cue counted, nested list
    # walked, DMX bare-uuid form, garbage dropped
    assert found == {CTRL, NODE1, GHOST}


def test_settings_reader(tmp_path):
    settings, _ = _files(tmp_path, [])
    st = project_nodes.read_settings(settings)
    assert st.controller_uuid == CTRL          # lower-cased
    assert st.library_path == str(tmp_path / "lib")


def test_adopted_parsing(tmp_path):
    p = tmp_path / "m.xml"
    p.write_text(_map([
        ("a" * 8 + "-0000-0000-0000-000000000001", "NodeType.slave", "10.0.0.1", "True"),
        ("a" * 8 + "-0000-0000-0000-000000000002", "NodeType.slave", "10.0.0.2", "1"),
        ("a" * 8 + "-0000-0000-0000-000000000003", "NodeType.slave", "10.0.0.3", "yes"),
        ("a" * 8 + "-0000-0000-0000-000000000004", "NodeType.slave", "10.0.0.4", "False"),
        ("a" * 8 + "-0000-0000-0000-000000000005", "NodeType.slave", "10.0.0.5", None),
    ]))
    assert [n.adopted for n in network_map.parse(str(p))] == [True, True, True, False, False]


def _classify(tmp_path, entries, peers):
    settings, nmap = _files(tmp_path, entries)
    project, nodes, ctrl, err = project_nodes.scan(settings, nmap, "show")
    assert err is None
    return project_nodes.classify(project, nodes, ctrl, peers)


BASE = [
    (CTRL, "NodeType.master", "10.0.0.10", "True"),   # nodeconf adopts masters
    (NODE1, "NodeType.slave", "10.0.0.11", "True"),
    (SPARE, "NodeType.slave", "10.0.0.12", "True"),   # adopted, unused
]


def test_controller_in_project_and_adopted_is_excluded(tmp_path):
    rep = _classify(tmp_path, BASE, {"10.0.0.11"})
    assert [n["uuid"] for n in rep.needed] == [NODE1]
    assert rep.needed[0]["bus"] == "on_bus"
    assert rep.not_in_cluster == [GHOST]
    assert rep.off_bus() == []


def test_needed_node_off_bus(tmp_path):
    rep = _classify(tmp_path, BASE, {"10.0.0.12"})   # only the spare is on
    assert [n["uuid"] for n in rep.off_bus()] == [NODE1]


def test_unused_node_off_bus_never_refuses(tmp_path):
    rep = _classify(tmp_path, BASE, {"10.0.0.11"})   # spare is off
    assert rep.off_bus() == []


def test_failed_ss_is_unknown_never_off(tmp_path):
    rep = _classify(tmp_path, BASE, None)
    assert rep.off_bus() == [] and [n["uuid"] for n in rep.unchecked()] == [NODE1]


def test_unclaimed_peer_makes_a_missing_node_unknown(tmp_path):
    # node01 is on the bus on an address the map doesn't know (stale <ip>)
    rep = _classify(tmp_path, BASE, {"10.0.0.99"})
    assert rep.off_bus() == [] and rep.unchecked()[0]["uuid"] == NODE1


def test_needed_node_without_ip_is_unknown(tmp_path):
    entries = [BASE[0], (NODE1, "NodeType.slave", None, "True")]
    rep = _classify(tmp_path, entries, set())
    assert rep.unchecked()[0]["uuid"] == NODE1


def test_not_adopted_project_node_is_not_in_cluster(tmp_path):
    entries = [BASE[0], (NODE1, "NodeType.slave", "10.0.0.11", "False")]
    rep = _classify(tmp_path, entries, set())
    assert rep.needed == [] and sorted(rep.not_in_cluster) == sorted([NODE1, GHOST])


def test_two_masters_only_settings_uuid_is_excluded(tmp_path):
    entries = BASE + [(NODE1.replace("e", "f"), "NodeType.master", "10.0.0.20", "True")]
    rep = _classify(tmp_path, entries, {"10.0.0.11"})
    assert [n["uuid"] for n in rep.needed] == [NODE1]


def test_scan_errors_are_reported(tmp_path):
    settings, nmap = _files(tmp_path, BASE)
    assert project_nodes.scan(str(tmp_path / "nope.xml"), nmap, "show")[3]
    assert project_nodes.scan(settings, nmap, "missing")[3]
    (tmp_path / "empty.xml").write_text("<a/>")
    assert project_nodes.scan(settings, str(tmp_path / "empty.xml"), "show")[3]


def test_hub_peers_or_none_on_ss_failure(monkeypatch):
    async def boom(*a, **kw):
        raise FileNotFoundError("ss")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    assert asyncio.run(cluster_bus.hub_peers_or_none(9093)) is None
