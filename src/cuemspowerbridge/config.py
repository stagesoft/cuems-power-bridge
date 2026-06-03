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

    # Operational
    dry_run: bool = False
    unresolvable_nodes_policy: str = "skip"

    # SSH
    ssh_user: str = "cuems"
    ssh_key: str = "/etc/cuems/power-bridge.key"
    poweroff_cmd: str = "sudo /sbin/poweroff"
    # Optional override for the LOCAL controller poweroff. Empty → fall back
    # to `poweroff_cmd`. Useful when:
    # - Controller WoL-from-S5 is unreliable (Realtek r8169 known issue), so
    #   you want the controller to reboot while nodes really power off.
    # - Maintenance flows where the controller comes back unattended via
    #   boot-on-power-restore + a systemd reboot is the safer recovery.
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
    projector_command_timeout_s: int = 5

    extras: dict = field(default_factory=dict)

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


def _coerce(name: str, raw: str, current):
    """Coerce raw string to the type matching the current field value."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("true", "1", "yes", "on")
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

    cfg.validate()
    return cfg
