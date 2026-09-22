<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# cuems-common machinery that belongs to cuems-power-bridge

**Status**: findings only, no change made in either repository. **The split itself
is deliberately deferred**: it belongs to the future relegation of `cuems-common`'s
systemd units to their real owners (see `../cuems-common/dev/planning/systemd-service-split-architecture.md`),
not to the current xml-refactor feature.
**Measured**: 2026-09-21/22, from
- `cuems-power-bridge` @ `feat/xml-refactor` (`1e882b1`)
- `cuems-common` @ `feat/xml-refactor` (`11fab0e`, level with `origin`)
- the sibling checkouts `cuems-engine` (`rc_1`), `cuems-editor` (`main`),
  `cuems-utils` (`feat/xml-refactor`), `cuems-nodeconf` (`feat/xml-refactor`),
  `cuems-frontend` (`main`), `gradient-motion-engine` (`main`)

**Companion documents**, beside this file:
- `cuems-utils-xml-refactor-consumer-migration.md` — the current feature (node_role
  parser migration). §8 below states how this document constrains it.
- `cuems-power-bridge-node-role-findings.md` — the 2026-09-15 defect record.

**Path convention**: paths are relative to this repository's root; `../cuems-common`
is the sibling checkout. Line numbers are as measured on the commits above — verify
before acting, they drift.

---

## 0. The question, and the short answer

*Is `cuems-cluster-poweroff` the only cross-dependency between `cuems-common` and
this repository, and is the SSH infrastructure `cuems-power-bridge`'s responsibility
or does other code rely on it?*

1. **`cuems-cluster-poweroff` is the only cross-dependency the xml-refactor breaks,
   but it is not the only one.** `cuems-common` ships **two scripts, two systemd
   units, one conffile, one logind drop-in and two sudoers drop-ins** whose sole
   consumer — or sole reason to exist — is this package. All of it drives this
   repository's Python library through the shared venv, or its HTTP API on `:8478`.
2. **The node-poweroff SSH machinery is used by this package and nothing else.** No
   sibling repository opens an SSH connection (§5.1). The *SSH platform* underneath
   it — the `cuems-admin` operator account, sshd hardening, the organisation key,
   host-key rotation, the firewall's TCP/22 — is general-purpose, exists for human
   operators, and stays in `cuems-common`.
3. **The machinery splits cleanly by role.** Everything that runs on the
   **controller** can be absorbed by this (controller-only) package. The one piece
   that must exist on **nodes** — `99-cuems-admin-poweroff` — cannot, and is slated
   to become obsolete with the NNG-native shutdown anyway (§6).

---

## 1. Summary — what moves, what stays

| # | Item in `../cuems-common` | Kind | Verdict | Section |
|---|---|---|---|---|
| — | `cuems-power-bridge.service` | systemd unit | **Already absorbed** (`bb69a9e`, 2026-05-29) | §2.6 |
| A1 | `usr/bin/cuems-cluster-poweroff` | bash + Python heredocs | **Should absorb** | §2.1 |
| A2 | `etc/systemd/system/cuems-cluster-poweroff.service` | systemd unit (ExecStop hook) | **Should absorb**, with A1 | §2.1 |
| A3 | `usr/bin/cuems-displays-on` | bash + Python one-liners + curl | **Should absorb** | §2.2 |
| A4 | `etc/systemd/system/cuems-displays-on.service` | systemd unit | **Should absorb**, with A3 | §2.2 |
| A5 | `etc/cuems/cluster-poweroff.conf` | conffile (shell syntax) | **Should absorb**, with A1–A4 | §2.3 |
| A6 | `etc/systemd/logind.conf.d/10-cuems-power-button.conf` | logind drop-in | **Could absorb** — exists only to pin A2's contract | §2.4 |
| A7 | `etc/sudoers.d/99-cuems-poweroff` | sudoers (`cuems` → poweroff) | **Could absorb** — controller-only, bridge-only today; revisit at the NNG migration | §2.5 |
| S1 | `etc/sudoers.d/99-cuems-admin-poweroff` | sudoers (`cuems-admin` → poweroff), **needed on nodes** | **Stays**; delete after the NNG shutdown lands | §3.1 |
| P* | `cuems-admin` account, `50-cuems-admin`, `cuems-org.pub`, `sshd_config.d/50-cuems.conf`, host-key rotation, firewall TCP/22, the `cuems` user, `/etc/cuems/`, `network_map.xml`/`.xsd` + converter | platform | **Stays** — general-purpose, the bridge *consumes* it | §3.2 |

---

## 2. The absorption candidates

### 2.1 A1 + A2 — `cuems-cluster-poweroff` and its unit

**What it is.** The orderly pre-poweroff sequence on a controller. The unit does
nothing at boot (`ExecStart=/bin/true`, `RemainAfterExit=yes`); all its work is in
`ExecStop=/usr/bin/cuems-cluster-poweroff`, so it fires on **any** poweroff
transaction — the physical power button (via logind, A6), `systemctl poweroff`, and
the bridge's own `/shutdown` step 10 (which re-enters it). Two stages:

1. **Displays off** — `config.load()`, `DisplayManager.from_config(cfg)`,
   `.configured`, `.status_all()`, `.power_off_all()`, `.snapshot()`; gated on
   `cfg.projector_power_off_on_shutdown`. Idempotent by design: it queries first so
   the bridge-driven re-entry is a fast no-op.
2. **Nodes off** — `network_map.parse(cfg.network_map_path)`, filters
   `n.node_type != "NodeType.slave"` (**`:275`, the xml-refactor defect site 3**),
   uses `n.avahi`, `n.uuid`, `n.role_id`; self-exclusion by uuid read from
   `cfg.settings_xml_path` (`own_uuid()`, `:239-247`, swallows every exception),
   then by exact local-address membership, loopback, and hostname; one liveness
   pass `reachability.wait_until_all_down(..., max_wait_s=0, confirm_failures=1)`;
   then `node_executor.poweroff_all([SshTarget(host, user=cfg.ssh_user,
   key_path=cfg.ssh_key, poweroff_cmd=cfg.poweroff_cmd)], dry_run=cfg.dry_run)`;
   then a real `wait_until_all_down` bounded by `node_wait_s`.

**What it uses from this repository** — the complete surface, which today is an
*undeclared public API* because another package calls it through a heredoc:

| Module | Symbols |
|---|---|
| `cuemspowerbridge.config` | `load()`; fields `projector_power_off_on_shutdown`, `network_map_path`, `settings_xml_path`, `ssh_user`, `ssh_key`, `poweroff_cmd`, `dry_run` |
| `cuemspowerbridge.displays.manager` | `DisplayManager.from_config`, `.configured`, `.status_all`, `.power_off_all`, `.snapshot` (dict keys `name`, `power`) |
| `cuemspowerbridge.network_map` | `parse()`; `Node.node_type`, `.avahi`, `.uuid`, `.role_id` |
| `cuemspowerbridge.reachability` | `wait_until_all_down(targets, interval_s, max_wait_s, confirm_failures)` → `.stuck_hosts`; logger name `cuemspowerbridge.reachability` |
| `cuemspowerbridge.node_executor` | `SshTarget(host, user, key_path, poweroff_cmd)`, `poweroff_all(targets, dry_run)` |
| runtime | the venv interpreter `/usr/lib/cuems/bin/python3`, `/etc/cuems/power-bridge.conf`, `/etc/cuems/power-bridge.key` |

**What it uses from `cuems-common` itself:** only A5 (its own config) and A6 (the
power-button contract). Nothing else.

**Unit contract that must survive the move verbatim** (every edge is there for its
*stop-direction* meaning; the comments in the unit explain each):

```ini
After=networking.service network-online.target avahi-daemon.service avahi-daemon.socket
Before=cuems-power-bridge.service      # stop AFTER the bridge (no POWR 1 racing POWR 0)
Type=oneshot / RemainAfterExit=yes / ExecStart=/bin/true
ExecStop=/usr/bin/cuems-cluster-poweroff
Environment=HOME=/root                 # ssh known_hosts for a root ExecStop
TimeoutStopSec=200                     # > projector stage + node stage; never infinite
WantedBy=cuems-controller.target       # role gate; WantedBy never propagates stop
```

Plus `debian/postinst:707` — `systemctl enable cuems-cluster-poweroff.service`,
**unconditional** (the role gate is `WantedBy=`, the behaviour gate is A5's
`enabled=`, shipped `false`).

**Why the owner is this package.** Every line of logic it runs is this repository's
code; `cuems-common` contributes a bash wrapper, a config gate and a unit. Its own
config comment says so: *"Both scripts drive the bridge's own PJLink/SSH/reachability
code through it, so no protocol logic is duplicated."* The heredoc boundary is what
made the xml-refactor a two-repository cutover (defect site 3) with a `Breaks:`
dance; inside this package it would be an internal call, refactored atomically with
the parser.

**Recommended shape after absorption.** Turn the two heredocs into a Python console
entry point of this package (e.g. `cuems-power-bridge-cluster-poweroff`), keep a
`/usr/bin/cuems-cluster-poweroff` shim for operators and docs, and ship the unit
from `debian/` beside `cuems-power-bridge.service`. Fold stage 2's self-exclusion
and empty-target reporting into the same code path the daemon's `/shutdown` uses —
today they are two parallel implementations of "which nodes do I power off".

### 2.2 A3 + A4 — `cuems-displays-on` and its unit

**What it is.** Boot-time confirmation that the projectors came on. The bridge
already fires one power-on at start (`projector_power_on_on_start`); this script
polls and re-issues until every display confirms. Bash, `Type=simple` (sleeps up to
~10 min — a oneshot would block `systemctl start cuems-controller.target`),
`Restart=no`, `WantedBy=cuems-controller.target`, `After=`/`Wants=cuems-power-bridge.service`
(Wants, not Requires, only because the bridge is a `Suggests:` today).

**What it uses from this repository:**
- Python: `config.load().projector_power_on_on_start`, `config.load().shared_token`
- HTTP on `bridge_url=http://127.0.0.1:8478`: `POST /poweron` with `X-Auth-Token`
  (token fed to curl on stdin via `-K`, never argv), `GET /status` (the cached
  display snapshot)
- external tools: `curl`, `jq` (checked at start; missing → ERROR, exit 0)

**Unit contract to keep:** `Type=simple`, `Restart=no`, `WantedBy=cuems-controller.target`,
ordered after the bridge; enabled unconditionally at `debian/postinst:708`.

**Why the owner is this package.** It is a client of this package's HTTP API and
config, and of nothing else. Absorbed, it could equally become an in-daemon task
(the bridge already owns `_cancel_projector_on_task()` and the power-on at start) —
**decide that deliberately**: the external poller exists partly so a bridge restart
does not reset the confirmation window.

Adds `Depends:` on `curl`, `jq` to this package if kept as a script.

### 2.3 A5 — `/etc/cuems/cluster-poweroff.conf`

Conffile, **shell syntax** (`key=value`, sourced by bash — unlike
`power-bridge.conf`, which is `key = value` parsed by Python). Keys:
`enabled` (ships **false**; the single kill switch for A1–A4), `nodes_off`,
`node_wait_s`, `projector_timeout_s`, `displays_on_first_delay_s`,
`displays_on_interval_s`, `displays_on_max_tries`, `venv_python`, `bridge_url`.

Why it ships disabled (recorded in the file): `cuems-common` is identical on every
host; several venues keep configured projector fleets dark on purpose, and Shelly
sites already get an orderly shutdown via `POST /shutdown`. A routine upgrade must
not change behaviour with nobody present. **That rationale survives the move
unchanged** and the default must stay `false`.

After absorption, `venv_python` and `bridge_url` become internal knowledge and can
go. Merging the remaining keys into `power-bridge.conf` is the tidy end state, but
it migrates operator edits on live controllers (at least Sala1 at Castillo Medina
del Campo runs `nodes_off=false`, 2026-08-07) — do it as a **second step**, after
the ownership transfer, with a read-both-files compatibility window.

### 2.4 A6 — `logind.conf.d/10-cuems-power-button.conf`

`HandlePowerKey=poweroff` — already systemd's compiled-in default, so it changes no
behaviour. It exists *only* to make explicit the contract A2 depends on: a button
press produces an ordinary, orderly poweroff transaction (a `--force` one skips
`ExecStop=`). Not reloadable on bookworm's systemd 252 (takes effect next boot).

**Could absorb**, moving with A2 as the documentation of its precondition. The
counter-argument for keeping it: power-button policy is host policy, and a future
host without the bridge still wants an orderly poweroff. Since the value equals the
default, either choice is behaviour-neutral; **move it with A2** so the contract and
its only dependant live together.

### 2.5 A7 — `sudoers.d/99-cuems-poweroff`

```
cuems ALL=(root) NOPASSWD: /sbin/poweroff
cuems ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff
cuems ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff --no-block
```

**Consumer today: only this package's daemon**, which runs as `User=cuems`
(`debian/cuems-power-bridge.service`) and executes the local poweroff after arming
the Shelly (`controller_poweroff_cmd` / `sudo systemctl poweroff --no-block`). No
sibling repository calls `sudo poweroff` (§5.1).

Its header comment is **stale**: it says node-side `cuems` users run
`sudo /sbin/poweroff` when the bridge SSHes in. The SSH target has been
`cuems-admin` since the hardening (`cuems` is `/usr/sbin/nologin`,
`debian/preinst:103`); on nodes this rule is currently unused.

**Could absorb** (controller-only, bridge-only). **But weigh the NNG migration
first** (§6): once node poweroff is engine-native, the engine — running as `cuems`
on *nodes* — becomes a consumer of exactly this rule, and it becomes a platform rule
again. Recommendation: **leave it in `cuems-common`** unless the NNG migration is
abandoned; fix the comment now.

If absorbed: this package needs its own `visudo -cf` test (cuems-common's
`tests/test_sudoers_syntax.py` covers every file in its `etc/sudoers.d/`) and a
`chmod 440` in `postinst`.

### 2.6 Already absorbed — `cuems-power-bridge.service`

Shipped from `debian/cuems-power-bridge.service` since `bb69a9e` (2026-05-29,
"debian: ship cuems-power-bridge.service from this package"); `cuems-common`'s
history contains no bridge unit. It is the precedent for everything above.

**Stale claims that it is shipped by cuems-common**, to correct independently:
this repository's `CLAUDE.md` (Role paragraph), `debian/control:45-46`
("systemd unit is shipped in cuems-common"), and `debian/cuems-power-bridge.postinst:12,46`.

---

## 3. What stays in cuems-common, and why

### 3.1 S1 — `sudoers.d/99-cuems-admin-poweroff` (node-side)

`cuems-admin ALL=(root) NOPASSWD: /sbin/poweroff`. Its consumers are the bridge
daemon and A1, both SSHing **into nodes** as `cuems-admin` with
`power-bridge.key` — so the rule must be installed **on nodes**, and this package is
controller-only. Absorbing it would need a second binary package from this source
(e.g. `cuems-power-bridge-node`) added to every node image — a new edge for one
sudoers line that the NNG migration deletes. **Keep it; delete it when node
poweroff no longer goes over SSH.**

### 3.2 The SSH platform

| Item | Where | Who relies on it |
|---|---|---|
| `cuems-admin` account (created on first install, full sudo with password) | `debian/postinst:415-450`, `sudoers.d/50-cuems-admin` | human operators (TTY + SSH); **the bridge as SSH target** |
| Org public key → `cuems-admin`'s `authorized_keys` | `etc/cuems/ssh/cuems-org.pub`, `postinst:441-447` | human operators (`docs/default-credentials.md`) |
| sshd hardening (pubkey only, no root, no forwarding) | `etc/ssh/sshd_config.d/50-cuems.conf`; reload last in `postinst:757-764` | everyone; the bridge key operates within it |
| Cloned-image host-key rotation | `postinst:453-458` | every SSH client, incl. the bridge's `accept-new` |
| Firewall TCP/22 on the wifi fence | `usr/share/cuems/firewall/cuems-wifi-fence.nft:56` | human operators |
| `cuems` system user (`nologin`, home `/var/lib/cuems`) | `debian/preinst:95-110` | every CUEMS service, incl. the bridge |
| `/etc/cuems/`, `network_map.xml`(+`.xsd`), `cuems-migrate-network-map` | `debian/install`, `postinst:130-170` | every consumer of the topology |

These are general-purpose. The bridge **consumes** them, so `Depends: cuems-common`
stays after any absorption.

---

## 4. The coupling surface, both directions (the current state)

### 4.1 cuems-common → this package

| From | Mechanism | Surface |
|---|---|---|
| A1 stage 1 | Python heredoc in the shared venv | `config`, `displays.manager` (§2.1) |
| A1 stage 2 | Python heredoc in the shared venv | `config`, `network_map`, `reachability`, `node_executor` (§2.1) |
| A3 | Python one-liners + HTTP | `config`; `POST /poweron`, `GET /status`, `X-Auth-Token` |
| A2, A4 | systemd ordering | `Before=` / `After=` / `Wants=cuems-power-bridge.service` |
| `debian/control:49` | packaging | `Suggests: cuems-power-bridge` (deliberately not Depends/Recommends) |
| `docs/upgrade-verification.md` §7 | prose | known issue: power-off selects no nodes until this package is fixed |

### 4.2 This package → cuems-common

| Needs | Provided by cuems-common as |
|---|---|
| `debian/control:19` `Depends: cuems-common (>= 1.0.0)` | unbounded floor |
| `/etc/cuems/network_map.xml` in the vocabulary the parser reads | conffile, **rewritten to `node_role` by `postinst`** — the actual trigger of the xml-refactor defect |
| `cuems` user (daemon `User=`), `/etc/cuems/` | `preinst`, `debian/install` |
| `cuems-admin` on nodes + S1 | `postinst`, `sudoers.d` |
| local poweroff privilege | A7 |
| `cuems-controller.target` (bridge unit `WantedBy=`) | `cuems-common` units |

### 4.3 Shared and unowned

`/etc/cuems/settings.xml` — read by this package (`config.py:65`, `nng_hub_port`;
`cluster_bus`) **and** by A1 (`own_uuid()`), shipped by **no** package (class D in
cuems-common's split document §7.1). Under the xml-refactor's `ConfigManager` path it
becomes a hard precondition. Absorbing A1 does not resolve it; it removes one of its
two readers from `cuems-common`.

---

## 5. The SSH infrastructure, end to end

### 5.1 Nobody else uses it

Searched every sibling checkout (`cuems-engine`, `cuems-editor`, `cuems-utils`,
`cuems-nodeconf`, `cuems-frontend`, `gradient-motion-engine`) for `ssh`, `paramiko`,
`asyncssh`, `authorized_keys`, `cuems-admin`, `sudo … poweroff`,
`systemctl poweroff|reboot`: **no runtime use**. Hits are CI `sudo apt-get`, deploy
docs, and one `cuems-utils` test path string. In `cuems-common`'s shipped scripts
the only SSH initiator is A1 — through this package's `node_executor`.

### 5.2 The chain and its owners

| Link | Owner today | Host |
|---|---|---|
| Keypair `/etc/cuems/power-bridge.key` (ed25519, generated on first install, `chown cuems`) | **this package** (`postinst`) | controller |
| Key distribution, locked `restrict,command="sudo /sbin/poweroff"` entry in `cuems-admin`'s `authorized_keys` | **this package** (`cuems-power-bridge-deploy-keys`) | operator → nodes |
| SSH fan-out (`BatchMode=yes`, `StrictHostKeyChecking=accept-new`, `ConnectTimeout`) | **this package** (`node_executor.py:35-37`) | controller |
| Initiator 1: the daemon's `/shutdown`, as `cuems` | **this package** | controller |
| Initiator 2: A1's ExecStop, as **root** (`HOME=/root`) | cuems-common (A1/A2) | controller |
| Target account `cuems-admin` | cuems-common (platform) | nodes |
| Target privilege S1 | cuems-common | nodes |
| sshd accepting the key | cuems-common (platform) | nodes |

Everything that *initiates* the SSH is this package's code or runs it. Two
initiators share one key but **not one `known_hosts`** (`/var/lib/cuems/.ssh/` for
the daemon, `/root/.ssh/` for A1), each learning host keys independently with
`accept-new`. Absorbing A1 is the moment to decide whether that is intended.

---

## 6. Interaction with the planned NNG-native shutdown

`CLAUDE.md` § Future migration: node SSH-fanout is to be replaced by an
engine-native `/engine/command/shutdown` broadcasting COMMAND/SHUTDOWN over the NNG
bus; only `_shutdown_nodes()` is rewritten. Consequences for this inventory:

| Item | After the NNG migration |
|---|---|
| S1 `99-cuems-admin-poweroff` | **obsolete** — delete from cuems-common |
| `power-bridge.key`, `cuems-power-bridge-deploy-keys`, `node_executor` | obsolete — delete from this package |
| A1 stage 2 | rewritten over the same bus call (easier if already absorbed) |
| A7 `99-cuems-poweroff` | **becomes a node-side rule for the engine** (running as `cuems`) — platform again |
| SSH platform (§3.2) | unchanged; it serves humans |

Ordering advice: absorb A1–A6 **before** the NNG migration so `_shutdown_nodes()`
and A1 stage 2 are rewritten once, in one repository. Do **not** move A7 or S1.

---

## 7. How to execute the absorption (when its time comes)

1. **One transition, two packages.** This package ships the files at the same paths
   and declares `Replaces: cuems-common (<< X)` + `Breaks: cuems-common (<< X)`;
   `cuems-common` X drops them from `debian/install` and its `postinst` enables.
   Bound `Depends: cuems-common` to `>= X`. Keep cuems-common's `Suggests:` until the
   last reference disappears, then drop it.
2. **Conffile hand-over.** A5 (and A6, A7 if moved) are cuems-common conffiles, some
   operator-edited. Verify on a test host that a modified conffile survives the
   `Replaces:` take-over without a prompt or reset **before** shipping — a controller
   silently returning to `enabled=false` loses its orderly power-off with nobody
   noticing, the same failure class as the xml-refactor defect.
3. **Enable state.** The units are enabled by cuems-common's `postinst` today. The
   new owner enables them in its own `postinst` (explicitly, same pattern — do not
   rely on `dh_installsystemd` inference); cuems-common's removal of the files must
   not run `deb-systemd-helper purge`/`disable` on them. Check with
   `systemctl is-enabled` across the upgrade.
4. **The absent-bridge path disappears — keep the gates.** Today both scripts log
   one ERROR and exit 0 when the venv is missing, because cuems-common is on every
   host. Owned by this package they only exist where the bridge exists, so that
   branch goes; the role gate (`WantedBy=cuems-controller.target`) and behaviour gate
   (`enabled=false`) stay.
5. **Shared-venv rule** (`CLAUDE.md` Field notes): nothing moves into the venv
   bundle; inspect the built `.deb` (`dpkg-deb -c`) for collisions.
6. **Tests to add here:** unit tests for the absorbed selection/self-exclusion logic
   (one fixture per map vocabulary while unconverted hosts exist), a `visudo -cf`
   test if any sudoers moves, and a systemd-analyze verify of the units.
7. **Docs to move** from `../cuems-common`: `README.md` §§ "`cuems-cluster-poweroff`"
   (`:398`) and "`cuems-displays-on`" (`:435`), the operator-tool table row (`:212`),
   `docs/upgrade-verification.md` §7, the `CHANGELOG.md` entries (`:21-22`), and the
   `debian/install` comments (`:81-84`, `:163-166`).

---

## 8. Constraint on the current xml-refactor feature

The absorption is **out of scope** for the xml-refactor. Until it happens, §2.1's
table is a **frozen public API** of this package: the migration plan's "delete the
private parser" must either keep `network_map.parse()` returning objects with
`.avahi`, `.uuid`, `.role_id` (and a role attribute A1 can filter on), or land
`../cuems-common/usr/bin/cuems-cluster-poweroff:275,240` in the same cutover with a
`Breaks:` — a new bridge beside an old A1 would raise `AttributeError` in the middle
of a poweroff transaction. The rest of the surface (`config` field names,
`DisplayManager`, `reachability`, `node_executor`, HTTP `/poweron` + `/status`) is
untouched by the refactor and must stay so.

---

## 9. Stale items found during this research (fix independently)

- `../cuems-common/etc/sudoers.d/99-cuems-poweroff` header: describes node-side
  `cuems` users receiving the bridge's SSH — the target is `cuems-admin`.
- This repository: `CLAUDE.md`, `debian/control:45-46`,
  `debian/cuems-power-bridge.postinst:12,46` claim the unit is shipped by
  cuems-common (§2.6).
- `../cuems-common/usr/bin/cuems-cluster-poweroff:240` docstring: "matches the
  network_map `NodeType.master` entry" (already tracked by the xml-refactor).

---

## 10. Open questions for the future feature

- [ ] A3: stay an external poller or become a daemon task? (§2.2 — the restart
      semantics are the deciding factor.)
- [ ] A5: merge into `power-bridge.conf`, and with what compatibility window?
- [ ] A6: move with A2, or keep as host policy in cuems-common?
- [ ] A7: confirm the NNG migration's privilege model before deciding it (§6).
- [ ] One `known_hosts` for both initiators, or two on purpose? (§5.2)
- [ ] Unify A1 stage 2 with the daemon's `/shutdown` node selection into a single
      code path, including the empty-selection invariant from the xml-refactor.
- [ ] Coordinate with `../cuems-common/dev/planning/systemd-service-split-architecture.md`
      §7.2, whose per-unit inventory lists `cuems-cluster-poweroff.service` as
      class A + D + B but does not yet name this package as its owner.
