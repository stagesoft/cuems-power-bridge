# cuems-power-bridge

CUEMS WebSocket client utilities and the **cuems-power-bridge** daemon
(Shelly Pro 1 / Bitfocus Companion shutdown coordinator).

> Renamed from `cuems-wsclient` (Python package `cuemswsclient`) at 0.2.6.
> The repo, source package and import package are now `cuems-power-bridge`
> / `cuemspowerbridge`. The legacy `cuems-wsclient` console entry point is
> kept for backwards compatibility.

## What this repo contains

| Component | Purpose |
|-----------|---------|
| `cuems-power-bridge` | HTTP `:8478` coordinator. Receives `POST /shutdown` from Shelly mJS or Stream Deck; runs the orderly cluster shutdown (refuse-if-running guard → SSH-fanout poweroff to nodes → reachability poll → arm Shelly hardware safety timer → `systemctl poweroff --no-block`). Also fronts `POST /go` and `POST /stop` (Companion's HTTP module → bridge → engine WebSocket OSC). Auto-loads a configurable show project on boot via the editor's `project_ready` action. |
| `cuems-power-bridge-deploy-keys` | One-shot helper for operators. Distributes `/etc/cuems/power-bridge.key.pub` to every node's `/home/cuems/.ssh/authorized_keys`. |
| `cuems-wsclient` (legacy CLI) | The original `wsclient.py` — loads a project (via editor `/ws`) and sends GO (via engine `/realtime`). Kept for backwards compat. |

The bridge lives on the controller as a systemd unit (`cuems-power-bridge.service`, shipped in `cuems-common`).

## Architecture

```
Stream Deck Nano ─USB─► Bitfocus Companion ─HTTP─►
                                                   │
              flip-switch (SW0) ─► Shelly Pro 1 ─HTTP─►
                                                   │
                                                   ▼
                          ┌───────────────────────────────────┐
                          │ cuems-power-bridge (controller)   │
                          │ HTTP :8478                        │
                          │ ─ asyncio HTTP server             │
                          │ ─ editor WS  → ws://localhost:9092 (JSON, project_ready)
                          │ ─ engine WS  → ws://localhost:9190 (binary OSC, GO/STOP/status)
                          │ ─ Shelly RPC client (Switch.Set toggle_after)
                          │ ─ SSH fan-out to nodes (poweroff)
                          │ ─ reachability poller
                          └───────────────────────────────────┘
```

## Endpoints

- `GET /status` → JSON: state, engine_state, nodes_pending, shelly_timer_armed_s, last_error
- `POST /go` → forwards `/engine/command/go` (Impulse). Returns 409 `not_armed` if engine isn't armed.
- `POST /stop` → forwards `/engine/command/stop` (Impulse).
- `POST /shutdown[?force=1]` → orderly cluster shutdown. Returns 409 `project_running` (unless `force=1` or `refuse_if_running=false`), 409 `shutdown_already_in_progress`, 503 `engine_state_unknown`, 502 `shelly_unreachable`, 401 `bad_token`.

All responses are JSON `{"ok": bool, "reason": "<token>"?}`. All endpoints validate the optional `X-Auth-Token` header against `power-bridge.conf:shared_token`.

## Shutdown flow

1. Acquire `asyncio.Lock` (concurrent calls get 409).
2. Token check; refuse-if-running guard against the engine status cache.
3. Parse `/etc/cuems/network_map.xml` → list of `NodeType.slave` avahi hostnames (`role_id.local`, `alias.local`, or `hostname.local`; **never** `<ip>`).
4. Parallel `ssh cuems@<host> sudo /sbin/poweroff` to every node (fire-and-forget).
5. Reachability poll (ICMP + TCP/22 fallback) until every node is silent or `shutdown_max_wait_s` elapses.
6. Pre-check Shelly `Switch.GetStatus`; abort if `output=false` (pre-existing fault).
7. Arm Shelly hardware safety timer: `Switch.Set { on: true, toggle_after: shelly_safety_timer_s }`. Relay opens after that many seconds regardless of what the bridge or the controller do next.
8. Local `sudo systemctl poweroff --no-block` (real orderly shutdown — `ExecStop=` hooks run, journald flushes, ext4 commits, network down).
9. Shelly cuts mains on an already-off controller.

If the Shelly RPC fails after 3 retries, the bridge **does not** issue the local poweroff — it logs an error and returns 502. Better to fail-safe with the cluster still up than to power off without a confirmed mains-cut deadline.

## Auto-load project

If `auto_load_project = <uuid>` is set in `power-bridge.conf`, the bridge sends `{"action": "project_ready", "value": "<uuid>"}` over the editor WebSocket when the engine reports an empty load state. The same path `wsclient.py` uses (editor does media validation + NNG deploy to nodes; engine status broadcasts the project's `unix_name` on `/engine/status/load`).

- `auto_load_persistent = false` (default) — once per bridge process. Engine restart does NOT retrigger.
- `auto_load_persistent = true` — fire on every observed empty-load. For unattended installations that must come back up after any failure.

Bad UUID → editor returns `{"type": "error", "action": "project_ready"}` → counts as one failure. 3 consecutive failures disable auto-load for the session.

## Configuration

`/etc/cuems/power-bridge.conf` — installed from `/usr/share/cuems/power-bridge.conf.default` on first install. Operator must edit:

- `shelly_url` (use IP, not `.local` — Shelly's DNS is unreliable)
- `shared_token` (recommended on LAN deployments)
- `auto_load_project` (project UUID to auto-load on boot)
- `shelly_safety_timer_s` (45..300; default 60)

See `src/cuemspowerbridge/data/power-bridge.conf.default` for the full list.

## Shelly mJS template

`/usr/share/cuems/shelly-mjs/cuems-shutdown.js` — paste into the Shelly's Scripts tab via its web UI. Edit `BRIDGE` (controller IP) and `TOKEN` (matches `shared_token`) at the top. The script reacts to SW0 flipping to OFF and HTTP-POSTs `/shutdown` to the bridge.

The Shelly input is a **flip-switch**, not momentary. `addStatusHandler` fires only on state deltas, so leaving the switch in OFF position when applying mains is safe (no shutdown attempt on boot — operator must flip ON, then back OFF, to trigger).

## Bootstrap on a fresh cluster

1. Install `cuems-power-bridge` on the controller.
2. Edit `/etc/cuems/power-bridge.conf`.
3. `sudo systemctl start cuems-power-bridge`. Verify with `curl http://localhost:8478/status`.
4. Distribute the bridge's SSH public key to every node:
   ```bash
   cuems-power-bridge-deploy-keys node01.local node02.local ...
   ```
   (Run as the operator, not as the cuems service user. Uses your own SSH credentials.)
5. Verify the bridge can SSH every node:
   ```bash
   ssh -i /etc/cuems/power-bridge.key cuems@node01.local sudo /sbin/poweroff --no-wall --dry-run
   ```
6. Install the Shelly mJS template via the Shelly web UI (Scripts tab).
7. Configure Bitfocus Companion buttons:
   - GO → `HTTP POST http://controller.local:8478/go`, header `X-Auth-Token`
   - STOP → `http://controller.local:8478/stop`
   - SHUTDOWN → `http://controller.local:8478/shutdown`

## Test (no live shutdown)

`dry_run = true` in the config: full state machine runs, but SSH/Shelly RPC/`systemctl poweroff` calls are **logged** instead of executed. Useful for validating the refuse-if-running guard, reachability poll, and Shelly URL before going live.

## Development

```bash
# Smoke test (imports + config + osc parse)
python3 -m compileall -q src/

# Build the .deb (on a Debian-12 host with dh-virtualenv installed)
debuild -b -uc -us -nc
```

## Future migration

Long-term, the SSH-fanout poweroff path should be replaced by an engine-native `/engine/command/shutdown` that broadcasts COMMAND/SHUTDOWN via the existing NNG bus. The bridge's external contract (HTTP shape, auth, response codes, Shelly RPC, mJS, Companion buttons) does not change — only `_shutdown_nodes()` in `bridge.py` is rewritten to a single OSC send. ~85% of the codebase carries forward unchanged. See the design plan at `cuems-RELATIONS/.claude/plans/we-need-shelly-pro-jolly-chipmunk.md`.
