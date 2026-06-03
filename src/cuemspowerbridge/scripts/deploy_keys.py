# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Operator-run helper: distribute the bridge's SSH pubkey to every node.

Run as the operator (not as the cuems service user). SSHes to each node
using the operator's own credentials, appends
/etc/cuems/power-bridge.key.pub to /home/cuems/.ssh/authorized_keys,
and verifies dir+file modes.

Usage:
  cuems-power-bridge-deploy-keys node01.local node02.local ...
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

PUB_KEY = Path("/etc/cuems/power-bridge.key.pub")

REMOTE_SCRIPT = r"""
set -e
sudo mkdir -p /home/cuems/.ssh
sudo chmod 0700 /home/cuems/.ssh
sudo chown cuems:cuems /home/cuems/.ssh
sudo touch /home/cuems/.ssh/authorized_keys
sudo chmod 0600 /home/cuems/.ssh/authorized_keys
sudo chown cuems:cuems /home/cuems/.ssh/authorized_keys
# Append pubkey only if not already present.
if ! sudo grep -qxF -- "$KEY" /home/cuems/.ssh/authorized_keys; then
    echo "$KEY" | sudo tee -a /home/cuems/.ssh/authorized_keys >/dev/null
    echo "added"
else
    echo "already present"
fi
"""


def deploy(host: str, pubkey: str, ssh_user: str | None) -> bool:
    target = f"{ssh_user}@{host}" if ssh_user else host
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", target,
           f"KEY={shlex.quote(pubkey)}\n" + REMOTE_SCRIPT]
    print(f"→ {host}: ", end="", flush=True)
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        print("TIMEOUT")
        return False
    if r.returncode != 0:
        print(f"FAILED (rc={r.returncode})")
        if r.stderr.strip():
            print(f"  stderr: {r.stderr.strip()[:300]}")
        return False
    print(r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "done")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cuems-power-bridge-deploy-keys",
        description="Append /etc/cuems/power-bridge.key.pub to /home/cuems/.ssh/authorized_keys on each node.",
    )
    parser.add_argument("nodes", nargs="+", help="avahi hostnames (e.g. node01.local)")
    parser.add_argument("--ssh-user",
                        help="username for the operator's SSH to each node (default: current user)")
    parser.add_argument("--pubkey", default=str(PUB_KEY),
                        help=f"public key path on this host (default: {PUB_KEY})")
    args = parser.parse_args()

    p = Path(args.pubkey)
    if not p.is_file():
        print(f"ERROR: pubkey not found at {p}. Did postinst generate it?", file=sys.stderr)
        return 2
    pubkey = p.read_text().strip()
    if not pubkey:
        print(f"ERROR: pubkey at {p} is empty", file=sys.stderr)
        return 2

    failures = 0
    for host in args.nodes:
        if not deploy(host, pubkey, args.ssh_user):
            failures += 1
    if failures:
        print(f"\n{failures} of {len(args.nodes)} hosts failed", file=sys.stderr)
        return 1
    print(f"\nAll {len(args.nodes)} hosts done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
