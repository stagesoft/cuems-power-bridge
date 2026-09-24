# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Internal helper: run one stage of the orderly cluster power-off.

**Not an operator command.** There is deliberately no `PATH` entry for this.
It is invoked by `cuems-common`'s `cuems-cluster-poweroff` through the
interpreter that package's configuration names::

    "$venv_python" -m cuemspowerbridge.scripts.cluster_poweroff \\
        --stage {displays|nodes} [--node-wait S] [--projector-timeout S]
        [--include-unadopted] [--dry-run] [--pre-pass] [--lock-held]
        [--refuse-if-running [--while-playing]] [-v]

Invoking it as a module rather than a shim keeps three things working that the
wrapper depends on: the configuration override of that interpreter, the
missing-package guard that tests it, and the rule that exactly **one** command
powers the venue down — `cuems-cluster-poweroff [--force]`.

It never arms the mains-cut relay, never pre-checks it and never powers this
controller off. Those belong to the daemon's `POST /shutdown`, which *ends* by
triggering the transition that runs this helper — a shared sequence containing
them would arm the relay twice per shutdown.

Exit status — the contract the wrapper reads:

===  ==========================================================================
0    complete, including a stated "there was nothing to do"
1    usage error
3    configuration or precondition failure (unreadable config, no lock)
4    refused: topology unreadable, nothing adopted, nothing addressable,
     a project is playing (manual runs only), or another power-off holds
     the lock
5    ran, but some machine never went quiet
===  ==========================================================================

The wrapper turns each of these into a log line and still exits 0 on any path
that runs during a power-off transition: an ExecStop that fails must never fail
the stop it belongs to.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from urllib.error import URLError
from urllib.request import urlopen

from cuemspowerbridge import cluster_shutdown, config, network_map, shutdown_lock

log = logging.getLogger("cuems-power-bridge-cluster-poweroff")

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_PRECONDITION = 3
EXIT_REFUSED = 4
EXIT_STUCK = 5


def _progress(event: str, detail: dict) -> None:
    """Line-oriented stdout: the wrapper forwards each line to the journal."""
    if event == "selected":
        print(f"selected {len(detail.get('targets', []))} node(s): "
              f"{', '.join(detail.get('targets', [])) or '(none)'}")
    elif event == "ssh-issued":
        print("powering off: " + ", ".join(detail.get("hosts", [])))
    elif event == "nothing-to-poll":
        print(f"nothing to poll ({detail.get('reason')})")


def _project_is_playing(bridge_url: str, timeout: float = 3.0) -> bool | None:
    """Ask the running daemon. None means "cannot know".

    Only ever called when the caller passes --refuse-if-running, which the
    wrapper does on its manual path alone. During a power-off transition the
    daemon has already stopped, so this is not merely skipped — it is never
    requested.
    """
    try:
        with urlopen(f"{bridge_url.rstrip('/')}/status", timeout=timeout) as r:
            state = json.loads(r.read().decode()).get("engine_state")
    except (URLError, OSError, ValueError) as e:
        log.warning("cannot ask the bridge whether a project is playing (%s)", e)
        return None
    if state == "unknown":
        return None
    return state == "running"


def _guard(args) -> int | None:
    """The running-show guard. Fails OPEN, and only when requested."""
    if not args.refuse_if_running:
        return None
    if args.dry_run:
        print("dry run — the running-show guard does not apply")
        return None
    playing = _project_is_playing(args.bridge_url)
    if playing is None:
        print("WARNING cannot determine whether a project is playing — proceeding")
        return None
    if not playing:
        return None
    if args.while_playing:
        print("WARNING a project is PLAYING; --while-playing given, proceeding anyway")
        return None
    print("REFUSED a project is playing. Re-run with --while-playing to power the "
          "cluster off anyway. (A power-off transaction never asks this.)")
    return EXIT_REFUSED


async def _run(args) -> int:
    try:
        cfg = config.load()
    except Exception as e:  # noqa: BLE001 - validate() raises on any bad field
        print(f"ERROR config load/validate failed: {e}", file=sys.stderr)
        return EXIT_PRECONDITION

    if args.dry_run:
        cfg.dry_run = True
    if args.projector_timeout:
        cfg.projector_command_timeout_s = args.projector_timeout

    from cuemspowerbridge.displays.manager import DisplayManager

    ctx = cluster_shutdown.StageContext(
        cfg=cfg, displays=DisplayManager.from_config(cfg), progress=_progress,
    )

    if args.stage == "displays":
        out = await cluster_shutdown.run_display_stage(ctx)
        print(f"displays: {out.detail}")
        return EXIT_OK

    # --- nodes ------------------------------------------------------------
    try:
        nodes = network_map.load_nodes(cfg.settings_xml_path, cfg.network_map_path)
        selection = network_map.shutdown_targets(
            cfg.settings_xml_path, cfg.network_map_path,
            include_unadopted=args.include_unadopted, nodes=nodes,
        )
    except network_map.TopologyError as e:
        print(f"REFUSED {e}", file=sys.stderr)
        return EXIT_REFUSED

    if selection.found and not selection.targets:
        if selection.adopted_count == 0 and not args.include_unadopted:
            print(f"REFUSED {selection.found} node(s) in the map, none adopted: "
                  + ", ".join(k.label for k in selection.skipped)
                  + ". Adopt them, or re-run with --include-unadopted.", file=sys.stderr)
        else:
            print("REFUSED none of the node(s) to power off is addressable "
                  "(no role_id/alias/hostname): "
                  + ", ".join(k.uuid for k in selection.unresolvable), file=sys.stderr)
        return EXIT_REFUSED

    out = await cluster_shutdown.run_node_stage(
        ctx, selection, pre_pass=args.pre_pass, dry_run_skips_wait=True,
        max_wait_s=args.node_wait,
    )
    if out.stuck_hosts:
        print("WARNING still up: " + ", ".join(out.stuck_hosts))
        return EXIT_STUCK
    return EXIT_OK


def main() -> None:
    p = argparse.ArgumentParser(
        prog="cuems-power-bridge-cluster-poweroff",
        description="One stage of the orderly cluster power-off (internal helper; "
                    "the operator command is cuems-cluster-poweroff).",
    )
    p.add_argument("--stage", required=True, choices=("displays", "nodes"))
    p.add_argument("--node-wait", type=float, default=120.0,
                   help="seconds to wait for nodes to go quiet (caller's value)")
    p.add_argument("--projector-timeout", type=float, default=0.0,
                   help="per-device display timeout (caller's value; 0 = config)")
    p.add_argument("--include-unadopted", action="store_true",
                   help="target every node in the map, not only the adopted ones")
    p.add_argument("--dry-run", action="store_true",
                   help="exercise every branch and change nothing")
    p.add_argument("--pre-pass", action="store_true",
                   help="probe liveness once and SSH only the nodes that answered")
    p.add_argument("--lock-held", action="store_true",
                   help="the CALLER holds the sequence lock (the wrapper holds one "
                        "across both stages); do not take it here")
    p.add_argument("--refuse-if-running", action="store_true",
                   help="refuse while a project is playing. Manual runs only: a "
                        "power-off transaction never passes this")
    p.add_argument("--while-playing", action="store_true",
                   help="with --refuse-if-running, proceed anyway and say so")
    p.add_argument("--bridge-url", default="http://127.0.0.1:8478")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(name)s: %(message)s", stream=sys.stdout, force=True,
    )

    refused = _guard(args)
    if refused is not None:
        sys.exit(refused)

    lock_cm = (shutdown_lock.held_elsewhere() if args.lock_held
               else shutdown_lock.acquire())
    try:
        with lock_cm:
            sys.exit(asyncio.run(_run(args)))
    except shutdown_lock.ShutdownInProgress as e:
        print(f"REFUSED {e}", file=sys.stderr)
        sys.exit(EXIT_REFUSED)
    except shutdown_lock.ShutdownLockUnavailable as e:
        print(f"ERROR {e}", file=sys.stderr)
        sys.exit(EXIT_PRECONDITION)


if __name__ == "__main__":  # pragma: no cover - module entry point
    main()
