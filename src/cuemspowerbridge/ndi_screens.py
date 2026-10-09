# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The cluster's screens, as an operator names them (plan 869ekxuez rev 10).

An operator picks a source and a screen; which machine drives that screen,
and whether the source reaches it directly or through the controller's relay,
is the tool's business. A screen is named by its UI name (`<name>` in the
controller's default_mappings.xml, e.g. "Monitor derecha"), by
`<role_id>_<connector>` (e.g. "node01_HDMI-A-1") or by its number in the list.

Per-project mappings.xml files are ignored on purpose: the engine places video
cues from each node's default_mappings.xml too.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from . import network_map

log = logging.getLogger(__name__)

DEFAULT_MAPPINGS = "/etc/cuems/default_mappings.xml"


class ScreenError(Exception):
    def __init__(self, status: int, reason: str, **extra):
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.extra = extra


@dataclass
class Screen:
    n: int
    name: str            # UI name ("" when the mappings do not name it)
    alias: str           # <role_id>_<connector>
    machine: str         # role_id ("controller" for the master)
    target: str          # what resolve_target() takes: "local" or the node's role_id/label
    connector: str       # DRM connector, e.g. HDMI-A-1
    present: bool | None = None   # the VC drives it (None = not checked)

    @property
    def layer_id(self) -> str:
        """One preview layer per screen (D24)."""
        return f"ndi-preview-{self.connector}"

    def public(self) -> dict:
        return {"n": self.n, "name": self.name, "alias": self.alias, "machine": self.machine,
                "connector": self.connector, "present": self.present}


def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element | None, name: str) -> str | None:
    if el is None:
        return None
    c = _child(el, name)
    return c.text.strip() if c is not None and c.text and c.text.strip() else None


def video_outputs(mappings_path: str) -> dict[str, list[tuple[str, str]]]:
    """{node uuid: [(UI name, connector), ...]} from default_mappings.xml."""
    try:
        root = ET.parse(mappings_path).getroot()
    except (OSError, ET.ParseError) as e:
        log.warning("ndi-screens: cannot read %s (%s)", mappings_path, e)
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for node in root.iter():
        if _local(node.tag) != "node":
            continue
        uuid = _text(node, "uuid")
        video = _child(node, "video")
        if not uuid or video is None:
            continue
        found = []
        for o in video.iter():
            if _local(o.tag) != "output":
                continue
            name = _text(o, "name") or ""
            maps = _child(o, "mappings")
            connector = _text(maps, "mapped_to") if maps is not None else None
            if connector:
                found.append((name, connector))
        if found:
            out[uuid] = found
    return out


def catalogue(mappings_path: str, network_map_path: str,
              live: dict[str, list[str]] | None = None) -> list[Screen]:
    """Every screen of the cluster: the controller first, then nodes by role,
    each machine's connectors in order. `live` = {target: connectors the VC
    drives} when known: marks absent ones and adds connectors the mappings
    omit (no UI name)."""
    nodes = network_map.parse(network_map_path)
    outs = video_outputs(mappings_path)
    machines = []
    for n in nodes:
        is_master = n.node_type == "NodeType.master"
        role = n.role_id or n.alias or n.hostname or n.uuid
        machine = "controller" if is_master and not n.role_id else role
        target = "local" if is_master else role
        machines.append((0 if is_master else 1, machine, target, outs.get(n.uuid, [])))
    if not any(m[0] == 0 for m in machines):
        machines.append((0, "controller", "local", []))   # single-box map without a master entry
    machines.sort(key=lambda m: (m[0], m[1]))
    screens: list[Screen] = []
    for _, machine, target, mapped in machines:
        rows = sorted(mapped, key=lambda r: r[1])
        drives = (live or {}).get(target)
        if drives is not None:
            known = {c for _, c in rows}
            rows += [("", c) for c in sorted(drives) if c not in known]
        for name, connector in rows:
            present = None if drives is None else connector in drives
            screens.append(Screen(0, name, f"{machine}_{connector}", machine, target, connector,
                                  present))
    for i, sc in enumerate(screens, 1):
        sc.n = i
    return screens


def resolve(arg: str, screens: list[Screen]) -> Screen:
    """Number -> alias -> unique UI name -> "<machine> <UI name>" (D23)."""
    a = (arg or "").strip()
    if not a:
        raise ScreenError(400, "missing_output")
    key = a[1:] if a.startswith("#") else a
    if key.isdigit():
        n = int(key)
        for sc in screens:
            if sc.n == n:
                return sc
        raise ScreenError(404, "unknown_output", have=[s.public() for s in screens])
    for sc in screens:
        if sc.alias.lower() == a.lower():
            return sc
    named = [sc for sc in screens if sc.name and sc.name.lower() == a.lower()]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise ScreenError(409, "ambiguous_output",
                          candidates=[f"{sc.alias} ({sc.machine} {sc.name})" for sc in named])
    machine, _, rest = a.partition(" ")
    for sc in screens:
        if sc.name and sc.machine.lower() == machine.lower() and sc.name.lower() == rest.lower():
            return sc
    raise ScreenError(404, "unknown_output", have=[s.public() for s in screens])
