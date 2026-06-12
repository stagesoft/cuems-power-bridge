# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Epson ESC/VP21-over-ESC/VP.net driver — TCP 3629.

This is the *brightness* channel for Epson projectors. PJLink (the power
channel, see `pjlink.py`) has no brightness command in its set, so anything
beyond power — image brightness, light-source mode, color mode — goes over
Epson's own ESC/VP.net transport on TCP 3629.

Deliberately NOT a `DisplayDriver`/`DRIVERS` entry: `DRIVERS` selects a
device's *power* driver. This driver is instantiated directly by
`DisplayManager` for the (separate) brightness-opt-in fleet, and exposes a
single generic `send_command()` so the verb/value live entirely in config
(`brightness.<level>.command`), not in code.

ESC/VP.net handshake (verified live against Epson EB-810E/815E):
  - Request: ``b"ESC/VP.net" + bytes([0x10, 0x03, 0x00, 0x00, 0x00, nhdr])``.
    With a password, ``nhdr=0x01`` and an 18-byte header follows:
    ``bytes([0x01, 0x01]) + password[:16].ljust(16, b"\\x00")`` (id 0x01,
    attr 0x01 = plain text, then the 16-byte NUL-padded password). With an
    empty password, ``nhdr=0x00`` and no trailing header.
  - Response: 16-byte header; byte 14 = status (0x20 OK, 0x41 password
    required, 0x43 wrong password, 0x40 bad request, 0x45 request not
    allowed — e.g. ESC/VP.net "Command Communication" disabled on the unit,
    0x53 busy); byte 15 = count of trailing 18-byte headers to DRAIN before
    sending any command (else they corrupt the first reply).
  - After 0x20, send a CR-terminated ESC/VP21 command (e.g. ``BRIGHT 0``).
    Reply ends at the ``:`` prompt: a bare ``:`` (or ``=value:`` for a query)
    is success; anything containing ``ERR`` is a failure.

One command per TCP session (authenticate → send → close), each bounded by a
single ``asyncio.wait_for(timeout_s)`` so a hung projector can never stall the
caller. Errors raise `EscVpError` and are isolated per-device by the caller.
"""

from __future__ import annotations

import asyncio
import logging

from .base import DeviceDef, DisplayError

log = logging.getLogger(__name__)

DEFAULT_PORT = 3629

# Fail-fast retry, like PJLinkDriver. Only transport/timeout/busy faults retry;
# deterministic protocol errors (auth, request-not-allowed, ERR replies) are
# raised immediately so an unreachable/misconfigured unit can't burn the budget.
_RETRY_DELAYS = (1, 3)

_HELLO_ID = b"ESC/VP.net"
_STATUS_OK = 0x20
_STATUS_PW_REQUIRED = 0x41
_STATUS_PW_WRONG = 0x43
_STATUS_BUSY = 0x53


class EscVpError(DisplayError):
    """ESC/VP.net transport / protocol / auth failure.

    `retryable` distinguishes transient faults (timeout, transport, BUSY) from
    deterministic ones (auth, request-not-allowed, ERR reply) so the retry loop
    doesn't waste the budget re-trying a hard failure.
    """

    def __init__(self, message: str, *, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


class EscVpDriver:
    def __init__(self, dev: DeviceDef, *, timeout_s: float = 5.0, dry_run: bool = False):
        self.dev = dev
        self.timeout_s = timeout_s
        self.dry_run = dry_run
        self.host = dev.host
        self.port = dev.escvp_port or DEFAULT_PORT
        self.password = dev.escvp_password

    # ------------------- public API -------------------

    async def send_command(self, escvp_cmd: str) -> None:
        """Send one ESC/VP21 command (e.g. ``"BRIGHT 0"``). Raises EscVpError
        on hard failure.

        Rejects CR/LF in the command so a malformed config value can't smuggle
        a second command onto the wire.
        """
        if "\r" in escvp_cmd or "\n" in escvp_cmd:
            raise ValueError(
                f"ESC/VP21 command must not contain CR/LF: {escvp_cmd!r}"
            )
        if self.dry_run:
            log.info("[dry_run] ESC/VP %s: would send %r", self.dev.label(), escvp_cmd)
            return
        attempts = len(_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            last_attempt = attempt == attempts - 1
            try:
                await self._command(escvp_cmd)
            except EscVpError as e:
                if last_attempt or not e.retryable:
                    raise
                log.warning("ESC/VP %s: %r attempt %d failed: %s; retry in %ds",
                            self.dev.label(), escvp_cmd, attempt + 1, e,
                            _RETRY_DELAYS[attempt])
                await asyncio.sleep(_RETRY_DELAYS[attempt])
                continue
            log.info("ESC/VP %s: %r OK", self.dev.label(), escvp_cmd)
            return

    # ------------------- protocol primitives -------------------

    async def _command(self, escvp_cmd: str) -> None:
        """Bound the whole connect→handshake→send→read→close cycle by one
        wait_for. Raises EscVpError on any failure."""
        try:
            await asyncio.wait_for(self._exchange(escvp_cmd), timeout=self.timeout_s)
        except asyncio.TimeoutError as e:
            raise EscVpError(
                f"{self.dev.label()}: timeout after {self.timeout_s}s"
            ) from e
        except EscVpError:
            raise
        except Exception as e:  # OSError, IncompleteReadError, ...
            raise EscVpError(f"{self.dev.label()}: transport error: {e}") from e

    async def _exchange(self, escvp_cmd: str) -> None:
        reader, writer = await asyncio.open_connection(self.host, self.port)
        try:
            await self._handshake(reader, writer)
            writer.write(f"{escvp_cmd}\r".encode("ascii"))
            await writer.drain()
            resp = await reader.readuntil(b":")
            self._check_reply(escvp_cmd, resp)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _handshake(self, reader, writer) -> None:
        pw = self.password.encode("ascii")
        if pw:
            hello = _HELLO_ID + bytes([0x10, 0x03, 0x00, 0x00, 0x00, 0x01])
            hello += bytes([0x01, 0x01]) + pw[:16].ljust(16, b"\x00")
        else:
            hello = _HELLO_ID + bytes([0x10, 0x03, 0x00, 0x00, 0x00, 0x00])
        writer.write(hello)
        await writer.drain()
        head = await reader.readexactly(16)
        status, nhdr = head[14], head[15]
        if nhdr:
            await reader.readexactly(nhdr * 18)  # drain trailing headers
        if status == _STATUS_OK:
            return
        if status == _STATUS_BUSY:
            raise EscVpError(f"{self.dev.label()}: ESC/VP.net busy (0x53)")
        if status == _STATUS_PW_REQUIRED:
            raise EscVpError(
                f"{self.dev.label()}: ESC/VP.net password required (0x41)",
                retryable=False,
            )
        if status == _STATUS_PW_WRONG:
            raise EscVpError(
                f"{self.dev.label()}: ESC/VP.net wrong password (0x43)",
                retryable=False,
            )
        raise EscVpError(
            f"{self.dev.label()}: ESC/VP.net handshake refused (status 0x{status:02x})",
            retryable=False,
        )

    def _check_reply(self, escvp_cmd: str, resp: bytes) -> None:
        text = resp.decode("ascii", "replace").strip()
        # Success is a bare ':' (set) or 'VERB=value:' (query). A failure is
        # 'ERR :' — must scan the buffer BEFORE the colon, not treat the first
        # ':' as success.
        if "ERR" in text.upper():
            raise EscVpError(
                f"{self.dev.label()}: ESC/VP21 {escvp_cmd!r} returned {text!r}",
                retryable=False,
            )
