# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""NDI preview rev 12.2 (plan cuems-RELATIONS
Plans/2026-10-08-ndi-preview-montajes.md §3.9): sizes in pixels per layer,
machine health off the request path, and a Spanish-able reason for every
unconfirmed preview."""

import asyncio
import os
import time

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cuemspowerbridge import ndi_health
from cuemspowerbridge import ndi_preview as ndi
from cuemspowerbridge import ndi_screens
from cuemspowerbridge.ndi_health import Health, HealthMonitor
from cuemspowerbridge.ndi_preview import (
    PreviewError, Source, Verdict, _Request, classify, fit_size, pixel_scale,
)

from test_ndi_preview import (  # noqa: F401  (helpers + the autouse speed fixture)
    MAPPINGS, NETMAP, NODE01, SRC, _Engine, _arm, _fast, _loaded, _preview, _seed,
)

OTHER = "MAC (Canvas #1)"


# --------------------------------------------------------------------------
# sizes
# --------------------------------------------------------------------------

def test_catalogue_carries_mode_and_region(tmp_path):
    m = tmp_path / "m.xml"
    m.write_text(MAPPINGS)
    n = tmp_path / "n.xml"
    n.write_text(NETMAP)
    info = {"node01": {"HDMI-A-1": {"mode": "1920x1080@60", "region": "1920,0,1920,1080"}}}
    screens = ndi_screens.catalogue(str(m), str(n), None, info)
    sc = next(s for s in screens if s.alias == "node01_HDMI-A-1")
    assert (sc.mode, sc.region, sc.region_size()) == ("1920x1080@60", "1920,0,1920,1080",
                                                      (1920, 1080))
    assert sc.public()["mode"] == "1920x1080@60"
    other = next(s for s in screens if s.alias == "node01_HDMI-A-2")
    assert (other.mode, other.region_size()) == ("", None)


def test_size_comes_from_the_layers_own_fit_line_never_from_invented():
    assert fit_size({"basis": "dims 1280x720"}) == (1280, 720)
    for basis in ("fallback", "invented", "", None):
        assert fit_size({"basis": basis} if basis is not None else None) is None


@pytest.mark.parametrize("line,fps", [
    ("NDI: Source format: 1920x1080 @ 25 fps (BGRA)", 25.0),
    ("NDI: Source format: 1920x1080 @ 29.97 fps (BGRA)", 29.97),
    ("NDI: Source format: 1920x1080 @ 59.9401 fps (BGRA)", 59.9401),
])
def test_fps_from_the_open_time_line(line, fps):
    lines = [line, f"Async load complete: ndi://{SRC} (cue ID: L)",
             "fit_output: L -> HDMI-A-1 fill pos 0,0 scale 1,1 (dims 1920x1080)"]
    v = classify(lines, SRC, layer_id="L")
    assert v.state == "frames" and v.fps == fps


def test_an_updated_line_neither_sets_fps_nor_frames():
    lines = ["NDI: Source format updated 1280x720 @ 50 fps (was invented)",
             f"Async load complete: ndi://{SRC} (cue ID: L)",
             "NDI: No video frame received, using defaults (1920x1080 @25fps)",
             "fit_output: L -> HDMI-A-1 fill pos 0,0 scale 1,1 (invented)"]
    v = classify(lines, SRC, layer_id="L")
    assert v.state == "no_frames_yet" and v.fps is None and fit_size(v.fit) is None


def test_pixel_scale_fill_and_native():
    assert pixel_scale((1280, 720), (1920, 1080), "fill") == 1.5
    assert pixel_scale((1280, 720), (1920, 1080), "native") == 1.0     # never enlarged
    assert pixel_scale((3840, 2160), (1920, 1080), "native") == 0.5    # but shrunk to fit
    assert pixel_scale((1920, 1080), (1920, 1200), "fill") == 1.0      # region, not mode
    assert pixel_scale(None, (1920, 1080), "fill") is None
    assert pixel_scale((1920, 1080), None, "fill") is None


def _req(alias_layer, source, state="frames", fit=None, fps=None):
    sc = ndi_screens.Screen(1, "x", f"node01_{alias_layer}", "node01", "node01", alias_layer,
                            True, "1920x1080@60", "0,0,1920,1080")
    t = ndi.Target("node01", NODE01, "test")
    return _Request(sc, t, source, "fill", {"t": 0}, time.time(),
                    verdict=Verdict(state=state, fit=fit, fps=fps))


def test_two_previews_on_one_machine_keep_their_own_sizes():
    # Interleaved lines of two layers of one VC, as a status re-read sees them.
    a = _req("HDMI-A-1", SRC, fps=25.0)
    b = _req("HDMI-A-2", OTHER, fps=60.0)
    msgs = [
        "fit_output: ndi-preview-HDMI-A-1 -> HDMI-A-1 fill pos 0,0 scale 1,1 (dims 1920x1080)",
        "NDI: Source format updated 1280x720 @ 30 fps (was 1920x1080)",   # no layer id
        "fit_output: ndi-preview-HDMI-A-2 -> HDMI-A-2 fill pos 0,0 scale 1.5,1.5 (dims 1280x720)",
    ]
    ndi.NdiPreview._reread(a, msgs)
    ndi.NdiPreview._reread(b, msgs)
    assert a.public()["source_size"] == {"w": 1920, "h": 1080} and a.public()["scale"] == 1.0
    assert b.public()["source_size"] == {"w": 1280, "h": 720} and b.public()["scale"] == 1.5
    assert (a.verdict.fps, b.verdict.fps) == (25.0, 60.0)    # never overwritten


def test_a_vc_restart_after_the_preview_wipes_it():
    a = _req("HDMI-A-1", SRC)
    ndi.NdiPreview._reread(a, ["cuems-videocomposer 0.1.2-7 starting"])
    assert a.wiped is True


def test_no_frames_yet_flips_only_on_its_own_dims_fit():
    a = _req("HDMI-A-1", SRC, state="no_frames_yet")
    ndi.NdiPreview._reread(a, [
        "fit_output: ndi-preview-HDMI-A-2 -> HDMI-A-2 fill pos 0,0 scale 1,1 (dims 1920x1080)"])
    assert a.verdict.state == "no_frames_yet"
    ndi.NdiPreview._reread(a, [
        "fit_output: ndi-preview-HDMI-A-1 -> HDMI-A-1 fill pos 0,0 scale 1,1 (dims 1920x1080)"])
    assert a.verdict.state == "frames"


async def test_source_list_size_only_for_sources_on_screen(tmp_path):
    pv, sent = _preview(tmp_path)
    shown = _req("HDMI-A-1", SRC, fit={"basis": "dims 1920x1080"}, fps=25.0)
    stopped = _req("HDMI-A-2", OTHER, fit={"basis": "dims 1280x720"})
    stopped.stopped = True
    pv._previews = {"a": shown, "b": stopped}
    assert pv._shown_sizes() == {SRC: {"w": 1920, "h": 1080, "fps": 25.0}}


# --------------------------------------------------------------------------
# health monitor
# --------------------------------------------------------------------------

def _monitor(machines, ages=None, icmp=False, tcp=False, vc=True, blocked=False):
    calls = []

    async def ms():
        return machines

    async def ping(ip):
        calls.append(("icmp", ip))
        return icmp

    async def tcp22(ip):
        calls.append(("tcp", ip))
        return tcp

    async def vcp():
        calls.append(("vc", None))
        return vc
    return HealthMonitor(ms, lambda ip: (ages or {}).get(ip), lambda: blocked,
                         icmp_probe=ping, tcp_probe=tcp22, vc_probe=vcp), calls


async def test_unreachable_only_after_two_cycles():
    hm, _ = _monitor([("node02", "169.254.0.12")])
    await hm.run_cycle()
    assert hm.get("node02").state == "unknown"
    await hm.run_cycle()
    assert hm.get("node02").state == "unreachable"


async def test_one_lost_echo_does_not_flip_an_alive_machine():
    hm, _ = _monitor([("node01", NODE01)], icmp=True)
    await hm.run_cycle()
    assert hm.get("node01").state == "alive"
    hm._icmp = hm._tcp = (lambda ip: asyncio.sleep(0, result=False))
    await hm.run_cycle()
    assert hm.get("node01").state == "alive"           # one failed cycle
    await hm.run_cycle()
    assert hm.get("node01").state == "unreachable"


async def test_tcp22_or_a_fresh_journal_means_alive():
    hm, calls = _monitor([("node01", NODE01)], tcp=True)
    await hm.run_cycle()
    await hm.run_cycle()
    assert hm.get("node01").state == "alive"
    hm2, calls2 = _monitor([("node01", NODE01)], ages={NODE01: 5.0})
    await hm2.run_cycle()
    await hm2.run_cycle()
    assert hm2.get("node01").state == "alive" and calls2 == []   # no probe needed
    assert hm2.get("node01").journal_age_s == 5.0


async def test_controller_is_its_vc_unit():
    hm, calls = _monitor([("local", None)], vc=False)
    await hm.run_cycle()
    await hm.run_cycle()
    assert hm.get("local").state == "vc_down" and ("icmp", None) not in calls


async def test_nothing_is_probed_during_a_show():
    hm, calls = _monitor([("node01", NODE01), ("local", None)], blocked=True)
    await hm.run_cycle()
    assert calls == [] and hm.all() == {}


async def test_a_node_without_ip_stays_unknown():
    hm, calls = _monitor([("node03", None)])
    await hm.run_cycle()
    await hm.run_cycle()
    assert hm.get("node03").state == "unknown" and calls == []


async def test_probes_have_a_ceiling(monkeypatch):
    monkeypatch.setattr(ndi_health, "PROBE_CEILING_S", 0.2)
    t0 = time.monotonic()
    assert await ndi_health.tcp22("10.255.255.1") is False      # black-holed
    assert time.monotonic() - t0 < 1.0


# --------------------------------------------------------------------------
# requests read the state; they never probe
# --------------------------------------------------------------------------

def _set(pv, label, state, age=None):
    pv.health._state[label] = Health(state=state, journal_age_s=age)


async def test_show_refuses_an_unreachable_machine_before_anything(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01})
    _set(pv, "node01", "unreachable")
    t0 = time.monotonic()
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Monitor derecha")
    assert (e.value.status, e.value.reason, e.value.extra["machine"]) == \
        (503, "machine_unreachable", "node01")
    assert sent == [] and pv._relays == {} and time.monotonic() - t0 < 0.5


async def test_show_refuses_a_stale_journal_and_the_controllers_stopped_vc(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01})
    pv.journal_stale_s = 60
    _set(pv, "node01", "alive", age=3600)
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Monitor derecha")
    assert e.value.reason == "journal_stale" and e.value.extra["journal_age_s"] == 3600
    _set(pv, "local", "vc_down")
    with pytest.raises(PreviewError) as e:
        await pv.show(SRC, "Monitor izquierda")
    assert e.value.reason == "vc_down"
    assert sent == []


async def test_stop_is_never_blocked_by_health(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01})
    _set(pv, "node01", "unreachable")
    stopped = await pv.stop("Monitor derecha")
    assert [s["alias"] for s in stopped] == ["node01_HDMI-A-1"] and sent


async def test_sources_and_live_outputs_skip_dead_and_stale_machines(tmp_path):
    pv, sent = _preview(tmp_path, resolve={"node01.local": NODE01})
    pv.journal_stale_s = 60
    _set(pv, "node01", "unreachable")
    _set(pv, "node02", "alive", age=3600)
    _seed(pv, "127.0.0.1", [Source(SRC, "10.0.0.5:5961")])
    t0 = time.monotonic()
    found = await pv.sources()
    assert [s.name for s in found] == [SRC] and time.monotonic() - t0 < 0.5
    screens = await pv.screens(live=True)
    assert sent == [("127.0.0.1", "/videocomposer/output/list", [])]   # only the controller
    assert {s.alias: s.present for s in screens}["node01_HDMI-A-1"] is None


async def test_confirm_timeout_gives_a_token_and_keeps_the_english_apart(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, "")])
    r = await pv.show(SRC, "Monitor izquierda")          # the fake journal never answers
    await asyncio.sleep(0.8)
    p = (await pv.status())["previews"][0]
    assert p["confirm"] == "unconfirmed" and p["reason"] == "no_vc_reply"
    assert p["detail"].startswith("no terminal VC line")
    assert r["confirm"] == "pending"


async def test_confirm_timeout_names_the_dead_machine(tmp_path):
    pv, sent = _preview(tmp_path)
    _seed(pv, "127.0.0.1", [Source(SRC, "")])
    await pv.show(SRC, "Monitor izquierda")
    _set(pv, "local", "vc_down")                          # it died during the load
    await asyncio.sleep(0.8)
    assert (await pv.status())["previews"][0]["reason"] == "vc_down"


# --------------------------------------------------------------------------
# HTTP shape
# --------------------------------------------------------------------------

async def test_outputs_and_status_carry_mode_and_machine_state(tmp_path):
    pv, sent = _preview(tmp_path)
    pv._outputs_info["local"] = {"HDMI-A-1": {"mode": "1920x1080@60",
                                              "region": "-1920,0,1920,1080"}}
    _set(pv, "node02", "unreachable")
    app = web.Application()
    pv.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        pv.bridge.engine = _Engine(load="x")             # blocked: catalogue only, no VC asked
        outs = (await (await client.get("/ndi/outputs")).json())["outputs"]
        left = next(o for o in outs if o["alias"] == "controller_HDMI-A-1")
        assert left["mode"] == "1920x1080@60" and left["region"] == "-1920,0,1920,1080"
        n2 = next(o for o in outs if o["alias"] == "node02_HDMI-A-1")
        assert n2["machine_state"]["state"] == "unreachable"
        st = await (await client.get("/ndi/status")).json()
        assert st["machines"]["node02"]["state"] == "unreachable"
        assert sent == []
    finally:
        await client.close()


async def test_journal_age_is_the_newest_per_ip_file(tmp_path):
    pv, _ = _preview(tmp_path)
    d = tmp_path / "remote"
    d.mkdir()
    old = d / f"remote-{NODE01}@aaa.journal"
    new = d / f"remote-{NODE01}.journal"
    old.write_text("x")
    new.write_text("x")
    now = time.time()
    os.utime(old, (now - 900, now - 900))
    os.utime(new, (now - 30, now - 30))
    pv.journal.remote_files = lambda ip: [str(old), str(new)] if ip == NODE01 else []
    assert 25 <= pv._journal_age(NODE01) <= 40
    assert pv._journal_age("169.254.99.9") is None


def test_stale_threshold_comes_from_config(tmp_path):
    pv, _ = _preview(tmp_path)
    assert pv.journal_stale_s == 600
    pv.cfg.extras["ndi_journal_stale_s"] = "90"
    assert ndi.NdiPreview(pv.bridge, journal=pv.journal).journal_stale_s == 90
    pv.cfg.extras["ndi_journal_stale_s"] = "soon"
    assert ndi.NdiPreview(pv.bridge, journal=pv.journal).journal_stale_s == 600
