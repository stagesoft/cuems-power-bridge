<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Phase 0 research — cluster power-off CLI

**Feature**: `002-cluster-poweroff-cli` · **Date**: 2026-09-23
**Method**: measured against the live trees — `cuems-power-bridge` @ `b807e53`
(`002-cluster-poweroff-cli`), `cuems-common` @ `df7e354` (`feat/xml-refactor`, pushed).

The user's framing governs every decision below: **the systemd + Shelly path is the
product.** The CLI's manual mode is a maintenance, development, rehearsal and recovery tool.
Where the two disagree, the product wins, and anything added for the manual mode must be
*provably absent* from the transition path.

---

## R1. Where the shared sequence lives — **the central decision**

**Measured.** `bridge.py:508-644` is `_run_shutdown(selection)`: steps 5 through 10 of the
sequence (SSH fan-out, display power-off in parallel, reachability poll, Shelly pre-check and
arm, local poweroff). It is a method on `Bridge` and reads six collaborators off `self`:
`cfg`, `displays`, `shelly`, `engine`, plus the state setters `_set_state` and
`_nodes_pending`. `handle_shutdown` (`:348-470` after feature 001) holds the token check, the
lock, the running-project guard, the topology read and the six-case decision.

**Decision: extract the sequence into a new module, `src/cuemspowerbridge/cluster_shutdown.py`,
as a plain async function over an explicit context — and make `handle_shutdown` a thin
caller.** The user asked for a *proper end product*, not a shim: the CLI must not be a
parallel path that happens to call the same selection function.

```
run_cluster_shutdown(ctx, decision, *, progress) -> ShutdownOutcome
```

where `ctx` carries `cfg`, `displays`, `shelly` and nothing else the sequence does not use;
`decision` is the six-case verdict (§R1a — the `Selection` is the decision's *input*, not the
sequence's); and `progress` is a callback the caller supplies to receive state transitions.

**Rationale.**
- The sequence becomes testable without an HTTP request and without a `Bridge`.
- `Bridge._set_state` stays in `Bridge`: the daemon has a state machine and a `/status`
  surface; the CLI has stdout and an exit code. The sequence itself reports through
  `progress` and does not know which it is feeding.
- It removes the last excuse for two implementations. After this, "the wall switch and the
  API cannot disagree" is a property of the call graph, not of review discipline.

**What does NOT move**: the six-case decision (it needs `force`, and the CLI's own flag maps
onto it), the token check, the engine guard, the auto-load loop, and the `/status` payload.
The decision function moves *beside* the sequence so both callers share it, but it stays a
pure function of `(selection, force)`.

**Alternatives considered.** (a) CLI shells out to the daemon's HTTP API — impossible: the
unit orders the poweroff hook to run *after* the bridge has stopped, deliberately, so no API
exists at that moment. (b) CLI imports `Bridge` and calls `_run_shutdown` — keeps a private
method as a cross-process contract and drags the WebSocket clients into a process that needs
none of them. (c) Leave the sequence in `bridge.py` and have the CLI import the method —
rejected for the same reason, and it would make the daemon a dependency of the tool.

---

## R2. The cross-process lock (spec Q1)

**Measured.** `bridge.py:103` `self._shutdown_lock = asyncio.Lock()` — in-process only.
`cuems-common` ships no tmpfiles rule for `/run/cuems` (`etc/tmpfiles.d/` holds
`cuems-tmp.conf` for `/tmp/cuems`, plus journal and jack rules); `/run/cuems` is created
ad hoc by the helpers that write into it. `/run` itself is root-owned `0755`, so the daemon
(running as `cuems`) **cannot** create a directory there.

**Decision: `flock` on `/run/cuems-power-bridge/shutdown.lock`, in this package's own runtime
namespace, created by a tmpfiles rule this package ships.**

```
# /usr/lib/tmpfiles.d/cuems-power-bridge.conf
d /run/cuems-power-bridge 0770 cuems cuems - -
```

- `flock` is released by the kernel when the holder dies, so a killed daemon or an
  interrupted CLI leaves nothing to clean up — no stale-lock heuristics, which is the usual
  way lock files become their own outage.
- Own namespace (`/run/cuems-power-bridge`, not `/run/cuems`) avoids arguing with
  `cuems-common` over a shared directory and needs no coordination to land.
- `0770 cuems cuems` lets the daemon take it; root ignores the mode.
- `postinst` runs `systemd-tmpfiles --create` so the directory exists before the next boot.

**Semantics**: acquisition is **non-blocking**. A second attempt refuses immediately and says
which entry point holds it; it never queues, because a queued power-off is a power-off that
happens at an unpredictable moment.

**The daemon takes the same lock**, in addition to its asyncio lock (which still gives the
cheaper in-process rejection and the existing `shutdown_already_in_progress` response).

### R2a. The lock spans the SEQUENCE, not a stage — corrected 2026-09-23 (analysis C1)

An earlier draft had each CLI invocation take and release the lock. The wrapper runs the two
stages as **two processes** (`--stage displays`, then `--stage nodes`), so mutual exclusion
would lapse in the gap between them: the daemon or a second run could acquire the lock and
begin a shutdown *mid-transition*. That is the same class of defect this feature exists to
remove — a guarantee that holds in each part and not across the whole, on the product path.

**Decision (user): the wrapper holds the lock for the whole transition.**

```sh
exec 9>/run/cuems-power-bridge/shutdown.lock
flock -n 9 || { log "another power-off is already in progress"; exit 4; }
# ... stage 1 ... stage 2 ...   (fd 9 stays open; the kernel releases it when the wrapper exits)
```

The CLI is then told not to acquire it: **`--lock-held`** declares "my caller holds the
sequence lock". The flag exists so the skip is *explicit* — a CLI that guessed from an
inherited file descriptor would silently run unlocked when the guess was wrong.

| Caller | Who holds the lock | For how long |
|---|---|---|
| systemd transition (wrapper) | the **wrapper**, fd 9 | both stages, plus the gap between them |
| manual `--stage all` | the CLI | the whole sequence |
| manual single stage | the CLI | that stage |
| daemon `POST /shutdown` | the daemon | the whole sequence |

**Using `--lock-held` without actually holding it is a caller bug**, and the only caller that
may pass it is the wrapper. The CLI states as much in its help.

### R2b. Who may run it by hand — `sudo` (analysis H1)

The lock lives at `0770 cuems cuems`. The operator account `cuems-admin` gets its **own**
primary group (`cuems-common/debian/postinst:429-433`), so it is not in group `cuems` and
cannot open that file. **Decision (user): manual runs require `sudo`**, and the tool says so —
in its `--help`, in its module docstring, and in the README.

The lock's mode stays as it is: widening it to make an unprivileged run work would hand the
ability to block a cluster power-off to any account that can open the file. The failure is
instead made legible — a permission error on the lock exits 3 with "run this with sudo",
never proceeding unlocked.

---

## R3. The running-show guard, and where it may not be (spec Q2)

**Decision: the guard lives in the CLI's entry point, implemented as a `GET /status` probe of
the running daemon, and never in `cluster_shutdown.py`.**

| Invocation | Daemon | Guard | Behaviour |
|---|---|---|---|
| manual, nothing playing | up | probe answers "not playing" | proceeds, no flag needed |
| manual, project playing | up | probe answers "playing" | **refuses**, exit 4, unless `--force` |
| manual, daemon down | down | probe fails | proceeds (no guard possible) |
| **systemd transition** | already stopped by unit ordering | probe never runs | **identical to today** |

**Rationale.** The probe needs no engine connection, no new dependency and no state: the
daemon already publishes `engine_state` on `/status`. And it disables itself exactly where it
cannot apply, because the unit stops the bridge *before* the hook runs — which is why the
heredoc never asked either.

**Constitutional check.** Principle VII freezes the product path's behaviour. The guard is in
`scripts/cluster_poweroff.py`, executed before `run_cluster_shutdown` is called at all, so
the transition path does not execute the guard's code — demonstrable by reading the call
graph, which is what SC-010 asks for.

**Rejected**: putting the check inside the sequence (the product path would then run code
that exists only for a maintenance convenience — the user's objection, and it is correct);
opening an engine WebSocket from the CLI (a new dependency for the tool, useless under
ExecStop).

---

## R4. Stage decomposition and the exit-code contract

**Measured.** The wrapper bounds each stage separately: `timeout "$projector_timeout_s"` for
displays, `timeout "$((node_wait_s + 30))"` for nodes, and it decides between them
(`nodes_off=false` runs displays only).

**Decision: one tool, two stages, selected by `--stage {displays,nodes,all}`.** The wrapper
keeps calling them separately, so its per-stage time limits keep working unchanged; `all` is
for operators and for the daemon-free rehearsal.

**Exit codes** (the contract the wrapper reads):

| Code | Meaning | Example |
|---|---|---|
| 0 | complete, including a stated "nothing to do" | controller-only cluster; no displays configured |
| 1 | usage error | argparse |
| 3 | configuration/precondition failure | `config.load()` raised; config-dir mismatch |
| 4 | **refused** | topology unreadable; nothing adopted; nothing addressable; a project is playing (manual); another power-off holds the lock |
| 5 | proceeded, but some machine never went quiet | reachability timeout |

`0` and `5` both mean "the sequence ran"; `3` and `4` mean "it did not". The wrapper treats
`4` as an error to log loudly and `5` as a warning, and neither stops the poweroff
transaction — which is correct, because by then the machine is going down regardless.

**stdout stays line-oriented** and unbuffered (`-u` equivalent), because the wrapper pipes
each line through its `log` function.

---

## R5. The config query for `cuems-displays-on` (spec Q3)

**Measured.** The script needs exactly two values —
`config.load().projector_power_on_on_start` and `.shared_token` — and otherwise uses
`curl`/`jq` against `:8478`.

**Decision: a second small tool, `cuems-power-bridge-config --get <key>`**, printing one bare
value to stdout and exiting non-zero for an unknown key. It is the documented query the spec
requires, it keeps the polling loop and its retry knobs next to the conffile that tunes them,
and it removes the last Python import from `cuems-common`'s scripts.

**Secret handling**: `shared_token` is printed to stdout only. The script already feeds it to
`curl` via stdin (`-K -`) precisely to keep it out of `ps`; the query must not weaken that,
so its own invocation takes the key name as the argument, never the value.

---

## R6. Logging, and not flooding a poweroff journal

**Decision.** The sequence logs through the standard `logging` module; the CLI installs a
plain stdout handler at INFO (`-v` for DEBUG), no syslog transport — the wrapper already puts
every line into the journal with its own prefix, and the daemon keeps its existing setup.
The `cuemsutils` logger bound added by feature 001 (`network_map.py`) applies to both callers
because it is set at import.

---

## R7. Release model — one coordinated candidate, not a staged rollout

**Corrected 2026-09-23 (user).** An earlier draft of this research proposed shipping
`cuems-power-bridge` 0.3.2-1 first and `cuems-common` 1.3.0-24 second, so the pair could be
adopted gradually. **That is not how this ecosystem lands the xml refactor.** Every
repository's half lands together under the coordinated tag **`xml-refactor-merge-candidate`**,
each keeping the version it already holds:

| Repository | Version | Tag |
|---|---|---|
| `cuems-power-bridge` | **0.3.1-1** (open; features 001 and 002 both land in it) | tag created at the merge point |
| `cuems-common` | **1.3.0-23** (`UNRELEASED`; both halves land in the same entry) | existing tag **relocated** — it currently sits at `f2fc0f5`, behind feature 001's half |
| `cuems-nodeconf` | 0.1.0-8 (`UNRELEASED`) | tag at `6c0cca7` — the precedent this follows |
| `cuems-utils` | 0.1.0rc16 | its own half |

**Consequences for this feature:**

- **No new version.** The CLI, the extraction and the sibling's heredoc deletion all land in
  `0.3.1-1` / `1.3.0-23`, beside feature 001.
- **No `Breaks: cuems-power-bridge (<< 0.3.2-1)`.** Feature 001's existing reciprocal pair
  (`cuems-common (<< 1.3.0-23)` ↔ `cuems-power-bridge (<< 0.3.1-1)`) already forces the two to
  move together, and that is now the mechanism for both features at once.
- **Mixed-version tolerance stops being a requirement.** The candidate is validated as a set,
  on real machines, before anything is released. What replaces "it must survive a half
  upgrade" is "the whole candidate is verified together" (R7a).
- The venv library surface may therefore be **removed** in the same change rather than kept
  alive for a transitional release — once `cuems-common` stops importing it, nothing does.

## R7a. Consumer-state validation lands on a hardware checklist

**Decision (user).** Everything that needs real hardware, a real cluster or a human goes on a
**hardware-verification ledger** in this repository, following `cuems-nodeconf`'s and
`cuems-common`'s existing strategies, so all three can be executed together on production
machines as one coordinated validation of the candidate.

**The shape, taken from the siblings** (measured):

- `cuems-nodeconf`: `specs/<feature>/checklists/hardware-verification.md` — *one ledger for
  BOTH its features*, every box unchecked until performed, each entry stating **Do / Proves /
  Why the suite cannot**, and deferrals recorded explicitly rather than implied. Its companion
  `evidence/verification-record.md` records what *was* done.
- `cuems-common`: `docs/upgrade-verification.md` — operator-facing, ordered by upgrade step,
  ending in a **copy-once-per-host record sheet**.

**This repository adopts both halves**: a ledger under the feature that carries the debt
(covering features 001 **and** 002, because 001's hardware items are still open), plus a
per-host record sheet so a technician can work a production controller without reading either
feature's prose.

**Why a ledger rather than prose**: the constitution accepts "verified" and "not verified"; it
does not accept silence. Feature 001 ended with six such items scattered across a task list
and an evidence README — countable only by reading both.

## R8. What the wrapper keeps

Explicitly **not** moved, because they are the platform package's own concerns:

- the poweroff-vs-reboot check (`systemctl list-jobs`) — it decides whether the hook should
  act at all;
- `enabled=` in `cluster-poweroff.conf` — the single kill switch, shipped `false`;
- the per-stage `timeout` bounds and `TimeoutStopSec=200`;
- the ordering probe;
- the "no bridge installed → log one ERROR, exit 0" probe, which now tests for the **tool**
  (`command -v cuems-power-bridge-cluster-poweroff`) rather than the venv interpreter.

---

## R9. The four carried-over defects (spec FR-017..FR-020)

| # | Today | After |
|---|---|---|
| FR-017 | `own_uuid()` parses `settings.xml` with `ElementTree` — the fifth copy of the node-identity model | deleted; the self entry comes from `NodeView.is_self`, which the library already resolves |
| FR-018 | that read swallows every exception and returns `None`, silently disabling uuid self-exclusion | gone with it; a failed read is already a classified `TopologyError` (Case 5) |
| FR-019 | address/hostname self-exclusion, now largely redundant | **kept**, with its reason recorded: it catches a *second entry describing this host under a different uuid* — the shape `cuems-nodeconf`'s MAC-keyed merge bug produced |
| FR-020 | two reachability passes with different `confirm_failures` | moved verbatim, comments included; the one-pass probe stays `max_wait_s=0, confirm_failures=1` |

FR-019's logic moves into the sequence (both callers benefit: the daemon has never had it).
That is a **behaviour addition on the product path** and must be called out: it can only ever
*remove* a target, never add one, and the removal it makes is a host excluding itself.

---

## Resolved unknowns

| Unknown | Resolution |
|---|---|
| Where the shared sequence lives | R1 — new `cluster_shutdown.py`, explicit context, `handle_shutdown` becomes a thin caller |
| How two processes exclude each other | R2 — `flock` in this package's own `/run` namespace, tmpfiles-created, non-blocking |
| Where the running-show guard may live | R3 — CLI entry point only, via `/status`; unreachable from the transition path |
| Stage split and exit codes | R4 |
| How `cuems-displays-on` stops importing us | R5 — `cuems-power-bridge-config --get` |
| Release model | R7 — one coordinated `xml-refactor-merge-candidate` tag, current versions, no staged rollout |
| Where consumer-state validation lands | R7a — a hardware-verification ledger + record sheet, following cuems-nodeconf and cuems-common |
| Fate of the frozen library surface | R7 — removed in the same change; the CLI contract replaces it |
