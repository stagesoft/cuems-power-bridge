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
from .escvp import EscVpDriver
from .pjlink import PJLinkDriver

log = logging.getLogger(__name__)

# protocol name → POWER driver class. Add "cec" / "rs232" here later; nothing
# else in the bridge needs to change. NOTE: the ESC/VP21 brightness driver
# (escvp.py) is deliberately NOT here — DRIVERS selects a device's power
# driver, and ESC/VP21 is a separate brightness-only channel built directly
# from the brightness opt-in list (see DisplayManager).
DRIVERS: dict[str, type[DisplayDriver]] = {
    "pjlink": PJLinkDriver,
}

_KEY_RE = re.compile(
    r"^projector\.(\d+)\.(host|driver|name|port|password|brightness|escvp_port|escvp_password)$"
)

# Global, install-defined brightness presets: `brightness.<level>.command = <verb>`.
# Not projector.N.* — parsed separately (parse_brightness_levels), never by _KEY_RE.
_BRIGHTNESS_RE = re.compile(r"^brightness\.([A-Za-z0-9_-]+)\.command$")

_TRUE = ("true", "1", "yes", "on")


def parse_brightness_levels(extras: dict) -> dict[str, str]:
    """Group `brightness.<level>.command` keys into {level: escvp_command}."""
    levels: dict[str, str] = {}
    for key, value in extras.items():
        m = _BRIGHTNESS_RE.match(key.strip())
        if m:
            levels[m.group(1)] = value.strip()
    return levels


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
        port = _coerce_port(idx, "port", fields.get("port", ""))
        escvp_port = _coerce_port(idx, "escvp_port", fields.get("escvp_port", ""))
        devices.append(DeviceDef(
            name=fields.get("name", ""),
            host=host,
            driver=driver,
            port=port,
            password=fields.get("password", ""),
            brightness=fields.get("brightness", "").strip().lower() in _TRUE,
            escvp_port=escvp_port,
            escvp_password=fields.get("escvp_password", ""),
        ))
    return devices


def _coerce_port(idx: int, field: str, raw: str) -> int:
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        log.error("projector.%d.%s=%r not an int; using driver default",
                  idx, field, raw)
        return 0


class DisplayManager:
    def __init__(
        self,
        devices: list[DeviceDef],
        *,
        brightness_levels: dict[str, str] | None = None,
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

        # Brightness (ESC/VP21) — a WHOLLY SEPARATE fleet/list with NO positional
        # coupling to _drivers/_states. Built only from devices that opted in via
        # `brightness=true`. `_brightness_levels` maps level name → ESC/VP21
        # command (install-defined).
        self._brightness_levels: dict[str, str] = dict(brightness_levels or {})
        bright_devices = [d for d in devices if d.brightness]
        if bright_devices and not self._brightness_levels:
            raise ValueError(
                "projector.N.brightness=true is set but no brightness.<level>.command "
                "is configured — define at least one level or remove the opt-in"
            )
        for level, cmd in self._brightness_levels.items():
            if not cmd:
                raise ValueError(f"brightness.{level}.command is empty")
            if "\r" in cmd or "\n" in cmd:
                raise ValueError(f"brightness.{level}.command must not contain CR/LF")
        self._brightness_drivers: list[EscVpDriver] = [
            EscVpDriver(d, timeout_s=timeout_s, dry_run=dry_run) for d in bright_devices
        ]
        self._last_brightness_level: str | None = None

    @classmethod
    def from_config(cls, cfg) -> "DisplayManager":
        devices = parse_devices(cfg.extras)
        levels = parse_brightness_levels(cfg.extras)
        if devices:
            log.info("display fleet: %d device(s): %s", len(devices),
                     ", ".join(f"{d.label()}({d.driver}@{d.host})" for d in devices))
        else:
            log.info("display fleet: none configured (no projector.N.* keys)")
        nbright = sum(1 for d in devices if d.brightness)
        if levels or nbright:
            log.info("brightness fleet: %d device(s), levels: %s",
                     nbright, ", ".join(sorted(levels)) or "none")
        return cls(
            devices,
            brightness_levels=levels,
            timeout_s=float(getattr(cfg, "projector_command_timeout_s", 5)),
            dry_run=cfg.dry_run,
        )

    def __len__(self) -> int:
        return len(self._drivers)

    @property
    def configured(self) -> bool:
        return bool(self._drivers)

    @property
    def brightness_configured(self) -> bool:
        return bool(self._brightness_drivers)

    @property
    def brightness_count(self) -> int:
        return len(self._brightness_drivers)

    @property
    def brightness_levels(self) -> list[str]:
        """Configured level names (sorted) — also used by the endpoint to
        validate an incoming `level` (membership test)."""
        return sorted(self._brightness_levels)

    @property
    def last_brightness_level(self) -> str | None:
        return self._last_brightness_level

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

    # ------------------- brightness (ESC/VP21) -------------------

    async def set_brightness(self, level: str) -> dict:
        """Fan the configured ESC/VP21 command for `level` out across the
        brightness fleet. Per-device errors are isolated (logged, never
        raised) — one projector in standby/refusing must not sink the rest.

        Caller validates `level` against `brightness_levels` first; this method
        assumes it is configured. Returns
        ``{"results": [{"name", "ok", "error"?}], "applied": n, "failed": m}``
        so the endpoint can distinguish full success / partial / all-failed.
        """
        cmd = self._brightness_levels[level]
        log.info("setting brightness '%s' (%s) on %d device(s)%s",
                 level, cmd, len(self._brightness_drivers),
                 " [dry_run]" if self.dry_run else "")

        results = await asyncio.gather(
            *(d.send_command(cmd) for d in self._brightness_drivers),
            return_exceptions=True,
        )
        out: list[dict] = []
        applied = failed = 0
        for drv, res in zip(self._brightness_drivers, results):
            label = drv.dev.label()
            if isinstance(res, Exception):
                failed += 1
                log.error("brightness %s: %s ← %s FAILED: %s", level, label, cmd, res)
                out.append({"name": label, "ok": False, "error": str(res)})
            else:
                applied += 1
                out.append({"name": label, "ok": True})
        # Only record the level when EVERY device confirmed it (a partial apply
        # leaves the fleet in mixed state — don't claim a coherent level).
        if applied and not failed:
            self._last_brightness_level = level
        return {"results": out, "applied": applied, "failed": failed}
