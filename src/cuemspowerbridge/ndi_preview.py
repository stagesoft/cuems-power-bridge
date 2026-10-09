# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""NDI preview during montajes: show an NDI source on any node's output,
outside a show (ClickUp 869ekxuez; plan cuems-RELATIONS
Plans/2026-10-08-ndi-preview-montajes.md).

The bridge only drives the videocomposers (VC, UDP OSC :7000). Placement and
discovery happen inside each VC (`fit_output`, `ndi/discover`, deb >=
0.1.2-8). The VC has no OSC reply channel, so its answers are read back from
the journal on the controller: the local VC's own journal, or the node's
uploaded journal under /var/log/journal/remote/. The lines matched here are
the VC's `remote/JournalContract.h`, pinned by its unit tests — change both
sides together.

Refused while a project is loaded or running (the engine's reset at load
would wipe the preview anyway, and a show must never be touched), and while
the bridge's own auto-load would reload a project at its next tick.
"""

from __future__ import annotations

import asyncio
import base64
import glob
import hashlib
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable, Iterable

from aiohttp import web

from . import network_map
from .engine_state import UNKNOWN
from .ndi_relay import Relay, RelayError
from .ndi_screens import DEFAULT_MAPPINGS, Screen, ScreenError
from . import ndi_screens

log = logging.getLogger(__name__)

LAYER_ID = "ndi-preview"   # prefix; one layer per screen: ndi-preview-<connector> (D24)
VC_OSC_PORT = 7000
VC_IDENTIFIER = "Cuems:videocomposer"  # SYSLOG_IDENTIFIER of the VC's lines
REMOTE_JOURNAL_DIR = "/var/log/journal/remote"

CONFIRM_TIMEOUT_S = 15.0    # NDI open worst case ~10.2 s + upload lag
SOURCES_CACHE_S = 5.0
OUTPUTS_CACHE_S = 60.0
RESOLVE_TIMEOUT_S = 2.0
LOAD_RECHECK_S = 1.0
POLL_S = 0.5
STATUS_CACHE_S = 1.0     # rev 11: pages poll status; one computation per second
PAGE_FILE = "ndi_page.html"
FIT_MODES = ("fill", "native")

# An NDI source's full name: "MACHINE (source)".
_FULL_NAME_RE = re.compile(r"^.+ \(.+\)$")
_LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")


class PreviewError(Exception):
    """A refusal or failure with an HTTP status and a reason token."""

    def __init__(self, status: int, reason: str, **extra: Any):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.extra = extra


# --------------------------------------------------------------------------
# Journal line parsing (pure)
# --------------------------------------------------------------------------

_DISCOVER_PREFIX = "NDI discover: "
_DONE_RE = re.compile(r"^NDI discover: done \((\d+)\)$")
_REGION_RE = re.compile(r"^region (\S+) (-?\d+),(-?\d+),(\d+),(\d+)$")
_OUTPUT_RE = re.compile(r"^\s*\[\d+\] (\S+) (\d+)x(\d+)@([\d.]+)Hz( \(disabled\))?$")
_VERSION_RE = re.compile(r"^cuems-videocomposer (\S+) starting$")
_FIT_RE = re.compile(
    r"^fit_output: (?P<id>\S+) -> (?P<region>\S+) (?P<mode>\S+) "
    r"pos (?P<x>-?\d+),(?P<y>-?\d+) scale (?P<sx>[\d.e+-]+),(?P<sy>[\d.e+-]+) "
    r"\((?P<basis>[^)]*)\)$"
)
_FIT_UNKNOWN_RE = re.compile(r"^fit_output: unknown output '([^']*)' \(have: (.*)\)$")


@dataclass
class Source:
    name: str
    address: str
    via: str = "direct"   # "controller": only the controller sees it (relayed, rev 9)


def parse_discover(messages: Iterable[str]) -> tuple[list[Source] | None, bool]:
    """(sources, busy) from the lines after an ndi/discover. `sources` is
    None until the "done" line arrives. Names may contain " @ ": split on
    the LAST one."""
    found: list[Source] = []
    busy = False
    for msg in messages:
        if not msg.startswith(_DISCOVER_PREFIX):
            continue
        if msg == "NDI discover: busy":
            busy = True
            continue
        if _DONE_RE.match(msg):
            return sorted(found, key=lambda s: s.name.lower()), busy
        body = msg[len(_DISCOVER_PREFIX):]
        if body.startswith("started (") or body.startswith("unavailable") or body.startswith("aborted"):
            continue
        name, sep, addr = body.rpartition(" @ ")
        if sep and name:
            found.append(Source(name=name, address="" if addr == "-" else addr))
    return None, busy


def parse_outputs(messages: Iterable[str]) -> list[dict]:
    """Regions (canvas rectangles) and physical modes from an output/list."""
    modes: dict[str, str] = {}
    regions: list[dict] = []
    for msg in messages:
        m = _OUTPUT_RE.match(msg)
        if m:
            modes[m.group(1)] = f"{m.group(2)}x{m.group(3)}@{m.group(4)}"
            continue
        m = _REGION_RE.match(msg)
        if m:
            regions.append({"name": m.group(1),
                            "region": f"{m.group(2)},{m.group(3)},{m.group(4)},{m.group(5)}",
                            "x": int(m.group(2))})
    regions.sort(key=lambda r: r["x"])
    return [{"name": r["name"], "mode": modes.get(r["name"], ""), "region": r["region"]}
            for r in regions]


def parse_version(messages: Iterable[str]) -> str | None:
    version = None
    for msg in messages:
        m = _VERSION_RE.match(msg)
        if m:
            version = m.group(1)
    return version


@dataclass
class Verdict:
    """What the journal says about one preview request."""
    state: str = "pending"  # pending | frames | no_frames_yet | failed | superseded
    reason: str | None = None
    fit: dict | None = None
    unknown_output: str | None = None


def classify(messages: list[str], source: str, filepath: str | None = None,
             layer_id: str = LAYER_ID) -> Verdict:
    """Classify a show request from the VC lines that followed its send.
    `filepath` is what was loaded (the relay's address form, rev 9); by
    default the name form. `layer_id` is the screen's layer (rev 10)."""
    url = filepath or f"ndi://{source}"
    complete = f"Async load complete: {url} (cue ID: {layer_id})"
    failed = f"Async load failed for: {url} (cue ID: {layer_id})"
    gone = f"Layer no longer exists for cue ID: {layer_id}"
    cancelled = (f"AsyncVideoLoader: Discarding result for cancelled cue: {layer_id}",
                 f"AsyncVideoLoader: Skipping cancelled load for cue: {layer_id}")
    v = Verdict()
    format_known = False
    no_frame = False
    for msg in messages:
        if msg.startswith("NDI: Source format: ") or msg.startswith("NDI: Source format updated "):
            format_known = True
        elif msg.startswith("NDI: No video frame received"):
            no_frame = True
        elif msg == f"NDI: Source not found: {source}":
            v.reason = "source_not_found"
        elif msg == "NDI: empty source name":
            v.reason = "empty_source_name"
        m = _FIT_RE.match(msg)
        if m and m.group("id") == layer_id:
            v.fit = {"output": m.group("region"), "mode": m.group("mode"),
                     "pos": [int(m.group("x")), int(m.group("y"))],
                     "scale": float(m.group("sx")), "basis": m.group("basis")}
        m = _FIT_UNKNOWN_RE.match(msg)
        if m:
            v.unknown_output = m.group(1)
        if msg == complete:
            v.state = "terminal-complete"
        elif msg == failed:
            v.state = "failed"
            v.reason = v.reason or "load_failed"
        elif msg == gone or msg in cancelled:
            if v.state == "pending":
                v.state = "superseded"
                v.reason = "stopped_or_reset_during_load"
    if v.unknown_output is not None and v.state in ("pending", "terminal-complete"):
        v.state = "failed"
        v.reason = "unknown_output"
        return v
    if v.state == "terminal-complete":
        if format_known:
            v.state = "frames"
        elif no_frame:
            v.state = "no_frames_yet"
        else:
            v.state = "frames"  # the load completed; no format line seen (yet)
    return v


def guard(engine: Any, auto_load_active: bool, force_unknown_engine: bool = False
          ) -> PreviewError | None:
    """The refusal for a `show`, or None. Running before loaded (a running
    project is also loaded); UNKNOWN is never taken for idle."""
    unknown = (not engine.connected or engine.load == UNKNOWN or engine.running == UNKNOWN)
    if unknown:
        if not force_unknown_engine:
            return PreviewError(503, "engine_unknown")
    else:
        if engine.running == "yes":
            return PreviewError(409, "project_running")
        if engine.load != "":
            return PreviewError(409, "project_loaded")
    if auto_load_active:
        return PreviewError(409, "auto_load_active")
    return None


def _inline_hash(html: str, tag: str) -> str:
    """CSP hash source of the page's one inline <tag> block (rev 11).
    HTML comments are skipped: a tag named in one would shift the match."""
    m = re.search(rf"<{tag}>(.*?)</{tag}>", re.sub(r"<!--.*?-->", "", html, flags=re.S), re.S)
    if not m:
        return "'none'"
    digest = hashlib.sha256(m.group(1).encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def load_page() -> tuple[bytes, dict[str, str]] | None:
    """The operator page and its headers, or None if the package lacks it."""
    try:
        html = resources.files("cuemspowerbridge.data").joinpath(PAGE_FILE).read_text("utf-8")
    except (OSError, ModuleNotFoundError) as e:
        log.warning("ndi-preview: page %s not available (%s)", PAGE_FILE, e)
        return None
    csp = ("default-src 'none'; connect-src 'self'; img-src 'self'; "
           f"script-src {_inline_hash(html, 'script')}; style-src {_inline_hash(html, 'style')}; "
           "frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
    headers = {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache",
               "Content-Security-Policy": csp, "X-Frame-Options": "DENY",
               "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
    return html.encode("utf-8"), headers


def is_exact_name(name: str) -> bool:
    return bool(_FULL_NAME_RE.match(name))


def pick_source(arg: str, sources: list[Source] | None) -> str:
    """Resolve '#n' / exact / substring against a sorted source list."""
    if arg.startswith("#"):
        if sources is None:
            raise PreviewError(404, "source_not_found", hint="no source list")
        try:
            n = int(arg[1:])
        except ValueError:
            raise PreviewError(400, "bad_source")
        if not 1 <= n <= len(sources):
            raise PreviewError(404, "source_not_found", have=len(sources))
        return sources[n - 1].name
    if sources:
        for s in sources:
            if s.name == arg:
                return s.name
        hits = [s.name for s in sources if arg.lower() in s.name.lower()]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise PreviewError(409, "ambiguous_source", matches=hits)
    if is_exact_name(arg):
        return arg
    raise PreviewError(404, "source_not_found")


# --------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------

@dataclass
class Target:
    key: str                 # "local" or the node's role_id/label
    address: str             # where the OSC goes
    via: str                 # how the address was chosen (loopback / avahi / map-ip)
    iface: str | None = None
    journal_ip: str | None = None
    hostnames: list[str] = field(default_factory=list)

    @property
    def local(self) -> bool:
        return self.key == "local"

    def public(self) -> dict:
        return {"node": None if self.local else self.key, "address": self.address,
                "resolved": self.via, "iface": self.iface}


async def route_dev(addr: str) -> str | None:
    """Outgoing interface for addr (`ip -o route get`)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "-o", "route", "get", addr,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, asyncio.TimeoutError):
        return None
    m = re.search(r"\bdev (\S+)", out.decode(errors="replace"))
    return m.group(1) if m else None


async def route_src(addr: str) -> str | None:
    """The source address the controller uses towards addr (`ip -o route get`):
    where a relay listens so the node can reach it."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "-o", "route", "get", addr,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=2.0)
    except (OSError, asyncio.TimeoutError):
        return None
    m = re.search(r"\bsrc (\S+)", out.decode(errors="replace"))
    return m.group(1) if m else None


async def resolve_name(name: str, timeout: float = RESOLVE_TIMEOUT_S) -> str | None:
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(name, None, family=socket.AF_INET, type=socket.SOCK_DGRAM),
            timeout=timeout)
    except (OSError, asyncio.TimeoutError):
        return None
    return infos[0][4][0] if infos else None


# --------------------------------------------------------------------------
# Journal access
# --------------------------------------------------------------------------

def _message(entry: dict) -> str:
    msg = entry.get("MESSAGE", "")
    if isinstance(msg, list):  # non-UTF-8 payloads come as a byte array
        msg = bytes(msg).decode(errors="replace")
    return msg


class Journal:
    """journalctl reader for one VC's lines. Remote nodes: their uploaded
    per-IP file (exact cursor, no _HOSTNAME collisions on clone images);
    without one, the merged journal by hostname with a 10 s clock-skew
    margin (journalctl refuses --since together with --after-cursor)."""

    def __init__(self, journal_dir: str = "/var/log/journal",
                 runner: Callable[[list[str]], Any] | None = None):
        self.journal_dir = journal_dir
        self._runner = runner or self._run_journalctl

    @staticmethod
    async def _run_journalctl(args: list[str]) -> list[dict]:
        proc = await asyncio.create_subprocess_exec(
            "journalctl", "--no-pager", "-o", "json", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        entries = []
        for line in out.decode(errors="replace").splitlines():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
        return entries

    def remote_files(self, ip: str) -> list[str]:
        base = os.path.join(self.journal_dir, "remote", f"remote-{ip}")
        return sorted(glob.glob(base + ".journal") + glob.glob(base + "@*.journal"))

    def local_files(self) -> list[str]:
        try:
            with open("/etc/machine-id") as f:
                mid = f.read().strip()
        except OSError:
            return []
        for d in (os.path.join(self.journal_dir, mid), os.path.join("/run/log/journal", mid)):
            path = os.path.join(d, "system.journal")
            if os.path.exists(path):
                return [path]
        return []

    def check_readable(self, target: Target) -> None:
        files = self.local_files() if target.local else (
            self.remote_files(target.journal_ip) if target.journal_ip else [])
        unreadable = [f for f in files if not os.access(f, os.R_OK)]
        if files and len(unreadable) == len(files):
            raise PreviewError(503, "journal_unreadable", path=unreadable[0])

    def _source_args(self, target: Target) -> tuple[list[str], bool]:
        """(args, cursor_ok)."""
        if target.local:
            return [], True
        if target.journal_ip and self.remote_files(target.journal_ip):
            base = os.path.join(self.journal_dir, "remote", f"remote-{target.journal_ip}")
            return [f"--file={base}.journal", f"--file={base}@*.journal"], True
        hosts = [f"_HOSTNAME={h}" for h in target.hostnames] or []
        return ["--merge", *hosts], False

    async def mark(self, target: Target) -> dict:
        """A position to read from later: a cursor where it is exact,
        otherwise a send time (with skew margin applied at read)."""
        args, cursor_ok = self._source_args(target)
        mark = {"t": time.time()}
        if cursor_ok:
            entries = await self._runner([*args, "-n", "1"])
            if entries and "__CURSOR" in entries[-1]:
                mark["cursor"] = entries[-1]["__CURSOR"]
        return mark

    async def read(self, target: Target, mark: dict) -> list[str]:
        args, _ = self._source_args(target)
        if "cursor" in mark:
            pos = ["--after-cursor", mark["cursor"]]
        else:
            skew = 1.0 if target.local else 10.0
            pos = ["--since", f"@{int(mark['t'] - skew)}"]
        entries = await self._runner([*args, *pos, f"SYSLOG_IDENTIFIER={VC_IDENTIFIER}"])
        return [_message(e) for e in entries]

    async def _latest(self, target: Target, pattern: str) -> tuple[int, str] | None:
        """(realtime µs, message) of the VC's latest line matching pattern."""
        args, _ = self._source_args(target)
        try:
            entries = await self._runner(
                [*args, "-r", "-n", "1", "--grep", pattern, f"SYSLOG_IDENTIFIER={VC_IDENTIFIER}"])
        except Exception:
            return None
        if not entries:
            return None
        e = entries[0]
        try:
            ts = int(e.get("__REALTIME_TIMESTAMP", 0))
        except (TypeError, ValueError):
            ts = 0
        return ts, _message(e)

    async def _current(self, target: Target, pattern: str) -> str | None:
        """The latest line matching pattern, but only if the RUNNING VC
        process logged it. A VC older than rev 9 logs neither the version
        nor the transport line, so after a downgrade the newest such line in
        the journal belongs to a previous binary. Every VC logs "Worker
        thread running" right after starting: a line more than 60 s older
        than the newest of those is stale."""
        line = await self._latest(target, pattern)
        if line is None:
            return None
        worker = await self._latest(target, "^AsyncVideoLoader: Worker thread running$")
        if worker is not None and line[0] and worker[0] and line[0] < worker[0] - 60_000_000:
            return None
        return line[1]

    async def transport(self, target: Target) -> str | None:
        """The running VC's "NDI receive transport: ..." line, or None (a VC
        older than rev 9 never logs one)."""
        return await self._current(target, "^NDI receive transport: ")

    async def version(self, target: Target) -> str | None:
        line = await self._current(target, "^cuems-videocomposer .* starting$")
        return parse_version([line]) if line else None


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

@dataclass
class _Request:
    screen: Screen
    target: Target
    source: str
    mode: str
    mark: dict
    sent_at: float
    filepath: str = ""
    relay: Relay | None = None
    verdict: Verdict = field(default_factory=Verdict)
    confirmed_at: float | None = None
    raced_by_load: bool = False
    wiped: bool = False
    stopped: bool = False

    @property
    def layer_id(self) -> str:
        return self.screen.layer_id

    def public(self, with_upstream: bool = True) -> dict:
        out = {"screen": self.screen.public(), "source": self.source, "mode": self.mode,
               "route": "relay" if self.relay else "direct",
               "machine_address": self.target.address,
               "confirm": self.verdict.state, "reason": self.verdict.reason,
               "fit": self.verdict.fit, "raced_by_load": self.raced_by_load,
               "wiped": self.wiped, "stopped": self.stopped}
        if self.relay is not None:
            out["relay"] = self.relay.stats(with_upstream)
        return out


@dataclass
class SeenSource:
    """One source in the cluster-wide list: who sees it, and at what address."""
    name: str
    seen_by: dict[str, str]      # machine -> "ip:port" ("" when unknown)

    def public(self, n: int) -> dict:
        return {"n": n, "name": self.name, "seen_by": sorted(self.seen_by)}


class NdiPreview:
    def __init__(self, bridge: Any, journal: Journal | None = None,
                 osc_send: Callable[[str, int, str, list], None] | None = None,
                 resolver: Callable[[str], Any] | None = None,
                 router: Callable[[str], Any] | None = None,
                 src_router: Callable[[str], Any] | None = None):
        self.bridge = bridge
        self.cfg = bridge.cfg
        self.journal = journal or Journal()
        self._osc_send = osc_send or self._udp_send
        self._resolve = resolver or resolve_name
        self._route = router or route_dev
        self._route_src = src_router or route_src
        self.mappings_path = self.cfg.extras.get("ndi_mappings_path", DEFAULT_MAPPINGS)
        self._locks: dict[str, asyncio.Lock] = {}
        self._discovering: dict[str, asyncio.Task] = {}
        self._sources_cache: dict[str, tuple[float, list[Source]]] = {}
        self._outputs_cache: dict[str, tuple[float, list[dict]]] = {}
        self._versions: dict[str, str] = {}
        self._previews: dict[str, _Request] = {}     # by screen alias (D24)
        self._relays: dict[str, Relay] = {}          # by "<vc address>|<connector>"
        self._tasks: set[asyncio.Task] = set()
        self._status_cache: dict[bool, tuple[float, dict]] = {}   # keyed by `authorized` (D22)
        self._status_tasks: dict[bool, asyncio.Task] = {}
        self._page = load_page()
        self.vc_port = int(self.cfg.extras.get("ndi_vc_osc_port", VC_OSC_PORT))

    # ---------------- plumbing ----------------

    @staticmethod
    def _udp_send(address: str, port: int, path: str, args: list) -> None:
        from pythonosc.udp_client import SimpleUDPClient
        SimpleUDPClient(address, port).send_message(path, args)

    def _send(self, target: Target, path: str, *args: Any) -> None:
        self._osc_send(target.address, self.vc_port, path, list(args))

    async def resolve_target(self, node: str | None) -> Target:
        """The master entry is always the local VC on 127.0.0.1. A node's
        avahi name is only trusted if it resolves onto the cluster segment:
        role_ids repeat across clusters, and on a shared LAN `node01.local`
        can answer from another cluster's box (M-B). Otherwise `<ip>`."""
        if not node or node == "local":
            return Target("local", "127.0.0.1", "loopback")
        loop = asyncio.get_running_loop()
        nodes = await loop.run_in_executor(None, network_map.parse, self.cfg.network_map_path)
        for n in nodes:
            names = {n.role_id, n.alias, n.hostname, n.ip, n.uuid} - {None}
            if node not in names:
                continue
            if n.node_type == "NodeType.master":
                return Target("local", "127.0.0.1", "loopback")
            label = n.role_id or n.alias or n.hostname or n.uuid
            hostnames = [h for h in (n.role_id, n.hostname, n.alias) if h]
            cluster_dev = await self._route("169.254.0.1")
            for cand in hostnames:
                addr = await self._resolve(f"{cand}.local")
                if not addr:
                    continue
                dev = await self._route(addr)
                on_segment = (ipaddress.ip_address(addr) in _LINK_LOCAL
                              and dev is not None and dev == cluster_dev)
                if addr == n.ip or on_segment:
                    return Target(label, addr, f"avahi {cand}.local", dev, n.ip, hostnames)
                log.warning("ndi-preview: %s.local resolved to %s via %s, not on the "
                            "cluster segment (%s) — ignoring it", cand, addr, dev, cluster_dev)
            if n.ip:
                return Target(label, n.ip, "network_map <ip>", await self._route(n.ip),
                              n.ip, hostnames)
            raise PreviewError(400, "bad_node", detail=f"{node}: unresolvable")
        raise PreviewError(400, "bad_node", detail=f"{node}: not in network_map")

    async def _machines(self) -> list[str]:
        """'local' plus every node's label, as resolve_target() takes them."""
        loop = asyncio.get_running_loop()
        nodes = await loop.run_in_executor(None, network_map.parse, self.cfg.network_map_path)
        return ["local"] + [n.role_id or n.alias or n.hostname or n.uuid for n in nodes
                            if n.node_type == "NodeType.slave"]

    def _lock(self, target: Target) -> asyncio.Lock:
        return self._locks.setdefault(target.address, asyncio.Lock())

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def _note_version(self, target: Target, messages: list[str]) -> None:
        v = parse_version(messages)
        if v:
            self._versions[target.address] = v

    # ---------------- screens (rev 10) ----------------

    async def screens(self, live: bool = False) -> list[Screen]:
        """The cluster's screens. `live` also asks every VC which connectors
        it drives (marks absent ones, adds unmapped ones)."""
        drives: dict[str, list[str]] | None = None
        if live:
            drives = {}
            machines = await self._machines()
            results = await asyncio.gather(*(self.outputs(m) for m in machines),
                                           return_exceptions=True)
            for m, r in zip(machines, results):
                if not isinstance(r, BaseException):
                    drives[m] = [o["name"] for o in r[1]]
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, ndi_screens.catalogue, self.mappings_path,
                                          self.cfg.network_map_path, drives)

    async def _screen(self, arg: str) -> Screen:
        try:
            return ndi_screens.resolve(arg, await self.screens())
        except ScreenError as e:
            raise PreviewError(e.status, e.reason, **e.extra)

    # ---------------- discovery ----------------

    async def sources(self) -> list[SeenSource]:
        """Every source any VC of the cluster sees, sorted by name, with who
        sees it. `#n` in show indexes this list."""
        # Not while a project is loaded or running, nor with the engine
        # unknown: discovery would poke every VC of a show (rev 11.1).
        err = guard(self.bridge.engine, False)
        if err:
            raise err
        machines = await self._machines()
        targets = await asyncio.gather(*(self.resolve_target(m) for m in machines),
                                       return_exceptions=True)
        pairs = [(m, t) for m, t in zip(machines, targets) if isinstance(t, Target)]
        lists = await asyncio.gather(*(self._list(t) for _, t in pairs), return_exceptions=True)
        seen: dict[str, SeenSource] = {}
        answered = 0
        for (m, _), lst in zip(pairs, lists):
            if isinstance(lst, BaseException):
                log.info("ndi-preview: no source list from %s (%s)", m, lst)
                continue
            answered += 1
            for src in lst:
                seen.setdefault(src.name, SeenSource(src.name, {})).seen_by[
                    "controller" if m == "local" else m] = src.address
        if not answered:
            raise PreviewError(504, "no_vc_answer",
                               hint="no videocomposer answered ndi/discover (older than 0.1.2-8?)")
        return sorted(seen.values(), key=lambda s: s.name.lower())

    async def _list(self, target: Target, timeout: int = 3) -> list[Source]:
        cached = self._sources_cache.get(target.address)
        if cached and time.monotonic() - cached[0] < SOURCES_CACHE_S:
            return cached[1]
        task = self._discovering.get(target.address)
        if task is None or task.done():
            task = self._spawn(self._discover(target, max(1, min(10, timeout))))
            self._discovering[target.address] = task
        return await asyncio.shield(task)

    async def _discover(self, target: Target, seconds: int) -> list[Source]:
        self.journal.check_readable(target)
        mark = await self.journal.mark(target)
        self._send(target, "/videocomposer/ndi/discover", seconds)
        deadline = time.monotonic() + seconds + 5
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            msgs = await self.journal.read(target, mark)
            found, busy = parse_discover(msgs)
            if found is not None:
                self._sources_cache[target.address] = (time.monotonic(), found)
                return found
        if target.local:
            found = await self._legacy_discover(seconds)
            if found is not None:
                self._sources_cache[target.address] = (time.monotonic(), found)
                return found
        version = await self.journal.version(target)
        raise PreviewError(504, "no_vc_answer", vc_version=version,
                           hint="no 'NDI discover: done' line: VC older than 0.1.2-8, "
                                "journal lag, or not running")

    async def _legacy_discover(self, seconds: int) -> list[Source] | None:
        """`cuems-videocomposer --discover-ndi N` (a VC older than -8 has no
        ndi/discover). Exit 1 = nothing found or no SDK. Names only."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "cuems-videocomposer", "--discover-ndi", str(seconds),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=seconds + 5)
        except asyncio.TimeoutError:
            proc.kill()
            return None
        except OSError:
            return None
        if proc.returncode == 1:
            return []
        names = re.findall(r"^\s*\d+\.\s+(.+?)\s*$", out.decode(errors="replace"), re.M)
        return sorted((Source(n, "") for n in names), key=lambda s: s.name.lower())

    # ---------------- outputs of one VC ----------------

    async def outputs(self, machine: str | None) -> tuple[Target, list[dict]]:
        target = await self.resolve_target(machine)
        cached = self._outputs_cache.get(target.address)
        if cached and time.monotonic() - cached[0] < OUTPUTS_CACHE_S:
            return target, cached[1]
        self.journal.check_readable(target)
        mark = await self.journal.mark(target)
        self._send(target, "/videocomposer/output/list")
        deadline = time.monotonic() + 6
        msgs: list[str] = []
        while time.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            msgs = await self.journal.read(target, mark)
            if any(m.startswith("=== Capture Status") for m in msgs):
                break
        outs = parse_outputs(msgs)
        if not outs:
            raise PreviewError(503, "no_region_lines",
                               hint="VC older than 0.1.2-8 (no 'region' lines), or no answer")
        self._outputs_cache[target.address] = (time.monotonic(), outs)
        return target, outs

    # ---------------- show / stop / status ----------------

    async def show(self, source_arg: str, output_arg: str, mode: str = "fill",
                   wait: bool = False, force_unknown_engine: bool = False) -> dict:
        if mode not in FIT_MODES:
            raise PreviewError(400, "bad_mode", modes=list(FIT_MODES))
        err = guard(self.bridge.engine, self.bridge.auto_load_active(), force_unknown_engine)
        if err:
            raise err
        screen = await self._screen(output_arg)
        target = await self.resolve_target(screen.target)
        lock = self._lock(target)
        if lock.locked():
            raise PreviewError(409, "busy", hint="a preview request on this machine is in flight")
        await lock.acquire()
        new_relay: Relay | None = None
        key = f"{target.address}|{screen.connector}"
        try:
            self.journal.check_readable(target)
            name, address, route = await self._route_for(target, screen, source_arg)
            if route == "relay":
                await self._relay_preconditions(target, address)
            err = guard(self.bridge.engine, self.bridge.auto_load_active(), force_unknown_engine)
            if err:
                raise err
            layer = screen.layer_id
            mark = await self.journal.mark(target)
            self._send(target, "/videocomposer/layer/unload", layer)
            # The screen's previous relay goes only after its unload, so the
            # old layer does not sit reconnecting to a closed port.
            await self._close_relay(key, "replaced")
            filepath = f"ndi://{name}"
            if route == "relay":
                new_relay = await self._open_relay(target, key, address)
                filepath = f"ndi://@{new_relay.address}"
            self._send(target, "/videocomposer/layer/load", filepath, layer)
            self._send(target, f"/videocomposer/layer/{layer}/fit_output", screen.connector, mode)
            self._send(target, f"/videocomposer/layer/{layer}/zorder", 1000)
            self._send(target, f"/videocomposer/layer/{layer}/visible", 1)
            log.info("ndi-preview: %s on %s (%s %s) mode=%s route=%s%s", name, screen.alias,
                     target.key, target.address, mode, route,
                     f" {new_relay.address}" if new_relay else "")
            req = _Request(screen, target, name, mode, mark, time.time(),
                           filepath=filepath, relay=new_relay)
            self._previews[screen.alias] = req
            self._status_cache.clear()
        except BaseException:
            if new_relay is not None:
                await self._close_relay(key, "show failed")
            lock.release()
            raise
        # The lock is held until the journal shows how this load ended: two
        # overlapping loads on one VC would leave it showing the first while
        # the second is confirmed.
        task = self._spawn(self._confirm(req, lock))
        if not wait:
            return {"sent": True, **req.public(), "confirm": "pending"}
        await asyncio.shield(task)
        if req.raced_by_load:
            raise PreviewError(409, "raced_by_load", request=req.public())
        if req.verdict.state in ("failed", "superseded"):
            raise PreviewError(502, req.verdict.reason or req.verdict.state,
                               request=req.public())
        return {"sent": True, **req.public()}

    async def _route_for(self, target: Target, screen: Screen, arg: str) -> tuple[str, str, str]:
        """(source name, address, "direct"|"relay") for this screen. A name
        the screen's own machine already lists goes direct without asking
        anyone else; otherwise the cluster-wide list decides."""
        own = self._sources_cache.get(target.address)
        if (is_exact_name(arg) and own and time.monotonic() - own[0] < SOURCES_CACHE_S
                and any(s.name == arg for s in own[1])):
            return arg, next(s.address for s in own[1] if s.name == arg), "direct"
        machine = "controller" if target.local else target.key
        listed = await self.sources()
        if arg.startswith("#"):
            try:
                idx = int(arg[1:])
            except ValueError:
                raise PreviewError(400, "bad_source")
            if not 1 <= idx <= len(listed):
                raise PreviewError(404, "source_not_found", have=len(listed))
            entry = listed[idx - 1]
        else:
            name = pick_source(arg, [Source(s.name, "") for s in listed])
            entry = next((s for s in listed if s.name == name), None)
            if entry is None:
                # An exact name nobody lists: the controller's VC may still
                # find it (rev 8 behaviour); a node needs a route, so no.
                if target.local:
                    return name, "", "direct"
                raise PreviewError(404, "source_not_found",
                                   hint=f"no machine of the cluster sees {name!r}")
        if machine in entry.seen_by:
            return entry.name, entry.seen_by[machine], "direct"
        if target.local:
            raise PreviewError(404, "source_not_found",
                               hint=f"the controller does not see {entry.name!r}")
        if "controller" in entry.seen_by:
            return entry.name, entry.seen_by["controller"], "relay"
        raise PreviewError(404, "source_not_found",
                           hint=f"neither {machine} nor the controller sees {entry.name!r} "
                                f"(seen by: {', '.join(sorted(entry.seen_by))})")

    async def _confirm(self, req: _Request, lock: asyncio.Lock) -> None:
        try:
            deadline = time.monotonic() + CONFIRM_TIMEOUT_S
            rechecked = False
            while time.monotonic() < deadline:
                await asyncio.sleep(POLL_S)
                if not rechecked and time.time() - req.sent_at >= LOAD_RECHECK_S:
                    rechecked = True
                    if self.bridge.engine.load not in ("", UNKNOWN):
                        # A project load raced the send: get out of its way.
                        req.raced_by_load = True
                        self._send(req.target, "/videocomposer/layer/unload", req.layer_id)
                        log.warning("ndi-preview: a project was loaded during the "
                                    "preview send; preview unloaded")
                msgs = await self.journal.read(req.target, req.mark)
                self._note_version(req.target, msgs)
                v = classify(msgs, req.source, req.filepath, req.layer_id)
                if (req.relay is not None and req.relay.upstream_errors
                        and not req.relay.bytes_to_node):
                    v = Verdict(state="failed", reason="relay_upstream_unreachable")
                if v.state == "terminal-complete":
                    continue
                if v.state != "pending":
                    if v.state in ("frames", "no_frames_yet"):
                        # One more read so the re-fit on the real size is seen.
                        await asyncio.sleep(POLL_S)
                        v = classify(await self.journal.read(req.target, req.mark),
                                     req.source, req.filepath, req.layer_id)
                        if (req.relay is not None and v.state == "frames"
                                and not req.relay.bytes_to_node):
                            v = Verdict(state="failed", reason="relay_stalled", fit=v.fit)
                    req.verdict = v
                    req.confirmed_at = time.time()
                    if v.reason == "unknown_output":
                        # It would sit visible at canvas centre otherwise.
                        self._send(req.target, "/videocomposer/layer/unload", req.layer_id)
                    log.info("ndi-preview: %s on %s → %s%s", req.source, req.screen.alias,
                             v.state, f" ({v.reason})" if v.reason else "")
                    return
            req.verdict = Verdict(state="unconfirmed",
                                  reason="no terminal VC line within %.0fs" % CONFIRM_TIMEOUT_S)
            version = self._versions.get(req.target.address) or await self.journal.version(req.target)
            if version is None:
                req.verdict.reason += (" — no VC version line seen (VC older than "
                                       "0.1.2-8, journal lag, or a vacuumed journal)")
        except PreviewError as e:
            req.verdict = Verdict(state="unconfirmed", reason=e.reason)
        except Exception:
            log.exception("ndi-preview: confirmation failed")
            req.verdict = Verdict(state="unconfirmed", reason="internal_error")
        finally:
            lock.release()

    async def stop(self, output_arg: str | None = None) -> list[dict]:
        """One screen, or (no argument) every screen of the cluster: the
        catalogue's connectors on every machine, so a preview started before
        a bridge restart is cleared too."""
        stopped: list[dict] = []
        screens = [await self._screen(output_arg)] if output_arg else await self.screens()
        targets: dict[str, Target] = {}
        for sc in screens:
            if sc.target not in targets:
                try:
                    targets[sc.target] = await self.resolve_target(sc.target)
                except PreviewError:
                    continue
            t = targets[sc.target]
            self._send(t, "/videocomposer/layer/unload", sc.layer_id)
            await self._close_relay(f"{t.address}|{sc.connector}", "stop")
            req = self._previews.get(sc.alias)
            if req is not None:
                req.stopped = True
            stopped.append(sc.public())
        if not output_arg:
            for key in list(self._relays):
                await self._close_relay(key, "stop")
        self._status_cache.clear()
        log.info("ndi-preview: stop sent to %s", ", ".join(s["alias"] for s in stopped))
        return stopped

    async def status(self, authorized: bool = True) -> dict:
        """Per screen (D24). `authorized` = the request carried a valid token:
        only then do relay upstream addresses and byte counts appear (D22).
        Pages poll this, so one computation serves every caller for
        STATUS_CACHE_S, kept apart per `authorized` (rev 11.1)."""
        cached = self._status_cache.get(authorized)
        if cached and time.monotonic() - cached[0] < STATUS_CACHE_S:
            return cached[1]
        task = self._status_tasks.get(authorized)
        if task is None or task.done():
            task = self._spawn(self._status(authorized))
            self._status_tasks[authorized] = task
        result = await asyncio.shield(task)
        self._status_cache[authorized] = (time.monotonic(), result)
        return result

    async def _status(self, authorized: bool) -> dict:
        eng = self.bridge.engine
        previews = []
        for alias, req in sorted(self._previews.items()):
            if req.target.address not in self._versions:
                version = await self.journal.version(req.target)
                if version:
                    self._versions[req.target.address] = version
            if req.verdict.state in ("frames", "no_frames_yet") and not req.stopped:
                try:
                    msgs = await self.journal.read(req.target, req.mark)
                    if req.verdict.state == "no_frames_yet" and any(
                            m.startswith("NDI: Source format updated ") for m in msgs):
                        req.verdict.state = "frames"
                    if any(m.startswith("Reset: removing all layers") for m in msgs):
                        req.wiped = True
                except PreviewError:
                    pass
            previews.append(req.public(authorized))
        blocked = guard(eng, self.bridge.auto_load_active())
        return {
            "engine": {"running": eng.running, "load": eng.load, "armed": eng.armed},
            "blocked": blocked.reason if blocked else None,
            "auto_load": self.bridge.auto_load_state(),
            "vc_versions": dict(self._versions),
            "previews": previews,
            "relays": [r.stats(authorized) for r in self._relays.values()],
        }

    # ---------------- relay (rev 9) ----------------

    async def _relay_preconditions(self, target: Target, address: str) -> None:
        line = await self.journal.transport(target)
        if not line or not line.startswith("NDI receive transport: base TCP"):
            raise PreviewError(409, "relay_needs_vc", node=target.key,
                               vc_version=await self.journal.version(target), transport=line,
                               hint="this machine's videocomposer cannot take a relayed source "
                                    "(needs >= 0.1.2-8 with base-TCP NDI receive)")
        if not address:
            raise PreviewError(409, "relay_needs_controller_vc",
                               hint="the controller's videocomposer reports no source address "
                                    "(needs >= 0.1.2-8)")

    async def _open_relay(self, target: Target, key: str, address: str) -> Relay:
        listen_ip = await self._route_src(target.address)
        if not listen_ip:
            raise PreviewError(503, "relay_no_route", node=target.key, address=target.address)
        relay = Relay(target.address, address, listen_ip, should_close=self._relay_should_close)
        try:
            await relay.start()
        except RelayError as e:
            raise PreviewError(502 if e.reason == "relay_upstream_unreachable" else 503,
                               e.reason, detail=e.detail)
        self._relays[key] = relay
        return relay

    async def _close_relay(self, key: str, reason: str) -> None:
        relay = self._relays.pop(key, None)
        if relay is not None:
            await relay.close(reason)

    def _relay_should_close(self) -> str | None:
        eng = self.bridge.engine
        if eng.running == "yes":
            return "project running"
        if eng.load not in ("", UNKNOWN):
            return "project loaded"
        return None

    # ---------------- HTTP ----------------

    def register(self, app: web.Application) -> None:
        app.router.add_get("/ndi", self.h_page_redirect)
        app.router.add_get("/ndi/", self.h_page)
        app.router.add_get("/ndi/sources", self.h_sources)
        app.router.add_get("/ndi/outputs", self.h_outputs)
        app.router.add_post("/ndi/preview", self.h_preview)
        app.router.add_post("/ndi/stop", self.h_stop)
        app.router.add_get("/ndi/status", self.h_status)

    def _gate(self, request: web.Request, key: str) -> web.Response | None:
        """No token on /ndi/* (D30, Ion 2026-10-09): like the CUEMS UI, the
        preview is open on the internal network until there are users. The
        bridge's shared_token keeps guarding /go, /stop, /shutdown, /poweroff…;
        handing it to a page would hand those out too. What stands between a
        stray web page and these endpoints is D27 (JSON only → a CORS
        preflight the bridge never answers). A valid token still unlocks the
        relay details in /ndi/status (D22)."""
        if not self.bridge._rate.allow(key):
            return self.bridge._err("rate_limited", 429)
        return None

    @staticmethod
    def _fail(e: PreviewError) -> web.Response:
        return web.json_response({"ok": False, "reason": e.reason, **e.extra}, status=e.status)

    @staticmethod
    async def _body(request: web.Request) -> dict:
        """A POST body: JSON only (D27: a plain form or text/plain POST from
        any web page would otherwise reach the bridge without a preflight).
        An empty body is {}; anything unparseable is refused, never read as
        {} (that would mean "every screen" to stop)."""
        if request.content_type != "application/json":
            raise PreviewError(415, "bad_content_type", expected="application/json")
        raw = await request.read()
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            raise PreviewError(400, "bad_body")
        if not isinstance(data, dict):
            raise PreviewError(400, "bad_body")
        return data

    async def h_page(self, request: web.Request) -> web.Response:
        if self._page is None:
            return self.bridge._err("page_missing", 404)
        body, headers = self._page
        return web.Response(body=body, headers=headers)

    async def h_page_redirect(self, request: web.Request) -> web.Response:
        raise web.HTTPMovedPermanently(location="ndi/")

    async def h_sources(self, request: web.Request) -> web.Response:
        if (r := self._gate(request, "ndi_sources")):
            return r
        try:
            found = await self.sources()
        except PreviewError as e:
            return self._fail(e)
        body = {"ok": True, "sources": [s.public(i + 1) for i, s in enumerate(found)]}
        if not found:
            body["hint"] = ("no NDI source seen by any machine: put the laptop on the nodes' "
                            "switch, the controller's network or its WiFi")
        return web.json_response(body)

    async def h_outputs(self, request: web.Request) -> web.Response:
        if (r := self._gate(request, "ndi_outputs")):
            return r
        try:
            # The live check asks every VC for its outputs: not during a show.
            live = guard(self.bridge.engine, False) is None
            screens = await self.screens(live=live)
        except PreviewError as e:
            return self._fail(e)
        return web.json_response({"ok": True, "outputs": [s.public() for s in screens]})

    async def h_preview(self, request: web.Request) -> web.Response:
        if (r := self._gate(request, "ndi_preview")):
            return r
        try:
            body = await self._body(request)
        except PreviewError as e:
            return self._fail(e)
        source = body.get("source")
        output = body.get("output")
        if not source:
            return self.bridge._err("missing_source", 400)
        if not output:
            return self.bridge._err("missing_output", 400)
        wait = bool(body.get("wait"))
        try:
            result = await self.show(
                str(source), str(output), str(body.get("mode") or "fill"),
                wait=wait, force_unknown_engine=bool(body.get("force_unknown_engine")))
        except PreviewError as e:
            return self._fail(e)
        return web.json_response({"ok": True, **result}, status=200 if wait else 202)

    async def h_stop(self, request: web.Request) -> web.Response:
        if (r := self._gate(request, "ndi_stop")):
            return r
        try:
            body = await self._body(request)
            if "output" in body:
                # Named but empty is a mistake, not "all": only {} clears all.
                output = body["output"]
                if not output or not isinstance(output, (str, int)):
                    raise PreviewError(400, "missing_output")
                stopped = await self.stop(str(output))
            else:
                stopped = await self.stop(None)
        except PreviewError as e:
            return self._fail(e)
        return web.json_response({"ok": True, "stopped": stopped})

    async def h_status(self, request: web.Request) -> web.Response:
        authorized = self.bridge._check_token(request)
        return web.json_response({"ok": True, **(await self.status(authorized))})

    async def close(self) -> None:
        for key in list(self._relays):
            await self._close_relay(key, "bridge stop")
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
