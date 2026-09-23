<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Contract — HTTP surface (`:8478`) after this feature

**Feature**: `001-node-role-parser` · **Status**: additive only. No existing status code,
reason token or `/status` key changes meaning. Consumers that must keep working unchanged:
the Shelly mJS, Bitfocus Companion configurations, and `cuems-common`'s
`cuems-displays-on` (which parses `GET /status` and posts `/poweron`).

Envelope, unchanged: `{"ok": bool, "reason"?: "<token>"}`.

---

## `POST /shutdown[?force=1]`

### Outcomes

| # | Precondition | Code | Body | Mains |
|---|---|---|---|---|
| — | bad/missing token | 401 | `{"ok": false, "reason": "bad_token"}` | untouched |
| — | another shutdown in flight | 409 | `… "shutdown_already_in_progress"` | untouched |
| — | engine state unknown, `force` unset, `refuse_if_running` | 503 | `… "engine_state_unknown"` | untouched |
| — | project running, `force` unset, `refuse_if_running` | 409 | `… "project_running"` | untouched |
| **5** | topology unreadable — **`force` does NOT override** | **503** | `{"ok": false, "reason": "topology_unreadable", "detail": "<kind>"}` | **untouched** |
| **3** | machines exist, none adopted, `force` unset | **409** | `{"ok": false, "reason": "no_adopted_nodes", "found": <n>, "skipped": [<name>, …]}` | **untouched** |
| **1** | read OK, only the self entry | 200 | `{"ok": true}` | cut after the timer |
| **2** | read OK, adopted machines targeted | 200 | `{"ok": true}` | cut after the timer |
| **4** | `force=1` — every machine targeted | 200 | `{"ok": true}` | cut after the timer |
| — | Shelly RPC failed after retries | 502 | `… "shelly_unreachable"` | **untouched** (fail-safe) |

`detail` for `topology_unreadable` is one of: `settings_xml_missing`,
`settings_xml_invalid`, `network_map_missing`, `network_map_invalid`,
`network_map_retired_vocabulary`, `self_entry_missing`, `config_dir_mismatch`.

### `force=1` semantics — two effects, one non-effect

1. overrides the running-project refusal (existing behaviour, unchanged);
2. overrides the adoption filter: **every** machine in the document is targeted;
3. does **not** override `topology_unreadable` — *force overrides policy, never evidence*.

**Operator-visible consequence**: the shipped Shelly mJS sends `force=1` by default
(`FORCE = true`), so the wall switch powers off every machine and does not stop for a
running show. `cuems-power-bridge-install-mjs --safe` sets `FORCE = false`, after which the
switch behaves as Cases 2/3 and refuses while a project runs.

### Status-code policy

- **409** — a *policy* refusal the operator can override with `force`.
- **503** — the bridge cannot know; `force` does not help. Consistent with the existing
  `project_running` (409) / `engine_state_unknown` (503) split.

---

## `GET /status` (unauthenticated)

All existing keys keep their shape and meaning. One key is added:

```json
{
  "node_selection": {
    "mode": "adopted | forced_all | controller_only | none",
    "found": 3,
    "adopted": 2,
    "targeted": 2,
    "skipped": [
      {"node": "node03", "reason": "unadopted"}
    ],
    "source": "network_map.xml",
    "read_ok": true,
    "read_error": null
  }
}
```

- `mode` is `"none"` before the first read, and after a failed one.
- `read_ok: false` with `read_error: "<kind>"` is the Case 5 signal a monitor can alert on
  **without** triggering a shutdown to discover it.
- `controller_only` (Case 1) and `read_ok: false` (Case 5) are the two shapes that today
  are indistinguishable. A consumer MUST be able to tell them apart from this object alone
  (FR-004, FR-014).

`last_error` continues to carry the most recent refusal token for humans; `node_selection`
is the machine-readable form.

---

## Unchanged endpoints

`/go`, `/stop`, `/setnextcue`, `/gocue`, `/poweron`, `/brightness` — untouched by this
feature, including when the topology is unreadable: only topology-dependent operations
refuse (FR-018). A controller with no `settings.xml` still runs a show.
