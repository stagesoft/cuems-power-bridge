# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""POST /loadproject, POST /unloadproject, GET /projects and the operation
record (plan 869fghmxn S3–S6).

The real EditorClient and EngineClient are used without sockets: editor
frames go out through a recording send_action() and come back through
_resolve(); engine frames are fed with EngineClient._apply_status(), in the
engines' own orders (see test_engine_state_fresh_arm)."""

import asyncio
import json

import pytest

from cuemspowerbridge import cluster_bus, network_map, project_nodes, project_ops
from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.editor_client import EditorClient
from cuemspowerbridge.engine_state import EngineClient
from cuemspowerbridge.ndi_preview import guard

UA = "0123abcd-4567-89ab-cdef-0123456789ab"
UB = "fedcba98-7654-3210-fedc-ba9876543210"
CTRL = "9cf04acb-f784-470e-a7f8-c14d77003c2f"
NODE1 = "e1684ce4-d3f1-447d-bb01-c53af3a3d66d"
PROJECTS = [(UA, "Show A", "show_a"), (UB, "Show B", "show_b")]


class FakeEditor(EditorClient):
    """Records frames; `on_ready(uuid)` / `on_unload()` script what follows."""

    def __init__(self):
        super().__init__("ws://test")
        self.connected = True
        self.sent = []
        self.on_ready = None
        self.on_unload = None
        self.fail_actions = set()

    async def send_action(self, action, value):
        if action in self.fail_actions:
            return False
        self.sent.append((action, value))
        loop = asyncio.get_running_loop()
        if action == "project_list":
            loop.call_soon(self._resolve, {"type": "project_list", "value": [
                {u: {"name": n, "unix_name": x, "modified": ""}} for u, n, x in PROJECTS]})
        elif action == "project_ready" and self.on_ready is not None:
            loop.call_soon(self.on_ready, value)
        elif action == "project_unload" and self.on_unload is not None:
            loop.call_soon(self.on_unload)
        return True

    def readies(self):
        return [v for a, v in self.sent if a == "project_ready"]


class _Req:
    def __init__(self, query=None, headers=None, body=None):
        self.query = query or {}
        self.headers = headers or {}
        self.remote = "127.0.0.1"
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


def _feed(b, *frames):
    for key, value in frames:
        b.engine._apply_status(key, value)


def _bridge(**cfg_kw) -> Bridge:
    cfg = Config()
    for k, v in cfg_kw.items():
        setattr(cfg, k, v)
    b = Bridge(cfg)
    b.engine = EngineClient("ws://test")
    b.engine.connected = True
    b.editor = FakeEditor()
    b.engine.on_status(b.projects.on_engine_status)
    _feed(b, ("running", "no"), ("armed", "no"), ("load", ""))
    return b


def _arm_script(b, unix, *, reply=True, arm=True, cw=None):
    """What the engine and editor do for a load: armed no, load, [cw],
    editor OK, armed yes."""
    def run(uuid):
        _feed(b, ("armed", "no"), ("load", unix))
        if cw is not None:
            _feed(b, ("cluster_warning", json.dumps(cw)))
        if reply:
            b.editor._resolve({"type": "project_ready", "value": uuid})
        if arm:
            _feed(b, ("armed", "yes"))
    return run


async def _settle(n=20):
    for _ in range(n):
        await asyncio.sleep(0)


async def _until(cond, n=200):
    for _ in range(n):
        if cond():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")


def _cancel_loop(b):
    """A restarted auto-load loop is a real task; tests don't let it run."""
    if b._auto_load_task is not None:
        b._auto_load_task.cancel()


@pytest.fixture(autouse=True)
def _no_files(monkeypatch):
    """Default node check: the project uses no remote node."""
    monkeypatch.setattr(project_nodes, "scan",
                        lambda *a: (set(), [], CTRL, None))


async def _load(b, **query):
    return await b.projects.handle_load(_Req(query=query))


def _body(resp):
    return json.loads(resp.text)


# ---------------- happy path, gates ----------------

async def test_load_happy_path():
    b = _bridge()
    b.editor.on_ready = _arm_script(b, "show_a")
    r = await _load(b, name="Show A")
    assert r.status == 202 and _body(r)["seq"] == 1
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    st = b._status_payload()
    assert st["project_op"]["state"] == "armed"
    assert st["project_loaded"] == "show_a"
    assert b.editor.readies() == [UA]


async def test_running_refuses_without_any_frame():
    b = _bridge()
    _feed(b, ("load", "show_a"), ("armed", "yes"), ("running", "yes"))
    r = await _load(b, name="Show B")
    assert r.status == 409 and _body(r)["reason"] == "project_running"
    assert b.editor.sent == []


@pytest.mark.parametrize("query,status,reason", [
    ({}, 400, "missing_project"),
    ({"project": "not-a-uuid"}, 400, "invalid_project"),
    ({"name": "Nope"}, 404, "unknown_project"),
])
async def test_parameter_errors(query, status, reason):
    b = _bridge()
    r = await _load(b, **query)
    assert r.status == status and _body(r)["reason"] == reason


async def test_token_and_rate_limit():
    b = _bridge(shared_token="t")
    assert (await _load(b, name="Show A")).status == 401
    b2 = _bridge()
    await _load(b2, name="Show A")
    assert (await _load(b2, name="Show A")).status == 429


async def test_uuid_is_case_insensitive():
    b = _bridge()
    b.editor.on_ready = _arm_script(b, "show_a")
    assert (await _load(b, project=UA.upper())).status == 202
    assert b.editor.readies() == [UA]


# ---------------- no-op, join, one request at a time ----------------

async def test_fresh_arm_is_a_noop():
    b = _bridge()
    b.editor.on_ready = _arm_script(b, "show_a")
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    b._rate = type(b._rate)(0)
    r = await _load(b, name="Show A")
    assert r.status == 200 and _body(r)["already_loaded"] is True
    assert b.editor.readies() == [UA]


@pytest.mark.parametrize("after", ["run", "stop_rearm", "reconnect", "reload_flag"])
async def test_reload_when_not_fresh(after):
    b = _bridge()
    b._rate = type(b._rate)(0)
    b.editor.on_ready = _arm_script(b, "show_a")
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    query = {"name": "Show A"}
    if after == "run":
        _feed(b, ("running", "yes"), ("running", "no"))
    elif after == "stop_rearm":
        _feed(b, ("running", "yes"), ("running", "no"), ("armed", "no"), ("armed", "yes"))
    elif after == "reconnect":
        _feed(b, ("running", "no"), ("armed", "yes"), ("load", "show_a"))
    else:
        query["reload"] = "1"
    r = await _load(b, **query)
    assert r.status == 202
    assert b.editor.readies() == [UA, UA]


async def test_second_press_joins():
    b = _bridge()
    b._rate = type(b._rate)(0)
    await _load(b, name="Show A")                  # editor never answers
    r = await _load(b, name="Show A")
    assert r.status == 202 and _body(r)["joined"] is True and _body(r)["seq"] == 1
    assert b.editor.readies() == [UA]


async def test_other_project_while_pending_is_refused():
    b = _bridge()
    b._rate = type(b._rate)(0)
    await _load(b, name="Show A")
    r = await _load(b, name="Show B")
    assert r.status == 409
    body = _body(r)
    assert body["reason"] == "previous_request_pending"
    assert body["pending"]["seq"] == 1
    assert b.editor.readies() == [UA]


async def test_no_join_after_editor_lost():
    b = _bridge()
    b._rate = type(b._rate)(0)
    await _load(b, name="Show A")
    b.editor._fail_pending("editor socket closed")
    await _settle()
    assert b.projects.op.editor == "lost"
    r = await _load(b, name="Show A")
    assert r.status == 202 and _body(r)["joined"] is False
    assert b.editor.readies() == [UA, UA]


async def test_supersede_after_editor_answered():
    b = _bridge()
    b._rate = type(b._rate)(0)
    b.editor.on_ready = _arm_script(b, "show_a", arm=False)
    await _load(b, name="Show A")
    await _settle()
    first = b.projects.op
    assert first.editor == "ok" and first.state == "loading"
    b.editor.on_ready = _arm_script(b, "show_b")
    r = await _load(b, name="Show B")
    assert r.status == 202
    assert first.state == "superseded" and first.done.is_set()
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    assert b.projects.op.state == "armed"


async def test_failed_send_leaves_previous_op():
    b = _bridge()
    b._rate = type(b._rate)(0)
    b.editor.on_ready = _arm_script(b, "show_a")
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    first = b.projects.op
    b.editor.fail_actions = {"project_ready"}
    r = await _load(b, name="Show B")
    assert r.status == 502 and _body(r)["reason"] == "editor_send_failed"
    assert b.projects.op is first and first.state == "armed"
    assert b.editor.outstanding() is None


# ---------------- outcomes ----------------

async def test_definitive_editor_error_fails():
    b = _bridge()

    def err(uuid):
        b.editor._resolve({"type": "error", "action": "project_ready", "uuid": uuid,
                           "value": "Engine reports error: Project script error"})
    b.editor.on_ready = err
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    assert b.projects.op.state == "failed" and b.projects.op.reason == "editor_error"


async def test_editor_timeout_keeps_watching_the_engine():
    b = _bridge()

    def slow(uuid):
        b.editor._resolve({"type": "error", "action": "project_ready", "uuid": uuid,
                           "value": "Timeout: Engine did not respond in 25s"})
        _feed(b, ("armed", "no"), ("load", "show_a"), ("armed", "yes"))
    b.editor.on_ready = slow
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    await _settle()
    assert b.projects.op.state == "armed" and b.projects.op.editor == "timeout"


async def test_go_before_the_load_reached_the_engine_fails():
    b = _bridge()
    _feed(b, ("load", "show_b"), ("armed", "yes"))
    await _load(b, name="Show A")
    _feed(b, ("running", "yes"))               # a GO on show_b, before armed no
    assert b.projects.op.state == "failed"
    assert b.projects.op.reason == "project_running"


async def test_stale_yes_is_not_accepted():
    b = _bridge()
    _feed(b, ("load", "show_a"), ("armed", "yes"))
    await _load(b, name="Show A", reload="1")
    _feed(b, ("armed", "yes"), ("load", "show_a"))     # a reconnect dump
    assert b.projects.op.state == "loading"
    _feed(b, ("armed", "no"), ("armed", "yes"))
    assert b.projects.op.state == "armed"


async def test_accepts_on_the_last_frame_of_a_reconnect_dump():
    b = _bridge()
    await _load(b, name="Show A")
    _feed(b, ("armed", "no"))                          # our load started
    _feed(b, ("running", "no"), ("armed", "yes"))      # dump: load not yet sent
    assert b.projects.op.state == "loading"
    _feed(b, ("load", "show_a"))
    assert b.projects.op.state == "armed"


async def test_wait_outcomes(monkeypatch):
    b = _bridge()
    b.editor.on_ready = _arm_script(b, "show_a")
    r = await _load(b, name="Show A", wait="1")
    assert r.status == 200 and _body(r)["state"] == "armed"

    b = _bridge()
    b.editor.on_ready = _arm_script(
        b, "show_a", cw={"load_id": 3, "project": "show_a", "missing": [],
                         "unreachable": [NODE1]})
    r = await _load(b, name="Show A", wait="1")
    assert r.status == 207 and _body(r)["state"] == "degraded"

    b = _bridge()
    b.cfg.op_budget_s = lambda: 0.05
    monkeypatch.setattr(project_ops, "EDITOR_WARN_S", 10)
    r = await _load(b, name="Show A", wait="1")        # nobody answers
    assert r.status == 504 and _body(r)["reason"] == "timeout"
    assert b.projects.op.overdue is True


async def test_shutdown_wakes_a_waiter_with_503():
    b = _bridge()
    task = asyncio.create_task(_load(b, name="Show A", wait="1"))
    await _until(lambda: b.projects.op is not None)
    b.projects.abort("shutdown")
    r = await asyncio.wait_for(task, 1)
    assert r.status == 503 and _body(r)["reason"] == "shutdown"


async def test_editor_lost_without_engine_evidence_fails(monkeypatch):
    monkeypatch.setattr(project_ops, "EDITOR_LOST_GRACE_S", 0.01)
    b = _bridge()
    await _load(b, name="Show A")
    b.editor._fail_pending("editor socket closed")
    await asyncio.sleep(0.05)
    assert b.projects.op.state == "failed" and b.projects.op.reason == "editor_lost"


# ---------------- check 12: needed nodes ----------------

def _nodes_scan(monkeypatch, peers):
    nodes = [network_map.Node(uuid=CTRL, avahi=None, role_id="controller", alias=None,
                              hostname=None, node_type="NodeType.master",
                              ip="10.0.0.10", adopted=True),
             network_map.Node(uuid=NODE1, avahi=None, role_id="node01", alias=None,
                              hostname=None, node_type="NodeType.slave",
                              ip="10.0.0.11", adopted=True)]
    monkeypatch.setattr(project_nodes, "scan",
                        lambda *a: ({CTRL, NODE1}, nodes, CTRL, None))

    async def hub(port):
        return peers
    monkeypatch.setattr(cluster_bus, "hub_peers_or_none", hub)


async def test_needed_node_off_bus_refuses(monkeypatch):
    _nodes_scan(monkeypatch, set())
    b = _bridge()
    r = await _load(b, name="Show A")
    assert r.status == 409
    body = _body(r)
    assert body["reason"] == "nodes_not_ready"
    assert body["nodes"] == [{"uuid": NODE1, "label": "node01", "expected_ip": "10.0.0.11"}]
    assert b.editor.readies() == []


async def test_forced_load_is_degraded_on_a_released_engine(monkeypatch):
    _nodes_scan(monkeypatch, set())
    b = _bridge()
    b.editor.on_ready = _arm_script(b, "show_a")
    r = await _load(b, name="Show A", force="1")
    assert r.status == 202 and _body(r)["nodes_missing"] == ["node01"]
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    assert b.projects.op.state == "degraded"


async def test_forced_load_trusts_a_clean_rc1_cluster_warning(monkeypatch):
    _nodes_scan(monkeypatch, set())
    b = _bridge()
    b.editor.on_ready = _arm_script(
        b, "show_a", cw={"load_id": 4, "project": "show_a", "missing": [],
                         "unreachable": []})
    await _load(b, name="Show A", force="1")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    assert b.projects.op.state == "armed"


async def test_failed_ss_never_refuses(monkeypatch):
    _nodes_scan(monkeypatch, None)
    b = _bridge()
    r = await _load(b, name="Show A")
    assert r.status == 202 and _body(r)["nodes_unchecked"] == ["node01"]


async def test_unreadable_files_load_anyway(monkeypatch):
    monkeypatch.setattr(project_nodes, "scan",
                        lambda *a: (None, [], None, "script.xml unreadable"))
    b = _bridge()
    r = await _load(b, name="Show A")
    assert r.status == 202 and _body(r)["node_check"] == "unavailable"


async def test_dry_run_sends_nothing(monkeypatch):
    _nodes_scan(monkeypatch, set())
    b = _bridge()
    r = await _load(b, name="Show A", check="1")
    body = _body(r)
    assert r.status == 200 and body["would"] == "refuse"
    assert body["reason"] == "nodes_not_ready"
    assert body["node_report"]["needed"][0]["bus"] == "off_bus"
    assert b.editor.readies() == [] and b.projects.op is None


# ---------------- unload ----------------

def _unload_script(b, *, value="OK", load_after=""):
    def run():
        if value == "OK":
            _feed(b, ("armed", "no"), ("load", load_after))
            b.editor._resolve({"type": "project_unload", "value": "OK"})
        else:
            b.editor._resolve({"type": "error", "action": "project_unload",
                               "value": value})
    return run


async def test_unload_happy_path_and_suspension():
    b = _bridge(auto_load_project=UA)
    _feed(b, ("load", "show_a"), ("armed", "yes"))
    b.editor.on_unload = _unload_script(b)
    r = await b.projects.handle_unload(_Req())
    assert r.status == 200 and _body(r)["scope"] == "controller"
    assert b._auto_load_disabled and b._auto_load_disabled_reason == "operator_unload"


async def test_unload_refused_while_running_no_frame_no_suspension():
    b = _bridge(auto_load_project=UA)
    _feed(b, ("load", "show_a"), ("armed", "yes"), ("running", "yes"))
    r = await b.projects.handle_unload(_Req())
    assert r.status == 409 and b.editor.sent == []
    assert not b._auto_load_disabled


async def test_unload_engine_refusal_maps_to_409():
    b = _bridge(auto_load_project=UA)
    _feed(b, ("load", "show_a"), ("armed", "yes"))
    b.editor.on_unload = _unload_script(
        b, value="Engine reports error: Command <class 'RuntimeError'>: "
                 "Cannot unload while running. Stop playback first.")
    r = await b.projects.handle_unload(_Req())
    assert r.status == 409 and _body(r)["reason"] == "project_running"
    assert not b._auto_load_disabled


async def test_already_unloaded():
    b = _bridge()
    r = await b.projects.handle_unload(_Req())
    assert r.status == 200 and _body(r)["already_unloaded"] is True
    assert b.editor.sent == []


async def test_unload_unconfirmed(monkeypatch):
    monkeypatch.setattr(project_ops, "UNLOAD_CONFIRM_S", 0.05)
    b = _bridge()
    _feed(b, ("load", "show_a"), ("armed", "yes"))
    b.editor.on_unload = lambda: b.editor._resolve({"type": "project_unload",
                                                    "value": "OK"})
    r = await b.projects.handle_unload(_Req())
    assert r.status == 200 and _body(r)["confirmed"] is False


# ---------------- GET /projects, /status ----------------

async def test_projects_endpoint():
    b = _bridge(shared_token="t")
    assert (await b.projects.handle_projects(_Req())).status == 401
    _feed(b, ("load", "show_b"))
    r = await b.projects.handle_projects(_Req(headers={"X-Auth-Token": "t"}))
    body = _body(r)
    assert [p["uuid"] for p in body["projects"]] == [UA, UB]
    assert body["loaded"] == UB


async def test_status_derivation(monkeypatch):
    monkeypatch.setattr(project_ops, "STABLE_S", 0)
    b = _bridge()
    assert b._status_payload()["project_op"] is None
    b.editor.on_ready = _arm_script(b, "show_a")
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    st = lambda: b._status_payload()["project_op"]["state"]  # noqa: E731
    assert st() == "armed"
    _feed(b, ("running", "yes"), ("running", "no"))
    assert st() == "played"
    b.engine.connected = False
    assert st() == "played"                  # engine unknown keeps the last
    assert b._status_payload()["project_op"]["engine"] == "unknown"
    b.engine.connected = True
    _feed(b, ("load", "show_b"))
    assert st() == "replaced"
    _feed(b, ("load", ""))
    assert st() == "unloaded"


async def test_status_keeps_every_old_key():
    b = _bridge()
    st = b._status_payload()
    for key in ("state", "engine_state", "nodes_pending", "autoload_pending",
                "auto_load_armed", "nextcue", "nextcue_name", "displays",
                "project_loaded", "project_op", "auto_load"):
        assert key in st


# ---------------- auto-load interplay ----------------

async def test_loading_the_auto_project_keeps_auto_load_and_counts_done():
    b = _bridge(auto_load_project=UA)
    b.editor.on_ready = _arm_script(b, "show_a")
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    assert not b._auto_load_disabled
    assert b._auto_load_done is True and b._auto_load_unix_name == "show_a"
    _cancel_loop(b)


async def test_loading_another_project_suspends_auto_load():
    b = _bridge(auto_load_project=UA)
    b.editor.on_ready = _arm_script(b, "show_b")
    await _load(b, name="Show B")
    assert b._auto_load_disabled and b._auto_load_disabled_reason == "operator_load"


async def test_operator_press_joins_auto_loads_op():
    b = _bridge(auto_load_project=UA)
    op = await b.projects.start_auto_load(UA)
    assert op is not None and op.source == "auto_load"
    r = await _load(b, name="Show A")
    assert _body(r)["joined"] is True and _body(r)["source"] == "auto_load"
    assert b.editor.readies() == [UA]


async def test_auto_load_skips_while_the_slot_is_held():
    b = _bridge(auto_load_project=UA)
    await _load(b, name="Show B")
    assert await b.projects.start_auto_load(UA) is None
    assert b.editor.readies() == [UB]


async def test_operator_load_pauses_and_restarts_the_loop():
    b = _bridge(auto_load_project=UA)
    b._auto_load_task = asyncio.create_task(asyncio.sleep(100))
    b.editor.on_ready = _arm_script(b, "show_a")
    await _load(b, name="Show A")
    await asyncio.wait_for(b.projects.op.done.wait(), 1)
    assert b._auto_load_task is not None and b._auto_load_task.get_name() == "auto-load"
    _cancel_loop(b)


async def test_no_restart_while_stopping():
    b = _bridge(auto_load_project=UA)
    b._stopping = True
    await _load(b, name="Show A")
    b.projects.abort("shutdown")
    assert b._auto_load_task is None


# ---------------- NDI, config ----------------

def test_ndi_guard_refuses_while_a_load_is_tracked():
    class E:
        connected, load, running = True, "", "no"
    assert guard(E(), False, op_loading=True).reason == "project_op_in_progress"
    assert guard(E(), False) is None


async def test_ndi_ignores_an_overdue_op():
    b = _bridge()
    await _load(b, name="Show A")
    assert b.ndi._op_loading() is True
    b.projects.op.overdue = True
    assert b.ndi._op_loading() is False


def test_op_budget_accessor():
    c = Config()
    assert c.op_budget_s() == 450
    c.project_op_timeout_s = "5"
    assert c.op_budget_s() == 60
    c.project_op_timeout_s = "99999"
    assert c.op_budget_s() == 3600
    c.project_op_timeout_s = "4 50"
    assert c.op_budget_s() == 450


def test_config_load_never_raises_on_the_new_key(tmp_path):
    from cuemspowerbridge import config
    p = tmp_path / "pb.conf"
    p.write_text("project_op_timeout_s = soon\n")
    cfg = config.load(str(p))
    assert cfg.op_budget_s() == 450
