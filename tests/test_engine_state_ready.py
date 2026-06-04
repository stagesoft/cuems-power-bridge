# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""EngineClient.fully_ready(): loaded AND armed=='yes'."""

from cuemspowerbridge.engine_state import UNKNOWN, EngineClient


def _engine():
    return EngineClient("ws://localhost:9190")


def test_not_ready_when_unloaded():
    e = _engine()
    e.load = ""
    e.armed = "yes"
    assert e.fully_ready() is False


def test_not_ready_when_loaded_but_not_armed():
    e = _engine()
    e.load = "myproject"
    e.armed = "no"
    assert e.fully_ready() is False


def test_not_ready_when_armed_unknown():
    e = _engine()
    e.load = "myproject"
    e.armed = UNKNOWN
    assert e.fully_ready() is False


def test_ready_when_loaded_and_armed():
    e = _engine()
    e.load = "myproject"
    e.armed = "yes"
    assert e.fully_ready() is True
