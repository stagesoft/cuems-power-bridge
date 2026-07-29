# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Entry point for the cuems-power-bridge systemd service."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from cuemspowerbridge.bridge import Bridge
from cuemspowerbridge.config import load as load_config

log = logging.getLogger(__name__)


def _setup_logging(level: str) -> None:
    # force=True is the load-bearing part: this runs inside main() AFTER all
    # imports, and without it a single import-time logging call from any
    # third-party module (aiohttp and friends do this) implicitly installs a
    # root handler first — at which point basicConfig() silently no-ops and
    # the configured format AND --log-level are lost.
    #
    # Handler choice: under systemd (JOURNAL_STREAM set) use the syslog
    # transport — stdout lines are stamped PRIORITY=6 regardless of level, so
    # only /dev/log records let `journalctl -p` / `cuems-logs -e` filter
    # truthfully. The explicit ident matches the unit's existing
    # SyslogIdentifier=cuems-power-bridge, so both transports merge under one
    # identifier. On a terminal, keep the human-readable stdout format.
    if os.environ.get("JOURNAL_STREAM"):
        from logging.handlers import SysLogHandler

        handler: logging.Handler = SysLogHandler(
            address="/dev/log", facility=SysLogHandler.LOG_LOCAL0
        )
        handler.ident = "cuems-power-bridge: "
        handler.setFormatter(
            logging.Formatter("%(levelname)-7s %(name)s: %(message)s")
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
        handlers=[handler],
    )


async def _run(cfg_path: str | None) -> int:
    cfg = load_config(cfg_path)
    bridge = Bridge(cfg)
    runner = await bridge.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_event_loop()

    def _sig(_signo, _frame=None):
        log.info("signal received, shutting down bridge")
        loop.call_soon_threadsafe(stop_event.set)

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda: stop_event.set())
        except NotImplementedError:
            signal.signal(s, _sig)

    # Tell systemd we're ready (best-effort).
    try:
        from systemd.daemon import notify
        notify("READY=1")
    except ImportError:
        pass

    await stop_event.wait()
    log.info("bridge: stopping")
    await bridge.stop()
    await runner.cleanup()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="cuems-power-bridge")
    parser.add_argument(
        "-c", "--config",
        default=os.environ.get("CUEMS_POWER_BRIDGE_CONF"),
        help="Path to power-bridge.conf (default: /etc/cuems/power-bridge.conf)",
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("CUEMS_LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    args = parser.parse_args()
    _setup_logging(args.log_level)
    try:
        return asyncio.run(_run(args.config))
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        log.exception("bridge fatal: %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
