# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Engine WebSocket OSC client: status cache + reconnect/UNKNOWN.

Subscribes to /engine/status/* by virtue of being connected — the engine
broadcasts to every connected client. On connect, the engine sends a full
state dump (see ControllerEngine._on_ws_client_connect at line 746) so we
get `running`, `armed`, `load`, `nextcue` immediately.

The `load` field carries the project's **unix_name** (filesystem dir),
not its UUID. Only test it for empty/non-empty — never UUID equality.

Bridge guarantees:
- `running == "yes"` is what blocks shutdown (loaded-but-stopped is fine).
- Disconnect (engine restart, network blip) → state goes UNKNOWN.
- Reconnect with exponential backoff (1, 2, 4, 8, 16s cap).
- /shutdown returns 503 while UNKNOWN.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed
from pythonosc.osc_message_builder import OscMessageBuilder

from .osc_parse import parse_osc_message

log = logging.getLogger(__name__)

UNKNOWN = "unknown"


class EngineClient:
    """Persistent binary-OSC WebSocket client to cuems-controller-engine."""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        # Status cache. Values default to UNKNOWN until first broadcast.
        self.running: str = UNKNOWN  # "yes" | "no" | UNKNOWN
        self.armed: str = UNKNOWN    # "yes" | "no" | UNKNOWN
        self.load: str = UNKNOWN     # "" (empty == no project) | <unix_name> | UNKNOWN
        self.nextcue: str = UNKNOWN
        self.connected: bool = False
        # Listeners called on every status update: cb(key, value).
        self._listeners: list[Callable[[str, Any], None]] = []
        # Listeners called once each time an established connection drops.
        self._disconnect_listeners: list[Callable[[], None]] = []
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    def on_status(self, cb: Callable[[str, Any], None]) -> None:
        """Register a listener; called every time a /engine/status/* arrives."""
        self._listeners.append(cb)

    def on_disconnect(self, cb: Callable[[], None]) -> None:
        """Register a listener fired once when an established WS connection
        drops (not on failed connect attempts). Lets callers re-arm
        connection-scoped state so a reconnect's status dump is treated as
        fresh."""
        self._disconnect_listeners.append(cb)

    def is_known(self) -> bool:
        """True iff connected AND we have the on-connect state dump."""
        return self.connected and self.running != UNKNOWN

    def project_running(self) -> bool:
        """True iff running == 'yes'. UNKNOWN returns False (caller must
        gate via is_known() before relying on this)."""
        return self.running == "yes"

    def project_loaded(self) -> bool:
        """True iff load is non-empty (and not UNKNOWN). UUID equality
        intentionally NOT supported — broadcast carries unix_name."""
        return self.load not in ("", UNKNOWN)

    def fully_ready(self) -> bool:
        """True iff a project is loaded AND armed=='yes'.

        The auto-load gate requires THIS, not merely project_loaded(): a
        loaded-but-unarmed project (nodes excluded at load time, or not yet
        armed within the engine's 120 s watchdog) must be treated as
        not-yet-ready so the bridge keeps driving / confirming."""
        return self.project_loaded() and self.armed == "yes"

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="engine-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send_osc(self, address: str, value: Any = None) -> bool:
        """Send a binary-OSC frame. Returns True if sent, False if not connected."""
        if self._ws is None or not self.connected:
            log.warning("send_osc(%s): not connected, dropping", address)
            return False
        builder = OscMessageBuilder(address)
        if value is None:
            # Impulse: no arg (engine accepts impulse-typed GO/STOP).
            # For commands like /load that take a string, caller passes value=str.
            pass
        elif isinstance(value, bool):
            builder.add_arg(value)
        elif isinstance(value, (int, float)):
            builder.add_arg(value)
        elif isinstance(value, str):
            builder.add_arg(value)
        else:
            builder.add_arg(str(value))
        try:
            msg = builder.build()
            await self._ws.send(msg.dgram)
            return True
        except ConnectionClosed:
            log.warning("send_osc(%s): connection closed mid-send", address)
            return False
        except Exception as e:
            log.error("send_osc(%s) failed: %s", address, e)
            return False

    async def _run_loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, max_size=2**20) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 1  # reset on successful connection
                    log.info("engine WS connected: %s", self.ws_url)
                    await self._consume(ws)
            except (ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.info("engine WS disconnected (%s); reconnect in %ds",
                         type(e).__name__, backoff)
            except Exception as e:
                log.error("engine WS error: %s; reconnect in %ds", e, backoff)
            finally:
                self._ws = None
                # Mark cache UNKNOWN on disconnect — safety guard for /shutdown.
                was_connected = self.connected
                self.connected = False
                self.running = UNKNOWN
                self.armed = UNKNOWN
                self.load = UNKNOWN
                self.nextcue = UNKNOWN
                # Fire disconnect listeners only on a real drop (we were
                # connected), not on failed connect attempts.
                if was_connected:
                    for cb in self._disconnect_listeners:
                        try:
                            cb()
                        except Exception as e:
                            log.error("disconnect listener crashed: %s", e)
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break  # stop signaled during wait
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 16)

    async def _consume(self, ws) -> None:
        async for msg in ws:
            if not isinstance(msg, (bytes, bytearray)):
                # Engine may emit JSON for OSCQuery — ignore.
                continue
            parsed = parse_osc_message(bytes(msg))
            if parsed is None:
                continue
            address, args = parsed
            value = args[0] if args else None
            if address.startswith("/engine/status/"):
                key = address[len("/engine/status/"):]
                # Top-level fields are flat strings; nested paths (cue/...,
                # cue_enabled/..., audio/...) we don't track in the cache.
                if key in ("running", "armed", "load", "nextcue"):
                    setattr(self, key, str(value) if value is not None else "")
                    log.debug("engine status %s=%r", key, value)
                for cb in self._listeners:
                    try:
                        cb(key, value)
                    except Exception as e:
                        log.error("status listener crashed: %s", e)
