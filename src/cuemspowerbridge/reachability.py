# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Poll a set of hosts until they're all unreachable, or timeout.

ICMP via /usr/bin/ping; falls back to TCP-connect on :22 if ping fails
locally (no setcap on ping). Each host needs 3 consecutive failed
probes before we consider it down (debounces transient network blips
during shutdown).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class _State:
    consecutive_failures: int = 0
    confirmed_down: bool = False


@dataclass
class PollResult:
    elapsed_s: float
    stuck_hosts: list[str] = field(default_factory=list)
    timed_out: bool = False


async def _ping_once(host: str) -> bool:
    """One ICMP ping. Returns True iff host responded."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/ping", "-c", "1", "-W", "1", "-n", host,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await asyncio.wait_for(proc.wait(), timeout=2)
        return rc == 0
    except (asyncio.TimeoutError, FileNotFoundError):
        return False
    except Exception:
        return False


async def _tcp_once(host: str, port: int = 22) -> bool:
    """One TCP connect attempt. Returns True iff socket opened."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=2
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def _alive(host: str) -> bool:
    """Host considered alive if EITHER ICMP OR TCP/22 succeeds."""
    if await _ping_once(host):
        return True
    return await _tcp_once(host, 22)


async def wait_until_all_down(
    hosts: list[str],
    *,
    interval_s: float = 2.0,
    max_wait_s: int = 180,
    confirm_failures: int = 3,
) -> PollResult:
    """Poll `hosts` every `interval_s`. A host is "confirmed down" after
    `confirm_failures` consecutive non-responses. Returns when all hosts
    confirmed-down OR `max_wait_s` elapses (stuck hosts logged).
    """
    if not hosts:
        return PollResult(elapsed_s=0.0)

    states: dict[str, _State] = {h: _State() for h in hosts}
    loop = asyncio.get_event_loop()
    started = loop.time()

    while True:
        # Probe every still-up host in parallel.
        targets = [h for h, s in states.items() if not s.confirmed_down]
        if not targets:
            return PollResult(elapsed_s=loop.time() - started)

        results = await asyncio.gather(*[_alive(h) for h in targets])
        for h, alive in zip(targets, results):
            st = states[h]
            if alive:
                st.consecutive_failures = 0
            else:
                st.consecutive_failures += 1
                if st.consecutive_failures >= confirm_failures:
                    st.confirmed_down = True
                    log.info("reachability: %s confirmed down", h)

        elapsed = loop.time() - started
        if elapsed >= max_wait_s:
            stuck = [h for h, s in states.items() if not s.confirmed_down]
            if stuck:
                log.warning("reachability: timeout after %.1fs; stuck hosts: %s",
                            elapsed, ", ".join(stuck))
            return PollResult(elapsed_s=elapsed, stuck_hosts=stuck, timed_out=True)

        await asyncio.sleep(interval_s)
