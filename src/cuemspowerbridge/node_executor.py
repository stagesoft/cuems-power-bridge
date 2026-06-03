# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""SSH-based parallel poweroff. Fire-and-forget; reachability poller is
the ack.

This is the module that gets **replaced** when we migrate to engine-
native NNG-broadcast shutdown. See the migration section of the plan.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class SshTarget:
    host: str            # avahi hostname (e.g. "node01.local")
    user: str
    key_path: str
    poweroff_cmd: str    # e.g. "sudo /sbin/poweroff"


async def _ssh_one(target: SshTarget, dry_run: bool, connect_timeout: int = 5) -> bool:
    """Run the poweroff command on one host. Returns True if ssh exited 0."""
    argv = [
        "ssh",
        "-i", target.key_path,
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        f"{target.user}@{target.host}",
        "--",
        target.poweroff_cmd,
    ]
    if dry_run:
        log.info("[dry_run] would ssh: %s", " ".join(shlex.quote(a) for a in argv))
        return True
    log.info("ssh poweroff → %s", target.host)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Tight timeout: a graceful poweroff doesn't need long, and we
        # don't want to hang the gather. The node going unreachable on
        # the network is the real ack (reachability poller).
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("ssh poweroff → %s: timed out (will rely on reachability poll)", target.host)
            return False
        if proc.returncode == 0:
            log.info("ssh poweroff → %s: command accepted (rc=0)", target.host)
            return True
        log.warning("ssh poweroff → %s: rc=%d stderr=%s",
                    target.host, proc.returncode, stderr.decode(errors="replace").strip()[:200])
        return False
    except FileNotFoundError:
        log.error("ssh binary not found — install openssh-client")
        return False
    except Exception as e:
        log.warning("ssh poweroff → %s: %s (will rely on reachability poll)", target.host, e)
        return False


async def poweroff_all(targets: list[SshTarget], dry_run: bool) -> dict[str, bool]:
    """Run ssh_one() in parallel for every target. Returns {host: rc0_ok}."""
    if not targets:
        return {}
    results = await asyncio.gather(*[_ssh_one(t, dry_run) for t in targets])
    return dict(zip([t.host for t in targets], results))
