# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""parse_devices(): grouping projector.N.* config keys into DeviceDefs."""

from cuemspowerbridge.displays.manager import parse_devices


def test_grouping_skips_and_defaults():
    extras = {
        "projector.1.host": "10.0.0.1",
        "projector.1.name": "Left",
        "projector.1.driver": "pjlink",
        "projector.2.driver": "pjlink",   # no host → skipped
        "projector.3.host": "10.0.0.3",
        "projector.3.driver": "voodoo",   # unknown driver → skipped
        "projector.5.host": "10.0.0.5",   # gap is fine; driver defaults
        "unrelated.key": "x",             # ignored
        "shelly_url": "http://x",         # ignored
    }
    devs = parse_devices(extras)
    assert [d.host for d in devs] == ["10.0.0.1", "10.0.0.5"]
    assert devs[0].name == "Left"
    assert devs[0].driver == "pjlink"
    assert devs[1].driver == "pjlink"     # default applied
    assert devs[1].name == ""             # label() will fall back to host


def test_port_parsing_and_fallback():
    devs = parse_devices({"projector.1.host": "h", "projector.1.port": "4352"})
    assert devs[0].port == 4352
    bad = parse_devices({"projector.1.host": "h", "projector.1.port": "nope"})
    assert bad[0].port == 0  # driver default


def test_empty_extras_gives_no_devices():
    assert parse_devices({}) == []


def test_label_prefers_name_then_host():
    devs = parse_devices({"projector.1.host": "10.0.0.9"})
    assert devs[0].label() == "10.0.0.9"
    named = parse_devices({"projector.1.host": "10.0.0.9", "projector.1.name": "Main"})
    assert named[0].label() == "Main"
