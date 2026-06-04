<!--
***
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
***
-->

# Changelog

All notable changes to `cuems-wsclient` are documented here.

The Debian package uses a separate revision suffix (`-1`, `-2`, …) in `debian/changelog`
for packaging-only changes. The Python package version (`pyproject.toml`,
`cuemswsclient.__version__`) is authoritative for the software version.

---

## [Unreleased] — post-0.2.0 Debian revisions

Changes committed since v0.2.0 and tracked in `debian/changelog` as Debian package
revisions 0.2.1-1 through 0.2.5-1. All are currently UNRELEASED; the Python package
version in `pyproject.toml` has not yet been bumped.

### Added

- **`controller_poweroff_cmd` config field** (0.2.1-1) — optional override for the local
  controller's poweroff or reboot command. When empty (default) the bridge falls back to
  `poweroff_cmd`, preserving identical behaviour for all existing deployments. The split
  supports two real-world use cases: (1) you want the controller to self-recover during
  testing without relying on Wake-on-LAN (WoL-from-S5 can be flaky across r8169 hardware),
  so operators can set `poweroff_cmd = sudo /sbin/poweroff` (nodes truly power
  off) and `controller_poweroff_cmd = sudo /usr/bin/systemctl reboot` (controller cycles
  back automatically — both code paths run for real); (2) maintenance flows where nodes
  should fully power down but the controller must recover autonomously.

- **`/usr/bin/` shims for operator CLIs** (0.2.2-1) — three shell wrapper scripts at
  `/usr/bin/cuems-power-bridge`, `/usr/bin/cuems-power-bridge-deploy-keys`, and
  `/usr/bin/cuems-power-bridge-install-mjs`. Each `exec`s the venv binary at
  `/usr/lib/cuems/bin/<tool>`, placing the CLIs on the operator's PATH. The systemd
  `ExecStart` still uses the absolute venv path.

- **`debian/cuems-power-bridge.service` shipped from this package** (0.2.3-1) — the systemd
  unit was moved from `cuems-common` to `cuems-wsclient`. `cuems-power-bridge` is an opt-in
  controller-only daemon; shipping its unit in `cuems-common` (which installs on every CUEMS
  host) was incorrect. Picked up automatically by `dh_installsystemd`. The `99-cuems-poweroff`
  sudoers file remains in `cuems-common` since every node needs the `NOPASSWD` entry for the
  SSH fan-out `sudo /sbin/poweroff` to succeed without prompting.

### Fixed

- **Auto-load editor-success race logged a false failure** (0.2.1-1) — when the editor's
  `project_ready` acknowledgement arrived before `wait_engine`'s first sleep cycle in
  `_try_auto_load`, the function fell through to the "timed out waiting for engine status"
  branch and incremented the failure counter, even though the engine was in fact loading
  the project. Three consecutive races would have disabled auto-load for the bridge session.
  Root cause: the `asyncio.wait` result was checked for `FIRST_COMPLETED` winner only; when
  `wait_editor_error` completed with `False` (no error — success) and `wait_engine` had not
  yet completed, neither `True` branch fired. Fix: after `asyncio.wait`, accept two
  additional positive paths before declaring failure: (a) editor success + engine cache
  shows `project_loaded()`; (b) engine cache shows `project_loaded()` regardless of which
  task completed first.

- **Shelly mJS template default `BRIDGE` corrected to `controller.local`** (0.2.4-1) — the
  previous template default was the raw bond0 IP (`192.168.x.x`). This was wrong on two
  counts: the bond0 IP is not stable (it takes DHCP if available and only falls back to
  a static lease, so the IP shifts across boots), and the rationale "mJS won't resolve
  `.local`" was false — verified end-to-end on a real Shelly Pro 1 that its mJS HTTP client
  resolves `controller.local` cleanly. Both the bundled template and `install_shelly_mjs.py`'s
  `_patched_code` search string now use `http://controller.local:8478`.

- **Shelly mJS template contained a non-ASCII em-dash** (0.2.5-1) — a `U+2014` em-dash in
  the `formitgo.local` migration comment introduced in 0.2.4-1 caused `Script.PutCode` to
  return `-103: Missing or bad argument 'code'!`. Shelly silently drops the offending byte;
  the `install_shelly_mjs.py` pre-flight ASCII validator raises before upload so operators
  see a clear error rather than a corrupt script on the device. Replaced with `--`.
  **Reminder: the `cuems-shutdown.js` template must remain ASCII-only.**

### Notes

- The `formitgo.local` migration is in progress: a bond0 avahi alias rename is planned
  where `bond0` becomes `formitgo.local` and `controller.local` re-scopes to a non-bond0
  "ipv4all" interface. When that migration ships, both the mJS `BRIDGE` constant and
  Companion's HTTP module URL must be updated to `http://formitgo.local:8478`. The template
  carries an inline comment documenting this.

---

## v0.2.0 — 2026-05-27

First complete release of the `cuems-power-bridge` daemon and the CUEMS cluster shutdown
infrastructure. Establishes the full HTTP + WebSocket-OSC + Shelly + SSH + systemd stack.

### Added

- **`cuems-power-bridge` daemon** — asyncio HTTP server on `:8478` coordinating orderly
  cluster shutdown for a Shelly Pro 1 flip-switch and a Bitfocus Companion Stream Deck.
  Single process; `asyncio.Lock` prevents double-trigger.

- **`POST /shutdown`** — full shutdown sequence: token check → refuse-if-running guard →
  SSH fan-out `sudo poweroff` to every `NodeType.slave` parsed from
  `/etc/cuems/network_map.xml` → reachability poll (ICMP + TCP/22 fallback, 3 consecutive
  failures, configurable max wait) → Shelly `Switch.GetStatus` pre-flight → Shelly
  `Switch.Set { on:true, toggle_after:N }` hardware safety timer → local
  `sudo systemctl poweroff --no-block`. If the Shelly RPC fails after 3 retries the bridge
  aborts and does **not** issue the local poweroff — fail-safe, mains stay on.

- **`POST /go` and `POST /stop`** — thin HTTP → WebSocket-OSC proxies for Bitfocus
  Companion (Companion's OSC module is UDP/TCP, not WebSocket-OSC). GO is gated by
  `engine_state.armed == "yes"`; a 409 is returned early rather than letting the engine
  silently no-op. 200 ms per-endpoint rate-limit via `_RateLimiter`.

- **`GET /status`** — JSON snapshot: `state`, `since`, `engine_state`, `nodes_pending`,
  `shelly_timer_armed_s`, `last_error`. No authentication required.

- **`EngineClient`** — persistent binary-OSC WebSocket client to `ws://localhost:9190`.
  Maintains a four-field status cache (`running`, `armed`, `load`, `nextcue`); all fields
  revert to `UNKNOWN` on disconnect. Reconnects with exponential backoff (1→2→4→8→16 s cap).
  `/shutdown` returns 503 while the cache is `UNKNOWN`.

- **`EditorClient`** — persistent JSON WebSocket client to `ws://localhost:9092`. Used for
  the auto-load path: sends `{"action": "project_ready", "value": "<uuid>"}` and detects
  `{"type": "error", "action": "project_ready"}` for fail-fast on unknown UUID.

- **Auto-load on boot** — `auto_load_project = <uuid>` in config: the bridge watches the
  engine cache and triggers `project_ready` when the engine reports an empty load state.
  `auto_load_persistent = false` (default): once per bridge process. `true`: re-arms on
  every observed empty-load for unattended installations. Three consecutive failures
  permanently disable auto-load for the session.

- **`ShellyClient`** — aiohttp-based Shelly Gen 2 RPC client. `Switch.GetStatus` +
  `Switch.Set`. 3-retry with 1/3/9 s exponential backoff. `ShellyError` abort path.

- **`NetworkMap` parser** (`network_map.py`) — namespace-agnostic XML reader; resolves
  `NodeType.slave` entries to Avahi `.local` names (`role_id` → `alias` → `hostname`).
  Never reads the `<ip>` field.

- **`poweroff_all`** (`node_executor.py`) — `asyncio.gather` SSH fan-out. Fire-and-forget;
  reachability poll is the ack. 15 s per-host SSH timeout; dry-run logs the command.

- **`wait_until_all_down`** (`reachability.py`) — ICMP + TCP/22 poller, 3 consecutive
  failures to confirm down, configurable interval and max wait.

- **`parse_osc_message`** (`osc_parse.py`) — binary OSC parser matching the engine's wire
  format. Supports all standard type tags: `i`, `f`, `s`, `b`, `T`, `F`, `N`, `I`, `d`.

- **`Config` dataclass + loader** (`config.py`) — layered: package-data defaults first,
  system `/etc/cuems/power-bridge.conf` overrides on top. Startup validation enforces
  Shelly timer range (45–300 s), minimum wait (≥ 30 s), and URL schemes.

- **Debian packaging** — `dh-virtualenv`-based build; installs into the shared cuems venv
  at `/usr/lib/cuems/`. `postinst` generates the ed25519 SSH keypair at
  `/etc/cuems/power-bridge.key{,.pub}` on first install and drops a starter
  `/etc/cuems/power-bridge.conf`. Ships the Shelly mJS template at
  `/usr/share/cuems/shelly-mjs/cuems-shutdown.js`.

- **`cuems-power-bridge-deploy-keys`** — distributes `/etc/cuems/power-bridge.key.pub` to
  every node's `/home/cuems/.ssh/authorized_keys` via the operator's own SSH credentials.
  Idempotent (append-if-not-present). Verifies directory and file permissions.

- **`cuems-power-bridge-install-mjs`** — uploads the bundled `cuems-shutdown.js` to a
  Shelly Pro 1 via its HTTP RPC. Patches `BRIDGE` + `TOKEN` inline. Pre-flight ASCII
  validator raises before upload if a non-ASCII character is detected. Removes any
  pre-existing `cuems-shutdown` script; enables auto-start; confirms `running=true`.
  Uploaded in 1024-byte chunks (`Script.PutCode` buffer limit).

- **`cuems-shutdown.js` Shelly mJS template** — registers a `statusHandler` for `input:0`;
  fires `HTTP.POST /shutdown` when the flip-switch transitions to OFF (`delta.state ===
  false`). `inflight` boolean debounce + 10 s `Timer.set` fail-safe. Logs all bridge
  response codes (200, 409, 401, 503, 502) to the Shelly console.

- **Repo restructured** to `src/cuemswsclient/` layout with `pyproject.toml` (Poetry build
  backend) and four console entry points: `cuems-power-bridge`, `cuems-power-bridge-
  deploy-keys`, `cuems-power-bridge-install-mjs`, `cuems-wsclient`.

### Notes

- `osclistener.py` and `wsclient.py` are retained for backwards compatibility with
  operator scripts from the pre-bridge era. They are not recommended for new deployments.
- Long-term, the SSH fan-out poweroff path is planned for replacement by an engine-native
  `/engine/command/shutdown` that broadcasts COMMAND/SHUTDOWN via the existing NNG bus.
  Only `node_executor.py` / `Bridge._shutdown_nodes()` would change (~85 % of the codebase
  carries forward).

---

## Pre-v0.2.0 — legacy wsclient era

Early-stage, venue-specific scripts that pre-date the bridge architecture.

### Added

- **`wsclient.py`** — one-shot CLI: connects to the editor WebSocket (`/ws`), sends
  `{"action": "project_ready", "value": "<uuid>"}`, waits for `project_ready` OK, then
  connects to the engine WebSocket (`/realtime`), sends a binary OSC GO command, and waits
  for the engine confirmation. Reads the project UUID from a command-line argument or from
  `/etc/cuems/project_id`.

- **`osclistener.py`** — UDP OSC server for a specific venue deployment (`afrucat`). Binds
  to `192.168.2.204:6007`. Handles `/afrucat/shutdown` (poweroff), `/afrucat/restart`
  (reboot), and `/afrucat/program` (switch project by number then restart) OSC addresses.
  Calls venue-specific shell scripts.
