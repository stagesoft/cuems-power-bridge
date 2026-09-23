<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Tasks: Cluster power-off CLI — one shutdown, two entry points

**Input**: Design documents from `/specs/002-cluster-poweroff-cli/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/cli.md](./contracts/cli.md),
[quickstart.md](./quickstart.md)

**Tests**: **Included and mandatory.** The spec's whole second story is "the logic is testable
where it is maintained" (SC-002), and the equivalence tests are the deliverable that makes
"the wall switch and the API cannot disagree" checkable rather than asserted.

**Organization**: grouped by user story. The governing constraint runs through all of them:
**the systemd + Shelly path is the product**; every safeguard this feature adds must be
provably absent from it.

**Release**: everything lands in the versions already open — `cuems-power-bridge 0.3.1-1`,
`cuems-common 1.3.0-23` — under the coordinated `xml-refactor-merge-candidate` tag. The final
phase creates that tag here and relocates it in `cuems-common`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from [spec.md](./spec.md)

## Path Conventions

`src/cuemspowerbridge/` and `tests/` at the repository root; feature artifacts under
`specs/002-cluster-poweroff-cli/`. The sibling is `../cuems-common`.

**Runner**:

```bash
uv run --python 3.11 --with pytest --with pytest-asyncio --with pytest-mock \
  --with aiohttp --with websockets --with python-osc --with ../cuems-utils \
  python -m pytest -q
```

Baseline: **185 passed** (feature 001).

---

## Phase 1: Setup (Shared Infrastructure)

- [ ] T001 Record the baseline suite state and the runner line in `specs/002-cluster-poweroff-cli/evidence/baseline-suite.txt` (must be 185 passed; never start on a red suite)
- [ ] T002 [P] Add the tmpfiles rule `debian/tmpfiles.d/cuems-power-bridge.conf` — `d /run/cuems-power-bridge 0770 cuems cuems - -` (research R2: `/run` is root-owned, so the daemon running as `cuems` cannot create its own lock directory)
- [ ] T003 [P] Ship the rule from `debian/cuems-power-bridge.install` to `usr/lib/tmpfiles.d/`, and run `systemd-tmpfiles --create` in `debian/cuems-power-bridge.postinst` so the directory exists before the next boot
- [ ] T004 [P] Capture a pre-change copy of `src/cuemspowerbridge/bridge.py` lines 508-644 into `specs/002-cluster-poweroff-cli/evidence/sequence-before.txt`, so Phase 2's extraction can be reviewed as a **move** rather than a rewrite (quickstart §2)

**Checkpoint**: the lock has a home; the sequence's "before" is on record.

---

## Phase 2: Foundational (Blocking Prerequisites)

**⚠️ Blocks every user story. T007–T011 are the extraction — reviewed as a move.**

- [ ] T005 Implement `src/cuemspowerbridge/shutdown_lock.py`: a non-blocking exclusive `flock` over `/run/cuems-power-bridge/shutdown.lock`, as a context manager that raises a typed "already held" error rather than waiting, and reports a precondition failure when the directory is absent (data-model §4)
- [ ] T006 [P] Test the lock in `tests/test_shutdown_lock.py`: a second acquisition fails immediately; the lock is released when the holding process dies; a missing directory is a clear precondition error, not an unlocked run
- [ ] T007 Create `src/cuemspowerbridge/cluster_shutdown.py` with `ShutdownContext` (`cfg`, `displays`, `shelly`, `progress` — and deliberately **no** `engine`) and `ShutdownOutcome` per [data-model.md](./data-model.md) §1, §3
- [ ] T008 Move the shared six-case decision into `src/cuemspowerbridge/cluster_shutdown.py` as a pure function of `(selection, force)` returning `ShutdownDecision` (data-model §2), leaving the HTTP/CLI mapping to the callers
- [ ] T009 Move the sequence from `src/cuemspowerbridge/bridge.py:508-644` into `run_cluster_shutdown()` in `src/cuemspowerbridge/cluster_shutdown.py` — steps, order, timeouts and log lines unchanged; state reported through `progress` instead of `_set_state`
- [ ] T010 Add self-exclusion to `src/cuemspowerbridge/cluster_shutdown.py` per [data-model.md](./data-model.md) §5 — uuid via `NodeView.is_self`, then **exact** address-set membership (never a substring test), then hostname/FQDN — with the duplicate-entry reason recorded in a comment (FR-019)
- [ ] T011 Rewrite `handle_shutdown` in `src/cuemspowerbridge/bridge.py` as a thin caller: token, engine guard, **the new lock**, topology read, shared decision, `run_cluster_shutdown`, map `ShutdownOutcome` to HTTP and `/status`
- [ ] T012 [P] Test the sequence in isolation in `tests/test_cluster_shutdown.py`: both stages, the partial-resolution marker, the reachability policies (FR-020), and self-exclusion including the duplicate-entry case
- [ ] T013 Implement `src/cuemspowerbridge/scripts/cluster_poweroff.py` per [contracts/cli.md](./contracts/cli.md) — `--stage {displays,nodes,all}`, `--node-wait`, `--projector-timeout`, `--force`, `--dry-run`, `--transition`, `-v`; module docstring carrying the usage synopsis and the exit-status contract; `argparse` with `prog=`; `main()` (match `scripts/test_projector.py`)
- [ ] T014 Implement the exit-code mapping in `src/cuemspowerbridge/scripts/cluster_poweroff.py`: 0 complete · 1 usage · 3 precondition · 4 refused · 5 stuck hosts; line-oriented unbuffered stdout
- [ ] T015 [P] Implement `src/cuemspowerbridge/scripts/config_get.py` — `--get <key>`, one bare value on stdout, exit 3 on unknown key or unreadable config; the key is the argument, never the value (FR-016, research R5)
- [ ] T016 [P] Add both console entries to `pyproject.toml` `[tool.poetry.scripts]`, the two POSIX-sh shims in `data/bin/` that `exec` the venv entries, and their `debian/cuems-power-bridge.install` lines

**Checkpoint**: one sequence, two callers, both on `PATH`; suite green.

---

## Phase 3: User Story 1 — Identical behaviour, however it is triggered (Priority: P1) 🎯 MVP

**Goal**: the wall switch and the HTTP API select the same machines, refuse in the same
situations, and say the same things.

**Independent Test**: drive every fixture through both entry points and compare decisions
field by field.

- [ ] T017 [P] [US1] Add `tests/test_shutdown_equivalence.py`: parameterise over `tests/fixtures/network_map/*` and assert the HTTP route and the CLI produce the same `ShutdownDecision` — case, targets, refusal token, partial flag
- [ ] T018 [P] [US1] Assert in `tests/test_shutdown_equivalence.py` that each refusal maps consistently across transports: `no_adopted_nodes` → 409/exit 4, `no_resolvable_nodes` → 409/exit 4, `topology_unreadable` → 503/exit 4
- [ ] T019 [P] [US1] Assert in `tests/test_shutdown_equivalence.py` that a proceeding run reaches the same steps in the same order through both callers (progress events compared, not just the outcome)
- [ ] T020 [US1] Assert the guard is **unreachable from the sequence** in `tests/test_shutdown_equivalence.py`: `src/cuemspowerbridge/cluster_shutdown.py` references neither the engine nor `/status`; the running-show probe exists only in `scripts/cluster_poweroff.py` (SC-010 — a claim about reachability, so a source-level assertion is the right instrument)
- [ ] T021 [US1] Diff `run_cluster_shutdown` against `specs/002-cluster-poweroff-cli/evidence/sequence-before.txt` and record the review in `specs/002-cluster-poweroff-cli/evidence/extraction-diff.md`, justifying every line that is not a pure move
- [ ] T022 [US1] Verify no existing HTTP behaviour changed: reason tokens, status codes and `/status` keys are as feature 001 left them (`tests/test_shutdown_cases.py` must pass untouched)
- [ ] T023 [US1] Take the lock in both callers and assert cross-process exclusion in `tests/test_shutdown_equivalence.py`: whichever holds it, the other refuses (`409` / exit 4) and never queues (FR-014, SC-009)

**Checkpoint**: US1 is the MVP — the de-duplication is real and checkable.

---

## Phase 4: User Story 2 — The logic is testable where it is maintained (Priority: P1)

**Goal**: this repository's code lives, and is tested, in this repository; the platform
package holds none of it.

**Independent Test**: run the suite and see both stages covered; grep the sibling's scripts
for this package's name and find nothing.

- [ ] T024 [P] [US2] Extend `tests/test_cluster_shutdown.py` to cover the display stage end to end — configured fleet, no fleet, an unreachable fleet — without hardware
- [ ] T025 [US2] Delete the two Python heredocs from `../cuems-common/usr/bin/cuems-cluster-poweroff` and call `cuems-power-bridge-cluster-poweroff --stage displays|nodes` with the conffile's values passed as arguments, plus `--transition`
- [ ] T026 [US2] Keep the wrapper's own responsibilities in `../cuems-common/usr/bin/cuems-cluster-poweroff` (research R8): the poweroff-vs-reboot `systemctl list-jobs` check, `enabled=`, the per-stage `timeout` bounds, the ordering probe — and switch its "is the bridge installed?" probe from the venv interpreter to `command -v cuems-power-bridge-cluster-poweroff`
- [ ] T027 [US2] Replace the two Python one-liners in `../cuems-common/usr/bin/cuems-displays-on` with `cuems-power-bridge-config --get`, leaving its polling loop, retry knobs and `curl` usage untouched (FR-016)
- [ ] T028 [P] [US2] Assert in `tests/test_sibling_decoupling.py` that neither sibling script mentions `cuemspowerbridge` (a repository-local check of the sibling checkout, skipped when it is absent)
- [ ] T029 [US2] Record the `cuems-common` half in its open `debian/changelog` entry `1.3.0-23` — the same entry that already carries feature 001's half
- [ ] T030 [US2] Remove `specs/001-node-role-parser/contracts/venv-library-surface.md`'s "frozen" status by superseding it: the contract is now [contracts/cli.md](./contracts/cli.md); note it in that file rather than deleting the record

**Checkpoint**: the platform package contains no Python of ours; the frozen library surface is discharged.

---

## Phase 5: User Story 3 — An operator can run and understand the tool (Priority: P2)

**Goal**: the tool is usable by a human on a controller, and rehearsable at zero cost.

**Independent Test**: run it from a plain shell, ask for help, rehearse a power-off.

- [ ] T031 [P] [US3] Implement the running-show guard in `src/cuemspowerbridge/scripts/cluster_poweroff.py` as a `GET /status` probe of the local daemon; refuse with exit 4 unless `--force`; skip entirely under `--transition` or when the daemon is unreachable (FR-015, research R3)
- [ ] T032 [P] [US3] Test the guard's truth table in `tests/test_cli_poweroff.py` — daemon up/playing, up/idle, unreachable, and `--transition` — asserting the transition case never probes
- [ ] T033 [P] [US3] Test the exit codes and `--dry-run` in `tests/test_cli_poweroff.py`: every refusal yields 4, a stuck host yields 5, a clean rehearsal yields 0 and touches nothing
- [ ] T034 [US3] Document both tools in `README.md`: synopsis, every option, the exit-status table, the `--transition` flag's meaning, and the rehearsal workflow
- [ ] T035 [P] [US3] Update `CLAUDE.md` — the new tools, the lock, and the fact that `cuems-common` no longer imports this package
- [ ] T036 [P] [US3] Verify `data/bin/cuems-power-bridge-cluster-poweroff` and `data/bin/cuems-power-bridge-config` work from a plain `PATH` on an installed system; record in `specs/002-cluster-poweroff-cli/evidence/deb-contents.txt` alongside T042

---

## Phase 6: User Story 4 — The platform package stays useful without this one (Priority: P2)

**Goal**: a host with no bridge still shuts down; the kill switches still work.

**Independent Test**: exercise each degradation path on a host or in a shell harness.

- [ ] T037 [P] [US4] Verify `../cuems-common/usr/bin/cuems-cluster-poweroff` with the tool absent: one ERROR line, exit 0, shutdown unimpeded (`bash -n` plus a PATH-stubbed run)
- [ ] T038 [P] [US4] Verify `enabled=false` in `../cuems-common/etc/cuems/cluster-poweroff.conf` still short-circuits `../cuems-common/usr/bin/cuems-cluster-poweroff` before either stage
- [ ] T039 [P] [US4] Verify `nodes_off=false` in `../cuems-common/etc/cuems/cluster-poweroff.conf` still makes `../cuems-common/usr/bin/cuems-cluster-poweroff` run the display stage only
- [ ] T040 [US4] Confirm `debian/control` here and `../cuems-common/debian/control` still carry feature 001's reciprocal `Breaks:` pair, and that this feature adds no new version relationship (research R7)

---

## Phase 7: Polish, evidence and the coordinated candidate

- [ ] T041 [P] Add the `debian/changelog` entry for this feature **inside the open `0.3.1-1` entry** — no new version; it lands beside feature 001
- [ ] T042 Build and check the package: the two shims land in `usr/bin/`, the tmpfiles rule in `usr/lib/tmpfiles.d/`, and `dpkg-deb -c` still shows no `site-packages/cuemsutils`; record in `specs/002-cluster-poweroff-cli/evidence/deb-contents.txt`
- [ ] T043 Run the full suite and record the final count in `specs/002-cluster-poweroff-cli/evidence/final-suite.txt`
- [ ] T044 [P] Update `specs/002-cluster-poweroff-cli/checklists/hardware-verification.md` if implementation changed any check's steps; every box stays unchecked until actually performed
- [ ] T045 Merge `002-cluster-poweroff-cli` into `feat/xml-refactor` with `--no-ff` and push `feat/xml-refactor` (the feature branch itself stays local)

### The candidate tag — last, and only once both halves are on their integration branches

- [ ] T046 Verify the candidate is coherent before tagging: `feat/xml-refactor` here contains features 001 and 002; `../cuems-common`'s `feat/xml-refactor` contains both of its halves; both are pushed and level with `origin`
- [ ] T047 Create `xml-refactor-merge-candidate` in this repository at the `feat/xml-refactor` merge commit (this repository has no such tag yet) and push it: `git tag -a xml-refactor-merge-candidate -m "<what the candidate contains>" && git push origin xml-refactor-merge-candidate`
- [ ] T048 **Relocate** `xml-refactor-merge-candidate` in `../cuems-common` from `f2fc0f5` — which predates even feature 001's half — to its current `feat/xml-refactor` head, and force-push the moved tag. **Moving a published tag is outward-facing and rewrites what other checkouts see: confirm with the user before pushing**, and record the old and new commits in the message
- [ ] T049 [P] Record the candidate's composition in `specs/002-cluster-poweroff-cli/evidence/candidate.md`: each repository, its version, its tagged commit, and which features it carries — the sheet a technician reads before starting the hardware ledger
- [ ] T050 Cross-check the ecosystem's tags with `git -C <repo> log --oneline -1 xml-refactor-merge-candidate` for `.`, `../cuems-common`, `../cuems-nodeconf` (expect `6c0cca7`, 0.1.0-8) and `../cuems-utils`; record the four results in `specs/002-cluster-poweroff-cli/evidence/candidate.md`. A tag not pointing at the reviewed head is a finding, not a formality

---

## Dependencies & Execution Order

- **Phase 1** → **Phase 2**: the lock needs its directory; the extraction needs its "before" capture (T004 must precede T009).
- **Phase 2** blocks all stories: both callers must exist before anything can compare them.
- **Phase 3 (US1)** and **Phase 5 (US3)** depend only on Phase 2. **Phase 4 (US2)** additionally needs the CLI installed (T013–T016), because the sibling calls it. **Phase 6 (US4)** needs Phase 4's rewritten wrapper.
- **Phase 7**: T041–T044 need the code complete; T045 needs the suite green; **T046–T050 are last**, and T048 needs the user's confirmation.

### Critical path

`T004 → T005 → T007-T011 → T013-T016 → T017-T023 → T025-T027 → T042-T043 → T045 → T046-T048`

### Parallel opportunities

- Phase 1: T002, T003, T004 together.
- Phase 2: T006 with T007–T011; T015 and T016 with T013–T014.
- Phase 3: T017–T019 are one new file but independent concerns.
- Phases 5 and 6 by different people: the CLI's operator surface versus the sibling's degradation paths.

---

## Implementation Strategy

**MVP = Phases 1 + 2 + 3 (US1).** The sequence exists once, both callers run it, and the
equivalence tests prove it. That is the whole point of the feature; everything after is
relocation and polish.

**Increment 2 = Phase 4 (US2)** — the sibling stops holding our code.

**Increment 3 = Phases 5–7** — the operator surface, the degradation paths, and the
coordinated candidate.

**Standing rules** (constitution): the suite is green at every checkpoint; commits are
GPG-signed; line numbers are re-measured before being edited; and the product path's
behaviour is verified by diff, by call graph and by test — not by assertion.
