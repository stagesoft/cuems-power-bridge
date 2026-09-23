<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Contract — the command-line surface

**Feature**: `002-cluster-poweroff-cli` · **Status**: new. This contract **replaces** the
undeclared venv library surface that `cuems-common` drives today (feature 001,
`contracts/venv-library-surface.md`). Once `cuems-common` 1.3.0-24 ships, argv and exit codes
are the whole boundary between the two packages.

Both tools follow this repository's existing CLI shape: implementation in
`src/cuemspowerbridge/scripts/<name>.py` with a usage + exit-status docstring, a
`[tool.poetry.scripts]` entry, a POSIX-sh shim in `data/bin/<name>` that `exec`s the venv
entry, and a `debian/cuems-power-bridge.install` line putting the shim on `PATH`.

---

## `cuems-power-bridge-cluster-poweroff`

```
cuems-power-bridge-cluster-poweroff --stage {displays|nodes|all}
                                    [--node-wait S] [--projector-timeout S]
                                    [--force] [--dry-run] [--transition] [-v]
```

| Option | Meaning |
|---|---|
| `--stage` | which half of the sequence to run. The wrapper calls `displays` and `nodes` separately so its own per-stage time limits keep applying; `all` is for operators and rehearsals. |
| `--node-wait` | seconds to wait for machines to go quiet. Supplied by the caller (it lives in the platform package's config file, which is not moving). |
| `--projector-timeout` | bound for the display stage, same reason. |
| `--force` | power off every machine in the map, adopted or not, **and** override the running-show refusal. Same meaning as the API's `force=1`. |
| `--dry-run` | run every branch, change nothing. Independent of the config file's own `dry_run`. |
| `--transition` | declare that this run is the system's own power-off transition. **Suppresses the running-show probe** (there is no daemon to ask). The wrapper passes it; an operator does not. |
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
configuration file. It exists so `cuems-displays-on` can stop importing this package's
modules for the two values it needs (`projector_power_on_on_start`, `shared_token`).

**Secrets**: the value is printed to stdout only, and the *key* is the argument — a token
never appears in `argv`, preserving the reason `cuems-displays-on` already feeds it to `curl`
through stdin.

---

## Lock

Both tools' power-off path and the daemon take one non-blocking exclusive lock on
`/run/cuems-power-bridge/shutdown.lock`. The loser refuses (exit 4, or HTTP 409
`shutdown_already_in_progress`) and never queues. The kernel releases it when the holder
dies, so an interrupted run blocks nothing.
