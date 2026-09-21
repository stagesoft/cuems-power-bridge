<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later

*** THIS FILE IS DELIBERATELY UNCOMMITTED. ***
It was written in cuems-common's tree for convenience only; it belongs to
cuems-power-bridge. Move it there and delete it here. Nothing in cuems-common
links to it.
-->

# cuems-power-bridge — the node_role migration findings

**Status**: findings only, no fix attempted
**Measured**: 2026-09-15, from `cuems-common` @ `feat/xml-refactor` (`a826a67`)
**Owner of the fix**: `cuems-power-bridge` (not checked out beside `cuems-common`,
so everything here is measured from the *consumer* side)
**Why it exists**: feature 010 flow 03 recorded this as out of scope with a
report-only decision — see `specs/001-node-role-and-conversion-ordering/spec.md`
§ Out of Scope, OOS-2, and FR-023 which makes this report a deliverable.

---

## 1. The defect, in one paragraph

`cuems-common` ships `/usr/bin/cuems-cluster-poweroff`, the orderly pre-poweroff
sequence that runs as the `ExecStop=` of `cuems-cluster-poweroff.service` on a
controller. Its stage 2 selects which cluster nodes to power off by reading each
node's **role out of `network_map.xml` using the retired vocabulary**:

```
usr/bin/cuems-cluster-poweroff:274-275
    for n in network_map.parse(cfg.network_map_path):
        if n.node_type != "NodeType.slave":
            continue
```

`cuems-common` 1.3.0-2x (feature 007, on `feat/xml-refactor`) ships
`/usr/bin/cuems-migrate-network-map` and runs it from `postinst` over
`/etc/cuems/network_map.xml` and its `.dpkg-new` sibling. After that conversion the
document carries `<node_role>controller|node|firstrun</node_role>` and **no
`<node_type>` element at all**. So on every upgraded controller, the selection above
matches against a vocabulary the file no longer contains.

## 2. Why it is silent, and therefore worse than a crash

Two outcomes are possible depending on what `cuemspowerbridge.network_map.parse`
does with a document whose `<node_type>` is gone. Both are bad, and the first is
much worse:

| If the parser… | Then… | Operator sees |
|---|---|---|
| still reads `<node_type>` and yields `None`/`""` for it | `n.node_type != "NodeType.slave"` is **true for every node**, so every node is skipped. The loop finishes with `targets == []`, stage 2 reports "complete", and the controller powers itself off | **A clean, successful-looking poweroff. The nodes stay on.** |
| has been migrated to expose `node_role` and no longer defines `node_type` | `AttributeError` inside the heredoc; the `timeout … venv_python` pipeline fails | An error in the journal during the poweroff transition |

The first row is the realistic one and it is a **silent, physical** failure: the
venue loses its controller, every node keeps running, and nothing in the log says
"selected zero nodes". Worse, it happens during a poweroff transition — exactly
when nobody is watching a terminal — and the evidence (the journal of a machine
that then powered off) is the least likely thing anyone reads.

**Whatever the fix, add the invariant: a successful stage 2 with an empty target
list is an error, not a success.** That single check would have made this loud.

## 3. Where the boundary actually runs

`cuems-common` owns the *tool*; `cuems-power-bridge` owns everything the tool
reasons with. The tool is a bash script that pipes a Python heredoc into the
bridge's venv interpreter:

```
usr/bin/cuems-cluster-poweroff:47   venv_python=/usr/lib/cuems/bin/python3
usr/bin/cuems-cluster-poweroff:209  from cuemspowerbridge import config, network_map, reachability
usr/bin/cuems-cluster-poweroff:210  from cuemspowerbridge.node_executor import SshTarget, poweroff_all
usr/bin/cuems-cluster-poweroff:130  from cuemspowerbridge import config
usr/bin/cuems-cluster-poweroff:131  from cuemspowerbridge.displays.manager import DisplayManager
```

So four `cuemspowerbridge` modules are load-bearing for a `cuems-common` tool:
`config`, `network_map`, `reachability`, `node_executor`, plus
`displays.manager` in stage 1.

**Everything `cuems-common` ships in this area** (for the fix's blast radius):

| Path | What it is |
|---|---|
| `usr/bin/cuems-cluster-poweroff` | the tool carrying the defect |
| `usr/bin/cuems-displays-on` | stage-1 counterpart; reads `config.load()` only — **no node_role coupling** |
| `etc/systemd/system/cuems-cluster-poweroff.service` | the `ExecStop=` carrier, enabled explicitly in `debian/postinst` |
| `etc/systemd/system/cuems-displays-on.service` | idem |
| `etc/cuems/cluster-poweroff.conf` | conffile; carries the `enabled=` gate and `node_wait_s` |
| `etc/sudoers.d/99-cuems-poweroff`, `99-cuems-admin-poweroff` | the privilege for the poweroff path |
| `debian/control` | `Suggests: cuems-power-bridge` — **deliberately not `Depends:`/`Recommends:`** |

That `Suggests:` matters for the fix. `cuems-common` must stay fully functional on
a host with no bridge at all (every node, and any controller not wanting orderly
power-off): both scripts probe for the venv interpreter and, if absent, log one
ERROR and exit 0. **A fix must not turn the bridge into a hard dependency**, and
must not make the absent-bridge path fail a poweroff.

## 4. The full coupling surface — what the parser has to expose

The selection loop and its self-exclusion logic use these attributes of whatever
`network_map.parse()` yields. A migration of that parser has to keep all of them
working, not only the role field:

| Used at | Attribute | Notes |
|---|---|---|
| `:275` | `n.node_type` | **the defect** — the retired vocabulary, compared against the literal `"NodeType.slave"` |
| `:277,278,286,288,295,298,300` | `n.avahi` | the resolvable name; the error text calls it "role_id/alias/hostname", i.e. it is a *derived* field |
| `:278,283,284` | `n.uuid` | the stable primary key — used for self-exclusion |
| `:295` | `n.role_id` | compared against the OS hostname set |
| `:274` | `cfg.network_map_path` | the bridge's own config decides where the map is |
| `:242` | `cfg.settings_xml_path` | see §5 |

Note the parser is a **parallel implementation of the node-identity model** — the
same model `cuems-utils` owns and `cuems-common`'s `cuems-logs` resolves against
(`role_id → alias → hostname → uuid`). That is the deeper finding: the vocabulary
break is a symptom of the model being reimplemented in a repository that the
six-repo migration never enumerated, so it did not receive feature 007's changes
when the other three consumers did.

`cuems-common`'s own three tools *were* migrated in feature 007
(`cuems-write-chrony-source`, `cuems-log-collector-url`, `cuems-logs` — commit
`df4eb2b`). This one was missed precisely because its role lookup goes through the
bridge rather than through XPath on the document.

## 5. A second, independent fragility found while measuring

`own_uuid()` (`:239-247`) reads this host's uuid from `cfg.settings_xml_path`, i.e.
`/etc/cuems/settings.xml`. Per the 2026-09-15 packaging audit (recorded in
`dev/planning/systemd-service-split-architecture.md` §7.3-①) **no package ships
that file**: `cuems-common` does not install it, and `cuems-utils`'s `.deb` is a
`dh-virtualenv` build that installs only the venv under `/usr/lib/cuems`. It is
operator-hand-placed, from a template in `cuems-utils`'s source tree.

The function swallows every exception and returns `None`, so on a host without
`settings.xml` the uuid-based self-exclusion is silently disabled and the tool falls
back to address/name matching. That is the *good* branch of a bad situation — but it
means the "one identifier that is stable by contract" (its own docstring, `:240`)
is, in deployment terms, optional. Worth deciding deliberately rather than
inheriting.

Also at `:240`: the docstring still says "matches the network_map `NodeType.master`
entry" — stale vocabulary in prose, same family as the defect.

## 6. Reproduction (no cluster required)

1. Take any `network_map.xml` that `cuems-migrate-network-map` has converted — e.g.
   `cuems-common`'s own shipped default, `etc/cuems/network_map.xml`, which already
   reads `<node_role>controller</node_role>`; add a couple of `<node_role>node</node_role>`
   entries.
2. Point `cuemspowerbridge`'s config at it and call
   `network_map.parse(path)`; inspect the yielded objects for `node_type` /
   `node_role`.
3. Run the selection predicate from `:275` over them and count the survivors. On a
   converted map the expected-correct answer is "every node entry"; the defect shows
   as zero (or as an `AttributeError`).

A unit test of exactly this shape belongs in `cuems-power-bridge` as the regression
guard, with one fixture per vocabulary (pre-007 and post-007) so the parser is
pinned against both for as long as unconverted hosts can exist.

## 7. Fix options, with the trade-off each carries

| # | Approach | Pro | Con |
|---|---|---|---|
| A | **Migrate `cuemspowerbridge.network_map` to `node_role`**, exposing `node_role` and dropping `node_type` | Correct model; matches `cuems-utils` and every other consumer | Hard break: an old `cuems-cluster-poweroff` against a new bridge raises `AttributeError`. Needs a versioned dependency edge to be safe (see §8) |
| B | Expose `node_role` **and** keep `node_type` as a deprecated alias for one release | No flag day; either tool version works | Two vocabularies alive in the code that the ecosystem is trying to retire; needs a removal date that someone actually honours |
| C | Adopt `cuems-utils`'s model instead of reimplementing it | Deletes the parallel implementation — the root cause | Largest change; pulls a dependency on `cuemsutils` into the bridge's venv; needs the §4 derived fields (`avahi`) preserved or reimplemented |
| D | Fix only in `cuems-common`'s tool (read the map directly, bypass the parser) | Keeps the fix in one repository | Leaves the bridge's model wrong for every other consumer, and duplicates identity resolution a *fourth* time |

**Recommendation from the consumer side**: **A + the empty-selection invariant from
§2**, with C recorded as the direction of travel. B only if a mixed-version window
is actually unavoidable — and this ecosystem's stated position is that a cluster
upgrades as a unit, which argues it is not.

Whatever is chosen, the fix should also:

- correct the stale docstring at `:240`;
- decide the `settings.xml` question from §5 explicitly;
- add the two fixtures from §6 as regression tests.

## 8. The packaging edge this needs

`cuems-common` `Suggests: cuems-power-bridge` — no version relationship exists in
either direction today. Under option A there is a real ordering requirement (a new
bridge with an old tool, or vice versa, misbehaves), and this ecosystem's position
is that such a requirement belongs in `debian/control` rather than in prose — see
`cuems-common`'s constitution, Principle IV ("the gate is mechanical or it is not a
gate"). Concretely: whichever side changes first should acquire a `Breaks:` against
the versions of the other that cannot work with it. Note `Suggests:` must be
preserved as-is — see §3 for why it is not a `Depends:`.

## 9. Unknowns — resolve these first, they are not answerable from here

- [ ] What does `cuemspowerbridge.network_map.parse()` actually do with a document
      that has `<node_role>` and no `<node_type>`? §2's two rows are the possibility
      space, not a measurement.
- [ ] Does the bridge's model already carry `node_role` (i.e. was it migrated and
      only `cuems-common`'s call site left behind)? That would flip the defect from
      "silent zero" to "AttributeError" and change the urgency, not the fix.
- [ ] Which other consumers does the bridge have? Only `cuems-common`'s two tools
      are visible from here.
- [ ] Does the bridge validate `network_map.xml` against a schema, and if so, which
      copy — its own, or `/etc/cuems/network_map.xsd` (mirrored by `cuems-common`,
      byte-identical to `cuems-utils`'s canonical copy as of 2026-09-15)?
- [ ] Is there a deployed controller where this has already happened? The symptom —
      "nodes still on after the controller powered off" — would have been read as a
      network or SSH problem, not as a parser one.
