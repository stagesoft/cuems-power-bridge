# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The SSH fan-out's argv — in particular, ONE host-key store.

The daemon runs as `cuems` (HOME=/var/lib/cuems) and the ExecStop hook as root
(HOME=/root). Left to `HOME`, they keep separate `known_hosts`, so a node
re-imaged between a daemon shutdown and a transition shutdown is trusted by one
initiator and unknown to the other — discovered mid-shutdown.
"""

from __future__ import annotations

import asyncio

from cuemspowerbridge import node_executor
from cuemspowerbridge.node_executor import KNOWN_HOSTS, SshTarget, poweroff_all


def _target(host="node01.local"):
    return SshTarget(host=host, user="cuems-admin",
                     key_path="/etc/cuems/power-bridge.key",
                     poweroff_cmd="sudo /sbin/poweroff")


def _argv_of(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def fake_exec(*argv, **kw):
        seen.append(list(argv))
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    return seen


def test_every_fan_out_pins_the_shared_known_hosts(monkeypatch):
    seen = _argv_of(monkeypatch)
    asyncio.run(poweroff_all([_target("node01.local"), _target("node02.local")],
                             dry_run=False))
    assert seen, "no ssh was invoked"
    for argv in seen:
        assert f"UserKnownHostsFile={KNOWN_HOSTS}" in argv
        # first contact is still accepted; pinning the file does not pin trust
        assert "StrictHostKeyChecking=accept-new" in argv


def test_the_store_is_the_service_account_s_not_the_caller_s_home():
    """Root and `cuems` must land on the same file, so neither HOME decides."""
    assert KNOWN_HOSTS == "/var/lib/cuems/.ssh/known_hosts"


def test_a_dry_run_issues_no_ssh(monkeypatch):
    seen = _argv_of(monkeypatch)
    asyncio.run(poweroff_all([_target()], dry_run=True))
    assert seen == []
