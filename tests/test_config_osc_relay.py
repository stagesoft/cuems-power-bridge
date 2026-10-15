# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The five osc_relay_* keys can never make config.load() raise (cuems-common's
cuems-cluster-poweroff exits 3 on any load error), and OscRelay.from_config()
turns every bad value into either a clamp/fallback with one warning (rate,
refresh) or a disabled relay/listener with last_error (targets, listen, keys).
Mirrors tests/test_project_ops.py's project_op_timeout_s tests.
"""

import pytest

from cuemspowerbridge import config
from cuemspowerbridge import osc_relay as R
from cuemspowerbridge.config import Config
from cuemspowerbridge.osc_relay import OscRelay

KEYS = ("osc_relay_targets", "osc_relay_listen", "osc_relay_keys",
        "osc_relay_refresh_s", "osc_relay_rate")
BAD = ("abc", "30.5", "", "60s", "-1", "1e9", "nan", "inf", "0x10", "127.0.0.1",
       "host.local:8480", "1.2.3.4:0", "1.2.3.4:99999", ",,,", "a b", "/x")


@pytest.fixture(autouse=True)
def fresh_warnings():
    config._warned.clear()
    yield
    config._warned.clear()


def _cfg(**kw):
    c = Config()
    for k, v in kw.items():
        setattr(c, k, v)
    return c


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("value", BAD)
def test_config_load_never_raises(tmp_path, key, value):
    p = tmp_path / "pb.conf"
    p.write_text(f"{key} = {value}\nosc_relay_targets = 127.0.0.1:8480\n"
                 if key != "osc_relay_targets" else f"{key} = {value}\n")
    cfg = config.load(str(p))
    assert isinstance(getattr(cfg, key), str)
    # from_config never raises either.
    OscRelay.from_config(cfg)


def test_defaults_mean_off_and_no_socket():
    cfg = config.load("/nonexistent/power-bridge.conf")
    assert cfg.osc_relay_targets == ""
    assert OscRelay.from_config(cfg) is None


def test_good_config():
    r = OscRelay.from_config(_cfg(osc_relay_targets="127.0.0.1:8480, 127.0.0.1:8481",
                                  osc_relay_keys="cue/, running,/engine/status/load"))
    assert r.enabled and r.last_error is None
    assert [t.name for t in r._targets] == ["127.0.0.1:8480", "127.0.0.1:8481"]
    assert r.keys == ["cue/", "running", "load"]
    assert r.listen == ("127.0.0.1", 8478) and r.rate == 250 and r.refresh_s == 0


@pytest.mark.parametrize("targets", ["abc", "host.local:8480", "127.0.0.1",
                                     "127.0.0.1:0", "127.0.0.1:70000",
                                     "127.0.0.1:8480,nope:1"])
def test_bad_targets_disable_the_relay(targets, caplog):
    r = OscRelay.from_config(_cfg(osc_relay_targets=targets))
    assert r is not None and not r.enabled
    assert "osc_relay_targets" in r.last_error
    assert r.stats()["enabled"] is False
    assert any("DISABLED" in m for m in caplog.messages)


@pytest.mark.parametrize("keys", ["", " , ", "a b", "/x"])
def test_bad_keys_disable_the_relay(keys):
    r = OscRelay.from_config(_cfg(osc_relay_targets="127.0.0.1:8480", osc_relay_keys=keys))
    assert not r.enabled and "osc_relay_keys" in r.last_error


@pytest.mark.parametrize("listen", ["abc", "x.local:8478", "127.0.0.1:0",
                                    "127.0.0.1:8480"])  # last = equal to the target
def test_bad_listen_disables_only_the_listener(listen):
    r = OscRelay.from_config(_cfg(osc_relay_targets="127.0.0.1:8480",
                                  osc_relay_listen=listen))
    assert r.enabled and r.listen is None
    assert "osc_relay_listen" in r.last_error


def test_empty_listen_means_no_listener_and_no_error():
    r = OscRelay.from_config(_cfg(osc_relay_targets="127.0.0.1:8480", osc_relay_listen=""))
    assert r.enabled and r.listen is None and r.last_error is None


@pytest.mark.parametrize("raw,want", [("abc", 250), ("30.5", 250), ("", 250),
                                      ("60s", 250), ("5", 100), ("99999", 8000),
                                      ("2000", 2000)])
def test_rate_clamps_or_falls_back(raw, want, caplog):
    r = OscRelay.from_config(_cfg(osc_relay_targets="127.0.0.1:8480", osc_relay_rate=raw))
    assert r.enabled and r.rate == want and r.last_error is None
    if raw != str(want):
        assert sum("osc_relay_rate" in m for m in caplog.messages) == 1


def test_rate_warns_once_per_process(caplog):
    for _ in range(3):
        R.parse_rate("abc")
    assert sum("osc_relay_rate" in m for m in caplog.messages) == 1


@pytest.mark.parametrize("raw,want", [("0", 0), ("", 0), ("abc", 0), ("60s", 0),
                                      ("-5", 0), ("nan", 0), ("inf", 0),
                                      ("1", 30), ("29", 30), ("30.5", 30.5),
                                      ("120", 120)])
def test_refresh_falls_back_or_clamps(raw, want):
    r = OscRelay.from_config(_cfg(osc_relay_targets="127.0.0.1:8480",
                                  osc_relay_refresh_s=raw))
    assert r.enabled and r.refresh_s == want


def test_typos_are_reported(tmp_path, caplog):
    p = tmp_path / "pb.conf"
    p.write_text("osc_relay_target = 127.0.0.1:8480\nosc_relay_rates = 5\n")
    cfg = config.load(str(p))
    assert OscRelay.from_config(cfg) is None          # the typo means: off
    assert R.disabled_status(cfg) == {
        "enabled": False, "unknown_keys": ["osc_relay_rates", "osc_relay_target"]}
    assert any("osc_relay_target" in m for m in caplog.messages)


def test_default_conf_ships_the_block_commented_out():
    from importlib import resources
    text = resources.files("cuemspowerbridge.data").joinpath(
        "power-bridge.conf.default").read_text()
    lines = [ln for ln in text.splitlines() if "osc_relay_" in ln]
    assert lines and all(ln.lstrip().startswith("#") for ln in lines)
