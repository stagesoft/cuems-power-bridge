# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Byte relay that lets a node receive an NDI source it cannot reach itself
(plan 869ekxuez rev 9, §3.6).

A node only reaches its own cluster segment. The controller also sits on the
venue LAN and on its WiFi AP, so it can reach a laptop there. With the node's
NDI receive forced to the base TCP connection (videocomposer >= 0.1.2-8,
"NDI receive transport: base TCP"), the whole stream is one TCP connection,
and this relay copies it untouched: no decode, no re-encode.

What it does NOT do: route, bridge or NAT anything. It listens on the
controller's cluster-side address, accepts only the target node's address
(binding alone is no boundary: the controller answers ARP for that address on
every interface), forwards to one source, and lives as long as one preview.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Callable

log = logging.getLogger(__name__)

READ_SIZE = 256 * 1024
CONNECT_TIMEOUT_S = 2.0
MAX_CONNECTIONS = 4
IDLE_CLOSE_S = 30.0      # no node connection for this long -> close
WATCHDOG_TICK_S = 1.0


class RelayError(Exception):
    """The relay could not be set up; `reason` is the HTTP reason token."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


def _tune(sock: socket.socket | None) -> None:
    """Low latency, and give up on a peer that vanished within ~10 s instead
    of the kernel's two hours, so a dead sender frees its slot."""
    if sock is None:
        return
    for level, opt, val in (
        (socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),
        (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPIDLE", None), 5),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPINTVL", None), 2),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPCNT", None), 3),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_USER_TIMEOUT", None), 10000),
    ):
        if opt is None:
            continue
        try:
            sock.setsockopt(level, opt, val)
        except OSError:
            pass


class Relay:
    """One relay: node <- controller:<port> <- upstream source."""

    def __init__(self, node_addr: str, upstream: str, listen_ip: str,
                 should_close: Callable[[], str | None] | None = None,
                 max_connections: int = MAX_CONNECTIONS,
                 idle_close_s: float = IDLE_CLOSE_S):
        host, _, port = upstream.rpartition(":")
        if not host or not port.isdigit():
            raise RelayError("relay_bad_upstream", upstream)
        self.node_addr = node_addr
        self.upstream = upstream
        self._up_host, self._up_port = host, int(port)
        self.listen_ip = listen_ip
        self.port: int | None = None
        self.state = "new"            # new | listening | connected | stalled | closed
        self.close_reason: str | None = None
        self.bytes_to_node = 0
        self.bytes_to_source = 0
        self.connections = 0          # accepted from the node, ever
        self.refused = 0              # peers that were not the node, or over the cap
        self.upstream_errors = 0
        self._active = 0
        self._last_byte = 0.0
        self._last_activity = time.monotonic()
        self._rate_mark = (time.monotonic(), 0)
        self._mbps = 0.0
        self._should_close = should_close
        self._max = max_connections
        self._idle_close_s = idle_close_s
        self._server: asyncio.base_events.Server | None = None
        self._tasks: set[asyncio.Task] = set()
        self._watchdog: asyncio.Task | None = None

    # ---------------- lifecycle ----------------

    async def start(self) -> None:
        """Check the upstream answers, then listen. Raises RelayError."""
        try:
            r, w = await asyncio.wait_for(
                asyncio.open_connection(self._up_host, self._up_port), CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as e:
            raise RelayError("relay_upstream_unreachable", f"{self.upstream} ({e or 'timeout'})")
        w.close()
        try:
            await w.wait_closed()
        except OSError:
            pass
        try:
            self._server = await asyncio.start_server(self._on_client, self.listen_ip, 0)
        except OSError as e:
            raise RelayError("relay_listen_failed", f"{self.listen_ip}: {e}")
        self.port = self._server.sockets[0].getsockname()[1]
        self.state = "listening"
        self._last_activity = time.monotonic()
        self._watchdog = asyncio.create_task(self._watch(), name=f"ndi-relay-watchdog:{self.port}")
        log.info("ndi-relay: listening on %s:%d for %s -> %s", self.listen_ip, self.port,
                 self.node_addr, self.upstream)

    @property
    def address(self) -> str:
        return f"{self.listen_ip}:{self.port}"

    async def close(self, reason: str) -> None:
        if self.state == "closed":
            return
        self.state = "closed"
        self.close_reason = reason
        if self._server is not None:
            self._server.close()
        if self._watchdog is not None and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("ndi-relay: closed %s (%s): %d connection(s), %.1f MB to node, %d refused",
                 self.address if self.port else self.listen_ip, reason, self.connections,
                 self.bytes_to_node / 1e6, self.refused)

    async def _watch(self) -> None:
        try:
            while self.state != "closed":
                await asyncio.sleep(WATCHDOG_TICK_S)
                why = self._should_close() if self._should_close else None
                if why:
                    await self.close(why)
                    return
                now = time.monotonic()
                if self._active == 0 and now - self._last_activity > self._idle_close_s:
                    await self.close("idle")
                    return
                if self._active and self.bytes_to_node and now - self._last_byte > 5.0:
                    self.state = "stalled"
                t0, b0 = self._rate_mark
                if now - t0 >= 2.0:
                    self._mbps = (self.bytes_to_node - b0) * 8 / (now - t0) / 1e6
                    self._rate_mark = (now, self.bytes_to_node)
        except asyncio.CancelledError:
            pass

    # ---------------- connections ----------------

    async def _on_client(self, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        peer = (cw.get_extra_info("peername") or ("?",))[0]
        if peer != self.node_addr or self._active >= self._max or self.state == "closed":
            self.refused += 1
            log.warning("ndi-relay %s: refused %s (%s)", self.address, peer,
                        "not the target node" if peer != self.node_addr else "connection cap")
            cw.close()
            return
        try:
            ur, uw = await asyncio.wait_for(
                asyncio.open_connection(self._up_host, self._up_port), CONNECT_TIMEOUT_S)
        except (OSError, asyncio.TimeoutError) as e:
            self.upstream_errors += 1
            log.warning("ndi-relay %s: upstream %s unreachable: %s", self.address, self.upstream, e)
            cw.close()
            return
        _tune(cw.get_extra_info("socket"))
        _tune(uw.get_extra_info("socket"))
        self._active += 1
        self.connections += 1
        self.state = "connected"
        log.info("ndi-relay %s: %s connected (%d active)", self.address, peer, self._active)
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await asyncio.gather(self._pipe(cr, uw, to_node=False), self._pipe(ur, cw, to_node=True))
        finally:
            self._active -= 1
            self._last_activity = time.monotonic()
            for w in (cw, uw):
                w.close()
            if task is not None:
                self._tasks.discard(task)
            if self._active == 0 and self.state != "closed":
                self.state = "listening"

    async def _pipe(self, r: asyncio.StreamReader, w: asyncio.StreamWriter, to_node: bool) -> None:
        try:
            while True:
                data = await r.read(READ_SIZE)
                if not data:
                    break
                w.write(data)
                if to_node:
                    self.bytes_to_node += len(data)
                    self._last_byte = time.monotonic()
                    if self.state == "stalled":
                        self.state = "connected"
                else:
                    self.bytes_to_source += len(data)
                await w.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                w.close()
            except OSError:
                pass

    # ---------------- reporting ----------------

    def stats(self, with_upstream: bool = True) -> dict:
        age = round(time.monotonic() - self._last_byte, 1) if self._last_byte else None
        out = {"state": self.state, "listen": self.address if self.port else None,
               "node": self.node_addr, "connections": self.connections, "active": self._active,
               "refused": self.refused, "mbit_s": round(self._mbps, 1),
               "last_byte_age_s": age, "closed_reason": self.close_reason}
        if with_upstream:
            out.update({"upstream": self.upstream, "bytes_to_node": self.bytes_to_node,
                        "bytes_to_source": self.bytes_to_source,
                        "upstream_errors": self.upstream_errors})
        return out
