<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Feature Specification: Node-role parser migration

**Feature Branch**: `001-node-role-parser`

**Created**: 2026-09-23

**Status**: Draft — clarified 2026-09-23 (Q1, Q2, Q3 answered; see Clarifications)

**Input**: Move `cuems-power-bridge` off its private network-map parser and onto the
node model's owning library, restore the two features a cluster-wide vocabulary rename
silently disabled, and make an empty node selection impossible to mistake for success.

## Context

The cluster topology document (`network_map.xml`) describes every machine in a CUEMS
cluster and what role it plays. Its role field was renamed ecosystem-wide, and every
installed controller has had its document converted automatically on package upgrade.

This component reads that document with its own private reader, which still looks for the
retired field. On a converted document the reader finds no machine of any role and
reports nothing wrong. Two operator-visible behaviours depend on that selection, and both
are currently broken **in the field, on every upgraded controller**:

1. **Orderly cluster power-off** selects no machines to shut down, skips the check that
   they went quiet, reports success, and arms the hardware timer that cuts mains power —
   so the other machines in the cluster lose power while running.
2. **Boot auto-load** believes the cluster has no other machines, so it loads the show
   before those machines are ready, reproducing a regression that a previous release
   already fixed once.

Neither produces an error, a warning, or a failed test.

## Clarifications

### Session 2026-09-23

- **Q1 — What must happen when the topology yields no machines to power off?**
  **A:** A controller-only system MUST shut down normally. If the topology document
  contains only the self entry, that is a valid cluster, not an anomaly, and the shutdown
  proceeds. (See **Shutdown target selection**, Case 1.)
- **Q2 — Who provides `/etc/cuems/settings.xml`, which the new reading path requires?**
  **A:** `cuems-utils` will ship it into `/etc/cuems`, so every host that has the CUEMS
  packages has the file. It therefore becomes a declared, versioned dependency of this
  component rather than an operator-hand-placed file. Confirmed 2026-09-23: this is part of
  the **same unreleased refactor**, not a later release — so is the platform package's half.
  (See **FR-018**, **Dependencies**.)
- **Q3 — Should the bridge honour the per-node `adopted` flag?**
  **A:** Yes. Shutdown targets adopted machines only — **unless `force` is set, which
  means "power off every machine in the system"**. (See **Shutdown target selection**,
  Cases 2–4, and **The `force` flag**.)

## Shutdown target selection *(normative — this section defines operator-visible behaviour)*

This section is the authoritative description of **which machines a shutdown powers off,
and when a shutdown refuses to run at all**. It is written to be readable by an operator,
not only by an implementer.

### Vocabulary

| Term | Meaning |
|---|---|
| **self entry** | The entry in the topology document describing *this* controller, identified by this host's own identifier (from the host identity document), with address and name matching as corroboration. |
| **other machine** | Any entry whose role is the non-controller role, excluding the self entry. |
| **adopted machine** | An "other machine" whose entry is marked as adopted into this cluster. |
| **unadopted machine** | An "other machine" present in the document but not marked adopted — a machine that has been seen but not taken into the cluster. |
| **`force`** | The explicit override an operator (or the wall switch) sends with a shutdown request. See **The `force` flag**. |

### The five cases

| # | Situation | What happens | Machines powered off |
|---|---|---|---|
| **1** | **Controller-only system.** The document is read successfully and contains only the self entry (no other machine at all). | **Shutdown proceeds normally.** This is a supported, ordinary configuration — not an anomaly and not an error. No machines are commanded off, so there is nothing to wait for; the mains-cut timer is armed and the controller powers off. The log and status say plainly that this is a controller-only system. | none (correctly) |
| **2** | **Normal cluster.** The document is read successfully, other machines exist, and at least one is adopted. `force` not set. | **Shutdown proceeds.** Every **adopted** machine is commanded off and confirmed off the network before the mains-cut timer is armed. Unadopted machines are **not** targeted, and each one is named in the log as deliberately skipped. | adopted machines |
| **3** | **Nothing adopted.** The document is read successfully and other machines exist, but **none** is adopted. `force` not set. | **Shutdown REFUSES.** Mains power is not cut. The refusal names the machines that were skipped, states that none is adopted, and states the remedy: adopt them, or repeat the request with `force`. Rationale: the document says this cluster has machines; powering off the controller and cutting mains while they run is exactly the field failure this feature exists to remove. | none — shutdown refused |
| **4** | **Forced shutdown.** `force` is set, and the document was read successfully. | **Shutdown proceeds and targets EVERY other machine — adopted or not.** `force` means "power off every machine in the system". It also retains its existing meaning of overriding the refusal to shut down while a project is running. | every other machine |
| **5** | **The topology could not be read.** Any failure of the read: the document is in the retired vocabulary, is invalid against the schema, is missing, has an incomplete machine entry, the host identity document is missing, or this host has no entry in the document. | **Shutdown REFUSES, and `force` does NOT override this.** Mains power is not cut. The refusal names the document and the remedy. Rationale: **`force` overrides policy, never evidence.** A read that failed produced no knowledge of the cluster, so there is no basis on which to power anything off — including this controller, whose mains would be cut on machines nobody has accounted for. | none — shutdown refused |

### Why Case 1 and Case 5 are different

Both end with "no machines were powered off", and today they are indistinguishable. After
this feature they never are:

- **Case 1** is an *answer*: the document was read and it says this cluster is one machine.
  The shutdown is complete and correct.
- **Case 5** is the *absence of an answer*: nothing is known about the cluster. The
  shutdown refuses.

Any implementation in which these two produce the same log line, the same status, or the
same outcome is wrong, however the code is arranged.

### The `force` flag

`force` has **two** effects, and an operator must be able to see both:

1. It overrides the refusal to shut down while a project is running (existing behaviour).
2. It overrides the adoption filter: **every** machine in the document is powered off,
   adopted or not (new in this feature).

It overrides **no** read failure (Case 5).

**Consequence for the wall switch, which MUST be documented for operators:** the shipped
Shelly script sends `force` by default, so *flipping the physical switch powers off every
machine in the document and does not stop for a running show*. The installer's safe mode
turns that default off, and a safe-mode switch then behaves like Case 2 and 3 — adopted
machines only, refusing while a project runs. This is a deliberate choice: a physical
switch is the operator's last resort and must always be able to kill the venue.

### Boot readiness gate and adoption

The boot readiness gate (which decides how long to wait before loading the show) waits for
**adopted** machines that carry an address. Unadopted machines are not waited for: nothing
can force them at boot, and a machine that is not in the cluster cannot be required to
join it before the show loads. The single-controller settle path is taken **only** when the
document was read successfully and lists no adopted machine with an address — never
because a read failed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The power switch shuts the whole cluster down (Priority: P1)

An operator ends the day and flips the venue's power switch (or presses the shutdown
button on their control surface). Every machine that belongs to the cluster must be
commanded to shut down, confirmed off the network, and only then may mains power be cut.

**Why this priority**: this is the failure that is live in the field today and it is
physical: machines lose mains power while still running, which risks the filesystem on
every one of them. It is also the failure that reports success, so nobody is alerted.

**Independent Test**: point the component at a converted topology document describing a
controller and two adopted machines, trigger a shutdown in the no-op rehearsal mode
(`dry_run`), and confirm the recorded target list names both machines.

**Acceptance Scenarios**:

1. **Given** a converted document listing the controller and two adopted machines,
   **When** an operator triggers shutdown, **Then** both machines are commanded to power
   off, each is confirmed off the network (or reported as stuck), and only then is the
   mains-cut timer armed. *(Case 2)*
2. **Given** the same document, **When** the shutdown completes, **Then** the
   operator-visible status and the log name exactly the machines that were selected, and
   how many were skipped as unadopted.
3. **Given** a document containing only the self entry, **When** an operator triggers
   shutdown, **Then** the shutdown completes normally and is reported as a controller-only
   system, with no warning and no error. *(Case 1)*
4. **Given** a document listing two machines, neither adopted, **When** an operator
   triggers shutdown without `force`, **Then** the shutdown is refused, mains power is not
   cut, and the refusal names both machines and the remedy. *(Case 3)*
5. **Given** that same document, **When** the operator repeats the request with `force`,
   **Then** both machines are powered off and the shutdown proceeds. *(Case 4)*
6. **Given** a document the component cannot read, **When** an operator triggers shutdown
   with or without `force`, **Then** the shutdown is refused, mains power is not cut, and
   the refusal names the document and the remedy. *(Case 5)*

---

### User Story 2 - The show is loaded only once the cluster is ready (Priority: P1)

A controller boots (after a power cut, or a scheduled start) and automatically loads the
configured show. It must wait for the cluster's machines to join before loading, or the
show comes up with those machines excluded and the operator must restart engines by hand
before the show can run.

**Why this priority**: independent of User Story 1 — it fails and recovers separately, and
it silently reintroduces a regression a previous release was made to fix. A verified
power-off says nothing about this path.

**Independent Test**: point the component at a converted document listing two adopted
machines with addresses, start it, and confirm it waits for both before loading.

**Acceptance Scenarios**:

1. **Given** a converted document listing two adopted machines with addresses, **When** the
   controller boots, **Then** the component waits for both engines to join before loading
   the show, and says which ones it is waiting for.
2. **Given** a document that lists no adopted machine with an address, **When** the
   controller boots, **Then** the component takes its documented single-controller settle
   path and says so.
3. **Given** a document listing an unadopted machine, **When** the controller boots,
   **Then** the component does not wait for it, and says it was skipped.
4. **Given** a document that cannot be read, **When** the controller boots, **Then** the
   component MUST NOT take the single-controller path, the read failure is reported, and
   auto-load does not proceed on an unknown topology.

---

### User Story 3 - An upgraded cluster keeps working, in both directions (Priority: P2)

A site upgrades its packages. During and after the upgrade, the orderly power-off tool
shipped by the platform package (which drives this component's library) must keep working:
it must not fail part-way through a shutdown transaction because the two packages disagree
about the node model.

**Why this priority**: the failure window is an upgrade, and its blast radius is a poweroff
transaction in progress. It cannot be fixed after the fact from a machine that has already
powered off.

**Independent Test**: run the platform package's power-off tool against this component's
library on a converted document and confirm it selects the same machines, then verify the
packaging refuses the version combinations that cannot work.

**Acceptance Scenarios**:

1. **Given** the platform tool and this component both upgraded, **When** a power-off runs,
   **Then** the tool selects the same machines as this component does, under the same five
   cases above.
2. **Given** a version combination that cannot work together, **When** an operator installs
   it, **Then** the package manager refuses it rather than allowing a shutdown to fail
   mid-transaction.

---

### User Story 4 - The next rename is loud (Priority: P2)

A future change to the topology document's vocabulary, in a repository this one does not
own, must not be able to disable a feature here silently.

**Why this priority**: the durable deliverable. The specific field rename is one instance
of a class this ecosystem has now hit repeatedly; the guard outlives the instance.

**Independent Test**: present the component with a document in the retired vocabulary and
confirm it refuses it with a named, actionable error instead of quietly selecting nothing.

**Acceptance Scenarios**:

1. **Given** a document still in the retired vocabulary, **When** the component reads it,
   **Then** it fails with an error naming the document, the offending machine and the
   remedy — never an empty answer.
2. **Given** the test suite, **When** it is run against the pre-migration reader with a
   current-vocabulary document, **Then** at least one test fails — and that failing run is
   recorded as evidence.

---

### Edge Cases

- **A machine that cannot be named**: a machine whose entry carries none of the resolvable
  names is reported as unresolvable, never silently dropped, and never addressed by its raw
  address.
- **A machine with no address**: for the readiness gate only, such a machine is skipped
  with a warning; it remains a shutdown target if it is adopted (or if `force` is set).
- **The controller's own entry**: the controller must never command itself off over the
  network, and must never wait to observe itself absent.
- **An adopted machine that is already off**: it is confirmed down immediately and the
  shutdown does not wait out the timeout for it.
- **A document edited while the component is running**: a changed document is picked up on
  the next read rather than served from a stale cache.
- **The host identity document is missing**: Case 5 — topology-dependent operations refuse
  with a named error. After this feature, this is a packaging fault, because the identity
  document is shipped (Q2).
- **The host's own entry is missing from the topology document**: Case 5 — refused and
  reported as a configuration fault. The component continues to serve every function that
  does not need the topology.
- **A machine entry that is incomplete**: Case 5 — the document is rejected as invalid
  rather than yielding a partial machine, including a hand-maintained document that
  predates the completeness rules.

## Requirements *(mandatory)*

### Functional Requirements

**Reading the topology**

- **FR-001**: The component MUST obtain cluster topology through the owning library's
  public interface, and MUST NOT carry its own reader for that document.
- **FR-002**: The component MUST NOT define its own copy of the role vocabulary; it MUST
  use the owning library's.
- **FR-003**: A document in the retired vocabulary, an unreadable document, or an
  incomplete machine entry MUST produce a named, actionable failure — never an empty or
  partial answer.
- **FR-004**: A failure to read the topology MUST be distinguishable, in behaviour, in
  status and in logs, from a successfully read topology that contains no other machine
  (Case 5 versus Case 1).

**Selecting machines**

- **FR-005**: Machines to power off MUST be addressed by resolvable name, tried in the
  established order, and MUST NOT be addressed by the raw address recorded in the document.
  Machines resolving to no name MUST be reported, not dropped.
- **FR-006**: Machines to wait for at boot MUST be identified by the address recorded in
  the document, which the readiness signal matches on.
- **FR-007**: Both selections MUST filter on the role field of the current vocabulary.
- **FR-008**: Shutdown MUST target **adopted** machines only, except under `force`
  (FR-011). Each unadopted machine skipped MUST be named in the log.
- **FR-009**: The boot readiness gate MUST wait for **adopted** machines with an address
  only, and MUST name any machine it skips.

**Not mistaking nothing for success**

- **FR-010**: Shutdown behaviour MUST follow the five cases in **Shutdown target
  selection** exactly:
  - Case 1 (self entry only, read OK) — proceed, reported as a controller-only system.
  - Case 2 (adopted machines exist, no `force`) — proceed, targeting adopted machines.
  - Case 3 (other machines exist, none adopted, no `force`) — **refuse**; do not arm
    mains-cut; name the skipped machines and the remedy.
  - Case 4 (`force`) — proceed, targeting every other machine.
  - Case 5 (topology read failed) — **refuse**, regardless of `force`; do not arm
    mains-cut; name the document and the remedy.
- **FR-011**: `force` MUST override (a) the refusal to shut down while a project is
  running and (b) the adoption filter. It MUST NOT override a failed topology read.
- **FR-012**: The step that confirms machines have gone quiet MUST run whenever a shutdown
  proceeds with one or more targets — it MUST NOT be skipped because the selection was
  empty or short. In Case 1 there are no targets and no wait, which MUST be stated in the
  log rather than implied by silence.
- **FR-013**: The boot readiness gate MUST enter its single-controller settle path only
  after a successful read that lists no adopted machine with an address.
- **FR-014**: Operator-visible status MUST expose enough state to distinguish, without
  reading the log: a normal cluster shutdown, a controller-only shutdown, a refusal for
  lack of adopted machines, a refusal for a failed read, and a forced shutdown. It MUST
  also expose how many machines were found, how many were adopted, and how many were
  targeted.

**Compatibility across packages**

- **FR-015**: The library surface the platform package drives MUST either keep working
  unchanged, or its caller MUST be changed in the same release; a mixed pair MUST NOT be
  installable.
- **FR-016**: The dependency on the owning library MUST be a real, non-optional dependency
  with both a lower and an upper bound, consistently expressed in the language packaging
  and the system packaging. The lower bound MUST be at least the version that ships the
  host identity document (Q2).
- **FR-017**: The installed package MUST NOT ship a second copy of anything another CUEMS
  package installs into the shared runtime.

**Preconditions of the new reading path**

- **FR-018**: The host identity document is a **declared dependency**, not an operator
  responsibility. When it is absent the component MUST refuse topology-dependent
  operations with a named error identifying the missing file and the package that provides
  it, and MUST remain available for every function that does not depend on topology.
  Silently swallowing the failure and continuing with a reduced answer is prohibited.
- **FR-019**: A topology document with no entry for this host MUST be reported as a
  configuration fault (Case 5).

**Evidence**

- **FR-020**: The suite MUST contain fixtures in both the retired and the current
  vocabulary, each valid against the owning schema, plus fixtures covering all five
  shutdown cases and the adopted/unadopted distinction.
- **FR-021**: At least one test MUST fail against the pre-migration reader, and that
  failing run MUST be recorded in the feature's artifacts.
- **FR-022**: Both restored behaviours MUST be verified separately, on real hardware, and
  the result stated.
- **FR-023**: Zero occurrences of the retired vocabulary MUST remain in shipped code or
  shipped prose, counted, with the exempt set enumerated and justified.

**Documentation (operator-facing)**

- **FR-024**: The shipped documentation MUST state the five cases, the two meanings of
  `force`, and the wall-switch consequence (the physical switch sends `force` by default,
  so it powers off every machine and does not stop for a running show; safe mode changes
  this). An operator MUST be able to predict what a shutdown will do without reading code.

### Key Entities

- **Cluster topology document**: the list of machines in the cluster, owned and versioned
  by another repository. Each machine carries a stable identifier, a role, an adoption
  flag, a set of names, and an address.
- **Machine role**: the current vocabulary distinguishing the controller from the other
  machines and from an unconfigured machine.
- **Adoption flag**: whether a machine has been taken into this cluster. Decides shutdown
  targeting (unless forced) and readiness waiting.
- **Shutdown target**: a machine selected to be powered off, addressed by resolvable name.
- **Readiness peer**: an adopted machine whose engine must join the cluster before the show
  loads, identified by address.
- **Host identity document**: the per-machine document naming this host's identifier,
  required by the reading path and, after this feature, shipped by the owning library's
  package.

## Success Criteria *(mandatory)*

- **SC-001**: On a converted topology document, a rehearsed shutdown selects 100% of the
  adopted machines listed — today it selects 0%.
- **SC-002**: On a converted topology document, the boot readiness gate waits for 100% of
  the adopted machines with addresses before loading the show — today it waits for none.
- **SC-003**: Every way the topology read can fail produces an operator-visible message
  that names the document and the remedy; no failure mode produces a silent empty answer.
- **SC-004**: Mains power is never cut following a failed topology read, with or without
  `force`, in any tested scenario.
- **SC-005**: A controller-only system shuts down and boots its show automatically, with no
  new warning and no error (Case 1).
- **SC-006**: A shutdown request against a document whose machines are all unadopted is
  refused without `force` and succeeds with it, in both cases naming every machine involved
  (Cases 3 and 4).
- **SC-007**: A document in the retired vocabulary is rejected within one operation, with a
  message that names the conversion tool.
- **SC-008**: The suite passes, and includes a recorded run in which the current-vocabulary
  fixture fails against the pre-migration reader.
- **SC-009**: An operator cannot install a combination of this component and the platform
  package that would fail part-way through a shutdown.
- **SC-010**: Both restored behaviours are demonstrated separately on a real cluster and
  the demonstration is recorded.
- **SC-011**: A count of the retired vocabulary across shipped code and shipped prose
  returns zero, with exemptions listed.
- **SC-012**: A reader of the shipped documentation can state, for each of the five cases
  and for `force`, what a shutdown will do — verified by review against this section.

## Assumptions

- The owning library's public configuration interface is available at a version that
  provides the typed node model (including the adoption flag), rejects the retired
  vocabulary, **and ships the host identity document**; this feature does not ship until
  that version is released (ecosystem release gate).
- The platform package's power-off tool remains the only external consumer of this
  component's library surface. Absorbing that tool into this package is a separate,
  deferred feature and is explicitly out of scope here.
- The two name-resolution policies are deliberate and field-learned, and this feature
  preserves them exactly: power-off resolution ignores the recorded address; the readiness
  gate trusts it.
- A topology document without an entry for this host is treated as a configuration fault
  rather than a reason to exit: the component still serves transport commands, display
  control and status. Only the topology-dependent operations refuse.
- Case 3's refusal is the correct default because an unadopted machine in the document is
  evidence that machines exist; `force` is the documented way to power them off anyway.
- The readiness gate's adoption filter follows from Q3 by analogy (no `force` exists at
  boot). If operators expect unadopted machines to be waited for, this is the one decision
  to revisit.
- No change to the operator-facing HTTP surface is implied beyond the refusal reasons and
  the status detail required by FR-014, which are additive.
- The retired-vocabulary default hostname in the legacy client entry point is a network
  name, not a role value, and is out of scope.

## Out of Scope

- Absorbing the platform package's power-off and display-confirmation machinery, its units,
  its configuration or its privilege files into this package.
- Replacing the network-based node power-off with an engine-native broadcast.
- Re-implementing or re-testing the node model here; that model belongs to its owning
  library.
- Renaming this component (see the constitution's direction-of-travel section).
- Changing who may set `force` or how the wall switch is wired; this feature documents the
  existing arrangement and adds a second meaning to the flag.

## Dependencies

- **Owning library (`cuems-utils`)** — the public configuration interface, the role
  vocabulary, the adoption flag, the schema, **and the shipped host identity document**
  (Q2). All of it is in flight in the same unreleased refactor, so this is a coordinated
  cutover rather than a wait; that version is the lower bound of FR-016.
- **Platform package (`cuems-common`)** — its power-off tool is changed in the same
  unreleased version; the packaging expresses the pairing mechanically (FR-015).
- **Release shape** — the three halves are unreleased and land together. None ships alone,
  and nothing in the feature ships before every flow of the wider migration lands.
