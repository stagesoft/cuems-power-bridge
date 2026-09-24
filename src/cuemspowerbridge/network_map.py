# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Cluster topology, read through the library that owns it.

`network_map.xml` and `settings.xml` belong to ``cuemsutils``: it versions
them, ships their schemas, validates them on load, and ships the tool that
converts documents written before the ``node_type`` -> ``node_role`` rename.
This module is a **thin adapter** over that public path (``ConfigManager``),
not a second reader — a private reader here is how a rename in another
repository silently disabled two features of this one (feature 001).

Two selection policies live here, both field-learned, both preserved verbatim
from the reader this module replaces:

* **Power-off targets resolve by NAME and ignore ``<ip>``** —
  ``role_id`` -> ``alias`` -> ``hostname``, suffixed ``.local``. The recorded
  address is a stale link-local on many adopted nodes, so it is never used as
  a substitute; a node resolving to none of the three is REPORTED as
  unresolvable, never silently dropped.
* **Bus-readiness peers TRUST ``<ip>``** — the NNG-hub readiness gate matches
  bus peers by address, not by hostname, so this is the deliberate exception.

Everything this module returns carries the reason it looks the way it does: a
``Selection`` knows what it skipped and why, and a topology that cannot be read
raises ``TopologyError`` instead of yielding an empty answer. Those two
outcomes are mutually exclusive, and that exclusivity is the point — an empty
step must never be indistinguishable from a successful one on a machine that
cuts mains power.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# The library's own logging.
#
# cuemsutils decorates its internals with a call-logger that emits a DEBUG
# record per call (measured 2026-09-23). The bridge reads the topology on the
# shutdown path AND on the auto-load retry loop, so a DEBUG-level bridge would
# flood the controller's journal with the library's internals. Bound it once,
# here, at import: WARNING unless the operator deliberately raises it via the
# CUEMSUTILS_LOG_LEVEL environment variable.
# ---------------------------------------------------------------------------
_CONF_PATH_ENV = "CUEMS_CONF_PATH"

_LIB_LOG_LEVEL = os.environ.get("CUEMSUTILS_LOG_LEVEL", "WARNING").upper()
logging.getLogger("cuemsutils").setLevel(
    getattr(logging, _LIB_LOG_LEVEL, logging.WARNING)
)


class SkipReason(str, Enum):
    """Why a node present in the map is not in a selection."""

    SELF = "self"                  # this controller; never a target of its own fan-out
    UNADOPTED = "unadopted"        # not taken into the cluster (or no <adopted> at all)
    UNRESOLVABLE = "unresolvable"  # no role_id/alias/hostname: cannot be named
    NO_IP = "no_ip"                # readiness only; defensive (schema makes <ip> required)


class TopologyErrorKind(str, Enum):
    """Classification of a failed topology read. Each maps to one refusal."""

    CONFIG_DIR_MISSING = "config_dir_missing"
    SETTINGS_XML_MISSING = "settings_xml_missing"
    SETTINGS_XML_INVALID = "settings_xml_invalid"
    NETWORK_MAP_MISSING = "network_map_missing"
    NETWORK_MAP_INVALID = "network_map_invalid"
    NETWORK_MAP_RETIRED_VOCABULARY = "network_map_retired_vocabulary"
    SELF_ENTRY_MISSING = "self_entry_missing"
    CONFIG_DIR_MISMATCH = "config_dir_mismatch"


class TopologyError(Exception):
    """The topology could not be read. NEVER converted into an empty selection.

    Carries a `kind` for the HTTP layer to turn into a refusal reason, and a
    message that names the offending document or node AND a remedy — an
    operator reading the journal of a machine that then powered off has only
    this line to work from.
    """

    def __init__(self, kind: TopologyErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.kind.value}: {super().__str__()}"


@dataclass(frozen=True)
class NodeView:
    """One node of the map, projected to what selection needs.

    Deliberately has **no** `node_type` attribute and no string role: code
    still looking for the retired vocabulary fails at attribute access instead
    of silently matching nothing.
    """

    uuid: str
    role: Any                 # cuemsutils NodeRole; imported lazily (see _node_role)
    adopted: bool             # absent <adopted> counts as NOT adopted
    role_id: str | None
    alias: str | None
    hostname: str | None
    ip: str | None
    is_self: bool

    @property
    def avahi(self) -> str | None:
        """role_id -> alias -> hostname, suffixed `.local`. NEVER from <ip>."""
        for candidate in (self.role_id, self.alias, self.hostname):
            if candidate and candidate.strip():
                return f"{candidate.strip()}.local"
        return None

    @property
    def label(self) -> str:
        """Operator-facing name: the first thing that identifies this node."""
        for candidate in (self.role_id, self.alias, self.hostname):
            if candidate and candidate.strip():
                return candidate.strip()
        return self.uuid


@dataclass(frozen=True)
class Skip:
    """A node that is not in the selection, and why."""

    uuid: str
    label: str
    reason: SkipReason


@dataclass
class Selection:
    """The answer, carrying the reasons it is the size it is.

    `targets` plus `skipped` always account for every non-self node in the
    document, so a caller never has to infer why a list is short.
    """

    mode: str                                  # adopted | forced_all | controller_only
    targets: list = field(default_factory=list)
    found: int = 0                             # non-self nodes present in the document
    adopted_count: int = 0                     # of those, adopted
    skipped: list[Skip] = field(default_factory=list)
    partial: bool = False                      # some intended target is unresolvable
    #: The nodes this selection was computed from, so a caller can corroborate
    #: self-exclusion by address and name (cluster_shutdown.exclude_self).
    nodes: list = field(default_factory=list)

    @property
    def unresolvable(self) -> list[Skip]:
        return [s for s in self.skipped if s.reason is SkipReason.UNRESOLVABLE]


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

_RETIRED_MARKERS = ("node_type", "retired")


def _config_dir(settings_xml_path: str, network_map_path: str) -> str:
    """The directory the library reads both documents from.

    ConfigManager takes one directory and reads settings.xml and
    network_map.xml from it. If the operator has pointed the two config keys
    at different directories, that is a configuration fault to report, not
    something to paper over.
    """
    settings_dir = os.path.dirname(os.path.abspath(settings_xml_path))
    map_dir = os.path.dirname(os.path.abspath(network_map_path))
    if settings_dir != map_dir:
        raise TopologyError(
            TopologyErrorKind.CONFIG_DIR_MISMATCH,
            f"settings_xml_path ({settings_xml_path}) and network_map_path "
            f"({network_map_path}) are in different directories; the topology "
            f"library reads both from one directory. Point both at the same "
            f"directory (normally /etc/cuems) in power-bridge.conf.",
        )
    return settings_dir


def _classify(exc: Exception, settings_xml_path: str, network_map_path: str) -> TopologyError:
    """Map a cuemsutils failure onto a kind + an actionable message.

    The three source exception types were measured against cuemsutils
    0.1.0rc16 (research R2a): FileNotFoundError, SchemaError, ValueError.
    """
    text = str(exc)
    low = text.lower()

    # The whole configuration directory is absent: both documents are missing,
    # and neither path is named in the library's message.
    if "configuration directory" in low and "not found" in low:
        return TopologyError(
            TopologyErrorKind.CONFIG_DIR_MISSING,
            f"{os.path.dirname(os.path.abspath(network_map_path))} does not exist, "
            f"so neither settings.xml nor network_map.xml can be read. It is created "
            f"by the cuems-common package; restore it, or point network_map_path and "
            f"settings_xml_path at the right directory in power-bridge.conf.",
        )

    if isinstance(exc, FileNotFoundError) or "not found" in low and "configuration file" in low:
        if "settings.xml" in low:
            return TopologyError(
                TopologyErrorKind.SETTINGS_XML_MISSING,
                f"{settings_xml_path} is missing. It is shipped by the cuems-utils "
                f"package (>= 0.1.0rc16); reinstall or restore it. The bridge cannot "
                f"read cluster topology without this host's own identity.",
            )
        if "network_map.xml" in low:
            return TopologyError(
                TopologyErrorKind.NETWORK_MAP_MISSING,
                f"{network_map_path} is missing. It is shipped by the cuems-common "
                f"package and maintained by cuems-nodeconf; restore it before "
                f"shutting the cluster down.",
            )

    # ValueError: "Node with uuid <uuid> not found" — this host has no entry.
    if isinstance(exc, ValueError) and "uuid" in low and "not found" in low:
        return TopologyError(
            TopologyErrorKind.SELF_ENTRY_MISSING,
            f"{network_map_path} has no entry for this host ({text}). Adopt this "
            f"controller into the map (cuems-nodeconf) so it can identify itself "
            f"and exclude itself from the power-off fan-out.",
        )

    # SchemaError — the library names the retired vocabulary explicitly.
    if all(m in low for m in _RETIRED_MARKERS):
        return TopologyError(
            TopologyErrorKind.NETWORK_MAP_RETIRED_VOCABULARY,
            f"{network_map_path} still uses the retired <node_type> vocabulary "
            f"({text}). Run cuems-migrate-network-map to convert it to <node_role>, "
            f"then retry.",
        )

    which = settings_xml_path if "settings" in low else network_map_path
    kind = (TopologyErrorKind.SETTINGS_XML_INVALID if "settings" in low
            else TopologyErrorKind.NETWORK_MAP_INVALID)
    return TopologyError(
        kind,
        f"{which} is not a valid document ({text}). Every node needs uuid, mac, "
        f"name, node_role and ip; fix the document (or restore it from the package) "
        f"and retry.",
    )


def _node_role():
    """The owning library's role enum. Imported here, never redeclared."""
    from cuemsutils.tools.NodeList import NodeRole

    return NodeRole


def load_nodes(settings_xml_path: str, network_map_path: str) -> list[NodeView]:
    """Every node in the map, as NodeViews. Raises TopologyError on any failure.

    NB a map whose node_list is EMPTY does not produce an empty list here: the
    library cannot then resolve this host's own entry and raises, which is
    classified as `self_entry_missing`. "The map says one machine" therefore
    always means the self entry is present and nothing else is.
    """
    config_dir = _config_dir(settings_xml_path, network_map_path)

    # The library reads CUEMS_CONF_PATH from the environment and prefers it
    # OVER the config_dir argument (ConfigBase.load_base_settings, measured
    # 2026-09-23). An inherited value would therefore silently decide which
    # cluster this controller powers off. The bridge's own configuration is
    # authoritative, so pin the variable for the duration of the read and put
    # back whatever was there.
    inherited = os.environ.get(_CONF_PATH_ENV)
    if inherited is not None and os.path.abspath(inherited.rstrip("/")) != config_dir:
        log.warning(
            "%s=%s in the environment disagrees with power-bridge.conf (%s); "
            "using the configured directory", _CONF_PATH_ENV, inherited, config_dir,
        )
    os.environ[_CONF_PATH_ENV] = config_dir
    try:
        from cuemsutils.tools.ConfigManager import ConfigManager

        cm = ConfigManager(config_dir=config_dir, load_all=False)
        cm.load_network_map()
        own_uuid = str(cm.node_uuid)
        node_list = cm.network_map["node_list"] or []
    except TopologyError:
        raise
    except Exception as exc:  # noqa: BLE001 - deliberately broad; classified below
        raise _classify(exc, settings_xml_path, network_map_path) from exc
    finally:
        if inherited is None:
            os.environ.pop(_CONF_PATH_ENV, None)
        else:
            os.environ[_CONF_PATH_ENV] = inherited

    views: list[NodeView] = []
    for wrapper in node_list:
        n = wrapper["node"] if isinstance(wrapper, dict) and "node" in wrapper else wrapper
        uuid = str(n.get("uuid"))
        views.append(
            NodeView(
                uuid=uuid,
                role=n.get("node_role"),
                adopted=bool(n.get("adopted") or False),
                role_id=n.get("role_id"),
                alias=n.get("alias"),
                hostname=n.get("hostname"),
                ip=n.get("ip"),
                is_self=(uuid == own_uuid),
            )
        )
    return views


def _other_nodes(views: Iterable[NodeView]) -> list[NodeView]:
    """Non-controller entries that are not this host."""
    NodeRole = _node_role()
    return [v for v in views if v.role is NodeRole.node and not v.is_self]


# ---------------------------------------------------------------------------
# Selections
# ---------------------------------------------------------------------------


def shutdown_targets(
    settings_xml_path: str,
    network_map_path: str,
    *,
    include_unadopted: bool = False,
    nodes: list[NodeView] | None = None,
) -> Selection:
    """Which nodes this controller powers off, and what it skipped.

    `include_unadopted` is the `force` flag at the boundary: False targets
    adopted nodes only, True targets every other node in the document.

    An empty `targets` never means "nothing to do" on its own — read it with
    `found` and `adopted_count`:

    * found == 0                      -> controller-only cluster (Case 1)
    * adopted_count == 0              -> nothing adopted (Case 3), refuse
    * targets == [] with candidates   -> nothing addressable (Case 3b), refuse
    """
    views = nodes if nodes is not None else load_nodes(settings_xml_path, network_map_path)
    others = _other_nodes(views)
    adopted = [v for v in others if v.adopted]

    sel = Selection(
        mode="forced_all" if include_unadopted else "adopted",
        found=len(others),
        adopted_count=len(adopted),
        nodes=list(others),
    )
    if not others:
        sel.mode = "controller_only"
        return sel

    candidates = others if include_unadopted else adopted
    for v in others:
        if v not in candidates:
            sel.skipped.append(Skip(v.uuid, v.label, SkipReason.UNADOPTED))

    for v in candidates:
        name = v.avahi
        if name is None:
            sel.skipped.append(Skip(v.uuid, v.label, SkipReason.UNRESOLVABLE))
            continue
        sel.targets.append(name)

    sel.partial = bool(sel.targets) and bool(sel.unresolvable)
    return sel


def readiness_peers(
    settings_xml_path: str,
    network_map_path: str,
    *,
    nodes: list[NodeView] | None = None,
) -> Selection:
    """Which node-engines the boot auto-load waits for on the NNG hub.

    Adopted nodes only (an unadopted node cannot be required to join before a
    show loads, and nothing can force it at boot). Identified by `<ip>`,
    because the hub matches bus peers by address — the deliberate exception to
    the avahi-only rule.

    `targets` is a list of `(ip, label)`. The NO_IP skip is defensive: the
    schema makes `<ip>` mandatory, so a node without one invalidates the whole
    document long before this runs (research R11). It is kept because this
    adapter does not get to assume a guarantee it does not itself check.
    """
    views = nodes if nodes is not None else load_nodes(settings_xml_path, network_map_path)
    others = _other_nodes(views)
    adopted = [v for v in others if v.adopted]

    sel = Selection(mode="adopted", found=len(others), adopted_count=len(adopted))
    for v in others:
        if not v.adopted:
            sel.skipped.append(Skip(v.uuid, v.label, SkipReason.UNADOPTED))
    for v in adopted:
        if not (v.ip and v.ip.strip()):
            sel.skipped.append(Skip(v.uuid, v.label, SkipReason.NO_IP))
            continue
        sel.targets.append((v.ip.strip(), v.label))

    if not others:
        sel.mode = "controller_only"
    sel.partial = bool(sel.targets) and any(
        s.reason is SkipReason.NO_IP for s in sel.skipped
    )
    return sel
