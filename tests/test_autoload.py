# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Bridge auto-load: classification, re-drive guard, stop-cancellation.

Async paths run through asyncio.run wrappers. The engine/editor channels are
replaced with lightweight fakes; the NNG-hub wait is disabled
(auto_load_wait_nodes=False) so these focus on the load/arm classification."""

import asyncio

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.engine_state import UNKNOWN


class FakeEngine:
    def __init__(self):
        self.load = ""
        self.armed = "no"
        self.running = "no"
        self.connected = True
        self.stopped = False

    def is_known(self):
        return True

    def project_loaded(self):
        return self.load not in ("", UNKNOWN)

    def project_running(self):
        return self.running == "yes"

    def fully_ready(self):
        return self.project_loaded() and self.armed == "yes"

    async def stop(self):
        self.stopped = True


class FakeEditor:
    def __init__(self, resp=None, raise_on_wait=False):
        self.connected = True
        self._resp = resp
        self.raise_on_wait = raise_on_wait
        self.sent = []
        self.stopped = False

    async def send_action(self, action, value):
        self.sent.append((action, value))
        return True

    async def wait_for_response(self, action, timeout):
        if self.raise_on_wait:
            raise RuntimeError("editor watcher boom")
        return self._resp

    async def stop(self):
        self.stopped = True


def _bridge(*, armed_timeout=1, editor_resp=None):
    cfg = Config()
    cfg.auto_load_project = "uuid-x"
    cfg.auto_load_wait_nodes = False          # skip the NNG-hub wait
    cfg.auto_load_node_settle_s = 0
    cfg.auto_load_armed_timeout_s = armed_timeout  # set directly (bypass validate floor)
    b = Bridge(cfg)
    b.engine = FakeEngine()
    b.editor = FakeEditor(resp=editor_resp)
    return b


def test_armed_success():
    b = _bridge()
    b.engine.load = "projX"
    b.engine.armed = "yes"
    outcome = asyncio.run(b._try_auto_load())
    assert outcome == "armed"
    assert b._auto_load_done is True
    assert b._auto_load_unix_name == "projX"
    assert b.editor.sent == [("project_ready", "uuid-x")]


def test_editor_error_hard_disables_after_three():
    b = _bridge(editor_resp={"type": "error", "action": "project_ready"})
    for i in range(2):
        assert asyncio.run(b._try_auto_load()) == "editor_error"
        assert b._auto_load_disabled is False
        assert b._auto_load_failures == i + 1
    assert asyncio.run(b._try_auto_load()) == "editor_error"
    assert b._auto_load_disabled is True


def test_not_armed_is_soft_never_disables():
    # Editor acks nothing, engine never arms → soft.
    b = _bridge(armed_timeout=1, editor_resp=None)
    b.engine.load = "projX"
    b.engine.armed = "no"
    outcome = asyncio.run(b._try_auto_load())
    assert outcome == "not_armed"
    assert b._auto_load_disabled is False
    assert b._auto_load_done is False


def test_operator_clobber_detects_different_load():
    b = _bridge()
    b._auto_load_unix_name = "projA"
    b.engine.load = "projB"
    assert b._operator_clobber() is True
    b.engine.load = "projA"
    assert b._operator_clobber() is False
    b.engine.load = ""
    assert b._operator_clobber() is False
    b.engine.load = UNKNOWN
    assert b._operator_clobber() is False


def test_operator_clobber_false_before_first_success():
    # During soft retries _auto_load_unix_name is None → never a clobber.
    b = _bridge()
    b._auto_load_unix_name = None
    b.engine.load = "projB"
    assert b._operator_clobber() is False


def test_loop_backs_off_on_operator_clobber():
    # The loop must disable (and NOT re-send project_ready) when an operator
    # has loaded a different project than the one we drove.
    b = _bridge()
    b._auto_load_unix_name = "projA"
    b.engine.load = "projB"
    b.engine.armed = "no"  # not fully ready → loop reaches the guard

    async def driver():
        await asyncio.wait_for(b._auto_load_loop(), timeout=5)

    asyncio.run(driver())
    assert b._auto_load_disabled is True
    assert b.editor.sent == []  # never clobbered


def test_not_armed_records_unix_name_for_clobber_guard():
    # Fix: during the soft-retry window (loaded but not armed), the bridge must
    # record the unix_name its drive produced, so an operator loading a
    # DIFFERENT project before first arm is caught by _operator_clobber.
    b = _bridge(armed_timeout=1)
    b.engine.load = "projX"
    b.engine.armed = "no"
    assert asyncio.run(b._try_auto_load()) == "not_armed"
    assert b._auto_load_unix_name == "projX"
    # Operator now loads a different project mid soft-retry → clobber detected.
    b.engine.load = "projY"
    assert b._operator_clobber() is True


def test_editor_watcher_exception_does_not_crash_attempt():
    # Fix: a raising editor-error watcher must not propagate out of the attempt
    # (which would kill the auto-load loop). It degrades to a soft miss.
    b = _bridge(armed_timeout=1)
    b.editor.raise_on_wait = True
    b.engine.load = "projX"
    b.engine.armed = "no"
    outcome = asyncio.run(b._try_auto_load())
    assert outcome == "not_armed"
    assert b._auto_load_disabled is False


def test_disconnect_resets_projector_debounce():
    # Fix: a genuine engine disconnect must clear the power-on debounce window
    # so a reconnect within the window still re-asserts projector power.
    b = _bridge()
    b._last_projector_on_monotonic = 12345.0
    b._project_loaded_seen = True
    b._on_engine_disconnect()
    assert b._project_loaded_seen is False
    assert b._last_projector_on_monotonic == 0.0


def test_slave_ips_cache_reparses_on_mtime_change(tmp_path, monkeypatch):
    # Fix(efficiency): repeated resolutions reuse the parse; a changed mtime
    # (operator edit) forces a reparse.
    from cuemspowerbridge import network_map
    calls = {"n": 0}

    def fake_slave_ips(path):
        calls["n"] += 1
        return [("10.0.0.1", "node01")]

    monkeypatch.setattr(network_map, "slave_ips", fake_slave_ips)
    p = tmp_path / "network_map.xml"
    p.write_text("<a/>")
    b = _bridge()
    b.cfg.network_map_path = str(p)

    asyncio.run(b._expected_node_ips())
    asyncio.run(b._expected_node_ips())
    assert calls["n"] == 1  # second call served from cache

    # Bump mtime → reparse.
    import os
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 10))
    asyncio.run(b._expected_node_ips())
    assert calls["n"] == 2


def test_stop_cancels_inflight_auto_load_task():
    async def driver():
        b = _bridge()
        # Simulate an in-flight long wait inside the auto-load task.
        b._auto_load_task = asyncio.create_task(asyncio.sleep(100))
        await asyncio.sleep(0.05)
        await asyncio.wait_for(b.stop(), timeout=5)
        return b

    b = asyncio.run(driver())
    assert b._auto_load_task is None
    assert b.engine.stopped is True
    assert b.editor.stopped is True
