<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Quickstart — validating the cluster power-off CLI

**Feature**: `002-cluster-poweroff-cli` · **Date**: 2026-09-23

How to prove this feature works. Details of the sequence live in
[data-model.md](./data-model.md); the command surface lives in
[contracts/cli.md](./contracts/cli.md).

The governing question for every check below: **did the product path change?** It must not.

---

## 0. Prerequisites

- This repository on `002-cluster-poweroff-cli`, with feature 001 merged.
- The sibling `../cuems-common` on `feat/xml-refactor` (@ `df7e354` or later).
- Fixtures from feature 001 (`tests/fixtures/network_map/`) — reused unchanged.

Runner:

```bash
uv run --python 3.11 --with pytest --with pytest-asyncio --with pytest-mock \
  --with aiohttp --with websockets --with python-osc --with ../cuems-utils \
  python -m pytest -q
```

Baseline: **185 passed** (feature 001's final state).

---

## 1. Equivalence — the reason the feature exists

For each of the six outcomes, drive the same fixture through **both** entry points and
compare the decision, not just the result:

| Fixture | Expected, identically from both |
|---|---|
| `map-controller-only` | proceeds; no targets; "controller-only" stated |
| `map-two-adopted` | both machines targeted, polled, then mains armed |
| `map-mixed` | one targeted, the unadopted one named as skipped |
| `map-none-adopted` | refused — HTTP 409 `no_adopted_nodes` / exit 4 |
| `map-unresolvable` | refused — HTTP 409 `no_resolvable_nodes` / exit 4, with and without force |
| `map-pre007` | refused — HTTP 503 `topology_unreadable` / exit 4, with and without force |
| `map-partial-resolve` | proceeds, marked partial, unreachable machine logged at ERROR |

The equivalence test is the deliverable: parameterise over the fixtures, run both callers,
assert the decisions match field by field. A test that only checks the CLI works has missed
the point.

---

## 2. The product path is untouched

Three checks, in increasing strength:

1. **By test** — a transition-mode run (`--transition`) against a playing project proceeds,
   exactly as the heredoc does today.
2. **By call graph** — assert that `cluster_shutdown.py` contains no reference to the engine,
   to `/status`, or to the guard; the guard exists only in `scripts/cluster_poweroff.py`
   before the sequence is entered (SC-010). A grep-based test is legitimate here: the claim
   is about reachability, not behaviour.
3. **By diff** — the sequence's steps, their order and their log lines are unchanged from
   `bridge.py:508-644`; review the extraction as a move, not a rewrite.

---

## 3. The lock

```bash
# hold it, then try again
flock /run/cuems-power-bridge/shutdown.lock -c 'sleep 30' &
cuems-power-bridge-cluster-poweroff --stage nodes --dry-run   # exit 4, names the holder
```

- a second CLI run refuses immediately (never blocks);
- `POST /shutdown` while the CLI holds it returns 409;
- killing the holder (`kill -9`) leaves the next run able to proceed — no cleanup step.

---

## 4. The running-show guard (manual only)

| Setup | Expect |
|---|---|
| daemon up, project playing, no `--force` | exit 4, message names the project |
| daemon up, project playing, `--force` | proceeds |
| daemon up, nothing playing | proceeds, no flag needed |
| daemon down | proceeds (probe fails, no guard) |
| `--transition` | probe never runs, whatever the daemon says |

---

## 5. The other package

```bash
grep -c 'cuemspowerbridge' ../cuems-common/usr/bin/cuems-cluster-poweroff   # MUST be 0
grep -c 'cuemspowerbridge' ../cuems-common/usr/bin/cuems-displays-on        # MUST be 0
bash -n ../cuems-common/usr/bin/cuems-cluster-poweroff
```

And the degradation paths, which are the reason the wrapper stays:

- no tool on `PATH` (this package absent) → one ERROR line, exit 0, shutdown completes;
- `enabled=false` → nothing runs at all;
- `nodes_off=false` → displays stage only.

---

## 6. Packaging

```bash
dpkg-deb -c ../cuems-power-bridge_*.deb | grep -E 'usr/bin/cuems-power-bridge-(cluster-poweroff|config)'
dpkg-deb -c ../cuems-power-bridge_*.deb | grep 'tmpfiles.d/cuems-power-bridge.conf'
dpkg-deb -c ../cuems-power-bridge_*.deb | grep -c 'site-packages/cuemsutils'   # still 0
```

Then the staged upgrade, in order (research R7): **bridge first**, then cuems-common. Verify
new-bridge + old-common still performs an orderly power-off through the old heredocs — that
is what makes this feature staged rather than a cutover.

---

## 7. Real hardware

1. **Wall switch** — flip it; the cluster powers off exactly as before, and the journal shows
   the new tool's lines rather than the heredoc's.
2. **API** — `POST /shutdown` on the same cluster; compare the two journals: same machines,
   same order, same decisions.
3. **Manual rehearsal** — `--stage all --dry-run` during a show: refused without `--force`,
   proceeds with it, and nothing is touched either way.
