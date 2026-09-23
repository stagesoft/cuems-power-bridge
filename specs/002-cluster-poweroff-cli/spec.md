<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Feature Specification: Cluster power-off CLI — one shutdown, two entry points

**Feature Branch**: `002-cluster-poweroff-cli`

**Created**: 2026-09-23

**Status**: Draft — clarified 2026-09-23 (Q1, Q2, Q3 answered)

**Input**: Move the orderly power-off logic that currently lives inside another package's
shell script into this package as a documented command-line tool, and make that tool and the
HTTP shutdown share one implementation, so the wall switch and the API cannot disagree.

## Context

The orderly cluster power-off that runs when a controller shuts down is **not** one program.
It is a shell script in the platform package with this package's code embedded inside it: of
its 381 lines, **221 are this repository's logic**, executed by this repository's interpreter,
maintained in a repository whose tests never run it.

The consequences are already recorded, not predicted:

- The defect the previous feature fixed lived in that embedded code for months, invisible to
  both repositories' test suites, and would have cut mains power to running machines.
- The power-off sequence exists **twice**: once for the HTTP shutdown, once inside that
  script. After the previous feature they agree on *which machines to select* and on nothing
  else — the self-exclusion rules, the "already off" fast path and the timeout policy are
  separate implementations that happen to match.
- A second script in the platform package reaches into this package the same way, for two
  values it could simply ask for.

This feature relocates that logic here, gives it the same shape as this package's other
operator tools, and makes both entry points run the same code. It is a **relocation and a
de-duplication, not a behaviour change**: every operator-visible outcome must be identical
before and after.

## Clarifications

### Session 2026-09-23

- **Q1 — Two power-offs at once.** The daemon's guard is within its own process, so it cannot
  see a separate tool process. **A: one exclusive lock, taken by every entry point.** Whoever
  loses refuses. "One power-off at a time" becomes true for the whole machine rather than for
  one program. (**FR-014**)
- **Q2 — A hand-run during a show.** **A: the guard lives OUTSIDE the shared sequence.** A
  manual run asks the running daemon whether a project is playing and refuses unless
  explicitly forced; when the daemon is unreachable — which is exactly the system-transition
  case — there is no guard and the behaviour is identical to today. The reasoning is the
  governing constraint of this feature: **the system-and-relay path is the product**, the
  manual run is a maintenance, development, rehearsal and recovery tool, and a convenience
  for the second must not add a line of code to the first. (**FR-015**)
- **Q3 — The display-confirmation script.** **A: it stays a shell script** and asks this
  package for the two values it needs through a documented query, instead of importing its
  internals. Its polling loop and retry settings stay next to the configuration file that
  tunes them. (**FR-016**)

### Session 2026-09-23 (second) — after reading the platform wrapper end to end

- **Q5 — What may the shared implementation contain?** **A: the two stage bodies and the
  machine selection, and nothing beyond them.** Arming the mains-cut relay and powering this
  controller off stay with the HTTP route. The platform wrapper's own header explains why:
  that route *"always arms a Shelly relay and fail-safes to HTTP 502 without powering the
  controller off when no Shelly answers — correct for a mains-cut deadline, useless at a venue
  with no Shelly. Here systemd is already powering the box off, so only steps 1-2 are needed."*
  The HTTP route also **ends by triggering this very transition**, so a shared sequence that
  armed the relay would arm it twice per shutdown.
- **Q6 — Is the new tool operator-facing?** **A: no.** It is an internal helper, invoked
  through the runtime environment's own interpreter, with no command on the operator's path.
  The operator command stays the platform package's existing `cuems-cluster-poweroff --force`,
  which already documents rehearsal. This also preserves the configuration override and the
  missing-package guard, both of which name that interpreter.
- **Q7 — Does a running show block this path?** **A: only a manual run, never a power-off.**
  See **The running-show guard** below.

**The constraint all three answers share**: this feature may not change what happens when the
wall switch is flipped or the power button is pressed. Every safeguard it adds must be
provably absent from that path.

## The running-show guard *(normative)*

**The product must always be able to power the venue off mid-show.** Cutting power during a
performance is one of the wall switch's primary intents — it is the operator's last resort,
and nothing this feature adds may stand between the switch and the relay.

That invariant decides the whole question, because the two callers are not symmetric:

| Caller | Is a show playing relevant? | Behaviour |
|---|---|---|
| **A power-off transaction** — wall switch, power button, `systemctl poweroff`, or the HTTP route's own final step | **No.** The machine is already going down; refusing would only skip the two stages, leaving the projectors lit and the machines running while the controller dies — the exact failure this work exists to remove | **Never checks.** No probe, no refusal, no new failure mode |
| **A manual rehearsal run** | **Yes.** Nothing is powering off; the operator is present and can answer | **Refuses while a project is playing**, unless the operator says otherwise on that run |

The discriminator is the **invocation**, and it is visible twice over: the platform wrapper
knows which case it is in (it already inspects the transaction), and the daemon is
unreachable during a transition anyway. Two independent signals agree, so the failure mode is
always "the guard does not fire" and never "the guard blocks a shutdown".

**The manual path is unguarded today**, and destructive: it stops the boot watchdog, blanks
the fleet and powers the machines off with no check at all. Adding the guard there closes an
existing hole; it adds nothing to the product path.

**No configuration key.** The override is per-run, not a setting. A key that permanently
disables a safety check is indistinguishable, months later, from the check never having
existed, and leaves no trace in the journal of the run that needed it. A per-run override is
one flag away when a recovery demands it and is recorded in that run's log.

**Three edge cases, settled:**

- the daemon cannot be reached on a manual run → **proceed**, with a warning; this is the same
  "cannot know" the transition path lives in permanently;
- the engine's state is unknown → **proceed**, with a warning. A wedged engine is the recovery
  case the manual run exists for; refusing there would disable the tool exactly when it is
  needed;
- the run is a no-op rehearsal → **skip the guard**; a run that changes nothing cannot harm a
  show, and refusing it is noise.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The power-off behaves identically, however it is triggered (Priority: P1)

An operator flips the wall switch, or presses the shutdown button, or the system powers off
for any other reason. Every route must select the same machines, skip the same ones for the
same reasons, refuse in the same situations, and log the same things.

**Why this priority**: today two implementations produce that behaviour, and they agree only
because one person wrote both recently. This story is the reason the feature exists.

**Independent Test**: drive the same topology through both entry points and compare the
selected machines, the refusals and the reported reasons.

**Acceptance Scenarios**:

1. **Given** a topology with two adopted machines, **When** the power-off runs through the
   system transition, **Then** it selects exactly the machines the HTTP route selects.
2. **Given** a topology whose machines are none of them adopted, **When** either route runs,
   **Then** both refuse, and both name the same machines and the same remedy.
3. **Given** an unreadable topology, **When** either route runs, **Then** both refuse without
   cutting mains, and neither can be overridden into proceeding.
4. **Given** any of the six documented outcomes, **When** exercised through both routes,
   **Then** the observable result differs in transport only — never in decision.

---

### User Story 2 - The logic is testable where it is maintained (Priority: P1)

A developer changes the power-off sequence and finds out whether it still works before a
venue does.

**Why this priority**: this is the largest untested stretch of the mains-cut path. The
previous feature's defect survived precisely because no suite could reach it.

**Independent Test**: run this repository's suite and observe coverage of both stages —
display power-off and machine power-off — including every refusal.

**Acceptance Scenarios**:

1. **Given** the relocated logic, **When** the suite runs, **Then** both stages execute in
   tests, with no hardware and no cluster.
2. **Given** a change to the sequence, **When** it breaks a documented outcome, **Then** a
   test fails before the change can be released.
3. **Given** the platform package, **When** its scripts are inspected, **Then** none of them
   contains this package's logic.

---

### User Story 3 - The maintenance run is safe, and there is still only one of it (Priority: P2)

An engineer rehearses or recovers with the platform package's existing manual command. It must
not have become a second way to power the venue down, and it must not go dark on a live show
by accident.

**Why this priority**: the manual path is the one this work makes reachable and testable. It is
also unguarded today — it stops the boot watchdog, blanks the fleet and powers the machines
off with no check at all.

**Independent Test**: run the existing manual command during a rehearsal, and during a playing
project, with and without the override.

**Acceptance Scenarios**:

1. **Given** an installed system, **When** an operator lists the commands available to them,
   **Then** there is exactly one way to power the venue down — the platform package's existing
   command — and the new tool is not on their path.
2. **Given** a manual run while a project is playing, **When** it is issued without the
   override, **Then** it refuses, names the project, and changes nothing.
3. **Given** the same situation with the override, **When** it is issued, **Then** it proceeds
   and the log records that the override was used.
4. **Given** a manual run with the daemon unreachable, or the engine state unknown, or in
   rehearsal mode, **When** it is issued, **Then** it proceeds with a warning rather than
   refusing.
5. **Given** the runtime environment is missing or overridden in configuration, **When** the
   wrapper runs, **Then** the existing guard and the existing override both behave exactly as
   before.

---

### User Story 4 - The platform package stays useful without this one (Priority: P2)

A machine that does not have this package installed — every non-controller — must keep
working exactly as before.

**Why this priority**: the platform package is installed everywhere; this one is
controller-only and optional by design. Breaking that is worse than the problem being fixed.

**Independent Test**: on a host without this package, trigger the power-off transition and
observe it reports one clear message and does not fail the shutdown.

**Acceptance Scenarios**:

1. **Given** a host without this package, **When** the system powers off, **Then** the
   platform script logs one clear line and completes without error.
2. **Given** the coordinated candidate installed as a set, **When** the system powers off,
   **Then** it performs the orderly sequence; and **Given** a mixed pair, **When** an operator
   attempts to install it, **Then** the package manager refuses rather than allowing a
   half-upgraded poweroff.

---

### Edge Cases

- **The tool is interrupted** part-way (the transition's own time limit expires): whatever it
  had already commanded stays commanded; it must not leave a half-armed state.
- **A machine is already off**: detected in one quick pass and not waited for, exactly as
  today — this fast path is easy to lose in a rewrite and is worth real time on every
  power-off.
- **A machine never goes quiet**: the tool reports which, and the sequence continues, as
  today.
- **A second entry describes this same host** under a different identifier: it must still be
  excluded from the machines to power off. This is not hypothetical — a past bug in another
  component produced exactly this shape.
- **No displays are configured, or none answers**: the display stage reports and returns
  without treating it as a failure.
- **The tool is run by hand while the daemon is also shutting down**: the second attempt is
  refused, naming the one in progress.
- **The lock holder is killed** (operator interrupt, out-of-memory, a timed-out transition):
  the lock MUST be gone, so the next power-off is not blocked by a corpse.
- **A manual run while the daemon is stopped but a show is somehow playing**: no guard is
  possible and the run proceeds. Accepted deliberately — it is the same condition the system
  transition runs under, where proceeding is correct.

## Requirements *(mandatory)*

### Functional Requirements

**One implementation**

- **FR-001**: Machine **selection** and the two **stage bodies** — displays off, machines off
  — MUST exist once and be executed by both callers. They are the parts that were duplicated.
- **FR-001a**: The shared implementation MUST NOT contain, and MUST NOT be able to reach:
  arming the mains-cut relay, pre-checking it, or powering this controller off. Those belong
  to the HTTP route alone, which ends by triggering the transition that runs the other caller
  — a shared sequence containing them would arm the relay twice per shutdown (Q5).
- **FR-002**: The documented selection outcomes MUST be reproduced identically through both
  routes: the decision, the refusal reasons, and the machines named. **Orchestration is
  deliberately not identical**: the HTTP route fans the machine power-off and the display
  power-off out concurrently, while the transition path runs displays fully first — the
  machines feed the projectors, so blanking them first puts "no signal" on stage.
- **FR-003**: Machine selection, self-exclusion, the already-off fast path and the per-stage
  timeout policy MUST each exist in exactly one place.
- **FR-003a**: The transition path's own sequencing MUST be preserved verbatim: displays
  before machines; the single liveness pass (`max_wait_s=0`, one confirmation, its poller's
  logger silenced) before the fan-out; and the fan-out addressed **only to machines that
  answered alive**.

**Relocation**

- **FR-004**: The platform package's power-off script MUST contain none of this package's
  logic; it MUST invoke this package's tool instead.
- **FR-005**: The platform package's display-confirmation script MUST no longer reach into
  this package's internals (see FR-016).
- **FR-006**: The platform script MUST keep the responsibilities that are genuinely its own:
  distinguishing a power-off from a restart, its own enable switch, its own time limits, and
  the check that lets a host without this package installed complete its shutdown cleanly.

**The tool**

- **FR-007**: The tool MUST be invoked through the runtime environment's own interpreter, the
  one the platform wrapper already names in its configuration, and MUST NOT install any
  command on the operator's path. This keeps three existing properties intact: the
  configuration override of that interpreter, the missing-package guard that tests it, and the
  rule that there is exactly **one** way for an operator to power the venue down (Q6).
- **FR-008**: The tool MUST document, in its own help output, what it does, its stages, its
  options and the meaning of every exit status.
- **FR-009**: The tool MUST report its outcome through exit status, distinguishing at least:
  completed (including "there was nothing to do"); a configuration or precondition failure;
  a **refusal**; and completed-but-some-machines-never-went-quiet.
- **FR-009a**: The platform wrapper MUST translate those statuses into log lines and **still
  exit 0 on every path that runs during a power-off transaction**, including stage failures —
  an `ExecStop` that fails must never fail the stop it is part of. A refused **manual** run MAY
  exit non-zero, because no transition is in progress and an operator should see a failure.
- **FR-010**: The tool's output MUST remain readable line by line, because the calling script
  forwards each line to the system log.
- **FR-011**: The tool MUST offer a rehearsal mode that exercises the whole sequence and
  changes nothing.
- **FR-012**: The tool MUST take the caller's timing and policy values as arguments rather
  than reading the platform package's configuration file, which stays where it is.
- **FR-013**: The shared implementation MUST assume neither execution identity — it runs as
  the privileged account under the system transition and as the service account inside the
  daemon, and reads no identity-dependent path directly. Because the tool is never invoked
  directly by an operator (FR-007), no unprivileged path to it exists: the only callers are
  the privileged transition and the daemon itself.

**Safety decisions**

- **FR-014**: Concurrent power-offs MUST NOT interleave, across processes as well as within
  one. One exclusive machine-wide lock MUST cover **a whole power-off sequence**, not a part
  of one: where the sequence is invoked as two separate stages, the **caller** holds the lock
  across both and the stages are told not to take it themselves. Every entry point MUST refuse
  — reporting that a power-off is already in progress — rather than wait. The lock MUST be
  released automatically if its holder dies, so a killed process cannot block the next
  shutdown.
- **FR-015**: A **manual** run MUST refuse while a project is playing, unless the operator
  overrides it on that run. A run that is part of a power-off transaction MUST NOT check at
  all. The guard MUST live in the tool's entry point, never in the shared implementation, and
  MUST be requested by the caller rather than inferred — so the transition path cannot reach
  it even by accident (see **The running-show guard**).
- **FR-015a**: The override MUST be a per-run argument, NOT a configuration key, and the run
  that uses it MUST say so in its log.
- **FR-015b**: The guard MUST fail open: an unreachable daemon, an unknown engine state, or a
  no-op rehearsal all **proceed** with a warning rather than refusing.
- **FR-015c**: **The power-off transaction MUST always complete, whatever is playing.** No
  requirement in this feature may add a condition between the wall switch, the power button or
  a `systemctl poweroff` and the machine going down. Powering the venue off mid-show is one of
  the wall switch's primary intents.
- **FR-016**: The display-confirmation script MUST stop importing this package's internals,
  and MUST instead obtain the two values it needs through a documented query this package
  provides. Its polling loop, its retry settings and its use of the local API stay as they
  are.

**Preserved behaviour the relocation must not drop**

- **FR-023**: The display stage MUST keep obeying the configuration switch that disables it,
  reporting that it is disabled and succeeding without touching any device. Venues keep
  configured fleets dark deliberately.
- **FR-024**: Both power-off initiators — the daemon and the system transition — MUST use
  **one** SSH host-key store, so a re-imaged machine cannot be trusted by one and unknown to
  the other, discovered mid-shutdown.
- **FR-025**: The lock MUST be released before the daemon triggers the local power-off, because
  that command re-enters the same sequence through the system transition. A re-entrant
  transition MUST acquire the lock cleanly; a failure to acquire MUST mean another power-off is
  genuinely in progress.

**Findings fixed in passing** (each is a defect this move must not carry across)

- **FR-017**: The script's own reading of this host's identity document MUST be replaced by
  the value the owning library already provides. It is the fifth copy of the same identity
  model in the ecosystem.
- **FR-018**: That reading currently hides every error and continues with an incomplete
  answer. The replacement MUST report failure instead.
- **FR-019**: The self-exclusion pass that matches by address and by name MUST be preserved,
  with its reason recorded: it now exists solely to catch a duplicate entry describing this
  host under a second identifier.
- **FR-020**: The two liveness checks have deliberately different confirmation policies. Both
  MUST be preserved exactly, with the reasoning that accompanies them.

**Adoption**

- **FR-021**: Both halves MUST land as one coordinated candidate, in the versions already
  open, under the ecosystem's shared release tag — not as a staged rollout. The existing
  version relationship between the two packages already forces them to move together; no new
  one is added. The candidate is proven as a **set**, on real machines, before release.
- **FR-021a**: Every check that needs real hardware, a real cluster or a human MUST be
  recorded on a **hardware-verification ledger** in this repository, covering this feature and
  the previous one, in the shape the sibling repositories already use: each entry states what
  to do, what it proves and why the automated suite cannot, every box starts unchecked, and a
  deferral is written down rather than implied. A per-host record sheet MUST accompany it so a
  technician can work a production machine without reading either feature's prose.
- **FR-022**: No operator-visible behaviour of the shutdown may change: not the HTTP surface,
  not the wall-switch behaviour, not the sequence, not the timings.

### Key Entities

- **Power-off sequence**: the ordered, partly irreversible procedure — displays off, machines
  off, confirm quiet, arm the mains cut, power off locally. Owned here, invoked from two
  places.
- **Stage**: one half of that sequence (displays; machines), separately invocable because the
  calling script bounds each with its own time limit.
- **Outcome**: what the sequence decided — completed, refused (with a reason), or completed
  with machines still up. Reported as exit status and as readable output.
- **Caller**: either the system's power-off transition (privileged, non-interactive, log
  forwarded) or an operator at a terminal.

## Success Criteria *(mandatory)*

- **SC-001**: The platform package contains zero lines of this package's logic; verified by
  inspection of both scripts.
- **SC-002**: Both stages are exercised by this repository's automated tests, with no cluster
  and no hardware — up from none today.
- **SC-003**: Each of the six power-off outcomes is verified through **both** entry points,
  and the decisions match.
- **SC-004**: An operator has exactly one command that powers the venue down, unchanged from
  today; the new tool appears on no operator path.
- **SC-004a**: A manual run refuses during a playing project, proceeds with the override, and
  proceeds with a warning when the daemon is unreachable, the engine state is unknown, or the
  run is a rehearsal.
- **SC-005**: A host without this package installed still completes its shutdown, logging one
  clear line.
- **SC-006**: The coordinated candidate performs an orderly power-off on a real controller,
  and a mixed pair is refused by the package manager.
- **SC-012**: With the display switch off, a power-off through either entry point leaves every
  device untouched and still reports success.
- **SC-013**: A power-off initiated through the API completes its re-entrant transition with
  both stages running — the lock never refuses the transition the daemon itself caused, and the
  relay is armed exactly once per shutdown.
- **SC-014**: **A power-off during a playing project completes, through every trigger the
  product uses** — wall switch, power button and `systemctl poweroff` — with no condition added
  by this feature. Verified on hardware.
- **SC-011**: Every hardware- or human-dependent check for this feature **and the previous
  one** appears on one ledger with an explicit state; none is left to prose. A technician can
  run the whole ecosystem's validation from the three repositories' ledgers in one pass.
- **SC-007**: The four carried-over defects (FR-017 to FR-020) are each demonstrably fixed or
  preserved as specified.
- **SC-009**: A second power-off attempt, from either entry point, is refused while one is in
  progress; and killing the holder leaves the next attempt able to proceed.
- **SC-010**: The running-show guard is demonstrably unreachable on the system-transition
  path — shown by code path, not only by test.
- **SC-008**: On real hardware, an orderly power-off triggered by the wall switch and one
  triggered through the API select the same machines and produce the same log decisions.

## Assumptions

- The previous feature's six documented outcomes are the behaviour to reproduce; this feature
  does not revisit them.
- The platform package's configuration file, its units, its enable switch and its privilege
  files stay where they are. Only code moves.
- The calling script continues to bound each stage with its own time limit, so the tool does
  not need to implement an overall deadline.
- The display stage remains safe to run twice in a row (the HTTP shutdown's own local
  power-off re-enters the same transition), because it checks state before acting.
- Both entry points continue to read this package's own configuration file for everything
  except the caller-supplied timing and policy values.
- The daemon is reachable on the local API whenever a human is running the tool by hand, and
  unreachable when the system transition runs it — that difference is what makes the Q2 guard
  both possible and self-disabling.
- A lock under a runtime directory that is cleared on boot is sufficient; no power-off
  survives a reboot, so stale state across boots is not a concern.

## Out of Scope

- Moving the platform package's units, configuration file, power-button policy or privilege
  files — deferred to the ecosystem-wide service-ownership work.
- Replacing the network-based machine power-off with the engine-native broadcast; this
  feature makes that a one-place change rather than a two-place one.
- Any change to the HTTP surface, the wall-switch script, or the shutdown's observable
  behaviour.
- Merging the two configuration files.
- Adding any operator-facing command: the new tool is internal, and the existing manual
  command stays the only way to power the venue down by hand.

## Dependencies

- **The platform package** — its power-off and display-confirmation scripts are edited in the
  same work and land in the same coordinated candidate (FR-021).
- **The previous feature** (node-role parser migration) — already merged; its selection
  function and its six outcomes are the starting point.
