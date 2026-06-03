# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Binary OSC message parser. Mirrors the engine's own parser (see
cuems-engine/src/cuemsengine/osc/WebSocketOscHandler.parse_osc_message)
so the bridge speaks the exact same wire format on `/realtime`.
"""

from __future__ import annotations

import logging
from typing import Any

from pythonosc.parsing import osc_types

log = logging.getLogger(__name__)


def parse_osc_message(data: bytes) -> tuple[str, list[Any]] | None:
    """Parse a binary OSC message into (address, [args])."""
    try:
        address, index = osc_types.get_string(data, 0)
        if index >= len(data):
            return (address, [])
        type_tags, index = osc_types.get_string(data, index)
        if not type_tags.startswith(","):
            return (address, [])
        args: list[Any] = []
        for tag in type_tags[1:]:
            if tag == "i":
                v, index = osc_types.get_int(data, index)
                args.append(v)
            elif tag == "f":
                v, index = osc_types.get_float(data, index)
                args.append(v)
            elif tag == "s":
                v, index = osc_types.get_string(data, index)
                args.append(v)
            elif tag == "b":
                v, index = osc_types.get_blob(data, index)
                args.append(v)
            elif tag == "T":
                args.append(True)
            elif tag == "F":
                args.append(False)
            elif tag in ("N", "I"):
                args.append(None)
            elif tag == "d":
                v, index = osc_types.get_double(data, index)
                args.append(v)
            else:
                log.debug("unknown OSC type tag: %s", tag)
        return (address, args)
    except Exception as e:
        log.debug("osc parse failed: %s", e)
        return None
