# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Shelly Gen 2 RPC client.

Two operations the bridge cares about:

  - Switch.GetStatus { id } → { output: bool, ... }   pre-flight check
  - Switch.Set { id, on, toggle_after } → 200 on accept

Critical trick: `Switch.Set { on: true, toggle_after: T }` on an
already-closed relay is a no-op state-wise but **arms a T-second
hardware timer** that flips the relay to `false` (mains-cut). This is
how the bridge schedules a deadline that survives software faults — the
controller will systemctl-poweroff itself; the Shelly's hardware timer
opens the relay after T regardless.

3-retry with exponential backoff (1, 3, 9 s). Optional digest auth.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import BasicAuth

log = logging.getLogger(__name__)


class ShellyError(Exception):
    """Raised after all retries are exhausted."""


class ShellyClient:
    def __init__(
        self,
        base_url: str,
        switch_id: int = 0,
        username: str = "",
        password: str = "",
        timeout_s: float = 5.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.switch_id = switch_id
        self.username = username
        self.password = password
        self.timeout = aiohttp.ClientTimeout(total=timeout_s)
        # Shelly Gen 2 uses HTTP digest, not Basic. aiohttp's built-in auth
        # is Basic; Shelly accepts Basic in factory-reset state but standard
        # admin login is digest. For now we send Basic and document the
        # limitation — most fielded units are set up without auth.
        self._auth = (
            BasicAuth(username, password) if username and password else None
        )

    async def _call(self, method: str, params: dict) -> dict:
        """One RPC call, no retry."""
        url = f"{self.base_url}/rpc/{method}"
        async with aiohttp.ClientSession(timeout=self.timeout, auth=self._auth) as s:
            async with s.post(url, json=params) as r:
                body = await r.text()
                if r.status != 200:
                    raise ShellyError(f"HTTP {r.status} from {url}: {body[:200]}")
                try:
                    return await r.json(content_type=None)
                except Exception:
                    # Some Shelly firmware returns empty body on success.
                    return {}

    async def call_with_retry(self, method: str, params: dict) -> dict:
        delays = (1, 3, 9)
        last_exc: Exception | None = None
        for attempt in range(len(delays) + 1):
            try:
                return await self._call(method, params)
            except (aiohttp.ClientError, asyncio.TimeoutError, ShellyError) as e:
                last_exc = e
                if attempt < len(delays):
                    log.warning("Shelly %s attempt %d failed: %s; retry in %ds",
                                method, attempt + 1, e, delays[attempt])
                    await asyncio.sleep(delays[attempt])
        raise ShellyError(f"Shelly {method} failed after retries: {last_exc}")

    async def get_status(self) -> dict:
        """Returns the full status dict (`output: bool` is what we check)."""
        return await self.call_with_retry(
            "Switch.GetStatus", {"id": self.switch_id}
        )

    async def arm_timer(self, seconds: int) -> dict:
        """Arm the hardware safety timer.

        Sends `Switch.Set { id, on=true, toggle_after=seconds }`. The
        relay is already closed (mains flowing), so `on=true` is a no-op
        for state. `toggle_after` schedules a hardware flip to OFF after
        `seconds` — that's the mains-cut deadline.
        """
        return await self.call_with_retry(
            "Switch.Set",
            {"id": self.switch_id, "on": True, "toggle_after": int(seconds)},
        )
