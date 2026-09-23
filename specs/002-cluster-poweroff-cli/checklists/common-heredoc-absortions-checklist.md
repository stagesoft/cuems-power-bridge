<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Heredoc-absorption invariants — cuems-common's side

**Marked 2026-09-23** against feature `002-cluster-poweroff-cli` as specified, planned and
tasked. Convention, as for the other planning documents: `[x]` = **true now** (either untouched
by this feature, or guaranteed by a decision with nothing left to implement); `[ ]` = decided
but **not yet true**, annotated with the task that closes it. Nothing is ticked because it is
intended.

## Deviations — four lines this feature knowingly departs from

All four come from **decision 3** (the running-show guard), taken after this checklist was
written, and **all four are confined to the manual `--force` path**. None of them is reachable
from a power-off transaction, which is the product:

1. *"It never applies the `refuse_if_running` guard"* — a **manual** run now refuses while a
   project is playing, unless `--while-playing` is given. A transaction never asks.
2. *"It does not call `http://127.0.0.1:8478`"* — a **manual** run probes `GET /status` to
   answer (1). The bullet's own reason — the API is gone once `ExecStop` runs — is honoured:
   the probe is never requested there.
3. *"`--force` still bypasses only the transaction check"* — `--force` now also requests the
   guard.
4. *"The wrapper's final exit is 0 on every path that ran"* — a **refused** manual run may exit
   non-zero. Every transaction path still exits 0.

The reason to accept them: the manual path is **unguarded today** and destructive — it stops
the boot watchdog, blanks the fleet and powers the machines off with no check at all. The
guard closes that hole without touching the product path, and the invariant that governs it is
now written into the spec: **the product must always be able to power the venue off mid-show.**

## Substitutions — two lines satisfied by a different mechanism

- the self-exclusion uuid comes from the owning library rather than a private `settings.xml`
  read (FR-017) — same value, correct source;
- `Environment=HOME=/root` stays, but `known_hosts` is pinned explicitly to one shared store
  (FR-024), so `HOME` no longer selects it.

---

The Python heredocs can move into cuems-power-bridge. These are the invariants that have to stay true afterward, especially the separate entry path from `POST /shutdown` and the systemd poweroff contract. Nothing is committed.

## Separate entry path from `POST /shutdown`

- [x] The migrated code is a different entry point from `bridge._run_shutdown` / `POST /shutdown`. It is callable as a library function or a CLI, and it does not go through the HTTP handler.
      → **by design** — an internal module entry (`python -m`), not the HTTP handler (research R4a). Verified by T013, T017.
- [ ] It never arms a Shelly relay, never pre-checks Shelly reachability, and never returns HTTP 502 when no Shelly answers.
      → **decided, FR-001a** — the relay and its pre-check stay in `bridge.py`; the shared code cannot reach them. Closed by T009, T019a.
- [ ] It never runs `systemctl poweroff`, `controller_poweroff_cmd`, or any local poweroff. Systemd is already in the poweroff transaction; the entry point returns and lets systemd finish.
      → **decided, FR-001a** — the local power-off stays with the HTTP route. Closed by T009, T019a.
- [ ] It never applies the `/shutdown` `refuse_if_running` guard. A playing project must not block a poweroff that has already started.
      → ⚠️ **DEVIATION (manual path only)** — see *Deviations* above. On a transaction: never, as written. Closed by T026c, T032.
- [ ] It does not call `http://127.0.0.1:8478`. `ExecStop` runs after `cuems-power-bridge.service` has stopped (`Before=cuems-power-bridge.service`), so the HTTP API is already gone. The entry point imports the library in-process.
      → ⚠️ **DEVIATION (manual path only)** — see *Deviations*. The stated reason (the API is gone during a transition) is honoured: no probe is requested there. Closed by T031, T032.
- [ ] Stage order stays displays off, then nodes off. The node feeds the projectors; powering the node first flashes "no signal" on stage. This is the opposite of `/shutdown`, which fans out SSH and projector power-off concurrently.
      → **FR-003a** — the caller orders the stages; the wrapper keeps displays-then-nodes. Closed by T025.
- [ ] Display power-off still queries status first and skips `POWR 0` when every display is already `off` or `cooldown`. A Shelly `/shutdown` ends in `sudo /sbin/poweroff`, which re-enters this path; that second pass has to stay a fast no-op instead of a second full PJLink cycle.
      → **preserved verbatim** in the moved stage body. Closed by T009, T009b.
- [ ] Display power-off still skips the whole fleet only when every device answers `unknown`. If even one device answers, every configured display still gets `POWR 0`.
      → **preserved verbatim**. Closed by T009b.
- [ ] `projector_power_off_on_shutdown=false` and "no displays configured" still leave the lamps alone and exit success.
      → **FR-023** — this was missing from the whole feature until analysis G4 caught it. Closed by T009a, T009b.
- [ ] Node selection still self-excludes this host, in this order: UUID from `settings.xml`, exact address-set intersection (never a substring match), loopback, then hostname / `role_id`. A missed self-entry cannot be observed as "down" and would burn the whole `node_wait_s`.
      → **satisfied with one substitution**: the uuid comes from the library's resolved value instead of a private `settings.xml` read (FR-017) — same value, owning source. Address-set, loopback and hostname tests preserved verbatim, including the never-a-substring rule. Closed by T010.
- [ ] The liveness probe stays `max_wait_s=0` with `confirm_failures=1`. The default of 3 leaves every host unconfirmed. That probe's reachability logger stays silenced so "timeout after 0.0s" does not read as a fault.
      → **FR-003a** — `pre_pass=True`, logger silenced for that call only. Closed by T009c.
- [ ] SSH runs only for hosts that answered alive. `dry_run=true` still skips the wait. `ssh` exit 255 while a node drops is still treated as expected; the reachability poll is the acknowledgement.
      → **FR-003a** — the transition path's fan-out is addressed only to live hosts; `dry_run` still skips the wait. Closed by T009c, T012.
- [x] `nodes_off=false` still skips stage 2 entirely, including the wait. It must not be encoded as a missing SSH key.
      → **untouched** — the early return stays in the wrapper. Re-verified by T039.
- [ ] Config meaning stays the bridge's: `ssh_user`, `ssh_key`, `poweroff_cmd`, `dry_run`, `projector_power_off_on_shutdown`, and `projector.N.*` are read via `config.load()`. No secret is placed on a command line.
      → **preserved**; the config query takes the KEY as its argument, never the value, so no secret reaches a command line (contract, U2). Closed by T015, T026.
- [x] A future `node_role` fix in `network_map` is the same cutover as this call site. The heredoc's `n.node_type != "NodeType.slave"` filter must not be copied forward as a second parser.
      → **done** — feature 001 replaced the parser and the heredoc now calls the adapter; no second parser was copied forward.

## Systemd stability

- [x] `cuems-cluster-poweroff.service` stays in cuems-common. `ExecStop=` still points at a cuems-common script. The unit still does nothing at start (`Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=/bin/true`).
      → **untouched** — this feature moves code only; the unit, its type and its `ExecStart=/bin/true` are not edited.
- [x] cuems-common keeps `Suggests: cuems-power-bridge` and does not gain `Depends` or `Recommends`. The bridge package already `Depends:` on cuems-common; the reverse edge stays soft. Nodes and controllers without the bridge remain a supported install.
      → **untouched** — no new dependency edge in either direction (research R7). Re-verified by T040.
- [x] If the venv interpreter is missing, the wrapper logs one error and exits 0. A missing bridge cannot fail `systemctl stop` or a poweroff.
      → **preserved by decision 2** — the helper is invoked through that same interpreter, so the existing `[ ! -x "$venv_python" ]` guard keeps working unchanged. Re-verified by T026, T037.
- [ ] The wrapper's own final exit is 0 on every path that ran: disabled, reboot, not-a-poweroff, stage warnings, and success. Stage failures are warnings. Stage 1 failing still continues to stage 2.
      → ⚠️ **DEVIATION (manual path only)** — a refused manual run may exit non-zero; every transaction path still exits 0, stage failures still warnings, stage 1 failing still continues to stage 2 (FR-009a). Closed by T026b.
- [x] Reboot and kexec still exit 0 before any PJLink or SSH. `POWR 0` on a reboot leaves the room dark because PJLink answers `ERR3` to `POWR 1` during lamp cooldown.
      → **untouched** — the transaction check is not edited.
- [x] `isolate rescue.target`, a stray `systemctl stop`, and any transaction that is not `poweroff.target` or `halt.target` still exit 0 before either stage. Unknown means skip.
      → **untouched** — unknown-means-skip preserved.
- [ ] `--force` still bypasses only the transaction check. It still stops `cuems-displays-on.service` and still runs both stages.
      → ⚠️ **DEVIATION** — `--force` additionally requests the running-show guard (decision 3). It still stops `cuems-displays-on.service` and still runs both stages. Closed by T026c.
- [x] `systemctl list-jobs` stays wrapped in `timeout 5s`. `systemctl stop cuems-displays-on.service` stays wrapped in `timeout 10s`. A synchronous `systemctl` from inside a unit script has deadlocked this fleet before.
      → **untouched**.
- [x] Stopping `cuems-displays-on.service` remains in the wrapper, including on the `--force` path, so the boot watchdog cannot issue `POWR 1` behind the shutdown sequence.
      → **untouched**, including on the `--force` path.
- [x] Unit ordering is unchanged: `After=networking.service network-online.target avahi-daemon.service avahi-daemon.socket` and `Before=cuems-power-bridge.service`. Stop order is the reverse, so PJLink and `<node>.local` still work, and `_cancel_projector_on_task()` has already run before `POWR 0`.
      → **untouched** — no unit is edited by this feature.
- [x] `WantedBy=cuems-controller.target` stays the only install edge. `WantedBy=` does not propagate stop, so stopping the controller target or restarting an engine must not run this `ExecStop`.
      → **untouched**.
- [x] `TimeoutStopSec=200` still exceeds the script budget: `projector_timeout_s` (shipped 45 in `cluster-poweroff.conf`; script fallback 25) plus `node_wait_s + 30` (default 150). The outer timeouts stay in the wrapper even if the Python grows its own deadlines.
      → **untouched** — the outer `timeout`s stay in the wrapper; the helper adds no deadline of its own.
- [ ] `Environment=HOME=/root` stays on the unit. Root `ExecStop` has no `$HOME`, and ssh needs it for `known_hosts`.
      → **stays**, but its stated reason narrows: `known_hosts` is now pinned explicitly to one shared store for both initiators (FR-024, analysis G5), so `HOME` no longer selects it. Closed by T010c; the one-time re-learn is on the hardware ledger.
- [x] The ordering probe still logs on every path, including the early exits: `cuems-power-bridge`, `avahi-daemon`, `networking`, and the default route.
      → **untouched**.
- [ ] Journal output stays a single stream. The wrapper echoes lines; it does not also call `logger`. Python stays unbuffered (`-u` or `PYTHONUNBUFFERED`) so PJLink and SSH progress appears while the stage is running, not only after it exits.
      → **preserved** — the helper writes line-oriented unbuffered stdout and installs no syslog handler; the wrapper keeps its `| while read … log` pipe. Closed by T014, T025.
- [x] The interpreter is still `"$venv_python"` from `cluster-poweroff.conf`, not a bare shebang, so the override and the missing-interpreter guard both keep working.
      → **guaranteed by decision 2** — `"$venv_python" -m cuemspowerbridge.scripts.cluster_poweroff`; no `PATH` shim exists for the helper, so both the override and the guard keep working. Implemented by T025, T026, T026a.
- [x] `/etc/cuems/cluster-poweroff.conf` stays a cuems-common conffile, sourced by both this script and `cuems-displays-on`, shipped with `enabled=false`. Opt-in behaviour does not change on a package upgrade.
      → **untouched** — it stays there, shell-syntax, shipped `enabled=false`; merging it is explicitly out of scope.
- [x] No new sandboxing directives are added to the unit. The stop script needs ssh, `ip`, `systemctl`, and `/usr/lib/cuems/`.
      → **untouched**.

## Packaging of the move itself

- [x] The new bridge entry point is not required for cuems-common to configure or to shut down. An upgraded common against an older bridge degrades to the existing "interpreter missing / entry missing → log and exit 0" behaviour, or the two packages land in the same cutover with an explicit version gate.
      → **satisfied by the second branch**: both halves land in the coordinated `xml-refactor-merge-candidate`, and a host without the bridge still hits the missing-interpreter guard and exits 0.
- [x] An old `cuems-cluster-poweroff` against a bridge that drops `node_type` does not raise `AttributeError` during poweroff. That failure mode is already documented for the `node_role` migration; this move must not add another one.
      → **satisfied by mechanism** — feature 001 chose the reciprocal `Breaks:` cutover, which this item allows as its alternative; the mixed pair is refused by dpkg rather than failing mid-poweroff.
- [x] Operator commands stay `cuems-cluster-poweroff [--force]`. The bridge entry point is an internal helper, not a second way to power the venue down.
      → **guaranteed by decision 2** — the helper installs no `PATH` command; only `cuems-power-bridge-config` (a read-only query) ships a shim.
