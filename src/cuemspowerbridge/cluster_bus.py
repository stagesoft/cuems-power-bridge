# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Node-engine readiness via the controller's NNG hub.

Node-engines are NNG **Bus0 clients** of the controller's hub (pynng over
TCP, local port = ``<nng_hub_port>`` from settings.xml). The bridge runs ON
the controller, so the controller's ESTABLISHED TCP sockets whose **local
port == hub_port** carry each connected node-engine's IP as their **peer**
(remote) address. We read that with ``ss`` (no root) — exactly the signal the
engine's own NNG-liveness probe counts as "node alive". Gating auto-load on
"expected node-engines connected to the hub" therefore prevents node
exclusion at the source.

Loopback self-connects (127.x / ::1) and the controller's own IP (a socket's
local address appearing as a peer) are stripped here, so the returned set is
genuine *remote* node peers only. The auto-load gate then intersects that set
with the expected node IPs — any peer that is not expected is simply ignored,
never required.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger(__name__)


@dataclass
class BusPollResult:
    elapsed_s: float
    connected: set[str] = field(default_factory=set)  # expected IPs matched on the bus
    stuck: list[str] = field(default_factory=list)     # expected IPs still missing at timeout
    timed_out: bool = False


def _clean_ip(raw: str) -> str:
    """Strip IPv6 brackets and any zone id from an address token."""
    ip = raw.strip()
    if ip.startswith("[") and ip.endswith("]"):
        ip = ip[1:-1]
    if "%" in ip:  # link-local zone, e.g. fe80::1%bond0
        ip = ip.split("%", 1)[0]
    return ip


def _is_loopback(ip: str) -> bool:
    return ip.startswith("127.") or ip == "::1"


def _peer_ips(ss_text: str, hub_port: int) -> set[str]:
    """Parse ``ss -Htn state established`` output → set of remote peer IPs
    whose **local** port == ``hub_port``.

    With the ``state established`` filter ss omits the state column, so each
    row is ``Recv-Q Send-Q Local:Port Peer:Port``. We take the last two
    whitespace tokens that contain a ':' as the local/peer address pair
    (robust against an optional trailing process column). Self-connections
    (peer == one of our local hub addresses) and loopback are dropped.
    """
    local_ips: set[str] = set()
    candidates: list[str] = []
    for line in ss_text.splitlines():
        tokens = [t for t in line.split() if ":" in t]
        if len(tokens) < 2:
            continue
        local, peer = tokens[-2], tokens[-1]
        lip, _, lport = local.rpartition(":")
        pip, _, _pport = peer.rpartition(":")
        if lport != str(hub_port):
            continue
        local_ips.add(_clean_ip(lip))
        candidates.append(_clean_ip(pip))
    peers: set[str] = set()
    for ip in candidates:
        if _is_loopback(ip) or ip in local_ips:
            continue
        peers.add(ip)
    return peers


async def _run_ss() -> str:
    """Run ``ss -Htn state established`` and return stdout (empty on error)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ss", "-Htn", "state", "established",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return out.decode("utf-8", "replace")
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        log.warning("cluster_bus: ss failed (%s); treating as no bus peers",
                    type(e).__name__)
        return ""


async def hub_connected_ips(hub_port: int) -> set[str]:
    """Set of remote node-engine peer IPs currently connected to the hub."""
    return _peer_ips(await _run_ss(), hub_port)


async def wait_until_engines_on_bus(
    expected_ips: set[str],
    hub_port: int,
    *,
    interval_s: float = 3.0,
    max_wait_s: float = 420,
    wait_all: bool = True,
    id_by_ip: dict[str, str] | None = None,
) -> BusPollResult:
    """Poll the hub until every expected node-engine is connected, or timeout.

    Gate satisfied when **either**:
      * every ``expected_ip`` is in the connected-peer set, **or**
      * (only when ``wait_all`` — i.e. waiting for ALL adopted slaves, no
        ``auto_load_node_ids`` subset) the count of distinct remote peers is
        ``>= len(expected_ips)``. This count-based fallback keeps a single
        stale ``<ip>`` from forcing a full-timeout wait when the node is in
        fact on the bus; it logs a WARNING naming the unmatched role_ids.

    Empty ``expected_ips`` → immediate, non-timeout result. The loop awaits
    ``asyncio.sleep`` so it is cancellable (``bridge.stop()`` aborts it).
    """
    id_by_ip = id_by_ip or {}
    expected = set(expected_ips)
    loop = asyncio.get_event_loop()
    started = loop.time()
    if not expected:
        return BusPollResult(elapsed_s=0.0)
    expected_count = len(expected)

    while True:
        connected = await hub_connected_ips(hub_port)
        matched = expected & connected

        if matched == expected:
            for ip in sorted(matched):
                log.info("auto-load bus: %s (%s) connected",
                         ip, id_by_ip.get(ip, "?"))
            return BusPollResult(elapsed_s=loop.time() - started,
                                 connected=matched)

        if wait_all and len(connected) >= expected_count:
            unmatched = sorted(expected - matched)
            ids = ", ".join(id_by_ip.get(ip, ip) for ip in unmatched)
            log.warning(
                "auto-load bus: count-fallback satisfied (%d remote peers "
                ">= %d expected); proceeding despite unmatched role_ids "
                "(stale <ip> in network_map.xml?): %s",
                len(connected), expected_count, ids,
            )
            return BusPollResult(elapsed_s=loop.time() - started,
                                 connected=matched)

        elapsed = loop.time() - started
        if elapsed >= max_wait_s:
            stuck = sorted(expected - matched)
            return BusPollResult(elapsed_s=elapsed, connected=matched,
                                 stuck=stuck, timed_out=True)
        await asyncio.sleep(interval_s)
