# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""NDI preview during montajes (/ndi/*, cuems-ndi-preview; ClickUp 869ekxuez).

The VC log lines below are the videocomposer's remote/JournalContract.h
(pinned there by TestNdiPreview's JournalContract test): if one changes on
either side, both must change.
"""

import asyncio
import os
import textwrap
import time as _time

import pytest

from cuemspowerbridge import ndi_preview as ndi
from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import Config
from cuemspowerbridge.engine_state import UNKNOWN
from cuemspowerbridge.ndi_preview import (
    Journal, NdiPreview, PreviewError, Source, Target, classify, guard,
    parse_discover, parse_outputs, parse_version, pick_source,
)

SRC = "LAPTOP (OBS)"


class _Engine:
    def __init__(self, running="no", load="", armed="no", connected=True):
        self.running, self.load, self.armed, self.connected = running, load, armed, connected


# --------------------------------------------------------------------------
# parsers
# --------------------------------------------------------------------------

def test_parse_discover_sorted_last_at_split_and_done():
    msgs = [
        "NDI discover: busy",
        "NDI discover: started (3 s)",
        "NDI discover: ZED (cam) @ 169.254.7.9:5961",
        "NDI discover: A @ B (OBS) @ 169.254.7.10:5962",
        "NDI discover: LAPTOP (OBS) @ -",
        "NDI: Not configured (use NDI SDK)",
        "NDI discover: done (3)",
    ]
    found, busy = parse_discover(msgs)
    assert busy
    assert [s.name for s in found] == ["A @ B (OBS)", "LAPTOP (OBS)", "ZED (cam)"]
    assert found[0].address == "169.254.7.10:5962"
    assert found[1].address == ""


def test_parse_discover_pending_until_done():
    assert parse_discover(["NDI discover: X (y) @ 1.2.3.4:5"]) == (None, False)


def test_parse_outputs_regions_in_canvas_order():
    msgs = [
        "=== Physical Outputs (3) ===",
        "  [0] HDMI-A-1 3840x2160@30Hz",
        "  [1] HDMI-A-2 1920x1080@60Hz",
        "  [2] HDMI-A-3 3840x2160@30Hz",
        "region HDMI-A-3 3840,0,3840,2160",
        "region HDMI-A-1 0,0,3840,2160",
        "region HDMI-A-2 7680,0,1920,1080",
        "=== Capture Status ===",
    ]
    assert parse_outputs(msgs) == [
        {"name": "HDMI-A-1", "mode": "3840x2160@30", "region": "0,0,3840,2160"},
        {"name": "HDMI-A-3", "mode": "3840x2160@30", "region": "3840,0,3840,2160"},
        {"name": "HDMI-A-2", "mode": "1920x1080@60", "region": "7680,0,1920,1080"},
    ]


def test_parse_version_latest_wins():
    assert parse_version(["cuems-videocomposer 0.1.2-8 starting",
                          "x", "cuems-videocomposer 0.1.2-9 starting"]) == "0.1.2-9"
    assert parse_version(["nothing"]) is None


# --------------------------------------------------------------------------
# classify
# --------------------------------------------------------------------------

def _loaded(source=SRC, fmt=True, layer="ndi-preview"):
    lines = [f"NDI: Connected to source: {source}"]
    lines.append("NDI: Source format: 1920x1080 @ 25 fps (BGRA)" if fmt
                 else "NDI: No video frame received, using defaults (1920x1080 @25fps)")
    lines += [f"Async load complete: ndi://{source} (cue ID: {layer})",
              f"fit_output: {layer} -> HDMI-A-1 fill pos -2880,0 scale 1,1 (dims 1920x1080)"]
    return lines


def test_classify_frames_with_fit():
    v = classify(["fit_output: ndi-preview -> HDMI-A-1 fill pos -2880,0 scale 1,1 (fallback)"]
                 + _loaded(), SRC)
    assert v.state == "frames"
    assert v.fit == {"output": "HDMI-A-1", "mode": "fill", "pos": [-2880, 0],
                     "scale": 1.0, "basis": "dims 1920x1080"}  # the LAST fit line


def test_classify_no_frames_yet():
    assert classify(_loaded(fmt=False), SRC).state == "no_frames_yet"


def test_classify_pending_and_other_source_does_not_count():
    assert classify(["NDI: Connected to source: X"], SRC).state == "pending"
    assert classify(_loaded(source="OTHER (OBS)"), SRC).state == "pending"


def test_classify_not_found():
    v = classify([f"NDI: Source not found: {SRC}",
                  f"Async load failed for: ndi://{SRC} (cue ID: ndi-preview)"], SRC)
    assert (v.state, v.reason) == ("failed", "source_not_found")


def test_classify_unknown_output_is_a_failure():
    v = classify(["fit_output: unknown output 'DP-9' (have: HDMI-A-1, HDMI-A-2)"] + _loaded(), SRC)
    assert (v.state, v.reason, v.unknown_output) == ("failed", "unknown_output", "DP-9")


def test_classify_stop_or_reset_mid_load():
    assert classify(["Layer no longer exists for cue ID: ndi-preview"], SRC).state == "superseded"
    assert classify(["AsyncVideoLoader: Discarding result for cancelled cue: ndi-preview"],
                    SRC).state == "superseded"


# --------------------------------------------------------------------------
# guard / auto-load / names
# --------------------------------------------------------------------------

@pytest.mark.parametrize("engine,auto,force,expect", [
    (_Engine(), False, False, None),
    (_Engine(load="show1"), False, False, "project_loaded"),
    (_Engine(load="show1", running="yes"), False, False, "project_running"),
    (_Engine(load=UNKNOWN), False, False, "engine_unknown"),
    (_Engine(running=UNKNOWN), False, False, "engine_unknown"),
    (_Engine(connected=False), False, False, "engine_unknown"),
    (_Engine(load=UNKNOWN), False, True, None),
    (_Engine(), True, False, "auto_load_active"),
    (_Engine(load=UNKNOWN), True, True, "auto_load_active"),
])
def test_guard(engine, auto, force, expect):
    err = guard(engine, auto, force)
    assert (err.reason if err else None) == expect


@pytest.mark.parametrize("project,persistent,done,disabled,active", [
    ("", False, False, False, False),
    ("uuid", False, False, False, True),    # not completed in THIS process
    ("uuid", False, True, False, False),    # once-only load done
    ("uuid", True, True, False, True),      # persistent re-drives
    ("uuid", True, True, True, False),      # loop disabled
])
def test_auto_load_active(project, persistent, done, disabled, active):
    cfg = Config()
    cfg.auto_load_project, cfg.auto_load_persistent = project, persistent
    b = Bridge(cfg)
    b._auto_load_done, b._auto_load_disabled = done, disabled
    assert b.auto_load_active() is active
    assert b.auto_load_state()["active"] is active


def test_pick_source():
    lst = [Source("A (OBS)", ""), Source("LAPTOP (OBS)", ""), Source("MYLAPTOP (OBS)", "")]
    assert pick_source("#2", lst) == "LAPTOP (OBS)"
    assert pick_source("LAPTOP (OBS)", lst) == "LAPTOP (OBS)"  # exact beats substring
    assert pick_source("a (o", lst) == "A (OBS)"
    with pytest.raises(PreviewError) as e:
        pick_source("LAPTOP", lst)
    assert e.value.reason == "ambiguous_source"
    with pytest.raises(PreviewError) as e:
        pick_source("#4", lst)
    assert e.value.reason == "source_not_found"
    assert pick_source("NEW (cam)", None) == "NEW (cam)"  # exact-looking, no list needed
    with pytest.raises(PreviewError):
        pick_source("cam", None)


# --------------------------------------------------------------------------
# target resolution (M-B)
# --------------------------------------------------------------------------

NETMAP = textwrap.dedent("""\
    <network_map>
      <nodes>
        <node><uuid>c-1</uuid><role_id>controller</role_id><node_type>NodeType.master</node_type>
              <ip>169.254.9.204</ip></node>
        <node><uuid>n-1</uuid><role_id>node01</role_id><hostname>node01</hostname>
              <node_type>NodeType.slave</node_type><ip>169.254.13.233</ip></node>
        <node><uuid>n-2</uuid><role_id>node02</role_id><node_type>NodeType.slave</node_type>
              <ip>169.254.20.1</ip></node>
      </nodes>
    </network_map>
""")


MAPPINGS = textwrap.dedent("""\
    <CuemsProjectMappings>
      <nodes>
        <node><uuid>c-1</uuid><video><outputs>
          <output><id>0</id><name>Monitor izquierda</name><mappings><mapped_to>HDMI-A-1</mapped_to></mappings></output>
          <output><id>1</id><name>Emulador EDID A2</name><mappings><mapped_to>HDMI-A-2</mapped_to></mappings></output>
        </outputs></video></node>
        <node><uuid>n-1</uuid><video><outputs>
          <output><id>0</id><name>Monitor derecha</name><mappings><mapped_to>HDMI-A-1</mapped_to></mappings></output>
          <output><id>1</id><name>Emulador EDID A2</name><mappings><mapped_to>HDMI-A-2</mapped_to></mappings></output>
        </outputs></video></node>
        <node><uuid>n-2</uuid><video><outputs>
          <output><id>0</id><name>Emulador EDID A1</name><mappings><mapped_to>HDMI-A-1</mapped_to></mappings></output>
        </outputs></video></node>
      </nodes>
    </CuemsProjectMappings>
""")


def _preview(tmp_path, *, resolve=None, routes=None, engine=None, auto=False):
    cfg = Config()
    p = tmp_path / "network_map.xml"
    p.write_text(NETMAP)
    cfg.network_map_path = str(p)
    m = tmp_path / "default_mappings.xml"
    m.write_text(MAPPINGS)
    cfg.extras["ndi_mappings_path"] = str(m)
    bridge = Bridge(cfg)
    bridge.engine = engine or _Engine()
    bridge.auto_load_active = lambda: auto
    resolve = resolve or {}
    routes = routes or {}

    async def resolver(name):
        return resolve.get(name)

    async def router(addr):
        if addr.startswith("169.254."):
            return routes.get(addr, "ethernet1")
        return routes.get(addr, "bond0")

    sent = []
    pv = NdiPreview(bridge, journal=_FakeJournal(),
                    osc_send=lambda a, port, path, args: sent.append((a, path, args)),
                    resolver=resolver, router=router)
    return pv, sent


async def test_master_is_always_loopback(tmp_path):
    pv, _ = _preview(tmp_path, resolve={"controller.local": "10.16.10.4"})
    for node in (None, "local", "controller"):
        t = await pv.resolve_target(node)
        assert (t.key, t.address) == ("local", "127.0.0.1")


async def test_node_on_cluster_segment_via_avahi(tmp_path):
    pv, _ = _preview(tmp_path, resolve={"node01.local": "169.254.13.233"})
    t = await pv.resolve_target("node01")
    assert (t.address, t.iface, t.journal_ip) == ("169.254.13.233", "ethernet1", "169.254.13.233")
    assert t.via == "avahi node01.local"


async def test_foreign_cluster_answer_is_ignored(tmp_path):
    # Another cluster's node01 answering on the uplink (test2/test3, Medina).
    pv, _ = _preview(tmp_path, resolve={"node01.local": "10.16.10.4"})
    t = await pv.resolve_target("node01")
    assert (t.address, t.via) == ("169.254.13.233", "network_map <ip>")


async def test_unknown_node(tmp_path):
    pv, _ = _preview(tmp_path)
    with pytest.raises(PreviewError) as e:
        await pv.resolve_target("node09")
    assert (e.value.status, e.value.reason) == (400, "bad_node")


# --------------------------------------------------------------------------
# show / stop / status with a fake journal
# --------------------------------------------------------------------------

class _FakeJournal:
    """Returns `script` lines once the load has been sent."""

    def __init__(self):
        self.script: list[str] = []
        self.loaded = False
        self.unreadable = False
        self.transport_line = "NDI receive transport: base TCP (/usr/share/cuems-videocomposer/ndi)"

    async def transport(self, target):
        return self.transport_line

    def check_readable(self, target):
        if self.unreadable:
            raise PreviewError(503, "journal_unreadable", path="/var/log/journal/x")

    async def mark(self, target):
        return {"t": 0}

    async def read(self, target, mark):
        return list(self.script) if self.loaded else []

    async def version(self, target):
        return "0.1.2-8"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(ndi, "POLL_S", 0.01)
    monkeypatch.setattr(ndi, "LOAD_RECHECK_S", 0.0)
    monkeypatch.setattr(ndi, "CONFIRM_TIMEOUT_S", 0.5)
    monkeypatch.setattr(ndi, "STATUS_CACHE_S", 0.0)


def _arm(pv, sent, lines):
    """Make the fake journal answer once the load OSC has gone out."""
    orig = pv._osc_send

    def send(a, port, path, args):
        orig(a, port, path, args)
        if path.endswith("/layer/load"):
            pv.journal.loaded = True
    pv._osc_send = send
    pv.journal.script = lines


NODE01 = "169.254.13.233"


def _seed(pv, address, sources):
    pv._sources_cache[address] = (_time.monotonic(), sources)


async def test_show_by_ui_name_sends_the_sequence_and_confirms(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01})
    _seed(pv, NODE01, [Source(SRC, "169.254.7.9:5961")])   # node01 sees it: direct
    _arm(pv, sent, _loaded(layer="ndi-preview-HDMI-A-1"))
    r = await pv.show(SRC, "Monitor derecha", wait=True)
    assert r["confirm"] == "frames" and r["route"] == "direct"
    assert r["screen"]["alias"] == "node01_HDMI-A-1"
    assert [p for _, p, _ in sent] == [
        "/videocomposer/layer/unload", "/videocomposer/layer/load",
        "/videocomposer/layer/ndi-preview-HDMI-A-1/fit_output",
        "/videocomposer/layer/ndi-preview-HDMI-A-1/zorder",
        "/videocomposer/layer/ndi-preview-HDMI-A-1/visible"]
    assert sent[1] == (NODE01, "/videocomposer/layer/load", [f"ndi://{SRC}", "ndi-preview-HDMI-A-1"])
    assert sent[2][2] == ["HDMI-A-1", "fill"]


async def test_show_by_alias_and_number(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, "169.254.7.9:5961")])
    _arm(pv, sent, _loaded(layer="ndi-preview-HDMI-A-2"))
    r = await pv.show(SRC, "controller_HDMI-A-2", wait=True)
    assert r["screen"]["name"] == "Emulador EDID A2" and r["machine_address"] == "127.0.0.1"
    sent.clear(); pv.journal.loaded = False
    _arm(pv, sent, _loaded(layer="ndi-preview-HDMI-A-1"))
    r = await pv.show(SRC, "1", wait=True)       # screen #1 = controller HDMI-A-1
    assert r["screen"]["alias"] == "controller_HDMI-A-1"


async def test_ambiguous_and_unknown_screens(tmp_path):
    pv, sent = _preview(tmp_path)
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Emulador EDID A2")
    assert e.value.reason == "ambiguous_output" and len(e.value.extra["candidates"]) == 2
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Monitor central")
    assert (e.value.status, e.value.reason) == (404, "unknown_output") and sent == []


async def test_show_without_wait_returns_pending_and_status_follows(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, "")])
    _arm(pv, sent, _loaded(fmt=False, layer="ndi-preview-HDMI-A-1"))
    r = await pv.show(SRC, "Monitor izquierda", mode="native")
    assert r["confirm"] == "pending" and r["machine_address"] == "127.0.0.1"
    assert sent[2][2] == ["HDMI-A-1", "native"]
    await asyncio.sleep(0.2)
    st = await pv.status()
    assert st["previews"][0]["confirm"] == "no_frames_yet"
    assert st["vc_versions"] == {"127.0.0.1": "0.1.2-8"}
    pv.journal.script += ["NDI: Source format updated 1280x720 @ 50 fps (was invented)"]
    assert (await pv.status())["previews"][0]["confirm"] == "frames"
    pv.journal.script += ["Reset: removing all layers, cancelling loads, resetting master"]
    assert (await pv.status())["previews"][0]["wiped"] is True


async def test_two_screens_on_one_machine_keep_both_previews(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, "")])
    _arm(pv, sent, _loaded(layer="ndi-preview-HDMI-A-1") + _loaded(layer="ndi-preview-HDMI-A-2"))
    await pv.show(SRC, "Monitor izquierda", wait=True)
    await pv.show(SRC, "controller_HDMI-A-2", wait=True)
    st = await pv.status()
    assert sorted(p["screen"]["alias"] for p in st["previews"]) == ["controller_HDMI-A-1",
                                                                   "controller_HDMI-A-2"]
    # The second show unloaded only its own layer.
    unloads = [a[0] for _, p, a in sent if p.endswith("/layer/unload")]
    assert unloads == ["ndi-preview-HDMI-A-1", "ndi-preview-HDMI-A-2"]
    sent.clear()
    await pv.stop("Monitor izquierda")
    assert sent == [("127.0.0.1", "/videocomposer/layer/unload", ["ndi-preview-HDMI-A-1"])]


async def test_show_refused_before_anything_is_sent(tmp_path):
    for engine, auto, reason in ((_Engine(load="show1"), False, "project_loaded"),
                                 (_Engine(load="show1", running="yes"), False, "project_running"),
                                 (_Engine(load=UNKNOWN), False, "engine_unknown"),
                                 (_Engine(), True, "auto_load_active")):
        pv, sent = _preview(tmp_path, engine=engine, auto=auto)
        with pytest.raises(PreviewError) as e:
            await pv.show(SRC, "Monitor izquierda")
        assert e.value.reason == reason and sent == []


async def test_show_busy_while_a_load_is_in_flight(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, ""), Source("OTHER (cam)", "")])
    _arm(pv, sent, [])  # never terminal -> the machine's lock is held until the timeout
    await pv.show(SRC, "Monitor izquierda")
    with pytest.raises(PreviewError) as e:
        await pv.show("OTHER (cam)", "controller_HDMI-A-2")   # same machine
    assert (e.value.status, e.value.reason) == (409, "busy")


async def test_show_unknown_output_unloads(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, "")])
    _arm(pv, sent, ["fit_output: unknown output 'HDMI-A-2' (have: HDMI-A-1)"]
         + _loaded(layer="ndi-preview-HDMI-A-2"))
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "controller_HDMI-A-2", wait=True)
    assert (e.value.status, e.value.reason) == (502, "unknown_output")
    assert sent[-1][1:] == ("/videocomposer/layer/unload", ["ndi-preview-HDMI-A-2"])


async def test_show_raced_by_load(tmp_path):
    eng = _Engine()
    pv, sent = _preview(tmp_path, engine=eng)
    _seed(pv, "127.0.0.1", [Source(SRC, "")])
    _arm(pv, sent, [])
    orig = pv._osc_send

    def send(a, port, path, args):
        orig(a, port, path, args)
        if path.endswith("/visible"):
            eng.load = "show1"  # the operator loads a project right then
    pv._osc_send = send
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Monitor izquierda", wait=True)
    assert e.value.reason == "raced_by_load"
    assert ("127.0.0.1", "/videocomposer/layer/unload", ["ndi-preview-HDMI-A-1"]) in sent[5:]


async def test_show_journal_unreadable(tmp_path):
    pv, sent = _preview(tmp_path)
    pv.journal.unreadable = True
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Monitor izquierda")
    assert (e.value.status, e.value.reason) == (503, "journal_unreadable") and sent == []


async def test_stop_everything_covers_every_screen(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01, "node02.local": "169.254.20.1"})
    stopped = await pv.stop()
    assert [s["alias"] for s in stopped] == ["controller_HDMI-A-1", "controller_HDMI-A-2",
                                             "node01_HDMI-A-1", "node01_HDMI-A-2",
                                             "node02_HDMI-A-1"]
    assert (NODE01, "/videocomposer/layer/unload", ["ndi-preview-HDMI-A-2"]) in sent


# --------------------------------------------------------------------------
# Journal source selection
# --------------------------------------------------------------------------

async def test_journal_remote_file_uses_cursor(tmp_path):
    (tmp_path / "remote").mkdir()
    (tmp_path / "remote" / "remote-169.254.13.233.journal").write_text("")
    (tmp_path / "remote" / "remote-169.254.13.23.journal").write_text("")  # not ours
    calls = []

    async def runner(args):
        calls.append(args)
        return [{"__CURSOR": "s=abc", "MESSAGE": "x"}]

    j = Journal(journal_dir=str(tmp_path), runner=runner)
    t = Target("node01", "169.254.13.233", "avahi", journal_ip="169.254.13.233")
    assert j.remote_files("169.254.13.233") == [str(tmp_path / "remote" / "remote-169.254.13.233.journal")]
    mark = await j.mark(t)
    assert mark["cursor"] == "s=abc"
    await j.read(t, mark)
    assert "--after-cursor" in calls[-1] and "--since" not in calls[-1]
    assert f"--file={tmp_path}/remote/remote-169.254.13.233.journal" in calls[-1]
    assert "SYSLOG_IDENTIFIER=Cuems:videocomposer" in calls[-1]


async def test_journal_without_file_merges_by_hostname_with_since(tmp_path):
    calls = []

    async def runner(args):
        calls.append(args)
        return [{"MESSAGE": [72, 105]}]  # byte-array MESSAGE

    j = Journal(journal_dir=str(tmp_path), runner=runner)
    t = Target("node01", "169.254.13.233", "avahi", journal_ip="169.254.13.233",
               hostnames=["node01"])
    mark = await j.mark(t)
    assert "cursor" not in mark
    assert await j.read(t, mark) == ["Hi"]
    assert calls[-1][:2] == ["--merge", "_HOSTNAME=node01"] and "--since" in calls[-1]


def test_journal_unreadable_names_the_file(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    (tmp_path / "remote").mkdir()
    f = tmp_path / "remote" / "remote-169.254.13.233.journal"
    f.write_text("")
    f.chmod(0)
    j = Journal(journal_dir=str(tmp_path))
    with pytest.raises(PreviewError) as e:
        j.check_readable(Target("node01", "x", "y", journal_ip="169.254.13.233"))
    assert e.value.reason == "journal_unreadable" and e.value.extra["path"] == str(f)


# --------------------------------------------------------------------------
# rev 9: relay through the controller
# --------------------------------------------------------------------------

import socket as _socket

from cuemspowerbridge.ndi_relay import Relay, RelayError


async def _stream_server(payload: bytes = b"x" * 65536):
    """A fake NDI sender: sends `payload` repeatedly to whoever connects."""
    async def handle(r, w):
        try:
            for _ in range(20):
                w.write(payload)
                await w.drain()
                await asyncio.sleep(0.01)
        except (ConnectionError, OSError):
            pass
        finally:
            w.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    return srv, srv.sockets[0].getsockname()[1]


def _closed_port() -> int:
    s = _socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    return port


async def test_relay_copies_bytes_for_the_node_only():
    srv, port = await _stream_server()
    relay = Relay("127.0.0.1", f"127.0.0.1:{port}", "127.0.0.1")
    await relay.start()
    r, w = await asyncio.open_connection("127.0.0.1", relay.port)
    got = 0
    while got < 65536 * 5:
        chunk = await r.read(1 << 20)
        if not chunk:
            break
        got += len(chunk)
    w.close()
    assert got >= 65536 * 5 and relay.bytes_to_node >= got and relay.connections == 1
    await relay.close("test")
    assert relay.state == "closed"
    srv.close()


async def test_relay_refuses_a_peer_that_is_not_the_node():
    srv, port = await _stream_server()
    relay = Relay("169.254.0.11", f"127.0.0.1:{port}", "127.0.0.1")  # node is elsewhere
    await relay.start()
    r, w = await asyncio.open_connection("127.0.0.1", relay.port)
    assert await r.read(10) == b""          # closed at accept
    assert relay.refused == 1 and relay.connections == 0
    await relay.close("test"); srv.close()


async def test_relay_checks_the_upstream_before_listening():
    relay = Relay("127.0.0.1", f"127.0.0.1:{_closed_port()}", "127.0.0.1")
    with pytest.raises(RelayError) as e:
        await relay.start()
    assert e.value.reason == "relay_upstream_unreachable" and relay.port is None


async def test_relay_connection_cap():
    srv, port = await _stream_server(b"y" * 1024)
    relay = Relay("127.0.0.1", f"127.0.0.1:{port}", "127.0.0.1", max_connections=1)
    await relay.start()
    r1, w1 = await asyncio.open_connection("127.0.0.1", relay.port)
    await r1.read(10)
    r2, w2 = await asyncio.open_connection("127.0.0.1", relay.port)
    assert await r2.read(10) == b"" and relay.refused == 1
    w1.close(); await relay.close("test"); srv.close()


async def test_relay_watchdog_closes_on_project_and_on_idle(monkeypatch):
    from cuemspowerbridge import ndi_relay
    monkeypatch.setattr(ndi_relay, "WATCHDOG_TICK_S", 0.02)
    srv, port = await _stream_server()
    loaded = {"why": None}
    relay = Relay("127.0.0.1", f"127.0.0.1:{port}", "127.0.0.1", should_close=lambda: loaded["why"])
    await relay.start()
    loaded["why"] = "project loaded"
    for _ in range(50):
        if relay.state == "closed":
            break
        await asyncio.sleep(0.02)
    assert relay.state == "closed" and relay.close_reason == "project loaded"
    idle = Relay("127.0.0.1", f"127.0.0.1:{port}", "127.0.0.1", idle_close_s=0.05)
    await idle.start()
    for _ in range(50):
        if idle.state == "closed":
            break
        await asyncio.sleep(0.02)
    assert idle.close_reason == "idle"
    srv.close()


async def test_sources_are_one_cluster_wide_list(tmp_path):
    pv, _ = _preview(tmp_path, resolve={"node01.local": NODE01, "node02.local": "169.254.20.1"})
    _seed(pv, "127.0.0.1", [Source("AEON-NODE (CUEMS-TEST)", "10.16.10.11:5961"),
                            Source("NODE CAM (x)", "169.254.7.9:5961")])
    _seed(pv, NODE01, [Source("NODE CAM (x)", "169.254.7.9:5961")])
    _seed(pv, "169.254.20.1", [])
    listed = await pv.sources()
    assert [(s.name, sorted(s.seen_by)) for s in listed] == [
        ("AEON-NODE (CUEMS-TEST)", ["controller"]),
        ("NODE CAM (x)", ["controller", "node01"])]


async def test_show_relays_a_controller_only_source(tmp_path):
    srv, port = await _stream_server()
    pv, sent = _preview(tmp_path)
    # Loopback stands in for the cluster: node01 "is" 127.0.0.2's twin, 127.0.0.1.
    node = Target("node01", "127.0.0.1", "test", "ethernet1", "127.0.0.1", ["node01"])

    async def resolve(n):
        return node if n == "node01" else Target("local", "127.0.0.1", "loopback")
    pv.resolve_target = resolve
    pv._route_src = lambda a: asyncio.sleep(0, result="127.0.0.1")

    async def machines():
        return ["local", "node01"]
    pv._machines = machines

    async def fake_list(t, timeout=3):
        return [Source("AEON-NODE (CUEMS-TEST)", f"127.0.0.1:{port}")] if t.key == "local" else []
    pv._list = fake_list
    loaded = []

    def send(a, p, path, args):
        sent.append((a, path, args))
        if path.endswith("/layer/load"):
            loaded.append(args[0])
            pv.journal.loaded = True
            pv.journal.script = [f"NDI: Connected to source: {args[0][6:]}",
                                 "NDI: Source format: 1920x1080 @ 25 fps (BGRA)",
                                 f"Async load complete: {args[0]} (cue ID: {args[1]})"]
            relay = pv._relays["127.0.0.1|HDMI-A-1"]
            asyncio.get_running_loop().create_task(_drain(relay.port))
    pv._osc_send = send
    r = await pv.show("#1", "Monitor derecha", wait=True)
    assert r["route"] == "relay" and r["confirm"] == "frames", r
    assert loaded[0].startswith("ndi://@127.0.0.1:")
    assert r["relay"]["bytes_to_node"] > 0
    paths = [p for _, p, _ in sent]
    assert paths.index("/videocomposer/layer/unload") < paths.index("/videocomposer/layer/load")
    st = await pv.status(authorized=False)
    assert "upstream" not in st["previews"][0]["relay"]
    await pv.stop("node01_HDMI-A-1")
    assert not pv._relays
    srv.close()


async def _drain(port):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    try:
        while await r.read(1 << 20):
            pass
    except (ConnectionError, OSError):
        pass


async def test_show_relay_refusals(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01, "node02.local": "169.254.20.1"})
    _seed(pv, NODE01, [])
    _seed(pv, "169.254.20.1", [])
    _seed(pv, "127.0.0.1", [Source("AEON-NODE (CUEMS-TEST)", "10.16.10.11:5961")])
    pv.journal.transport_line = None                       # VC without the capability line
    with pytest.raises(PreviewError) as e:
        await pv.show("AEON-NODE (CUEMS-TEST)", "Monitor derecha")
    assert (e.value.status, e.value.reason) == (409, "relay_needs_vc") and sent == []
    pv.journal.transport_line = "NDI receive transport: NOT base TCP (NDI_CONFIG_DIR=/x) - relay unavailable"
    with pytest.raises(PreviewError) as e:
        await pv.show("AEON-NODE (CUEMS-TEST)", "Monitor derecha")
    assert e.value.reason == "relay_needs_vc"
    pv.journal.transport_line = "NDI receive transport: base TCP (/usr/share/cuems-videocomposer/ndi)"
    _seed(pv, "127.0.0.1", [Source("AEON-NODE (CUEMS-TEST)", "")])  # legacy controller discovery
    with pytest.raises(PreviewError) as e:
        await pv.show("AEON-NODE (CUEMS-TEST)", "Monitor derecha")
    assert (e.value.status, e.value.reason) == (409, "relay_needs_controller_vc") and sent == []
    _seed(pv, "127.0.0.1", [Source("AEON-NODE (CUEMS-TEST)", f"127.0.0.1:{_closed_port()}")])
    pv._route_src = lambda a: asyncio.sleep(0, result="127.0.0.1")
    with pytest.raises(PreviewError) as e:
        await pv.show("AEON-NODE (CUEMS-TEST)", "Monitor derecha")
    assert (e.value.status, e.value.reason) == (502, "relay_upstream_unreachable")
    assert [p for _, p, _ in sent] == ["/videocomposer/layer/unload"]  # no load was sent
    assert not pv._relays
    with pytest.raises(PreviewError) as e:
        await pv.show("NOBODY (x)", "Monitor derecha")
    assert (e.value.status, e.value.reason) == (404, "source_not_found")


async def test_version_and_transport_lines_must_belong_to_the_running_vc(tmp_path):
    """After a downgrade the newest version/transport line is a previous
    binary's: it must not count (seen on test node02, 2026-10-09)."""
    lines = {
        "^cuems-videocomposer .* starting$": (1_000_000_000, "cuems-videocomposer 0.1.2-8~test1 starting"),
        "^NDI receive transport: ": (1_000_000_100, "NDI receive transport: base TCP (/x)"),
        "^AsyncVideoLoader: Worker thread running$": (5_000_000_000, "AsyncVideoLoader: Worker thread running"),
    }

    async def runner(args):
        pat = args[args.index("--grep") + 1]
        ts, msg = lines[pat]
        return [{"__REALTIME_TIMESTAMP": str(ts), "MESSAGE": msg}]

    j = Journal(journal_dir=str(tmp_path), runner=runner)
    t = Target("node02", "169.254.0.12", "x")
    assert await j.version(t) is None and await j.transport(t) is None   # stale
    lines["^AsyncVideoLoader: Worker thread running$"] = (1_000_500_000, "AsyncVideoLoader: Worker thread running")
    assert await j.version(t) == "0.1.2-8~test1"
    assert (await j.transport(t)).startswith("NDI receive transport: base TCP")



# --------------------------------------------------------------------------
# rev 10: screen catalogue
# --------------------------------------------------------------------------

from cuemspowerbridge import ndi_screens


def test_catalogue_names_and_aliases(tmp_path):
    (tmp_path / "nm.xml").write_text(NETMAP)
    (tmp_path / "dm.xml").write_text(MAPPINGS)
    screens = ndi_screens.catalogue(str(tmp_path / "dm.xml"), str(tmp_path / "nm.xml"))
    assert [(s.n, s.alias, s.name, s.target) for s in screens] == [
        (1, "controller_HDMI-A-1", "Monitor izquierda", "local"),
        (2, "controller_HDMI-A-2", "Emulador EDID A2", "local"),
        (3, "node01_HDMI-A-1", "Monitor derecha", "node01"),
        (4, "node01_HDMI-A-2", "Emulador EDID A2", "node01"),
        (5, "node02_HDMI-A-1", "Emulador EDID A1", "node02")]
    assert screens[0].layer_id == "ndi-preview-HDMI-A-1"


def test_catalogue_live_marks_absent_and_adds_unmapped(tmp_path):
    (tmp_path / "nm.xml").write_text(NETMAP)
    (tmp_path / "dm.xml").write_text(MAPPINGS)
    screens = ndi_screens.catalogue(str(tmp_path / "dm.xml"), str(tmp_path / "nm.xml"),
                                    live={"local": ["HDMI-A-1", "HDMI-A-3"]})
    ctrl = [(s.alias, s.name, s.present) for s in screens if s.target == "local"]
    assert ctrl == [("controller_HDMI-A-1", "Monitor izquierda", True),
                    ("controller_HDMI-A-2", "Emulador EDID A2", False),
                    ("controller_HDMI-A-3", "", True)]


def test_resolve_screen_order(tmp_path):
    (tmp_path / "nm.xml").write_text(NETMAP)
    (tmp_path / "dm.xml").write_text(MAPPINGS)
    sc = ndi_screens.catalogue(str(tmp_path / "dm.xml"), str(tmp_path / "nm.xml"))
    r = ndi_screens.resolve
    assert r("3", sc).alias == "node01_HDMI-A-1"
    assert r("#3", sc).alias == "node01_HDMI-A-1"
    assert r("NODE01_hdmi-a-1", sc).name == "Monitor derecha"        # alias, any case
    assert r("monitor derecha", sc).alias == "node01_HDMI-A-1"       # unique UI name
    assert r("node01 Emulador EDID A2", sc).alias == "node01_HDMI-A-2"
    with pytest.raises(ndi_screens.ScreenError) as e:
        r("Emulador EDID A2", sc)
    assert e.value.reason == "ambiguous_output"
    for bad in ("9", "Monitor central", ""):
        with pytest.raises(ndi_screens.ScreenError):
            r(bad, sc)
