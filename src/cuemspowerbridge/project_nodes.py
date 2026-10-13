# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Which nodes does a project need, and are they on the bus? (plan 869fghmxn
check 12, decisions D6/D11/D13.)

The engine decides which nodes a load waits for in
``ControllerEngine._resolve_cluster_state``: ``(adopted ∩ alive ∩ project) ∪
{controller}``. This module applies the same rule BEFORE the load, from the
same files, so /loadproject can refuse when a node the project needs is not on
the NNG hub — and only then:

- **project**: ``_collect_project_nodes`` — the first 36 characters of every
  cue output's ``output_name`` (``<uuid>_<n>`` for audio/video, ``<uuid>`` for
  DMX) that have the UUID shape. Read here from ``script.xml`` (only
  ``outputs/*/output_name`` — ``ui_properties`` is ``xs:anyType`` and could
  hold stray names).
- **adopted**: ``<adopted>`` true/1/yes in network_map.xml; absent = not.
- **controller**: ``settings.xml <node><uuid>`` (the engine's
  ``cm.node_conf["uuid"]``). It is always required by the engine and its own
  node-engine never shows on the hub, so it is excluded. Never guessed from
  ``node_type``: nodeconf marks every master adopted.

The three engine helpers are byte-identical in rc5-6, rc6-3 and rc_1.

The bridge reads disk; the engine uses the network map it loaded at start (or
at the last nodelist_modify). After a hand edit of network_map.xml the two can
differ until the engine restarts.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import network_map

DEFAULT_LIBRARY_PATH = "/opt/cuems_library"


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _is_uuid_head(head: str) -> bool:
    """The engine's cheap UUID-shape test (hyphens at 8/13/18/23)."""
    return (len(head) == 36 and head[8] == "-" and head[13] == "-"
            and head[18] == "-" and head[23] == "-")


@dataclass
class Settings:
    library_path: str | None = None
    controller_uuid: str | None = None


def read_settings(path: str) -> Settings:
    """`<library_path>` and `<node><uuid>` from settings.xml, namespace-
    agnostic. Raises OSError / ET.ParseError when the file can't be read."""
    root = ET.parse(path).getroot()
    out = Settings()
    for el in root.iter():
        name = _local(el.tag)
        if name == "library_path" and out.library_path is None:
            if el.text and el.text.strip():
                out.library_path = el.text.strip()
        elif name == "node" and out.controller_uuid is None:
            for child in el:
                if _local(child.tag) == "uuid" and child.text and child.text.strip():
                    out.controller_uuid = child.text.strip().lower()
                    break
    return out


def script_path(library_path: str | None, unix_name: str) -> str:
    """`BaseEngine.read_script`'s path for a project."""
    return os.path.join(library_path or DEFAULT_LIBRARY_PATH, "projects",
                        unix_name, "script.xml")


def project_node_uuids(path: str) -> set[str]:
    """Node UUIDs referenced by the project's cue outputs (the engine's
    `_collect_project_nodes`). Raises OSError / ET.ParseError."""
    nodes: set[str] = set()
    root = ET.parse(path).getroot()
    for outputs in root.iter():
        if _local(outputs.tag) != "outputs":
            continue
        for out in outputs:
            for child in out:
                if _local(child.tag) != "output_name" or not child.text:
                    continue
                head = child.text.strip()[:36]
                if _is_uuid_head(head):
                    nodes.add(head.lower())
    return nodes


@dataclass
class NodeReport:
    """The per-load node check, as returned by /loadproject (check=1, 409,
    202) and kept on the operation for /status."""
    node_check: str = "ok"          # ok | unavailable
    reason: str | None = None       # why node_check is unavailable
    needed: list[dict] = field(default_factory=list)   # {uuid,label,ip,bus}
    peers_seen: list[str] | None = None
    not_in_cluster: list[str] = field(default_factory=list)

    def off_bus(self) -> list[dict]:
        return [n for n in self.needed if n["bus"] == "off_bus"]

    def unchecked(self) -> list[dict]:
        return [n for n in self.needed if n["bus"] == "unknown"]

    def as_dict(self) -> dict:
        return {"node_check": self.node_check, "reason": self.reason,
                "needed": self.needed, "peers_seen": self.peers_seen,
                "not_in_cluster": self.not_in_cluster}


def _label(n: network_map.Node) -> str:
    return n.role_id or n.alias or n.hostname or n.uuid


def classify(project: set[str], nodes: list[network_map.Node],
             controller_uuid: str, peers: set[str] | None) -> NodeReport:
    """needed = project ∩ adopted − {controller}, each marked on_bus / off_bus
    / unknown against the hub peers. `unknown` (never refuses, D11) when the
    peers could not be read, when the node has no <ip>, or when the hub has a
    peer that no <ip> in the map claims (it could be this node on another or a
    stale address). Project references to nodes that are not adopted are
    reported apart: their cues can't play whatever the bridge does."""
    rep = NodeReport(peers_seen=sorted(peers) if peers is not None else None)
    adopted = {n.uuid.lower(): n for n in nodes if n.adopted}
    controller = controller_uuid.lower()
    map_ips = {n.ip for n in nodes if n.ip}
    unclaimed = bool(peers is not None and (peers - map_ips))
    for uuid in sorted(project - {controller}):
        node = adopted.get(uuid)
        if node is None:
            rep.not_in_cluster.append(uuid)
            continue
        if peers is None or not node.ip:
            bus = "unknown"
        elif node.ip in peers:
            bus = "on_bus"
        elif unclaimed:
            bus = "unknown"
        else:
            bus = "off_bus"
        rep.needed.append({"uuid": uuid, "label": _label(node),
                           "ip": node.ip, "bus": bus})
    return rep


def scan(settings_xml: str, network_map_xml: str, unix_name: str
         ) -> tuple[set[str] | None, list[network_map.Node], str | None, str | None]:
    """Blocking file reads for check 12 (run it in an executor).
    Returns (project uuids, map nodes, controller uuid, error); on error the
    first and third are None and `error` says why."""
    try:
        st = read_settings(settings_xml)
    except (OSError, ET.ParseError) as e:
        return None, [], None, f"settings.xml unreadable ({type(e).__name__})"
    if not st.controller_uuid:
        return None, [], None, "settings.xml has no <node><uuid>"
    try:
        project = project_node_uuids(script_path(st.library_path, unix_name))
    except (OSError, ET.ParseError) as e:
        return None, [], None, f"script.xml unreadable ({type(e).__name__})"
    nodes = network_map.parse(network_map_xml)
    if not nodes:
        # parse() logs and returns [] for a missing or broken file; a real map
        # always lists at least the controller.
        return None, [], None, "network_map.xml empty or unreadable"
    return project, nodes, st.controller_uuid, None
