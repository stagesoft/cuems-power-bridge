# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""DisplayManager — owns the configured display fleet and fans power
commands across it concurrently.

Builds `DeviceDef`s from `projector.N.*` keys in `Config.extras`,
instantiates the right `DisplayDriver` per device via the `DRIVERS`
registry (the single extension point for new protocols), and runs
`power_on_all` / `power_off_all` / `status_all` with `asyncio.gather`
(mirroring `node_executor.poweroff_all`). Per-device errors are caught
and logged, NEVER raised — one unreachable projector must not abort the
safety-critical cluster shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import re

from .base import DeviceDef, DisplayDriver, DisplayUnconfirmed, PowerState
from .pjlink import PJLinkDriver

log = logging.getLogger(__name__)

# protocol name → driver class. Add "escvp" / "cec" / "rs232" here later;
# nothing else in the bridge needs to change.
DRIVERS: dict[str, type[DisplayDriver]] = {
    "pjlink": PJLinkDriver,
}

_KEY_RE = re.compile(r"^projector\.(\d+)\.(host|driver|name|port|password)$")


def parse_devices(extras: dict) -> list[DeviceDef]:
    """Group `projector.N.<field>` keys into DeviceDefs (sorted by N).

    Devices with no host, or an unknown driver, are skipped with an ERROR
    log rather than crashing the bridge.
    """
    grouped: dict[int, dict[str, str]] = {}
    for key, value in extras.items():
        m = _KEY_RE.match(key.strip())
        if not m:
            continue
        grouped.setdefault(int(m.group(1)), {})[m.group(2)] = value.strip()

    devices: list[DeviceDef] = []
    for idx in sorted(grouped):
        fields = grouped[idx]
        host = fields.get("host", "")
        if not host:
            log.error("projector.%d.* has no host; skipping", idx)
            continue
        driver = (fields.get("driver") or "pjlink").lower()
        if driver not in DRIVERS:
            log.error("projector.%d.driver=%r unknown (known: %s); skipping %s",
                      idx, driver, ", ".join(sorted(DRIVERS)), host)
            continue
        port = 0
        raw_port = fields.get("port", "")
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError:
                log.error("projector.%d.port=%r not an int; using driver default",
                          idx, raw_port)
        devices.append(DeviceDef(
            name=fields.get("name", ""),
            host=host,
            driver=driver,
            port=port,
            password=fields.get("password", ""),
        ))
    return devices


class DisplayManager:
    def __init__(
        self,
        devices: list[DeviceDef],
        *,
        timeout_s: float = 5.0,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self._drivers: list[DisplayDriver] = [
            DRIVERS[d.driver](d, timeout_s=timeout_s, dry_run=dry_run)
            for d in devices
        ]
        # Last-known power state per device, index-aligned with _drivers (NOT
        # keyed by label — two devices can share a label and must stay
        # distinct). We never make a live network call inside /status; this is
        # refreshed whenever a power action runs (and by status_all()).
        self._states: list[PowerState] = [PowerState.UNKNOWN] * len(self._drivers)

    @classmethod
    def from_config(cls, cfg) -> "DisplayManager":
        devices = parse_devices(cfg.extras)
        if devices:
            log.info("display fleet: %d device(s): %s", len(devices),
                     ", ".join(f"{d.label()}({d.driver}@{d.host})" for d in devices))
        else:
            log.info("display fleet: none configured (no projector.N.* keys)")
        return cls(
            devices,
            timeout_s=float(getattr(cfg, "projector_command_timeout_s", 5)),
            dry_run=cfg.dry_run,
        )

    def __len__(self) -> int:
        return len(self._drivers)

    @property
    def configured(self) -> bool:
        return bool(self._drivers)

    def snapshot(self) -> list[dict]:
        """Informational power snapshot for the /status payload (cached;
        no live network call). One entry per configured device, in order."""
        return [{"name": drv.dev.label(), "power": self._states[i].value}
                for i, drv in enumerate(self._drivers)]

    async def _apply(self, action: str, on_ok: PowerState) -> None:
        if not self._drivers:
            return

        async def run(drv: DisplayDriver) -> None:
            if action == "on":
                await drv.power_on()
            else:
                await drv.power_off()

        results = await asyncio.gather(
            *(run(d) for d in self._drivers), return_exceptions=True
        )
        for i, (drv, res) in enumerate(zip(self._drivers, results)):
            label = drv.dev.label()
            if isinstance(res, DisplayUnconfirmed):
                # Command sent but state not confirmed (e.g. ERR3 warmup) —
                # record UNKNOWN, not the optimistic target, and WARN.
                log.warning("display %s: power %s unconfirmed: %s", label, action, res)
                self._states[i] = PowerState.UNKNOWN
            elif isinstance(res, Exception):
                log.error("display %s: power %s failed: %s", label, action, res)
                self._states[i] = PowerState.UNKNOWN
            else:
                self._states[i] = on_ok

    async def power_on_all(self) -> None:
        log.info("powering ON %d display(s)%s",
                 len(self._drivers), " [dry_run]" if self.dry_run else "")
        await self._apply("on", PowerState.ON)

    async def power_off_all(self) -> None:
        log.info("powering OFF %d display(s)%s",
                 len(self._drivers), " [dry_run]" if self.dry_run else "")
        await self._apply("off", PowerState.OFF)

    async def status_all(self) -> list[dict]:
        """Query every device's power state in parallel, refresh the cache,
        and return the same shape as snapshot() (so callers see one
        consistent type for power state across both methods)."""
        if not self._drivers:
            return []
        results = await asyncio.gather(
            *(d.power_status() for d in self._drivers), return_exceptions=True
        )
        for i, res in enumerate(results):
            self._states[i] = res if isinstance(res, PowerState) else PowerState.UNKNOWN
        return self.snapshot()
