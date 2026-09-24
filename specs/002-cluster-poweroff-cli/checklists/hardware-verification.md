# Hardware & manual verification ledger — features 001 and 002

**Created 2026-09-23 (feature 002 — FR-021a, SC-011); expanded the same day** after two
analysis passes and three design decisions (the shared code is the two stage bodies, the helper
is internal, the running-show guard is requested and manual-only). One place for every check that needs
real hardware, a real cluster or a human, for **both** features, so the debt is countable
instead of spread across a task list and an evidence README.

**Nothing here is performed.** Every box is unchecked, and that is the accurate state as of
2026-09-23. The constitution accepts "verified" and "not verified"; it does not accept
silence.

**Part of a coordinated validation.** This repository's half of the xml refactor lands in the
`xml-refactor-merge-candidate` tag at `cuems-power-bridge 0.3.1-1`, beside `cuems-common`
1.3.0-23, `cuems-nodeconf` 0.1.0-8 and `cuems-utils` 0.1.0rc16. The sibling ledgers —
`cuems-nodeconf`'s `specs/002-public-network-map-path/checklists/hardware-verification.md` and
`cuems-common`'s `docs/upgrade-verification.md` — are meant to be worked **in one pass on the
same machines**. This one covers what only a controller running the bridge can prove.

**Why a ledger at all.** Feature 001 shipped with six hardware-dependent items scattered
across `tasks.md` and `evidence/README.md`, countable only by reading both. Two of them are
the difference between "the power-off works" and "the power-off reports that it worked".

---

## Order of work on a controller

1. §1–§2 before upgrading (they capture the *old* behaviour, and cannot be redone after).
2. §3–§6 after the candidate is installed.
3. §13's "before" capture belongs with step 1 — the old host-key store cannot be read back
   after the first post-upgrade power-off.
4. §7–§14 are the power-off rehearsals proper; do them last, and expect the machine to go
   down. **§11 is the one that matters most**: a power-off during a playing show must complete
   through every trigger the product uses.

---

## 1. Feature 001 T053 — the built package bundles nothing another package ships

- [ ] **Not performed.** Needs a build host (`dh-virtualenv`, `python3-dev`); the development
      box has neither.

**Do**: `dpkg-buildpackage -b -uc -us` on the controller, then
`dpkg-deb -c ../cuems-power-bridge_*.deb`, asserting **no** `site-packages/cuemsutils*` and
**yes** the aiohttp stack. Record in `specs/001-node-role-parser/evidence/deb-contents.txt`.

**Proves**: the shared venv rule (constitution VI). `cuemsutils` became a real dependency in
feature 001; if the build bundles it, `dpkg -i` collides with `cuems-utils`, and if a strip
goes too far it removes a module another component needs at its next restart.

**Why the suite cannot**: the suite never builds a package.

## 2. Feature 001 T054 — the identity document is actually shipped

- [ ] **Not performed.**

**Do**: confirm the built `cuems-utils` 0.1.0rc16 `.deb` installs `/etc/cuems/settings.xml`
(its packaging lives on that repository's `debian/bookworm` branch, not on
`feat/xml-refactor`). Record in
`specs/001-node-role-parser/evidence/settings-xml-provenance.txt`.

**Proves**: the precondition the whole topology read now rests on. Without that file the
bridge refuses every topology-dependent operation — correctly, and uselessly.

**Why the suite cannot**: it tests behaviour *given* the file; it cannot test who ships it.

## 3. Feature 001 T045 — the mixed pair is refused

- [ ] **Not performed.**

**Do**: on a test host, attempt to install this package beside a `cuems-common` older than
1.3.0-23, and the reverse. Both must be refused by `dpkg`/`apt`. Record in
`specs/001-node-role-parser/evidence/upgrade-refusal.txt`.

**Proves**: the reciprocal `Breaks:` pair is real, not prose. The failure it prevents is an
`AttributeError` part-way through a poweroff transaction.

**Why the suite cannot**: package relationships are resolved by the package manager.

## 4. Feature 001 T056 — orderly power-off selects the right machines

- [ ] **Not performed.**

**Do**: on a real cluster with a converted `network_map.xml`, run `POST /shutdown` with
`dry_run=true` and read the logged target list; then live, and watch the adopted machines go
off, the reachability poll confirm them, the Shelly arm, and mains cut on an already-off
controller. Record in `specs/001-node-role-parser/evidence/hardware-verification.md`.

**Proves**: the defect feature 001 fixed is actually fixed on the machines that had it — the
old reader selected **zero** nodes here and reported success.

**Why the suite cannot**: no SSH, no Shelly, no mains.

## 5. Feature 001 T057 — the auto-load gate waits

- [ ] **Not performed.** **Verify SEPARATELY from §4** — the two features fail and recover
      independently, and "the bridge works now" is not an answer to either.

**Do**: cold-boot the controller with the nodes powered; confirm the gate waits for the
adopted machines on the NNG hub, the show loads and arms, and the proven timings
(`settle=45`, `armed_timeout=125`) still hold.

**Proves**: the readiness gate reads the converted map. Before the fix it saw no nodes, took
the single-controller path, and loaded the show before the node engines joined.

**Why the suite cannot**: there is no NNG hub, no engine and no boot.

## 6. Feature 001 T058 — an unconverted map refuses, and mains stays on

- [ ] **Not performed.** Needs no cluster — a controller and a deliberately un-migrated map.

**Do**: point a test controller at a `network_map.xml` still carrying `<node_type>` and call
`POST /shutdown`, with and without `force=1`. Both must return 503 `topology_unreadable` and
leave mains on.

**Proves**: *force overrides policy, never evidence* — on real hardware, not in a mock.

**Why the suite cannot**: it can prove the refusal; it cannot prove the relay stayed closed.

---

## 7. Feature 002 — the wall switch behaves exactly as before

- [ ] **Not performed.**

**Do**: with the candidate installed, flip the physical switch on a real cluster. Compare the
journal against a pre-upgrade power-off: same machines, same order, same decisions — the only
difference being that the lines now come from the module the wrapper invokes
(`"$venv_python" -m cuemspowerbridge.scripts.cluster_poweroff`) instead of the heredoc.

**Proves**: the governing constraint of feature 002 — *the systemd + Shelly path is the
product and must not change*. This is the check the whole feature is measured against.

**Why the suite cannot**: the transition path only exists during a real poweroff, driven by
systemd, with the daemon already stopped. That combination cannot be simulated.

## 8. Feature 002 — both entry points decide identically, on hardware

- [ ] **Not performed.**

**Do**: on the same cluster and the same map, trigger a power-off through the wall switch and
(after boot) through `POST /shutdown`. Diff the two journals' selection lines.

**Proves**: the de-duplication held where it matters. The automated equivalence test compares
decisions in-process; this compares them across two processes and two identities (root vs
`cuems`) — now sharing one host-key store (§13).

**Why the suite cannot**: one of the two callers only ever runs as root during a transition.

## 9. Feature 002 — the manual path, on a controller

- [ ] **Not performed.**

**Do**: with a show playing, run `sudo cuems-cluster-poweroff --force` (the operator command —
there is deliberately no new one). Confirm it **refuses**, names the project, and changes
nothing. Repeat with `--force --while-playing` and confirm it proceeds and says so in the
journal. Then
confirm the fail-open cases: stop the daemon and re-run (proceeds with a WARNING); with the
engine wedged so `engine_state` is `unknown` (proceeds with a WARNING); with `dry_run=true`
(guard skipped entirely).

**Proves**: the guard closes the hole on the manual path — which today blanks the fleet and
powers the machines off mid-show with no check at all — without ever being able to reach the
product path.

**Why the suite cannot**: it needs a real engine playing a real project, and a daemon that can
be stopped and wedged.

## 9a. Feature 002 — the lock, across two real processes

- [ ] **Not performed.**

**Do**: hold it (`flock /run/cuems-power-bridge/shutdown.lock -c 'sleep 30'`), then confirm a
wrapper run and `POST /shutdown` both refuse immediately rather than queueing. `kill -9` the
holder and confirm the next attempt proceeds. Finally confirm the wrapper holds **one** lock
across both stages — the gap between them is where a second power-off could otherwise start.

**Proves**: FR-014 and SC-009 across processes, identities and the stage boundary.

**Why the suite cannot**: `flock` between the packaged daemon and a root transition.

## 10. Feature 002 — a host with no bridge still shuts down

- [ ] **Not performed.**

**Do**: on a node (which never installs this package), trigger a poweroff and confirm
`cuems-cluster-poweroff` logs one ERROR line about the missing tool and exits 0, leaving the
shutdown to complete normally.

**Proves**: `cuems-common` stays functional without this package — the reason its dependency
is a `Suggests:` and not a `Depends:`.

**Why the suite cannot**: it tests this package; this is a check about its absence.

## 11. Feature 002 — **a power-off during a show completes** (SC-014)

- [ ] **Not performed.** ⚠️ **The single most important check in this ledger.**

**Do**: with a project **playing**, power the venue off through each trigger the product uses,
one per run: the **wall switch**, the **physical power button**, and `systemctl poweroff`.
Each must complete the full sequence — displays off, machines off and confirmed, controller
down — with nothing refusing and nothing waiting for an answer.

**Proves**: the invariant this feature is governed by — *the product must always be able to
power the venue off mid-show*, one of the wall switch's primary intents. The running-show
guard added for manual runs must be **unreachable** from every one of these paths.

**Why the suite cannot**: the guard's absence on the product path is provable by call graph
(T020), but only hardware proves that a real switch, with a real show running, still kills the
venue.

## 12. Feature 002 — the relay is armed exactly once (SC-013)

- [ ] **Not performed.**

**Do**: trigger `POST /shutdown` and follow the journal through the re-entrant transition it
causes. Confirm the relay is pre-checked and armed **once**, by the HTTP route only; that the
wrapper's stages run on the re-entry (displays a fast no-op, machines already down); and that
the lock does not refuse that transition.

**Proves**: FR-001a and the C2 fix together — the shared code cannot arm the relay, and the
daemon releases the lock before triggering the transition it causes.

**Why the suite cannot**: re-entry only exists when systemd really powers the box off.

## 13. Feature 002 — the shared host-key store, and its one-time re-learn

- [ ] **Not performed.** ⚠️ **Do this deliberately, and record it.**

**Do**: before the upgrade, note the contents of `/root/.ssh/known_hosts` (the transition
path's current store). After it, confirm both initiators use
`/var/lib/cuems/.ssh/known_hosts`, and that the first power-off through the transition
**re-accepts** each machine's host key there under `accept-new`. Compare the re-learned keys
against the old root store: they must match.

**Proves**: FR-024. It is also the one security-relevant consequence of the change — for one
power-off, a substituted machine would be accepted silently, so the comparison against the old
store is the check that closes the window.

**Why the suite cannot**: there is no second identity and no real host key in a test.

## 14. Feature 002 — the wrapper still cannot fail a shutdown

- [ ] **Not performed.**

**Do**: force each failure the helper can report during a real transition — an unreadable
topology, a refused selection, a stuck machine — and confirm the wrapper logs each and still
**exits 0**, that stage 1 failing still continues to stage 2, and that `systemctl stop` never
reports a failed unit.

**Proves**: FR-009a. An `ExecStop` that fails must never fail the stop it belongs to, and this
feature introduces four new non-zero statuses into that path.

**Why the suite cannot**: the discipline only matters inside a real stop job.

---

## Record sheet

Copy once per host.

```
host:                          role (controller/node):
date:                          operator:
versions before:   cuems-power-bridge          cuems-common          cuems-utils
versions after:    cuems-power-bridge 0.3.1-1  cuems-common 1.3.0-23 cuems-utils 0.1.0rc16
candidate tag present in all repos:            [ ] yes

BEFORE UPGRADE
  power-off journal captured for comparison (§7):        [ ] yes  [ ] n/a (fresh host)

PACKAGING
  .deb bundles no cuemsutils, keeps aiohttp (§1):        [ ] pass
  cuems-utils .deb installs /etc/cuems/settings.xml (§2):[ ] pass
  mixed pair refused, both directions (§3):              [ ] pass

FEATURE 001 — the two broken behaviours, verified SEPARATELY
  power-off selects the adopted nodes (§4):              [ ] dry-run  [ ] live
      nodes confirmed down before Shelly armed:          [ ] yes
  auto-load waits for the nodes, show arms (§5):         [ ] pass   settle/armed timings held: [ ] yes
  unconverted map refuses 503, mains stays on (§6):      [ ] no force  [ ] with force=1

FEATURE 002 — the product path, unchanged
  wall switch: same machines, same order (§7):           [ ] pass
      journal diff vs before-upgrade capture:            [ ] identical decisions
  wall switch vs POST /shutdown agree (§8):              [ ] pass
  POWER-OFF DURING A PLAYING SHOW COMPLETES (§11):
      wall switch  [ ]    power button  [ ]    systemctl poweroff  [ ]
  relay armed exactly once per shutdown (§12):           [ ] pass
  wrapper exits 0 on every transaction path (§14):       [ ] pass
      stage 1 failure still continued to stage 2:        [ ] yes
  node with no bridge still shuts down (§10):            [ ] pass

FEATURE 002 — the manual path and the machinery
  manual run refuses during a show; override proceeds (§9):   [ ] pass
      fail-open: daemon down [ ]  engine unknown [ ]  dry_run [ ]
  lock: second run and API both refuse, no queue (§9a):  [ ] pass
      released after kill -9 [ ]   held across BOTH stages [ ]
  host-key store migrated, keys re-learned and compared (§13): [ ] pass
      old /root/.ssh/known_hosts captured beforehand:    [ ] yes

notes / anything that surprised you:
```

---

## Companion ledgers for the same candidate

| Repository | Ledger |
|---|---|
| `cuems-nodeconf` | `specs/002-public-network-map-path/checklists/hardware-verification.md` |
| `cuems-common` | `docs/upgrade-verification.md` (+ its own record sheet) |
| `cuems-power-bridge` | **this file** |

Work them in one pass per machine. `cuems-common`'s §7 currently documents the orderly
power-off defect as a *known issue* — once §4 and §7 here pass, that note is what should be
retired, and its removal is the ecosystem-level sign that the candidate is sound.
