# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Editor WebSocket client (JSON protocol).

Carries the bridge's project operations: `project_ready` (load, by project
UUID), `project_unload` and `project_list`. The editor looks the UUID up in its
sqlite DB, refreshes media, then IPCs the engine via /tmp/editor.ipc.

Requests are matched to replies through `register()` + `send_registered()`
(or `request()`), never by "the last frame of that type":

- A request is registered BEFORE its frame is sent, so a fast reply can't
  slip past it.
- `project_ready` and `project_unload` are **single-flight**: the editor runs
  a session's requests concurrently (three processor tasks per socket, a new
  NNG socket per request), so a later unload can overtake a slow load and
  reach the engine first. Only once the editor has answered is the order
  fixed. A second one while either is outstanding raises `RequestPending`.
- The slot is freed by the reply or by a disconnect, never by a waiter's
  timeout: a request that timed out stays registered until the editor
  answers, so a late reply can never be taken for a later request's.
- A failed or cancelled send unregisters the request (the slot is freed).
- On disconnect every pending request fails with `EditorDisconnected`.

Editor frames: `{"type": "<action>", "value": ...}` on success,
`{"type": "error", "action": "<action>", ["uuid": ...,] "value": msg}` on
failure. `project_ready` replies carry the project UUID (`value` on success,
`uuid` on error), so they are matched by UUID as well.

Localhost-only binding on the editor side — see CuemsWsServer.py:119.
No authentication; loopback is the trust boundary.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)

# Actions that reach the engine and must not overtake each other.
SINGLE_FLIGHT = ("project_ready", "project_unload")
# Actions whose concurrent waiters may all share one reply (equivalent data).
FAN_OUT = ("project_list",)


class EditorDisconnected(Exception):
    """The editor socket closed while the request was pending."""


class RequestPending(Exception):
    """A single-flight request is still waiting for the editor's reply."""

    def __init__(self, pending: "EditorRequest"):
        super().__init__(f"{pending.action} pending for {pending.age_s():.1f}s")
        self.pending = pending


@dataclass(eq=False)
class EditorRequest:
    action: str
    value: Any
    future: asyncio.Future
    match: Callable[[dict], bool] | None = None
    registered_at: float = field(default_factory=time.monotonic)

    def age_s(self) -> float:
        return time.monotonic() - self.registered_at


def _swallow(fut: asyncio.Future) -> None:
    """Retrieve a failed future's exception so asyncio never logs
    'exception was never retrieved' for a request nobody is waiting on."""
    if not fut.cancelled():
        fut.exception()


def match_project_ready(uuid: str) -> Callable[[dict], bool]:
    """A `project_ready` reply belongs to the request for `uuid`: success
    echoes it as `value`, an error carries it as `uuid`."""
    want = uuid.lower()

    def _m(obj: dict) -> bool:
        if obj.get("type") == "error":
            return str(obj.get("uuid", "")).lower() == want
        return str(obj.get("value", "")).lower() == want
    return _m


class EditorClient:
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self.connected: bool = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._pending: list[EditorRequest] = []

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
        self._fail_pending("editor client stopped")

    # ------------------- requests -------------------

    def outstanding(self) -> EditorRequest | None:
        """The single-flight request still waiting for its reply, if any."""
        for req in self._pending:
            if req.action in SINGLE_FLIGHT:
                return req
        return None

    def register(self, action: str, value: Any = None, *,
                 match: Callable[[dict], bool] | None = None) -> EditorRequest:
        """Register a request before its frame is sent. Synchronous: callers
        re-check their own preconditions and register with no await between.
        Raises RequestPending for a second single-flight request."""
        if action in SINGLE_FLIGHT:
            pending = self.outstanding()
            if pending is not None:
                raise RequestPending(pending)
        fut = asyncio.get_running_loop().create_future()
        fut.add_done_callback(_swallow)
        req = EditorRequest(action=action, value=value, future=fut, match=match)
        self._pending.append(req)
        return req

    def unregister(self, req: EditorRequest) -> None:
        """Drop a request whose frame never went out (frees the slot)."""
        if req in self._pending:
            self._pending.remove(req)
        if not req.future.done():
            req.future.cancel()

    async def send_registered(self, req: EditorRequest) -> bool:
        """Send a registered request's frame. False (and unregistered) if it
        could not be sent; the request stays registered only once sent."""
        try:
            ok = await self.send_action(req.action, req.value)
        except BaseException:
            self.unregister(req)
            raise
        if not ok:
            self.unregister(req)
        return ok

    async def request(self, action: str, value: Any = None, *,
                      match: Callable[[dict], bool] | None = None
                      ) -> EditorRequest | None:
        """register() + send_registered(). None if the frame was not sent."""
        req = self.register(action, value, match=match)
        return req if await self.send_registered(req) else None

    @staticmethod
    async def wait(req: EditorRequest, timeout: float) -> dict | None:
        """Wait for the reply. None on timeout — the request stays registered
        (its slot held) until the editor answers or the socket drops. Raises
        EditorDisconnected if the socket dropped first."""
        try:
            return await asyncio.wait_for(asyncio.shield(req.future), timeout)
        except asyncio.TimeoutError:
            return None

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

    def close_socket(self, why: str) -> None:
        """Drop the editor socket (it reconnects by itself). Used when a
        single-flight request has gone unanswered past the operation budget:
        the reconnect fails it and frees the slot."""
        ws = self._ws
        if ws is None:
            return
        log.warning("editor WS: closing the socket (%s); it reconnects and "
                    "every pending request fails", why)
        asyncio.get_running_loop().create_task(ws.close())

    def _fail_pending(self, why: str) -> None:
        pending, self._pending = self._pending, []
        for req in pending:
            if not req.future.done():
                req.future.set_exception(EditorDisconnected(why))

    def _resolve(self, obj: dict) -> None:
        """Hand a reply to the request it belongs to (oldest match first);
        `project_list` replies go to every pending list request."""
        if obj.get("type") == "error":
            key = obj.get("action")
            if not key:
                log.warning("editor error frame without action: %r",
                            str(obj.get("value", ""))[:200])
                return
        else:
            key = obj.get("type")
        if not key:
            return
        hits = [r for r in self._pending
                if r.action == key and (r.match is None or r.match(obj))]
        if not hits:
            log.debug("editor reply with no pending request: %s", key)
            return
        if key not in FAN_OUT:
            hits = hits[:1]
        for req in hits:
            self._pending.remove(req)
            if not req.future.done():
                req.future.set_result(obj)

    # ------------------- connection -------------------

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
                # The editor cancels a closed session's in-flight requests, so
                # none of them will ever be answered on a new socket.
                self._fail_pending("editor socket closed")
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
            if isinstance(obj, dict):
                self._resolve(obj)
