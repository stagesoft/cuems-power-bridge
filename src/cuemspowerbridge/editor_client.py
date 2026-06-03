# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Editor WebSocket client (JSON protocol).

Used for the auto-load path: `{"action": "project_ready", "value": "<uuid>"}`
matches what wsclient.py sends today. Editor lookups uuid → unix_name in
its sqlite DB, then IPCs the engine via /tmp/editor.ipc.

The bridge primarily relies on the engine channel's `/engine/status/load`
to detect load completion (the broadcast fires mid-load_project, before
the editor's ack returns). The editor's response frame is still useful
for **fail-fast on bad UUID**: editor returns
`{"type": "error", "action": "project_ready"}` immediately when the UUID
isn't in the DB.

Localhost-only binding on the editor side — see CuemsWsServer.py:119.
No authentication; loopback is the trust boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)


class EditorClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.connected: bool = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        # Last seen response for a given action — bridge polls this for
        # fail-fast on `{"type": "error", "action": "project_ready"}`.
        self._last_responses: dict[str, dict] = {}
        self._response_event = asyncio.Event()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop(), name="editor-ws")

    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except asyncio.TimeoutError:
                self._task.cancel()

    async def send_action(self, action: str, value) -> bool:
        """Send {"action": ..., "value": ...} JSON. Returns False if not connected."""
        if self._ws is None or not self.connected:
            log.warning("send_action(%s): editor WS not connected", action)
            return False
        payload = json.dumps({"action": action, "value": value})
        try:
            await self._ws.send(payload)
            return True
        except ConnectionClosed:
            log.warning("send_action(%s): connection closed mid-send", action)
            return False

    async def wait_for_response(
        self, action: str, timeout: float
    ) -> dict | None:
        """Wait for a response with matching `action` field within `timeout`.

        Returns the response dict, or None on timeout. Clears any prior
        response for this action first so we wait for a fresh one.
        """
        self._last_responses.pop(action, None)
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            if action in self._last_responses:
                return self._last_responses.pop(action)
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            self._response_event.clear()
            try:
                await asyncio.wait_for(self._response_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

    async def _run_loop(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                async with websockets.connect(self.ws_url, max_size=2**20) as ws:
                    self._ws = ws
                    self.connected = True
                    backoff = 1
                    log.info("editor WS connected: %s", self.ws_url)
                    await self._consume(ws)
            except (ConnectionClosed, ConnectionRefusedError, OSError) as e:
                log.info("editor WS disconnected (%s); reconnect in %ds",
                         type(e).__name__, backoff)
            except Exception as e:
                log.error("editor WS error: %s; reconnect in %ds", e, backoff)
            finally:
                self._ws = None
                self.connected = False
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                break
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 16)

    async def _consume(self, ws) -> None:
        async for msg in ws:
            if not isinstance(msg, str):
                continue
            try:
                obj = json.loads(msg)
            except json.JSONDecodeError:
                log.debug("editor sent non-JSON: %s", msg[:80])
                continue
            # Editor responses look like {"type": "<action>", "value": ...}
            # or {"type": "error", "action": "<action>", ...}. Bridge keys
            # by either "type" (success path) or "action" (error path).
            key = None
            if obj.get("type") == "error" and obj.get("action"):
                key = obj["action"]
            elif "type" in obj:
                key = obj["type"]
            if key:
                self._last_responses[key] = obj
                self._response_event.set()
                log.debug("editor response stored: key=%s", key)
