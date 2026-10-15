# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Relay the engine's /engine/status/* frames to Companion over UDP OSC.

Bitfocus Companion's Generic OSC module can only listen on UDP/TCP, while the
engine publishes cue state as binary OSC over its WebSocket (:9190). The
bridge already holds that WebSocket, so it forwards what Companion needs
(plan cuems-RELATIONS/Plans/2026-10-09-power-bridge-companion-cue-status-relay.md):

- **Forwarded unchanged**: frames whose key matches ``osc_relay_keys``
  (prefix if the entry ends in "/", exact otherwise), byte for byte.
- **Hold / commit**: per-cue frames (``cue/``, ``cue_enabled/``,
  ``cue_name/``) are held while no project is known to be loaded, and
  committed in order when a non-empty ``load`` arrives. Both engine load
  orders (released ≤ rc6-3, and rc_1 with ``load ""`` at the head) work.
- **Synthesised resets**: ``cue/<uuid> = 0`` for every committed cue when
  ``load`` becomes "" and when the engine link drops. Generic OSC keeps a
  path's last value forever; the engine never re-sends per-cue state then.
- **Link state**: ``/cuems/bridge/engine_connected i 0|1``; a change jumps the
  queue, and the value is re-sent every 2 s as a heartbeat.
- **Edge pulses** (live only): ``/cuems/bridge/cue_started/<uuid>`` and
  ``/cuems/bridge/cue_ended/<uuid>``, each sent as 0 → 60 ms → 1 → 150 ms → 0
  so Companion's two debounces (≈45 ms worst case) see false, true, false.
- **Dump on request**: ``/cuems/bridge/dump`` on the listen socket re-sends
  the cache to the configured targets (never to the sender).

Everything is paced by ``osc_relay_rate`` datagrams per second. ``on_frame``
only enqueues, so the engine read loop never waits on the network. The relay
never sends anything to the engine.
"""

from __future__ import annotations

import asyncio
import heapq
import ipaddress
import logging
import math
import time
from collections import OrderedDict, deque
from typing import Any

from pythonosc.osc_message_builder import OscMessageBuilder

from . import config as _config
from .osc_parse import parse_osc_message

log = logging.getLogger(__name__)

STATUS_PREFIX = "/engine/status/"
LINK_PATH = "/cuems/bridge/engine_connected"
STARTED_PREFIX = "/cuems/bridge/cue_started/"
ENDED_PREFIX = "/cuems/bridge/cue_ended/"
DUMP_PATH = "/cuems/bridge/dump"

PER_CUE_PREFIXES = ("cue/", "cue_enabled/", "cue_name/")

DEFAULT_KEYS = "cue/,running,armed,load,nextcue"
DEFAULT_LISTEN = "127.0.0.1:8478"
DEFAULT_RATE = 1000
RATE_MIN, RATE_MAX = 100, 8000
REFRESH_MIN_S = 30.0

HEARTBEAT_S = 2.0
EDGE_SUPPRESS_S = 2.0          # after a (re)connect: the late-join dump is no edge
PULSE_GAP_S = 0.060            # leading 0 -> 1
PULSE_HOLD_S = 0.150           # 1 -> trailing 0
EVENT_CAP = 2 * 3 * 1000       # pending pulse sends
HOLD_CAP = 20000               # held per-cue frames
CACHE_CAP = 20000              # cached per-cue entries
DUMP_MIN_INTERVAL_S = 1.0
TICK_S = 0.002
TARGET_RECOVER_S = 5.0
CLOSE_DEADLINE_S = 0.5
RETRY_TARGET_S = 5.0

_LOAD_UNKNOWN = object()       # the relay's view of `load` before any frame


def _osc(address: str, *args: Any) -> bytes:
    b = OscMessageBuilder(address)
    for a in args:
        b.add_arg(a)
    return b.build().dgram


def _first_arg(raw: bytes) -> Any:
    parsed = parse_osc_message(raw)
    if parsed is None or not parsed[1]:
        return None
    return parsed[1][0]


def _playing(v: int | None) -> bool:
    return v is not None and 1 <= v <= 99


def _as_int(v: Any) -> int | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _warn_once(key: str, msg: str, *args: Any) -> None:
    # Same once-per-process dict as Config.op_budget_s().
    if not _config._warned.get(key):
        _config._warned[key] = True
        log.warning(msg, *args)


# ---------------------------------------------------------------------------
# Config parsing (the five keys are raw strings in Config; nothing here raises)
# ---------------------------------------------------------------------------

def _parse_hostport(text: str) -> tuple[str, int]:
    """'1.2.3.4:5' or '[::1]:5' → (host, port). Literal IPs only."""
    text = text.strip()
    if text.startswith("["):
        host, sep, port = text[1:].partition("]:")
        if not sep:
            raise ValueError(f"{text!r}: expected [v6]:port")
    else:
        host, sep, port = text.rpartition(":")
        if not sep:
            raise ValueError(f"{text!r}: expected IP:port")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError(f"{text!r}: host must be a literal IP (no names, no .local)")
    if not port.isdigit() or not (1 <= int(port) <= 65535):
        raise ValueError(f"{text!r}: port must be 1..65535")
    return (str(ip), int(port))


def parse_targets(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for item in (text or "").split(","):
        if item.strip():
            hp = _parse_hostport(item)
            if hp not in out:
                out.append(hp)
    return out


def parse_keys(text: str) -> list[str]:
    keys: list[str] = []
    for item in (text or "").split(","):
        k = item.strip()
        if k.startswith(STATUS_PREFIX):
            k = k[len(STATUS_PREFIX):]
        if not k:
            continue
        if any(c.isspace() for c in k) or k.startswith("/"):
            raise ValueError(f"bad key {item.strip()!r}")
        if k not in keys:
            keys.append(k)
    if not keys:
        raise ValueError("no keys")
    return keys


def parse_rate(text: str) -> int:
    """Never disables: non-number → default, out of range → clamped."""
    try:
        v = int(str(text).strip())
    except ValueError:
        _warn_once("osc_relay_rate", "config: osc_relay_rate=%r is not a number; "
                   "using %d", text, DEFAULT_RATE)
        return DEFAULT_RATE
    c = max(RATE_MIN, min(RATE_MAX, v))
    if c != v:
        _warn_once("osc_relay_rate", "config: osc_relay_rate=%d out of %d..%d; "
                   "using %d", v, RATE_MIN, RATE_MAX, c)
    return c


def parse_refresh(text: str) -> float:
    """0 = off; 1..29 → 30; non-number or negative → off. Never disables."""
    raw = str(text).strip()
    try:
        v = float(raw) if raw else 0.0
    except ValueError:
        v = math.nan
    if not math.isfinite(v) or v < 0:
        _warn_once("osc_relay_refresh_s", "config: osc_relay_refresh_s=%r is not "
                   "a usable number; refresh off", text)
        return 0.0
    if v == 0:
        return 0.0
    if v < REFRESH_MIN_S:
        _warn_once("osc_relay_refresh_s", "config: osc_relay_refresh_s=%r below "
                   "%d; using %d", text, REFRESH_MIN_S, REFRESH_MIN_S)
        return REFRESH_MIN_S
    return v


def unknown_relay_keys(cfg: Any) -> list[str]:
    """osc_relay* keys that landed in extras (typos)."""
    return sorted(k for k in getattr(cfg, "extras", {}) if k.startswith("osc_relay"))


# ---------------------------------------------------------------------------
# Sockets
# ---------------------------------------------------------------------------

class _Target(asyncio.DatagramProtocol):
    """One connected UDP endpoint: a connected socket is what reports ICMP
    errors (port unreachable) back to us."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.name = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        self.transport: asyncio.DatagramTransport | None = None
        self.sent = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_error_t = 0.0      # monotonic
        self.last_send: float | None = None  # wall clock
        self.state = "ok"
        self.next_attempt = 0.0

    def error_received(self, exc: Exception) -> None:
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.last_error_t = time.monotonic()
        if self.state != "error":
            self.state = "error"
            log.warning("osc relay: target %s: %s", self.name, self.last_error)

    def connection_lost(self, exc: Exception | None) -> None:
        self.transport = None

    def send(self, data: bytes) -> None:
        tr = self.transport
        if tr is None or tr.is_closing():
            return
        tr.sendto(data)
        self.sent += 1
        self.last_send = time.time()
        if self.state == "error" and time.monotonic() - self.last_error_t >= TARGET_RECOVER_S:
            self.state = "ok"
            log.info("osc relay: target %s recovered", self.name)


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, relay: "OscRelay"):
        self.relay = relay

    def datagram_received(self, data: bytes, addr: Any) -> None:
        try:
            parsed = parse_osc_message(data)
            if parsed is not None and parsed[0] == DUMP_PATH and not parsed[1]:
                self.relay.request_dump()
            else:
                self.relay.dumps_ignored += 1
        except Exception as e:  # never let a datagram kill the endpoint
            log.error("osc relay: listener: %s", e)


class _Pulse:
    __slots__ = ("id", "path", "stage", "dropped")

    def __init__(self, pid: int, path: str):
        self.id = pid
        self.path = path
        self.stage = 0       # next send: 0 → "0", 1 → "1", 2 → "0", 3 = done
        self.dropped = False


# ---------------------------------------------------------------------------
# The relay
# ---------------------------------------------------------------------------

class OscRelay:
    def __init__(
        self,
        targets: list[tuple[str, int]],
        *,
        keys: list[str] | None = None,
        listen: tuple[str, int] | None = None,
        rate: int = DEFAULT_RATE,
        refresh_s: float = 0.0,
        engine: Any = None,
        enabled: bool = True,
        last_error: str | None = None,
        unknown_keys: list[str] | None = None,
    ):
        self.enabled = enabled and bool(targets)
        self.degraded = False
        self.last_error = last_error
        self.unknown_keys = list(unknown_keys or [])
        self.keys = list(keys if keys is not None else parse_keys(DEFAULT_KEYS))
        self._exact = {k for k in self.keys if not k.endswith("/")}
        self._prefixes = tuple(k for k in self.keys if k.endswith("/"))
        self.listen = listen
        self.rate = rate
        self.refresh_s = refresh_s
        self.engine = engine
        self._targets = [_Target(h, p) for h, p in targets]
        self._listener_tr: asyncio.DatagramTransport | None = None

        # Engine-derived state
        self._load: Any = _LOAD_UNKNOWN
        self._connected = False
        self._suppress_until = 0.0
        self._held: list[tuple[str, str, bytes]] = []
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._cached_per_cue = 0
        self._cue_vals: dict[str, int] = {}     # committed cue/ values (edges, resets)

        # Lanes
        self._state: OrderedDict[str, bytes] = OrderedDict()
        self._heap: list[tuple[float, int, _Pulse]] = []
        self._seq = 0
        self._by_path: dict[str, deque[_Pulse]] = {}
        # Live pulses in creation order (drop-oldest); removed when done.
        self._order: dict[int, _Pulse] = {}
        self._pending_sends = 0
        self._wake = asyncio.Event()

        # Counters
        self.events_dropped = 0
        self.held_dropped = 0
        self.cache_dropped = 0
        self.dumps_served = 0
        self.dumps_ignored = 0
        self.edges_sent = 0
        self._last_dump = -1e9

        self._tasks: list[asyncio.Task] = []
        self._closing = False
        self._started = False

    # ------------------------------------------------------------ construction

    @classmethod
    def from_config(cls, cfg: Any, engine: Any = None) -> "OscRelay | None":
        """None when no targets are configured (the default). Never raises: a
        bad target/keys value returns a disabled relay carrying last_error; a
        bad listen value disables only the listener; rate/refresh clamp."""
        unknown = unknown_relay_keys(cfg)
        for k in unknown:
            log.warning("config: unknown key %r (osc relay typo?)", k)
        raw_targets = str(getattr(cfg, "osc_relay_targets", "") or "")
        if not raw_targets.strip():
            return None
        errors: list[str] = []
        try:
            targets = parse_targets(raw_targets)
        except ValueError as e:
            targets, errors = [], [f"osc_relay_targets: {e}"]
        try:
            keys = parse_keys(str(getattr(cfg, "osc_relay_keys", DEFAULT_KEYS)))
        except ValueError as e:
            keys = None
            errors.append(f"osc_relay_keys: {e}")
        rate = parse_rate(getattr(cfg, "osc_relay_rate", str(DEFAULT_RATE)))
        refresh = parse_refresh(getattr(cfg, "osc_relay_refresh_s", "0"))
        listen = None
        raw_listen = str(getattr(cfg, "osc_relay_listen", DEFAULT_LISTEN) or "").strip()
        if raw_listen:
            try:
                listen = _parse_hostport(raw_listen)
                if listen in targets:
                    raise ValueError(f"{raw_listen!r} is also a target")
            except ValueError as e:
                listen = None
                errors.append(f"osc_relay_listen: {e} (listener disabled)")
        enabled = bool(targets) and keys is not None
        last_error = "; ".join(errors) or None
        if last_error:
            log.error("osc relay: %s%s", last_error,
                      "" if enabled else " — relay DISABLED")
        return cls(targets, keys=keys, listen=listen, rate=rate, refresh_s=refresh,
                   engine=engine, enabled=enabled, last_error=last_error,
                   unknown_keys=unknown)

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        if not self.enabled or self._started:
            return
        self._started = True
        loop = asyncio.get_running_loop()
        for t in self._targets:
            await self._open_target(t)
        if self.listen is not None:
            try:
                self._listener_tr, _ = await loop.create_datagram_endpoint(
                    lambda: _Listener(self), local_addr=self.listen)
            except OSError as e:
                self._set_error(f"osc_relay_listen {self.listen[0]}:{self.listen[1]}: "
                                f"{e} (listener disabled)")
                self._listener_tr = None
        self._put_state(LINK_PATH, _osc(LINK_PATH, 0), head=True)
        self._spawn(self._drain(), "osc-relay-drain")
        self._spawn(self._heartbeat(), "osc-relay-heartbeat")
        if self.refresh_s > 0:
            self._spawn(self._refresh(), "osc-relay-refresh")
        log.info("osc relay: targets %s, listen %s, keys %s, rate %d/s, refresh %s",
                 ",".join(t.name for t in self._targets),
                 "%s:%d" % self.listen if self._listener_tr else "off",
                 ",".join(self.keys), self.rate,
                 f"{self.refresh_s:g}s" if self.refresh_s else "off")

    async def close(self) -> None:
        """Send the link state first, then the trailing 0 of every pulse whose
        1 is out, then flush the state lane within CLOSE_DEADLINE_S."""
        if not self._started or self._closing:
            return
        self._closing = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        link = self._state.pop(LINK_PATH, None) or _osc(LINK_PATH, int(self._connected))
        self._send(link)
        for dq in self._by_path.values():
            if dq and dq[0].stage == 2 and not dq[0].dropped:
                self._send(_osc(dq[0].path, 0))
                dq[0].stage = 3
        self._heap.clear()
        self._by_path.clear()
        self._order.clear()
        self._pending_sends = 0
        deadline = time.monotonic() + CLOSE_DEADLINE_S
        per_tick = max(1, int(self.rate * TICK_S))
        while self._state and time.monotonic() < deadline:
            for _ in range(per_tick):
                if not self._state:
                    break
                self._send(self._state.popitem(last=False)[1])
            await asyncio.sleep(TICK_S)
        if self._state:
            log.warning("osc relay: close: %d state frames not sent", len(self._state))
            self._state.clear()
        if self._listener_tr is not None:
            self._listener_tr.close()
        for t in self._targets:
            if t.transport is not None:
                t.transport.close()

    def _spawn(self, coro, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._task_done)
        self._tasks.append(task)

    def _task_done(self, task: asyncio.Task) -> None:
        if self._closing:
            return
        exc = None if task.cancelled() else task.exception()
        why = "cancelled" if task.cancelled() else f"{type(exc).__name__}: {exc}"
        self.degraded = True
        self._set_error(f"task {task.get_name()} died: {why}")

    def _set_error(self, msg: str) -> None:
        self.last_error = msg
        log.error("osc relay: %s", msg)

    async def _open_target(self, t: _Target) -> None:
        t.next_attempt = time.monotonic() + RETRY_TARGET_S
        try:
            tr, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: t, remote_addr=(t.host, t.port))
            t.transport = tr
        except OSError as e:
            t.error_received(e)

    # ----------------------------------------------------------- engine hooks

    def on_engine_connect(self) -> None:
        self._connected = True
        self._suppress_until = time.monotonic() + EDGE_SUPPRESS_S
        self._put_state(LINK_PATH, _osc(LINK_PATH, 1), head=True)

    def on_engine_disconnect(self) -> None:
        self._connected = False
        self._synth_resets()
        self._put_state(LINK_PATH, _osc(LINK_PATH, 0), head=True)
        self._cache.clear()
        self._cached_per_cue = 0
        self._held.clear()
        self._cue_vals.clear()
        self._load = _LOAD_UNKNOWN

    def on_frame(self, address: str, raw: bytes) -> None:
        """Called for every /engine/status/* frame, in arrival order. Only
        enqueues; never does I/O."""
        if not address.startswith(STATUS_PREFIX):
            return
        key = address[len(STATUS_PREFIX):]
        if key == "load":
            v = _first_arg(raw)
            new = "" if v is None else str(v)
            if new == "":
                self._synth_resets()
                self._drop_per_cue()
                self._held.clear()
            else:
                held, self._held = self._held, []
                for k, a, r in held:
                    self._commit(k, a, r, live=False)
            self._load = new
            self._forward(key, address, raw)
            return
        if key.startswith(PER_CUE_PREFIXES):
            if self._load is _LOAD_UNKNOWN or self._load == "":
                if len(self._held) >= HOLD_CAP:
                    self.held_dropped += 1
                    _warn_once("osc_relay_hold_cap", "osc relay: hold buffer full "
                               "(%d frames); dropping", HOLD_CAP)
                else:
                    self._held.append((key, address, raw))
            else:
                self._commit(key, address, raw, live=True)
            return
        self._forward(key, address, raw)

    # -------------------------------------------------------------- the rules

    def _matches(self, key: str) -> bool:
        return key in self._exact or key.startswith(self._prefixes)

    def _forward(self, key: str, address: str, raw: bytes) -> None:
        if not self._matches(key):
            return
        if address not in self._cache and key.startswith(PER_CUE_PREFIXES):
            if self._cached_per_cue >= CACHE_CAP:
                self.cache_dropped += 1
                _warn_once("osc_relay_cache_cap", "osc relay: cache full (%d "
                           "per-cue entries); not caching more", CACHE_CAP)
                self._put_state(address, raw)
                return
            self._cached_per_cue += 1
        self._cache[address] = raw
        self._put_state(address, raw)

    def _commit(self, key: str, address: str, raw: bytes, *, live: bool) -> None:
        if key.startswith("cue/"):
            uuid = key[4:]
            v = _as_int(_first_arg(raw))
            prev = self._cue_vals.get(uuid, 0)
            self._cue_vals[uuid] = v if v is not None else prev
            if live and v is not None and time.monotonic() >= self._suppress_until:
                if not _playing(prev) and _playing(v):
                    self._add_pulse(STARTED_PREFIX + uuid)
                elif _playing(prev) and v == 100:
                    self._add_pulse(ENDED_PREFIX + uuid)
        self._forward(key, address, raw)

    def _synth_resets(self) -> None:
        if self._matches("cue/"):
            for uuid in self._cue_vals:
                addr = f"{STATUS_PREFIX}cue/{uuid}"
                self._put_state(addr, _osc(addr, 0))

    def _drop_per_cue(self) -> None:
        for addr in [a for a in self._cache
                     if a[len(STATUS_PREFIX):].startswith(PER_CUE_PREFIXES)]:
            del self._cache[addr]
        self._cached_per_cue = 0
        self._cue_vals.clear()

    # ------------------------------------------------------------------- dump

    def request_dump(self) -> None:
        """From the listener: at most one per DUMP_MIN_INTERVAL_S. A dump that is
        still queued absorbs later ones (the state lane coalesces by address)."""
        now = time.monotonic()
        if now - self._last_dump < DUMP_MIN_INTERVAL_S:
            self.dumps_ignored += 1
            return
        self._last_dump = now
        self.dump()

    def dump(self) -> None:
        if not self.enabled:
            return
        names = getattr(self.engine, "cue_names", None) or {}
        self._put_state(LINK_PATH, _osc(LINK_PATH, int(self._connected)))
        for addr, raw in self._cache.items():
            key = addr[len(STATUS_PREFIX):]
            if names and key.startswith(PER_CUE_PREFIXES):
                if key.split("/", 1)[1] not in names:
                    continue
            self._put_state(addr, raw)
        self.dumps_served += 1

    # ------------------------------------------------------------------ lanes

    def _put_state(self, address: str, raw: bytes, *, head: bool = False) -> None:
        """Latest value wins. A re-queued address moves to the end, except the
        link state: a heartbeat or dump must not demote a queued change, which
        went to the head."""
        if address == LINK_PATH and address in self._state and not head:
            self._state[address] = raw
        else:
            self._state.pop(address, None)
            self._state[address] = raw
            if head:
                self._state.move_to_end(address, last=False)
        self._wake.set()

    def _add_pulse(self, path: str) -> None:
        while self._pending_sends + 3 > EVENT_CAP:
            if not self._drop_oldest_pulse():
                self.events_dropped += 1
                return
        self._seq += 1
        p = _Pulse(self._seq, path)
        self._order[p.id] = p
        self._pending_sends += 3
        dq = self._by_path.setdefault(path, deque())
        dq.append(p)
        if len(dq) == 1:
            self._schedule(p, time.monotonic())

    def _drop_oldest_pulse(self) -> bool:
        """Drop the oldest pulse whose 1 has not gone out (one whose 1 is out
        always keeps its trailing 0)."""
        for p in self._order.values():
            if p.stage < 2:
                self._drop(p)
                return True
        return False

    def _drop(self, p: _Pulse) -> None:
        p.dropped = True
        self._order.pop(p.id, None)
        self._pending_sends -= 3 - p.stage
        self.events_dropped += 1
        dq = self._by_path.get(p.path)
        if dq is None:
            return
        was_head = bool(dq) and dq[0] is p
        try:
            dq.remove(p)
        except ValueError:
            pass
        if not dq:
            del self._by_path[p.path]
        elif was_head:
            self._schedule(dq[0], time.monotonic())

    def _schedule(self, p: _Pulse, due: float) -> None:
        self._seq += 1
        heapq.heappush(self._heap, (due, self._seq, p))
        self._wake.set()

    def _send_event(self, p: _Pulse) -> None:
        value = 1 if p.stage == 1 else 0
        self._send(_osc(p.path, value))
        sent = time.monotonic()
        p.stage += 1
        self._pending_sends -= 1
        if p.stage == 2:
            self.edges_sent += 1
            self._schedule(p, sent + PULSE_HOLD_S)
        elif p.stage == 1:
            self._schedule(p, sent + PULSE_GAP_S)
        else:  # done: the next pulse on this path starts now
            self._order.pop(p.id, None)
            dq = self._by_path.get(p.path)
            if dq and dq[0] is p:
                dq.popleft()
                if dq:
                    self._schedule(dq[0], sent)
                else:
                    del self._by_path[p.path]

    def _send(self, data: bytes) -> None:
        for t in self._targets:
            t.send(data)

    # ------------------------------------------------------------------ tasks

    async def _drain(self) -> None:
        """Events that are due first, then state, paced at `rate` per second
        in TICK_S batches. Waits on _wake while idle (no wake-ups), and with a
        timeout of the next due event when only future events remain."""
        credit_per_tick = self.rate * TICK_S
        credit_cap = max(1.0, credit_per_tick)
        credit = credit_cap
        while True:
            now = time.monotonic()
            for t in self._targets:
                if t.transport is None and now >= t.next_attempt:
                    await self._open_target(t)
            while self._heap and (self._heap[0][2].dropped
                                  or self._heap[0][2].stage >= 3):
                heapq.heappop(self._heap)
            due = bool(self._heap) and self._heap[0][0] <= now
            if not due and not self._state:
                self._wake.clear()
                if self._heap:
                    try:
                        await asyncio.wait_for(self._wake.wait(),
                                               self._heap[0][0] - now)
                    except asyncio.TimeoutError:
                        pass
                else:
                    await self._wake.wait()
                credit = credit_cap
                continue
            while credit >= 1.0:
                now = time.monotonic()
                if self._heap and self._heap[0][0] <= now:
                    _, _, p = heapq.heappop(self._heap)
                    if p.dropped or p.stage >= 3:
                        continue
                    self._send_event(p)
                elif self._state:
                    self._send(self._state.popitem(last=False)[1])
                else:
                    break
                credit -= 1.0
            await asyncio.sleep(TICK_S)
            credit = min(credit_cap, credit + credit_per_tick)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            self._put_state(LINK_PATH, _osc(LINK_PATH, int(self._connected)))

    async def _refresh(self) -> None:
        while True:
            await asyncio.sleep(self.refresh_s)
            self.dump()

    # ------------------------------------------------------------------ stats

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "degraded": self.degraded,
            "last_error": self.last_error,
            "unknown_keys": list(self.unknown_keys),
            "rate": self.rate,
            "refresh_s": self.refresh_s,
            "keys": list(self.keys),
            "listen": (f"{self.listen[0]}:{self.listen[1]}"
                       if self._listener_tr is not None else None),
            "engine_connected": self._connected,
            "targets": {
                t.name: {
                    "sent": t.sent,
                    "errors": t.errors,
                    "last_error": t.last_error,
                    "last_send": t.last_send,
                    "state": t.state,
                } for t in self._targets
            },
            "queue_state": len(self._state),
            "queue_events": self._pending_sends,
            "events_dropped": self.events_dropped,
            "held": len(self._held),
            "held_dropped": self.held_dropped,
            "cached": len(self._cache),
            "dumps_served": self.dumps_served,
            "dumps_ignored": self.dumps_ignored,
            "edges_sent": self.edges_sent,
        }


def disabled_status(cfg: Any, error: str | None = None) -> dict:
    """/status block when no relay object exists (relay off, or construction
    failed)."""
    out: dict = {"enabled": False}
    if error:
        out["last_error"] = error
    unknown = unknown_relay_keys(cfg)
    if unknown:
        out["unknown_keys"] = unknown
    return out
