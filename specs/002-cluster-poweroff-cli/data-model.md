<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Phase 1 data model — cluster power-off CLI

**Feature**: `002-cluster-poweroff-cli` · **Date**: 2026-09-23

No persistent data. What this feature models is **the sequence itself**, lifted out of the
daemon so two callers can run it, plus the small values that cross the new boundary.

---

## 1. `ShutdownContext` — what the sequence needs, and nothing more

The sequence today reads six things off `Bridge`. Extracted, it takes them explicitly, which
is what makes it testable and what stops the CLI from dragging the daemon's WebSocket clients
into a process that has no use for them.

| Field | Type | Why the sequence needs it |
|---|---|---|
| `cfg` | `Config` | ssh user/key, poweroff commands, timeouts, Shelly timer, `dry_run` |
| `displays` | `DisplayManager` | stage 1, and the parallel power-off in stage 2 |
| `shelly` | `ShellyClient` | the pre-check and the mains-cut arming |
| `progress` | `Callable[[str, dict], None]` | state transitions reported OUT; the daemon maps them to `_set_state` and `/status`, the CLI to stdout lines |

**Not in the context, deliberately**: `engine` (the sequence never consults it — the running
-show guard is the caller's business, R3), `editor`, the auto-load state, the HTTP app.

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
| Holders | the daemon (in addition to its in-process lock) and every CLI power-off run |
| On contention | refuse immediately, naming that a power-off is in progress. Never queue. |
| On holder death | released by the kernel — no stale-lock handling, which is how lock files usually become their own outage |

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
