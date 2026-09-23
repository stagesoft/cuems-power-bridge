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

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1–US4 from [spec.md](./spec.md)

## Path Conventions

Single Python package: `src/cuemspowerbridge/`, `tests/` at the repository root. Feature
artifacts under `specs/001-node-role-parser/`. The sibling checkouts are `../cuems-utils`
and `../cuems-common`.

**Runner** (the default interpreter has none of the dependencies):

```bash
uv run --python 3.11 --with pytest --with pytest-asyncio --with pytest-mock \
  --with aiohttp --with websockets --with python-osc --with ../cuems-utils \
  python -m pytest -q
```

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: capture what cannot be captured later, and make the library available to the
suite. **T001 is first and blocking: once the parser is replaced, its evidence is
unobtainable.**

- [ ] T001 Capture the pre-migration failing run into `specs/001-node-role-parser/evidence/pre-migration-parser-failure.txt` — run today's `network_map.slave_avahi_names()` / `slave_ips()` against a converted fixture per [quickstart.md](./quickstart.md) §1, recording the command, date, commit and the `([], [])` / `[]` output (FR-021, SC-008)
- [ ] T002 Record the baseline suite state (`142 passed`) and the exact runner line in `specs/001-node-role-parser/evidence/baseline-suite.txt`
- [ ] T003 [P] Create `specs/001-node-role-parser/fixtures/` with one directory per case from [quickstart.md](./quickstart.md) §2, each holding a schema-valid `network_map.xml` plus a `settings.xml`: `map-controller-only`, `map-two-adopted`, `map-none-adopted`, `map-mixed`, `map-pre007`, `map-no-self`, `map-incomplete`, `map-no-settings`
- [ ] T004 [P] Validate every fixture against `../cuems-utils/src/cuemsutils/xml/schemas/network_map.xsd` (canonical uuid, `mac`, `name`, `node_role`, `ip` all required) and record the validation command in `specs/001-node-role-parser/evidence/fixture-validation.txt`
- [ ] T005 Add a `tests/conftest.py` fixture that points the library at a fixture directory by setting the `CUEMS_CONF_PATH` environment variable (research R2: the library honours it **over** any `config_dir` argument) and restores it afterwards

**Checkpoint**: evidence banked, fixtures exist and are schema-valid, the suite can aim the library at them.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the adapter both user stories consume. **No user-story work can begin until this phase is complete.**

**⚠️ T006–T010 replace the private parser; they are the point of the feature (D11, D32, Principle III).**

- [ ] T006 Add `NodeView` to `src/cuemspowerbridge/network_map.py` per [data-model.md](./data-model.md) §2.1 — `uuid`, `role` (imported `NodeRole`), `adopted` (**absent ⇒ `False`**, research R3), `role_id`/`alias`/`hostname`, `ip`, derived `avahi`, `is_self`; **no `node_type` attribute exists**
- [ ] T007 Add `Selection` and `Skip` to `src/cuemspowerbridge/network_map.py` per [data-model.md](./data-model.md) §2.2 — `mode`, `targets`, `found`, `adopted_count`, `skipped`; invariant: `targets ∪ skipped` is the full non-self machine set
- [ ] T008 Add `TopologyError` with the seven `kind` values to `src/cuemspowerbridge/network_map.py` per [data-model.md](./data-model.md) §2.3; it MUST never be converted into an empty `Selection`
- [ ] T009 Implement the loader in `src/cuemspowerbridge/network_map.py`: build `ConfigManager(config_dir=…, load_all=False)`, call `load_network_map()`, unwrap `["node_list"]`'s `{"node": …}` wrappers into `NodeView`s, and classify every library failure into a `TopologyError` kind (research R2)
- [ ] T010 Implement `shutdown_targets(..., include_unadopted: bool)` and `readiness_peers(...)` in `src/cuemspowerbridge/network_map.py` (research R4) — preserving both field-learned policies verbatim: avahi resolution `role_id → alias → hostname`, **never** `<ip>`, unresolvable machines reported in `skipped`; readiness peers **trust** `<ip>` and skip an adopted machine without one
- [ ] T011 Delete the private parser from `src/cuemspowerbridge/network_map.py` — `NS`, `_text`, the `Node` dataclass with its `node_type` field, `parse()`'s ElementTree scan, `slave_avahi_names()`, `slave_ips()` — and rewrite the module docstring, removing the retired vocabulary at `:5,9,58-59,120`
- [ ] T012 Derive the library's `config_dir` from `settings_xml_path` in `src/cuemspowerbridge/config.py`, and raise `TopologyError(config_dir_mismatch)` when `network_map_path` is not in that directory (research R2)
- [ ] T013 [P] Test the adapter in `tests/test_network_map_adapter.py`: both resolution policies, the absent-`adopted` default, `Selection` reasons for every skip kind, and one test per `TopologyError` kind against the Phase-1 fixtures
- [ ] T014 [P] Rewrite `tests/test_network_map_ips.py` onto schema-valid fixtures in both vocabularies — the current-vocabulary one asserting correct selection, the retired-vocabulary one asserting the load **raises** (FR-020)

**Checkpoint**: the adapter is the only reader in the package, tested, with no retired vocabulary left in it.

---

## Phase 3: User Story 1 — The power switch shuts the whole cluster down (Priority: P1) 🎯 MVP

**Goal**: an orderly power-off selects the cluster's machines from a converted document, confirms them down, and never cuts mains on an unverified cluster.

**Independent Test**: point the bridge at `fixtures/map-two-adopted`, trigger `/shutdown` with `dry_run=true`, and confirm the recorded target list names both machines; then walk the other four cases.

### Tests for User Story 1

- [ ] T015 [P] [US1] Add `tests/test_shutdown_cases.py` covering all five cases end-to-end through `handle_shutdown`: Case 1 proceeds (`controller_only`), Case 2 targets adopted only, Case 3 refuses `409 no_adopted_nodes`, Case 4 under `force=1` targets every machine, Case 5 refuses `503 topology_unreadable` **with and without `force`**
- [ ] T016 [P] [US1] Assert in `tests/test_shutdown_cases.py` that Cases 3 and 5 never reach `arming-shelly` — no SSH fan-out, no Shelly call, state returns to `idle` (Principle I)
- [ ] T017 [P] [US1] Assert in `tests/test_shutdown_cases.py` that the reachability poll runs whenever targets exist, including a list shorter than `found` (FR-012, Principle II)

### Implementation for User Story 1

- [ ] T018 [US1] Replace the target build at `src/cuemspowerbridge/bridge.py:391` with `shutdown_targets(...)`, passing `include_unadopted=force`, and log the selection: targeted, and every skipped machine with its reason
- [ ] T019 [US1] Implement the Case 3 refusal in `handle_shutdown` (`src/cuemspowerbridge/bridge.py:348-382`) — `409 no_adopted_nodes` with `found` and `skipped` in the body, per [contracts/http-shutdown.md](./contracts/http-shutdown.md)
- [ ] T020 [US1] Implement the Case 5 refusal in `handle_shutdown` (`src/cuemspowerbridge/bridge.py:348-382`) — `503 topology_unreadable` with the `TopologyError.kind` as `detail`, **not overridable by `force`**; the `settings_xml_missing` message names the providing package
- [ ] T021 [US1] Remove the `if resolved:` guard at `src/cuemspowerbridge/bridge.py:431` so the reachability poll runs on every proceeding shutdown; in Case 1 log explicitly that there is nothing to poll rather than leaving silence (Principle II)
- [ ] T022 [US1] Carry `selection_mode` (`adopted` / `forced_all` / `controller_only`) through `_run_shutdown` and expose the `node_selection` object in `_status_payload` — `src/cuemspowerbridge/bridge.py:144-165, 386+` per [contracts/http-shutdown.md](./contracts/http-shutdown.md) (FR-014)
- [ ] T023 [US1] Verify in `src/cuemspowerbridge/bridge.py` and `tests/test_bridge_poweron.py` that no existing reason token, status code or `/status` key changed meaning — `cuems-displays-on` parses `GET /status`, Companion posts the transport endpoints (Principle VII)

**Checkpoint**: US1 is independently shippable — power-off is correct on a converted map, and refuses rather than guessing.

---

## Phase 4: User Story 2 — The show loads only once the cluster is ready (Priority: P1)

**Goal**: the boot readiness gate waits for the cluster's adopted machines, and takes its single-controller path only when the document genuinely says so.

**Independent Test**: point the bridge at `fixtures/map-two-adopted` and confirm it waits for both addresses on the NNG hub before loading; then at `map-controller-only` (settles and loads) and `map-pre007` (refuses, does **not** settle-and-load).

### Tests for User Story 2

- [ ] T024 [P] [US2] Extend `tests/test_autoload.py`: with two adopted machines carrying `<ip>`, the gate waits for both and names them
- [ ] T025 [P] [US2] Extend `tests/test_autoload.py`: the single-controller settle path is entered **only** after a successful read that lists no adopted machine with an `<ip>` (FR-013, Principle IV)
- [ ] T026 [P] [US2] Extend `tests/test_autoload.py`: a `TopologyError` disables auto-load with a reported failure and does **not** fall through to the settle path; and an adopted machine without `<ip>` is skipped with a warning

### Implementation for User Story 2

- [ ] T027 [US2] Replace `_slave_ips_cached` (`src/cuemspowerbridge/bridge.py:575-593`) with an mtime-keyed cache over the adapter's load, keyed on **both** `network_map.xml` and `settings.xml`, running in an executor (research R5)
- [ ] T028 [US2] Rewrite `_expected_node_ips` (`src/cuemspowerbridge/bridge.py:594-616`) onto `readiness_peers(...)`, keeping the `auto_load_node_ids` subset behaviour and its warn-once on an unknown id
- [ ] T029 [US2] Gate the single-controller branch (`src/cuemspowerbridge/bridge.py:652`) on a successful read, and distinguish its log line from a failed read (FR-004)
- [ ] T030 [US2] Surface auto-load's topology failure in `/status.node_selection` (`read_ok: false`, `read_error`) in `src/cuemspowerbridge/bridge.py:144-165, 618+` so a monitor sees it without triggering a shutdown

**Checkpoint**: US2 is independently shippable and verified separately from US1.

---

## Phase 5: User Story 3 — An upgraded cluster keeps working, in both directions (Priority: P2)

**Goal**: the platform tool and this package select identically, and a half-upgraded pair is refused by the package manager rather than failing mid-poweroff.

**Independent Test**: run `cuems-cluster-poweroff` against this package's library on a converted map and compare its target list with the bridge's; then attempt the refused install combination on a test host.

- [ ] T031 [US3] Make `cuemsutils` a real, non-optional, bounded dependency in `pyproject.toml` — remove it from `[tool.poetry.extras]`, pin `>=0.1.0rc16,<0.1.1` (research R2)
- [ ] T032 [US3] Update `debian/control`: `cuems-utils (>= 0.1.0rc16)`, `cuems-common (>= 1.3.0-23)`, and `Breaks: cuems-common (<< 1.3.0-23)` (FR-016, contracts/venv-library-surface.md)
- [ ] T033 [US3] Port `../cuems-common/usr/bin/cuems-cluster-poweroff` stage 2 (`:274-275`) onto the adapter's `shutdown_targets(...)`, so the five cases hold identically whether a shutdown comes through HTTP or the poweroff transition
- [ ] T034 [US3] Correct the stale docstring at `../cuems-common/usr/bin/cuems-cluster-poweroff:240` ("matches the network_map `NodeType.master` entry")
- [ ] T035 [US3] Add `Breaks: cuems-power-bridge (<< 0.3.1-1)` to `../cuems-common/debian/control`, leaving `Suggests: cuems-power-bridge` unversioned and unchanged (it must stay installable with no bridge present)
- [ ] T036 [US3] Record the cuems-common half in its open `debian/changelog` entry `1.3.0-23` (UNRELEASED) rather than opening a new revision
- [ ] T037 [US3] Rehearse the refused half-upgrade on a test host and record it in `specs/001-node-role-parser/evidence/upgrade-refusal.txt` (SC-009)

**Checkpoint**: the cutover is mechanical, not a note in a document.

---

## Phase 6: User Story 4 — The next rename is loud (Priority: P2)

**Goal**: a future vocabulary change in a repository this one does not own cannot silently disable a feature here.

**Independent Test**: present the retired-vocabulary document and confirm a named, actionable failure rather than an empty answer.

- [ ] T038 [P] [US4] Assert in `tests/test_network_map_ips.py` that the retired-vocabulary fixture raises with a message naming the document, the offending machine and the conversion tool (FR-003, SC-007)
- [ ] T039 [US4] Confirm the recorded failing run from T001 is complete and legible as the FR-021/SC-008 artifact, referenced from `specs/001-node-role-parser/evidence/README.md`
- [ ] T040 [US4] Count the retired vocabulary across shipped code and shipped prose and record it with the exempt set and reasons in `specs/001-node-role-parser/evidence/retired-vocabulary-count.txt` — exemptions: the deliberately pre-007 fixture; excluded by reason: `src/cuemspowerbridge/wsclient.py:69-70`'s `master.local` (an mDNS hostname, not the role field) (FR-023, SC-011)

---

## Phase 7: Polish & Cross-Cutting Concerns

- [ ] T041 [P] Rewrite the shipped package description at `debian/control:34` ("SSHes every NodeType.slave from /etc/cuems/network_map.xml") in the current vocabulary
- [ ] T042 [P] Update the five README sites carrying the retired vocabulary (`README.md:320,323,476,767,1265` — re-measure before editing)
- [ ] T043 Add the operator-facing documentation FR-024 requires to `README.md`: the five shutdown cases, both meanings of `force`, and the wall-switch consequence (the shipped mJS sends `force` by default, so the physical switch powers off every machine and does not stop for a running show; `--safe` changes that) (SC-012)
- [ ] T044 [P] Add the `debian/changelog` entry for `0.3.1-1` describing the behaviour change: adoption filtering, the two new refusals, and the required cuems-common pairing
- [ ] T045 Build the package and assert the shared-venv rule: `dpkg-deb -c ../cuems-power-bridge_*.deb` shows **no** `site-packages/cuemsutils` and **does** show the aiohttp stack; record in `specs/001-node-role-parser/evidence/deb-contents.txt` (FR-017, Principle VI, research R10)
- [ ] T046 Verify the built `cuems-utils` `0.1.0rc16` `.deb` installs `/etc/cuems/settings.xml` (its packaging lives on the `debian/bookworm` branch) and record it in `specs/001-node-role-parser/evidence/settings-xml-provenance.txt`
- [ ] T047 Run the full suite green through the documented runner and record the final count in `specs/001-node-role-parser/evidence/final-suite.txt`
- [ ] T048 [US1] Hardware rehearsal 1 — orderly power-off on a real cluster (`dry_run` first, then live): adopted machines go off, the poll confirms them, the Shelly arms, the controller powers off, mains cuts on an already-off box; record in `specs/001-node-role-parser/evidence/hardware-verification.md` (FR-022, SC-010)
- [ ] T049 [US2] Hardware rehearsal 2 — cold boot with nodes powered: the gate waits for the adopted machines, the show loads and arms, and `settle=45` / `armed_timeout=125` still hold; record in `specs/001-node-role-parser/evidence/hardware-verification.md` as a **separate** run from T048 (FR-022, SC-010)
- [ ] T050 Negative rehearsal, no cluster needed: an unconverted map makes `/shutdown` refuse `503` and leaves mains on, `force=1` included; record in `specs/001-node-role-parser/evidence/hardware-verification.md`

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)** — no dependencies. **T001 must precede T011**, or the evidence is lost.
- **Phase 2 (Foundational)** — depends on Phase 1 fixtures. **Blocks every user story.**
- **Phase 3 (US1)** and **Phase 4 (US2)** — both depend only on Phase 2, and are independent of each other. Either may ship alone.
- **Phase 5 (US3)** — depends on Phase 2's adapter API being settled (T010); its sibling half (T033–T036) must merge simultaneously with this repository.
- **Phase 6 (US4)** — depends on Phase 2 (T014) and on T001.
- **Phase 7 (Polish)** — T041–T044 need only Phase 2; T045–T047 need the code complete; T048–T050 need US1/US2 complete and hardware.

### Critical path

`T001 → T003/T004 → T006-T012 → (T018-T022 | T027-T030) → T045-T047 → T048-T050`

### Parallel opportunities

- Phase 1: T003 and T004 with T002.
- Phase 2: T013 and T014 in parallel once T010 lands.
- Phase 3: T015, T016, T017 together (same new file — write it once, but the three concerns are independent); implementation T018–T022 is sequential (one file).
- Phases 3 and 4 in parallel by different people: `bridge.py` shutdown path versus auto-load path.
- Phase 6: T038 parallel with Phase 7 prose tasks T041, T042, T044.

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
