# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""PJLink (JBMIA) driver — TCP 4352.

PJLink is the vendor-neutral projector-control standard. This one driver
covers the Epson EB-690SU / EB-815E *and* most other PJLink-class
projectors (Panasonic, NEC, Sony, BenQ, ...), which is why it is the
modular foundation for this feature.

One command per TCP session (stateless, like ShellyClient): open, read
the greeting, optionally prepend an MD5 auth digest, send `%1POWR ...`,
read the response, close. The whole exchange runs under a single
`asyncio.wait_for(timeout_s)` so a projector that accepts the socket then
hangs cannot block the caller (and therefore cannot stall shutdown).

Wire notes:
  - Greeting: `PJLINK 0` (no auth) | `PJLINK 1 <8hex>` (auth required).
    On `PJLINK 1`, the client prepends md5(<rand> + password) as 32 lower
    hex chars to the first command — even when the password is blank
    (blank means the empty string, NOT "skip auth").
  - Power: set `%1POWR 1` / `%1POWR 0`; query `%1POWR ?` → `=0/1/2/3`
    (off / on / cooling / warming).
  - Responses are CR-terminated per spec; we strip CR *and* LF defensively
    because some third-party stacks emit CRLF (a stray LF left in the read
    buffer would corrupt the next line).

Power-ON from a fully powered-off projector requires the unit's
"Standby Mode Communications = ON" (network standby) plus PJLink enabled.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from .base import DeviceDef, DisplayDriver, DisplayError, PowerState

log = logging.getLogger(__name__)

DEFAULT_PORT = 4352

# Short fail-fast retry profile. Deliberately NOT ShellyClient's (1, 3, 9):
# a projector failure is logged and discarded (it must never abort or delay
# the cluster shutdown), so we must not burn ~33 s on an unreachable unit.
_RETRY_DELAYS = (1, 3)

_POWR_STATE = {
    "0": PowerState.OFF,
    "1": PowerState.ON,
    "2": PowerState.COOLDOWN,
    "3": PowerState.WARMUP,
}


class PJLinkError(DisplayError):
    """PJLink transport / protocol / auth failure."""


class PJLinkDriver(DisplayDriver):
    def __init__(self, dev: DeviceDef, *, timeout_s: float = 5.0, dry_run: bool = False):
        super().__init__(dev, timeout_s=timeout_s, dry_run=dry_run)
        self.host = dev.host
        self.port = dev.port or DEFAULT_PORT
        self.password = dev.password

    # ------------------- protocol primitives -------------------

    async def _command(self, body: str) -> str:
        """Run one `%1<body>` exchange; return the payload after '='.

        The entire connect→greet→send→read→close cycle is bounded by a
        single wait_for. Raises PJLinkError on any failure.
        """
        try:
            return await asyncio.wait_for(self._exchange(body), timeout=self.timeout_s)
        except asyncio.TimeoutError as e:
            raise PJLinkError(
                f"{self.dev.label()}: timeout after {self.timeout_s}s"
            ) from e
        except PJLinkError:
            raise
        except Exception as e:  # OSError, IncompleteReadError, LimitOverrunError, ...
            raise PJLinkError(f"{self.dev.label()}: transport error: {e}") from e

    async def _exchange(self, body: str) -> str:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            greeting = (await reader.readuntil(b"\r")).strip(b"\r\n")
            prefix = self._auth_prefix(greeting)
            writer.write(f"{prefix}%1{body}\r".encode("ascii"))
            await writer.drain()
            resp = (await reader.readuntil(b"\r")).strip(b"\r\n")
            return self._parse_response(body, resp)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _auth_prefix(self, greeting: bytes) -> str:
        """Return the digest to prepend ('' when the projector wants no auth)."""
        text = greeting.decode("ascii", "replace").strip()
        parts = text.split()
        if len(parts) >= 2 and parts[0] == "PJLINK":
            if parts[1] == "0":
                return ""
            if parts[1] == "1" and len(parts) >= 3:
                rand = parts[2]
                # ALWAYS compute on a PJLINK 1 greeting. A blank password is
                # md5(rand + ""), not a reason to skip the digest.
                return hashlib.md5((rand + self.password).encode("ascii")).hexdigest()
        raise PJLinkError(f"{self.dev.label()}: unexpected greeting {text!r}")

    def _parse_response(self, body: str, resp: bytes) -> str:
        text = resp.decode("ascii", "replace").strip()
        if text == "PJLINK ERRA":
            raise PJLinkError(f"{self.dev.label()}: authentication failed (bad password)")
        verb = body.split()[0]  # e.g. "POWR"
        marker = f"%1{verb}="
        if not text.startswith(marker):
            raise PJLinkError(f"{self.dev.label()}: unexpected response {text!r}")
        return text[len(marker):]  # payload after '='

    # ------------------- public API -------------------

    async def power_on(self) -> None:
        await self._set_power("1")

    async def power_off(self) -> None:
        await self._set_power("0")

    async def _set_power(self, value: str) -> None:
        if self.dry_run:
            log.info("[dry_run] PJLink %s: would send POWR %s", self.dev.label(), value)
            return
        last: Exception | None = None
        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                payload = await self._command(f"POWR {value}")
            except PJLinkError as e:
                last = e
                if attempt < len(_RETRY_DELAYS):
                    log.warning("PJLink %s POWR %s attempt %d failed: %s; retry in %ds",
                                self.dev.label(), value, attempt + 1, e,
                                _RETRY_DELAYS[attempt])
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                    continue
                raise
            if payload == "OK":
                log.info("PJLink %s: POWR %s OK", self.dev.label(), value)
                return
            if payload == "ERR3":
                # Unavailable now (warming up / cooling down). Retry within
                # budget; if it persists treat as "issued, verify later" and
                # WARN (not ERROR) — a 30-60 s warmup legitimately outlasts us.
                last = PJLinkError(f"{self.dev.label()}: ERR3 (busy/warming)")
                if attempt < len(_RETRY_DELAYS):
                    await asyncio.sleep(_RETRY_DELAYS[attempt])
                    continue
                log.warning(
                    "PJLink %s: POWR %s still ERR3 after retries — command "
                    "issued, projector likely warming/cooling; verify later",
                    self.dev.label(), value,
                )
                return
            # ERR1 (bad command) / ERR2 (bad param) / ERR4 (projector fault):
            # hard, not retryable.
            raise PJLinkError(f"{self.dev.label()}: POWR {value} returned {payload}")
        if last:  # pragma: no cover - defensive
            raise last

    async def power_status(self) -> PowerState:
        if self.dry_run:
            return PowerState.UNKNOWN
        try:
            payload = await self._command("POWR ?")
        except PJLinkError as e:
            log.warning("PJLink %s: status query failed: %s", self.dev.label(), e)
            return PowerState.UNKNOWN
        return _POWR_STATE.get(payload, PowerState.UNKNOWN)
