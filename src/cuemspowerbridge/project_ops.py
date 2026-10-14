# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Load and unload a project over HTTP (plan 869fghmxn): POST /loadproject,
POST /unloadproject, GET /projects, and the operation record /status shows.

The load goes through the editor (`project_ready <uuid>`), like the web UI;
readiness is read from the engine's status broadcasts (`armed == "yes"` for
the loaded unix_name). Neither endpoint ever stops a show: both refuse with
409 `project_running` while one plays.

One operation at a time: at most one `project_ready`/`project_unload` of the
bridge's (auto-load included) is outstanding at the editor, because the
editor runs requests concurrently and a later request can overtake an
earlier one on its way to the engine (see editor_client). Each operation is
an `Op` record updated by events — engine status frames, the editor reply,
two timers — never by a polling task, so superseding one is just replacing
the record.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import cluster_bus, project_nodes
from .editor_client import (EditorDisconnected, EditorRequest, RequestPending,
                            match_project_ready)
from .engine_state import UNKNOWN

if TYPE_CHECKING:  # pragma: no cover
    from .bridge import Bridge

log = logging.getLogger(__name__)

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

LIST_TIMEOUT_S = 5.0        # editor project_list round trip
SCAN_TIMEOUT_S = 3.0        # script.xml / settings.xml / network_map.xml reads
UNLOAD_TIMEOUT_S = 30.0     # editor project_unload round trip
UNLOAD_CONFIRM_S = 2.0      # wait for the engine's `load ""` after the reply
EDITOR_LOST_GRACE_S = 30.0  # engine evidence expected after an editor drop
EDITOR_WARN_S = 60.0        # log a request the editor has not answered
STABLE_S = 3.0              # a `load` value must hold this long to count
PROJECTS_CACHE_S = 2.0      # GET /projects only
MESSAGE_MAX = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _flag(params: dict, key: str) -> bool:
    return str(params.get(key, "")).strip().lower() in ("1", "true", "yes")


class OpRefused(Exception):
    """An HTTP refusal: status, reason token, extra body keys."""

    def __init__(self, status: int, reason: str, **extra: Any):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.extra = extra

    def response(self) -> web.Response:
        return web.json_response({"ok": False, "reason": self.reason, **self.extra},
                                 status=self.status)


@dataclass(eq=False)
class Op:
    seq: int
    source: str                 # operator | auto_load
    kind: str                   # load | unload
    project: str = ""           # uuid (load)
    name: str = ""
    unix_name: str = ""
    state: str = "loading"      # loading | armed | degraded | failed | superseded | unloaded
    reason: str | None = None
    message: str | None = None
    started: str = field(default_factory=_now_iso)
    started_mono: float = field(default_factory=time.monotonic)
    editor: str = "pending"     # pending | ok | timeout | error | lost
    saw_not_armed: bool = False
    load_seen: bool = False
    accepted: bool = False
    ran: bool = False
    forced: bool = False
    nodes_missing: list[str] = field(default_factory=list)
    nodes_unchecked: list[str] = field(default_factory=list)
    nodes_not_in_cluster: list[str] = field(default_factory=list)
    node_check: str = "ok"
    replaced: str | None = None
    overdue: bool = False
    caller: str = ""
    request: EditorRequest | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    timers: list[asyncio.TimerHandle] = field(default_factory=list)
    last_derived: str = "loading"

    def summary(self) -> dict:
        return {"seq": self.seq, "source": self.source, "kind": self.kind,
                "project": self.project, "name": self.name,
                "elapsed_s": round(time.monotonic() - self.started_mono, 1)}


@dataclass
class _Params:
    project: str | None
    name: str | None
    reload: bool
    force: bool
    check: bool
    wait: bool


class ProjectOps:
    def __init__(self, bridge: "Bridge"):
        self.bridge = bridge
        self._lock = asyncio.Lock()
        self._seq = 0
        self.op: Op | None = None
        # The op whose frame is being sent: engine frames that arrive while
        # send_registered() awaits the socket write are applied to it too.
        self._next: Op | None = None
        self._list_cache: tuple[float, list[dict]] | None = None
        self._load_value: str | None = None
        self._load_since = time.monotonic()
        # One per sent request, independent of the op's own timers (an op can
        # finish — e.g. an unload answered 504 — while its request still holds
        # the editor's one-request slot).
        self._watchdogs: list[asyncio.TimerHandle] = []

    def register(self, app: web.Application) -> None:
        app.router.add_post("/loadproject", self.handle_load)
        app.router.add_post("/unloadproject", self.handle_unload)
        app.router.add_get("/projects", self.handle_projects)

    # ------------------- small accessors -------------------

    @property
    def engine(self):
        return self.bridge.engine

    @property
    def editor(self):
        return self.bridge.editor

    def current_seq(self) -> int | None:
        return self.op.seq if self.op is not None else None

    def in_flight(self) -> bool:
        return self.op is not None and self.op.state == "loading"

    def op_loading(self) -> bool:
        """A load is being tracked and is not overdue (the NDI guard)."""
        op = self.op
        return (op is not None and op.kind == "load" and op.state == "loading"
                and not op.overdue)

    # ------------------- engine events -------------------

    def on_engine_status(self, key: str, value: Any) -> None:
        """Engine status listener, registered unconditionally in start().
        Runs on the bridge's loop for every /engine/status/* frame, after
        EngineClient has updated its cache."""
        if key == "load":
            v = str(value) if value is not None else ""
            if v != self._load_value:
                self._load_value = v
                self._load_since = time.monotonic()
        if key not in ("armed", "load", "running", "cluster_warning"):
            return
        for op in (self._next, self.op):
            if op is None or op.kind != "load":
                continue
            if key == "armed" and not op.accepted and str(value or "") != "yes":
                op.saw_not_armed = True
            elif key == "load":
                op.load_seen = True
            elif key == "running" and value == "yes":
                if op.accepted:
                    op.ran = True
                elif op.state == "loading" and not op.saw_not_armed:
                    # The engine drops a load while running, silently: a GO
                    # played the old show before this load reached it.
                    self._finish(op, "failed", "project_running",
                                 "a GO started the loaded show before this "
                                 "load reached the engine")
                    continue
            self._evaluate(op)

    def _evaluate(self, op: Op) -> None:
        if op.kind != "load" or op.accepted or op.state != "loading":
            return
        eng = self.engine
        if eng.armed != "yes" or not op.saw_not_armed:
            return
        if op.unix_name:
            if eng.load != op.unix_name:
                return
        elif eng.load in ("", UNKNOWN):
            return
        else:
            op.unix_name = eng.load   # auto-load could not resolve it up front
        op.accepted = True
        self._finish(op, "degraded" if self._degraded(op) else "armed")

    def _degraded(self, op: Op) -> bool:
        """D13: a node the project needs, adopted, was not armed. rc_1 says so
        in cluster_warning.unreachable (preferred); released engines publish
        nothing, so a forced load past missing nodes is the evidence."""
        eng = self.engine
        cw = eng.cluster_warning
        if (eng.cluster_warning_seen and cw and cw.get("load_id")
                and cw.get("project") == op.unix_name):
            return bool(cw.get("unreachable"))
        return op.forced and bool(op.nodes_missing)

    # ------------------- op lifecycle -------------------

    def _new_op(self, **kw: Any) -> Op:
        self._seq += 1
        return Op(seq=self._seq, **kw)

    def _install(self, op: Op) -> None:
        """Make `op` current; supersede the previous one if it was still
        loading. Called right after its frame was sent."""
        prev = self.op
        if prev is not None and prev.state == "loading":
            prev.state = "superseded"
            prev.reason = f"by #{op.seq}"
            self._cancel_timers(prev)
            prev.done.set()
            log.info("project op #%d superseded by #%d", prev.seq, op.seq)
        self.op = op
        loop = asyncio.get_running_loop()
        budget = self.bridge.cfg.op_budget_s()
        op.timers.append(loop.call_later(budget, self._on_budget, op))
        op.timers.append(loop.call_later(EDITOR_WARN_S, self._on_editor_warn, op))
        if op.request is not None:
            op.request.future.add_done_callback(
                lambda fut, op=op: self._on_editor_reply(op, fut))
            self._watchdogs = [h for h in self._watchdogs if not h.cancelled()]
            self._watchdogs.append(loop.call_later(
                budget, self._slot_watchdog, op.seq, op.request, budget))
        log.info("project op #%d %s %s %r (%s) started, caller %s",
                 op.seq, op.source, op.kind, op.name or op.project,
                 op.project or "-", op.caller or "-")

    @staticmethod
    def _cancel_timers(op: Op) -> None:
        for h in op.timers:
            h.cancel()
        op.timers.clear()

    def _finish(self, op: Op, state: str, reason: str | None = None,
                message: str | None = None) -> None:
        if op.state in ("failed", "superseded"):
            return
        op.state = state
        op.last_derived = state
        op.reason = reason
        if message:
            op.message = message[:MESSAGE_MAX]
        self._cancel_timers(op)
        op.done.set()
        elapsed = time.monotonic() - op.started_mono
        level = logging.INFO if state in ("armed", "unloaded") else logging.WARNING
        log.log(level, "project op #%d %s %s %r: %s after %.1f s%s",
                op.seq, op.source, op.kind, op.name or op.project, state, elapsed,
                f" [{reason}]" if reason else "")
        self.bridge._on_op_terminal(op)

    def abort(self, reason: str) -> None:
        """Shutdown / stop(): fail a loading op and wake its waiters."""
        for h in self._watchdogs:
            h.cancel()
        self._watchdogs.clear()
        op = self.op
        if op is not None and op.state == "loading":
            self._finish(op, "failed", reason)

    def _on_editor_reply(self, op: Op, fut: asyncio.Future) -> None:
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            op.editor = "lost"
            if op is self.op and op.state == "loading" and op.kind == "load":
                log.warning("project op #%d: editor connection lost before its "
                            "reply; waiting %.0f s for the engine",
                            op.seq, EDITOR_LOST_GRACE_S)
                op.timers.append(asyncio.get_running_loop().call_later(
                    EDITOR_LOST_GRACE_S, self._on_editor_lost, op))
            return
        obj = fut.result() or {}
        if obj.get("type") != "error":
            op.editor = "ok"
            return
        msg = str(obj.get("value", ""))
        if msg.startswith("Timeout:"):
            # The editor's own 25 s deadline: the request is in the engine's
            # queue and may still run. Keep watching the engine.
            op.editor = "timeout"
            op.message = msg[:MESSAGE_MAX]
            log.warning("project op #%d: editor timed out on the engine (%s); "
                        "still watching the engine", op.seq, msg[:120])
            return
        op.editor = "error"
        if op.kind == "load":
            self._finish(op, "failed", "editor_error", msg)

    def _on_editor_lost(self, op: Op) -> None:
        if op is not self.op or op.state != "loading":
            return
        if op.saw_not_armed or op.load_seen:
            return
        self._finish(op, "failed", "editor_lost",
                     "the editor connection dropped before the engine showed "
                     "this load; press again")

    def _on_editor_warn(self, op: Op) -> None:
        req = op.request
        if req is not None and not req.future.done():
            log.warning("project op #%d: the editor has not answered %s for "
                        "%.0f s", op.seq, req.action, req.age_s())

    def _on_budget(self, op: Op) -> None:
        if op.state != "loading" or op.done.is_set():
            return
        op.overdue = True
        budget = self.bridge.cfg.op_budget_s()
        log.warning("project op #%d %s %s %r: overdue after %d s (editor %s, "
                    "armed %r)", op.seq, op.source, op.kind,
                    op.name or op.project, budget, op.editor, self.engine.armed)
        op.done.set()
        self.bridge._on_op_terminal(op)

    def _slot_watchdog(self, seq: int, req: EditorRequest, budget: int) -> None:
        if req.future.done():
            return
        # A silent editor would hold the one-request slot forever; the
        # reconnect fails the request and frees it.
        self.editor.close_socket(
            f"project op #{seq}: no reply to {req.action} in {budget} s")

    # ------------------- editor helpers -------------------

    async def _project_list(self) -> list[dict]:
        ed = self.editor
        if not ed.connected:
            raise OpRefused(503, "editor_unavailable")
        try:
            req = await ed.request("project_list")
        except EditorDisconnected:
            raise OpRefused(503, "editor_unavailable") from None
        if req is None:
            raise OpRefused(503, "editor_unavailable")
        try:
            obj = await ed.wait(req, LIST_TIMEOUT_S)
        except EditorDisconnected:
            raise OpRefused(503, "editor_unavailable") from None
        if obj is None:
            ed.unregister(req)
            raise OpRefused(504, "editor_timeout")
        if obj.get("type") == "error":
            raise OpRefused(502, "editor_error",
                            message=str(obj.get("value", ""))[:MESSAGE_MAX])
        projects: list[dict] = []
        for entry in obj.get("value") or []:
            if not isinstance(entry, dict):
                continue
            for uuid, info in entry.items():
                info = info if isinstance(info, dict) else {}
                projects.append({
                    "uuid": str(uuid).lower(),
                    "name": str(info.get("name", "")),
                    "unix_name": str(info.get("unix_name", "")),
                    "modified": str(info.get("modified", "")),
                })
        return projects

    @staticmethod
    def _find(projects: list[dict], params: _Params) -> dict:
        if params.project:
            hit = [p for p in projects if p["uuid"] == params.project]
        else:
            hit = [p for p in projects if p["name"] == params.name]
        if not hit:
            raise OpRefused(404, "unknown_project")
        return hit[0]

    def _pending_body(self, req: EditorRequest) -> dict:
        op = self.op
        if op is not None and op.request is req:
            return {"pending": op.summary(), "retry_after_s": 2}
        return {"pending": {"action": req.action,
                            "elapsed_s": round(req.age_s(), 1)},
                "retry_after_s": 2}

    # ------------------- parameters -------------------

    @staticmethod
    async def _params(request: web.Request) -> _Params:
        params: dict = dict(request.query)
        if not params.get("project") and not params.get("name"):
            try:
                body = await request.json()
            except Exception:
                body = None
            if isinstance(body, dict):
                params = {**body, **params}
        project = str(params.get("project") or "").strip().lower() or None
        name = str(params.get("name") or "").strip() or None
        if project is None and name is None:
            raise OpRefused(400, "missing_project")
        if project is not None and not _UUID_RE.match(project):
            raise OpRefused(400, "invalid_project")
        return _Params(project=project, name=None if project else name,
                       reload=_flag(params, "reload"), force=_flag(params, "force"),
                       check=_flag(params, "check"), wait=_flag(params, "wait"))

    @staticmethod
    def _caller(request: web.Request) -> str:
        return f"{getattr(request, 'remote', None) or '?'} " \
               f"{request.headers.get('User-Agent', '-')!r}"

    # ------------------- checks -------------------

    def _joinable(self, uuid: str) -> Op | None:
        op = self.op
        if (op is not None and op.kind == "load" and op.project == uuid
                and op.state == "loading" and not op.overdue
                and op.editor in ("pending", "ok")):
            return op
        return None

    def _fresh_noop(self, unix_name: str) -> bool:
        eng = self.engine
        if self.in_flight():
            return False
        if eng.load != unix_name or eng.armed != "yes" or not eng.fresh_arm:
            return False
        cw = eng.cluster_warning
        if cw and cw.get("project") == unix_name and cw.get("unreachable"):
            return False
        op = self.op
        if (op is not None and op.kind == "load" and op.unix_name == unix_name
                and op.accepted and (op.forced or self._degraded(op))):
            return False
        return True

    async def _node_check(self, unix_name: str) -> project_nodes.NodeReport:
        cfg = self.bridge.cfg
        loop = asyncio.get_running_loop()
        try:
            project, nodes, controller, err = await asyncio.wait_for(
                loop.run_in_executor(None, project_nodes.scan,
                                     cfg.settings_xml_path, cfg.network_map_path,
                                     unix_name),
                SCAN_TIMEOUT_S)
        except asyncio.TimeoutError:
            err, project, nodes, controller = "scan timed out", None, [], None
        if err or project is None or controller is None:
            log.warning("load %r: node check unavailable (%s); loading anyway",
                        unix_name, err)
            return project_nodes.NodeReport(node_check="unavailable", reason=err)
        peers: set[str] | None = set()
        if project - {controller}:
            peers = await cluster_bus.hub_peers_or_none(cfg.nng_hub_port)
        report = project_nodes.classify(project, nodes, controller, peers)
        for n in report.unchecked():
            log.warning("load %r: node %s (%s) needed, bus state unknown",
                        unix_name, n["label"], n["uuid"])
        if report.not_in_cluster:
            log.info("load %r: project references nodes not in the cluster "
                     "(their cues will not play): %s", unix_name,
                     ", ".join(report.not_in_cluster))
        if peers:
            used = {n["uuid"] for n in report.needed}
            for n in nodes:
                if (n.adopted and n.node_type == "NodeType.slave" and n.ip
                        and n.uuid.lower() not in used and n.uuid.lower() != controller
                        and n.ip not in peers):
                    log.info("load %r: node %s is off the bus but this project "
                             "does not use it", unix_name,
                             n.role_id or n.alias or n.hostname or n.uuid)
        return report

    def _base_checks(self) -> None:
        if self.bridge._shutdown_lock.locked():
            raise OpRefused(409, "shutdown_already_in_progress")
        if not self.engine.is_known():
            raise OpRefused(503, "engine_state_unknown")
        if not self.editor.connected:
            raise OpRefused(503, "editor_unavailable")
        if self.engine.project_running():
            raise OpRefused(409, "project_running")

    # ------------------- POST /loadproject -------------------

    async def handle_load(self, request: web.Request) -> web.Response:
        b = self.bridge
        if not b._check_token(request):
            return b._err("bad_token", 401)
        try:
            params = await self._params(request)
        except OpRefused as r:
            return r.response()
        if not b._rate.allow("loadproject_check" if params.check else "loadproject"):
            return b._err("rate_limited", 429)
        if params.check:
            return await self._dry_run(params)
        if self._lock.locked():
            return b._err("op_busy", 409)
        await self._lock.acquire()
        try:
            op, body = await self._start_load(params, self._caller(request))
        except OpRefused as r:
            return r.response()
        finally:
            self._lock.release()
        if op is None:          # a no-op or a join: body is the answer
            return web.json_response(body, status=body.pop("_status", 200))
        if not params.wait:
            return web.json_response(body, status=202)
        return await self._wait_answer(op, body)

    async def _start_load(self, params: _Params, caller: str
                          ) -> tuple[Op | None, dict]:
        """Checks 5–13 under the lock. Returns (new op, 202 body) or (None,
        answer body) for a join / no-op; raises OpRefused."""
        b = self.bridge
        self._base_checks()
        proj = self._find(await self._project_list(), params)
        uuid, unix_name = proj["uuid"], proj["unix_name"]
        joined = None if params.reload else self._joinable(uuid)
        if joined is not None:
            log.info("load %r: joined op #%d (caller %s)", proj["name"],
                     joined.seq, caller)
            return None, {"_status": 202, "ok": True, "state": "loading",
                          "seq": joined.seq, "source": joined.source,
                          "project": uuid, "name": proj["name"], "joined": True}
        pending = self.editor.outstanding()
        if pending is not None:
            raise OpRefused(409, "previous_request_pending", **self._pending_body(pending))
        if not params.reload and self._fresh_noop(unix_name):
            log.info("load %r: already loaded and freshly armed; nothing sent "
                     "(caller %s)", proj["name"], caller)
            return None, {"ok": True, "already_loaded": True, "project": uuid,
                          "name": proj["name"]}
        report = await self._node_check(unix_name)
        off = report.off_bus()
        if off and not params.force:
            log.warning("load %r refused: needed node(s) off the bus: %s",
                        proj["name"], ", ".join(n["label"] for n in off))
            raise OpRefused(409, "nodes_not_ready", nodes=[
                {"uuid": n["uuid"], "label": n["label"], "expected_ip": n["ip"]}
                for n in off], peers_seen=report.peers_seen)

        # Teardown that may await (idempotent): a pending auto-play GO must not
        # fire on the project we are about to load, and the auto-load loop must
        # not send its own project_ready behind ours.
        await b._cancel_auto_play_task()
        paused = await b._pause_auto_load()

        # One synchronous block: re-check, register, send.
        try:
            self._base_checks()
            req = self.editor.register("project_ready", uuid,
                                       match=match_project_ready(uuid))
        except RequestPending as e:
            if paused:
                b._restart_auto_load_if_allowed(None)
            raise OpRefused(409, "previous_request_pending",
                            **self._pending_body(e.pending)) from None
        except OpRefused:
            if paused:
                b._restart_auto_load_if_allowed(None)
            raise
        prev_load = self.engine.load
        op = self._new_op(source="operator", kind="load", project=uuid,
                          name=proj["name"], unix_name=unix_name, caller=caller,
                          request=req, forced=bool(off) and params.force,
                          nodes_missing=[n["label"] for n in off],
                          nodes_unchecked=[n["label"] for n in report.unchecked()],
                          nodes_not_in_cluster=list(report.not_in_cluster),
                          node_check=report.node_check)
        if prev_load not in ("", UNKNOWN) and prev_load != unix_name:
            op.replaced = prev_load
        self._next = op
        try:
            sent = await self.editor.send_registered(req)
        finally:
            self._next = None
        if not sent:
            if paused:
                b._restart_auto_load_if_allowed(None)
            raise OpRefused(502, "editor_send_failed")
        self._install(op)
        if b._is_auto_project(uuid):
            pass    # D5: loading the auto project keeps auto-load
        else:
            b._suspend_auto_load("operator_load")
        return op, {"ok": True, "state": "loading", "seq": op.seq,
                    "source": op.source, "project": uuid, "name": op.name,
                    "joined": False, "replaced": op.replaced,
                    "node_check": op.node_check, "nodes_missing": op.nodes_missing,
                    "nodes_unchecked": op.nodes_unchecked,
                    "nodes_not_in_cluster": op.nodes_not_in_cluster,
                    "budget_s": b.cfg.op_budget_s()}

    async def _wait_answer(self, op: Op, body: dict) -> web.Response:
        budget = self.bridge.cfg.op_budget_s() + 5
        try:
            await asyncio.wait_for(op.done.wait(), budget)
        except asyncio.TimeoutError:
            pass
        base = {"seq": op.seq, "project": op.project, "name": op.name,
                "nodes_missing": op.nodes_missing}
        if op.state == "armed":
            return web.json_response({"ok": True, "state": "armed", **base})
        if op.state == "degraded":
            return web.json_response({"ok": False, "state": "degraded",
                                      "reason": "degraded", **base}, status=207)
        if op.state == "superseded":
            return web.json_response({"ok": False, "state": "superseded",
                                      "reason": "superseded", **base}, status=409)
        if op.state == "failed":
            status = 503 if op.reason == "shutdown" else 502
            return web.json_response({"ok": False, "state": "failed",
                                      "reason": op.reason, "message": op.message,
                                      **base}, status=status)
        return web.json_response({"ok": False, "state": "loading",
                                  "reason": "timeout", "timeout_s": budget, **base},
                                 status=504)

    async def _dry_run(self, params: _Params) -> web.Response:
        """`check=1`: what a press would do, and why. Sends nothing, takes no
        lock (D14)."""
        try:
            if not self.engine.is_known():
                raise OpRefused(503, "engine_state_unknown")
            proj = self._find(await self._project_list(), params)
        except OpRefused as r:
            return r.response()
        uuid, unix_name = proj["uuid"], proj["unix_name"]
        report = await self._node_check(unix_name)
        would, reason = "load", None
        pending = self.editor.outstanding()
        if self.bridge._shutdown_lock.locked():
            would, reason = "refuse", "shutdown_already_in_progress"
        elif self.engine.project_running():
            would, reason = "refuse", "project_running"
        elif not params.reload and self._joinable(uuid) is not None:
            would = "join"
        elif pending is not None:
            would, reason = "refuse", "previous_request_pending"
        elif not params.reload and self._fresh_noop(unix_name):
            would = "already_loaded"
        elif report.off_bus() and not params.force:
            would, reason = "refuse", "nodes_not_ready"
        return web.json_response({"ok": True, "check": True, "would": would,
                                  "reason": reason, "project": uuid,
                                  "name": proj["name"], "unix_name": unix_name,
                                  "node_report": report.as_dict()})

    # ------------------- POST /unloadproject -------------------

    async def handle_unload(self, request: web.Request) -> web.Response:
        b = self.bridge
        if not b._check_token(request):
            return b._err("bad_token", 401)
        if not b._rate.allow("unloadproject"):
            return b._err("rate_limited", 429)
        if self._lock.locked():
            return b._err("op_busy", 409)
        caller = self._caller(request)
        await self._lock.acquire()
        try:
            self._base_checks()
            pending = self.editor.outstanding()
            if pending is not None:
                raise OpRefused(409, "previous_request_pending",
                                **self._pending_body(pending))
            if self.engine.load == "" and not self.in_flight():
                log.info("unload: nothing loaded; nothing sent (caller %s)", caller)
                return web.json_response({"ok": True, "already_unloaded": True,
                                          "scope": "controller"})
            await b._cancel_auto_play_task()
            paused = await b._pause_auto_load()
            try:
                self._base_checks()
                req = self.editor.register("project_unload", None)
            except RequestPending as e:
                if paused:
                    b._restart_auto_load_if_allowed(None)
                raise OpRefused(409, "previous_request_pending",
                                **self._pending_body(e.pending)) from None
            except OpRefused:
                if paused:
                    b._restart_auto_load_if_allowed(None)
                raise
            op = self._new_op(source="operator", kind="unload",
                              name=self.engine.load, caller=caller, request=req)
            if not await self.editor.send_registered(req):
                if paused:
                    b._restart_auto_load_if_allowed(None)
                raise OpRefused(502, "editor_send_failed")
            self._install(op)
        except OpRefused as r:
            return r.response()
        finally:
            self._lock.release()
        return await self._await_unload(op, req)

    async def _await_unload(self, op: Op, req: EditorRequest) -> web.Response:
        b = self.bridge
        try:
            obj = await self.editor.wait(req, UNLOAD_TIMEOUT_S)
        except EditorDisconnected:
            self._finish(op, "failed", "editor_unavailable")
            return b._err("editor_unavailable", 503)
        if obj is None:
            self._finish(op, "failed", "editor_timeout")
            return b._err("editor_timeout", 504)
        if obj.get("type") == "error":
            msg = str(obj.get("value", ""))
            if msg.startswith("Timeout:"):
                # The editor's own 25 s deadline: the unload is queued at the
                # engine and may still run.
                self._finish(op, "failed", "editor_timeout", msg)
                return web.json_response({"ok": False, "reason": "editor_timeout",
                                          "message": msg[:MESSAGE_MAX]}, status=504)
            if "Cannot unload while running" in msg:
                self._finish(op, "failed", "project_running", msg)
                return b._err("project_running", 409)
            self._finish(op, "failed", "editor_error", msg)
            return web.json_response({"ok": False, "reason": "editor_error",
                                      "message": msg[:MESSAGE_MAX]}, status=502)
        deadline = time.monotonic() + UNLOAD_CONFIRM_S
        while self.engine.load != "" and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        confirmed = self.engine.load == ""
        if not confirmed:
            log.warning("unload: editor said OK but the engine still reports "
                        "load=%r after %.0f s", self.engine.load, UNLOAD_CONFIRM_S)
        self._finish(op, "unloaded")
        b._suspend_auto_load("operator_unload")
        body = {"ok": True, "scope": "controller", "seq": op.seq}
        if not confirmed:
            body["confirmed"] = False
        return web.json_response(body)

    # ------------------- auto-load's own operations -------------------

    async def start_auto_load(self, uuid: str) -> Op | None:
        """Send auto-load's project_ready as an operation (D12), so an operator
        press of the same project joins it. None when skipped: the lock or the
        one-request slot is taken, the editor is down, or the send failed."""
        if self._lock.locked():
            return None
        async with self._lock:
            uuid = uuid.strip().lower()
            name, unix_name = "", ""
            try:
                for p in await self._project_list():
                    if p["uuid"] == uuid:
                        name, unix_name = p["name"], p["unix_name"]
            except OpRefused as r:
                log.debug("auto-load: project_list unavailable (%s)", r.reason)
            if not self.editor.connected or self.engine.project_running():
                return None
            try:
                req = self.editor.register("project_ready", uuid,
                                           match=match_project_ready(uuid))
            except RequestPending as e:
                log.info("auto-load: %s still pending at the editor; skipping "
                         "this round", e.pending.action)
                return None
            op = self._new_op(source="auto_load", kind="load", project=uuid,
                              name=name, unix_name=unix_name, request=req)
            self._next = op
            try:
                sent = await self.editor.send_registered(req)
            finally:
                self._next = None
            if not sent:
                return None
            self._install(op)
            return op

    # ------------------- GET /projects -------------------

    async def handle_projects(self, request: web.Request) -> web.Response:
        b = self.bridge
        if not b._check_token(request):
            return b._err("bad_token", 401)
        if not b._rate.allow("projects"):
            return b._err("rate_limited", 429)
        now = time.monotonic()
        if self._list_cache is not None and now - self._list_cache[0] < PROJECTS_CACHE_S:
            projects = self._list_cache[1]
        else:
            try:
                projects = await self._project_list()
            except OpRefused as r:
                return r.response()
            self._list_cache = (now, projects)
        load = self.engine.load
        loaded = next((p["uuid"] for p in projects
                       if load not in ("", UNKNOWN) and p["unix_name"] == load), None)
        return web.json_response({"ok": True, "projects": projects, "loaded": loaded})

    # ------------------- /status -------------------

    def _derive(self, op: Op) -> str:
        eng = self.engine
        if not eng.connected or eng.load == UNKNOWN:
            return op.last_derived
        if op.state in ("failed", "superseded"):
            return op.state
        stable = time.monotonic() - self._load_since >= STABLE_S
        load = eng.load
        if op.kind == "load":
            if not op.accepted:
                return "loading"
            if load == "" and stable:
                return "unloaded"
            if load != op.unix_name and load != "" and stable:
                return "replaced"
            if load == op.unix_name:
                if op.ran:
                    return "played"
                return "degraded" if self._degraded(op) else "armed"
            return op.last_derived
        if op.state == "loading":
            return "loading"
        if op.state == "failed":
            return "failed"
        if load != "" and stable:
            return "replaced"
        if load == "":
            return "unloaded"
        return op.last_derived

    def status(self) -> dict | None:
        op = self.op
        if op is None:
            return None
        state = self._derive(op)
        op.last_derived = state
        req = op.request
        pending_s = (round(req.age_s(), 1)
                     if req is not None and not req.future.done() else 0)
        return {
            "seq": op.seq, "source": op.source, "kind": op.kind,
            "project": op.project, "name": op.name, "state": state,
            "editor": op.editor, "editor_pending_s": pending_s,
            "elapsed_s": round(time.monotonic() - op.started_mono, 1),
            "started": op.started, "overdue": op.overdue, "forced": op.forced,
            "reason": op.reason, "message": op.message, "replaced": op.replaced,
            "node_check": op.node_check, "nodes_missing": op.nodes_missing,
            "nodes_unchecked": op.nodes_unchecked,
            "nodes_not_in_cluster": op.nodes_not_in_cluster,
            "engine": "connected" if self.engine.connected else "unknown",
        }
