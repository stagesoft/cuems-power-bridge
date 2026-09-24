# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Print one value from /etc/cuems/power-bridge.conf.

Exists so shell scripts in other packages stop importing this package's
modules for a couple of values. `cuems-common`'s `cuems-displays-on` needs
`projector_power_on_on_start` and `shared_token`; with this it needs no Python
of ours at all.

    cuems-power-bridge-config --get <key> [--config PATH]

Prints the bare value and nothing else, so it can be captured directly::

    token="$(cuems-power-bridge-config --get shared_token)"

**Secrets**: the KEY is the argument, never the value, so a token never
reaches `argv` where `ps` would show it. The caller keeps feeding it to `curl`
on stdin.

Exit status: 0 with the value on stdout — **including an empty value**, which
is a legitimate configuration a caller branches on; 1 on usage; 3 for an
unknown key, or for a `--config` path that does not exist. A *missing default*
configuration file is not an error: the daemon itself falls back to defaults
there, and the query must answer what the daemon would use.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys

from cuemspowerbridge import config


def main() -> None:
    p = argparse.ArgumentParser(
        prog="cuems-power-bridge-config",
        description="Print one value from the bridge's configuration.",
    )
    p.add_argument("--get", required=True, metavar="KEY",
                   help="configuration key to print")
    p.add_argument("--config", default=None, metavar="PATH",
                   help="configuration file (default: /etc/cuems/power-bridge.conf)")
    args = p.parse_args()

    # config.load() falls back to defaults when the file is absent — correct
    # for the daemon, wrong for a query that was pointed at a specific file:
    # answering with a default would tell the caller the value it asked about
    # is set to something it is not.
    if args.config is not None and not os.path.exists(args.config):
        print(f"ERROR no such configuration file: {args.config}", file=sys.stderr)
        sys.exit(3)

    try:
        cfg = config.load(args.config)
    except Exception as e:  # noqa: BLE001 - validate() raises on any bad field
        print(f"ERROR cannot read the configuration: {e}", file=sys.stderr)
        sys.exit(3)

    known = {f.name for f in dataclasses.fields(cfg)}
    if args.get not in known:
        print(f"ERROR no such configuration key: {args.get}", file=sys.stderr)
        sys.exit(3)

    value = getattr(cfg, args.get)
    if isinstance(value, bool):
        # Shell-friendly, and the same spelling the scripts already compare to.
        print("true" if value else "false")
    else:
        print(value)


if __name__ == "__main__":  # pragma: no cover - console entry point
    main()
