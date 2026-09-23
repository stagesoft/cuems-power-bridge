# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Shared fixtures: point cuemsutils at one of tests/fixtures/network_map/*.

`ConfigBase.load_base_settings` honours the CUEMS_CONF_PATH **environment
variable over its config_dir argument** (measured 2026-09-23, research R2), so
a test that only passes a directory is silently ignored on a host that has the
variable set. Everything here sets the variable and restores it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "network_map"


@pytest.fixture
def conf_dir(monkeypatch):
    """Return a callable: case name -> that fixture directory, env pointed at it.

    Usage::

        def test_x(conf_dir):
            d = conf_dir("map-two-adopted")
    """

    def _use(case: str) -> Path:
        d = FIXTURES / case
        assert d.is_dir(), f"no such fixture: {case} (have: {sorted(p.name for p in FIXTURES.iterdir())})"
        monkeypatch.setenv("CUEMS_CONF_PATH", str(d))
        return d

    return _use


@pytest.fixture(autouse=True)
def _no_inherited_conf_path(monkeypatch):
    """A developer box with CUEMS_CONF_PATH exported must not steer the suite."""
    monkeypatch.delenv("CUEMS_CONF_PATH", raising=False)
