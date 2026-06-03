# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Diagnostic CLI: query or toggle a single display device's power over
its control protocol. For bring-up — verify a projector actually responds
before wiring `projector.N.*` into /etc/cuems/power-bridge.conf.

    cuems-power-bridge-test-projector <host> {status|on|off} \\
        [--driver pjlink] [--port 4352] [--password SECRET] [--timeout 5]

Exit status: 0 on success, 1 on driver error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from cuemspowerbridge.displays.base import DeviceDef, DisplayError
from cuemspowerbridge.displays.manager import DRIVERS


def main() -> None:
    p = argparse.ArgumentParser(
        prog="cuems-power-bridge-test-projector",
        description="Query/toggle a display device's power (bring-up tool).",
    )
    p.add_argument("host", help="projector IP or hostname")
    p.add_argument("action", choices=("status", "on", "off"))
    p.add_argument("--driver", default="pjlink", choices=sorted(DRIVERS))
    p.add_argument("--port", type=int, default=0, help="0 = driver default (pjlink: 4352)")
    p.add_argument("--password", default="", help="PJLink password (blank if none)")
    p.add_argument("--timeout", type=float, default=5.0)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    dev = DeviceDef(
        name="", host=args.host, driver=args.driver,
        port=args.port, password=args.password,
    )
    driver = DRIVERS[args.driver](dev, timeout_s=args.timeout)

    async def run() -> int:
        try:
            if args.action == "status":
                print(await driver.power_status())
            elif args.action == "on":
                await driver.power_on()
                print("ok")
            else:
                await driver.power_off()
                print("ok")
            return 0
        except DisplayError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    raise SystemExit(asyncio.run(run()))


if __name__ == "__main__":
    main()
