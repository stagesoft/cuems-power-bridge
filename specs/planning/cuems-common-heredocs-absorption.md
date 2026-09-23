<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Absorbing `cuems-cluster-poweroff`'s Python into this repository

**Status**: analysis, no change made. The concrete **first step** of the deferred split
recorded in [`cuems-common-machinery-to-absorb.md`](./cuems-common-machinery-to-absorb.md)
— it moves the *code*, and deliberately leaves the unit, the conffile and the bash wrapper
where they are.
**Measured**: 2026-09-23, against `cuems-power-bridge` @ feature 001 merged
(`b125b59`) and `cuems-common` @ `feat/xml-refactor` (`df7e354`, carrying feature 001's
other half).

**Companion documents**, beside this file:
- `cuems-common-machinery-to-absorb.md` — the full inventory (scripts, units, conffile,
  sudoers) and what stays in `cuems-common` regardless.
- `cuems-utils-xml-refactor-consumer-migration.md`, `cuems-power-bridge-node-role-findings.md`
  — the migration that produced the defect this document's subject carried for months.

---

## 1. What is actually over there, counted

`../cuems-common/usr/bin/cuems-cluster-poweroff`, 381 lines:

| Part | Lines | What it is |
|---|---|---|
| bash wrapper | 156 | config sourcing, the `enabled=` gate, the poweroff-vs-reboot transaction check, the ordering probe, `timeout` budgets, the venv-interpreter probe, log plumbing |
| **Python heredoc, stage 1** (`:126-176`) | 49 | displays off — `config.load()`, `DisplayManager.from_config/.status_all/.power_off_all/.snapshot` |
| **Python heredoc, stage 2** (`:204-377`) | 172 | nodes off — local address enumeration, `own_uuid()`, `resolve()`, self-exclusion, **selection via this repository's `shutdown_targets()`** (feature 001), `SshTarget`/`poweroff_all`, two `reachability.wait_until_all_down` passes |

**~58% of the tool is this repository's code**, executed by this repository's interpreter
(`/usr/lib/cuems/bin/python3`), maintained in a repository whose tests never run it.

After feature 001 the *selection* half is already ours: the heredoc calls
`network_map.load_nodes()` and `network_map.shutdown_targets()` instead of filtering on a
string. What remains local to the heredoc is the surrounding orchestration — and that is
the part still carrying duplicated logic (§4).

---

## 2. Validity — why this move is right

**2.1 It is our code without our guarantees.** The heredocs import six of our modules.
Living in `cuems-common` they get no type checking, no test suite, no review by the people
who own the semantics, and no version lockstep with the library they call. Feature 001
needed a `Breaks:` pair in both directions *because* a Python API crosses a package
boundary. Moving the code turns that into a **CLI contract** — argv plus exit codes — which
is cheap to keep compatible across versions and can be probed (`command -v`) instead of
discovered as an `AttributeError` part-way through a poweroff.

**2.2 It is the largest untested surface on the mains-cut path.** 221 lines that execute
only during a poweroff transition on a real controller. Neither repository's suite touches
them. The constitution requires the mains-cut path to be bounded and independently
verifiable (Principles I and II); this is the one stretch of it that is neither.

**2.3 The failure mode is proven, not theoretical.** The defect feature 001 fixed lived in
that heredoc at `:275` for months, invisible to both suites, and would have powered off a
controller while its nodes ran. The same blind spot still covers self-exclusion, address
enumeration and the two reachability passes.

**2.4 One implementation, or none.** Today `POST /shutdown` and the `ExecStop` hook share
*selection* and nothing else. Self-exclusion, the "already down" fast path and the
degraded-timeout policy exist once in each. They agree today because one person wrote both
this week; that is discipline, not construction.

**2.5 The NNG migration wants one target.** Replacing the SSH fan-out with an engine-native
broadcast rewrites `_shutdown_nodes()`. With the heredoc in place it rewrites two things in
two repositories, in lockstep. Absorbed first, it rewrites one.

---

## 3. Limitations — what the move does *not* buy

| Limitation | Detail |
|---|---|
| **The package boundary remains** | The unit, `cluster-poweroff.conf` and the bash wrapper stay in `cuems-common` (that split is deferred). The call becomes `exec` + exit codes instead of an import — better, not gone. |
| **Two config files remain** | `cluster-poweroff.conf` is shell-sourced (`enabled`, `nodes_off`, `node_wait_s`, `projector_timeout_s`); `power-bridge.conf` is Python-parsed. The CLI must accept the first as arguments rather than read it. |
| **The bash wrapper earns its keep** | It distinguishes a poweroff transaction from a reboot (`systemctl list-jobs`), carries the `enabled=` kill switch, applies `timeout` hard bounds, probes for the venv interpreter, and logs one ERROR and exits 0 when this package is absent. None of that should move: it is what keeps `cuems-common` functional on a host with no bridge. |
| **A new versioned edge** | `cuems-common` would call an entry point that exists only from 0.3.1-1. Milder than today's edge (a `command -v` probe degrades gracefully) but still an edge. |
| **Identity differs from the daemon's** | The hook runs as **root** with `HOME=/root` for `known_hosts`; the daemon runs as `cuems`. Shared code must not assume either, and the two `known_hosts` stores stay separate unless that is deliberately changed. |
| **No net code reduction** | ~220 lines relocate and an entry point is added. The gain is testability and ownership, not size. |

---

## 4. Findings in the current code, to fix *during* the move

1. **`own_uuid()` (`:238-247`) still parses `settings.xml` with `ElementTree`** — a private
   read of a cuemsutils-owned document, one file away from the private reader feature 001
   deleted for exactly that reason. `ConfigManager.node_uuid` already provides it, and the
   adapter already computes `NodeView.is_self` from it. This is the fifth copy of the
   node-identity model in the ecosystem.
2. **The address/hostname self-exclusion pass is now nearly dead — but keep it.** The
   adapter excludes `is_self` by uuid, and a map lacking this host's uuid raises
   (`self_entry_missing`), so the uuid branch is redundant. What the address and hostname
   branches still catch is a **second entry describing this host under a different uuid** —
   the duplicate-node shape `cuems-nodeconf`'s MAC-keyed merge bug used to produce. Preserve
   it, with that reason written down, rather than tidying it away.
3. **`own_uuid()` swallows every exception and returns `None`** (findings §5). Under the
   library path the same information arrives typed and loud; the swallow should not survive
   the move.
4. **Two reachability passes with different policies** (`confirm_failures=1` for the
   liveness probe, the default for the real wait) are deliberate and subtle. They must move
   verbatim, with their comments, or the "already down" fast path silently changes cost.

---

## 5. Recommended shape

A console entry point in this package, invoked by a thinned bash wrapper:

```
cuems-power-bridge-cluster-poweroff --stage displays --timeout 45
cuems-power-bridge-cluster-poweroff --stage nodes --wait 120 [--force] [--dry-run]
```

**Exit codes are the contract** (the unit already tolerates non-zero):

| Code | Meaning |
|---|---|
| 0 | stage complete (including "nothing to do", stated in the output) |
| 3 | configuration or precondition failure (`config.load()` raised, no displays configured and none expected) |
| 4 | **refused** — topology unreadable, nothing adopted, nothing addressable |
| 5 | proceeded but timed out with hosts still up |

**Rules for the move:**

- stdout stays line-oriented; the wrapper keeps piping it through `log`;
- the CLI reads `power-bridge.conf` itself and takes the shell-config values (`node_wait_s`,
  `projector_timeout_s`, `--force`) as **arguments**, so the two config files stay separate
  until the conffile itself moves;
- the bash wrapper keeps the transaction check, `enabled=`, the interpreter probe and
  `timeout`;
- `cuems-displays-on`'s two one-liners (`config.load().projector_power_on_on_start`,
  `.shared_token`) move the same way or become a `--print-config` query, so no shell script
  imports our modules any more.

**The deeper version, worth deciding early:** make the CLI and `handle_shutdown` both call
one `run_cluster_shutdown(selection, …)`, rather than making the CLI a parallel path that
happens to use the same selection function. That is the version in which "the wall switch
and the HTTP API cannot disagree" is true **by construction** instead of by discipline. It
is more work, and it is the reason to do this before the NNG migration rather than after.

---

## 6. Sequencing

- **After** feature 001 merges (done: `b125b59`). Folding a relocation into a safety fix
  would have widened a cutover that currently has a clean `Breaks:` pair.
- **Before** the NNG-native shutdown migration, so `_shutdown_nodes()` and the hook are
  rewritten once.
- **Independently of** the unit/conffile/sudoers split, which stays deferred to the
  ecosystem-wide systemd relegation. This move needs none of it.
- Packaging: the new entry point ships in this package; `cuems-common`'s wrapper probes for
  it and falls back to its current behaviour for one release, so the two halves need not be
  simultaneous — unlike feature 001, this move **can** be staged.

## 7. Exit criteria for the future feature

The heredocs are gone from `cuems-common`; both stages are console entry points here with
unit tests covering the five shutdown cases through the CLI as well as through HTTP; the
self-exclusion duplication is resolved with the duplicate-entry case preserved and
explained; `own_uuid()`'s private `settings.xml` read is deleted; the two reachability
policies are intact and covered; the bash wrapper still logs one ERROR and exits 0 on a host
with no bridge installed; and `cuems-displays-on` no longer imports this package's modules.
