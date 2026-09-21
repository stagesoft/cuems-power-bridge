<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# cuems-power-bridge's share of the cuems-utils XML/object-model rebuild (feature 010)

**Status**: not started — planning only, vendored here so this repository does not
need a live sibling checkout to pick the work up
**Source of truth**: the `cuems-utils` sibling checkout (`../cuems-utils`), branch
`feat/xml-refactor`, primarily
`specs/planning/xml-rebuild/010-consumer-prompts/07-cuems-power-bridge.md`
**Vendored**: 2026-09-18, after measuring this repository's working tree and
running its parser
**Companion document**: `cuems-power-bridge-node-role-findings.md`, beside this
file — the dated 2026-09-15 primary record, written from `cuems-common`'s side.
**It is kept, not superseded.** §0.5 below lists four corrections to it; read
those before acting on it.
**Coordinates with**: `cuems-common` — this feature lands in **both**
repositories, merged simultaneously (§7).

**Path convention**: every path here is relative to **this repository's root**, so
`../cuems-utils` and `../cuems-common` are the sibling checkouts beside it. No
absolute path appears on purpose — the layout is a convention, not a machine. If
your checkouts are not siblings, these paths are the only thing to adjust.

This is a working copy of the `cuems-power-bridge` slice of a seven-repository
consumer migration (`cuems-utils`, `cuems-engine`, `cuems-editor`, `cuems-common`,
`cuems-nodeconf`, `cuems-frontend`, `cuems-power-bridge`). The rebuild's shared
decision list, per-repo audits and the other six prompts stay in `cuems-utils`;
this file inlines only what binds this repository, plus enough surrounding context
to orient without the round trip. Where a fact might have moved on since it was
vendored, that is flagged inline — verify before acting.

---

## 0. Why this repository is in the feature at all

### 0.1 The defect, in one paragraph

`src/cuemspowerbridge/network_map.py` parses `/etc/cuems/network_map.xml` with its
own `ElementTree` reader and filters nodes on a string literal:

```python
# :33
node_type: str | None  # "NodeType.master" | "NodeType.slave" | None
# :94
node_type=_text(el, "node_type"),
# :110, inside slave_avahi_names()      # :141, inside slave_ips()
if n.node_type != "NodeType.slave":
    continue
```

`cuems-utils` feature 007 renamed that element to `<node_role>` with a typed
vocabulary (`controller` / `node` / `firstrun`), and `cuems-common` ships
`/usr/bin/cuems-migrate-network-map`, run from its `postinst` over
`/etc/cuems/network_map.xml`. After that conversion the document carries
`<node_role>` and **no `<node_type>` element at all**. `_text()` returns `None`
rather than raising, so `None != "NodeType.slave"` is true for **every** node,
every node is skipped, and both selections come back empty.

### 0.2 Measured, not inferred — 2026-09-18

Run against a `cuems-migrate-network-map`-converted document:

```
slave_avahi_names(converted_map)  ->  ([], [])
slave_ips(converted_map)          ->  []
```

Note the second element of the first result. **The unresolvable list is empty
too**, so not even the `ERROR … has no role_id/alias/hostname` path fires. There
is no log line anywhere that says anything is wrong.

### 0.3 Two features are broken, and they fail independently

**(i) Orderly cluster power-off** — `network_map.py:110` via `bridge.py:391`:

- Step 5 builds the target list → empty. Logs `"shutdown: 0 nodes to power off:
  (none)"` at **INFO**.
- Step 6 SSH-fans-out `poweroff` to nobody.
- Step 7's reachability poll is guarded by `if resolved:` (`bridge.py:431`) — with
  an empty list **the "are they actually down?" check never runs at all**.
- Step 8 arms the Shelly hardware safety timer, which cuts mains power.

A clean, successful-looking poweroff. The nodes stay on, and then lose mains. It
happens during a poweroff transition, when nobody is watching a terminal, and the
only evidence is the journal of a machine that then powered off.

**(ii) The autoload / NNG-hub readiness gate** — `network_map.py:141` via
`bridge.py:589` → `_expected_node_ips` → `_try_auto_load`. **The findings
document misses this entirely**; it is not on the poweroff path:

- `slave_ips()` returns `[]`, so `expected_ips` is empty.
- `_try_auto_load` takes the **`else` branch at `bridge.py:652`** — the one
  commented `SINGLE-CONTROLLER CLUSTER (network_map lists no slave with an <ip>)`
  — logs `"no remote node-engine in network_map (single-controller cluster)"`,
  settles, and loads.
- On a real multi-node cluster the show is loaded **before the node-engines join
  the hub**.

That is exactly the regression `0.3.0-5` was released to fix
(`debian/changelog:150-165`: node players excluded, GO blocked until a manual
engine restart). A converted map silently re-opens it.

**Verify the two recoveries separately.** "The bridge works now" is not an answer
to either.

### 0.4 The suite is red, and the fixtures certify the defect

```
$ uv run --python 3.11 --with pytest --with pytest-asyncio --with pytest-mock \
      --with aiohttp --with websockets --with python-osc python -m pytest -q
6 failed, 133 passed in 12.28s
```

All six are `tests/test_install_mjs.py`, same cause, **unrelated to this
migration**: `TypeError: _patched_code() missing 1 required positional argument:
'force'` — the function gained a `force` parameter and its six test callers were
not updated. **Standing rule: never `/speckit.implement` on a red suite.** Clear
these first, in their own commit.

Runner note: the default interpreter has no `pytest`, and `aiohttp`, `websockets`
and `python-osc` are not installed. The `uv` line above is what works.

And the green 133 are not evidence. `tests/test_network_map_ips.py:13-20` — the
only network-map fixture in the repository — is written in the **retired**
vocabulary, so the suite is green **because** it certifies the defect:

```xml
<node><uuid>u-n1</uuid><node_type>NodeType.slave</node_type>
  <ip>169.254.13.233</ip><role_id>node01</role_id></node>
```

That fixture has a second problem under the fix chosen in §5: it is **not
schema-valid**. `network_map.xsd`'s `NodeType` requires `uuid` (canonical
8-4-4-4-12 hex), `mac`, `name`, `node_role` and `ip`, all `minOccurs="1"`. `u-n1`
is not a uuid and there is no `mac` or `name` in the file. Today that does not
matter, because the parser validates against nothing. After §5 it matters for
every fixture here.

### 0.5 Four corrections to the findings document beside this file

Measured against this repository's source, which that document could not read.

**(a) The defect has three sites, not one.** It names only `cuems-common`'s
`usr/bin/cuems-cluster-poweroff:275`. This repository carries two more, in its own
parser: `network_map.py:110` (`slave_avahi_names`) and `:141` (`slave_ips`).
Fixing the tool alone — its **option D** — leaves both live. Conversely, fixing
only this repository leaves the tool broken, because the tool pipes a Python
heredoc into this repository's venv and calls `network_map.parse()` itself
(`cuems-cluster-poweroff:209-210`). **The two repositories land together.**

**(b) Two independent features are broken, not one.** §0.3 above. The findings
document's blast-radius section (§3) covers only the poweroff path.

**(c) §9's first two unknowns are measured, and they resolve to the worse
branch.** §0.2 above: the silent-zero row, not the `AttributeError` row. The model
was never migrated, so there is no `AttributeError` branch to hope for.

**(d) The packaging edge is not absent — it is unbounded.** §8 says "no version
relationship exists in either direction today". In fact `debian/control:19` here
declares `cuems-common (>= 1.0.0)`, and `cuems-common`'s `debian/control:49`
declares `Suggests: cuems-power-bridge`. So one direction has an **unbounded
floor** and the other has an unversioned suggestion. The fix is to *bound* an
existing edge and add a `Breaks:`, not to create a relation from nothing. (The
same correction applies to `cuems-utils`' own task file, which claimed this
repository declares no `cuems-utils` relation at all; `debian/control:18` declares
`cuems-utils (>= 0.1.0rc5)`.)

Everything else in that document stands, including its §2 recommendation that is
the most durable item in this whole feature: **a successful stage 2 with an empty
target list is an error, not a success.**

### 0.6 One correction to `cuems-utils`' own plan, recorded because it renames this work

Measured 2026-09-18: **this repository is the one feature 010 has been calling
`cuems-wsclient`.** `git merge-base --is-ancestor f78bea6 main` is true here —
`f78bea6` is the exact commit `cuems-utils`' flow 06 audited, 30 commits back; the
rename is in this history (`83d4f5d refactor: rename package cuems-wsclient ->
cuems-power-bridge (v0.2.6)`, 2026-06); and `../cuems-wsclient`, if it exists
beside you, is a **stale checkout** still pointing at
`git@github.com:stagesoft/cuems-wsclient.git`.

Consequences for anyone reading the sibling's documents:

- **US1 and US11 are one story about one file.** Feature 010 spans **seven**
  repositories, not eight.
- `cuems-utils`' `010-consumer-prompts/06-cuems-wsclient.md` is **superseded** by
  `07-cuems-power-bridge.md`. Do not run 06; three of its facts are stale here
  (`tests/` now exists, the import package is `cuemspowerbridge`, and every line
  number has moved).
- **Never run a census, count or verification against `../cuems-wsclient`.** It is
  30 commits behind, frozen at 2026-06-01, and measuring it both double-counts and
  reports on a tree nobody ships from.

The durable lesson is the discovery method, not the corrected number: this
repository was missed twice and then double-counted, all three times because the
list was maintained by hand. An ecosystem-wide count must discover its own
denominator and de-duplicate **by git remote, not by directory name**.

### 0.7 Every site carrying the retired vocabulary, counted 2026-09-18

| Path | What |
|---|---|
| `src/cuemspowerbridge/network_map.py:110` | `if n.node_type != "NodeType.slave"` — **defect site 1**, `slave_avahi_names` |
| `src/cuemspowerbridge/network_map.py:141` | `if n.node_type != "NodeType.slave"` — **defect site 2**, `slave_ips` |
| `src/cuemspowerbridge/network_map.py:94` | `node_type=_text(el, "node_type")` — the read that yields `None` |
| `src/cuemspowerbridge/network_map.py:33` | the dataclass field and its `"NodeType.master" \| "NodeType.slave"` comment |
| `src/cuemspowerbridge/network_map.py:58-59,102,120` | four docstrings in the retired vocabulary |
| `tests/test_network_map_ips.py:13-20,40` | the fixtures of §0.4 |
| `debian/control:34` | **shipped prose**: "SSHes every NodeType.slave from /etc/cuems/network_map.xml" |
| `README.md:320,323,476,767,1265` | five documentation sites |
| `../cuems-common/usr/bin/cuems-cluster-poweroff:275` | **defect site 3** — re-measured 2026-09-18, still at `:275` |
| `../cuems-common/usr/bin/cuems-cluster-poweroff:240` | docstring: "matches the network_map `NodeType.master` entry" |

**Deliberately excluded**, so it is not "fixed" by accident:
`src/cuemspowerbridge/wsclient.py:69-70` defaults `--host` to `master.local`. That
is an **mDNS hostname**, not the node-role field — renaming it changes what a
deployed host answers to. Out of scope, recorded so the next counter does not
silently include it.

---

## 1. Measured starting state — 2026-09-18

| | |
|---|---|
| Current branch | **`feat/xml-refactor`** already exists, at `c201405` (= `main`), clean but for the untracked `specs/` |
| Base | `main` @ `c201405` — `release: cuems-power-bridge 0.3.0-6 (single-controller auto-load settle)` |
| Spec-kit | **absent** — added on first run (§2) |
| Constitution | **absent** — written on first run (§3) |
| Existing features | none → this becomes **`001-node-role-parser`** |
| Tests | `pytest`, 15 files. **RED**: `6 failed, 133 passed` (§0.4) |
| `cuemsutils` | `pyproject.toml:36` `>=0.1.0rc5`, **optional**, `:40` `production = ["cuemsutils"]` — and **imported nowhere** (grep across `src/` and `tests/`) |
| Debian | `debian/control:18` `cuems-utils (>= 0.1.0rc5)`, `:19` `cuems-common (>= 1.0.0)` — both unbounded floors |
| Shipped | `cuems-power-bridge_0.3.0-5_all.deb`; the systemd unit is shipped by `cuems-common` |
| Schema validation | **none anywhere** — `parse()` uses bare `ElementTree` with namespace-agnostic local-name matching and validates against nothing |

---

## 2. Branch and bootstrap

```bash
git checkout feat/xml-refactor        # ALREADY EXISTS at c201405 — do not re-create
git status                            # specs/ is untracked — commit it FIRST (§2a)

specify init --here --integration claude --script sh --force
```

Commit the scaffold as its own commit before `/speckit.constitution`.

Spec-kit's sequential branch numbering will want its own branch. Stay on
`feat/xml-refactor`; let it name `specs/001-node-role-parser/` only.

### 2a. Commit the planning documents first

This file and `cuems-power-bridge-node-role-findings.md` are **untracked**. They
are currently the only written record of a live, silent, physical failure, and the
findings document has already survived two moves by luck (`cuems-common`'s tree →
`dev/planning/` → `specs/planning/`). Commit them before anything else. An
untracked file is not a deliverable.

---

## 3. Constitution — write one, this repository has none

```
/speckit.constitution

Establish the constitution for cuems-power-bridge, grounded in what this repository actually
is. Read CLAUDE.md, README.md, pyproject.toml, src/cuemspowerbridge/bridge.py,
src/cuemspowerbridge/shelly.py and src/cuemspowerbridge/cluster_bus.py before writing
anything.

WHAT THIS REPOSITORY IS: the CUEMS power bridge — a controller-only asyncio HTTP coordinator
on :8478 fronting orderly cluster shutdown and GO/STOP, triggered by a wired Shelly Pro 1
flip-switch or a Bitfocus Companion Stream Deck. Python 3.11+, Poetry, PyPI name
cuemspowerbridge, dh-virtualenv into the shared /usr/lib/cuems venv, Debian package
cuems-power-bridge (its systemd unit is shipped by cuems-common). It holds persistent
WebSockets to the engine (:9190, binary OSC) and the editor (:9092, JSON). Two of its
operations read the cluster topology from /etc/cuems/network_map.xml: the ordered shutdown
sequence (resolve nodes to avahi hostnames, SSH-fanout poweroff, poll reachability until they
are down, arm the Shelly hardware timer, then poweroff locally) and the boot auto-load gate
(wait for the expected node-engines on the controller's NNG hub before firing project_ready).
It was renamed from cuems-wsclient in 2026-06; the console entry cuems-wsclient survives
deliberately.

PRINCIPLES THE CODE ALREADY IMPLIES — derive from these, do not invent unrelated ones:
- IT CUTS MAINS POWER TO OTHER MACHINES. That is the top of the hierarchy and everything
  else is subordinate to it. A step that silently finds nothing to do must not be
  indistinguishable from a step that succeeded — an empty target list before a poweroff is
  an anomaly to surface, not a fast path. Write this as a principle in those terms, because
  the defect this feature fixes is exactly that distinction being absent, twice.
- IT ORCHESTRATES AN ORDERED, PARTIALLY-IRREVERSIBLE SEQUENCE. Steps have preconditions;
  skipping a verification step because its input was empty is a correctness bug, not an
  optimisation. State that verification steps run on the anomalous path too — bridge.py:431's
  `if resolved:` is the live counter-example.
- IT READS A SCHEMA IT DOES NOT OWN. network_map.xml belongs to cuemsutils, which versions it
  deliberately, ships a validating reader for it, and ships a conversion tool for documents
  written before a rename. A private parser for someone else's schema is a liability with a
  known failure mode — this repository is the proof, and it is the FOURTH copy of the same
  node-identity model. Depend on the owning library rather than re-deriving its format.
- ITS DEGRADED PATHS ARE DESIGNED, AND THEIR TRIGGERS MUST BE REAL. cluster_bus's
  single-controller branch, slave_ips's missing-<ip> skip and the node-timeout
  degraded-proceed are all deliberate, field-measured behaviours. Each is correct only when
  its trigger is genuine. A parse that silently yields nothing counterfeits every one of
  them at once. State that a degraded path entered because of a READ FAILURE is a defect,
  not a degradation.
- IT TALKS TO HARDWARE OVER THE NETWORK (Shelly RPC, SSH, ICMP/TCP reachability, PJLink and
  Epson ESC/VP21 projectors). Timeouts, partial failures and unreachable hosts are the
  normal case, not the exceptional one.
- IT SHARES A VIRTUALENV WITH EVERY OTHER CUEMS PYTHON COMPONENT. /usr/lib/cuems is shared;
  a .deb that bundles what another package ships breaks dpkg -i, and stripping a bundle can
  break a SIBLING component at its next restart (CLAUDE.md records exactly this happening
  with pythonosc and the engine). Packaging changes are a correctness concern here, not a
  release chore.

Do NOT weaken any rule to accommodate the migration that follows.
```

---

## 4. Context block — paste verbatim into `/speckit.specify` and `/speckit.plan`

```
CONTEXT — read these before writing anything.
  IN THIS REPOSITORY:
    specs/planning/cuems-utils-xml-refactor-consumer-migration.md  THIS FILE — §0.5 carries
        four corrections to the findings document; read them before acting on it
    specs/planning/cuems-power-bridge-node-role-findings.md        the 2026-09-15 findings
  SIBLING ../cuems-utils (branch feat/xml-refactor):
    specs/007-node-model-migration/migration-guide.md   the rename, the release gate, and
        §3's FR-030a-ii class
    specs/008-rebuild-extension/migration-guide.md      the strict load path, the conversion
        registry, the report types
    specs/planning/xml-rebuild/010-consumer-prompts/07-cuems-power-bridge.md  this flow
    specs/planning/xml-rebuild/xml-rebuild-07-speckit-prompts.md  §2 = the FULL decision list
    src/cuemsutils/xml/schemas/network_map.xsd   the schema this repository parses by hand
    src/cuemsutils/tools/NodeList.py             NodeRole, NodeIndex, node
    src/cuemsutils/tools/ConfigManager.py        network_map, node_network_map
  OTHER CONSUMER — this feature lands in BOTH repositories, together:
    ../cuems-common/usr/bin/cuems-cluster-poweroff  :275 and :240

SETTLED — the decisions that bind THIS repository. Do not reopen.
  D2  the schema is the single source of truth for structure/type/cardinality/order
  D11 the node model lives in cuemsutils ONLY. No consumer re-implements or re-tests it.
      THIS REPOSITORY IS THE FOURTH COPY. Deleting it is the point of the feature.
  D12 public surface returns objects, never raw dicts
  D15 the public objects are CuemsScript (show) and ConfigManager/ConfigBase (config)
  D19/D21 reading is strict: a document either loads, converts in memory, loads repaired,
      or raises — it never silently yields a partial answer
  D27 nothing in the ecosystem releases until every 010 flow lands
  D32 THIS REPOSITORY IS IN FEATURE 010'S SCOPE IN FULL, and it is the repository the plan
      called "cuems-wsclient" — one repository, renamed, not two (§0.6). Its private
      ElementTree network-map reader is REPLACED by the library's public path, not
      re-spelled to node_role.
  D33 a half-renamed vocabulary state is not shippable — here that means this repository and
      cuems-common's cuems-cluster-poweroff land TOGETHER
  Q14 -> (i) cuemsutils.xml is internal machinery; use ConfigManager, not xml/

MEASURED STARTING STATE — verified against live files 2026-09-18, not transcribed:
  src/cuemspowerbridge/network_map.py — a private, schema-less, namespace-agnostic parser:
    :23  NS = "{https://stagelab.coop/cuems/}"  (accepts namespaced and bare elements)
    :27-34  @dataclass(frozen=True) Node — uuid, avahi, role_id, alias, hostname, node_type, ip
    :37-44  _text(parent, *names) — first matching child by LOCAL NAME; returns None, never raises
    :47-52  _resolve_avahi — role_id -> alias -> hostname -> None. NEVER <ip>.
    :55-99  parse() — scans the whole tree for any element whose local-name is "node",
            requires only <uuid>, VALIDATES AGAINST NO SCHEMA
    :101-117 slave_avahi_names()  :110  if n.node_type != "NodeType.slave": continue  <- DEFECT 1
    :119-151 slave_ips()          :141  if n.node_type != "NodeType.slave": continue  <- DEFECT 2
  src/cuemspowerbridge/bridge.py:
    :386+ _run_shutdown — step 5 (:391) targets from slave_avahi_names; step 6 (:403)
             poweroff_all; step 7 (:430-431) `if resolved:` reachability poll — SKIPPED ENTIRELY
             when empty; step 8 (:458) arms the Shelly safety timer
    :575-593 _slave_ips_cached — mtime-keyed cache over slave_ips
    :594-616 _expected_node_ips -> :618 _try_auto_load; :652 the SINGLE-CONTROLLER branch
             that an empty result silently counterfeits
  MEASURED against a cuems-migrate-network-map-converted document:
    slave_avahi_names(converted) -> ([], [])   <- the UNRESOLVABLE list is empty too, so not
                                                  even the ERROR log path fires
    slave_ips(converted)         -> []
  tests/test_network_map_ips.py:13-20,40 — the only fixtures, in the RETIRED vocabulary, and
    NOT schema-valid (no <mac>, no <name>, and "u-n1" is not a uuid)
  SUITE IS RED BEFORE THIS FEATURE: 6 failed, 133 passed — tests/test_install_mjs.py,
    TypeError: _patched_code() missing 1 required positional argument: 'force'. Unrelated to
    this migration. FIX FIRST, in its own commit.
  pyproject.toml:36  cuemsutils = {version = ">=0.1.0rc5", optional = true}  AND IMPORTED NOWHERE
  pyproject.toml:40  production = ["cuemsutils"]
  debian/control:18  cuems-utils (>= 0.1.0rc5)     <- EXISTS, unbounded floor
  debian/control:19  cuems-common (>= 1.0.0)       <- EXISTS, unbounded floor
  debian/control:34  shipped description prose: "SSHes every NodeType.slave from ..."

WHAT THE LIBRARY ACTUALLY GIVES YOU — measured 2026-09-18 by running it, so the plan does
not discover these during implement:
  ConfigManager(config_dir=..., load_all=False).load_network_map() then .network_map
    -> CuemsNetworkMapType. Its ["node_list"] is a list of {"node": <node>} WRAPPERS
       (kept deliberately — cuems-engine reads that shape).
    -> per node, TYPED: uuid=Uuid, mac=str, name=str, node_role=NodeRole (an ENUM, so
       `n["node_role"] is NodeRole.node` is the filter), ip=str, adopted=bool, online=bool,
       role_id/alias/hostname=str where present.
       network_map is the ONE config schema that runs the adapter table (007, research R1).
  AN UNCONVERTED MAP RAISES, NAMED AND ACTIONABLE — the loud failure the findings document
  asks for, already built:
       SchemaError: <path>: node <uuid> still carries the retired <node_type> element
       (value 'NodeType.master') — network_map.xsd now requires <node_role>, one of
       ['controller', 'node', 'firstrun']. Run the network_map conversion
       (cuems-migrate-network-map) and re...
  THREE PRECONDITIONS THE PUBLIC PATH ADDS, ALL MEASURED — the plan MUST cover each:
    1. /etc/cuems/settings.xml MUST EXIST and be schema-valid. ConfigManager.__init__ calls
       ConfigBase.load_base_settings UNCONDITIONALLY — even with load_all=False — and raises
       FileNotFoundError without it. NO PACKAGE SHIPS THAT FILE (findings §5). Today this
       repository needs no such file. This turns findings §5 from "a secondary fragility to
       decide deliberately" into a HARD PRECONDITION of the fix.
    2. THIS host's uuid (settings.xml's node_uuid) MUST have an entry in the map, or
       load_network_map() raises ValueError: Node with uuid <uuid> not found — it resolves
       node_network_map eagerly.
    3. EVERY node entry must be schema-valid: uuid (canonical 8-4-4-4-12), mac, name,
       node_role, ip are all minOccurs="1". Today parse() requires only <uuid>. Every fixture
       here must be rewritten, and an operator-hand-maintained /etc/cuems/network_map.xml
       missing a <mac> or <name> will now RAISE where it used to yield a partial node.

DELIBERATE, AND NOT TO BE "FIXED": the avahi resolution ignores <ip> ON PURPOSE — it is a
stale link-local on many adopted nodes (network_map.py's module docstring, and
feedback_avahi_hostnames). Resolution runs role_id.local -> alias.local -> hostname.local,
and nodes resolving to none of the three are REPORTED as unresolvable, never dropped.
slave_ips() is the deliberate exception: it TRUSTS <ip> because the NNG-hub readiness gate
matches bus peers by IP, not hostname. Both policies are field-learned. Preserve them
EXACTLY; a migration that "simplifies" either is a regression.

CALLERS THAT KEEP RESOLVING BUT BECOME WRONG (007 FR-030a-ii): this repository is the
ecosystem's clearest instance, and the instance that proves the class needs SEARCHING for.
Nothing failed, nothing crashed, and the suite stayed green BECAUSE its fixtures were written
in the retired vocabulary. Fixing the comparison is NOT the deliverable. Proving the fix with
a test that fails against the old value is, and so is removing the private parser that made a
rename in another repository able to do this silently.
```

---

## 5. Specify

```
/speckit.specify <PASTE CONTEXT BLOCK>

Move cuems-power-bridge off its private network-map parser and onto cuemsutils' public
configuration path, restore BOTH broken features, and make an empty node selection impossible
to mistake for success.

WHAT MUST BE TRUE WHEN DONE:

- THE PRIVATE PARSER IS GONE, NOT CORRECTED. src/cuemspowerbridge/network_map.py reimplements
  a reader for a schema cuemsutils owns, ships, versions and validates. That duplication is
  what let a rename in another repository silently disable two features of this one. Read the
  map through ConfigManager's network_map object; keep this module only as a THIN ADAPTER
  that turns the library's node objects into whatever shape bridge.py wants, preserving the
  two resolution policies verbatim. This is D32 and D11.

- THE ROLE FILTER IS AN ENUM COMPARISON, NOT A STRING ONE. What was
  `n.node_type != "NodeType.slave"` becomes a comparison against NodeRole.node. Both sites
  (slave_avahi_names and slave_ips) change; there is no third site in this repository. Do NOT
  define a local NodeRole (007 FR-030a-i) — import it from cuemsutils.tools.NodeList.

- THE THREE PRECONDITIONS FROM THE CONTEXT BLOCK ARE HANDLED EXPLICITLY, NOT DISCOVERED.
  (1) settings.xml is now REQUIRED for the bridge to read the map at all. Decide and
  implement what happens on a host without it — and note that the honest answer probably
  involves cuems-common or cuems-nodeconf shipping or generating it, which makes it a
  cross-repository decision, not a try/except. Do NOT reproduce findings §5's
  swallow-everything-and-return-None: that is the pattern under repair.
  (2) A map with no entry for this host's own uuid raises. Decide whether that is fatal for
  the bridge or a condition it reports and continues past.
  (3) Fixtures and any hand-maintained /etc/cuems/network_map.xml must be schema-valid.

- AN EMPTY SELECTION IS AN ERROR, NOT A SUCCESS — in BOTH features, separately. This is the
  invariant the findings document asks for in §2, and the only deliverable here that survives
  the vocabulary question entirely: a future field rename breaks the selection the same
  silent way, and this check is what makes it loud.
  * Shutdown: a stage that resolves zero targets must not proceed silently to arming the
    Shelly. Decide the behaviour — abort, require an explicit force, or proceed with a loud
    ERROR and a distinguishable /status state — and implement it. Note this is NOT the same
    as a genuine single-node cluster, which must still work; distinguish "the map contains no
    other node" from "the read produced nothing".
  * Autoload: bridge.py:652's single-controller branch must be entered because the map
    genuinely lists no other node with an <ip>, never because a read failed. Those two are
    indistinguishable today.

- THE RESOLUTION POLICIES SURVIVE UNCHANGED, COVERED BY TESTS. avahi: ignore <ip>, resolve
  role_id -> alias -> hostname, report the unresolvable rather than dropping them. Bus
  readiness: trust <ip>, skip-with-WARNING when absent, degrade gracefully when stale.

- THE cuemsutils DEPENDENCY BECOMES REAL AND BOUNDED. It is optional, >=0.1.0rc5 and imported
  nowhere today; after this feature it is a genuine runtime dependency. Move it out of
  [tool.poetry.extras], bound it in pyproject.toml the way cuems-nodeconf bounds its
  (>=0.1.0rc16,<0.1.1), and raise debian/control:18's floor to match. A floor alone cannot
  express the release gate.

- THE FIXTURES DISCRIMINATE. tests/test_network_map_ips.py's fixtures are written in the
  retired vocabulary, so today's green suite CERTIFIES the defect. Two fixtures must exist —
  pre-007 (<node_type>NodeType.slave</node_type>) and post-007 (<node_role>node</node_role>)
  — both schema-valid, and the post-007 one MUST FAIL against the pre-migration parser.
  Record that failing run. A passing suite is not evidence in this class of defect.

- BOTH FEATURES ARE VERIFIED SEPARATELY. (i) orderly power-off selects the expected nodes
  from a converted map; (ii) the autoload / NNG-hub readiness gate does too. They fail and
  recover independently, so "the bridge works now" is not an answer to either.

- ../cuems-common/usr/bin/cuems-cluster-poweroff:275 LANDS IN THE SAME CUTOVER. It pipes a
  Python heredoc into this repository's venv and calls network_map.parse() itself (:209-210),
  so the two repositories are one change. Its :240 docstring ("matches the network_map
  NodeType.master entry") is corrected in the same pass. Coordinate the merges; do not land
  one half.

- ZERO OCCURRENCES OF THE RETIRED VOCABULARY REMAIN IN SHIPPED CODE OR SHIPPED PROSE,
  COUNTED — including debian/control:34's package description and the five README sites.
  EXEMPT, and recorded as exempt with its reason: any fixture that exists to prove the
  pre-007 document still fails. Do NOT include wsclient.py:69-70's `master.local` default in
  the count — that is an mDNS hostname, not the role field.

DO NOT re-implement or re-test the node model here (007 FR-030a-i).

RECORD, DO NOT SCHEDULE: this repository's parser is the FOURTH copy of the node-identity
model (role_id -> alias -> hostname -> uuid), after cuemsutils, cuems-common's cuems-logs and
cuems-nodeconf's. Deleting this copy is this feature's work; WHY a fourth copy existed —
this repository was absent from the migration's list for two features under one name, then
re-discovered under another — belongs in the 010 migration guide.
```

---

## 6. Clarify

```
/speckit.clarify
```

Force three questions, in this order. Everything else is mechanical.

1. **What should a shutdown do when it resolves zero nodes?** Abort, force-flag,
   or proceed loudly. A product decision about a machine that cuts mains power,
   and the item that outlives the vocabulary question.
2. **What happens on a host with no `/etc/cuems/settings.xml`?** The public path
   requires it and no package ships it. The answer may be "another repository
   ships it", which makes this a cross-repo dependency to raise now rather than at
   implement time.
3. **Does the bridge start honouring `adopted`?** `network_map.py`'s module
   docstring says "Resolve every **adopted** node", and `parse()` has never
   filtered on it — because the private parser had no typed `adopted` to filter
   on. The library gives one. Adding the filter is a **behaviour change** to a
   poweroff target list, so it is a decision, not a tidy-up.

---

## 7. Plan

```
/speckit.plan <PASTE CONTEXT BLOCK>

Per-file scope:
- src/cuemspowerbridge/network_map.py — the private parser deleted; what remains is a thin
  adapter over ConfigManager's network_map object, preserving _resolve_avahi's policy and
  slave_ips's deliberate <ip> trust. The two "NodeType.slave" comparisons (:110, :141) become
  NodeRole.node comparisons.
- src/cuemspowerbridge/bridge.py — :391 the shutdown target build, :431 the reachability poll
  no longer skipped on the anomalous path, :575-615 the cache and _expected_node_ips over the
  new adapter, :652 the single-controller branch distinguished from a failed read.
- tests/test_network_map_ips.py — both fixtures, schema-valid, with the discriminating run.
- tests/test_autoload.py — the readiness-gate half of the recovery, verified separately.
- tests/test_install_mjs.py — the six pre-existing failures, FIRST and in their own commit.
- pyproject.toml:36,40 and debian/control:18 — a real, non-optional, bounded dependency.
- debian/control:34 and README.md:320,323,476,767,1265 — the shipped prose.
- SIBLING ../cuems-common/usr/bin/cuems-cluster-poweroff:275,240 — the third defect site and
  its stale docstring, merged simultaneously with this repository.

Packaging edge — DECIDE AND RECORD (findings §8, as corrected by §0.5(d) of the bundle):
  cuems-common `Suggests: cuems-power-bridge` (its debian/control:49) carries no version, and
  this repository `Depends: cuems-common (>= 1.0.0)` (debian/control:19) — an unbounded
  floor. So nothing today refuses a new tool beside an old bridge or the reverse. Whichever
  side changes first acquires a `Breaks:` against the versions of the other that cannot work
  with it. `Suggests:` MUST BE PRESERVED AS-IS — it is deliberately not Depends:/Recommends:
  so cuems-common stays functional on a host with no bridge, and both its scripts already log
  one ERROR and exit 0 when the venv interpreter is absent. A fix must not turn the bridge
  into a hard dependency of cuems-common, and must not make the absent-bridge path fail a
  poweroff.

Shared-venv check, because this repository has broken a sibling this way before:
  making cuemsutils a real dependency must not change what the .deb BUNDLES into
  /usr/lib/cuems. cuems-utils already ships cuemsutils into that venv; bundling a second copy
  is a dpkg -i file-overwrite conflict, and stripping one another package needs has broken
  the engine's OSC at its next restart (CLAUDE.md, Field notes). Verify the built .deb's
  contents before shipping.

Sequencing: independent of every other 010 flow — it needs nothing from them and nothing
needs it, so it starts immediately and can land first, EXCEPT that its cuems-common half must
land simultaneously. It still does not RELEASE first (D27).

Constitution check, against the constitution written in §3:
- The cuts-mains-power principle is what makes the empty-selection invariant the centre of
  this feature rather than a footnote.
- The reads-a-schema-it-does-not-own principle is what makes deleting the parser the fix
  rather than correcting two string comparisons.
- The designed-degraded-paths principle is what makes bridge.py:652 in scope at all.
- Testing: the discriminating fixtures are the gate, and the six pre-existing failures are
  cleared before any of this starts.
```

---

## 8. Tasks, checklist, analyze, implement

```
/speckit.tasks
```
```
/speckit.checklist Parser-removal readiness: the six pre-existing test_install_mjs failures
cleared first, in their own commit; the private parser DELETED or reduced to a thin adapter,
not re-spelled to node_role; both role filters (network_map.py:110 and :141) on NodeRole with
a post-007 fixture that FAILS against the pre-migration parser, recorded; both fixtures
schema-valid; the three public-path preconditions (settings.xml, the self-uuid entry, node
validity) each decided and implemented rather than discovered; the empty-selection invariant
implemented in BOTH features, with the reachability poll no longer skipped on the anomalous
path and bridge.py:652's single-controller branch distinguished from a failed read; both
resolution policies (avahi ignores <ip>; slave_ips trusts it) preserved verbatim and covered;
both broken features verified SEPARATELY; cuems-common's cuems-cluster-poweroff:275 and :240
landing in the same cutover with the Suggests: relationship preserved and a Breaks: added;
the cuemsutils dependency non-optional and upper-bounded in pyproject.toml AND debian/control;
the built .deb bundling nothing another CUEMS package ships; and zero retired-vocabulary
occurrences in shipped code and shipped prose, counted, with the exempt set enumerated and
wsclient.py:69-70 excluded by reason.
```
```
/speckit.analyze
```
```
/speckit.implement
```

Then the shared quality loop: `/speckit.check-integration` and `/speckit.optimize`
after `/speckit.tasks` and before `/speckit.implement`; `/speckit.verify` after.
`check-integration` earns its place here more than anywhere — this is a migration,
and the failure mode is writing the new call alongside the old one instead of
replacing it.

---

## 9. Exit criteria

The suite green — including the six failures that were red before this feature
started, and a post-007 fixture whose failing run against the pre-migration parser
was recorded. The private network-map parser gone. Both role filters on
`NodeRole`. The three public-path preconditions decided and implemented. The
empty-selection invariant live in both features, each verified separately against
a converted map. Both resolution policies preserved and covered. `cuems-common`'s
`cuems-cluster-poweroff` merged simultaneously, with its `Suggests:` preserved and
a `Breaks:` added. The `cuemsutils` dependency real, non-optional and
upper-bounded in both `pyproject.toml` and `debian/control`. The built `.deb`
verified not to bundle anything another CUEMS package ships. Zero
retired-vocabulary occurrences in shipped code and shipped prose, counted, with
the exempt set enumerated.

Then, on real hardware — `cuems-utils`' T046 is the only task in the feature with
a cluster: orderly power-off selects the expected nodes from a converted map, and
the autoload gate waits for them.

**Does not ship alone** (D27). Nothing in feature 010 is worth fixing earlier:
every other consumer's failure is loud; this one's cuts mains power and files a
success.

---

## 10. Standing rules that bind this flow

From the rebuild's shared rule set, the ones that matter in a consumer repository:

- **Never `/speckit.implement` on a red suite.** Live here: §0.4.
- **Commits are GPG-signed.** Retry on "gpg failed to sign"; never `--no-gpg-sign`.
- **Planning artifacts stay in `specs/planning/`, feature artifacts in
  `specs/NNN-*/`.** This file and the findings document are planning; everything
  `/speckit.specify` produces is feature.
- **Callers that keep resolving but become semantically wrong** (007 FR-030a-ii)
  are a distinct and more dangerous class than callers that stop resolving.
  Nothing fails, the suite stays green, and the answer is silently wrong. They are
  **searched for**, and each one gets a test that fails against the old value.
  This repository is what that class looks like when nobody searches — twice, and
  under two names.
- **The node model lives in `cuemsutils` exclusively** (007 FR-030a-i). A
  node-model test appearing here during this migration is a regression, not
  coverage.

---

## 11. Two secondary findings, to be decided rather than inherited

From the findings document's §5, restated here because a decision left in a
planning document is a decision nobody made:

1. **`own_uuid()` reads `/etc/cuems/settings.xml`, which no package ships.** In
   `cuems-common`'s tool (`cuems-cluster-poweroff:238-247`) it swallows every
   exception and returns `None`, so on a host without that file the uuid-based
   self-exclusion is **silently disabled** and the tool falls back to
   address/name matching. That is the good branch of a bad situation — but it
   means "the one identifier that is stable by contract" (its own docstring,
   `:240`) is optional in deployment terms. **This is now a precondition, not a
   fragility**: §4's precondition 1 shows the same file is mandatory for the
   library's public path. Decide it deliberately.
2. **`cuems-cluster-poweroff:240`'s docstring** still says "matches the
   network_map `NodeType.master` entry" — stale prose in the same family as the
   defect, corrected in the same pass (§7).

---

## 12. Where to look for more

- `../cuems-utils/specs/planning/xml-rebuild/010-consumer-prompts/README.md` — the
  seven flows, their run order, and the repository-identity correction.
- `../cuems-utils/specs/planning/xml-rebuild/xml-rebuild-07-speckit-prompts.md` §2
  — **authoritative** for the decision list. The subset in §4 is derived from it;
  if a question arises that the subset does not answer, read §2 rather than
  inventing an answer locally.
- `../cuems-utils/specs/010-consumer-migration/tasks.md` — US11 is this
  repository's gate list on the library's side (T070–T079), and the identity
  correction at the top of that file explains why US1's T018/T019 point here too.
- `../cuems-common/dev/planning/cuems-utils-xml-refactor-consumer-migration.md` —
  the sibling bundle for the other half of this cutover.
