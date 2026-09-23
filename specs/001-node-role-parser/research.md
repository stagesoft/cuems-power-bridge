<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Phase 0 research — node-role parser migration

**Feature**: `001-node-role-parser` · **Date**: 2026-09-23
**Method**: measured against the live trees, per the constitution's *Measure, do not
transcribe* rule. Coordinates below were re-verified on
`cuems-power-bridge` @ `d14a786`, `cuems-utils` @ `ce5b5b0` (`feat/xml-refactor`),
`cuems-common` @ `11fab0e` (`feat/xml-refactor`).

---

## R0. Coordinates re-verified (the planning bundle's numbers still hold)

| Claim | Live location | Status |
|---|---|---|
| defect site 1 | `network_map.py:110` `if n.node_type != "NodeType.slave"` in `slave_avahi_names` | confirmed |
| defect site 2 | `network_map.py:141` same comparison in `slave_ips` | confirmed |
| the read that yields `None` | `network_map.py:94` `node_type=_text(el, "node_type")` | confirmed |
| dataclass field + retired comment | `network_map.py:33` | confirmed |
| retired vocabulary in docstrings | `network_map.py:5,9,58-59,120` | confirmed (4 sites) |
| shutdown target build | `bridge.py:391` | confirmed |
| the skipped verification | `bridge.py:431` `if resolved:` | confirmed |
| Shelly arming | `bridge.py:458-459` | confirmed |
| autoload cache / expected IPs | `bridge.py:115-116, 575-593, 594-616` | confirmed |
| single-controller branch | `bridge.py:652` | confirmed |
| third defect site (sibling) | `../cuems-common/usr/bin/cuems-cluster-poweroff:275`, stale docstring `:240` | confirmed |

---

## R1. Compatibility with `cuems-common`'s heredoc — **the pivotal decision**

**Decision: land both repositories in the same cutover, and express it mechanically with
`Breaks:` in both directions. The adapter keeps the *function* `parse()` but the node
objects carry `node_role`, never `node_type`.**

**Rationale.** The alternative — keeping a `node_type` attribute populated with
`"NodeType.slave"` derived from the new role — would keep an old `cuems-cluster-poweroff`
working untouched, but it keeps the retired vocabulary alive in shipped code, violating
FR-023, decision D33 (a half-renamed state is not shippable) and the constitution's
Principle III. It also leaves the ecosystem with a compatibility shim nobody has a date to
remove. The cutover is a single coordinated merge that both repositories are already on a
branch for.

**Consequence for upgrade ordering, which the packaging must enforce:**

- this package gains `Breaks: cuems-common (<< <first version carrying the fixed tool>)`
- `cuems-common` gains `Breaks: cuems-power-bridge (<< <this feature's version>)`
- `cuems-common`'s `Suggests: cuems-power-bridge` is **preserved unversioned** — it must
  stay a suggestion so `cuems-common` remains fully functional on a host with no bridge
  (both its scripts log one ERROR and exit 0 when the venv interpreter is absent)
- effect: `apt` upgrades the pair together; `dpkg -i` of one half alone is refused, instead
  of failing at the next poweroff with `AttributeError` mid-transaction

**Alternatives considered.** (a) Legacy alias attribute — rejected above. (b) Change only
`cuems-common` and leave the private parser — rejected by D32/D11; leaves two more defect
sites live. (c) Absorb the tool into this package now, removing the boundary entirely —
correct direction (`specs/planning/cuems-common-machinery-to-absorb.md`), but explicitly
out of scope here; doing it under this feature would couple a safety fix to a packaging
migration.

---

## R2. Constructing the library's reader, and the `settings.xml` precondition

**Measured.** `ConfigManager.__init__(config_dir=CUEMS_CONF_PATH, load_all=True)`
(`ConfigManager.py:114`) calls `ConfigBase.load_base_settings()` **unconditionally**, which
reads `<config_dir>/settings.xml` and lets `OSError` (`FileNotFoundError`,
`PermissionError`) propagate **unwrapped**, and raises `SchemaError` when the file exists
but does not validate (`ConfigBase.py:91-116`). `load_network_map()` reads
`<config_dir>/network_map.xml` and eagerly resolves this host's own entry via
`node_uuid` (`ConfigManager.py:212, 284-299`).

**Measured, and load-bearing for tests:** `load_base_settings` honours the environment
variable `CUEMS_CONF_PATH` **over** the `config_dir` argument (`ConfigBase.py:109-113`).
Tests must therefore set that variable, not only pass a directory.

**Decision.**
- The adapter derives the library's `config_dir` from `dirname(cfg.settings_xml_path)`
  (default `/etc/cuems`), and asserts that `cfg.network_map_path` lives in the same
  directory. If an operator has pointed the two config keys at different directories, that
  is a configuration fault reported at start, not something to paper over — the library
  reads both from one directory by design.
- It constructs with `load_all=False` and calls `load_network_map()` explicitly: the bridge
  needs topology, not the whole configuration set.
- Every failure of that construction or load is classified into the **Case 5** refusal
  (spec) with a specific `last_error` detail: `settings_xml_missing`,
  `settings_xml_invalid`, `network_map_missing`, `network_map_invalid`,
  `network_map_retired_vocabulary`, `self_entry_missing`.
- Q2 makes `settings.xml` a shipped file, so its absence is a **packaging fault**: the
  message names the providing package, not the operator.

**Open external dependency (tracked, not resolved here).** The `cuems-utils` checkout on
`feat/xml-refactor` has **no `debian/` directory** — its packaging lives on the
`debian/bookworm` branch — and nothing in the source tree installs `/etc/cuems/settings.xml`
today. The version floor required by FR-016 therefore **cannot be written yet**: it is the
first `cuems-utils` release that ships the file. The plan records this as a blocking
external dependency with a placeholder floor to be filled at release time, not guessed.

**Alternatives considered.** A library entry point that validates a map without loading
base settings would remove the precondition, but Q14 settles that consumers use
`ConfigManager` rather than `cuemsutils.xml`, and inventing a second public path is
cuems-utils' decision, not this feature's.

---

## R3. What `adopted` means when it is absent

**Measured.** In `network_map.xsd` the node element declares `adopted` with
`minOccurs="0"` (line 29) — it is **optional**; `online` likewise. In
`cuemsutils/config/network_map.py:75-81` the node's `DECLARED_DEFAULTS` give `adopted` the
sentinel `Unset`. `NodeIndex.set_controller_always_adopted()` marks controllers adopted;
`cuems-nodeconf` sets the flag for other machines on adoption.

**Decision: absent `adopted` is treated as NOT adopted for targeting — and that is safe
precisely because of spec Case 3.** A machine with no flag is never silently powered off;
it is named in the log as skipped, and if *no* machine in the document is adopted the
shutdown **refuses** rather than proceeding with an empty target list. A pre-adoption map
therefore produces a loud, actionable refusal naming every machine it skipped, whose remedy
is "adopt them, or use `force`".

**Rationale.** The opposite default (absent ⇒ adopted) would make an unflagged machine a
shutdown target, which is a power-off decision taken on missing data — the exact class of
inference this feature exists to remove. The chosen default can only ever *withhold* a
power-off, and never silently: Case 3 converts "nothing to do" into a refusal.

**Consequence to document for operators (FR-024).** On a cluster whose map predates the
adoption flag, an unforced shutdown refuses until the machines are adopted; the wall
switch, which sends `force`, still powers everything off.

---

## R4. Where the role and adoption filters live

**Decision: in the adapter, as two named selection functions with explicit parameters** —
not scattered at the call sites:

- `shutdown_targets(..., include_unadopted: bool)` → resolved avahi names + a report of
  what was skipped and why (unresolvable, unadopted, self), and a `partial` flag when some
  machine that should have been targeted is unresolvable (spec *Partial resolution*)
- `readiness_peers(...)` → `(ip, label)` for adopted machines carrying an `<ip>`

**Rationale.** D11 forbids re-implementing the node model, but *selection policy* is this
repository's own domain and is exactly what regressed. One place to test, one place for
`cuems-cluster-poweroff` to call after the cutover, and the `force` semantics become a
single boolean at the boundary rather than a condition repeated in two modules.

**Rejected.** Filtering inside `bridge.py` at each call site — that is today's shape, and
it is how two call sites drifted into the same bug twice.

---

## R5. Reading cost, caching and the event loop

**Measured.** Today `slave_ips()` is a bare `ElementTree` parse behind an mtime-keyed cache
(`bridge.py:115-116, 575-593`) executed in a worker thread; `slave_avahi_names()` is called
directly on the event loop from `_run_shutdown`.

**Decision.** One cached, mtime-keyed load of the library's network-map object serves both
features, and it runs in an executor in both paths (shutdown included). The library
validates against the schema on every load, which is strictly more work than the old parse,
and the constitution's direction-of-travel section makes "parsing that grows with traffic
stays off the event loop" a standing rule.

**Cache invalidation** keys on the mtime of **both** `network_map.xml` and `settings.xml`,
since the identity document now participates in the answer.

---

## R6. Making `cuemsutils` available to the test suite

**Measured.** `pyproject.toml:36,40` declare `cuemsutils` **optional** (extra
`production`), and it is imported nowhere in `src/` or `tests/` — the suite has never
needed it. The suite is green at 142 tests via the documented `uv run --with …` line.

**Decision: a real, non-optional runtime dependency, and a real test dependency — no stub
of the library.** The suite gains `--with cuemsutils` (resolved from the sibling checkout
during development, from the released artifact in CI). Fixtures are schema-valid documents
on disk, and the adapter is exercised against the **real** loader, because half of what
this feature buys is the library's strictness: a stub would re-create the silent-empty
failure mode in the test bed while the tests claim to guard against it.

**What is still not tested here (D11).** No test asserts the node model's own behaviour —
role enumeration, conversion, merge. Fixtures exist to drive the *adapter*; the retired-
vocabulary fixture asserts that the load **raises**, which is a statement about this
repository's error handling, not a re-test of the library.

---

## R7. Refusal vocabulary and status surface

**Measured.** `handle_shutdown` (`bridge.py:348-382`) already returns `{"ok": false,
"reason": "<token>"}` with 401 `bad_token`, 409 `shutdown_already_in_progress`, 409
`project_running`, 503 `engine_state_unknown`, 502 `shelly_unreachable`, 500
`internal_error`; `/status` exposes `state`, `nodes_pending`, `last_error` and friends
(`bridge.py:144-165`); states are the fixed list at `bridge.py:42`.

**Decision — additive only** (no existing token or code changes meaning):

| Case | HTTP | `reason` | new `state` |
|---|---|---|---|
| 3 — machines exist, none adopted, no `force` | 409 | `no_adopted_nodes` | `idle` (with `last_error`) |
| 5 — topology unreadable (any sub-reason) | 503 | `topology_unreadable` | `idle` (with `last_error` detail) |
| 1 — controller-only, proceeds | 200 | — | new terminal detail `selection_mode: "controller_only"` |
| 2 — normal | 200 | — | `selection_mode: "adopted"` |
| 4 — forced | 200 | — | `selection_mode: "forced_all"` |

`/status` gains a `node_selection` object — `{mode, found, adopted, targeted, skipped}` —
satisfying FR-014 without disturbing the keys Companion and `cuems-displays-on` already
parse. 409 is used for policy refusals (an operator can override with `force`), 503 for
"the bridge cannot know" (`force` does not help), matching the existing split between
`project_running` (409) and `engine_state_unknown` (503).

---

## R8. Evidence artifacts

**Decision.** The recorded failing run required by FR-021/SC-008 lives at
`specs/001-node-role-parser/evidence/pre-migration-parser-failure.txt`: the current-
vocabulary fixture executed against the pre-migration parser, captured with the command
line, the date and the commit it was run at. The retired-vocabulary count for FR-023/SC-011
lands beside it as `retired-vocabulary-count.txt`, including the exempt set with reasons.

---

## R9. Hardware verification (FR-022, SC-010)

**Decision.** Two separate rehearsals on a real cluster, recorded in
`evidence/hardware-verification.md`:

1. **Power-off** — `dry_run=true` first (the target list is the artifact), then a live
   run: the adopted machines go off, the reachability poll confirms them, the Shelly arms,
   the controller powers off, and mains cuts on an already-off box.
2. **Auto-load** — a cold boot with the nodes powered on: the gate waits for the adopted
   machines, the show loads and arms, and the proven timings (`settle=45`,
   `armed_timeout=125`) still hold.

Plus one negative rehearsal that needs no cluster: an unconverted map on a test host must
refuse `/shutdown` with 503 and leave mains on.

---

## R10. Shared-venv verification (constitution Principle VI)

**Measured.** `debian/rules:37` already strips `cuemsutils*` from the built tree, and
`debian/control:18` already declares `cuems-utils (>= 0.1.0rc5)`.

**Decision.** Promoting `cuemsutils` to a real dependency must leave the stripping intact;
the gate is a `dpkg-deb -c` of the built `.deb` asserting no `cuemsutils*` path, run as a
task in this feature and recorded in the evidence directory. The `.deb` must also still
bundle the aiohttp stack (unique to this package).

---

## Resolved unknowns

| Unknown from Technical Context | Resolution |
|---|---|
| How the tool/bridge boundary survives the cutover | R1 — both repositories land together, `Breaks:` both ways, `Suggests:` preserved |
| How the library reader is constructed, and what a missing `settings.xml` does | R2 — `config_dir` from `settings_xml_path`, `load_all=False`, every failure classified into Case 5 |
| Meaning of an absent `adopted` flag | R3 — not adopted; Case 3 makes it loud |
| Where the filters live | R4 — adapter, two named selection functions |
| Cost and threading of the new read | R5 — one mtime-keyed cached load, in an executor, keyed on both documents |
| How the suite gets `cuemsutils` | R6 — real dependency, real loader, schema-valid fixtures |
| New refusal tokens and status fields | R7 — additive; 409 policy, 503 unknowable |
| Where evidence lives | R8, R9, R10 — `specs/001-node-role-parser/evidence/` |

## Versions — all three halves are unreleased and land together

Measured 2026-09-23, confirmed by the user: nothing here waits on a published release. The
whole refactor is in flight across the three repositories simultaneously.

| Repository | Version carrying its half | State |
|---|---|---|
| `cuems-utils` | `0.1.0rc16` (`src/cuemsutils/__init__.py:4`) — ships `/etc/cuems/settings.xml` | unreleased, on `feat/xml-refactor` |
| `cuems-common` | `1.3.0-23` (`debian/changelog:1`, marked **UNRELEASED**) — carries the fixed `cuems-cluster-poweroff` | unreleased, on `feat/xml-refactor` |
| `cuems-power-bridge` | **`0.3.1-1`** — settled 2026-09-23 (behaviour change: three new refusals, adoption filtering) | this feature |

Resulting version relationships, concrete rather than placeheld:

- `pyproject.toml`: `cuemsutils = ">=0.1.0rc16,<0.1.1"`, non-optional
- `debian/control` (this package): `cuems-utils (>= 0.1.0rc16)`, `cuems-common (>= 1.3.0-23)`,
  `Breaks: cuems-common (<< 1.3.0-23)`
- `cuems-common/debian/control`: `Breaks: cuems-power-bridge (<< 0.3.1-1)`, `Suggests:`
  unchanged and unversioned

Since `1.3.0-23` is still open, the fix to `cuems-cluster-poweroff` lands **in that same
changelog entry** rather than opening a new revision.

Open only as verification, not as unknowns:

- confirm the built `cuems-utils` `0.1.0rc16` `.deb` installs `/etc/cuems/settings.xml`
  (Phase F). The bridge's own number is settled at `0.3.1-1`, so the reciprocal `Breaks:` in
  `cuems-common` can be written without waiting.
