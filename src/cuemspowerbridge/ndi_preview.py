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
import glob
import ipaddress
import json
import logging
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from aiohttp import web

from . import network_map
from .engine_state import UNKNOWN

log = logging.getLogger(__name__)

LAYER_ID = "ndi-preview"
VC_OSC_PORT = 7000
VC_IDENTIFIER = "Cuems:videocomposer"  # SYSLOG_IDENTIFIER of the VC's lines
REMOTE_JOURNAL_DIR = "/var/log/journal/remote"

CONFIRM_TIMEOUT_S = 15.0    # NDI open worst case ~10.2 s + upload lag
SOURCES_CACHE_S = 5.0
OUTPUTS_CACHE_S = 60.0
RESOLVE_TIMEOUT_S = 2.0
LOAD_RECHECK_S = 1.0
POLL_S = 0.5
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


def classify(messages: list[str], source: str) -> Verdict:
    """Classify a show request from the VC lines that followed its send."""
    url = f"ndi://{source}"
    complete = f"Async load complete: {url} (cue ID: {LAYER_ID})"
    failed = f"Async load failed for: {url} (cue ID: {LAYER_ID})"
    gone = f"Layer no longer exists for cue ID: {LAYER_ID}"
    cancelled = (f"AsyncVideoLoader: Discarding result for cancelled cue: {LAYER_ID}",
                 f"AsyncVideoLoader: Skipping cancelled load for cue: {LAYER_ID}")
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
        if m and m.group("id") == LAYER_ID:
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
                "via": self.via, "iface": self.iface}


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

    async def version(self, target: Target) -> str | None:
        args, _ = self._source_args(target)
        try:
            entries = await self._runner(
                [*args, "-r", "-n", "1", "--grep", "^cuems-videocomposer .* starting$",
                 f"SYSLOG_IDENTIFIER={VC_IDENTIFIER}"])
        except Exception:
            return None
        return parse_version(_message(e) for e in entries)


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------

@dataclass
class _Request:
    target: Target
    source: str
    output: str | None
    mode: str
    mark: dict
    sent_at: float
    verdict: Verdict = field(default_factory=Verdict)
    confirmed_at: float | None = None
    raced_by_load: bool = False
    wiped: bool = False
    stopped: bool = False

    def public(self) -> dict:
        return {**self.target.public(), "source": self.source, "output": self.output,
                "mode": self.mode, "confirm": self.verdict.state,
                "reason": self.verdict.reason, "fit": self.verdict.fit,
                "raced_by_load": self.raced_by_load, "wiped": self.wiped,
                "stopped": self.stopped}


class NdiPreview:
    def __init__(self, bridge: Any, journal: Journal | None = None,
                 osc_send: Callable[[str, int, str, list], None] | None = None,
                 resolver: Callable[[str], Any] | None = None,
                 router: Callable[[str], Any] | None = None):
        self.bridge = bridge
        self.cfg = bridge.cfg
        self.journal = journal or Journal()
        self._osc_send = osc_send or self._udp_send
        self._resolve = resolver or resolve_name
        self._route = router or route_dev
        self._locks: dict[str, asyncio.Lock] = {}
        self._discovering: dict[str, asyncio.Task] = {}
        self._sources_cache: dict[str, tuple[float, list[Source]]] = {}
        self._outputs_cache: dict[str, tuple[float, list[dict]]] = {}
        self._versions: dict[str, str] = {}
        self._last: _Request | None = None
        self._tasks: set[asyncio.Task] = set()
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

    # ---------------- discovery ----------------

    async def sources(self, node: str | None, timeout: int = 3) -> tuple[Target, list[Source]]:
        if self.bridge.engine.running == "yes":
            raise PreviewError(409, "project_running")
        target = await self.resolve_target(node)
        cached = self._sources_cache.get(target.address)
        if cached and time.monotonic() - cached[0] < SOURCES_CACHE_S:
            return target, cached[1]
        task = self._discovering.get(target.address)
        if task is None or task.done():
            task = self._spawn(self._discover(target, max(1, min(10, timeout))))
            self._discovering[target.address] = task
        return target, await asyncio.shield(task)

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

    # ---------------- outputs ----------------

    async def outputs(self, node: str | None) -> tuple[Target, list[dict]]:
        target = await self.resolve_target(node)
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

    async def show(self, source_arg: str, node: str | None, output: str | None,
                   mode: str = "fill", wait: bool = False,
                   force_unknown_engine: bool = False) -> dict:
        if mode not in FIT_MODES:
            raise PreviewError(400, "bad_mode", modes=list(FIT_MODES))
        err = guard(self.bridge.engine, self.bridge.auto_load_active(), force_unknown_engine)
        if err:
            raise err
        target = await self.resolve_target(node)
        lock = self._lock(target)
        if lock.locked():
            raise PreviewError(409, "busy", hint="a preview request on this VC is in flight")
        await lock.acquire()
        try:
            self.journal.check_readable(target)
            sources = None
            cached = self._sources_cache.get(target.address)
            if cached and time.monotonic() - cached[0] < SOURCES_CACHE_S:
                sources = cached[1]
            if source_arg.startswith("#") or not (is_exact_name(source_arg) or sources):
                _, sources = await self.sources(node)
            source = pick_source(source_arg, sources)
            if output:
                outs = self._outputs_cache.get(target.address)
                if outs and time.monotonic() - outs[0] < OUTPUTS_CACHE_S:
                    names = [o["name"] for o in outs[1]]
                    if output not in names:
                        raise PreviewError(400, "bad_output", have=names)
            err = guard(self.bridge.engine, self.bridge.auto_load_active(), force_unknown_engine)
            if err:
                raise err
            mark = await self.journal.mark(target)
            self._send(target, "/videocomposer/layer/unload", LAYER_ID)
            self._send(target, "/videocomposer/layer/load", f"ndi://{source}", LAYER_ID)
            fit_args = [output, mode] if output else [mode]
            self._send(target, f"/videocomposer/layer/{LAYER_ID}/fit_output", *fit_args)
            self._send(target, f"/videocomposer/layer/{LAYER_ID}/zorder", 1000)
            self._send(target, f"/videocomposer/layer/{LAYER_ID}/visible", 1)
            log.info("ndi-preview: %s on %s (%s) output=%s mode=%s", source,
                     target.key, target.address, output or "(first)", mode)
            req = _Request(target, source, output, mode, mark, time.time())
            self._last = req
        except BaseException:
            lock.release()
            raise
        # The lock is held until the journal shows how this load ended: two
        # overlapping loads of one layer id would leave the VC showing the
        # first while the second is confirmed.
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
                        self._send(req.target, "/videocomposer/layer/unload", LAYER_ID)
                        log.warning("ndi-preview: a project was loaded during the "
                                    "preview send; preview unloaded")
                msgs = await self.journal.read(req.target, req.mark)
                self._note_version(req.target, msgs)
                v = classify(msgs, req.source)
                if v.state == "terminal-complete":
                    continue
                if v.state != "pending":
                    if v.state in ("frames", "no_frames_yet"):
                        # One more read so the re-fit on the real size is seen.
                        await asyncio.sleep(POLL_S)
                        v = classify(await self.journal.read(req.target, req.mark), req.source)
                    req.verdict = v
                    req.confirmed_at = time.time()
                    if v.reason == "unknown_output":
                        # It would sit visible at canvas centre otherwise.
                        self._send(req.target, "/videocomposer/layer/unload", LAYER_ID)
                    log.info("ndi-preview: %s on %s → %s%s", req.source, req.target.key,
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

    async def stop(self, node: str | None = None, all_: bool = False) -> list[dict]:
        targets: list[Target] = []
        if all_:
            loop = asyncio.get_running_loop()
            nodes = await loop.run_in_executor(None, network_map.parse, self.cfg.network_map_path)
            keys = [None] + [n.role_id or n.alias or n.hostname or n.ip for n in nodes
                             if n.node_type == "NodeType.slave"]
            resolved = await asyncio.gather(*(self.resolve_target(k) for k in keys if k or k is None),
                                            return_exceptions=True)
            seen: set[str] = set()
            for t in resolved:
                if isinstance(t, Target) and t.address not in seen:
                    seen.add(t.address)
                    targets.append(t)
        else:
            targets.append(await self.resolve_target(node))
        for t in targets:
            self._send(t, "/videocomposer/layer/unload", LAYER_ID)
        if self._last and any(t.address == self._last.target.address for t in targets):
            self._last.stopped = True
        log.info("ndi-preview: stop sent to %s", ", ".join(t.address for t in targets))
        return [t.public() for t in targets]

    async def status(self) -> dict:
        eng = self.bridge.engine
        body: dict = {
            "engine": {"running": eng.running, "load": eng.load, "armed": eng.armed},
            "auto_load": self.bridge.auto_load_state(),
            "vc_versions": dict(self._versions),
            "last": None,
        }
        req = self._last
        if req is not None:
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
            body["last"] = req.public()
        return body

    # ---------------- HTTP ----------------

    def register(self, app: web.Application) -> None:
        app.router.add_get("/ndi/sources", self.h_sources)
        app.router.add_get("/ndi/outputs", self.h_outputs)
        app.router.add_post("/ndi/preview", self.h_preview)
        app.router.add_post("/ndi/stop", self.h_stop)
        app.router.add_get("/ndi/status", self.h_status)

    def _auth(self, request: web.Request, key: str) -> web.Response | None:
        if not self.bridge._check_token(request):
            return self.bridge._err("bad_token", 401)
        if not self.bridge._rate.allow(key):
            return self.bridge._err("rate_limited", 429)
        return None

    @staticmethod
    def _fail(e: PreviewError) -> web.Response:
        return web.json_response({"ok": False, "reason": e.reason, **e.extra}, status=e.status)

    async def _body(self, request: web.Request) -> dict:
        try:
            return dict(await request.json() or {})
        except Exception:
            return {}

    async def h_sources(self, request: web.Request) -> web.Response:
        if (r := self._auth(request, "ndi_sources")):
            return r
        try:
            timeout = int(request.query.get("timeout", "3"))
            target, found = await self.sources(request.query.get("node"), timeout)
        except PreviewError as e:
            return self._fail(e)
        except ValueError:
            return self.bridge._err("bad_timeout", 400)
        body = {"ok": True, **target.public(), "list": target.key,
                "sources": [{"n": i + 1, "name": s.name, "addr": s.address}
                            for i, s in enumerate(found)]}
        if not found:
            body["hint"] = ("no NDI source seen from this VC: put the laptop on the nodes' "
                            "switch (adapter on DHCP/automatic, or 169.254.0.0/16 on-link)")
        return web.json_response(body)

    async def h_outputs(self, request: web.Request) -> web.Response:
        if (r := self._auth(request, "ndi_outputs")):
            return r
        try:
            target, outs = await self.outputs(request.query.get("node"))
        except PreviewError as e:
            return self._fail(e)
        return web.json_response({"ok": True, **target.public(), "outputs": outs})

    async def h_preview(self, request: web.Request) -> web.Response:
        if (r := self._auth(request, "ndi_preview")):
            return r
        body = await self._body(request)
        source = body.get("source") or request.query.get("source")
        if not source:
            return self.bridge._err("missing_source", 400)
        wait = bool(body.get("wait")) or request.query.get("wait") == "1"
        try:
            result = await self.show(
                str(source), body.get("node") or request.query.get("node"),
                body.get("output") or request.query.get("output"),
                str(body.get("mode") or request.query.get("mode") or "fill"),
                wait=wait, force_unknown_engine=bool(body.get("force_unknown_engine")))
        except PreviewError as e:
            return self._fail(e)
        return web.json_response({"ok": True, **result}, status=200 if wait else 202)

    async def h_stop(self, request: web.Request) -> web.Response:
        if (r := self._auth(request, "ndi_stop")):
            return r
        body = await self._body(request)
        try:
            stopped = await self.stop(body.get("node") or request.query.get("node"),
                                      bool(body.get("all")) or request.query.get("all") == "1")
        except PreviewError as e:
            return self._fail(e)
        return web.json_response({"ok": True, "stopped": stopped})

    async def h_status(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, **(await self.status())})

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for t in list(self._tasks):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
