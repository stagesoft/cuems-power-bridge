<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Phase 1 data model — node-role parser migration

**Feature**: `001-node-role-parser` · **Date**: 2026-09-23

This feature owns **no** persistent data. It consumes a document another repository owns,
and produces two selections plus a report. Everything below is either (a) the borrowed
model, named so the adapter's boundary is explicit, or (b) this repository's own value
objects.

---

## 1. Borrowed — owned by `cuemsutils`, never redefined here

| Entity | Source | Fields this feature reads | Notes |
|---|---|---|---|
| **network map** | `ConfigManager.network_map` → `CuemsNetworkMapType` | `["node_list"]` — a list of `{"node": <node>}` **wrappers** | the wrapper shape is deliberate (cuems-engine reads it); the adapter unwraps once |
| **node** | `cuemsutils.config.network_map.node` | `uuid`, `mac`, `name`, `node_role`, `ip`, `adopted`, `online`, `role_id`, `alias`, `hostname` | typed: `uuid` is `Uuid`, `node_role` is `NodeRole`, `adopted`/`online` decode to `bool` |
| **NodeRole** | `cuemsutils.tools.NodeList.NodeRole` | `controller`, `node`, `firstrun` | **imported, never re-declared** (007 FR-030a-i) |
| **this host's identity** | `ConfigManager.node_uuid` (from `settings.xml`) | the controller's own uuid | resolves the self entry |

**Schema constraints that now bind fixtures and live documents** (`network_map.xsd`):
`uuid` (canonical 8-4-4-4-12), `mac`, `name`, `node_role`, `ip` are all `minOccurs="1"`;
`adopted`, `online`, `role_id`, `alias`, `hostname` are `minOccurs="0"`.

---

## 2. Owned here — the adapter's value objects

### 2.1 `NodeView` (the shape `bridge.py` and the platform tool consume)

A read-only projection of one library node, carrying only what selection needs.

| Field | Type | Derivation | Rules |
|---|---|---|---|
| `uuid` | `str` | library `uuid`, stringified | stable key; used for self-exclusion |
| `role` | `NodeRole` | library `node_role` | **imported enum**; no string comparison anywhere |
| `adopted` | `bool` | library `adopted`, **absent ⇒ `False`** | research R3 |
| `role_id`, `alias`, `hostname` | `str \| None` | library fields | resolution inputs, in this order |
| `ip` | `str \| None` | library `ip` | **only** the readiness gate may use it |
| `avahi` | `str \| None` | `role_id` → `alias` → `hostname`, first non-empty, suffixed `.local` | **never** derived from `ip`; `None` ⇒ unresolvable |
| `is_self` | `bool` | `uuid == ConfigManager.node_uuid` | corroborated by local addresses/hostname where the uuid is unavailable |

**Invariant**: `NodeView` has no `node_type` attribute and no string role. A reader looking
for the retired vocabulary fails loudly at attribute access rather than silently matching
nothing.

### 2.2 `Selection` (the answer, with its reasons)

Returned by both selection functions so callers never have to infer why a list is short.

| Field | Type | Meaning |
|---|---|---|
| `mode` | `"adopted" \| "forced_all" \| "controller_only"` | which policy produced this answer |
| `targets` | `list[str]` (avahi names) or `list[tuple[str, str]]` (ip, label) | what to act on |
| `found` | `int` | non-self machines present in the document |
| `adopted_count` | `int` | of those, adopted |
| `skipped` | `list[Skip]` | every machine not targeted, with a reason |

`Skip` = `(uuid, name_or_uuid, reason)` where `reason ∈ {unadopted, unresolvable,
no_ip, self}`. **Every** non-targeted machine appears exactly once; the union of `targets`
and `skipped` is the full non-self machine set. This is the structural expression of "an
empty answer must carry its reason" (constitution Principle I).

### 2.3 `TopologyError` (the Case 5 carrier)

One exception type raised by the adapter, wrapping every library failure with a
classification the HTTP layer maps to a refusal:

| `kind` | Raised when |
|---|---|
| `settings_xml_missing` | identity document absent (packaging fault — names the providing package) |
| `settings_xml_invalid` | identity document present, fails its schema |
| `network_map_missing` | topology document absent |
| `network_map_invalid` | topology document fails its schema (incomplete entry, bad uuid, …) |
| `network_map_retired_vocabulary` | the load reports the retired element — message names the conversion tool |
| `self_entry_missing` | this host's uuid has no entry in the document |
| `config_dir_mismatch` | `settings_xml_path` and `network_map_path` name different directories |

**Rule**: `TopologyError` is never caught and converted into an empty `Selection`. The two
are mutually exclusive outcomes — that mutual exclusivity *is* the feature.

---

## 3. State transitions — the shutdown sequence

Existing states (`bridge.py:42`) are unchanged: `idle → checking → polling →
arming-shelly → poweroff-issued → done`, plus `failed`.

This feature adds **no state**, and adds two refusals that terminate at `idle` before any
irreversible step, plus a `selection_mode` detail carried through the run:

```
checking ──(TopologyError)────────────► idle   reason=topology_unreadable      [Case 5]
checking ──(found>0, adopted=0, !force)► idle   reason=no_adopted_nodes         [Case 3]
checking ──(found=0)──────────────────► …      selection_mode=controller_only  [Case 1]
checking ──(adopted>0, !force)────────► …      selection_mode=adopted          [Case 2]
checking ──(force)────────────────────► …      selection_mode=forced_all       [Case 4]
```

Both refusals occur **before** SSH fan-out, before the reachability poll and before
`arming-shelly` — no partially executed shutdown, and mains is never armed.

`polling` is entered whenever `targets` is non-empty (Cases 2 and 4), including when the
list is shorter than `found`. In Case 1 there is nothing to poll: the log states this
explicitly rather than leaving silence to be read as success.

---

## 4. Validation rules (traceable to requirements)

| Rule | Source |
|---|---|
| Role comparison is against the imported enum; no string literal of the retired vocabulary in shipped code | FR-002, FR-007, FR-023 |
| `avahi` never derives from `ip`; unresolvable machines are reported, not dropped | FR-005 |
| Readiness peers are identified by `ip`, adopted only, and machines without `ip` are skipped with a warning | FR-006, FR-009 |
| Absent `adopted` ⇒ not adopted | R3, FR-008 |
| A topology failure raises `TopologyError`; it never becomes an empty `Selection` | FR-003, FR-004 |
| `force` flips `include_unadopted`; it cannot suppress `TopologyError` | FR-011 |
| Fixtures and live documents must satisfy the owning schema | FR-020, Case 5 |

---

## 5. What is deliberately NOT modelled here

- The node model itself (merge, adopt, conversion, signature) — `cuemsutils` owns it, and a
  test of it appearing in this repository is a regression, not coverage (D11).
- Liveness. `online` is read but unused: runtime liveness comes from the reachability poll
  and the NNG bus, never from the document.
- Anything about the platform tool's own selection logic beyond the `NodeView`/`Selection`
  shapes it will consume after the cutover (research R1).
