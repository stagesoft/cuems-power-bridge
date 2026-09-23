<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Implementation Plan: Cluster power-off CLI — one shutdown, two entry points

**Branch**: `002-cluster-poweroff-cli` | **Date**: 2026-09-23 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/002-cluster-poweroff-cli/spec.md`

## Summary

Extract the orderly power-off sequence out of `bridge.py` into its own module, give it an
explicit context instead of six attributes read off a `Bridge`, and let two callers run
it: the HTTP handler (which becomes a thin caller) and a new documented operator tool,
`cuems-power-bridge-cluster-poweroff`. The 221 lines of this repository's Python currently
embedded in `cuems-common`'s shell script are deleted there and replaced by invocations of
that tool, and a second small tool, `cuems-power-bridge-config`, removes the last import from
`cuems-displays-on`.

**The user's framing is the plan's governing constraint**: the systemd + Shelly path is the
product; the manual run is a maintenance, development, rehearsal and recovery tool. So the
two safeguards this feature adds are placed where the product path cannot execute them — the
running-show guard sits in the tool's entry point and is disabled by `--transition`, and the
one thing that does touch the shared sequence (the cross-process lock) is taken identically
by both callers, so it changes no decision.

This is a **relocation, a de-duplication and an extraction**, not a behaviour change. The one
deliberate exception is stated and bounded: the daemon inherits the script's address- and
name-based self-exclusion, which can only ever remove *this host* from its own target list.

## Technical Context

**Language/Version**: Python 3.11+, asyncio, single event loop in the daemon; the CLI runs
the same coroutines under `asyncio.run`

**Primary Dependencies**: unchanged — `cuemsutils` (>=0.1.0rc16,<0.1.1), `aiohttp`,
`websockets`, `python-osc`. The CLI adds no dependency; its `/status` probe uses the aiohttp
client already present.

**Storage**: none. One runtime lock file, `/run/cuems-power-bridge/shutdown.lock`.

**Testing**: `pytest` + `pytest-asyncio` + `pytest-mock` via the documented `uv` runner;
baseline **185 passed** (feature 001)

**Target Platform**: Debian 12 CUEMS controller. Two execution identities: the daemon as
`User=cuems`; the tool as **root** under `ExecStop` with `HOME=/root` for `known_hosts`. The
extracted sequence must assume neither.

**Project Type**: single Python package shipping a daemon plus operator CLIs

**Performance Goals**: not throughput-bound. The sequence's existing time budgets are
unchanged and remain enforced by the caller (the wrapper's per-stage `timeout`,
`TimeoutStopSec=200`).

**Constraints**: the product path's behaviour is frozen; the platform package must stay
functional with this package absent; the venv library surface stays intact until
`cuems-common` stops using it (release ordering, research R7)

**Scale/Scope**: 1–8 machine clusters. Moves ~140 lines within this package, adds two CLI
modules plus two shims, deletes 221 lines from the sibling.

## Constitution Check

*GATE: passed before Phase 0; re-checked after Phase 1 (below).*

| Principle | Bears on this feature | How it is satisfied | Verified by |
|---|---|---|---|
| **I — it cuts mains power; an empty step is not a success** | Yes, inherited. | The six outcomes and their refusals move wholesale into the shared decision; neither caller can reach the sequence without one. The CLI's exit codes distinguish *refused* (4) from *ran* (0/5) so a wrapper cannot mistake one for the other. | contracts/cli.md; quickstart §1 |
| **II — ordered, partially irreversible** | **Centrally.** | The sequence is extracted as a unit, in order, with its comments; the reachability poll and the Shelly pre-check keep their positions. The new lock makes "one sequence at a time" true across processes, closing a gap the in-process lock left. | data-model.md §4; quickstart §2, §3 |
| **III — it reads a schema it does not own** | Indirectly. | FR-017 deletes the script's private `settings.xml` read — the fifth copy of the node-identity model — in favour of the library's resolved value. | research R9 |
| **IV — designed degraded paths need real triggers** | Yes. | The running-show guard **self-disables by absence of the daemon**, which is exactly its trigger condition; `--transition` makes that explicit rather than implicit, so the degraded path is declared, not inferred. | research R3 |
| **V — hardware over the network is normal** | Yes. | No change to timeouts, retries or per-device isolation; both reachability passes move verbatim with their differing confirmation policies (FR-020). | research R9 |
| **VI — the shared venv makes packaging correctness** | Yes. | Two console entries, two shims, one tmpfiles rule; nothing new bundled. The `.deb` bundling gate from feature 001 still applies. | quickstart §6 |
| **VII — the external contract is the product** | **Centrally, and this feature discharges part of it.** | The frozen venv library surface exists because another package imports our modules. After this feature it imports nothing, and the contract becomes argv + exit codes — declared, versioned and probe-able. The HTTP surface is unchanged. | contracts/cli.md; research R7 |

**Gate result: PASS.** One change to the product path is deliberate and is recorded rather
than waved through: the daemon inherits address/name self-exclusion (data-model §5). It is
monotonic — it can only remove this host from its own target list — and it is the behaviour
the script has always had on that path.

**Post-design re-check: still PASS.** The design adds no bundled dependency, no event-loop
work in the daemon, and no change to an existing wire contract.

## Project Structure

### Documentation (this feature)

```text
specs/002-cluster-poweroff-cli/
├── plan.md              # This file
├── spec.md              # Feature specification (Q1-Q3 answered)
├── research.md          # Phase 0 — R1..R9
├── data-model.md        # Phase 1 — ShutdownContext/Decision/Outcome/Lock, self-exclusion
├── quickstart.md        # Phase 1 — how to prove it, product path first
├── contracts/
│   └── cli.md           # argv + exit codes: the contract that REPLACES the venv surface
├── checklists/
│   └── requirements.md  # spec quality checklist (all pass)
└── tasks.md             # /speckit-tasks output — NOT created here
```

### Source code

```text
src/cuemspowerbridge/
├── cluster_shutdown.py      # NEW: run_cluster_shutdown(ctx, decision, progress) — the
│                            #   sequence, extracted from bridge.py:508-644, plus the
│                            #   shared six-case decision and self-exclusion
├── shutdown_lock.py         # NEW: flock helper over /run/cuems-power-bridge/shutdown.lock
├── bridge.py                # handle_shutdown becomes a THIN caller: token, engine guard,
│                            #   lock, topology read, decision, run, map outcome to HTTP
├── scripts/
│   ├── cluster_poweroff.py  # NEW CLI: stages, exit codes, the /status running-show probe
│   └── config_get.py        # NEW CLI: --get <key>, for cuems-displays-on
└── data/bin/…               # shims (see below)

data/bin/
├── cuems-power-bridge-cluster-poweroff   # NEW sh shim -> venv entry
└── cuems-power-bridge-config             # NEW sh shim -> venv entry

debian/
├── cuems-power-bridge.install            # + the two shims, + the tmpfiles rule
└── tmpfiles.d/cuems-power-bridge.conf    # NEW: d /run/cuems-power-bridge 0770 cuems cuems

tests/
├── test_cluster_shutdown.py   # NEW: the sequence, in isolation
├── test_cli_poweroff.py       # NEW: stages, exit codes, the guard, --transition
├── test_shutdown_equivalence.py # NEW: both entry points, same decisions, per fixture
└── test_shutdown_lock.py      # NEW: contention, non-blocking, death-releases

../cuems-common/usr/bin/cuems-cluster-poweroff   # heredocs DELETED, calls the tool
../cuems-common/usr/bin/cuems-displays-on        # two one-liners -> config query
```

**Structure Decision**: a new module rather than a helper inside `bridge.py`. The daemon
keeps its state machine and `/status`; the sequence keeps the steps. That split is what makes
the CLI a peer rather than a second implementation, and it is the "proper end product" the
feature was asked for.

## Implementation phases

**Phase A — the lock.** `shutdown_lock.py` plus its tests, and the tmpfiles rule. First
because both later phases take it, and because it is independently verifiable.

**Phase B — the extraction.** Move `bridge.py:508-644` into `cluster_shutdown.py` behind
`ShutdownContext`/`ShutdownOutcome`; lift the six-case decision beside it; add
self-exclusion (data-model §5). `handle_shutdown` becomes a thin caller. **Reviewed as a
move**: the steps, their order and their log lines must be diff-able against the original.

**Phase C — the tools.** `scripts/cluster_poweroff.py` (stages, exit codes, the `/status`
guard, `--transition`) and `scripts/config_get.py`, each with the repository's standard
docstring-and-`main()` shape, console entries, shims and install lines.

**Phase D — equivalence tests.** Both entry points over every fixture, decisions compared
field by field; plus the call-graph assertion that the guard is unreachable from the
sequence.

**Phase E — the sibling.** Delete both heredocs; the wrapper calls the tools and keeps its
own responsibilities (research R8); `cuems-displays-on` uses the config query. `Breaks:
cuems-power-bridge (<< 0.3.2-1)` in `cuems-common`.

**Phase F — packaging, docs, gates.** Release `0.3.2-1`; README and CLAUDE.md gain the tool;
`.deb` contents checked; the staged upgrade rehearsed in the documented order.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The extraction silently changes the product path | Phase B is reviewed as a move, not a rewrite; quickstart §2 checks it by test, by call graph and by diff |
| The new lock deadlocks or blocks a poweroff | Non-blocking acquisition only; kernel-released on death; a losing caller refuses rather than waits |
| The guard leaks onto the transition path | It lives in the CLI entry point and is suppressed by `--transition`; a test asserts the sequence module never references the engine or `/status` |
| A half-upgraded pair breaks a poweroff | Release order is bridge-then-common (research R7); the venv library surface is kept intact until common stops using it, and `Breaks:` prevents the reverse |
| `/run/cuems-power-bridge` missing on first install | tmpfiles rule plus `systemd-tmpfiles --create` in `postinst`; the lock helper reports a clear precondition failure (exit 3) rather than proceeding unlocked |
| Inherited self-exclusion changes daemon behaviour | Monotonic by construction (removes only this host); called out in the Constitution Check and covered by a test |

## Sequencing

- Independent of the NNG-native shutdown migration, which this feature **unblocks** by making
  `_shutdown_nodes()` a one-place change.
- Independent of the systemd-unit relegation: units, conffile and sudoers stay put.
- **Releases in two steps**, bridge first — unlike feature 001, this can be staged, and the
  plan depends on that ordering being honoured.
