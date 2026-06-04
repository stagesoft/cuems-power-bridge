# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Config loader for /etc/cuems/power-bridge.conf.

Plain key=value (matching cuems-midi-connector style). Falls back to the
package-data default at src/cuemspowerbridge/data/power-bridge.conf.default
if the system file is absent (useful for unit tests).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

SYSTEM_CONFIG = "/etc/cuems/power-bridge.conf"


@dataclass
class Config:
    # Shelly endpoint
    shelly_url: str = "http://192.168.6.2"
    shelly_username: str = ""
    shelly_password: str = ""
    shelly_switch_id: int = 0

    # Safety
    refuse_if_running: bool = True
    shutdown_max_wait_s: int = 180
    shelly_safety_timer_s: int = 60

    # Engine + editor channels
    engine_ws_url: str = "ws://localhost:9190"
    editor_ws_url: str = "ws://localhost:9092"
    auto_load_project: str = ""
    auto_load_persistent: bool = False

    # Auto-load node-readiness gate (feat/autoload-wait-for-nodes). Before
    # firing project_ready we wait until the expected node-engines are
    # connected to the controller's NNG hub (so the engine's own liveness
    # probe counts them as alive and never excludes them), then require
    # armed=="yes" (not merely loaded) and retry. See power-bridge.conf.default.
    auto_load_wait_nodes: bool = True
    auto_load_node_ids: str = ""        # CSV of role_ids to wait for; empty = ALL adopted slaves
    auto_load_node_timeout_s: int = 420  # max wait for node-engines on the bus; covers fsck, then DEGRADED
    auto_load_node_settle_s: int = 10    # small margin after bus-connect before loading
    auto_load_armed_timeout_s: int = 150  # wait for armed==yes (> engine's 120 s arm watchdog)
    auto_load_max_attempts: int = 5      # soft attempts, then slow-cadence self-heal retries

    # NNG hub port the node-engines connect to. NOT a conf knob: resolved
    # from <nng_hub_port> in settings.xml at load() time (single source of
    # truth — a separate knob could drift from the engine's actual port).
    # The field default is only the fallback when settings.xml is unreadable.
    nng_hub_port: int = 9093
    settings_xml_path: str = "/etc/cuems/settings.xml"

    # Operational
    dry_run: bool = False
    unresolvable_nodes_policy: str = "skip"

    # SSH
    ssh_user: str = "cuems-admin"
    ssh_key: str = "/etc/cuems/power-bridge.key"
    poweroff_cmd: str = "sudo /sbin/poweroff"
    # Optional override for the LOCAL controller poweroff. Empty → fall back
    # to `poweroff_cmd`. Useful for safe unattended testing: set it to
    # "sudo /usr/bin/systemctl reboot" so the controller cycles back on its
    # own (nodes still really power off) without depending on Wake-on-LAN or a
    # physical power-on. WoL-from-S5 is verified working on this cluster's
    # Realtek r8169 NICs (re-armed each boot by cuems-arm-wol); r8169 WoL can
    # be flaky across hardware generally, which is the other reason a
    # self-recovering reboot is convenient during tests.
    controller_poweroff_cmd: str = ""

    # Bind
    listen_host: str = "0.0.0.0"
    listen_port: int = 8478
    shared_token: str = ""

    # network_map
    network_map_path: str = "/etc/cuems/network_map.xml"

    # Projectors / displays (see the displays/ subpackage). Global toggles
    # here; per-device config is in projector.N.* keys, captured in `extras`
    # and parsed by DisplayManager.from_config().
    projector_power_off_on_shutdown: bool = True
    projector_power_on_on_load: bool = True
    projector_command_timeout_s: float = 5.0

    extras: dict = field(default_factory=dict)

    def node_ids_list(self) -> list[str]:
        """Parse `auto_load_node_ids` CSV → stripped, blank-free role_ids."""
        return [s.strip() for s in self.auto_load_node_ids.split(",") if s.strip()]

    def validate(self) -> None:
        """Hard-validate at startup; raises ValueError on bad config."""
        if not (45 <= self.shelly_safety_timer_s <= 300):
            raise ValueError(
                f"shelly_safety_timer_s={self.shelly_safety_timer_s} out of "
                "range; must be 45..300 (too short = mid-shutdown power cut)"
            )
        if self.shutdown_max_wait_s < 30:
            raise ValueError(
                f"shutdown_max_wait_s={self.shutdown_max_wait_s} too low; "
                "nodes need time to shut down"
            )
        if self.unresolvable_nodes_policy not in ("skip",):
            raise ValueError(
                f"unresolvable_nodes_policy={self.unresolvable_nodes_policy!r} "
                "unsupported (only 'skip' for now)"
            )
        if not self.shelly_url.startswith(("http://", "https://")):
            raise ValueError(f"shelly_url must be http(s):// — got {self.shelly_url!r}")
        if not self.engine_ws_url.startswith(("ws://", "wss://")):
            raise ValueError(f"engine_ws_url must be ws(s):// — got {self.engine_ws_url!r}")
        if not self.editor_ws_url.startswith(("ws://", "wss://")):
            raise ValueError(f"editor_ws_url must be ws(s):// — got {self.editor_ws_url!r}")
        if self.projector_command_timeout_s <= 0:
            raise ValueError(
                f"projector_command_timeout_s={self.projector_command_timeout_s} "
                "must be > 0 (0 makes every projector command time out instantly)"
            )
        for name in ("auto_load_node_timeout_s", "auto_load_node_settle_s",
                     "auto_load_armed_timeout_s"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}={getattr(self, name)} must be >= 0")
        if self.auto_load_armed_timeout_s < 30:
            raise ValueError(
                f"auto_load_armed_timeout_s={self.auto_load_armed_timeout_s} too low; "
                "must be >= 30 (recommend >= 150 — the engine's arm watchdog is 120 s)"
            )
        if self.auto_load_max_attempts < 1:
            raise ValueError(
                f"auto_load_max_attempts={self.auto_load_max_attempts} must be >= 1"
            )


def _coerce(name: str, raw: str, current):
    """Coerce raw string to the type matching the current field value."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("true", "1", "yes", "on")
    if isinstance(current, float):
        # Check float before int — float fields accept "5" and "1.5";
        # int(raw) would reject "1.5".
        return float(raw)
    if isinstance(current, int):
        return int(raw)
    return raw


def _parse(text: str, cfg: Config) -> None:
    known = {f.name for f in cfg.__dataclass_fields__.values() if f.name != "extras"}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            log.warning("config line %d ignored (no '='): %s", lineno, raw)
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key in known:
            setattr(cfg, key, _coerce(key, value, getattr(cfg, key)))
        else:
            cfg.extras[key] = value


def _read_nng_hub_port(path: str) -> int | None:
    """Read <nng_hub_port> from settings.xml. Returns None if the file is
    missing/unparseable or the element is absent/non-numeric (caller keeps
    the fallback default). Namespace-agnostic (matches by local-name)."""
    import xml.etree.ElementTree as ET

    try:
        tree = ET.parse(path)
    except (FileNotFoundError, OSError, ET.ParseError) as e:
        log.info("config: could not read nng_hub_port from %s (%s); "
                 "using fallback", path, type(e).__name__)
        return None
    for el in tree.getroot().iter():
        if el.tag.split("}", 1)[-1] != "nng_hub_port":
            continue
        if el.text and el.text.strip():
            try:
                return int(el.text.strip())
            except ValueError:
                log.warning("config: non-numeric <nng_hub_port>=%r in %s; "
                            "using fallback", el.text, path)
                return None
    return None


def load(path: str | None = None) -> Config:
    """Load config from path (default /etc/cuems/power-bridge.conf).

    Layered: package-data default loaded first, then the system file
    overrides on top. Missing system file is OK (defaults apply).
    """
    cfg = Config()
    # 1) bundled defaults — best-effort
    try:
        default_text = resources.files("cuemspowerbridge.data").joinpath(
            "power-bridge.conf.default"
        ).read_text()
        _parse(default_text, cfg)
    except (FileNotFoundError, ModuleNotFoundError):
        pass

    # 2) system file overrides
    sys_path = Path(path or SYSTEM_CONFIG)
    if sys_path.is_file():
        _parse(sys_path.read_text(), cfg)
        log.info("config: loaded %s", sys_path)
    else:
        log.info("config: %s not found, using defaults", sys_path)

    # 3) resolve nng_hub_port from settings.xml (authoritative single source
    # of truth — overrides any value parsed above; falls back to the field
    # default when settings.xml is unreadable).
    port = _read_nng_hub_port(cfg.settings_xml_path)
    if port is not None:
        cfg.nng_hub_port = port
    log.info("config: nng_hub_port=%d", cfg.nng_hub_port)

    cfg.validate()
    return cfg
