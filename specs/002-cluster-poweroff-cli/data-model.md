<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Phase 1 data model — cluster power-off CLI

**Feature**: `002-cluster-poweroff-cli` · **Date**: 2026-09-23

No persistent data. What this feature models is **the sequence itself**, lifted out of the
daemon so two callers can run it, plus the small values that cross the new boundary.

---

## 1. `StageContext` — what the STAGES need, and nothing more

What is shared is not a sequence but **two stage bodies plus the selection**. The relay and the
local power-off are not here and cannot be reached from here (FR-001a): the HTTP route owns
them, and that route *ends by triggering the transition that runs the other caller*, so a
shared sequence containing them would arm the relay twice per shutdown.

| Field | Type | Why the stages need it |
|---|---|---|
| `cfg` | `Config` | ssh user/key, poweroff command, per-stage timeouts, `dry_run`, `projector_power_off_on_shutdown`, the `projector.N.*` fleet |
| `displays` | `DisplayManager` | the display stage |
| `progress` | `Callable[[str, dict], None]` | reported OUT; the daemon maps events to `_set_state` and `/status`, the CLI to stdout lines. Fixed vocabulary (§1a) |

**Not here, deliberately**: `shelly` (FR-001a), the local power-off command, `engine` (the
running-show guard is the *caller's* business and is requested, not inferred — R3), `editor`,
the auto-load state, the HTTP app.

### 1b. The two shared entry points

```
run_display_stage(ctx, *, progress)                    -> StageOutcome
run_node_stage(ctx, decision, *, pre_pass, progress)   -> StageOutcome
```

`pre_pass` is the transition path's single liveness probe (`max_wait_s=0`, one confirmation,
the poller's logger silenced) after which **only machines that answered alive** are SSHed. The
HTTP route passes `pre_pass=False` and keeps its concurrent orchestration. Callers order the
stages themselves: concurrent for the HTTP route, displays-then-machines for the transition,
because the machines feed the projectors.

### 1a. `progress` events — the fixed vocabulary

One event per step of the sequence, named after the daemon's existing states so nothing has
to be translated, plus the two that carry detail the CLI must print and `/status` must
expose:

| Event | Payload | Emitted |
|---|---|---|
| `selected` | `{targets, skipped, mode, partial}` | once, before anything irreversible |
| `ssh-issued` | `{hosts}` | after the fan-out is dispatched |
| `polling` | `{hosts}` | entering the reachability wait (omitted in the controller-only case, which emits `nothing-to-poll`) |
| `nothing-to-poll` | `{reason}` | Case 1 — stated, never silent |
| `displays` | `{before, after}` | display stage outcome |
| `arming-shelly` | `{seconds}` | before the mains-cut timer is armed |
| `done` | `{stuck_hosts, timed_out}` | terminal, per stage |

Two events that existed in the first draft — `arming-shelly` and `poweroff-issued` — are
**not** stage events. They belong to the HTTP route's own orchestration and stay in
`bridge.py` (FR-001a).

**Both callers must observe the same events in the same order for the same inputs** — that is
what T019 asserts. A new event is a change to this table, not an implementation detail.

---

## 2. `ShutdownDecision` — the six cases, computed once

A pure function of `(selection, force)`, shared by both callers so the decision cannot drift.

| Field | Type | Meaning |
|---|---|---|
| `proceed` | `bool` | whether the sequence runs at all |
| `case` | `"controller_only" \| "adopted" \| "forced_all" \| "no_adopted_nodes" \| "no_resolvable_nodes"` | which of the documented outcomes |
| `targets` | `list[str]` | avahi names to power off |
| `refusal` | `str \| None` | the reason token when `proceed` is false |
| `partial` | `bool` | some intended target is unresolvable |

A sixth outcome — an unreadable topology — never reaches this function: it is a
`TopologyError` raised by the read, and both callers translate it (HTTP 503 / exit 4).

**Mapping to the two callers:**

| Decision | HTTP | CLI |
|---|---|---|
| `controller_only`, `adopted`, `forced_all` | 200, sequence runs | exit 0 (or 5 if a machine never went quiet) |
| `no_adopted_nodes` | 409 | exit 4 |
| `no_resolvable_nodes` | 409 | exit 4 |
| `TopologyError` | 503 | exit 4 |

---

## 3. `ShutdownOutcome` — what the sequence reports back

| Field | Type | Meaning |
|---|---|---|
| `completed` | `bool` | the sequence ran to the local power-off (or its dry-run equivalent) |
| `stuck_hosts` | `list[str]` | machines that never went quiet |
| `timed_out` | `bool` | the reachability budget expired |
| `shelly_armed` | `bool` | the mains-cut deadline was confirmed |

The CLI turns this into an exit code (§ contract); the daemon turns it into `/status` state.
Neither derives anything the sequence did not state.

---

## 4. `ShutdownLock` — one power-off at a time, per machine

| Property | Value |
|---|---|
| Path | `/run/cuems-power-bridge/shutdown.lock` |
| Mechanism | `flock`, exclusive, **non-blocking** |
| Directory | created by a tmpfiles rule this package ships: `d /run/cuems-power-bridge 0770 cuems cuems - -` |
| **Scope** | **the whole sequence, never a single stage** (research R2a) |
| Holders | the daemon; a manual CLI run; and — for the systemd transition — **the wrapper**, which holds it across both stage invocations and the gap between them |
| CLI opt-out | `--lock-held`, passed **only** by the wrapper, declaring that the caller already holds it. Explicit, because a CLI that inferred this from an inherited descriptor would run unlocked whenever the inference was wrong |
| Privilege | `0770 cuems cuems`: the daemon and root can take it; the operator account (`cuems-admin`, own primary group) cannot. **Manual runs therefore require `sudo`**, stated in `--help`, in the module docstring and in the README (research R2b) |
| On contention | refuse immediately, naming that a power-off is in progress. Never queue. |
| On permission error | exit 3 with "run this with sudo" — never proceed unlocked |
| On holder death | released by the kernel — no stale-lock handling, which is how lock files usually become their own outage |
| **Released before the local power-off** | the daemon drops the lock **explicitly** just before running its power-off command, because that command re-enters this same sequence through the system transition (research R2a-i). By then every irreversible step is done and stage 1 is idempotent, so the re-entry is a fast no-op rather than a refusal |
| Path is injectable | the lock location is a parameter defaulting to the runtime path, so the suite can point it at a temporary directory instead of depending on `/run` existing on the developer's machine (analysis H2) |

---

## 3a. Invocation — the tool is internal

The wrapper calls `"$venv_python" -m cuemspowerbridge.scripts.cluster_poweroff …`. This package
ships **no** `PATH` command for it (FR-007), which keeps the conffile's interpreter override
working, keeps the missing-package guard exactly as written, and leaves exactly one operator
command that powers the venue down. `cuems-power-bridge-config` does ship a shim: it is a
read-only query `cuems-displays-on` needs, and it powers nothing off.

**Flag names avoid `--force`**, which already means *run outside a poweroff transaction* in the
wrapper and *ignore a running project, include unadopted machines* on the HTTP route. The CLI
spells its own: `--include-unadopted`, `--refuse-if-running`, `--while-playing`, `--dry-run`,
`--lock-held`, `--stage`.

---

## 4a. SSH host-key store — one, shared

Both initiators — the daemon as `cuems`, the transition as root — use
**`/var/lib/cuems/.ssh/known_hosts`**, pinned explicitly rather than inherited from `HOME`
(research R10). `StrictHostKeyChecking=accept-new` is unchanged.

Two stores would mean a node re-imaged between a daemon shutdown and a transition shutdown is
trusted by one initiator and unknown to the other, discovered mid-shutdown. One store is also
one place to audit when a node is replaced.

---

## 4b. Stage gates — the sequence obeys the configuration it always has

| Stage | Gate | Behaviour when the gate is closed |
|---|---|---|
| displays | `cfg.projector_power_off_on_shutdown` | report "disabled — leaving displays alone" and return **success**; touch nothing |
| displays | `DisplayManager.configured` | report "no displays configured", return success |
| displays | every device answers `unknown` | report the fleet unreachable and skip the power-off, as today |
| nodes | the caller's `--force` / adoption filter | the six documented outcomes |

The first row is easy to lose in a relocation and expensive to lose in the field: venues keep
configured fleets dark on purpose, which is why the gate exists (inventory §2.1, §2.3).

---

## 5. Self-exclusion — moved into the sequence, and why it must stay

Three tests, in order, each removing this host from its own target list:

1. **uuid** — `NodeView.is_self`, resolved by the owning library from `settings.xml`;
   replaces the script's private `ElementTree` read (FR-017, FR-018).
2. **exact address membership** — the target resolves to an address this host holds.
   *Never a substring test*: `10.16.10.1` is a substring of `10.16.10.10`.
3. **name** — the target's first label or `role_id` matches this host's hostname or FQDN.

Tests 2 and 3 look redundant after test 1, and are not: they catch **a second entry
describing this host under a different uuid** — the shape `cuems-nodeconf`'s MAC-keyed merge
bug produced (FR-019). Keeping them is a judgement, recorded here so the next reader does not
tidy them away.

**This is the one behaviour this feature adds to the product path**: the daemon has never had
tests 2 and 3. It can only ever *remove* a target, and the target it removes is this host, so
the worst case it introduces is a controller that does not SSH itself — which is the correct
outcome and the reason the script had them.

---

## 6. Validation rules

| Rule | Source |
|---|---|
| The sequence is executed by exactly one implementation | FR-001, FR-003 |
| The decision is a pure function of `(selection, force)`, shared | FR-002 |
| The running-show guard is not reachable from the sequence | FR-015, SC-010 |
| The lock is taken by every entry point, non-blocking, auto-released | FR-014, SC-009 |
| Self-exclusion keeps all three tests, with the duplicate-entry reason recorded | FR-019 |
| Both reachability passes keep their distinct confirmation policies | FR-020 |
| Exit codes distinguish complete / precondition / refused / stuck | FR-009 |
| The lock covers a whole sequence; the wrapper holds it across both stages | FR-014, research R2a |
| A manual run without privilege fails legibly rather than running unlocked | FR-013, research R2b |
| Both callers emit the same progress events in the same order | FR-001, §1a |
