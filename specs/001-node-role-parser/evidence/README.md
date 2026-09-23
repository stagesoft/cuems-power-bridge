<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Evidence — feature 001, node-role parser migration

What was actually run, and when. In this defect class a passing suite is not
evidence (the suite was green *because* its fixtures were written in the
retired vocabulary), so the artifacts below are part of the deliverable.

| File | Task | What it shows |
|---|---|---|
| `pre-migration-parser-failure.txt` | T001 | **The defect, captured before it was fixed.** The old reader against a schema-valid, current-vocabulary map: `([], [])` and `[]`. Two adopted nodes selected as zero, with an empty unresolvable list, so not even the ERROR path fired. **Unreproducible now** — the reader it exercised no longer exists. |
| `baseline-suite.txt` | T002 | The suite before the feature: 142 passed. Implementation does not start on a red suite. |
| `fixture-validation.txt` | T004 | Every fixture validated against `network_map.xsd`; the two that fail are deliberately invalid, and their cases are "the load MUST raise". |
| `retired-vocabulary-count.txt` | T048 | Zero occurrences in shipped code and shipped prose, with the exempt set enumerated and `wsclient.py`'s `master.local` excluded **by reason** (it is an mDNS hostname, not the role field). |
| `final-suite.txt` | T055 | The suite after the feature: 185 passed. |
| `deb-contents.txt` | T053 | **Pending** — the built `.deb` must bundle no `cuemsutils` (the shared venv already has it) and must still bundle the aiohttp stack. Needs a build host. |
| `settings-xml-provenance.txt` | T054 | **Pending** — confirm the built `cuems-utils` 0.1.0rc16 `.deb` installs `/etc/cuems/settings.xml`. Its packaging lives on that repo's `debian/bookworm` branch. |
| `upgrade-refusal.txt` | T045 | **Pending** — rehearse the refused half-upgrade (this package beside an un-fixed `cuems-common`) on a test host. |
| `hardware-verification.md` | T056–T058 | **Pending** — the two rehearsals on a real cluster (power-off; auto-load), verified **separately**, plus the negative rehearsal (an unconverted map must refuse 503 and leave mains on, `force=1` included). |

## The one that matters most

`pre-migration-parser-failure.txt` is the artifact the whole feature is
measured against, and the only one with a deadline: it had to be captured
before the parser was deleted. It records that the parse itself was fine —
all three nodes resolved to `controller.local`, `node01.local`,
`node02.local` — and that every one carried `node_type=None`, so the
**filter** discarded them. Silently.
