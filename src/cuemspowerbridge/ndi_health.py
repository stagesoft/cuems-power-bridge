# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Is each machine of the cluster there, for the NDI preview (plan 869ekxuez
rev 12.2, §3.9.3).

A preview to a machine that is off the network used to end, 15 s later, as
"sin confirmar (no terminal VC line within 15s)". This keeps a state per
machine, refreshed in the background, so a request never waits on a probe:

- a node is ``alive`` when ICMP or TCP/22 answers, or its uploaded journal
  was written in the last 60 s; ``unreachable`` after two consecutive cycles
  with none of the three (one lost echo on a loaded daisy chain is not a
  verdict);
- the controller is ``alive`` while ``cuems-videocomposer`` is active, else
  ``vc_down``;
- ``unknown`` until a machine has been checked.

Nothing is probed while a show blocks previews, and the loop stops after a
while without NDI requests (it restarts on the next one). The shutdown path's
``reachability`` helpers are left alone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CYCLE_S = 5.0            # one pass over every machine
PROBE_CEILING_S = 1.5    # per probe; ICMP and TCP run in parallel
JOURNAL_ALIVE_S = 60.0   # a journal written this recently = alive
FAILS_FOR_DOWN = 2       # consecutive failed cycles before "unreachable"
IDLE_STOP_S = 600.0      # stop probing after this long without NDI requests
VC_UNIT = "cuems-videocomposer"


@dataclass
class Health:
    state: str = "unknown"          # alive | unreachable | vc_down | unknown
    journal_age_s: float | None = None
    fails: int = 0
    checked_at: float = 0.0

    def public(self) -> dict:
        age = None if self.journal_age_s is None else round(self.journal_age_s)
        return {"state": self.state, "journal_age_s": age}


async def icmp(ip: str) -> bool:
    """One echo, 1 s wait."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ping", "-c", "1", "-W", "1", "-n", ip,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            return await asyncio.wait_for(proc.wait(), timeout=PROBE_CEILING_S) == 0
        except asyncio.TimeoutError:
            proc.kill()
            return False
    except OSError:
        return False


async def tcp22(ip: str) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(ip, 22),
                                           timeout=PROBE_CEILING_S)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def unit_active(unit: str = VC_UNIT) -> bool:
    """`systemctl is-active` is a read-only D-Bus query: no polkit."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "is-active", "--quiet", unit,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await asyncio.wait_for(proc.wait(), timeout=PROBE_CEILING_S) == 0
    except (OSError, asyncio.TimeoutError):
        return False


class HealthMonitor:
    """`machines()` → [(label, ip or None)], "local" for the controller.
    `journal_age(ip)` → seconds since that node's uploaded journal was last
    written, or None. `blocked()` → True while a show blocks previews."""

    def __init__(self, machines: Callable[[], Awaitable[list[tuple[str, str | None]]]],
                 journal_age: Callable[[str], float | None],
                 blocked: Callable[[], bool],
                 icmp_probe: Callable[[str], Awaitable[bool]] = icmp,
                 tcp_probe: Callable[[str], Awaitable[bool]] = tcp22,
                 vc_probe: Callable[[], Awaitable[bool]] = unit_active):
        self._machines = machines
        self._journal_age = journal_age
        self._blocked = blocked
        self._icmp = icmp_probe
        self._tcp = tcp_probe
        self._vc = vc_probe
        self._state: dict[str, Health] = {}
        self._task: asyncio.Task | None = None
        self._used = 0.0

    def get(self, label: str) -> Health:
        return self._state.get(label, Health())

    def all(self) -> dict[str, Health]:
        return dict(self._state)

    def touch(self) -> None:
        """An NDI request arrived: keep (or start) the background loop."""
        self._used = time.monotonic()
        if self._task is None or self._task.done():
            try:
                self._task = asyncio.get_running_loop().create_task(
                    self._loop(), name="ndi-health")
            except RuntimeError:   # no running loop (called from sync code)
                self._task = None

    async def _loop(self) -> None:
        try:
            while time.monotonic() - self._used < IDLE_STOP_S:
                try:
                    await self.run_cycle()
                except Exception:
                    log.exception("ndi-health: cycle failed")
                await asyncio.sleep(CYCLE_S)
        except asyncio.CancelledError:
            pass

    async def run_cycle(self) -> None:
        if self._blocked():
            return
        machines = await self._machines()
        await asyncio.gather(*(self._check(label, ip) for label, ip in machines))
        known = {label for label, _ in machines}
        for label in list(self._state):
            if label not in known:
                del self._state[label]

    async def _check(self, label: str, ip: str | None) -> None:
        h = self._state.setdefault(label, Health())
        if label == "local":
            ok = await self._vc()
            down_state = "vc_down"
            h.journal_age_s = 0.0
        elif not ip:
            return            # no <ip> in network_map: nothing to probe, stays unknown
        else:
            age = self._journal_age(ip)
            h.journal_age_s = age
            fresh = age is not None and age < JOURNAL_ALIVE_S
            if fresh:
                ok = True
            else:
                a, b = await asyncio.gather(self._icmp(ip), self._tcp(ip))
                ok = a or b
            down_state = "unreachable"
        h.checked_at = time.monotonic()
        if ok:
            if h.state not in ("alive", "unknown"):
                log.info("ndi-health: %s is back", label)
            h.state, h.fails = "alive", 0
            return
        h.fails += 1
        if h.fails >= FAILS_FOR_DOWN and h.state != down_state:
            log.warning("ndi-health: %s → %s (%d cycles)", label, down_state, h.fails)
            h.state = down_state

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
