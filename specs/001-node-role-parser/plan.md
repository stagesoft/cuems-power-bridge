<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Implementation Plan: Node-role parser migration

**Branch**: `001-node-role-parser` | **Date**: 2026-09-23 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-node-role-parser/spec.md`

## Summary

Delete this repository's private `network_map.xml` reader — the fourth copy of the CUEMS
node-identity model — and replace it with a thin adapter over `cuemsutils`' public
configuration path, so the role filter becomes an enum comparison against the owning
library's `NodeRole` instead of a string comparison against a vocabulary that no longer
exists in the document. That restores the two features a cluster-wide rename silently
disabled (orderly cluster power-off; the boot readiness gate), each verified separately.

Around that, the feature makes "nothing selected" impossible to confuse with "nothing to
do": the adapter returns a `Selection` that carries its own reasons, topology failures
raise a classified `TopologyError` instead of yielding an empty list, and the shutdown
handler implements the spec's five normative cases — controller-only proceeds; adopted-only
by default; refuse when the document lists machines but none is adopted; `force` targets
everything; a failed read refuses even under `force`, because *force overrides policy,
never evidence*.

The boundary with `cuems-common`'s `cuems-cluster-poweroff` is broken deliberately rather
than aliased, and the break is enforced by `Breaks:` in both directions so a half-upgrade is
refused by the package manager instead of discovered mid-poweroff (research R1).

## Technical Context

**Language/Version**: Python 3.11+ (`pyproject.toml` `python = "^3.11"`), asyncio, single
event loop

**Primary Dependencies**: `cuemsutils` (promoted from optional extra to a real, bounded
runtime dependency — the change this feature is about), `aiohttp`, `websockets`,
`python-osc`, `python3-systemd`

**Storage**: none. Two documents are *read*: `/etc/cuems/network_map.xml` and
`/etc/cuems/settings.xml`, both owned and versioned by `cuems-utils`. Nothing is written.

**Testing**: `pytest` + `pytest-asyncio` + `pytest-mock`, run through
`uv run --python 3.11 --with … --with ../cuems-utils python -m pytest -q`; baseline
**142 passed** at `f9f2c8c`

**Target Platform**: Debian 12 (bookworm) CUEMS **controller**, `dh-virtualenv` into the
**shared** venv `/usr/lib/cuems/`, service `cuems-power-bridge.service` running as
`User=cuems`

**Project Type**: single Python package (`src/cuemspowerbridge/`) shipping a daemon, an
HTTP API on `:8478`, and operator CLIs

**Performance Goals**: not throughput-bound. One constraint from the constitution's
direction-of-travel section: schema-validating reads MUST stay off the event loop
(executor + mtime-keyed cache, research R5). Shutdown timing budgets are unchanged.

**Constraints**: the shared venv (nothing another CUEMS package ships may be bundled); the
frozen venv library surface that `cuems-common` drives, changed here only under a
coordinated cutover; the two field-learned resolution policies preserved verbatim; no
existing HTTP status code, reason token or `/status` key may change meaning

**Scale/Scope**: clusters of 1–8 machines; ~3 800 lines of source, 15 test files; this
feature touches 4 source modules, the test suite, both packaging files and the README

## Constitution Check

*GATE: passed before Phase 0, re-checked after Phase 1 design (below).*

| Principle | Bears on this feature | How it is satisfied | Verified by |
|---|---|---|---|
| **I — it cuts mains power; an empty step is not a successful step** | **Centrally.** The defect is exactly this. | `Selection` carries `found`/`adopted`/`skipped` so an empty answer always states its reason; Case 3 refuses; Case 5 refuses even under `force`; `/status.node_selection` exposes it without triggering a shutdown. Fail-safe unchanged: a failed Shelly RPC still means no local poweroff. | spec Cases 1–5; contracts/http-shutdown.md; quickstart §3 |
| **II — ordered, partially irreversible; verification runs on the anomalous path** | **Yes.** `bridge.py:431`'s `if resolved:` is the constitution's named counter-example. | The reachability poll runs whenever a shutdown proceeds with targets, including a short list; Case 1's "no targets, nothing to poll" is stated in the log, not implied by silence; both refusals terminate before SSH, before the poll and before `arming-shelly`. | data-model.md §3; quickstart §3 |
| **III — it reads a schema it does not own** | **Centrally.** | The private ElementTree parser is deleted; topology comes from `ConfigManager`; `NodeRole` is imported, never redeclared; `cuemsutils.xml` is not imported (Q14). | research R2/R4; data-model.md §1 |
| **IV — designed degraded paths need real triggers** | **Yes.** The single-controller branch is the counterfeited path. | `TopologyError` and an empty `Selection` are mutually exclusive outcomes; the settle path is entered only after a successful read; the two resolution policies (avahi ignores `<ip>`; readiness trusts it) are preserved and covered by tests. | data-model.md §2.3; quickstart §2 |
| **V — hardware over the network is the normal case** | Partly. | No change to timeouts or retry budgets; the new read is bounded and cached; per-device display isolation untouched; `dry_run` still exercises every branch, including the two new refusals. | quickstart §3, §5 |
| **VI — the shared venv makes packaging a correctness concern** | **Yes.** `cuemsutils` becomes a real dependency. | `debian/rules` keeps stripping `cuemsutils*`; the gate is a `dpkg-deb -c` assertion in the task list and in evidence. | research R10; quickstart §4 |
| **VII — the external contract is the product** | **Yes**, and it is the one place this feature changes a frozen surface. | HTTP changes are additive only (new tokens, new `/status` key); the mJS is untouched; the venv library surface changes under a declared, mechanically enforced cutover with `Breaks:` both ways and `Suggests:` preserved. | contracts/*; research R1 |

**Gate result: PASS.** No violation requires justification; the Complexity Tracking table is
therefore omitted. The one deliberate breaking change (the venv library surface) is not a
principle violation but the mechanism Principle VII prescribes for changing such a surface:
declared, coordinated, and enforced by packaging rather than prose.

**Post-design re-check (after Phase 1): still PASS.** The design added no new state, no new
event-loop work, no new bundled dependency, and no change to an existing wire contract.

## Project Structure

### Documentation (this feature)

```text
specs/001-node-role-parser/
├── plan.md              # This file
├── spec.md              # Feature specification (normative: Shutdown target selection)
├── research.md          # Phase 0 — R1..R10, all unknowns resolved
├── data-model.md        # Phase 1 — borrowed model, NodeView/Selection/TopologyError
├── quickstart.md        # Phase 1 — how to prove it, in evidence order
├── contracts/
│   ├── http-shutdown.md         # the :8478 surface (additive)
│   └── venv-library-surface.md  # what cuems-common drives (changed, under cutover)
├── checklists/
│   └── requirements.md  # spec quality checklist (all items pass)
├── fixtures/            # created during implementation — schema-valid maps per case
├── evidence/            # created during implementation — recorded runs (FR-021/22/23)
└── tasks.md             # /speckit-tasks output — NOT created by /speckit-plan
```

### Source code (repository root)

```text
src/cuemspowerbridge/
├── network_map.py       # REWRITTEN: private ElementTree parser deleted; thin adapter over
│                        #   ConfigManager — NodeView, Selection, TopologyError,
│                        #   shutdown_targets(), readiness_peers()
├── bridge.py            # shutdown target build (:391); the `if resolved:` guard (:431);
│                        #   five-case handling + refusals in handle_shutdown (:348-382);
│                        #   /status node_selection (:144-165); autoload cache and
│                        #   expected-peers path (:115-116, :575-616); single-controller
│                        #   branch (:652)
├── config.py            # config_dir derivation from settings_xml_path; mismatch detection
├── reachability.py      # unchanged (surface frozen)
├── node_executor.py     # unchanged (surface frozen)
└── displays/            # unchanged (surface frozen)

tests/
├── test_network_map_ips.py   # fixtures rewritten schema-valid, both vocabularies
├── test_autoload.py          # readiness-gate half of the recovery
├── test_shutdown_cases.py    # NEW: the five cases, adopted/unadopted, refusal codes
└── test_network_map_adapter.py  # NEW: resolution policies, Selection reasons, TopologyError

debian/control       # cuemsutils floor+ceiling; Breaks: cuems-common (<< X); prose at :34
pyproject.toml       # cuemsutils out of [tool.poetry.extras], bounded
README.md            # the five cases, both meanings of force, the wall-switch consequence

../cuems-common/usr/bin/cuems-cluster-poweroff   # SIBLING, same cutover: :275 selection via
                                                 # the adapter, :240 docstring corrected
```

**Structure Decision**: single Python package, unchanged. The feature deliberately *shrinks*
`network_map.py` from a parser to an adapter and adds no module, because the point is to
delete a parallel implementation rather than to grow one.

## Implementation phases

Ordering is derived from the dependency edges, not from file convenience. `/speckit-tasks`
will expand each into tasks.

**Phase A — evidence that cannot be produced later (first, before any code change)**
Capture the pre-migration failing run (quickstart §1) into `evidence/`. Once the parser is
replaced this artifact is unobtainable, and FR-021/SC-008 require it.

**Phase B — fixtures and the adapter**
Schema-valid fixture directories per quickstart §2; then `NodeView`, `Selection`,
`TopologyError`, `shutdown_targets()`, `readiness_peers()`; delete the ElementTree parser
and its retired-vocabulary docstrings. Tests for both resolution policies and for the absent
`adopted` default land with the code.

**Phase C — the five cases in the bridge**
`handle_shutdown` refusals and codes; remove the `if resolved:` guard; `selection_mode`
through the run; `/status.node_selection`; the autoload path over the new adapter with the
single-controller branch gated on a successful read; cache keyed on both documents, in an
executor.

**Phase D — packaging and prose**
`cuemsutils` real and bounded — `>=0.1.0rc16,<0.1.1` in `pyproject.toml`, `cuems-utils
(>= 0.1.0rc16)` in `debian/control`; `cuems-common (>= 1.3.0-23)` + `Breaks: cuems-common
(<< 1.3.0-23)`, with the reciprocal `Breaks: cuems-power-bridge (<< 0.3.1-1)` in Phase E; the retired vocabulary removed from
`debian/control:34` and the README; the operator documentation FR-024 requires.

**Phase E — the sibling half**
`../cuems-common/usr/bin/cuems-cluster-poweroff` moved onto the adapter's selection function,
`:240` corrected, its `Breaks:` added, `Suggests:` untouched. Merged simultaneously with
this repository.

**Phase F — gates and evidence**
Suite green; `dpkg-deb -c` bundling assertion; retired-vocabulary count with exemptions;
the two hardware rehearsals plus the negative one (quickstart §4–§6).

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The `settings.xml` precondition depends on another repository | Not a wait: `cuems-utils` ships it in the same unreleased refactor (`0.1.0rc16`), so the floor is concrete. Residual risk is only verification — a Phase F task asserts the built `cuems-utils` `.deb` installs the file (research R2) |
| A half-upgraded pair fails mid-poweroff | `Breaks:` in both directions; a task rehearses the refusal on a test host (quickstart §4) |
| A stub library in tests re-creates the silent-empty failure in the test bed | Tests run against the real loader with schema-valid fixtures (research R6) |
| Stripping/bundling mistakes break a sibling component through the shared venv | `dpkg-deb -c` gate before shipping (research R10); this has happened before with `pythonosc` and the engine |
| "It works now" hides one of the two features still broken | Both verified separately, in separate rehearsals, recorded (quickstart §3, §5) |

## Sequencing outside this repository

All three halves are **unreleased and in flight together** — nothing here waits on a
published artifact (user, 2026-09-23):

| Repository | Version carrying its half | State |
|---|---|---|
| `cuems-utils` | `0.1.0rc16` — ships `/etc/cuems/settings.xml` | unreleased, `feat/xml-refactor` |
| `cuems-common` | `1.3.0-23` (**UNRELEASED** in its changelog) — the fixed `cuems-cluster-poweroff` lands in this same entry | unreleased, `feat/xml-refactor` |
| this package | proposed `0.3.1-1` | this feature |

- Independent of every other 010 flow: nothing here needs them and they do not need this.
- **Must** land simultaneously with `cuems-common`'s half (Phase E), expressed by the
  reciprocal `Breaks:` pair — not by prose.
- Does not release first regardless (D27: nothing ships until every 010 flow lands).
