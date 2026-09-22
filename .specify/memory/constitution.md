<!--
SYNC IMPACT REPORT
Version change: (none) → 1.0.0
Rationale: initial ratification. No prior constitution existed in this repository;
the scaffold at .specify/memory/constitution.md was the unfilled core template
(spec-kit 1.0.4, bootstrapped in a0acbcc).

Modified principles: none (initial adoption)

Added sections:
  - Core Principles I–VII
  - Operating Constraints
  - Direction of Travel — from power bridge to `cuems-bridge`
  - Testing Gate and Development Workflow
  - Governance

Principles derived from: CLAUDE.md; README.md §§ Overview, Architecture, Design
Goals; pyproject.toml; src/cuemspowerbridge/{bridge,shelly,cluster_bus,config,
network_map,displays/base}.py; debian/{control,rules,cuems-power-bridge.service};
and the two planning documents in specs/planning/ — all measured against the live
tree at feat/xml-refactor (1574608), not transcribed.

Follow-up TODOs: none. RATIFICATION_DATE is the date of this adoption; no earlier
governance document exists to date from.
-->

# cuems-power-bridge Constitution

`cuems-power-bridge` is the CUEMS cluster's **controller-only power and showcontrol
bridge**: an asyncio HTTP coordinator on `:8478` that fronts orderly cluster
shutdown and GO/STOP, triggered by a wired Shelly Pro 1 flip-switch or a Bitfocus
Companion Stream Deck. It holds persistent WebSockets to the engine (`:9190`,
binary OSC — the status cache and GO/STOP) and to the editor (`:9092`, JSON —
`project_ready` boot auto-load), drives projector fleets over PJLink and Epson
ESC/VP21, SSHes cluster nodes to power them off, polls their reachability, and arms
the Shelly's hardware mains-cut timer. Python 3.11+, Poetry, import package
`cuemspowerbridge`, dh-virtualenv into the **shared** venv `/usr/lib/cuems/`,
Debian package `cuems-power-bridge`, service `cuems-power-bridge.service` (shipped
by this package since `bb69a9e`).

Two of its operations read cluster topology from `/etc/cuems/network_map.xml`: the
ordered shutdown sequence and the boot auto-load readiness gate. It was renamed
from `cuems-wsclient` in 2026-06; the console entry `cuems-wsclient` survives
deliberately.

**This daemon is the only CUEMS component that can cut mains power to other
machines.** Every principle below is subordinate to that fact.

## Core Principles

### I. It Cuts Mains Power — An Empty Step Is Not a Successful Step

A stage of the shutdown sequence that finds nothing to do MUST NOT be
indistinguishable from a stage that succeeded. An empty node-target list, an empty
display fleet, an empty peer set — each is an **anomaly to surface**, never a fast
path, and MUST be reported at ERROR with a `/status` state that an operator or a
monitor can tell apart from the healthy case.

Rationale: this is the defect the node_role migration exists to fix, and it
occurred twice in one process. A converted `network_map.xml` made both node
selections return empty; the shutdown logged `0 nodes to power off: (none)` at
INFO, powered off nobody, armed the Shelly and cut mains on running nodes. It
reads, in every log and every HTTP response, as a clean shutdown. The evidence
lives in the journal of a machine that then powered off, during a transition when
nobody is watching a terminal.

Corollary, non-negotiable: **fail-safe beats availability.** If the mains-cut
deadline cannot be confirmed — three failed Shelly RPCs — the bridge MUST NOT
poweroff locally. It returns 502 and stays up. Powering off without a confirmed
deadline is the one outcome worse than not powering off at all.

### II. The Sequence Is Ordered and Partially Irreversible

Steps have preconditions, and those preconditions MUST be checked on the anomalous
path as well as the happy one. **Skipping a verification step because its input was
empty is a correctness bug, not an optimisation.**

The live counter-example, which any change to `_run_shutdown()` MUST remove rather
than imitate: `bridge.py`'s reachability poll is guarded by `if resolved:`, so when
target resolution yields nothing the "are they actually down?" check does not run
at all — and the sequence proceeds to arm the relay.

Ordering rules that MUST hold: nodes are commanded off and **confirmed silent**
before the Shelly timer is armed; the Shelly is armed before the local poweroff;
projector power-off is cancelled against any in-flight power-on so `POWR 1` cannot
race `POWR 0`; concurrent `/shutdown` calls serialise on the asyncio lock and the
loser is refused, never queued. A change that reorders these MUST state why the new
order is still safe when each step fails.

### III. It Reads a Schema It Does Not Own

`network_map.xml` and `settings.xml` belong to `cuemsutils`, which versions them
deliberately, ships a validating reader, and ships a conversion tool for documents
written before a rename. This repository MUST reach that model through the
library's **public** path (`ConfigManager` / `cuemsutils.tools.*`); it MUST NOT
re-implement, re-spell or re-test the node model locally, and MUST NOT import
`cuemsutils.xml`, which is internal machinery.

Rationale: the private ElementTree reader in `network_map.py` is the **fourth**
copy of the node-identity model (after `cuemsutils`, `cuems-common`'s `cuems-logs`
and `cuems-nodeconf`'s). Being a private copy is precisely what let a rename in
another repository silently disable two features here while the suite stayed green.

Dependency pins MUST agree between `pyproject.toml` and `debian/control` and MUST
carry an upper bound or a `Breaks:`, not a floor alone: a floor cannot say "refuse
a library that has moved past me".

### IV. Degraded Paths Are Designed, and Their Triggers MUST Be Real

The single-controller auto-load branch, the missing-`<ip>` skip, the degraded
proceed after a node-join timeout, the "fleet unreachable, skip POWR 0" branch:
each is deliberate and field-measured. Each is correct **only when its trigger is
genuine**. A read that silently yields nothing counterfeits all of them at once.

Therefore: a degraded path entered because of a **read or parse failure** is a
defect, not a degradation, and MUST be distinguishable in code and in logs from the
same path entered because the cluster genuinely is that way. "The map lists no
other node" and "the parse produced nothing" MUST NOT be the same branch.

Two resolution policies are field-learned and MUST be preserved verbatim by any
refactor: node **power-off** resolution ignores `<ip>` (stale link-local on adopted
nodes) and resolves `role_id` → `alias` → `hostname` as `.local`, reporting the
unresolvable rather than dropping them; the **bus readiness gate** trusts `<ip>`,
because it matches NNG peers by address. A migration that "simplifies" either is a
regression.

### V. Talking to Hardware Over a Network Is the Normal Case

Shelly RPC, SSH, ICMP/TCP reachability, PJLink, ESC/VP.net, the engine and editor
WebSockets: timeouts, partial failures, unreachable hosts and hostile firmware are
the expected case, not the exception. Every outbound interaction MUST carry an
explicit timeout and a bounded retry budget, and MUST NOT be able to wedge a
poweroff transaction.

Per-device errors MUST stay isolated: one bad projector may not sink the fleet, and
the fleet may not delay the safety-critical sequence — hence the concurrent
power-off with a bounded join. Device quirks belong **behind the driver seam**
(`displays/base.py`'s `DisplayDriver`), never in `bridge.py` or the HTTP layer.

`dry_run = true` MUST exercise every branch of the state machine with no special
code paths — the same coroutines, gated by one boolean, logging what they would
have done. A branch that `dry_run` cannot reach is untestable off hardware.

### VI. The Shared Virtualenv Makes Packaging a Correctness Concern

`/usr/lib/cuems/` is shared with every other CUEMS Python component. This package
MUST NOT bundle anything another CUEMS package ships — `cuemsutils`, `websockets`,
`pythonosc`, `xmlschema` and the rest are stripped in `debian/rules` for that
reason, and `PYTHONNOUSERSITE=1` blocks `~/.local` pollution. Unique-to-bridge
dependencies (the aiohttp stack, `typing_extensions`) stay bundled.

Rationale, measured: a build that stripped `pythonosc` removed the only copy on
hosts where the engine runs editable from source, crash-looping the bridge **and**
breaking the engine's OSC at its next restart. A packaging change here can break a
sibling component that this repository never mentions. Every change to
`debian/rules`, `debian/control` or the dependency set MUST be verified against the
built `.deb`'s contents (`dpkg-deb -c`) before shipping.

### VII. The External Contract Is the Product

Four surfaces are consumed by things this repository does not control, and each
MUST be treated as published API — changed deliberately, versioned, and never
narrowed by accident:

1. **HTTP `:8478`** — the JSON shape `{"ok": bool, "reason"?: "<token>"}`, the
   status keys, the reason tokens and the status codes. Bitfocus Companion
   configurations, the Shelly mJS and `cuems-common`'s `cuems-displays-on` depend
   on them.
2. **The Shelly mJS** — ASCII-only (`Script.PutCode` rejects non-ASCII), its
   patched literals, and the installer's drift guards.
3. **The venv library surface** that `cuems-common` drives through a Python
   heredoc: `config.load()` and its field names, `network_map.parse()` and its node
   attributes, `reachability`, `node_executor`, `displays.manager`. It is an
   *undeclared* public API today, and is therefore **frozen** until the machinery
   that calls it is absorbed by this package (see
   `specs/planning/cuems-common-machinery-to-absorb.md`). Breaking it strands a
   poweroff transaction mid-flight.
4. **Coordinated wire or vocabulary changes land as one cutover.** A half-renamed
   state MUST NOT ship; where a change spans this repository and `cuems-common`,
   both halves merge together and the packaging expresses the edge with `Breaks:`.

## Operating Constraints

**Scope.** Controller-only. HTTP coordination, engine/editor WebSocket clients,
orderly cluster shutdown, boot auto-load, display power and brightness, Shelly RPC,
and the operator tooling for the above. Showcontrol semantics (what a cue *is*,
what loading means) belong to the engine and editor; cluster topology and the node
model belong to `cuemsutils`; node-side discovery and adoption belong to
`cuems-nodeconf`. This daemon routes and orchestrates — it does not own other
components' domain logic.

**Never auto-stop a running project.** `/shutdown` refuses with `project_running`
unless explicitly forced. A LOADED-but-not-playing engine is NOT "running". The
engine status cache declares UNKNOWN on disconnect and `is_known()` is checked
before the guard, so a stale `running == "no"` can never pass it.

**Identity is resolved, never assumed.** Nodes are addressed by avahi name derived
from `network_map.xml`, never by raw `<ip>`; `shelly_url` in the bridge config, by
contrast, MUST be an IP, because Shelly mDNS is unreliable. Both facts are
field-learned and MUST survive refactors.

**Privilege is bounded by design.** The node SSH key is written **locked** into
each node's `authorized_keys`
(`restrict,command="sudo /sbin/poweroff"`), so a leaked key can do nothing else.
Any widening of what that key may do MUST be justified against this constitution
first.

**Measure, do not transcribe.** Line numbers, occurrence counts, version pins and
"the suite is green" claims recorded in planning documents MUST be re-measured
against the live tree before being relied upon. This repository has repeatedly
found stale coordinates, and was itself missed twice and then double-counted by a
hand-maintained migration list.

**Review against what actually runs.** The live Shelly script (`Script.GetCode`)
and current upstream, not a local checkout — a field checkout was once 23 commits
behind and still on the retired package name.

## Direction of Travel — From Power Bridge to `cuems-bridge`

This component is expected to **widen into the ecosystem's general bridge**: a
router and translator between CUEMS and a growing variety of external hardware and
control surfaces, carrying substantially more logic traffic than the occasional
power and transport commands it carries today. At the point where "power" no longer
describes what it does, the component — **and this repository, and the Debian and
import package names** — are expected to shed that qualifier and become
**`cuems-bridge`**.

This is direction, not a scheduled feature. It binds current work in one way: **the
cost of that widening and that rename MUST NOT be allowed to grow.** Concretely,
while this constitution stands:

- **Protocol knowledge stays behind a seam.** Every new device or transport is
  implemented as a driver behind an abstraction like `displays/base.py`'s
  `DisplayDriver`. Device- or vendor-specific behaviour MUST NOT leak into
  `bridge.py`, the HTTP layer, or the config loader.
- **Name by function, not by "power".** New modules, config keys, HTTP routes,
  state names and log messages are named for what they do (`shutdown`, `route`,
  `display`, `transport`), not for the current package name. Existing
  `power`-flavoured names are legacy to be retired at the rename, not a pattern to
  follow.
- **The rename must stay a one-day change.** A single import package, console entry
  points declared in one place, and no third-party surface that hard-codes the
  string `cuems-power-bridge` beyond what packaging requires. The `cuems-wsclient`
  → `cuems-power-bridge` rename (2026-06, v0.2.6) is the precedent, including its
  one deliberate backwards-compatible console entry; a future rename SHOULD follow
  the same shape and keep `cuems-power-bridge` working for one release.
- **Do not hard-code today's topology.** One Shelly, one controller, one engine,
  one editor and a single flip-switch are the current deployment, not an invariant.
  New code SHOULD express "the relay", "the engine", "a control surface" as
  addressable, configurable participants wherever that costs nothing today.
- **Traffic growth is a design constraint now.** The daemon is a single asyncio
  event loop with cooperative concurrency. Any blocking call, unbounded buffer,
  per-message allocation or serialised critical section added today is a throughput
  ceiling later; parsing and I/O that can grow with traffic MUST be off the event
  loop (the mtime-keyed `network_map` cache and its executor hop are the pattern).
- **Widening scope does not widen the safety envelope.** However many devices,
  transports and routes this component acquires, Principles I and II continue to
  govern the mains-cut path, which MUST remain identifiable, bounded and
  independently verifiable rather than one route among many.

An amendment to this document is required to widen the component's *responsibility*
(new classes of hardware, new routing duties, the rename itself) — not merely a
feature spec. That amendment MUST state what the new responsibility does to the
shutdown path's guarantees.

## Testing Gate and Development Workflow

**The gate.** `pytest` MUST pass before implementation of any feature starts and
before any merge — 15 test files under `tests/`, currently 142 tests, all green. A
feature MUST NOT be implemented on a red suite; pre-existing failures are cleared
first, in their own commit.

Runner note: the default interpreter has no `pytest`, `aiohttp`, `websockets` or
`python-osc`. What works is
`uv run --python 3.11 --with pytest --with pytest-asyncio --with pytest-mock --with aiohttp --with websockets --with python-osc python -m pytest -q`.

**A green suite is not evidence when the fixtures are complicit.** The
network-map fixtures were written in the retired vocabulary, so the suite passed
*because* it certified the defect. For any change to a value read from a document
or a protocol this repository does not own, the deliverable is **a test that fails
against the old value**, and its failing run MUST be recorded. Fixtures MUST be
valid against the owning schema.

**Callers that keep resolving but become wrong are searched for, not waited for.**
Nothing fails, nothing crashes, the suite stays green and the answer is silently
wrong. This class is the most expensive one in the ecosystem, and this repository
is its clearest instance.

**Hardware verification is stated, never implied.** The suite exercises no real
Shelly, projector, node or cluster. Any change to the shutdown sequence, the
auto-load gate, SSH fan-out, reachability, display drivers or the mJS MUST state
how it was verified on real hardware, or state plainly that it was not. Both are
acceptable; silence is not. Features that fail and recover independently — orderly
power-off and the auto-load gate are the standing example — MUST be verified
**separately**; "the bridge works now" is not an answer to either.

**Build and install reality.** Build on the box (`dpkg-buildpackage -b -uc -us`;
the `.deb` lands in the parent directory); pre-flight the target for venv file
collisions before installing. Live `.py` hotfixes into the venv yield an untracked
hybrid and are a last resort, not a workflow.

**Commits are GPG-signed.** Retry on "gpg failed to sign"; never `--no-gpg-sign`.
Planning artifacts live in `specs/planning/`, feature artifacts in `specs/NNN-*/`.

## Governance

This constitution supersedes other practices for `cuems-power-bridge`. Where it and
a planning document disagree, this document governs, and the disagreement is
recorded rather than silently resolved.

**Amendment procedure.** Amendments MUST be proposed as a change to this file,
carry a Sync Impact Report at its head, and state the rationale for the principle
added, changed or removed. A principle MUST NOT be weakened to accommodate an
in-flight migration; if a migration cannot satisfy a principle, that is a finding
about the migration. Widening the component's responsibility, or renaming it,
requires an amendment before the work starts.

**Versioning policy.** Semantic versioning of the document itself:
- **MAJOR** — a principle is removed or redefined incompatibly.
- **MINOR** — a principle or section is added, or guidance materially expanded.
- **PATCH** — clarification, wording, or non-semantic refinement.

**Compliance review.** Every feature plan MUST include a constitution check naming
which principles bear on it and how they are satisfied. Reviews MUST verify
compliance, and complexity introduced against a principle MUST be justified in
writing rather than assumed. `CLAUDE.md` remains the runtime development guidance
for this repository and is expected to stay consistent with this document.

**Version**: 1.0.0 | **Ratified**: 2026-09-22 | **Last Amended**: 2026-09-22
