# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The two power-off stage bodies, shared by the two callers.

Two entry points reach a cluster power-off, and they are **not** the same
sequence:

* ``POST /shutdown`` — decides, runs both stages *concurrently*, arms the
  Shelly's hardware mains-cut timer, then powers this controller off. It owns
  the relay and the local poweroff, and they stay in :mod:`bridge`.
* the systemd ``ExecStop`` hook (``cuems-cluster-poweroff``) — runs during a
  poweroff transition the machine is already committed to, so it needs neither
  the relay nor a local poweroff: *"only steps 1-2 are needed"*. It runs the
  stages **sequentially**, displays first, because the nodes feed the
  projectors and blanking them first puts "no signal" on stage.

What the two share is this module: the **stage bodies** and the selection they
act on. Sharing the whole sequence would be wrong twice over — the hook must
never arm the relay, and ``/shutdown`` ends by triggering the very transition
that runs the hook, so a shared sequence would arm it twice per shutdown.

Nothing here touches the relay, the local poweroff, or the engine. The
running-show guard is the *caller's* business and is requested explicitly, so
a poweroff transaction cannot reach it even by accident.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import network_map
from .node_executor import SshTarget, poweroff_all
from .reachability import wait_until_all_down

log = logging.getLogger(__name__)

#: Reported OUT by the stages; the daemon maps these to `_set_state` and
#: `/status`, the helper to stdout lines. Fixed vocabulary, so the two callers
#: can be compared event-for-event.
Progress = Callable[[str, dict], None]


def _noop(event: str, detail: dict) -> None:  # pragma: no cover - trivial
    pass


class _Displays(Protocol):
    """Only what the display stage uses (DisplayManager satisfies it)."""

    configured: bool

    async def status_all(self) -> list[dict]: ...
    async def power_off_all(self) -> None: ...
    def snapshot(self) -> list[dict]: ...


@dataclass
class StageContext:
    """What the stages need — and deliberately nothing else.

    No ``shelly``, no local-poweroff command: those belong to ``/shutdown``
    alone. No ``engine``: the running-show guard is requested by the caller,
    never consulted here.
    """

    cfg: Any
    displays: _Displays
    progress: Progress = _noop


@dataclass
class StageOutcome:
    """What a stage did, stated rather than inferred."""

    stage: str
    ok: bool = True
    detail: str = ""
    stuck_hosts: list[str] = field(default_factory=list)
    timed_out: bool = False
    targeted: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Stage 1 — displays
# ---------------------------------------------------------------------------


async def run_display_stage(ctx: StageContext) -> StageOutcome:
    """Power the display fleet off, or state precisely why it did not.

    Three gates, all of them load-bearing and none of them an error:

    * ``projector_power_off_on_shutdown=false`` — venues keep configured
      fleets dark on purpose; a power-off must leave them alone;
    * nothing configured — nothing to do;
    * every device already off or cooling — the caller's own local poweroff
      re-enters this path through the systemd transition, and that second pass
      must stay a fast no-op rather than a second full PJLink cycle.

    A fleet where *no* device answers is skipped loudly: a status query costs a
    timeout per device and the power-off costs three attempts plus backoff, so
    an unreachable fleet would add ~19 s to every shutdown to achieve nothing.
    One device answering is enough to command the whole fleet.
    """
    if not ctx.cfg.projector_power_off_on_shutdown:
        log.info("displays: projector_power_off_on_shutdown=false — leaving displays alone")
        ctx.progress("displays", {"skipped": "disabled"})
        return StageOutcome("displays", detail="disabled")

    if not ctx.displays.configured:
        log.info("displays: no displays configured")
        ctx.progress("displays", {"skipped": "not-configured"})
        return StageOutcome("displays", detail="not-configured")

    states = await ctx.displays.status_all()
    log.info("displays: current: %s",
             ", ".join(f"{d['name']}={d['power']}" for d in states))

    if states and all(d["power"] in ("off", "cooldown") for d in states):
        log.info("displays: all already off/cooling — skipping POWR 0")
        ctx.progress("displays", {"skipped": "already-off"})
        return StageOutcome("displays", detail="already-off")

    if states and all(d["power"] == "unknown" for d in states):
        log.warning(
            "displays: no display answered a status query — fleet unreachable, "
            "skipping POWR 0 (check cabling, projector.N.host, and "
            "'Standby Mode Communications = ON')"
        )
        ctx.progress("displays", {"skipped": "unreachable"})
        return StageOutcome("displays", detail="unreachable")

    await ctx.displays.power_off_all()
    after = ctx.displays.snapshot()
    log.info("displays: after:   %s",
             ", ".join(f"{d['name']}={d['power']}" for d in after))
    ctx.progress("displays", {"before": states, "after": after})
    return StageOutcome("displays", detail="powered-off")


# ---------------------------------------------------------------------------
# Stage 2 — cluster nodes
# ---------------------------------------------------------------------------


async def run_node_stage(
    ctx: StageContext,
    selection: network_map.Selection,
    *,
    pre_pass: bool = False,
    dry_run_skips_wait: bool = False,
    max_wait_s: float,
) -> StageOutcome:
    """Power the selected nodes off and confirm they went quiet.

    ``pre_pass`` is the transition path's single liveness probe: one pass at
    ``max_wait_s=0`` with ``confirm_failures=1`` — the default of 3 would leave
    every host unconfirmed and return them all as alive — after which **only
    the nodes that answered alive** are SSHed. A node already down is then
    never waited for, which is worth the full wait on every power-off. The
    poller's own logger is silenced for that one call: a deliberate zero-length
    wait logs "timeout after 0.0s; stuck hosts: …" at WARNING, which in a
    shutdown log reads like a fault when it is in fact the expected answer.

    ``/shutdown`` passes ``pre_pass=False`` and SSHes every resolved target.

    ``dry_run_skips_wait`` keeps the two callers' *existing* rehearsal
    behaviour, which differs and is preserved verbatim rather than unified:
    the ExecStop hook skips the wait under ``dry_run`` (nothing was commanded
    off, so nothing will go down), while ``/shutdown`` has always polled
    regardless. Merging them would have changed one of the two silently —
    which the existing suite caught.
    """
    resolved = list(selection.targets)
    outcome = StageOutcome("nodes", targeted=resolved)

    if selection.mode == "controller_only":
        # The map was read and it says this cluster is one machine. An ANSWER,
        # not an anomaly — and said out loud, because "no nodes to power off"
        # must never be silence.
        log.info("shutdown: controller-only cluster (%s lists no other node) "
                 "— no node poweroff, nothing to wait for",
                 ctx.cfg.network_map_path)
        ctx.progress("nothing-to-poll", {"reason": "controller_only"})
        return outcome

    log.info("shutdown: %d node(s) to power off (%s): %s",
             len(resolved), selection.mode, ", ".join(resolved))
    if selection.partial:
        log.error(
            "shutdown: PARTIAL selection — %d of %d adopted node(s) could "
            "not be addressed and will stay up when mains is cut: %s",
            len(selection.unresolvable), selection.adopted_count,
            ", ".join(k.uuid for k in selection.unresolvable),
        )
    skipped = [f"{k.label}({k.reason.value})" for k in selection.skipped]
    if skipped:
        log.info("shutdown: skipping %d node(s): %s", len(skipped), ", ".join(skipped))
    ctx.progress("selected", {
        "targets": resolved, "mode": selection.mode,
        "partial": selection.partial,
        "skipped": [(k.label, k.reason.value) for k in selection.skipped],
    })

    alive = resolved
    if pre_pass and resolved:
        reach_log = logging.getLogger("cuemspowerbridge.reachability")
        prev_level = reach_log.level
        reach_log.setLevel(logging.ERROR)
        try:
            snap = await wait_until_all_down(
                resolved, interval_s=0.1, max_wait_s=0, confirm_failures=1)
        finally:
            reach_log.setLevel(prev_level)
        alive = list(snap.stuck_hosts)
        already = [h for h in resolved if h not in alive]
        if already:
            log.info("shutdown: already down: %s", ", ".join(already))
        if not alive:
            log.info("shutdown: all node(s) already down — skipping SSH fan-out")
            ctx.progress("nothing-to-poll", {"reason": "already_down"})
            return outcome

    # ssh returns 255 as a node drops — expected. The poll is the ack.
    ctx.progress("ssh-issued", {"hosts": alive})
    await poweroff_all(
        [SshTarget(host=h, user=ctx.cfg.ssh_user, key_path=ctx.cfg.ssh_key,
                   poweroff_cmd=ctx.cfg.poweroff_cmd) for h in alive],
        dry_run=ctx.cfg.dry_run,
    )

    if ctx.cfg.dry_run and dry_run_skips_wait:
        log.info("[dry_run] not waiting for nodes to go down")
        return outcome

    if not alive:
        # Only reachable when the caller selected nothing and did not use the
        # pre-pass; stated rather than silent.
        log.info("shutdown: no nodes to poll")
        ctx.progress("nothing-to-poll", {"reason": "no_targets"})
        return outcome

    ctx.progress("polling", {"hosts": alive})
    result = await wait_until_all_down(alive, interval_s=2.0, max_wait_s=max_wait_s)
    outcome.stuck_hosts = list(result.stuck_hosts)
    outcome.timed_out = bool(result.timed_out)
    if result.timed_out:
        log.warning(
            "shutdown: reachability timeout (%.1fs), proceeding anyway "
            "with stuck hosts: %s", result.elapsed_s, ", ".join(result.stuck_hosts),
        )
    else:
        log.info("shutdown: all node(s) confirmed down in %.1fs", result.elapsed_s)
    ctx.progress("done", {"stuck_hosts": outcome.stuck_hosts,
                          "timed_out": outcome.timed_out})
    return outcome
