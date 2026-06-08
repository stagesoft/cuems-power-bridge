# Plan: Decouple projector power-on from project load

> **Status:** Implemented on branch `feat/projector-power-on-decouple` (commit
> `eb9d1d6`) against `main`. Tests pass (39, incl. 10 new in
> `tests/test_bridge_poweron.py`). **Not deployed.** See
> `controller-divergence.md` in this folder — the controller runs a power-on
> debounce that is **not** in `main`, to be reconciled before/with deploy.

## Context
`cuems-power-bridge` powers projectors ON **only** as a side effect of a project loading: `_on_engine_status` fires `_spawn_projector_power_on()` when the engine reports `load`, gated by `projector_power_on_on_load=true`. The HTTP API exposes only `/status /go /stop /shutdown` — there is **no standalone or boot-time power-on**.

Consequence (hit on the Badajoz/Formitgo cluster 2026-06-08): disabling autoload (`auto_load_project` commented out) also disabled boot-time projector power-on, while the Shelly `POST /shutdown` path still powered everything off — an asymmetric lifecycle. Also, even with autoload on, warm-up only *begins after* the project loads (~10–30s on the critical path).

**Goal:** add a projector power-on path independent of project load — (1) `projector_power_on_on_start` fires projectors ON at bridge **startup** (warming in parallel with the node-wait/load), and (2) a **`/poweron` HTTP route** symmetric with `/shutdown` for on-demand/Shelly/Companion power-on. ClickUp `869dkyv8w`.

## Approach (as implemented)
Two additive changes, reusing existing machinery (`DisplayManager.power_on_all()`, `_spawn_projector_power_on()`, the `handle_shutdown` shape). Idempotent, non-blocking.

1. **`config.py`** — new `projector_power_on_on_start: bool = False` (auto-parsed; `_coerce` handles the bool).
2. **`bridge.py`**
   - `_spawn_projector_power_on(reason="project loaded")` — neutral, accurate logging.
   - `_maybe_power_on_at_start()` — a **separate** method called unconditionally from `start()`, gated **only** by `projector_power_on_on_start and self.displays.configured` (NOT nested under `projector_power_on_on_load`, so the flag is live under the `on_load=False, on_start=True` config the Badajoz case needs). Fire-and-forget; does **not** touch `_project_loaded_seen`.
   - `POST /poweron` (`handle_poweron`) — token + rate-limit + `displays.configured` checks, then `self._spawn_projector_power_on("/poweron request")` and returns `200` immediately (fire-and-forget so a slow/unreachable projector can't hold the response open; the `_projector_on_task` guard dedups concurrent calls — no extra lock needed).
3. Docs: `power-bridge.conf.default` (the new key + the restart-re-powers caveat) and `CHANGELOG.md`.

## Risks / edge cases
- **Restart re-powers projectors:** `projector_power_on_on_start=True` fires on **every** service start (incl. crash/upgrade restarts). If projectors were manually powered off mid-interval, a restart wakes them. Ships **default `False`**; document so operators only enable it where unconditional power-on-at-start is wanted.
- **`displays.configured`** is a static "≥1 display declared" bool (confirmed), not a liveness check — `/poweron` won't 503 just because a projector is warming.
- **Debounce divergence:** `main` has no time-debounce (only the `_project_loaded_seen` edge-detector); the controller's installed copy does. Once the debounce lands in-tree, `_maybe_power_on_at_start()` and `handle_poweron` should also prime `_last_projector_on_monotonic`. See `controller-divergence.md`.

## Verification (repo only — no deploy)
- `python3 -m compileall src/` — clean.
- Full suite (venv: `aiohttp websockets python-osc pytest pytest-asyncio pytest-mock`, `PYTHONPATH=src`, `pytest tests/`): **39 passed**.
- New tests cover: config flag parse/default; startup fires when enabled **incl. `on_load=False`** (regression guard); no-op when disabled / no displays; in-flight dedup; `/poweron` 200/401/503/429.

## Follow-ups (deferred)
- Reconcile the controller divergence (see sibling note), then rebase `feat/projector-power-on-decouple` and add the debounce-prime.
- On-cluster validation (boot → parallel warm-up; `curl -X POST .../poweron`) at deploy time.
- Optional refinement: only fire startup power-on if displays aren't already ON / gate on system uptime to distinguish a true boot from a service restart.
