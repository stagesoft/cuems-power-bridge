# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Config: auto-load node-readiness settings — defaults, CSV, validate,
nng_hub_port resolution from settings.xml."""

import pytest

from cuemspowerbridge.config import Config, _parse, _read_nng_hub_port, load


def test_defaults():
    cfg = Config()
    assert cfg.auto_load_wait_nodes is True
    assert cfg.auto_load_node_ids == ""
    assert cfg.auto_load_node_timeout_s == 420
    assert cfg.auto_load_node_settle_s == 10
    assert cfg.auto_load_armed_timeout_s == 150
    assert cfg.auto_load_max_attempts == 5
    assert cfg.nng_hub_port == 9093
    cfg.validate()  # defaults must validate


def test_node_ids_csv_strip():
    cfg = Config()
    _parse("auto_load_node_ids = node01, node02 ,, node03\n", cfg)
    assert cfg.node_ids_list() == ["node01", "node02", "node03"]


def test_node_ids_empty_is_empty_list():
    assert Config().node_ids_list() == []


def test_wait_nodes_bool_coercion():
    cfg = Config()
    _parse("auto_load_wait_nodes = false\n", cfg)
    assert cfg.auto_load_wait_nodes is False


@pytest.mark.parametrize("key,val", [
    ("auto_load_node_timeout_s", -1),
    ("auto_load_node_settle_s", -5),
    ("auto_load_armed_timeout_s", 10),   # < 30
    ("auto_load_max_attempts", 0),       # < 1
])
def test_validate_rejects_bad(key, val):
    cfg = Config()
    setattr(cfg, key, val)
    with pytest.raises(ValueError):
        cfg.validate()


def test_validate_accepts_armed_timeout_floor():
    cfg = Config()
    cfg.auto_load_armed_timeout_s = 30
    cfg.validate()  # exactly the floor is OK


def test_read_nng_hub_port_from_settings(tmp_path):
    xml = tmp_path / "settings.xml"
    xml.write_text(
        '<?xml version="1.0"?>'
        '<cms:CuemsSettings xmlns:cms="https://stagelab.coop/cuems/">'
        '<Settings><node><nng_hub_port>9999</nng_hub_port></node></Settings>'
        '</cms:CuemsSettings>'
    )
    assert _read_nng_hub_port(str(xml)) == 9999


def test_read_nng_hub_port_missing_file(tmp_path):
    assert _read_nng_hub_port(str(tmp_path / "nope.xml")) is None


def test_read_nng_hub_port_nonnumeric(tmp_path):
    xml = tmp_path / "settings.xml"
    xml.write_text("<root><nng_hub_port>abc</nng_hub_port></root>")
    assert _read_nng_hub_port(str(xml)) is None


def test_load_resolves_hub_port(tmp_path):
    settings = tmp_path / "settings.xml"
    settings.write_text("<root><nng_hub_port>9191</nng_hub_port></root>")
    conf = tmp_path / "power-bridge.conf"
    conf.write_text(f"settings_xml_path = {settings}\n")
    cfg = load(str(conf))
    assert cfg.nng_hub_port == 9191


def test_load_falls_back_when_settings_absent(tmp_path):
    conf = tmp_path / "power-bridge.conf"
    conf.write_text(f"settings_xml_path = {tmp_path / 'missing.xml'}\n")
    cfg = load(str(conf))
    assert cfg.nng_hub_port == 9093
