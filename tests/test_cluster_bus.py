# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""cluster_bus: ss parsing + the bus-readiness wait gate.

Async pieces are driven through asyncio.run wrappers so the suite does not
depend on a pytest async plugin being present."""

import asyncio

from cuemspowerbridge import cluster_bus
from cuemspowerbridge.cluster_bus import _peer_ips, wait_until_engines_on_bus


# `ss -Htn state established` rows: Recv-Q Send-Q Local:Port Peer:Port
_SS = """\
0      0      169.254.9.204:9093    169.254.13.233:54321
0      0      169.254.9.204:9093    169.254.20.50:41020
0      0      127.0.0.1:9093        127.0.0.1:40000
0      0      169.254.9.204:9093    169.254.9.204:50000
0      0      169.254.9.204:22      10.0.0.5:33333
"""


def test_peer_ips_intersection_filters_loopback_and_self():
    peers = _peer_ips(_SS, 9093)
    # Two genuine remote node peers; loopback self-connect, controller
    # self-connect (peer == a local hub addr), and the :22 socket are dropped.
    assert peers == {"169.254.13.233", "169.254.20.50"}


def test_peer_ips_only_hub_port():
    # Filtering on a different local port selects only that port's peers.
    assert _peer_ips(_SS, 22) == {"10.0.0.5"}
    assert _peer_ips(_SS, 1234) == set()


def test_peer_ips_ipv6_brackets():
    ss = "0 0 [fe80::1]:9093 [fe80::dead]:5000\n"
    assert _peer_ips(ss, 9093) == {"fe80::dead"}


def test_peer_ips_empty():
    assert _peer_ips("", 9093) == set()


def _patch_hub(monkeypatch, value):
    async def fake(hub_port):
        return set(value)
    monkeypatch.setattr(cluster_bus, "hub_connected_ips", fake)


def test_wait_all_present_ok(monkeypatch):
    _patch_hub(monkeypatch, {"a", "b"})
    res = asyncio.run(wait_until_engines_on_bus(
        {"a", "b"}, 9093, interval_s=0.01, max_wait_s=1))
    assert not res.timed_out
    assert res.connected == {"a", "b"}


def test_wait_one_missing_times_out(monkeypatch):
    _patch_hub(monkeypatch, {"a"})
    res = asyncio.run(wait_until_engines_on_bus(
        {"a", "b"}, 9093, interval_s=0.01, max_wait_s=0.0, wait_all=False))
    assert res.timed_out
    assert res.stuck == ["b"]


def test_count_fallback_satisfies_on_stale_ip(monkeypatch):
    # Expected b has a stale <ip>; a real but unexpected peer "c" is on the
    # bus. With wait_all, two distinct peers >= two expected ⇒ proceed.
    _patch_hub(monkeypatch, {"a", "c"})
    res = asyncio.run(wait_until_engines_on_bus(
        {"a", "b"}, 9093, interval_s=0.01, max_wait_s=5, wait_all=True,
        id_by_ip={"a": "node01", "b": "node02"}))
    assert not res.timed_out


def test_count_fallback_disabled_for_subset(monkeypatch):
    # Subset mode (auto_load_node_ids set) must NOT use the count fallback.
    _patch_hub(monkeypatch, {"a", "c"})
    res = asyncio.run(wait_until_engines_on_bus(
        {"a", "b"}, 9093, interval_s=0.01, max_wait_s=0.0, wait_all=False))
    assert res.timed_out
    assert res.stuck == ["b"]


def test_wait_empty_expected_immediate():
    res = asyncio.run(wait_until_engines_on_bus(set(), 9093))
    assert not res.timed_out
    assert res.elapsed_s == 0.0


def test_wait_is_cancellable(monkeypatch):
    _patch_hub(monkeypatch, set())  # never satisfied

    async def driver():
        task = asyncio.create_task(wait_until_engines_on_bus(
            {"a"}, 9093, interval_s=10, max_wait_s=9999))
        await asyncio.sleep(0.05)
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        return cancelled

    assert asyncio.run(driver()) is True
