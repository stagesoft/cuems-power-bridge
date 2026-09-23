<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Tasks: Node-role parser migration

**Input**: Design documents from `/specs/001-node-role-parser/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/](./contracts/), [quickstart.md](./quickstart.md)

**Tests**: **Included and mandatory.** The spec requires them (FR-020, FR-021, FR-022) and
the constitution's Testing Gate makes the discriminating fixtures the gate for this class of
defect — a passing suite proves nothing here unless a fixture fails against the old reader.

**Organization**: grouped by user story. US1 (shutdown) and US2 (auto-load) fail and recover
independently and are verified separately; either alone is a shippable improvement.

**Revision 2026-09-23** (post-`/speckit-analyze`): adds Case 3b coverage, the partial-
resolution rule, the three-state readiness gate, message-content assertions, and moves the
fixtures into `tests/fixtures/network_map/`. T014a/T014b were added after the library
smoke test (research R2a).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from [spec.md](./spec.md)

## Path Conventions

Single Python package: `src/cuemspowerbridge/`, `tests/` at the repository root. Fixtures
live in `tests/fixtures/network_map/` (they are suite inputs, not feature artifacts);
evidence lives in `specs/001-node-role-parser/evidence/`. The sibling checkouts are
`../cuems-utils` and `../cuems-common`.

**Runner** (the default interpreter has none of the dependencies):

```bash
uv run --python 3.11 --with pytest --with pytest-asyncio --with pytest-mock \
  --with aiohttp --with websockets --with python-osc --with ../cuems-utils \
  python -m pytest -q
```

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: capture what cannot be captured later, and give the suite schema-valid inputs.
**T001 is first and blocking: once the parser is replaced, its evidence is unobtainable.**

- [X] T001 Capture the pre-migration failing run into `specs/001-node-role-parser/evidence/pre-migration-parser-failure.txt` — today's `network_map.slave_avahi_names()` / `slave_ips()` against a converted fixture per [quickstart.md](./quickstart.md) §1, recording command, date, commit and the `([], [])` / `[]` output (FR-021, SC-008)
- [X] T002 Record the baseline suite state (`142 passed`) and the exact runner line in `specs/001-node-role-parser/evidence/baseline-suite.txt`
- [X] T003 [P] Create `tests/fixtures/network_map/` with one directory per case from [quickstart.md](./quickstart.md) §2, each holding a schema-valid `network_map.xml` plus a `settings.xml`: `map-controller-only`, `map-two-adopted`, `map-none-adopted`, `map-mixed`, `map-unresolvable`, `map-partial-resolve`, `map-pre007`, `map-no-self`, `map-incomplete`, `map-no-settings`
- [X] T004 [P] Validate every fixture in `tests/fixtures/network_map/` against `../cuems-utils/src/cuemsutils/xml/schemas/network_map.xsd` (canonical uuid, `mac`, `name`, `node_role`, `ip` all required) and record the command in `specs/001-node-role-parser/evidence/fixture-validation.txt`
- [X] T005 Add `tests/conftest.py` with a fixture that points the library at a `tests/fixtures/network_map/<case>` directory by setting the `CUEMS_CONF_PATH` environment variable (research R2: the library honours it **over** any `config_dir` argument) and restores it afterwards

**Checkpoint**: evidence banked, fixtures exist and are schema-valid, the suite can aim the library at them.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the adapter both user stories consume. **No user-story work can begin until this phase is complete.**

**⚠️ T006–T012 replace the private parser; they are the point of the feature (D11, D32, Principle III).**

- [X] T006 Add `NodeView` to `src/cuemspowerbridge/network_map.py` per [data-model.md](./data-model.md) §2.1 — `uuid`, `role` (imported `NodeRole`), `adopted` (**absent ⇒ `False`**, research R3), `role_id`/`alias`/`hostname`, `ip`, derived `avahi`, `is_self`; **no `node_type` attribute exists**
- [X] T007 Add `Selection` and `Skip` to `src/cuemspowerbridge/network_map.py` per [data-model.md](./data-model.md) §2.2 — `mode`, `partial`, `targets`, `found`, `adopted_count`, `skipped`; invariant: `targets ∪ skipped` is the full non-self machine set
- [X] T008 Add `TopologyError` with its seven `kind` values to `src/cuemspowerbridge/network_map.py` per [data-model.md](./data-model.md) §2.3, each message naming the offending document or machine **and a remedy** (FR-026); it MUST never be converted into an empty `Selection`
- [X] T009 Implement the loader in `src/cuemspowerbridge/network_map.py`: build `ConfigManager(config_dir=…, load_all=False)`, call `load_network_map()`, unwrap `["node_list"]`'s `{"node": …}` wrappers into `NodeView`s, and classify every library failure into a `TopologyError` kind (research R2)
- [X] T010 Implement `shutdown_targets(..., include_unadopted: bool)` in `src/cuemspowerbridge/network_map.py` (research R4) — preserving the avahi policy verbatim (`role_id → alias → hostname`, **never** `<ip>`, unresolvable machines reported in `skipped`), setting `partial=True` when some machine that should have been targeted is unresolvable (FR-025), and returning an empty target set with `adopted_count > 0` when **none** is addressable so the caller can raise Case 3b
- [X] T011 Implement `readiness_peers(...)` in `src/cuemspowerbridge/network_map.py` — adopted machines only (FR-009, confirmed Q4), **trusting** `<ip>`, skipping an adopted machine without one into `skipped(no_ip)` so the caller can tell "no adopted machine at all" from "adopted machines with no address" (FR-013)
- [X] T012 Delete the private parser from `src/cuemspowerbridge/network_map.py` — `NS`, `_text`, the `Node` dataclass with its `node_type` field, `parse()`'s ElementTree scan, `slave_avahi_names()`, `slave_ips()` — and rewrite the module docstring, removing the retired vocabulary at `:5,9,58-59,120`
- [X] T013 Derive the library's `config_dir` from `settings_xml_path` in `src/cuemspowerbridge/config.py`, and raise `TopologyError(config_dir_mismatch)` when `network_map_path` is not in that directory (research R2)
- [X] T014a Bound the `cuemsutils` logger in `src/cuemspowerbridge/network_map.py` (or the daemon's logging setup) so the library's per-call DEBUG records cannot flood the controller's journal when the bridge runs at DEBUG — research R2a finding 2; topology reads now also happen on the auto-load retry loop
- [X] T014 [P] Test the adapter in `tests/test_network_map_adapter.py` against the Phase-1 fixtures: both resolution policies, the absent-`adopted` default, every `Skip` reason, the `partial` flag, and one test per `TopologyError` kind — explicitly including `self_entry_missing` (FR-019) and `config_dir_mismatch`
- [X] T014b [P] Assert in `tests/test_network_map_adapter.py` that a `cuemsutils` DEBUG record does not propagate to the bridge's root logger at the bridge's own level (T014a)
- [X] T015 [P] Assert message **content** in `tests/test_network_map_adapter.py`: every `TopologyError` names the offending path or machine and an actionable remedy; `network_map_retired_vocabulary` names `cuems-migrate-network-map`; `settings_xml_missing` names the package that provides the file (FR-026, SC-003, SC-014)
- [X] T016 [P] Rewrite `tests/test_network_map_ips.py` onto the schema-valid fixtures in both vocabularies — the current-vocabulary one asserting correct selection, the retired-vocabulary one asserting the load **raises** (FR-020)

**Checkpoint**: the adapter is the only reader in the package, tested by content as well as by shape, with no retired vocabulary left in it.

---

## Phase 3: User Story 1 — The power switch shuts the whole cluster down (Priority: P1) 🎯 MVP

**Goal**: an orderly power-off selects the cluster's machines from a converted document, confirms them down, and never cuts mains on an unverified cluster.

**Independent Test**: point the bridge at `tests/fixtures/network_map/map-two-adopted`, trigger `/shutdown` with `dry_run=true`, and confirm the recorded target list names both machines; then walk the other five cases.

### Tests for User Story 1

- [X] T017 [P] [US1] Add `tests/test_shutdown_cases.py` covering the six cases end-to-end through `handle_shutdown`: Case 1 proceeds (`controller_only`), Case 2 targets adopted only, Case 3 refuses `409 no_adopted_nodes`, Case 4 under `force=1` targets every machine, Case 5 refuses `503 topology_unreadable`
- [X] T018 [P] [US1] Assert Case 3b in `tests/test_shutdown_cases.py` — adopted machines all unresolvable ⇒ `409 no_resolvable_nodes`, **with and without `force=1`**, naming every unresolvable machine (FR-010, SC-013)
- [X] T019 [P] [US1] Assert Case 5 in `tests/test_shutdown_cases.py` is refused **with and without `force=1`** (FR-011)
- [X] T020 [P] [US1] Assert partial resolution in `tests/test_shutdown_cases.py` against `map-partial-resolve` — the shutdown proceeds, each unresolvable machine is logged at ERROR, and `node_selection.partial` is `true` (FR-025)
- [X] T021 [P] [US1] Assert in `tests/test_shutdown_cases.py` that Cases 3, 3b and 5 never reach `arming-shelly` — no SSH fan-out, no Shelly call, state returns to `idle` (Principle I)
- [X] T022 [P] [US1] Assert in `tests/test_shutdown_cases.py` that the reachability poll runs whenever targets exist, including a list shorter than `found` (FR-012, Principle II)

### Implementation for User Story 1

- [X] T023 [US1] Replace the target build at `src/cuemspowerbridge/bridge.py:391` with `shutdown_targets(...)`, passing `include_unadopted=force`, and log the selection: targeted, plus every skipped machine with its reason (unresolvable at ERROR)
- [X] T024 [US1] Implement the Case 3 refusal in `handle_shutdown` (`src/cuemspowerbridge/bridge.py:348-382`) — `409 no_adopted_nodes` with `found` and `skipped` in the body, per [contracts/http-shutdown.md](./contracts/http-shutdown.md)
- [X] T025 [US1] Implement the Case 3b refusal in `handle_shutdown` (`src/cuemspowerbridge/bridge.py:348-382`) — `409 no_resolvable_nodes` with `found` and `unresolvable`, **not overridable by `force`**
- [X] T026 [US1] Implement the Case 5 refusal in `handle_shutdown` (`src/cuemspowerbridge/bridge.py:348-382`) — `503 topology_unreadable` with the `TopologyError.kind` as `detail`, **not overridable by `force`**
- [X] T027 [US1] Remove the `if resolved:` guard at `src/cuemspowerbridge/bridge.py:431` so the reachability poll runs on every proceeding shutdown; in Case 1 log explicitly that there is nothing to poll rather than leaving silence (Principle II)
- [X] T028 [US1] Carry `selection_mode` and `partial` through `_run_shutdown` and expose the `node_selection` object in `_status_payload` — `src/cuemspowerbridge/bridge.py:144-165, 386+` (FR-014, FR-025)
- [X] T029 [US1] Verify in `src/cuemspowerbridge/bridge.py` and `tests/test_bridge_poweron.py` that no existing reason token, status code or `/status` key changed meaning — `cuems-displays-on` parses `GET /status`, Companion posts the transport endpoints (Principle VII)

**Checkpoint**: US1 is independently shippable — power-off is correct on a converted map, refuses rather than guessing, and says when it did less than the map implies.

---

## Phase 4: User Story 2 — The show loads only once the cluster is ready (Priority: P1)

**Goal**: the boot readiness gate waits for the cluster's adopted machines, and distinguishes a genuine standalone cluster from a misconfigured one and from a failed read.

**Independent Test**: `map-two-adopted` ⇒ waits for both; `map-controller-only` ⇒ settles and loads; `map-adopted-no-ip` ⇒ settles **with a WARNING**; `map-pre007` ⇒ refuses, does not settle.

### Tests for User Story 2

- [X] T030 [P] [US2] Extend `tests/test_autoload.py`: with two adopted machines carrying `<ip>`, the gate waits for both and names them
- [X] T031 [P] [US2] Extend `tests/test_autoload.py`: with no adopted machine at all, the settle path is taken and reported as a standalone cluster (FR-013)
- [X] T032 [P] [US2] Cover the defensive no-address branch in `tests/test_network_map_adapter.py` by constructing that state directly — the schema makes `<ip>` mandatory, so it is unreachable through a document fixture (research R11); assert it settles with a WARNING naming the machines and does NOT report a standalone cluster (FR-013, Principle IV)
- [X] T033 [P] [US2] Extend `tests/test_autoload.py`: a `TopologyError` disables auto-load with a reported failure and does **not** fall through to the settle path
- [X] T034 [P] [US2] Extend `tests/test_autoload.py`: an unadopted machine is never waited for and is named as skipped (FR-009, Q4)

### Implementation for User Story 2

- [X] T035 [US2] Replace `_slave_ips_cached` (`src/cuemspowerbridge/bridge.py:575-593`) with an mtime-keyed cache over the adapter's load, keyed on **both** `network_map.xml` and `settings.xml`, running in an executor (research R5)
- [X] T036 [US2] Rewrite `_expected_node_ips` (`src/cuemspowerbridge/bridge.py:594-616`) onto `readiness_peers(...)`, keeping the `auto_load_node_ids` subset behaviour and its warn-once on an unknown id
- [X] T037 [US2] Implement the three-state gate at `src/cuemspowerbridge/bridge.py:652` — standalone (settle, reported), adopted-but-address-less (settle with WARNING naming them), read failed (do not settle, report) — each with a distinct log line (FR-013, FR-004)
- [X] T038 [US2] Surface auto-load's topology failure in `/status.node_selection` (`read_ok: false`, `read_error`) in `src/cuemspowerbridge/bridge.py:144-165, 618+`

**Checkpoint**: US2 is independently shippable and verified separately from US1.

---

## Phase 5: User Story 3 — An upgraded cluster keeps working, in both directions (Priority: P2)

**Goal**: the platform tool and this package select identically, and a half-upgraded pair is refused by the package manager rather than failing mid-poweroff.

**Independent Test**: run `cuems-cluster-poweroff` against this package's library on a converted map and compare its target list with the bridge's; then attempt the refused install combination on a test host.

- [ ] T039 [US3] Make `cuemsutils` a real, non-optional, bounded dependency in `pyproject.toml` — remove it from `[tool.poetry.extras]`, pin `>=0.1.0rc16,<0.1.1` (research R2)
- [ ] T040 [US3] Update `debian/control`: `cuems-utils (>= 0.1.0rc16)`, `cuems-common (>= 1.3.0-23)`, and `Breaks: cuems-common (<< 1.3.0-23)` (FR-016, contracts/venv-library-surface.md)
- [ ] T041 [US3] Port `../cuems-common/usr/bin/cuems-cluster-poweroff` stage 2 (`:274-275`) onto the adapter's `shutdown_targets(...)`, so all six cases hold identically whether a shutdown comes through HTTP or the poweroff transition
- [ ] T042 [US3] Correct the stale docstring at `../cuems-common/usr/bin/cuems-cluster-poweroff:240` ("matches the network_map `NodeType.master` entry")
- [ ] T043 [US3] Add `Breaks: cuems-power-bridge (<< 0.3.1-1)` to `../cuems-common/debian/control`, leaving `Suggests: cuems-power-bridge` unversioned and unchanged (it must stay installable with no bridge present)
- [ ] T044 [US3] Record the cuems-common half in its open `../cuems-common/debian/changelog` entry `1.3.0-23` (UNRELEASED) rather than opening a new revision
- [ ] T045 [US3] Rehearse the refused half-upgrade on a test host and record it in `specs/001-node-role-parser/evidence/upgrade-refusal.txt` (SC-009)

**Checkpoint**: the cutover is mechanical, not a note in a document.

---

## Phase 6: User Story 4 — The next rename is loud (Priority: P2)

**Goal**: a future vocabulary change in a repository this one does not own cannot silently disable a feature here.

**Independent Test**: present the retired-vocabulary document and confirm a named, actionable failure rather than an empty answer.

- [ ] T046 [P] [US4] Assert in `tests/test_network_map_ips.py` that the retired-vocabulary fixture raises with a message naming the document, the offending machine and the conversion tool (FR-003, SC-007)
- [ ] T047 [US4] Confirm the recorded failing run from T001 is complete and legible as the FR-021/SC-008 artifact, referenced from `specs/001-node-role-parser/evidence/README.md`
- [ ] T048 [US4] Count the retired vocabulary across shipped code and shipped prose and record it with the exempt set and reasons in `specs/001-node-role-parser/evidence/retired-vocabulary-count.txt` — exemptions: the deliberately pre-007 fixture; excluded by reason: `src/cuemspowerbridge/wsclient.py:69-70`'s `master.local` (an mDNS hostname, not the role field) (FR-023, SC-011)

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T049 [P] Rewrite the shipped package description at `debian/control:34` ("SSHes every NodeType.slave from /etc/cuems/network_map.xml") in the current vocabulary
- [ ] T050 [P] Update the five README sites carrying the retired vocabulary (`README.md:320,323,476,767,1265` — re-measure before editing)
- [ ] T051 Add the operator-facing documentation FR-024 requires to `README.md`: the **six** shutdown cases, the partial-resolution rule, both meanings of `force` and its two non-effects, and the wall-switch consequence (the shipped mJS sends `force` by default, so the physical switch powers off every machine and does not stop for a running show; `--safe` changes that) (SC-012)
- [ ] T052 [P] Add the `debian/changelog` entry for `0.3.1-1` describing the behaviour change: adoption filtering, the three new refusals, the partial marker, and the required cuems-common pairing
- [ ] T053 Build the package and assert the shared-venv rule: `dpkg-deb -c ../cuems-power-bridge_*.deb` shows **no** `site-packages/cuemsutils` and **does** show the aiohttp stack; record in `specs/001-node-role-parser/evidence/deb-contents.txt` (FR-017, Principle VI, research R10)
- [ ] T054 Verify the built `cuems-utils` `0.1.0rc16` `.deb` installs `/etc/cuems/settings.xml` (its packaging lives on the `debian/bookworm` branch) and record it in `specs/001-node-role-parser/evidence/settings-xml-provenance.txt`
- [ ] T055 Run the full suite green through the documented runner and record the final count in `specs/001-node-role-parser/evidence/final-suite.txt`
- [ ] T056 [US1] Hardware rehearsal 1 — orderly power-off on a real cluster (`dry_run` first, then live): adopted machines go off, the poll confirms them, the Shelly arms, the controller powers off, mains cuts on an already-off box; record in `specs/001-node-role-parser/evidence/hardware-verification.md` (FR-022, SC-010)
- [ ] T057 [US2] Hardware rehearsal 2 — cold boot with nodes powered: the gate waits for the adopted machines, the show loads and arms, and `settle=45` / `armed_timeout=125` still hold; record in `specs/001-node-role-parser/evidence/hardware-verification.md` as a **separate** run from T056 (FR-022, SC-010)
- [ ] T058 Negative rehearsal, no cluster needed: an unconverted map makes `/shutdown` refuse `503` and leaves mains on, `force=1` included; record in `specs/001-node-role-parser/evidence/hardware-verification.md`

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)** — no dependencies. **T001 must precede T012**, or the evidence is lost.
- **Phase 2 (Foundational)** — depends on Phase 1 fixtures. **Blocks every user story.**
- **Phase 3 (US1)** and **Phase 4 (US2)** — both depend only on Phase 2, and are independent of each other. Either may ship alone.
- **Phase 5 (US3)** — depends on the adapter API being settled (T010, T011); its sibling half (T041–T044) must merge simultaneously with this repository.
- **Phase 6 (US4)** — depends on Phase 2 (T016) and on T001.
- **Phase 7 (Polish)** — T049–T052 need only Phase 2; T053–T055 need the code complete; T056–T058 need US1/US2 complete and hardware.

### Critical path

`T001 → T003/T004 → T006-T013 → (T023-T029 | T035-T038) → T053-T055 → T056-T058`

### Parallel opportunities

- Phase 1: T003 and T004 alongside T002.
- Phase 2: T014, T015 and T016 in parallel once T010–T013 land.
- Phase 3: T017–T022 are one new file but six independent concerns; implementation T023–T029 is sequential (one file).
- Phases 3 and 4 by different people: the shutdown path versus the auto-load path in `bridge.py`.
- Phase 6: T046 alongside Phase 7 prose tasks T049, T050, T052.

---

## Implementation Strategy

**MVP = Phase 1 + Phase 2 + Phase 3 (US1).** That restores the safety-critical half: no
shutdown proceeds on an unverified cluster, and a converted map selects the right machines.
It is shippable on its own, though it does not release alone (D27).

**Increment 2 = Phase 4 (US2)** — the auto-load gate. Verified separately; "power-off works"
says nothing about it.

**Increment 3 = Phases 5–7** — the cutover with cuems-common, the loudness guarantees, the
prose, the packaging gates and the hardware rehearsals.

**Standing rules while executing** (constitution): the suite is green before starting and at
every checkpoint; commits are GPG-signed; line numbers are re-measured before being edited,
never transcribed from this file.
