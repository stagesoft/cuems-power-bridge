The Python heredocs can move into cuems-power-bridge. These are the invariants that have to stay true afterward, especially the separate entry path from `POST /shutdown` and the systemd poweroff contract. Nothing is committed.

## Separate entry path from `POST /shutdown`

- [ ] The migrated code is a different entry point from `bridge._run_shutdown` / `POST /shutdown`. It is callable as a library function or a CLI, and it does not go through the HTTP handler.
- [ ] It never arms a Shelly relay, never pre-checks Shelly reachability, and never returns HTTP 502 when no Shelly answers.
- [ ] It never runs `systemctl poweroff`, `controller_poweroff_cmd`, or any local poweroff. Systemd is already in the poweroff transaction; the entry point returns and lets systemd finish.
- [ ] It never applies the `/shutdown` `refuse_if_running` guard. A playing project must not block a poweroff that has already started.
- [ ] It does not call `http://127.0.0.1:8478`. `ExecStop` runs after `cuems-power-bridge.service` has stopped (`Before=cuems-power-bridge.service`), so the HTTP API is already gone. The entry point imports the library in-process.
- [ ] Stage order stays displays off, then nodes off. The node feeds the projectors; powering the node first flashes "no signal" on stage. This is the opposite of `/shutdown`, which fans out SSH and projector power-off concurrently.
- [ ] Display power-off still queries status first and skips `POWR 0` when every display is already `off` or `cooldown`. A Shelly `/shutdown` ends in `sudo /sbin/poweroff`, which re-enters this path; that second pass has to stay a fast no-op instead of a second full PJLink cycle.
- [ ] Display power-off still skips the whole fleet only when every device answers `unknown`. If even one device answers, every configured display still gets `POWR 0`.
- [ ] `projector_power_off_on_shutdown=false` and "no displays configured" still leave the lamps alone and exit success.
- [ ] Node selection still self-excludes this host, in this order: UUID from `settings.xml`, exact address-set intersection (never a substring match), loopback, then hostname / `role_id`. A missed self-entry cannot be observed as "down" and would burn the whole `node_wait_s`.
- [ ] The liveness probe stays `max_wait_s=0` with `confirm_failures=1`. The default of 3 leaves every host unconfirmed. That probe's reachability logger stays silenced so "timeout after 0.0s" does not read as a fault.
- [ ] SSH runs only for hosts that answered alive. `dry_run=true` still skips the wait. `ssh` exit 255 while a node drops is still treated as expected; the reachability poll is the acknowledgement.
- [ ] `nodes_off=false` still skips stage 2 entirely, including the wait. It must not be encoded as a missing SSH key.
- [ ] Config meaning stays the bridge's: `ssh_user`, `ssh_key`, `poweroff_cmd`, `dry_run`, `projector_power_off_on_shutdown`, and `projector.N.*` are read via `config.load()`. No secret is placed on a command line.
- [ ] A future `node_role` fix in `network_map` is the same cutover as this call site. The heredoc's `n.node_type != "NodeType.slave"` filter must not be copied forward as a second parser.

## Systemd stability

- [ ] `cuems-cluster-poweroff.service` stays in cuems-common. `ExecStop=` still points at a cuems-common script. The unit still does nothing at start (`Type=oneshot`, `RemainAfterExit=yes`, `ExecStart=/bin/true`).
- [ ] cuems-common keeps `Suggests: cuems-power-bridge` and does not gain `Depends` or `Recommends`. The bridge package already `Depends:` on cuems-common; the reverse edge stays soft. Nodes and controllers without the bridge remain a supported install.
- [ ] If the venv interpreter is missing, the wrapper logs one error and exits 0. A missing bridge cannot fail `systemctl stop` or a poweroff.
- [ ] The wrapper's own final exit is 0 on every path that ran: disabled, reboot, not-a-poweroff, stage warnings, and success. Stage failures are warnings. Stage 1 failing still continues to stage 2.
- [ ] Reboot and kexec still exit 0 before any PJLink or SSH. `POWR 0` on a reboot leaves the room dark because PJLink answers `ERR3` to `POWR 1` during lamp cooldown.
- [ ] `isolate rescue.target`, a stray `systemctl stop`, and any transaction that is not `poweroff.target` or `halt.target` still exit 0 before either stage. Unknown means skip.
- [ ] `--force` still bypasses only the transaction check. It still stops `cuems-displays-on.service` and still runs both stages.
- [ ] `systemctl list-jobs` stays wrapped in `timeout 5s`. `systemctl stop cuems-displays-on.service` stays wrapped in `timeout 10s`. A synchronous `systemctl` from inside a unit script has deadlocked this fleet before.
- [ ] Stopping `cuems-displays-on.service` remains in the wrapper, including on the `--force` path, so the boot watchdog cannot issue `POWR 1` behind the shutdown sequence.
- [ ] Unit ordering is unchanged: `After=networking.service network-online.target avahi-daemon.service avahi-daemon.socket` and `Before=cuems-power-bridge.service`. Stop order is the reverse, so PJLink and `<node>.local` still work, and `_cancel_projector_on_task()` has already run before `POWR 0`.
- [ ] `WantedBy=cuems-controller.target` stays the only install edge. `WantedBy=` does not propagate stop, so stopping the controller target or restarting an engine must not run this `ExecStop`.
- [ ] `TimeoutStopSec=200` still exceeds the script budget: `projector_timeout_s` (shipped 45 in `cluster-poweroff.conf`; script fallback 25) plus `node_wait_s + 30` (default 150). The outer timeouts stay in the wrapper even if the Python grows its own deadlines.
- [ ] `Environment=HOME=/root` stays on the unit. Root `ExecStop` has no `$HOME`, and ssh needs it for `known_hosts`.
- [ ] The ordering probe still logs on every path, including the early exits: `cuems-power-bridge`, `avahi-daemon`, `networking`, and the default route.
- [ ] Journal output stays a single stream. The wrapper echoes lines; it does not also call `logger`. Python stays unbuffered (`-u` or `PYTHONUNBUFFERED`) so PJLink and SSH progress appears while the stage is running, not only after it exits.
- [ ] The interpreter is still `"$venv_python"` from `cluster-poweroff.conf`, not a bare shebang, so the override and the missing-interpreter guard both keep working.
- [ ] `/etc/cuems/cluster-poweroff.conf` stays a cuems-common conffile, sourced by both this script and `cuems-displays-on`, shipped with `enabled=false`. Opt-in behaviour does not change on a package upgrade.
- [ ] No new sandboxing directives are added to the unit. The stop script needs ssh, `ip`, `systemctl`, and `/usr/lib/cuems/`.

## Packaging of the move itself

- [ ] The new bridge entry point is not required for cuems-common to configure or to shut down. An upgraded common against an older bridge degrades to the existing "interpreter missing / entry missing → log and exit 0" behaviour, or the two packages land in the same cutover with an explicit version gate.
- [ ] An old `cuems-cluster-poweroff` against a bridge that drops `node_type` does not raise `AttributeError` during poweroff. That failure mode is already documented for the `node_role` migration; this move must not add another one.
- [ ] Operator commands stay `cuems-cluster-poweroff [--force]`. The bridge entry point is an internal helper, not a second way to power the venue down.
