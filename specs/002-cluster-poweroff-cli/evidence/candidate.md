<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# The `xml-refactor-merge-candidate` — what a technician is looking at

**Composed 2026-09-24.** Read this before starting the hardware ledger.

| Repository | Version | Tag at | Carries |
|---|---|---|---|
| `cuems-power-bridge` | **0.3.1-1** | `d5c4226` ✅ tagged | features 001 (node_role parser) and 002 (power-off CLI) |
| `cuems-common` | **1.3.0-23** (UNRELEASED) | `3af31cc` ✅ tagged (relocated from `f2fc0f5`, 2026-09-24) | the `node_role` conversion, the fixed `cuems-cluster-poweroff`, the heredoc removal |
| `cuems-nodeconf` | 0.1.0-8 (UNRELEASED) | `6c0cca7` ✅ tagged | its own two features |
| `cuems-utils` | 0.1.0rc16 (`ce5b5b0`) | — tags later, by decision | the node model, the schemas, `settings.xml` |

## What this candidate changes on a controller

1. `network_map.xml` is read through `cuemsutils`, not a private parser. A document in the
   retired vocabulary is now **refused**, loudly, instead of silently selecting nothing.
2. Orderly power-off selects **adopted** nodes and refuses rather than cutting mains over a
   cluster it could not account for. `force` overrides policy, never evidence.
3. The power-off sequence has one implementation. The wall switch and `POST /shutdown` run the
   same selection and the same stage bodies.
4. One `flock` covers a whole power-off, across processes.
5. Both SSH initiators share `/var/lib/cuems/.ssh/known_hosts` — **this re-learns every node's
   host key once**, under `accept-new`. Capture the old `/root/.ssh/known_hosts` *before*
   upgrading (ledger §13).

## What it deliberately does not change

The wall switch, the power button and `systemctl poweroff` pass no new condition. **A
power-off during a playing show completes** — ledger §11, the single most important check.

## Order of work

`cuems-common`'s `docs/upgrade-verification.md`, then `cuems-nodeconf`'s ledger, then
`specs/002-cluster-poweroff-cli/checklists/hardware-verification.md` here. One pass per
machine; the record sheets are per host.

## T050 — the tags, cross-checked 2026-09-24

```
cuems-power-bridge   d5c4226  ✅ at the reviewed merge
cuems-common         3af31cc  ✅ RELOCATED 2026-09-24, was f2fc0f5
cuems-nodeconf       6c0cca7  ✅ deliberate: its head be45dda is the docs commit
                              that records the tag, written after cutting it
cuems-utils          —        ⏳ tags later, by decision (below)
```

**The relocation, and why it mattered.** `cuems-common`'s tag sat at `f2fc0f5`, three commits
behind its own half — before the `node_role` fix (`df7e354`), the documentation corrections
(`123c93d`) and the heredoc removal (`3af31cc`). Anyone checking out the candidate there got a
tree whose orderly power-off was still broken, which is the precise failure this candidate
exists to fix. Moved with the user's confirmation and force-pushed, because moving a published
tag rewrites what other checkouts see.

**`cuems-utils` tags last, by decision**: it is the library every other repository here pins,
so its candidate tag is cut once all its consumers are ready rather than before them. Until
then this candidate names its version (`0.1.0rc16`) and its commit (`ce5b5b0`) here instead.

## Still open
- Two build-host tasks: the `.deb` bundling gate (T042) and the `cuems-utils` `settings.xml`
  provenance check.
- Every hardware check. **The candidate is not validated until the ledgers are worked.**
