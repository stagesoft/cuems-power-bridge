<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Quickstart — validating the node-role parser migration

**Feature**: `001-node-role-parser` · **Date**: 2026-09-23

How to prove this feature works, in the order the evidence should be produced. Details of
the selections live in [data-model.md](./data-model.md); the wire behaviour lives in
[contracts/](./contracts/).

---

## 0. Prerequisites

- This repository at branch `001-node-role-parser`.
- The sibling `../cuems-utils` on `feat/xml-refactor` (the library under test).
- The sibling `../cuems-common` on `feat/xml-refactor` (the other half of the cutover).
- No cluster needed until §5.

**The runner** (the default interpreter has none of the dependencies):

```bash
uv run --python 3.11 \
  --with pytest --with pytest-asyncio --with pytest-mock \
  --with aiohttp --with websockets --with python-osc \
  --with ../cuems-utils \
  python -m pytest -q
```

Baseline before any change: **142 passed**. Never start implementation on a red suite.

**Note**: the library honours the environment variable `CUEMS_CONF_PATH` *over* any
`config_dir` argument, so fixtures are pointed at a temporary directory by setting that
variable, not only by passing a path.

---

## 1. Reproduce the defect (before changing anything)

```bash
uv run --python 3.11 --with ../cuems-utils python - <<'PY'
from cuemspowerbridge import network_map
print(network_map.slave_avahi_names("specs/001-node-role-parser/fixtures/map-post007.xml"))
print(network_map.slave_ips("specs/001-node-role-parser/fixtures/map-post007.xml"))
PY
```

Expected, and the whole reason this feature exists: `([], [])` and `[]` — no error, no
warning, no unresolvable list. **Capture this output** into
`evidence/pre-migration-parser-failure.txt` together with the command, the date and the
commit — it is the FR-021/SC-008 artifact, and it cannot be produced after the parser is
replaced.

---

## 2. Unit level — the adapter

Each of the six shutdown cases and the readiness gate is driven by a fixture directory
under `tests/fixtures/network_map/`, containing a schema-valid `network_map.xml` plus a
`settings.xml`. The authoritative list of `TopologyError` kinds is
[data-model.md](./data-model.md) §2.3; this table is the fixture inventory:

| Fixture | Shape | Expected |
|---|---|---|
| `map-controller-only` | self entry only | Case 1: `mode=controller_only`, no targets, no error |
| `map-two-adopted` | self + 2 adopted | Case 2: both targeted, `skipped=[]` |
| `map-none-adopted` | self + 2 unadopted | Case 3: `TopologyError` **not** raised; selection empty with both machines in `skipped(unadopted)` → caller refuses |
| `map-mixed` | self + 1 adopted + 1 unadopted | Case 2 targets one, names the other in `skipped` |
| `map-unresolvable` | self + 2 adopted, neither with `role_id`/`alias`/`hostname` | Case 3b: refused `no_resolvable_nodes`, with and without `force` |
| `map-partial-resolve` | self + 1 adopted addressable + 1 adopted unresolvable | proceeds, `partial=true`, ERROR names the unresolvable one |
| `map-pre007` | retired vocabulary | Case 5: load raises, classified `network_map_retired_vocabulary` |
| `map-no-self` | no entry for this host | Case 5: `self_entry_missing` |
| `map-incomplete` | a node missing `<mac>` | Case 5: `network_map_invalid` |
| (no `settings.xml`) | identity document absent | Case 5: `settings_xml_missing`, message names the providing package |

Policy assertions that must be covered explicitly, because they are field-learned and easy
to "simplify" away:

- a machine with `role_id`, `alias` **and** `hostname` resolves by `role_id`;
- a machine with only `hostname` resolves by `hostname`;
- a machine with none of the three is **reported unresolvable**, never dropped, and its
  `<ip>` is never used; all-unresolvable refuses (Case 3b), some-unresolvable proceeds with
  `partial=true`;
- the readiness gate **does** use `<ip>`; its skip-with-warning for a machine without one is
  a defensive branch (the schema makes `<ip>` mandatory, research R11) and is covered by
  constructing that state directly, not by a document fixture;
- an absent `adopted` element counts as not adopted.

---

## 3. Behaviour level — the two features, separately

**Shutdown** (`dry_run=true`, no hardware):

```bash
curl -fsS -X POST -H "X-Auth-Token: $TOKEN" localhost:8478/shutdown        # Cases 1-3,5
curl -fsS -X POST -H "X-Auth-Token: $TOKEN" 'localhost:8478/shutdown?force=1'  # Case 4
curl -fsS localhost:8478/status | jq .node_selection
```

Assert per [contracts/http-shutdown.md](./contracts/http-shutdown.md): the codes
(200/409/503), the reason tokens, and that `node_selection` distinguishes
`controller_only` from `read_ok: false`. Assert too that Case 3 and Case 5 never reach
`arming-shelly` — the state must go back to `idle`.

**Auto-load**, driven independently: with a map listing two adopted machines with `<ip>`,
the gate waits for both; with a map listing no adopted machine at all, it takes the
single-controller settle path and says so; with an unreadable map, it does
**not** take that path and reports the failure. (The "adopted but address-less" state is
unreachable through a valid document — research R11.)

"The bridge works now" is not an answer to either — they are separate runs.

---

## 4. Packaging gates

```bash
dpkg-buildpackage -b -uc -us          # .deb lands in the PARENT directory
dpkg-deb -c ../cuems-power-bridge_*.deb | grep -c 'site-packages/cuemsutils'   # MUST be 0
dpkg-deb -c ../cuems-power-bridge_*.deb | grep -c 'site-packages/aiohttp'      # MUST be > 0
```

Then, on a test host, confirm the pair refuses to half-upgrade: installing this package
beside an un-fixed `cuems-common` must be rejected by `dpkg` (the `Breaks:` from
[contracts/venv-library-surface.md](./contracts/venv-library-surface.md)), not discovered
later.

---

## 5. Real hardware (FR-022 / SC-010)

Two rehearsals on a real cluster, recorded in `evidence/hardware-verification.md`:

1. **Orderly power-off** — `dry_run=true` first (the logged target list is the artifact),
   then live: adopted machines go off, the poll confirms them, the Shelly arms, the
   controller powers off, mains cuts on an already-off box.
2. **Auto-load** — cold boot with nodes powered: the gate waits for the adopted machines,
   the show loads and arms, and the proven timings (`settle=45`, `armed_timeout=125`) still
   hold.

Plus one negative rehearsal that needs no cluster: an unconverted map must make
`/shutdown` refuse with 503 and leave mains on, `force=1` included.

---

## 6. Final counts

```bash
git grep -nE 'NodeType\.(master|slave)' -- src debian README.md   # exemptions only
```

Expected: only the deliberately retired-vocabulary fixture (which exists to prove the old
document still fails), enumerated with its reason in
`evidence/retired-vocabulary-count.txt`. `wsclient.py`'s `master.local` default is an mDNS
hostname and is excluded by reason, not by oversight.
