<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Contract — the venv library surface `cuems-common` drives

**Feature**: `001-node-role-parser` · **Status**: **changed by this feature**, in a
coordinated cutover (research R1).

`cuems-common`'s `/usr/bin/cuems-cluster-poweroff` pipes a Python heredoc into this
package's interpreter (`/usr/lib/cuems/bin/python3`) and calls this package's modules
directly. It is an *undeclared* public API, frozen by constitution Principle VII until the
absorption feature — but this feature necessarily changes part of it, so the change is
declared here and enforced mechanically in packaging.

## Surface, before and after

| Module / symbol | Before | After | Breaking? |
|---|---|---|---|
| `config.load()` and fields `projector_power_off_on_shutdown`, `network_map_path`, `settings_xml_path`, `ssh_user`, `ssh_key`, `poweroff_cmd`, `dry_run` | as is | **unchanged** | no |
| `displays.manager.DisplayManager.from_config/.configured/.status_all/.power_off_all/.snapshot` | as is | **unchanged** | no |
| `reachability.wait_until_all_down(targets, interval_s, max_wait_s, confirm_failures)` → `.stuck_hosts` | as is | **unchanged** | no |
| `node_executor.SshTarget(host, user, key_path, poweroff_cmd)`, `poweroff_all(targets, dry_run)` | as is | **unchanged** | no |
| `network_map.parse(path)` | returns nodes with `.node_type` (string) | returns `NodeView`s with `.role` (`NodeRole`), `.adopted`; **no `.node_type`** | **YES** |
| `network_map.slave_avahi_names(path)` / `slave_ips(path)` | role-filtered by string | replaced by `shutdown_targets(...)` / `readiness_peers(...)` returning a `Selection` | **YES** |

## Why it breaks rather than aliasing

Keeping a `node_type` alias would let an un-upgraded tool keep working, at the cost of
shipping the retired vocabulary indefinitely (FR-023, D33, Principle III) with no date for
its removal. The two repositories are already on coordinated branches for this cutover.

## The mechanical gate (prose is not a gate)

- `cuems-power-bridge` declares `Breaks: cuems-common (<< <version with the fixed tool>)`.
- `cuems-common` declares `Breaks: cuems-power-bridge (<< <this feature's version>)`.
- `cuems-common`'s `Suggests: cuems-power-bridge` stays **unversioned and a suggestion** —
  it must remain installable and functional with no bridge present; its scripts already log
  one ERROR and exit 0 when the venv interpreter is absent, and that path must not regress.
- Result: the pair upgrades together; a half-upgrade is refused by `dpkg`/`apt` rather than
  discovered as an `AttributeError` in the middle of a poweroff transaction.

## What the fixed tool must call

After the cutover, `cuems-cluster-poweroff`'s stage 2 uses the adapter's selection function
instead of its own filter, so the five shutdown cases (spec) hold identically whether a
shutdown is triggered through the HTTP API or through the poweroff transition. Its
`:240` docstring's retired vocabulary is corrected in the same change.

Self-exclusion stays in the tool for now (it also matches by local address and hostname,
which the bridge does not need), but it reads `NodeView.is_self` where the uuid is
available.
