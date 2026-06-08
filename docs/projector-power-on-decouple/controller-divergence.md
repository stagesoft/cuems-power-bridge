# Note: controller installed copy diverges from `main` (power-on debounce)

> **Open item — reconcile before/with deploy.** Recorded 2026-06-08 while
> implementing the projector power-on decoupling (see `plan.md`).

## What
The Badajoz/Formitgo controller's **installed** `cuems-power-bridge`
(`/usr/lib/cuems/lib/python3.11/site-packages/cuemspowerbridge/bridge.py`) is
**ahead of git `main`** — an uncommitted overlay (same hazard as the
cuems-engine canvas-fix overlay: silently lost on the next package upgrade).

Known divergence: the controller's `_on_engine_status` carries a **time
debounce** — module const `_PROJECTOR_ON_DEBOUNCE_S = 30.0` plus a
`_last_projector_on_monotonic` timestamp — that suppresses projector power-on
re-firing when the engine's `load` status flaps (auto-load re-drives, engine WS
reconnect status-dumps). Its in-code comment: *"an auto-load re-drive can flip
load empty→non-empty repeatedly; only (re)spawn power-on if we haven't done so
within the debounce window."*

`main` has **only** the `_project_loaded_seen` edge-detector (no time debounce),
so on `main` every empty→loaded edge re-fires `POWR 1` to all configured
projectors. The debounce looks like a real, wanted hardening fix that never got
committed back.

## Why it matters
- The repo is **not** the source of truth for what the controller runs — there
  may be more controller-only changes than just this debounce.
- The `feat/projector-power-on-decouple` branch was cut from `main`, so it does
  not include the debounce. It is correct against `main` as-is, but the startup
  and `/poweron` power-on paths should integrate with the debounce once it
  exists in-tree.

## To reconcile (when the cluster is free — it was under power-cycle testing on
## 2026-06-08)
1. `diff` the controller's installed `bridge.py` (and `config.py`) against repo
   `main` to capture the **full** divergence, not just the debounce.
2. Commit that divergence to the repo as its own fix (with a clear message
   describing the load-flap/reconnect re-fire it prevents).
3. Rebase `feat/projector-power-on-decouple` on top, then make
   `_maybe_power_on_at_start()` and `handle_poweron` **prime
   `_last_projector_on_monotonic`** so they participate in the debounce window.
