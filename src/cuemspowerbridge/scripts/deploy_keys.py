# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Operator-run helper: authorise the bridge's SSH pubkey on every node.

Run as the operator. SSHes to each node AS the bridge's ssh_user
(default ``cuems-admin``) using the operator's own credentials (the org key
must be loaded in your agent / ~/.ssh), and appends the bridge public key to
THAT user's own ``~/.ssh/authorized_keys`` — no ``sudo`` needed, since you are
writing your own file (cuems-admin's full sudo requires a password and would
hang a non-interactive ``sudo``).

The key is written with a forced-command lock so it can do nothing but trigger
poweroff:

    restrict,command="sudo /sbin/poweroff" ssh-ed25519 AAAA... cuems-power-bridge@controller

Re-runs are idempotent: any prior entry for the same key (bare or otherwise
restricted) is replaced with the locked entry.

Usage:
  cuems-power-bridge-deploy-keys node01.local node02.local ...
  cuems-power-bridge-deploy-keys --ssh-user cuems-admin node01.local
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
from pathlib import Path

PUB_KEY = Path("/etc/cuems/power-bridge.key.pub")
DEFAULT_SSH_USER = "cuems-admin"
DEFAULT_POWEROFF_CMD = "sudo /sbin/poweroff"

# Runs AS the connecting user (e.g. cuems-admin), writing that user's OWN
# authorized_keys — deliberately NO sudo. Removes any existing line carrying
# the same key material (BLOB) so a re-run, or an earlier bare-key deploy, is
# upgraded to the locked ENTRY rather than duplicated.
REMOTE_SCRIPT = r"""
set -e
H="$HOME/.ssh"
mkdir -p "$H"
chmod 0700 "$H"
touch "$H/authorized_keys"
chmod 0600 "$H/authorized_keys"
if grep -qF -- "$BLOB" "$H/authorized_keys"; then
    grep -vF -- "$BLOB" "$H/authorized_keys" > "$H/authorized_keys.tmp"
    mv "$H/authorized_keys.tmp" "$H/authorized_keys"
    chmod 0600 "$H/authorized_keys"
    STATUS="updated"
else
    STATUS="added"
fi
printf '%s\n' "$ENTRY" >> "$H/authorized_keys"
echo "$STATUS"
"""


def deploy(host: str, pubkey: str, ssh_user: str, poweroff_cmd: str) -> bool:
    # pubkey = "<type> <base64 blob> [comment]"; lock it to the forced command.
    parts = pubkey.split()
    if len(parts) < 2:
        print(f"→ {host}: SKIPPED (malformed pubkey)")
        return False
    blob = parts[1]
    entry = f'restrict,command="{poweroff_cmd}" {pubkey}'

    target = f"{ssh_user}@{host}"
    prelude = (
        f"BLOB={shlex.quote(blob)}\n"
        f"ENTRY={shlex.quote(entry)}\n"
    )
    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", target,
           prelude + REMOTE_SCRIPT]
    print(f"→ {ssh_user}@{host}: ", end="", flush=True)
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
        description=("Authorise the bridge pubkey (poweroff-locked) in the "
                     "ssh_user's ~/.ssh/authorized_keys on each node."),
    )
    parser.add_argument("nodes", nargs="+", help="avahi hostnames (e.g. node01.local)")
    parser.add_argument("--ssh-user", default=DEFAULT_SSH_USER,
                        help=f"SSH user to connect/authorise as (default: {DEFAULT_SSH_USER})")
    parser.add_argument("--poweroff-cmd", default=DEFAULT_POWEROFF_CMD,
                        help=f"forced command locked to the key (default: {DEFAULT_POWEROFF_CMD!r})")
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
        if not deploy(host, pubkey, args.ssh_user, args.poweroff_cmd):
            failures += 1
    if failures:
        print(f"\n{failures} of {len(args.nodes)} hosts failed", file=sys.stderr)
        return 1
    print(f"\nAll {len(args.nodes)} hosts done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
