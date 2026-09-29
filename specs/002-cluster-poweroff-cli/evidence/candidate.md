<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# The `xml-refactor-merge-candidate` — what a technician is looking at

**Composed 2026-09-24.** Read this before starting the hardware ledger.

> **⚠ Superseded 2026-09-28 — do not start the hardware ledger from the table below.**
> Three of the four rows moved. The current state is in
> "[Re-measured 2026-09-28](#re-measured-2026-09-28--two-tags-are-behind)" at the foot of this
> sheet, and **two tags are behind their own branches**, one of them in a way that stops a
> controller booting. The table below is kept as the 2026-09-24 record, not as instructions.

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

## Re-measured 2026-09-28 — two tags are behind

T050's cross-check is a standing obligation, not a one-off: *a tag not pointing at the reviewed
head is a finding, not a formality.* Re-run four days later, it finds three:

```
cuems-power-bridge   tag d5c4226   head 13a9af4   ❌ 6 commits behind, 2 of them packaged
cuems-common         tag 3af31cc   head e3c9430   ❌ 3 commits behind, ALL packaged
cuems-nodeconf       tag b305c1c   head b305c1c   ✅ RE-CUT 2026-09-28, was 6c0cca7
cuems-utils          —             head 1a4e608   ⏳ tags later, by decision (D27)
```

**`cuems-nodeconf` re-cut for its new feature `003-startup-readiness`** — announced to this flow.
Its candidate now also renders `/etc/avahi/services/cuems.service` from `/etc/cuems/settings.xml`
at every start, refuses to start an unprovisioned node, and answers *"nodeconf is still starting
up"* to an adopt that arrives before its map is loaded.

**`cuems-common` is the one that blocks a technician.** `cuems-nodeconf`'s renderer exits rather
than announce a template with no sentinel placeholder. `cuems-common` ships the sentinel in
`cuems.service.controller` only from `f6750d7`; its still-tagged `3af31cc` carries the production
uuid. So the two candidate tags **do not compose on a controller** — `cuems-nodeconf` will not
start there. Both working trees are correct; only the tag is behind. A re-cut to `e3c9430` is due.

**This repository's own tag is behind for a different reason, and it is not cosmetic.** `d5c4226`
predates `ca67a99`, which makes `debian/rules` resolve `cuemsutils` from the sibling checkout
instead of PyPI. `cuemsutils 0.1.0rc16` **is not on PyPI** (the newest published is `0.1.0rc14`),
and `dh_virtualenv` resolves with pip against the index — so a `.deb` built from `d5c4226` fails at
dependency resolution on every host, including the controller. `13a9af4` then strips foreign
console scripts and moves `pyproject.toml` `0.3.0` → `0.3.1` to agree with the changelog the tag
already carried. **A candidate that cannot be built is not a candidate.** The version does not
move: `0.3.1-1` either way. Suite re-run at `13a9af4`: **276 passed**, the same figure the tag
message records — those two commits touch the build, not the code.

## Still open
- **Two tag re-cuts**, above: `cuems-common` `3af31cc` → `e3c9430`, and this repository
  `d5c4226` → `13a9af4`. Both are force-pushes of published tags — confirm before pushing, and
  record old and new in the message, as the 2026-09-24 relocation did.
- Two build-host tasks: the `.deb` bundling gate (T042) and the `cuems-utils` `settings.xml`
  provenance check.
- Every hardware check. **The candidate is not validated until the ledgers are worked.**
