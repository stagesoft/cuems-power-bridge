# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The sequence lock: one power-off at a time, per machine.

The properties under test are the ones that make it safe rather than merely
present: it never waits, it is released when its holder dies, it never degrades
into "proceed unlocked", and it can be dropped early — because the daemon's own
local power-off re-enters the sequence through systemd and must find the lock
free.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from cuemspowerbridge.shutdown_lock import (
    ShutdownInProgress,
    ShutdownLockUnavailable,
    acquire,
    held_elsewhere,
)


def test_a_second_acquisition_refuses_immediately(tmp_path):
    lock = str(tmp_path / "shutdown.lock")
    started = time.monotonic()
    with acquire(lock):
        with pytest.raises(ShutdownInProgress):
            with acquire(lock):
                pass
    # "Immediately" is the point: a queued power-off happens at an
    # unpredictable moment.
    assert time.monotonic() - started < 1.0


def test_the_lock_is_free_again_afterwards(tmp_path):
    lock = str(tmp_path / "shutdown.lock")
    with acquire(lock):
        pass
    with acquire(lock):
        pass  # no leak, no stale state


def test_release_lets_the_re_entrant_transition_take_it(tmp_path):
    """The daemon drops the lock before its local power-off, because that
    command re-enters the same sequence through systemd."""
    lock = str(tmp_path / "shutdown.lock")
    with acquire(lock) as held:
        held.release()
        with acquire(lock):          # the wrapper, on the re-entrant path
            pass
    # release() is idempotent — the context manager's own exit runs too.


def test_a_dead_holder_blocks_nothing(tmp_path):
    """No stale-lock heuristic: the kernel drops it when the holder dies."""
    lock = str(tmp_path / "shutdown.lock")
    src = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r} + "/src")
        from cuemspowerbridge.shutdown_lock import acquire
        with acquire({lock!r}):
            print("held", flush=True)
            time.sleep(30)
    """)
    proc = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        with pytest.raises(ShutdownInProgress):
            with acquire(lock):
                pass
        proc.kill()
        proc.wait(timeout=5)
        with acquire(lock):          # immediately available again
            pass
    finally:
        if proc.poll() is None:
            proc.kill()


def test_a_missing_runtime_directory_is_a_precondition_failure(tmp_path):
    """Never "no power-off is running" — the remedy is the tmpfiles rule."""
    with pytest.raises(ShutdownLockUnavailable) as exc:
        with acquire(str(tmp_path / "absent" / "shutdown.lock")):
            pass
    assert "tmpfiles" in str(exc.value)


def test_an_unopenable_lock_is_a_precondition_failure(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root ignores file modes")
    d = tmp_path / "locked"
    d.mkdir(mode=0o500)
    with pytest.raises(ShutdownLockUnavailable):
        with acquire(str(d / "shutdown.lock")):
            pass


def test_held_elsewhere_acquires_nothing(tmp_path):
    """--lock-held: the wrapper holds one lock across both stages."""
    lock = str(tmp_path / "shutdown.lock")
    with acquire(lock):
        with held_elsewhere():       # would raise if it tried to take it
            pass


def test_errors_are_distinguishable(tmp_path):
    """The two failures have different remedies, so they are different types."""
    assert not issubclass(ShutdownInProgress, ShutdownLockUnavailable)
    assert not issubclass(ShutdownLockUnavailable, ShutdownInProgress)
