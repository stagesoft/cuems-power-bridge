<!--
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
-->

# T021 — the extraction, reviewed as a move

**Before**: `evidence/sequence-before.txt` (`bridge.py:508-643` @ `ac46f78`).
**After**: `src/cuemspowerbridge/cluster_shutdown.py` + the rewired `_run_shutdown`.

Every line that is **not** a pure move, and why.

## 1. Deliberately left behind in `bridge.py`

The relay pre-check, `arm_timer`, and the local power-off (steps 8 and 10). They are
`/shutdown`'s alone: the ExecStop hook must never do either — *"only steps 1-2 are needed"* —
and `/shutdown` **ends by triggering the transition that runs the hook**, so a shared sequence
would arm the relay twice per shutdown (FR-001a). `_cancel_auto_play_task` and
`_cancel_projector_on_task` stay too: they cancel the daemon's own tasks.

## 2. Added to the display stage — behaviour the daemon did not have

`bridge.py` called `displays.power_off_all()` directly. The shared stage adds the three gates
the ExecStop heredoc has always had:

| Gate | Effect on `/shutdown` |
|---|---|
| `projector_power_off_on_shutdown=false` | now honoured on this route too — a venue that keeps its fleet dark is no longer overridden by an API shutdown |
| status queried first; all `off`/`cooldown` ⇒ skip `POWR 0` | the re-entrant transition is a fast no-op instead of a second full PJLink cycle |
| every device `unknown` ⇒ skip the fleet | saves ~19 s of certain failure per shutdown on an unreachable fleet |

**This is a change to the product path**, recorded rather than waved through. All three can
only ever *reduce* what is commanded, and each replaces an unconditional action with a
conditional one whose condition the other caller has relied on for months.

## 3. Made caller-specific rather than unified — caught by the suite

`dry_run` and the reachability wait. The heredoc skips the wait under `dry_run` (nothing was
commanded off, so nothing will go down); `/shutdown` has always polled regardless. The first
draft merged them, which silently changed `/shutdown` — `test_case2_targets_adopted_nodes_and_polls_them`
failed, which is exactly what that test is for. Now `dry_run_skips_wait` is a parameter:
`False` for the daemon, `True` for the hook. Both verbatim.

## 4. Mechanical, not behavioural

- `self._set_state(...)` → `ctx.progress(event, detail)`; the daemon maps the events straight
  back onto its state machine (`_on_stage_progress`), so `/status` is unchanged.
- `self._nodes_pending` is updated from the same events.
- `self.cfg` → `ctx.cfg`, `self.displays` → `ctx.displays`.
- log lines are **identical strings**, including the PARTIAL error and the controller-only
  notice.

## 5. Test-harness follow-on (not a behaviour change)

`tests/test_shutdown_cases.py` patched `bridge.poweroff_all` / `bridge.wait_until_all_down`;
those names now live in `cluster_shutdown`, so the patch targets moved. **No assertion
changed.**

## Verdict

One deliberate product-path change (§2), one near-miss the suite caught (§3), everything else
a move. 193 passed.
