# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Resolve every adopted node in /etc/cuems/network_map.xml to an avahi
hostname (`<role_id>.local` → `<alias>.local` → `<hostname>.local`).

We deliberately ignore the `<ip>` field: it's a stale link-local in many
adopted nodes (see feedback_avahi_hostnames). Nodes with NONE of
role_id/alias/hostname produce no avahi name; caller decides policy.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

log = logging.getLogger(__name__)

# Namespace in the schema. Elements may or may not be namespaced
# depending on how the file was written; we accept both.
NS = "{https://stagelab.coop/cuems/}"


@dataclass(frozen=True)
class Node:
    uuid: str
    avahi: str | None  # None means unresolvable (no role_id/alias/hostname)
    role_id: str | None
    alias: str | None
    hostname: str | None
    node_type: str | None  # "NodeType.master" | "NodeType.slave" | None
    ip: str | None = None  # operator-maintained cluster IP (see slave_ips)


def _text(parent: ET.Element, *names: str) -> str | None:
    """Return text of first matching child by local-name (namespace-agnostic)."""
    for name in names:
        for tag in (name, f"{NS}{name}"):
            el = parent.find(tag)
            if el is not None and el.text and el.text.strip():
                return el.text.strip()
    return None


def _resolve_avahi(role_id, alias, hostname) -> str | None:
    """role_id → alias → hostname → None. Never <ip>."""
    for candidate in (role_id, alias, hostname):
        if candidate and candidate.strip():
            return f"{candidate.strip()}.local"
    return None


def parse(path: str) -> list[Node]:
    """Read network_map.xml and return every node entry.

    Includes both NodeType.master and NodeType.slave. Caller filters
    by node_type if it wants only slaves (e.g. the bridge's shutdown
    flow targets only nodes, not the controller — controller shuts
    itself down via systemctl).
    """
    nodes: list[Node] = []
    try:
        tree = ET.parse(path)
    except FileNotFoundError:
        log.warning("network_map.xml not found at %s — empty node list", path)
        return nodes
    except ET.ParseError as e:
        log.error("network_map.xml parse error at %s: %s", path, e)
        return nodes

    root = tree.getroot()
    # Each <node> entry is wrapped under network_map > nodes > node (paths vary)
    # We scan the whole tree for elements whose local-name ends in "node".
    for el in root.iter():
        tag = el.tag.split("}", 1)[-1]
        if tag != "node":
            continue
        uuid = _text(el, "uuid")
        if not uuid:
            continue
        role_id = _text(el, "role_id")
        alias = _text(el, "alias")
        hostname = _text(el, "hostname")
        avahi = _resolve_avahi(role_id, alias, hostname)
        nodes.append(
            Node(
                uuid=uuid,
                avahi=avahi,
                role_id=role_id,
                alias=alias,
                hostname=hostname,
                node_type=_text(el, "node_type"),
                ip=_text(el, "ip"),
            )
        )
    return nodes


def slave_avahi_names(path: str) -> tuple[list[str], list[Node]]:
    """Return (avahi names of slaves, unresolvable slave entries).

    The bridge uses avahi names to SSH; the unresolvable list goes to ERROR
    logs per `unresolvable_nodes_policy=skip`.
    """
    resolved: list[str] = []
    unresolvable: list[Node] = []
    for n in parse(path):
        if n.node_type != "NodeType.slave":
            continue
        if n.avahi:
            resolved.append(n.avahi)
        else:
            unresolvable.append(n)
    return resolved, unresolvable


def slave_ips(path: str) -> list[tuple[str, str]]:
    """Return (ip, label) for each adopted NodeType.slave that carries an <ip>.

    `label` is `role_id` (→ alias → hostname → uuid) for logging/filtering.

    Unlike the rest of this module — which DELIBERATELY ignores `<ip>` as a
    stale link-local and resolves via avahi instead (see module docstring) —
    `slave_ips()` deliberately TRUSTS `<ip>`, because the NNG-hub readiness
    gate matches node-engine TCP peers by IP, not hostname. A missing or
    stale `<ip>` degrades gracefully and never hangs:

    - missing `<ip>`  → the slave is skipped here (logged WARNING); the gate
      simply can't wait for it by IP.
    - stale `<ip>`    → it never matches a real bus peer; the caller's
      count-based fallback (when waiting for ALL slaves) or its
      degraded-proceed after the node timeout (subset case) covers it.

    nodeconf is disabled cluster-wide, so `<ip>` is operator-maintained —
    the same trust the bridge already places in the rest of network_map.xml.
    """
    out: list[tuple[str, str]] = []
    for n in parse(path):
        if n.node_type != "NodeType.slave":
            continue
        if not n.ip:
            log.warning("network_map: slave uuid=%s has no <ip>; skipping "
                        "bus-readiness match for it", n.uuid)
            continue
        label = n.role_id or n.alias or n.hostname or n.uuid
        out.append((n.ip, label))
    return out
