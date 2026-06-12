# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Display-driver abstraction: the contract every protocol implements.

`DisplayDriver` is the modular seam. A driver speaks exactly one wire
protocol (PJLink, ESC/VP.net, CEC, ...) to exactly one device. All three
operations raise `DisplayError` on unrecoverable failure; the caller
(`DisplayManager`) isolates per-device errors so one bad device never
sinks the fleet — important because projector power-off rides the
safety-critical cluster shutdown and must never block it.
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from dataclasses import dataclass


class PowerState(enum.Enum):
    OFF = "off"
    ON = "on"
    WARMUP = "warmup"
    COOLDOWN = "cooldown"
    UNKNOWN = "unknown"

    def __str__(self) -> str:
        return self.value


class DisplayError(Exception):
    """Raised by a driver on unrecoverable failure (retries exhausted,
    protocol/auth error, transport failure)."""


class DisplayUnconfirmed(DisplayError):
    """The command was sent but the device did not confirm the new state
    within the retry budget (e.g. PJLink ERR3 while warming/cooling).

    Distinct from a hard failure: the request was likely accepted but the
    resulting power state is UNKNOWN, not the requested one. Callers must
    NOT record the optimistic target state for the device."""


@dataclass
class DeviceDef:
    """One configured display device (from `projector.N.*` config keys)."""

    name: str
    host: str
    driver: str = "pjlink"
    port: int = 0  # 0 → driver-specific default
    password: str = ""
    # Optional second channel: Epson ESC/VP21-over-ESC/VP.net for brightness
    # (TCP 3629), independent of the PJLink power channel above. `brightness`
    # opts a device into the brightness fleet; the escvp_* fields are its
    # ESC/VP.net port/password (often a different secret than PJLink).
    brightness: bool = False
    escvp_port: int = 0  # 0 → driver-specific default (3629)
    escvp_password: str = ""

    def label(self) -> str:
        """Human label for logs/status — name if set, else host."""
        return self.name or self.host


class DisplayDriver(ABC):
    """Protocol driver for a single display device.

    Subclasses must honor `dry_run` (log intent, perform no I/O) and keep
    each operation bounded by `timeout_s`.
    """

    def __init__(
        self,
        dev: DeviceDef,
        *,
        timeout_s: float = 5.0,
        dry_run: bool = False,
    ):
        self.dev = dev
        self.timeout_s = timeout_s
        self.dry_run = dry_run

    @abstractmethod
    async def power_on(self) -> None:
        """Power the device on. Raises DisplayError on hard failure."""

    @abstractmethod
    async def power_off(self) -> None:
        """Power the device off. Raises DisplayError on hard failure."""

    @abstractmethod
    async def power_status(self) -> PowerState:
        """Best-effort current power state; never raises (returns UNKNOWN
        on any failure) so it is safe to call from /status paths."""
