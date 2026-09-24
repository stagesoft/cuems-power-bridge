# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""One cluster power-off at a time, per MACHINE rather than per process.

The daemon's own guard is an ``asyncio.Lock``: it cannot see the ExecStop
helper, and the helper cannot see it. This module is the shared one — an
exclusive ``flock`` that every entry point takes before beginning a power-off.

Three properties, each deliberate:

* **Non-blocking.** A second attempt refuses immediately rather than queueing,
  because a queued power-off is a power-off that happens at an unpredictable
  moment.
* **Kernel-released.** ``flock`` is dropped when the holder dies, so an
  interrupted run leaves nothing to clean up. Stale-lock heuristics are how
  lock files become their own outage.
* **Sequence-scoped, not stage-scoped.** The ExecStop wrapper runs the helper
  twice (displays, then nodes) and holds ONE lock across both — otherwise the
  gap between them is a window in which another power-off could start. The
  helper is then told ``--lock-held`` and takes nothing (`held_elsewhere()`).

It is also released **before** the daemon issues its local power-off: that
command re-enters the same sequence through the systemd transition, and a
still-held lock would make the wrapper refuse and run neither stage.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

#: Runtime path, created by this package's tmpfiles rule
#: (``d /run/cuems-power-bridge 0770 cuems cuems``). Injectable so tests do not
#: depend on /run existing on a developer's machine.
DEFAULT_LOCK_PATH = "/run/cuems-power-bridge/shutdown.lock"


class ShutdownLockError(Exception):
    """Base: the lock could not be taken. NEVER means "proceed unlocked"."""


class ShutdownInProgress(ShutdownLockError):
    """Another power-off holds the lock. The caller refuses; it does not wait."""


class ShutdownLockUnavailable(ShutdownLockError):
    """The lock itself could not be opened — a precondition failure.

    A missing runtime directory (the tmpfiles rule never ran) or a permission
    error. Distinct from `ShutdownInProgress` because the remedy differs, and
    because "no power-off is running" must never be inferred from it.
    """


class _Held:
    """A lock this process holds, and can drop early."""

    def __init__(self, fd: int, path: str) -> None:
        self._fd: int | None = fd
        self.path = path

    def release(self) -> None:
        """Drop the lock now.

        Called before the local power-off: that command re-enters this sequence
        through the systemd transition, and the wrapper must be able to take the
        lock cleanly there. Idempotent.
        """
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None
            log.debug("shutdown lock released (%s)", self.path)


@contextmanager
def held_elsewhere() -> Iterator[_Held]:
    """A no-op lock: the CALLER already holds it (``--lock-held``).

    Explicit rather than inferred from an inherited file descriptor — a wrong
    inference would run a power-off unlocked, which is the failure this module
    exists to prevent.
    """
    log.debug("shutdown lock: held by caller, not acquiring")
    yield _Held(-1, "<held by caller>")


@contextmanager
def acquire(path: str = DEFAULT_LOCK_PATH) -> Iterator[_Held]:
    """Take the sequence lock, or raise. Never waits, never proceeds unlocked."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o660)
    except PermissionError as e:
        raise ShutdownLockUnavailable(
            f"cannot open the power-off lock {path}: {e}. It is owned by the "
            f"service account; the power-off runs as root or as that account."
        ) from e
    except OSError as e:
        if e.errno == errno.ENOENT:
            raise ShutdownLockUnavailable(
                f"{os.path.dirname(path)} does not exist, so the power-off lock "
                f"cannot be taken. It is created by this package's tmpfiles rule "
                f"(systemd-tmpfiles --create /usr/lib/tmpfiles.d/"
                f"cuems-power-bridge.conf)."
            ) from e
        raise ShutdownLockUnavailable(f"cannot open the power-off lock {path}: {e}") from e

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EACCES, errno.EAGAIN):
            raise ShutdownInProgress(
                "another cluster power-off is already in progress "
                f"(holding {path}); refusing rather than queueing"
            ) from e
        raise ShutdownLockUnavailable(f"cannot lock {path}: {e}") from e

    held = _Held(fd, path)
    log.debug("shutdown lock acquired (%s)", path)
    try:
        yield held
    finally:
        held.release()
