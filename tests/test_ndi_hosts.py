# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""NDI preview rev 12.2 §3.10: a source on another subnet, by IP (plan
cuems-RELATIONS Plans/2026-10-08-ndi-preview-montajes.md). The VC lines are
its remote/JournalContract.h (ndiDiscoverStarted(s, ips),
ndiDiscoverSourceByIp, ndiDiscoverBadIps), pinned on both sides."""

import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cuemspowerbridge import ndi_preview as ndi
from cuemspowerbridge.ndi_preview import (
    PreviewError, Source, Target, check_host_ip, parse_discovery,
)

from test_ndi_preview import _Engine, _fast, _preview  # noqa: F401

LAPTOP = "LAPTOP (OBS)"


# --------------------------------------------------------------------------
# parsing and validation
# --------------------------------------------------------------------------

def test_parse_discovery_tags_capability_and_bad_ips():
    d = parse_discovery([
        "NDI discover: started (3 s) ips=1",
        "NDI discover: AEON-NODE (CUEMS-TEST) @ 10.16.10.11:5961",
        f"NDI discover: {LAPTOP} @ 10.20.0.15:5961 (ip)",
        "NDI discover: done (2)",
    ])
    assert d.ips_capable is True and d.bad_ips is None
    assert [(s.name, s.address, s.via_ip) for s in d.sources] == [
        ("AEON-NODE (CUEMS-TEST)", "10.16.10.11:5961", False),
        (LAPTOP, "10.20.0.15:5961", True)]
    old = parse_discovery(["NDI discover: started (3 s)", "NDI discover: done (0)"])
    assert old.ips_capable is False and old.sources == []
    bad = parse_discovery(["NDI discover: started (3 s) ips=0",
                           "NDI discover: bad ips '10.0.0.x': not a dotted IPv4 address: '10.0.0.x'"])
    assert bad.sources == [] and bad.bad_ips.startswith("'10.0.0.x'")
    pending = parse_discovery(["NDI discover: started (3 s) ips=0"])
    assert pending.sources is None and pending.ips_capable is True


@pytest.mark.parametrize("value,ok", [
    ("10.20.0.15", "10.20.0.15"), (" 10.20.0.15 ", "10.20.0.15"),
    ("10.20.0.15,10.20.0.16", None), ("laptop.local", None), ("fe80::1", None),
    ("10.20.0.256", None), ("127.0.0.1", None), ("239.1.1.1", None), ("0.0.0.0", None),
    ("255.255.255.255", None), ("", None), (None, None), ("10.20.0.015", None),
])
def test_check_host_ip(value, ok):
    if ok:
        assert check_host_ip(value) == ok
    else:
        with pytest.raises(PreviewError) as e:
            check_host_ip(value)
        assert (e.value.status, e.value.reason) == (400, "bad_ip")


def test_check_host_ip_refuses_the_controllers_own_address():
    with pytest.raises(PreviewError):
        check_host_ip("10.16.10.2", own=["10.16.10.2", "169.254.0.1"])


def test_config_seeds_the_list_and_skips_bad_entries(tmp_path):
    pv, _ = _preview(tmp_path)
    pv.cfg.extras["ndi_hosts"] = "10.20.0.15, laptop.local, 10.20.0.16,127.0.0.1,10.20.0.15"
    pv2 = ndi.NdiPreview(pv.bridge, journal=pv.journal)
    assert pv2.hosts() == ["10.20.0.15", "10.20.0.16"]


# --------------------------------------------------------------------------
# discovery asks the extra IPs on the controller only
# --------------------------------------------------------------------------

def _answer_discover(pv, sent, lines_for):
    """The fake journal answers an ndi/discover with lines_for(address, args)."""
    def send(a, port, path, args):
        sent.append((a, path, args))
        if path.endswith("/ndi/discover"):
            pv.journal.loaded = True
            pv.journal.script = lines_for(a, args)
    pv._osc_send = send


async def test_extra_ips_go_to_the_controller_discovery_only(tmp_path):
    pv, sent = _preview(tmp_path)
    pv._hosts_page = ["10.20.0.15"]
    _answer_discover(pv, sent, lambda a, args: [
        "NDI discover: started (3 s) ips=%d" % (1 if len(args) > 1 else 0),
        "NDI discover: done (0)"])
    await pv._discover(Target("local", "127.0.0.1", "loopback"), 1)
    await pv._discover(Target("node01", "169.254.13.233", "test", journal_ip="169.254.13.233"), 1)
    discovers = [(a, args) for a, p, args in sent if p.endswith("/ndi/discover")]
    assert discovers == [("127.0.0.1", [1, "10.20.0.15"]), ("169.254.13.233", [1])]
    assert pv._ips_capable is True


# --------------------------------------------------------------------------
# /ndi/hosts
# --------------------------------------------------------------------------

async def _client(pv):
    app = web.Application()
    pv.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _setup_hosts(pv, sent, capable=True, laptop_at="10.20.0.15:5961"):
    pv._machines = lambda: asyncio.sleep(0, result=["local"])

    async def own():
        return ["10.16.10.2"]
    ndi.own_ipv4 = own   # restored by monkeypatch in the tests that use it

    def lines(a, args):
        out = ["NDI discover: started (3 s)" + (" ips=%d" % (len(args) - 1) if capable else "")]
        if capable and len(args) > 1:
            out.append(f"NDI discover: {LAPTOP} @ {laptop_at} (ip)")
        out.append("NDI discover: done (%d)" % (1 if capable and len(args) > 1 else 0))
        return out
    _answer_discover(pv, sent, lines)


async def test_add_a_host_finds_its_source(tmp_path, monkeypatch):
    monkeypatch.setattr(ndi, "own_ipv4", ndi.own_ipv4)
    pv, sent = _preview(tmp_path)
    _setup_hosts(pv, sent)
    client = await _client(pv)
    try:
        r = await client.post("/ndi/hosts", json={"ip": "10.20.0.15"})
        body = await r.json()
        assert r.status == 200, body
        assert body["found"] == [LAPTOP] and body["hosts"] == [{"ip": "10.20.0.15", "from": "page"}]
        src = (await (await client.get("/ndi/sources")).json())["sources"]
        assert [(s["name"], s["via_ip"]) for s in src] == [(LAPTOP, True)]
        assert src[0]["ip"] == "10.20.0.15"          # the page puts it under its IP chip
        await asyncio.sleep(0.25)
        r = await client.post("/ndi/hosts", json={"ip": "10.20.0.15", "remove": True})
        assert r.status == 200 and (await r.json())["hosts"] == []
        assert pv.hosts() == []
    finally:
        await client.close()


async def test_an_old_controller_vc_refuses_and_keeps_no_host(tmp_path, monkeypatch):
    monkeypatch.setattr(ndi, "own_ipv4", ndi.own_ipv4)
    pv, sent = _preview(tmp_path)
    _setup_hosts(pv, sent, capable=False)
    client = await _client(pv)
    try:
        r = await client.post("/ndi/hosts", json={"ip": "10.20.0.15"})
        assert r.status == 409 and (await r.json())["reason"] == "hosts_need_controller_vc"
        assert pv.hosts() == []
    finally:
        await client.close()


async def test_host_refusals(tmp_path, monkeypatch):
    monkeypatch.setattr(ndi, "own_ipv4", ndi.own_ipv4)
    pv, sent = _preview(tmp_path)
    _setup_hosts(pv, sent)
    pv._hosts_cfg = ["10.20.0.9"]
    client = await _client(pv)
    try:
        for body, status, reason in (
                ({"ip": "10.20.0.15,10.20.0.16"}, 400, "bad_ip"),
                ({"ip": "10.16.10.2"}, 400, "bad_ip"),              # the controller itself
                ({"ip": "10.20.0.9", "remove": True}, 409, "host_from_config")):
            r = await client.post("/ndi/hosts", json=body)
            assert (r.status, (await r.json())["reason"]) == (status, reason), body
            await asyncio.sleep(0.25)
        pv._hosts_page = [f"10.30.0.{i}" for i in range(1, 8)]     # 1 config + 7 page = 8
        r = await client.post("/ndi/hosts", json={"ip": "10.30.0.99"})
        assert (r.status, (await r.json())["reason"]) == (409, "too_many_hosts")
        await asyncio.sleep(0.25)
        pv.bridge.engine = _Engine(load="show")
        r = await client.post("/ndi/hosts", json={"ip": "10.30.0.98"})
        assert (r.status, (await r.json())["reason"]) == (409, "project_loaded")
        await asyncio.sleep(0.25)
        r = await client.post("/ndi/hosts", data="ip=10.1.1.1",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status == 415                                       # D27 holds here too
    finally:
        await client.close()


# --------------------------------------------------------------------------
# route: by address on the controller, relay on the nodes
# --------------------------------------------------------------------------

async def test_a_by_ip_source_loads_by_address_on_the_controller(tmp_path, monkeypatch):
    pv, sent = _preview(tmp_path)
    pv._sources_cache["127.0.0.1"] = (asyncio.get_event_loop().time() + 3600,
                                      [Source(LAPTOP, "10.20.0.15:5961", via_ip=True)])
    monkeypatch.setattr(ndi, "tcp_reachable", lambda addr, timeout=2.0: asyncio.sleep(0, result=True))
    r = await pv.show(LAPTOP, "Monitor izquierda")
    loads = [args for _, p, args in sent if p.endswith("/layer/load")]
    assert loads == [["ndi://@10.20.0.15:5961", "ndi-preview-HDMI-A-1"]]
    assert r["by_ip"] is True and r["route"] == "direct"


async def test_an_unreachable_by_ip_source_is_refused_before_loading(tmp_path, monkeypatch):
    pv, sent = _preview(tmp_path)
    pv._sources_cache["127.0.0.1"] = (asyncio.get_event_loop().time() + 3600,
                                      [Source(LAPTOP, "10.20.0.15:5961", via_ip=True)])
    monkeypatch.setattr(ndi, "tcp_reachable", lambda addr, timeout=2.0: asyncio.sleep(0, result=False))
    with pytest.raises(PreviewError) as e:
        await pv.show(LAPTOP, "Monitor izquierda")
    assert (e.value.status, e.value.reason) == (502, "source_unreachable")
    assert not [p for _, p, _ in sent if p.endswith("/layer/load")]


async def test_a_by_ip_source_reaches_a_node_through_the_relay(tmp_path):
    pv, _ = _preview(tmp_path)
    node = Target("node01", "169.254.13.233", "test", "ethernet1", "169.254.13.233", ["node01"])

    async def sources():
        s = ndi.SeenSource(LAPTOP, {"controller": "10.20.0.15:5961"}, via_ip=True)
        return [s]
    pv.sources = sources
    screen = next(s for s in await pv.screens() if s.alias == "node01_HDMI-A-1")
    assert await pv._route_for(node, screen, LAPTOP) == (LAPTOP, "10.20.0.15:5961", "relay")


async def test_tcp_reachable_is_bounded():
    t0 = asyncio.get_event_loop().time()
    assert await ndi.tcp_reachable("10.255.255.1:5961", timeout=0.3) is False
    assert await ndi.tcp_reachable("nonsense") is False
    assert asyncio.get_event_loop().time() - t0 < 1.5
