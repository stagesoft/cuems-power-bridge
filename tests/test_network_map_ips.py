# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""network_map.slave_ips(): (ip, role_id) of adopted slaves with an <ip>."""

from cuemspowerbridge.network_map import slave_ips

_XML = """\
<?xml version='1.0' encoding='utf-8'?>
<cms:CuemsNetworkMap xmlns:cms="https://stagelab.coop/cuems/">
<node_list>
  <node><uuid>u-ctrl</uuid><node_type>NodeType.master</node_type>
    <ip>169.254.9.204</ip><role_id>controller</role_id></node>
  <node><uuid>u-n1</uuid><node_type>NodeType.slave</node_type>
    <ip>169.254.13.233</ip><role_id>node01</role_id></node>
  <node><uuid>u-n2</uuid><node_type>NodeType.slave</node_type>
    <ip>169.254.13.234</ip><role_id>node02</role_id></node>
  <node><uuid>u-n3-noip</uuid><node_type>NodeType.slave</node_type>
    <role_id>node03</role_id></node>
</node_list>
</cms:CuemsNetworkMap>
"""


def test_slave_ips_returns_slaves_with_ip(tmp_path):
    p = tmp_path / "network_map.xml"
    p.write_text(_XML)
    result = slave_ips(str(p))
    # controller excluded (master); node03 excluded (no <ip>).
    assert result == [
        ("169.254.13.233", "node01"),
        ("169.254.13.234", "node02"),
    ]


def test_slave_ips_label_falls_back_to_uuid(tmp_path):
    xml = (
        "<root><node_list><node><uuid>only-uuid</uuid>"
        "<node_type>NodeType.slave</node_type><ip>10.0.0.9</ip></node>"
        "</node_list></root>"
    )
    p = tmp_path / "nm.xml"
    p.write_text(xml)
    assert slave_ips(str(p)) == [("10.0.0.9", "only-uuid")]


def test_slave_ips_missing_file(tmp_path):
    assert slave_ips(str(tmp_path / "nope.xml")) == []
