<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Contract — the command-line surface

**Feature**: `002-cluster-poweroff-cli` · **Status**: new. This contract **replaces** the
undeclared venv library surface that `cuems-common` drives today (feature 001,
`contracts/venv-library-surface.md`). Once both halves land in the coordinated
`xml-refactor-merge-candidate` (this repository at `0.3.1-1`, `cuems-common` at `1.3.0-23`),
argv and exit codes are the whole boundary between the two packages.

Both tools follow this repository's existing CLI shape: implementation in
`src/cuemspowerbridge/scripts/<name>.py` with a usage + exit-status docstring, a
`[tool.poetry.scripts]` entry, a POSIX-sh shim in `data/bin/<name>` that `exec`s the venv
entry, and a `debian/cuems-power-bridge.install` line putting the shim on `PATH`.

---

## `cuems-power-bridge-cluster-poweroff`

```
sudo cuems-power-bridge-cluster-poweroff --stage {displays|nodes|all}
         [--node-wait S] [--projector-timeout S]
         [--force] [--dry-run] [--transition] [--lock-held] [-v]
```

**Manual runs require `sudo`.** The sequence lock is `0770 cuems cuems`, and the operator
account has its own primary group, so an unprivileged run cannot take it — and a power-off
that cannot take the lock must fail, not proceed unlocked. Without privilege the tool exits 3
saying exactly that. The help output and the module docstring say it too.

| Option | Meaning |
|---|---|
| `--stage` | which half of the sequence to run. The wrapper calls `displays` and `nodes` separately so its own per-stage time limits keep applying; `all` is for operators and rehearsals. With `all`, the two stages run in order and each honours its own bound — `--projector-timeout` for the display stage, `--node-wait` for the machine stage — and the run takes the sequence lock once for both. |
| `--node-wait` | seconds to wait for machines to go quiet. Supplied by the caller (it lives in the platform package's config file, which is not moving). |
| `--projector-timeout` | bound for the display stage, same reason. |
| `--force` | power off every machine in the map, adopted or not, **and** override the running-show refusal. Same meaning as the API's `force=1`. |
| `--dry-run` | run every branch, change nothing. Independent of the config file's own `dry_run`. |
| `--transition` | declare that this run is the system's own power-off transition. **Suppresses the running-show probe** (there is no daemon to ask). The wrapper passes it; an operator does not. |
| `--lock-held` | declare that the **caller** holds the sequence lock, so this invocation must not take it. Passed **only** by the wrapper, which holds one lock across both stages (see **Lock**). Passing it without holding the lock is a caller bug, and the tool says so in its help. |
| `-v` | DEBUG on stdout. |

### Exit codes — the contract the wrapper reads

| Code | Meaning | Wrapper's treatment |
|---|---|---|
| 0 | complete, including a stated "nothing to do" | info |
| 1 | usage error | error |
| 3 | configuration or precondition failure | error |
| 4 | **refused**: topology unreadable · nothing adopted · nothing addressable · a project is playing (manual only) · another power-off holds the lock | error, loud |
| 5 | ran, but some machine never went quiet | warning |

`0` and `5` mean the sequence ran; `3` and `4` mean it did not. Neither `4` nor `5` aborts
the poweroff transaction — by then the machine is going down regardless, and the wrapper's
job is to make the reason legible afterwards.

### Configuration gates the tool still obeys

`--stage displays` honours `projector_power_off_on_shutdown` from `power-bridge.conf`: when
it is false the tool prints "projector_power_off_on_shutdown=false — leaving displays alone"
and exits **0**. Venues keep configured fleets dark deliberately, and a relocation that
dropped this gate would command them off at every shutdown.

### Output

Line-oriented, unbuffered, on stdout; the wrapper forwards each line to the journal. Every
line names what happened, and refusals name the remedy — the operator reading this is reading
the journal of a machine that then powered off.

### The six outcomes

Identical to the API's, by construction: both callers run the same decision and the same
sequence (see [data-model.md](../data-model.md) §2). `--force` maps to the API's `force=1`;
`--transition` has no API equivalent because the API is unreachable at that moment.

---

## `cuems-power-bridge-config`

```
cuems-power-bridge-config --get <key> [--config PATH]
```

Prints one bare value to stdout and exits 0; exits 3 for an unknown key or an unreadable
configuration file. **A key that is set to an empty value prints an empty line and exits 0** —
that is a legitimate configuration, and `cuems-displays-on` branches on the empty string to
decide whether to send an auth header at all. Only "no such key" and "cannot read the config"
are errors. It exists so `cuems-displays-on` can stop importing this package's
modules for the two values it needs (`projector_power_on_on_start`, `shared_token`).

**Secrets**: the value is printed to stdout only, and the *key* is the argument — a token
never appears in `argv`, preserving the reason `cuems-displays-on` already feeds it to `curl`
through stdin.

---

## Lock — one per SEQUENCE, not per stage

A non-blocking exclusive `flock` on `/run/cuems-power-bridge/shutdown.lock`. The loser refuses
(exit 4, or HTTP 409 `shutdown_already_in_progress`) and never queues. The kernel releases it
when the holder dies, so an interrupted run blocks nothing.

**Who holds it, and for how long:**

| Caller | Holder | Span |
|---|---|---|
| systemd transition | the **wrapper** (`exec 9>…; flock -n 9`) | both stage invocations *and the gap between them* |
| manual `--stage all` | the CLI | the whole sequence |
| manual single stage | the CLI | that stage |
| `POST /shutdown` | the daemon | the whole sequence |

The wrapper's span is the point: with a lock per invocation, the two stages would leave a gap
in which another power-off could start mid-transition — the same "true in each part, false
across the whole" defect this feature exists to remove. The wrapper therefore passes
`--lock-held` to both stages.

**Permission**: `0770 cuems cuems`. A caller that cannot open it exits **3** with "run this
with sudo"; it never proceeds unlocked. Widening the mode is rejected deliberately — it would
let any account that can open the file block a cluster power-off.

**Re-entry**: the daemon's own `/shutdown` ends by triggering the system power-off transition,
which runs the wrapper again. The daemon **releases the lock before issuing that command**
(research R2a-i), so the wrapper acquires it cleanly and a failure to acquire means what it
says — another power-off is genuinely in progress.
