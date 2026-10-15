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
import json
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
        # Cue names of the loaded script, {uuid: name}, from
        # /engine/status/cue_name/<uuid> (cuems-engine >= 869fedahu). The engine
        # sends them AFTER /engine/status/load, so a change of `load` empties
        # the table and the burst that follows refills it; emptied on
        # disconnect too (the late-join dump re-sends them). Empty against an
        # older engine.
        self.cue_names: dict[str, str] = {}
        # True only for an arm that followed a LOAD on this connection, with no
        # run since (plan 869fghmxn D15): a `load` frame with a name, then an
        # `armed` frame "yes" after a non-"yes" value. The engine's re-arm
        # after STOP (no `load` frame) is not fresh, nor is a reconnect dump
        # (it sends `armed` before `load`). Cleared by running=="yes", by any
        # further `load` frame and by a disconnect.
        self.fresh_arm: bool = False
        self._pending_load_arm: bool = False
        # Last /engine/status/cluster_warning (cuems-engine rc_1+ only), parsed:
        # {"load_id", "project", "missing", "unreachable"}. None until one is
        # seen on this connection; reset by `load` frames and on disconnect.
        # `cluster_warning_seen` tells an engine that publishes it (it always
        # does, even with empty lists) from a released one that never does.
        self.cluster_warning: dict | None = None
        self.cluster_warning_seen: bool = False
        self.connected: bool = False
        # Listeners called on every status update: cb(key, value).
        self._listeners: list[Callable[[str, Any], None]] = []
        # Listeners called once each time an established connection drops.
        self._disconnect_listeners: list[Callable[[], None]] = []
        # Raw-frame listeners, cb(address, raw_bytes), for every
        # /engine/status/* frame BEFORE the cache and on_status see it (the
        # Companion relay forwards the exact bytes); connect listeners, cb(),
        # once per established connection, before its first frame.
        self._frame_listeners: list[Callable[[str, bytes], None]] = []
        self._connect_listeners: list[Callable[[], None]] = []
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

    def on_frame(self, cb: Callable[[str, bytes], None]) -> None:
        """Register a listener called with (address, raw bytes) for every
        /engine/status/* frame, in arrival order, before the cache is updated
        and before the on_status listeners run. It must not block."""
        self._frame_listeners.append(cb)

    def on_connect(self, cb: Callable[[], None]) -> None:
        """Register a listener fired once each time a WS connection is
        established, before the first frame of that connection."""
        self._connect_listeners.append(cb)

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
                    for cb in self._connect_listeners:
                        try:
                            cb()
                        except Exception as e:
                            log.error("connect listener crashed: %s", e)
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
                self.cue_names = {}
                self.fresh_arm = False
                self._pending_load_arm = False
                self.cluster_warning = None
                self.cluster_warning_seen = False
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
            raw = bytes(msg)
            parsed = parse_osc_message(raw)
            if parsed is None:
                continue
            address, args = parsed
            value = args[0] if args else None
            if address.startswith("/engine/status/"):
                for cb in self._frame_listeners:
                    try:
                        cb(address, raw)
                    except Exception as e:
                        log.error("frame listener crashed: %s", e)
                self._apply_status(address[len("/engine/status/"):], value)

    def _apply_status(self, key: str, value: Any) -> None:
        """Update the cache from one /engine/status/<key> frame, then tell the
        listeners (they read the updated cache)."""
        # Top-level fields are flat strings; nested paths (cue/...,
        # cue_enabled/..., audio/...) we don't track in the cache.
        if key == "load":
            new_load = str(value) if value is not None else ""
            if new_load != self.load:
                # New load (or none): new name table. The engine's
                # cue_name burst follows this message.
                self.cue_names = {}
            self._pending_load_arm = new_load != ""
            self.fresh_arm = False
            self.cluster_warning = None
        elif key == "armed":
            new_armed = str(value) if value is not None else ""
            if (new_armed == "yes" and self.armed != "yes"
                    and self._pending_load_arm):
                self.fresh_arm = True
                self._pending_load_arm = False
        elif key == "running" and value == "yes":
            self.fresh_arm = False
            self._pending_load_arm = False
        elif key == "cluster_warning":
            self.cluster_warning_seen = True
            self.cluster_warning = _parse_cluster_warning(value)
        if key in ("running", "armed", "load", "nextcue"):
            setattr(self, key, str(value) if value is not None else "")
            log.debug("engine status %s=%r", key, value)
        elif key.startswith("cue_name/"):
            self.cue_names[key[len("cue_name/"):]] = (
                str(value) if value is not None else ""
            )
        for cb in self._listeners:
            try:
                cb(key, value)
            except Exception as e:
                log.error("status listener crashed: %s", e)

def _parse_cluster_warning(value: Any) -> dict | None:
    """The engine sends the load diagnosis as a JSON string (OSC carries no
    lists). Anything unparseable is treated as no diagnosis."""
    if not isinstance(value, str) or not value:
        return None
    try:
        obj = json.loads(value)
    except ValueError:
        log.debug("unparseable cluster_warning: %r", value[:120])
        return None
    return obj if isinstance(obj, dict) else None
