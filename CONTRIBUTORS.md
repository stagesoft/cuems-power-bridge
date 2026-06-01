<!--
***
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
***
-->

# Contributing to cuems-wsclient

Thank you for contributing. This document defines the full contribution workflow for
`cuems-wsclient`. Following it is a condition of review.

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Development Setup](#2-development-setup)
3. [Contribution Tiers](#3-contribution-tiers)
4. [Branch Naming](#4-branch-naming)
5. [Spec-First Requirement](#5-spec-first-requirement)
6. [TDD Workflow — Non-Negotiable](#6-tdd-workflow--non-negotiable)
7. [Commit Hygiene](#7-commit-hygiene)
8. [Developer Certificate of Origin (DCO)](#8-developer-certificate-of-origin-dco)
9. [Pull Request Requirements](#9-pull-request-requirements)
10. [Acceptance Criteria](#10-acceptance-criteria)
11. [Review Process](#11-review-process)
12. [Changelog Line](#12-changelog-line)
13. [Dependency Governance](#13-dependency-governance)
14. [License](#14-license)

---

## 1. Prerequisites

| Tool | Minimum version | Notes |
|---|---|---|
| Python | 3.11 | Match the declared `python = "^3.11"` in `pyproject.toml` |
| Poetry | 1.7 | Package and dependency manager |
| Git | 2.34 | For DCO sign-off support |
| openssh-client | any | Required by `cuems-power-bridge-deploy-keys` |
| iputils-ping | any | Required by the reachability poller in production |
| debhelper | 13 | For building the Debian package |
| dh-virtualenv | ≥ 1.2 | For building the Debian package |

Install Poetry:

```bash
pip install --user poetry
# or
curl -sSL https://install.python-poetry.org | python3 -
```

---

## 2. Development Setup

```bash
# Clone
git clone https://github.com/stagesoft/cuems-wsclient.git
cd cuems-wsclient

# Create the virtualenv and install all dependencies (including dev group)
poetry install

# Verify the environment
poetry run python -c "import cuemswsclient; print(cuemswsclient.__version__)"

# Smoke test (compile all modules)
python3 -m compileall -q src/

# Run the test suite
poetry run pytest

# Build the Debian package (requires debhelper + dh-virtualenv on Debian 12)
debuild -b -uc -us -nc
```

The test suite uses `pytest-asyncio` in `asyncio_mode = "auto"` and `pytest-mock` for
WebSocket and subprocess mocking. Both are declared in `[tool.poetry.group.dev.dependencies]`.

---

## 3. Contribution Tiers

### Tier 1 — Trivial changes

Examples: spelling/grammar fixes in documentation, adding or clarifying a `# comment`,
updating a URL, bumping a well-understood dependency version.

* No issue or spec required.
* No new tests required (though a test for a typo fix in a docstring is welcome).
* May be submitted directly as a PR.

### Tier 2 — Non-trivial changes

Examples: new features, behavioural changes, new config fields, new HTTP endpoints,
changes to the shutdown sequence, dependency additions or removals, refactoring that
touches multiple modules.

* **Requires a spec** (see §5 Spec-First Requirement) before any code is written.
* **Requires tests** (see §6 TDD Workflow) covering the new or changed behaviour.
* Must include a CHANGELOG line (see §12).
* Must pass all acceptance criteria (see §10).

If you are unsure whether your change is Tier 1 or Tier 2, treat it as Tier 2.

---

## 4. Branch Naming

All branches target `main`. Use the `feat/`, `fix/`, `chore/`, `docs/` prefixes:

| Prefix | Use for |
|---|---|
| `feat/<short-description>` | New features or capabilities |
| `fix/<short-description>` | Bug fixes |
| `chore/<short-description>` | Build, CI, packaging, dependency changes |
| `docs/<short-description>` | Documentation-only changes |
| `refactor/<short-description>` | Code restructuring without behaviour change |

Examples:

```
feat/nng-broadcast-shutdown
fix/auto-load-race-condition
chore/debian-shim-bin-paths
docs/architecture-diagram-update
```

Keep branch names lowercase, hyphen-separated, and under 50 characters.

---

## 5. Spec-First Requirement

For Tier 2 changes, open a GitHub Issue **before writing any code**. The issue must contain:

1. **Problem statement** — what the current behaviour is and why it is wrong or insufficient.
2. **Proposed solution** — the concrete change: new config keys, new HTTP endpoints,
   changed module interfaces, changed data structures.
3. **Invariants** — what the change guarantees (e.g., "dry_run=true exercises the new path
   without SSH calls").
4. **Test plan** — a list of test cases that would prove the spec is satisfied.
5. **Migration / backwards-compatibility** — if an existing config key, API response field,
   or data structure changes, how existing deployments are affected.

Do not open a PR that implements a Tier 2 change without a linked spec issue. PRs without
a spec issue will be closed and asked to re-open after the issue is created.

**Why:** the bridge coordinates hardware (Shelly relay, SSH to nodes, systemctl poweroff).
A misunderstanding at the spec stage is far cheaper to fix than a misunderstanding in a
deployed cluster.

---

## 6. TDD Workflow — Non-Negotiable

For every Tier 2 change, follow this sequence strictly:

1. **Write a failing test** that directly captures the spec invariant from §5. The test
   must fail before your implementation changes.
2. **Write the minimum implementation** to make the test pass. Do not add untested code.
3. **Refactor** the implementation for clarity and consistency. Tests must remain green.

```bash
# TDD iteration loop
poetry run pytest tests/test_<module>.py -v --tb=short

# Run the full suite before opening a PR
poetry run pytest
```

**Practical notes:**

* Use `pytest-asyncio` for all coroutines; the `asyncio_mode = "auto"` configuration
  means no `@pytest.mark.asyncio` decorator is needed.
* Use `pytest-mock` (`mocker` fixture) to mock WebSocket connections, subprocess calls,
  and Shelly RPC responses. Never make real network connections in tests.
* Place test files in `tests/test_<module_name>.py` (e.g., `tests/test_bridge.py`,
  `tests/test_network_map.py`).
* Use `asyncio.create_subprocess_exec` mock patterns to verify SSH and poweroff calls
  in `test_node_executor.py` without opening real connections.

---

## 7. Commit Hygiene

This project uses **Conventional Commits v1.0**.
See [conventionalcommits.org](https://www.conventionalcommits.org/en/v1.0.0/) for the full
specification.

### Format

```
<type>(<scope>): <short summary>

[optional body]

[optional footer]
Signed-off-by: Your Name <your@email.com>
```

### Allowed types

| Type | Use for |
|---|---|
| `feat` | New feature or behaviour |
| `fix` | Bug fix |
| `chore` | Build, CI, packaging, dependency change |
| `docs` | Documentation only |
| `refactor` | Code restructuring without behaviour change |
| `test` | Adding or fixing tests |
| `perf` | Performance improvement |
| `style` | Code style change (formatting, whitespace) |

### Allowed scopes

| Scope | Module |
|---|---|
| `bridge` | `bridge.py` |
| `config` | `config.py` |
| `engine` | `engine_state.py` |
| `editor` | `editor_client.py` |
| `shelly` | `shelly.py` |
| `network_map` | `network_map.py` |
| `node_executor` | `node_executor.py` |
| `reachability` | `reachability.py` |
| `osc` | `osc_parse.py` |
| `scripts` | any file under `scripts/` |
| `shelly-mjs` | `cuems-shutdown.js` |
| `debian` | anything under `debian/` |
| `build` | `pyproject.toml`, `debian/rules` |
| *(omit)* | cross-cutting or unclear |

### Examples

```
feat(bridge): add controller_poweroff_cmd config field

Adds an optional override for the local controller's poweroff command.
Empty string (default) falls back to poweroff_cmd — identical behaviour
for existing deployments.

Closes #42
Signed-off-by: Ion Reguera <ion@stagelab.coop>
```

```
fix(bridge): auto-load editor-success race produced false failure log

When the editor's project_ready ack arrived before wait_engine's first
sleep cycle, _try_auto_load fell through to a timeout failure log even
though the engine was loading. Adds two positive acceptance paths:
(a) editor success + engine cache loaded, (b) engine cache loaded
regardless of task completion order.

Signed-off-by: Ion Reguera <ion@stagelab.coop>
```

```
chore(shelly-mjs): keep template ASCII -- em-dash -> "--"

Shelly's Script.PutCode rejects any non-ASCII byte with -103. The
template must remain ASCII-only.

Signed-off-by: Ion Reguera <ion@stagelab.coop>
```

### Rules

* **One logical change per commit.** Do not bundle unrelated fixes.
* **Short summary ≤ 72 characters.** No trailing period.
* **Body explains WHY, not WHAT.** The diff shows what changed; the body explains the
  motivation, the constraint, or the non-obvious design choice.
* **No `WIP` commits** in PRs. Squash or rebase before opening.

---

## 8. Developer Certificate of Origin (DCO)

Every commit must carry a `Signed-off-by` trailer. This is your declaration that the
contribution is your original work and that you have the right to submit it under the
project's GPL-3.0-or-later licence.

**Add to every commit:**

```
Signed-off-by: Your Name <your@email.com>
```

Use `git commit -s` to add it automatically:

```bash
git commit -s -m "feat(bridge): add dry_run logging for reachability poll"
```

Full text of the DCO: [developercertificate.org](https://developercertificate.org/).

PRs with unsigned commits will not be merged. If you have unsigned commits on your branch:

```bash
git rebase --signoff HEAD~<n>   # sign the last n commits
git push --force-with-lease
```

---

## 9. Pull Request Requirements

Before opening a PR, verify every item:

- [ ] Branch is based on the latest `main`
- [ ] All commits are Conventional Commits v1.0 with DCO sign-off
- [ ] `poetry run pytest` passes with no failures
- [ ] `python3 -m compileall -q src/` exits 0
- [ ] No `print()` in production code (use `logging`)
- [ ] All new source files carry the SPDX header:
  ```python
  # SPDX-FileCopyrightText: <year> Stagelab Coop SCCL
  # SPDX-License-Identifier: GPL-3.0-or-later
  ```
- [ ] Tier 2 changes: spec issue is linked in the PR description
- [ ] Tier 2 changes: a CHANGELOG line is drafted in `debian/changelog`
- [ ] Tier 2 changes: new or modified behaviour is covered by at least one test

**PR description template:**

```markdown
## What

One sentence: what does this PR do?

## Why

One sentence: why is this change needed? Link to the spec issue.

## Test plan

- [ ] `poetry run pytest` passes
- [ ] Describe any manual verification done (e.g., dry_run test against a live bridge)

## Breaking changes

List any changes to: HTTP response fields, config key names or types, CLI option names.
If none, write "None."
```

**PR target:** `main`. Never target a feature branch.

---

## 10. Acceptance Criteria

A PR is accepted when all of the following are true:

| Criterion | Verification |
|---|---|
| Tests pass | `poetry run pytest` exits 0 |
| No compile errors | `python3 -m compileall -q src/` exits 0 |
| Conventional Commits | All commit summaries match `<type>(<scope>): <summary>` |
| DCO sign-off | Every commit has `Signed-off-by:` |
| SPDX headers | Every new file has the copyright + licence header |
| No regression | No existing test broken by the PR |
| Test coverage (Tier 2) | New behaviour has at least one direct test |
| Spec linked (Tier 2) | PR description links a GitHub Issue with the spec |
| No `print()` in src | Production code uses `logging` exclusively |
| No secrets or credentials | No hardcoded tokens, keys, or passwords in any file |

---

## 11. Review Process

PRs are reviewed by one of the two project maintainers:

* **Ion Reguera** ([@ibiltari](https://github.com/ibiltari)) — primary maintainer
* **Adrià Masip** ([@backenv](https://github.com/backenv)) — co-maintainer

At least one maintainer approval is required before merge. Maintainers aim to review within
5 business days. If a PR has no review after 10 business days, ping the issue or PR thread.

**Review scope:**

Reviewers check correctness, safety (the bridge touches real hardware), test coverage,
commit hygiene, and spec compliance. Code style comments are advisory unless they affect
readability or correctness.

**Responding to review:**

* Address every comment. If you disagree, explain why in the thread.
* Push fixup commits, then `git rebase -i` to squash before final approval.
* Do not force-push after a reviewer has approved — create a new fixup commit instead.

**Questions and discussion:**

Open a GitHub Issue at [github.com/stagesoft/cuems-wsclient/issues](https://github.com/stagesoft/cuems-wsclient/issues).

---

## 12. Changelog Line

Every Tier 2 PR must add an entry to `debian/changelog`. Use `dch` or edit manually.
Follow the existing format:

```
cuems-wsclient (<new-version>-1) UNRELEASED; urgency=medium

  * <scope>: <one-line summary of the change, imperative mood>
    <Optional second line with the key invariant or motivation.>

 -- Your Name <your@email.com>  <RFC-2822 date>
```

Example:

```
cuems-wsclient (0.2.6-1) UNRELEASED; urgency=medium

  * bridge: add /status endpoint timestamp for monitoring integrations.
    The `since` field now carries an ISO-8601 UTC timestamp of the last
    state transition, enabling external watchdogs to detect stale state.

 -- Ion Reguera <ion@stagelab.coop>  Mon, 01 Jun 2026 10:00:00 +0200
```

The changelog entry is the authoritative record of what changed and why. Write it for a
future operator who must diagnose a production issue — not for the reviewer.

---

## 13. Dependency Governance

All runtime dependencies are declared in `pyproject.toml` under
`[tool.poetry.dependencies]`. Dev-only tools belong in
`[tool.poetry.group.dev.dependencies]`.

**Rules:**

* **Pin lower bounds, not upper bounds.** Use `>=X.Y` not `==X.Y`. Upper bounds may block
  security updates.
* **No new runtime dependency without a spec issue.** Every dependency is a long-term
  maintenance commitment. Discuss in the issue first.
* **Optional dependencies** (like `cuemsutils`) use the `optional = true` + `extras`
  mechanism. The base install must not require them.
* **System packages** (`python3-systemd`, `openssh-client`, `iputils-ping`) are declared in
  `debian/control` as `Depends`, not in `pyproject.toml`. Do not add pip packages that
  wrap system binaries.
* **Security advisories:** if a dependency has a published CVE, open a PR immediately with
  the version bump. Include the CVE number in the commit message body.

**Checking for issues:**

```bash
poetry show --outdated
```

---

## 14. License

All contributions to `cuems-wsclient` are accepted under the
[GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0.html)
(`GPL-3.0-or-later`).

By submitting a contribution you:

1. Certify the Developer Certificate of Origin (§8).
2. Agree to license your contribution under GPL-3.0-or-later.
3. Confirm you are not knowingly introducing code under an incompatible licence.

If your contribution includes code from a third party, state the licence explicitly in the
PR description. Copyleft-compatible licences (LGPL, MPL, Apache 2.0 with GPLv3 exception)
may be acceptable after maintainer review. Non-copyleft-compatible licences will be
rejected.

Every new file must start with the SPDX header block:

```python
# SPDX-FileCopyrightText: <year> Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Your Name <your@email.com>
```

For Markdown files, use the HTML comment form:

```markdown
<!--
***
SPDX-FileCopyrightText: <year> Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
***
-->
```
