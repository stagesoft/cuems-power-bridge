<!--
***
SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
SPDX-License-Identifier: GPL-3.0-or-later
***
-->

# cuems-power-bridge

**Current release: v0.2.0** (development / pre-release) — see [CHANGELOG.md](./CHANGELOG.md).

**HTTP coordinator for CUEMS cluster shutdown, show control, and boot-time auto-load — fronting a Shelly Pro 1 hardware safety relay and a Bitfocus Companion Stream Deck.**

[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)

* **Source / issues:** [stagesoft/cuems-power-bridge](https://github.com/stagesoft/cuems-power-bridge) on GitHub

**cuems-power-bridge** daemon: an asyncio HTTP server that
coordinates orderly shutdown of a CUEMS multi-node audio/video production cluster when an
operator flips a wired Shelly Pro 1 power switch or presses a button on a Bitfocus Companion
Stream Deck. It also proxies GO and STOP commands from Companion's HTTP module into the binary
WebSocket-OSC protocol spoken by the CUEMS engine, and auto-loads a configurable show project
on every controller boot.

The key safety guarantee is a **hardware-enforced mains-cut deadline**: the bridge arms the
Shelly's built-in `toggle_after` timer before issuing the local `systemctl poweroff`. The relay
opens after the configured number of seconds regardless of what the software does next — even
if the controller hangs mid-shutdown, the cluster cannot stay powered on indefinitely.

It is composed of:

* **`cuems-power-bridge`** — the HTTP daemon (`:8478`): shutdown coordinator, GO/STOP proxy,
  auto-load, and status endpoint.
* **`cuems-power-bridge-deploy-keys`** — one-shot operator helper to distribute the bridge's
  SSH public key to every cluster node's `authorized_keys`.
* **`cuems-power-bridge-install-mjs`** — one-shot operator helper to upload the bundled
  Shelly mJS script to the physical Shelly device via its HTTP RPC.
* **`cuems-wsclient`** — the original legacy CLI (`wsclient.py`): loads a project and sends
  GO via the editor and engine WebSocket endpoints. Kept for backwards compatibility with
  existing operator scripts.
* **`osclistener`** — legacy venue-specific UDP OSC server (afrucat deployment). Pre-dates
  the bridge; superseded for new deployments.

---

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
  - [`bridge`](#bridge)
  - [`config`](#config)
  - [`engine_state`](#engine_state)
  - [`editor_client`](#editor_client)
  - [`shelly`](#shelly)
  - [`network_map`](#network_map)
  - [`node_executor`](#node_executor)
  - [`reachability`](#reachability)
  - [`osc_parse`](#osc_parse)
  - [`scripts/`](#scripts)
  - [Legacy components](#legacy-components)
  - [Threading and process model](#threading-and-process-model)
- [Core Concepts](#core-concepts)
- [Design Goals](#design-goals)
- [API documentation](#api-documentation)
  - [HTTP REST API](#http-rest-api)
  - [CLI reference](#cli-reference)
  - [Configuration reference](#configuration-reference)
  - [Process exit codes](#process-exit-codes)
- [Installation](#installation)
  - [Debian package (recommended)](#debian-package-recommended)
  - [Development install](#development-install)
- [Usage](#usage)
  - [Bootstrap on a fresh cluster](#bootstrap-on-a-fresh-cluster)
  - [Install the Shelly mJS script](#install-the-shelly-mjs-script)
  - [Configure Bitfocus Companion](#configure-bitfocus-companion)
  - [Dry-run and smoke testing](#dry-run-and-smoke-testing)
- [Development](#development)
- [Contributors](#contributors)
- [Release notes](#release-notes)
- [Future developments](#future-developments)
  - [Automated test suite](#automated-test-suite)
  - [CI/CD pipeline](#cicd-pipeline)
  - [Documentation site](#documentation-site)
  - [Packaging and release](#packaging-and-release)
  - [Target badge set](#target-badge-set)
- [Copyright notice](#copyright-notice)
- [License](#license)

---

## Overview

The diagram below shows the full signal path from operator input to mains-cut.

```
Stream Deck Nano ─USB─► Bitfocus Companion ─HTTP POST /go|/stop|/shutdown──────►
                                                                                  │
         flip-switch (SW0) ─► Shelly Pro 1 ─HTTP POST /shutdown ────────────────►
                                                                                  │
                                                                                  ▼
                         ┌────────────────────────────────────────────────────────────────┐
                         │                 cuems-power-bridge (:8478)                     │
                         │                                                                │
                         │  Config ──► Bridge (asyncio) ──┬─► EngineClient               │
                         │                                │   ws://localhost:9190          │
                         │                                │   binary OSC status cache     │
                         │                                │   (running/armed/load/nextcue)│
                         │                                │                               │
                         │                                ├─► EditorClient               │
                         │                                │   ws://localhost:9092          │
                         │                                │   JSON project_ready          │
                         │                                │                               │
                         │  POST /shutdown ───────────────►                               │
                         │                                ├─► network_map.xml parser     │
                         │                                │   slave avahi names only      │
                         │                                │                               │
                         │                                ├─► SSH fan-out                │
                         │                                │   asyncio.gather              │
                         │                                │   node01.local … nodeN.local  │
                         │                                │                               │
                         │                                ├─► Reachability poll           │
                         │                                │   ICMP ping + TCP/22          │
                         │                                │                               │
                         │                                └─► ShellyClient               │
                         │                                    Switch.GetStatus            │
                         │                                    Switch.Set toggle_after=T   │
                         └────────────────────────────────────────────────────────────────┘
                                          │ sudo systemctl poweroff --no-block
                                          ▼
                                   [controller halts]
                                          │ Shelly hardware timer fires after T seconds
                                          ▼
                                     [mains cut]
```

**What each layer does:**

* **Bitfocus Companion** — Stream Deck button interface; issues HTTP POSTs to `/go`, `/stop`,
  `/shutdown` using Companion's HTTP module. The bridge translates these to WebSocket-OSC
  frames for the engine, or to the full shutdown sequence.
* **Shelly Pro 1** — wired flip-switch triggered by a physical power switch (SW0). On
  transition to OFF the bundled mJS script fires an HTTP POST to `/shutdown`. The Shelly also
  acts as the mains relay: `Switch.Set toggle_after` arms a hardware timer that cuts power
  after T seconds regardless of software state.
* **cuems-power-bridge** — the single coordinator. Maintains two persistent WebSocket
  connections (engine + editor) with reconnect-with-backoff, runs the orderly shutdown state
  machine, and exposes status for monitoring.
* **CUEMS engine** (`ws://localhost:9190`) — receives binary OSC `GO` and `STOP` commands;
  broadcasts `/engine/status/*` on every state change, which the bridge caches.
* **CUEMS editor** (`ws://localhost:9092`) — receives JSON `project_ready` action for auto-load
  on boot; looks up the UUID in its SQLite DB and IPCs the engine.
* **Cluster nodes** — SSH target for `sudo /sbin/poweroff`; identified by Avahi hostnames
  derived from `network_map.xml` (never by raw IP).

[↑ Back to Table of Contents](#table-of-contents)

---

## Architecture

### `bridge`

**Module:** `src/cuemswsclient/bridge.py`

The central coordinator. Owns the HTTP server, the state machine, and all sub-clients.

* **`Bridge`** — asyncio HTTP server (`aiohttp.web`) and shutdown coordinator; instantiates
  `EngineClient`, `EditorClient`, `ShellyClient`, and `_RateLimiter`; owns the
  `asyncio.Lock` that serialises concurrent shutdown attempts.
* **`Bridge.start()`** — wires the four HTTP routes, starts the aiohttp `TCPSite`, starts
  the engine and editor WebSocket tasks, and spawns the `auto-load` background task.
* **`Bridge.stop()`** — cleanly stops engine and editor clients.
* **`Bridge.handle_status(request)`** — `GET /status`; returns the status payload without
  authentication (unauthenticated for monitoring tools).
* **`Bridge.handle_go(request)`** — `POST /go`; validates token, checks rate limit, checks
  `engine.armed == "yes"`, sends `/engine/command/go` as a binary OSC impulse.
* **`Bridge.handle_stop(request)`** — `POST /stop`; same flow without the armed check.
* **`Bridge.handle_shutdown(request)`** — `POST /shutdown`; acquires `asyncio.Lock`, runs
  the refuse-if-running guard, then delegates to `_run_shutdown()`.
* **`Bridge._run_shutdown()`** — implements the 8-step shutdown sequence:
  parse `network_map.xml` → SSH fan-out → reachability poll → arm Shelly hardware timer →
  issue local `systemctl poweroff --no-block`.
* **`Bridge._auto_load_loop()`** — background asyncio Task; watches the engine status cache
  every 2 s; triggers `_try_auto_load()` when the engine reports an empty load state and
  the configured UUID is non-empty.
* **`Bridge._try_auto_load()`** — race-aware auto-load: sends `project_ready` to the editor,
  then waits on two concurrent tasks — `wait_engine` (polls the engine cache for 60 s) and
  `wait_editor_error` (awaits the editor's response frame). Three consecutive failures
  permanently disable auto-load for the process lifetime.
* **`_RateLimiter`** — per-endpoint minimum-interval gate (default 200 ms); prevents GO/STOP
  replay from jittery Companion buttons or mJS debounce failures.
* **`STATES`** — valid state machine states: `"idle"`, `"checking"`, `"polling"`,
  `"arming-shelly"`, `"poweroff-issued"`, `"done"`, `"failed"`.

**Shutdown state machine:**

```
idle ──► checking ──► polling ──► arming-shelly ──► poweroff-issued ──► done
           │                            │
           ▼ (auth/running/engine err)  ▼ (Shelly RPC fails 3×)
          idle                        failed   ← mains stay on, cluster up
```

[↑ Back to Table of Contents](#table-of-contents)

---

### `config`

**Module:** `src/cuemswsclient/config.py`

Layered configuration loader.

* **`Config`** — `dataclasses.dataclass` of all configuration fields with typed defaults.
  Unknown keys are stored in `extras: dict` without error.
* **`Config.validate()`** — hard-validates at startup: `shelly_safety_timer_s` must be
  45–300 s; `shutdown_max_wait_s` must be ≥ 30 s; `shelly_url` must start with `http(s)://`;
  both WebSocket URLs must start with `ws(s)://`.
* **`load(path=None)`** — loads `power-bridge.conf.default` from the package data first,
  then overlays `/etc/cuems/power-bridge.conf` (or the path argument). Missing system file
  is silently accepted (defaults apply). Calls `validate()` before returning.
* **`_parse(text, cfg)`** — parses `key = value # comment` lines; coerces values to the
  field's declared type (bool, int, str) via `_coerce()`.

[↑ Back to Table of Contents](#table-of-contents)

---

### `engine_state`

**Module:** `src/cuemswsclient/engine_state.py`

Persistent binary-OSC WebSocket client for the CUEMS engine.

* **`EngineClient`** — maintains a long-lived WebSocket connection to
  `ws://localhost:9190`; receives binary OSC frames and updates a four-field status cache.
* **`EngineClient.is_known()`** — returns `True` iff connected and the on-connect state dump
  has been received (i.e., `running != UNKNOWN`).
* **`EngineClient.project_running()`** — `True` iff `running == "yes"`. Callers must gate
  via `is_known()` first.
* **`EngineClient.project_loaded()`** — `True` iff `load` is non-empty and not `UNKNOWN`.
  The `load` field carries the project's `unix_name`, not its UUID.
* **`EngineClient.send_osc(address, value=None)`** — builds a binary OSC frame with
  `python-osc` and sends it over the WebSocket. Impulse-type args (GO, STOP) pass
  `value=None`.
* **`EngineClient.on_status(cb)`** — registers a listener called with `(key, value)` on
  every `/engine/status/*` broadcast.
* **Status cache fields:** `running` (`"yes"` | `"no"` | `UNKNOWN`), `armed`
  (`"yes"` | `"no"` | `UNKNOWN`), `load` (project `unix_name` or `""` or `UNKNOWN`),
  `nextcue` (string or `UNKNOWN`). All four revert to `UNKNOWN` on disconnect.
* **Reconnect:** exponential backoff 1 → 2 → 4 → 8 → 16 s (cap). The cache is marked
  `UNKNOWN` the instant the WebSocket drops — `/shutdown` returns 503 while `UNKNOWN`.

[↑ Back to Table of Contents](#table-of-contents)

---

### `editor_client`

**Module:** `src/cuemswsclient/editor_client.py`

Persistent JSON WebSocket client for the CUEMS editor.

* **`EditorClient`** — maintains a long-lived WebSocket connection to
  `ws://localhost:9092`; used exclusively for the auto-load path.
* **`EditorClient.send_action(action, value)`** — sends `{"action": ..., "value": ...}`
  JSON. Returns `False` if the WebSocket is not connected.
* **`EditorClient.wait_for_response(action, timeout)`** — clears any prior cached response
  for the action, then awaits a fresh response frame matching the action within `timeout`
  seconds. Returns the response dict or `None` on timeout. Used for fail-fast detection of
  `{"type": "error", "action": "project_ready"}` (unknown UUID).
* **Protocol:** The editor sends `{"type": "<action>", "value": ...}` on success or
  `{"type": "error", "action": "<action>", ...}` on failure. The client keys by `"action"`
  field for error frames, by `"type"` otherwise.
* **Reconnect:** exponential backoff 1 → 2 → 4 → 8 → 16 s (cap). Loopback only; no
  authentication (loopback is the trust boundary per the editor's `CuemsWsServer.py:119`).

[↑ Back to Table of Contents](#table-of-contents)

---

### `shelly`

**Module:** `src/cuemswsclient/shelly.py`

Shelly Gen 2 RPC client.

* **`ShellyClient`** — `aiohttp`-based HTTP client posting to `<base_url>/rpc/{method}`.
  Supports optional HTTP Basic auth (note: Shelly Gen 2 normally uses digest auth; most
  fielded units are without auth).
* **`ShellyClient.get_status()`** — `Switch.GetStatus { id }` pre-flight check; the bridge
  reads `output: bool` to detect a pre-existing fault (relay already open).
* **`ShellyClient.arm_timer(seconds)`** — `Switch.Set { id, on: true, toggle_after: seconds }`.
  On an already-closed relay, `on: true` is a state no-op; `toggle_after` schedules a
  hardware flip to OFF after `seconds` — the mains-cut deadline. Range validated at config
  load (45–300 s).
* **`ShellyClient.call_with_retry(method, params)`** — 3-retry with 1 / 3 / 9 s exponential
  backoff; raises `ShellyError` after all retries are exhausted.
* **`ShellyError`** — exception raised when all retries fail. Caught in `Bridge.handle_shutdown`;
  the bridge **aborts and does not poweroff** (fail-safe: mains stay on).

[↑ Back to Table of Contents](#table-of-contents)

---

### `network_map`

**Module:** `src/cuemswsclient/network_map.py`

Reads `/etc/cuems/network_map.xml` and resolves cluster nodes to Avahi hostnames.

* **`Node`** — frozen dataclass: `uuid`, `avahi` (resolved `.local` name or `None`),
  `role_id`, `alias`, `hostname`, `node_type` (`"NodeType.master"` | `"NodeType.slave"`).
* **`parse(path)`** — namespace-agnostic XML scan: accepts both namespaced and
  non-namespaced `<node>` elements; returns all nodes (master + slave).
* **`slave_avahi_names(path)`** — filters to `NodeType.slave`; resolves each to an Avahi
  hostname using the priority `role_id.local` → `alias.local` → `hostname.local`. Returns
  `(resolved: list[str], unresolvable: list[Node])`. The `<ip>` field is intentionally
  ignored — it is a stale link-local in many adopted nodes.

[↑ Back to Table of Contents](#table-of-contents)

---

### `node_executor`

**Module:** `src/cuemswsclient/node_executor.py`

SSH-based parallel poweroff. This is the module targeted for replacement in the future
NNG-broadcast migration; all other bridge code is unaffected.

* **`SshTarget`** — dataclass: `host` (Avahi hostname), `user`, `key_path`, `poweroff_cmd`.
* **`poweroff_all(targets, dry_run)`** — launches `_ssh_one()` for every target with
  `asyncio.gather`; fire-and-forget (reachability poll is the ack). Returns
  `{host: rc0_ok}`.
* **`_ssh_one(target, dry_run, connect_timeout=5)`** — runs
  `ssh -i <key> -o ConnectTimeout=5 -o BatchMode=yes -o StrictHostKeyChecking=accept-new
  <user>@<host> -- <poweroff_cmd>` as an asyncio subprocess with a 15 s timeout.
  SSH failures are logged as warnings; the reachability poll still runs regardless.
  In `dry_run` mode the command is logged but not executed.

[↑ Back to Table of Contents](#table-of-contents)

---

### `reachability`

**Module:** `src/cuemswsclient/reachability.py`

Polls a set of hosts until they are all confirmed unreachable, or until a timeout.

* **`PollResult`** — dataclass: `elapsed_s`, `stuck_hosts: list[str]`, `timed_out: bool`.
* **`wait_until_all_down(hosts, interval_s=2.0, max_wait_s=180, confirm_failures=3)`** —
  probes all non-confirmed-down hosts in parallel every `interval_s`. A host is confirmed
  down after `confirm_failures` consecutive failed probes (debounces transient network
  blips during orderly shutdown). Returns when all hosts are confirmed down or
  `max_wait_s` elapses. Stuck hosts are logged at WARNING; the shutdown proceeds regardless.
* **`_alive(host)`** — considers a host alive if EITHER `_ping_once()` (ICMP via
  `/usr/bin/ping -c 1 -W 1`) OR `_tcp_once()` (TCP connect to port 22) succeeds. The TCP
  fallback handles environments where `ping` lacks `setcap` network capabilities.

[↑ Back to Table of Contents](#table-of-contents)

---

### `osc_parse`

**Module:** `src/cuemswsclient/osc_parse.py`

Binary OSC message parser.

* **`parse_osc_message(data: bytes)`** — parses a raw binary OSC frame into
  `(address: str, args: list[Any]) | None`. Mirrors the wire format of the engine's own
  `WebSocketOscHandler.parse_osc_message` (cuems-engine) so the bridge decodes the exact
  same frames. Supports OSC type tags: `i`, `f`, `s`, `b`, `T`, `F`, `N`, `I`, `d`.
  Returns `None` on any parse failure.

[↑ Back to Table of Contents](#table-of-contents)

---

### `scripts/`

**Module:** `src/cuemswsclient/scripts/`

Console-script entry points. All four are wired in `pyproject.toml` and installed at
`/usr/lib/cuems/bin/` by the `.deb`, with `/usr/bin/` shims for operator convenience.

* **`scripts/power_bridge.py: main()`** — the `cuems-power-bridge` entry point. Parses
  `--config` and `--log-level` args, sets up `logging.basicConfig`, calls `asyncio.run(_run())`.
  Sends `READY=1` to systemd via `systemd.daemon.notify` (best-effort; no-op if
  `python3-systemd` is absent). Handles SIGINT / SIGTERM via `asyncio.Event`.
* **`scripts/deploy_keys.py: main()`** — the `cuems-power-bridge-deploy-keys` entry point.
  Reads `/etc/cuems/power-bridge.key.pub`, iterates the provided host list, calls
  `deploy()` for each.
* **`scripts/deploy_keys.py: deploy(host, pubkey, ssh_user)`** — SSHes to a single node
  using the operator's own credentials, appends the pubkey to
  `/home/cuems/.ssh/authorized_keys` (idempotent: skips if already present), and verifies
  directory and file permissions.
* **`scripts/install_shelly_mjs.py: main()`** — the `cuems-power-bridge-install-mjs` entry
  point. Loads the bundled template or a custom `.js` file, patches `BRIDGE` and `TOKEN`
  literals, validates ASCII-only (Shelly's `Script.PutCode` rejects non-ASCII bytes with
  `-103`), then calls `install()`.
* **`scripts/install_shelly_mjs.py: install(shelly_url, code, name="cuems-shutdown")`** —
  removes any existing Shelly script with the same name, creates a new one, uploads the
  code in 1024-byte chunks (`Script.PutCode`), enables auto-start, starts the script, and
  confirms `running=true`.

[↑ Back to Table of Contents](#table-of-contents)

---

### Legacy components

* **`wsclient.py: load_and_go(project, host)`** — opens the editor WebSocket (`/ws`),
  sends `project_ready`, waits for an OK response, then opens the engine WebSocket
  (`/realtime`) and sends a binary OSC GO command. One-shot; no reconnect.
* **`wsclient.py: main_cli()`** — `cuems-wsclient` CLI. Reads the project UUID from the
  command-line arg or from `/etc/cuems/project_id`; calls `load_and_go()`.
* **`osclistener.py`** — venue-specific UDP OSC server for the `afrucat` deployment (IP
  `192.168.2.204`, port 6007). Handles `/afrucat/shutdown`, `/afrucat/restart`, and
  `/afrucat/program` OSC addresses by calling local shell scripts and `shutdown`. Not
  recommended for new deployments; superseded by `cuems-power-bridge`.

[↑ Back to Table of Contents](#table-of-contents)

---

### Threading and process model

```
Main thread (asyncio event loop)
├── HTTP server coroutines (aiohttp; share the event loop)
│   ├── handle_status()        — unauthenticated, no lock
│   ├── handle_go()            — rate-limited, token-checked
│   ├── handle_stop()          — rate-limited, token-checked
│   └── handle_shutdown()      — asyncio.Lock (single concurrent shutdown)
│
├── Task: engine-ws            — persistent EngineClient._run_loop()
│   └── consumes binary OSC frames; updates running/armed/load/nextcue cache
│
├── Task: editor-ws            — persistent EditorClient._run_loop()
│   └── consumes JSON frames; stores responses keyed by action
│
└── Task: auto-load            — Bridge._auto_load_loop() (conditional on config)
    └── polls engine cache every 2 s; spawns sub-tasks for wait_engine + wait_editor_error

Subprocesses (asyncio.create_subprocess_exec — non-blocking, no threads)
├── ssh <user>@<host> -- <poweroff_cmd>   (one per slave node, gather'd)
├── /usr/bin/ping -c 1 -W 1 <host>        (one per still-up host, gather'd)
└── systemctl poweroff --no-block         (once, at the end of the sequence)

Signal handlers: SIGINT, SIGTERM → asyncio.Event.set() → graceful stop
systemd notify: Type=notify; READY=1 sent after bridge.start() returns
```

All state shared between the HTTP handlers, the WebSocket tasks, and the auto-load loop is
either read-only (config, resolved node list) or protected by `asyncio.Lock`
(shutdown sequence). No `threading.Thread` is used; all concurrency is cooperative asyncio.

[↑ Back to Table of Contents](#table-of-contents)

---

## Core Concepts

* **Orderly shutdown sequence** — the nine-step path from an HTTP trigger to confirmed
  mains-cut: token check → refuse-if-running guard → SSH fan-out to every
  `NodeType.slave` node → reachability poll until all nodes are silent → Shelly pre-flight
  check → arm `toggle_after` hardware timer → `systemctl poweroff --no-block` on the
  controller. The sequence is protected by `asyncio.Lock` so concurrent triggers (Shelly
  + Companion simultaneously) produce one shutdown and one 409.

* **Hardware safety timer** — `Switch.Set { on: true, toggle_after: T }` arms a timer
  inside the Shelly relay. After T seconds the relay opens (cuts mains), regardless of what
  the controller software does. This is the bridge's last-resort guarantee: a hung kernel,
  a crashed bridge, or a lost network connection cannot prevent the Shelly from cutting
  power.

* **Engine status cache** — `EngineClient` maintains a four-field in-memory snapshot
  (`running`, `armed`, `load`, `nextcue`) updated by `/engine/status/*` OSC broadcasts.
  All four fields revert to `UNKNOWN` the instant the WebSocket disconnects. The bridge
  returns 503 on any request that requires engine knowledge while the cache is `UNKNOWN`.

* **Refuse-if-running guard** — when `refuse_if_running = true` (the default), a `POST
  /shutdown` without `?force=1` returns 409 `project_running` if the engine cache shows
  `running == "yes"`. This prevents cutting power mid-performance.

* **Auto-load** — if `auto_load_project = <uuid>` is set, the bridge watches the engine
  cache. When the engine reports an empty load state, the bridge sends
  `{"action": "project_ready", "value": "<uuid>"}` to the editor WebSocket, triggering
  the same load path used by `wsclient.py` (editor does media validation and NNG deploy to
  nodes; the engine status broadcasts the project's `unix_name` on `/engine/status/load`).
  `auto_load_persistent = false` (default): fires once per bridge process — an engine
  restart does **not** retrigger it. `auto_load_persistent = true`: re-arms on every
  observed empty-load, for unattended installations that must recover from any failure.
  Three consecutive failures permanently disable auto-load for the bridge session.

* **Avahi hostnames** — the bridge resolves SSH targets exclusively from Avahi `.local`
  names derived from `network_map.xml` fields (`role_id` → `alias` → `hostname`). The
  `<ip>` field is never used because it carries a stale link-local in many adopted nodes.

* **Dry-run mode** — `dry_run = true` in the config causes the bridge to run the full
  state machine (token check, refuse guard, network_map parse, reachability poll structure)
  but log every side-effect (SSH, Shelly RPC, `systemctl poweroff`) at INFO level instead
  of executing it. Used to validate configuration before a live deployment.

* **Reconnect-with-backoff** — both `EngineClient` and `EditorClient` reconnect on any
  disconnect with 1 → 2 → 4 → 8 → 16 s exponential backoff. The bridge survives engine
  or editor restarts without requiring a bridge restart.

[↑ Back to Table of Contents](#table-of-contents)

---

## Design Goals

* **Fail-safe over availability.** If the Shelly RPC fails after three retries, the bridge
  returns 502 and does **not** issue the local poweroff. Better to leave the cluster running
  than to power off the controller without a confirmed mains-cut deadline.

* **Hardware enforcement.** The Shelly `toggle_after` timer is always armed before the
  controller powers off. Software bugs, kernel panics, and network faults cannot prevent the
  relay from opening: the hardware fires autonomously after T seconds.

* **Single asyncio event loop.** All concurrency is cooperative. `asyncio.Lock` on the
  shutdown handler serialises concurrent triggers without threads or inter-process
  synchronisation.

* **UNKNOWN-safe cache.** The engine status cache declares `UNKNOWN` on disconnect; every
  `/shutdown` call checks `is_known()` before reading `project_running()`. There is no
  window where a stale `running == "no"` can pass the guard while the engine is actually
  playing.

* **Avahi-first addressing.** Raw IP addresses from `network_map.xml` are explicitly
  ignored. All SSH targets use `.local` mDNS names, which remain stable across DHCP lease
  changes and cluster reconfigurations.

* **Separation of concerns.** `node_executor` is the sole module that issues SSH poweroffs.
  The future NNG-broadcast migration rewrites only `_shutdown_nodes()` / `node_executor`;
  the HTTP surface, the auth layer, the Shelly client, the reachability poller, and the
  auto-load loop carry forward unchanged (~85 % of the codebase).

* **Dry-run parity.** `dry_run = true` exercises every branch of the state machine.
  SSH calls, Shelly RPC calls, and the local poweroff command are logged at INFO level with
  a `[dry_run]` prefix. No special code paths; the same coroutines run, gated by a single
  boolean.

* **Idempotent operator tooling.** `cuems-power-bridge-deploy-keys` appends the SSH pubkey
  only if it is not already present in `authorized_keys`. Re-running it on already-prepared
  nodes is safe. `cuems-power-bridge-install-mjs` removes any existing `cuems-shutdown`
  script before uploading, making re-runs idempotent.

[↑ Back to Table of Contents](#table-of-contents)

---

## API documentation

### HTTP REST API

Base URL: `http://<controller>:8478` (default bind `0.0.0.0:8478`).

All endpoints except `GET /status` validate the `X-Auth-Token` header when
`shared_token` is configured. All responses are JSON `{"ok": bool, "reason": "<token>"}`.

---

#### `GET /status`

Returns a snapshot of the bridge state machine. **No authentication required.** Safe for
monitoring scripts and health-check endpoints.

```
GET /status HTTP/1.1

HTTP/1.1 200 OK
Content-Type: application/json

{
  "state":                "idle",
  "since":                "2026-05-27T14:30:00Z",
  "engine_state":         "loaded",
  "nodes_pending":        [],
  "shelly_timer_armed_s": 60,
  "last_error":           null
}
```

| Field | Type | Description |
|---|---|---|
| `state` | string | State machine state: `idle` / `checking` / `polling` / `arming-shelly` / `poweroff-issued` / `done` / `failed` |
| `since` | ISO-8601 UTC | Timestamp of the last state transition |
| `engine_state` | string | `unknown` (disconnected or cache empty), `idle` (no project), `loaded`, `running` |
| `nodes_pending` | list[string] | Avahi hostnames still being polled during reachability phase |
| `shelly_timer_armed_s` | int | Configured `shelly_safety_timer_s` (informational) |
| `last_error` | string\|null | Reason token from the last failure |

---

#### `POST /go`

Forward GO to the engine as a binary OSC impulse on `/engine/command/go`. The engine only
acts if it is in `armed` state; the bridge pre-checks and returns 409 early rather than
letting the engine silently no-op.

```
POST /go HTTP/1.1
X-Auth-Token: <token>

HTTP/1.1 200 OK
{"ok": true}
```

| Status | Reason token | Condition |
|---|---|---|
| 200 | — | GO sent |
| 401 | `bad_token` | Token mismatch |
| 409 | `not_armed` | Engine `armed != "yes"` |
| 429 | `rate_limited` | Called within 200 ms of the previous call |
| 502 | `engine_send_failed` | WebSocket send failed |
| 503 | `engine_state_unknown` | Engine disconnected or cache empty |

---

#### `POST /stop`

Forward STOP to the engine as a binary OSC impulse on `/engine/command/stop`.

```
POST /stop HTTP/1.1
X-Auth-Token: <token>

HTTP/1.1 200 OK
{"ok": true}
```

| Status | Reason token | Condition |
|---|---|---|
| 200 | — | STOP sent |
| 401 | `bad_token` | Token mismatch |
| 429 | `rate_limited` | Called within 200 ms of the previous call |
| 502 | `engine_send_failed` | WebSocket send failed |
| 503 | `engine_state_unknown` | Engine disconnected or cache empty |

---

#### `POST /shutdown[?force=1]`

Run the full orderly cluster shutdown sequence. Concurrent calls return 409 immediately
(only one shutdown sequence runs at a time).

```
POST /shutdown HTTP/1.1
X-Auth-Token: <token>

HTTP/1.1 200 OK
{"ok": true}
```

```
POST /shutdown?force=1 HTTP/1.1
X-Auth-Token: <token>
```

`?force=1` bypasses the refuse-if-running guard.

| Status | Reason token | Condition |
|---|---|---|
| 200 | — | Shutdown sequence started; `systemctl poweroff` has been issued |
| 401 | `bad_token` | Token mismatch |
| 409 | `project_running` | Engine is playing and `refuse_if_running=true` (without `?force=1`) |
| 409 | `shutdown_already_in_progress` | Another shutdown call is in progress |
| 500 | `internal_error` | Unexpected exception |
| 502 | `shelly_unreachable` | Shelly RPC failed after 3 retries; **poweroff aborted, mains stay on** |
| 503 | `engine_state_unknown` | Engine cache is UNKNOWN and `refuse_if_running=true` |

**Shutdown sequence (step by step):**

1. Acquire `asyncio.Lock` — concurrent calls return 409.
2. Token validation; refuse-if-running guard against the engine status cache.
3. Parse `/etc/cuems/network_map.xml` → list of `NodeType.slave` Avahi hostnames
   (`role_id.local` → `alias.local` → `hostname.local`; `<ip>` is never used).
4. Parallel `ssh cuems@<host> sudo /sbin/poweroff` to every node (fire-and-forget).
5. Reachability poll: ICMP + TCP/22 fallback, 3 consecutive failures to confirm down,
   every 2 s, up to `shutdown_max_wait_s` (default 180 s).
6. `Switch.GetStatus` pre-flight: abort if Shelly reports `output=false` (pre-existing fault).
7. Arm Shelly hardware safety timer: `Switch.Set { on: true, toggle_after: shelly_safety_timer_s }`.
   Relay opens after that many seconds regardless of what the bridge or the controller do next.
8. Local `sudo systemctl poweroff --no-block` — real orderly shutdown: `ExecStop=` hooks run,
   journald flushes, ext4 commits, network goes down. (`controller_poweroff_cmd` overrides if set.)
9. Shelly hardware timer fires and cuts mains on an already-off controller.

If the Shelly RPC fails after 3 retries at step 7, the bridge **does not** issue the local
poweroff. It returns 502 and logs `shutdown ABORTED: Shelly RPC failed`. The cluster stays up.

[↑ Back to Table of Contents](#table-of-contents)

---

### CLI reference

#### `cuems-power-bridge`

The systemd daemon. Reads configuration from `/etc/cuems/power-bridge.conf` (or a custom
path), starts the asyncio HTTP server, and listens until SIGTERM or SIGINT.

```
cuems-power-bridge [-c CONFIG] [--log-level {DEBUG,INFO,WARNING,ERROR}]
```

| Option | Default | Description |
|---|---|---|
| `-c`, `--config` | `$CUEMS_POWER_BRIDGE_CONF` or `/etc/cuems/power-bridge.conf` | Path to `power-bridge.conf` |
| `--log-level` | `$CUEMS_LOG_LEVEL` or `INFO` | Log verbosity |

**Environment variables:**

| Variable | Description |
|---|---|
| `CUEMS_POWER_BRIDGE_CONF` | Overrides the default config path |
| `CUEMS_LOG_LEVEL` | Overrides the default log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

---

#### `cuems-power-bridge-deploy-keys`

One-shot operator helper. Distributes `/etc/cuems/power-bridge.key.pub` to every specified
node's `/home/cuems/.ssh/authorized_keys`. Run as the operator (not as the `cuems` service
user). Uses the operator's own SSH credentials to reach each node.

```
cuems-power-bridge-deploy-keys [--ssh-user USER] [--pubkey PATH] <node>...
```

| Argument | Default | Description |
|---|---|---|
| `<node>...` | (required) | Avahi hostnames to deploy to (e.g. `node01.local node02.local`) |
| `--ssh-user` | current user | Username for the operator's SSH to each node |
| `--pubkey` | `/etc/cuems/power-bridge.key.pub` | Path to the public key file |

**Exit codes:** `0` all succeeded, `1` one or more hosts failed, `2` pubkey file not found
or empty.

---

#### `cuems-power-bridge-install-mjs`

One-shot operator helper. Uploads the bundled `cuems-shutdown.js` (or a custom template) to
a Shelly Pro 1 via its HTTP RPC. Patches `BRIDGE` and `TOKEN` literals inline before upload.
Validates ASCII-only (Shelly's `Script.PutCode` rejects non-ASCII). Removes any pre-existing
`cuems-shutdown` script, enables auto-start on Shelly boot, confirms running.

```
cuems-power-bridge-install-mjs --shelly <URL> --bridge <URL> [--token TOKEN]
                                [--template PATH] [--name NAME]
```

| Option | Default | Description |
|---|---|---|
| `--shelly` | (required) | Shelly base URL, e.g. `http://10.16.8.10` |
| `--bridge` | (required) | Bridge URL the mJS will POST to, e.g. `http://controller.local:8478` |
| `--token` | `""` | `X-Auth-Token` value (must match `shared_token` in `power-bridge.conf`) |
| `--template` | bundled `cuems-shutdown.js` | Path to a custom `.js` template |
| `--name` | `cuems-shutdown` | Shelly script name |

**Exit codes:** `0` success, `1` error (template patch failure, RPC error, ASCII validation
failure).

---

#### `cuems-wsclient` (legacy)

One-shot legacy CLI: loads a project via the editor WebSocket then sends GO via the engine
WebSocket. Kept for backwards compatibility with existing operator scripts.

```
cuems-wsclient [project_id] [--host HOST]
```

| Argument | Default | Description |
|---|---|---|
| `project_id` | read from `/etc/cuems/project_id` | Project UUID |
| `--host` | `master.local` | Controller mDNS name or IP |

[↑ Back to Table of Contents](#table-of-contents)

---

### Configuration reference

Location: `/etc/cuems/power-bridge.conf`  
Format: `key = value  # comment` (one per line; `#` introduces a comment)  
Installed on first `.deb` install from `/usr/share/cuems/power-bridge.conf.default`.  
Loading: package-data defaults first, then system file overrides on top.

#### Shelly endpoint

| Key | Default | Description |
|---|---|---|
| `shelly_url` | `http://192.168.6.2` | Shelly base URL. Use an IP, not `.local` — Shelly's DNS resolver is unreliable |
| `shelly_username` | `""` | Optional HTTP Basic auth username |
| `shelly_password` | `""` | Optional HTTP Basic auth password |
| `shelly_switch_id` | `0` | Shelly switch channel ID |

#### Safety

| Key | Default | Constraints | Description |
|---|---|---|---|
| `refuse_if_running` | `true` | bool | Return 409 on `/shutdown` while engine `running == "yes"` |
| `shutdown_max_wait_s` | `180` | ≥ 30 | Maximum seconds to wait for nodes to go silent before proceeding anyway |
| `shelly_safety_timer_s` | `60` | 45–300 | Seconds after which the Shelly hardware timer cuts mains. Validated at startup |

#### Engine and editor channels

| Key | Default | Description |
|---|---|---|
| `engine_ws_url` | `ws://localhost:9190` | Engine binary-OSC WebSocket (status + GO/STOP) |
| `editor_ws_url` | `ws://localhost:9092` | Editor JSON WebSocket (project_ready for auto-load) |
| `auto_load_project` | `""` | Project UUID to auto-load on boot; empty disables auto-load |
| `auto_load_persistent` | `false` | `false` = once per bridge process (engine restart does **not** retrigger); `true` = re-arm on every observed empty-load |

#### Operational

| Key | Default | Description |
|---|---|---|
| `dry_run` | `false` | Log all side-effects (SSH, Shelly RPC, poweroff) without executing |
| `unresolvable_nodes_policy` | `skip` | Policy for nodes in `network_map.xml` with no `role_id`/`alias`/`hostname`: log ERROR and skip |

#### SSH to nodes

| Key | Default | Description |
|---|---|---|
| `ssh_user` | `cuems` | Remote user for poweroff SSH |
| `ssh_key` | `/etc/cuems/power-bridge.key` | ed25519 private key (generated by `postinst` on first install) |
| `poweroff_cmd` | `sudo /sbin/poweroff` | Command executed on each slave node via SSH, and on the controller if `controller_poweroff_cmd` is empty |
| `controller_poweroff_cmd` | `""` | Optional override for the local controller's shutdown command. Empty → falls back to `poweroff_cmd`. Set to `sudo /usr/bin/systemctl reboot` when controller WoL-from-S5 is unreliable (Realtek r8169 known issue) |

#### HTTP bind

| Key | Default | Description |
|---|---|---|
| `listen_host` | `0.0.0.0` | Bind address |
| `listen_port` | `8478` | TCP port |
| `shared_token` | `""` | Optional `X-Auth-Token` header value; recommended on LAN deployments |

#### Network map

| Key | Default | Description |
|---|---|---|
| `network_map_path` | `/etc/cuems/network_map.xml` | Path to the CUEMS network map XML |

[↑ Back to Table of Contents](#table-of-contents)

---

### Process exit codes

| Code | Meaning |
|---|---|
| `0` | Clean shutdown (SIGTERM, SIGINT, or normal exit) |
| `1` | Fatal startup error (config validation failure, port bind failure, unhandled exception) |

The `cuems-power-bridge-deploy-keys` and `cuems-power-bridge-install-mjs` helpers use
`0` (success), `1` (runtime error), `2` (missing input file).

[↑ Back to Table of Contents](#table-of-contents)

---

## Installation

### Debian package (recommended)

The `cuems-power-bridge` Debian package installs into the shared cuems virtualenv at
`/usr/lib/cuems/` (provided by `cuems-utils`) using `dh-virtualenv`.

**System dependencies** (installed automatically via `debian/control`):

```
cuems-utils (>= 0.1.0rc5)
cuems-common (>= 1.0.0)
python3 (>= 3.11)
python3-systemd (>= 235)
openssh-client
iputils-ping
```

**Build from source on Debian 12 (Bookworm):**

```bash
# Install build dependencies
sudo apt-get install debhelper dh-virtualenv python3-all python3-setuptools python3-pip python3-dev

# Clone and build
git clone https://github.com/stagesoft/cuems-wsclient.git
cd cuems-wsclient
debuild -b -uc -us -nc

# Install
sudo dpkg -i ../cuems-power-bridge_*.deb
```

**Post-install (automatic via `postinst`):**

* A starter `/etc/cuems/power-bridge.conf` is dropped from the package default.
* An ed25519 SSH keypair is generated at `/etc/cuems/power-bridge.key{,.pub}` (no
  passphrase; owned by the `cuems` user).
* systemd is reloaded.

**Enable and start the service:**

```bash
sudo systemctl enable cuems-power-bridge
sudo systemctl start cuems-power-bridge

# Verify
curl http://localhost:8478/status
```

[↑ Back to Table of Contents](#table-of-contents)

---

### Development install

```bash
git clone https://github.com/stagesoft/cuems-wsclient.git
cd cuems-wsclient

# Install dependencies (requires Poetry ≥ 1.7)
poetry install

# Run smoke tests (compile check + import)
python3 -m compileall -q src/

# Run the test suite
poetry run pytest

# Run a single test file
poetry run pytest tests/test_bridge.py -v

# Run with asyncio debug mode
poetry run pytest --asyncio-mode=auto -v
```

[↑ Back to Table of Contents](#table-of-contents)

---

## Usage

### Bootstrap on a fresh cluster

1. Install `cuems-power-bridge` on the **controller** only (nodes do not need this package).
2. Edit `/etc/cuems/power-bridge.conf`:
   ```ini
   shelly_url = http://10.16.8.10        # Shelly IP (not .local)
   shared_token = mysecret               # recommended
   auto_load_project = <uuid>            # optional: project to load on boot
   shelly_safety_timer_s = 60            # 45..300 s
   ```
3. Start the service and verify:
   ```bash
   sudo systemctl start cuems-power-bridge
   curl http://localhost:8478/status
   ```
4. Distribute the bridge's SSH public key to every node:
   ```bash
   cuems-power-bridge-deploy-keys node01.local node02.local node03.local
   # (Run as the operator, using your own SSH credentials)
   ```
5. Verify SSH fan-out access from the controller:
   ```bash
   ssh -i /etc/cuems/power-bridge.key cuems@node01.local sudo /sbin/poweroff --no-wall --dry-run
   ```
6. Install the Shelly mJS (see next section).
7. Configure Bitfocus Companion (see below).

[↑ Back to Table of Contents](#table-of-contents)

---

### Install the Shelly mJS script

Use the bundled installer:

```bash
cuems-power-bridge-install-mjs \
    --shelly http://10.16.8.10 \
    --bridge http://controller.local:8478 \
    --token mysecret
```

Or paste manually: copy `/usr/share/cuems/shelly-mjs/cuems-shutdown.js` into the Shelly's
Scripts tab via its web UI. Edit the two constants at the top:

```js
let BRIDGE = "http://controller.local:8478";   // controller's avahi alias on bond0
let TOKEN  = "REPLACE-ME";                     // must match shared_token in power-bridge.conf
```

**Network note:** Use the Avahi alias of the interface that is on the Shelly's L2 segment
(typically `controller.local`, which maps to the `bond0` interface). If the planned
`bond0` → `formitgo.local` rename ships, update `BRIDGE` accordingly.

**How the Shelly script works:**

The script registers a status handler for `input:0`. When the wired flip-switch transitions
to OFF (`delta.state === false`), the script fires an HTTP POST to `/shutdown`. An `inflight`
boolean debounces the handler; a `Timer.set(10000)` clears it after 10 s in case the HTTP
callback never fires. All bridge response codes (200, 409, 401, 503, 502) are logged to the
Shelly console.

The switch is a **flip-switch, not momentary**. `addStatusHandler` fires only on state
deltas. Leaving the switch in OFF when applying mains is safe — no shutdown fires on boot.
The operator must flip ON, then back OFF, to trigger.

[↑ Back to Table of Contents](#table-of-contents)

---

### Configure Bitfocus Companion

Add three buttons using Companion's **HTTP** module:

| Button | Method | URL | Headers |
|---|---|---|---|
| GO | POST | `http://controller.local:8478/go` | `X-Auth-Token: <token>` |
| STOP | POST | `http://controller.local:8478/stop` | `X-Auth-Token: <token>` |
| SHUTDOWN | POST | `http://controller.local:8478/shutdown` | `X-Auth-Token: <token>` |

The bridge returns `{"ok": true}` or `{"ok": false, "reason": "..."}` JSON that Companion's
HTTP module can use to drive button state feedback (colour/label).

[↑ Back to Table of Contents](#table-of-contents)

---

### Dry-run and smoke testing

Set `dry_run = true` in `/etc/cuems/power-bridge.conf` to run the full state machine
without executing any side-effects. SSH, Shelly RPC, and `systemctl poweroff` calls are
logged at `INFO` level with a `[dry_run]` prefix. The refuse-if-running guard, token check,
reachability poll structure, and Shelly URL validation all run for real.

```bash
# Restart with dry_run enabled
sudo systemctl restart cuems-power-bridge

# Trigger a test shutdown
curl -X POST -H "X-Auth-Token: mysecret" http://localhost:8478/shutdown

# Watch the journal
journalctl -u cuems-power-bridge -f
```

Watch for the `[dry_run] would ssh: ...` and
`[dry_run] would Shelly GetStatus + Set on=true toggle_after=60` lines to confirm the
correct nodes and Shelly URL are resolved before going live.

**Compile-time smoke test (no running daemon needed):**

```bash
python3 -m compileall -q src/
```

[↑ Back to Table of Contents](#table-of-contents)

---

## Development

```bash
# Install dev dependencies (Poetry ≥ 1.7)
poetry install

# Run tests
poetry run pytest
poetry run pytest -v --tb=short       # verbose
poetry run pytest tests/test_config.py # single file

# Smoke compile check
python3 -m compileall -q src/

# Build the Debian package (Debian 12 host, dh-virtualenv installed)
debuild -b -uc -us -nc
```

**Test infrastructure:** `pytest` with `pytest-asyncio` (asyncio mode `auto`) and
`pytest-mock`. Tests live in `tests/` (not yet populated in this release — see
[Future developments](#future-developments)).

**Code style:** no formatter is enforced yet (planned: `black` + `isort` in CI). Follow the
existing style: Google-style docstrings, type annotations on all public functions,
`from __future__ import annotations` in every module.

**SPDX header on all new files:**
```python
# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Your Name <your@email>
```

[↑ Back to Table of Contents](#table-of-contents)

---

## Contributors

Contributions are welcome. Please read [CONTRIBUTORS.md](./CONTRIBUTORS.md) for the full
contributing workflow, including:

* Contribution tiers (trivial vs. non-trivial) and branch naming
* Spec-first requirement for Tier 2 changes
* TDD sequence: failing test → implementation → refactor
* Conventional Commits v1.0 format with DCO sign-off
* PR requirements and acceptance criteria
* Review process: Ion Reguera ([@ibiltari](https://github.com/ibiltari)) or
  Adrià Masip ([@backenv](https://github.com/backenv))

[↑ Back to Table of Contents](#table-of-contents)

---

## Release notes

See [CHANGELOG.md](./CHANGELOG.md) for the full history.

**[Unreleased] — post-0.2.0 Debian revisions (0.2.1 – 0.2.5)**

A series of Debian packaging and operational refinements since the initial 0.2.0 bridge
release. The auto-load race condition that logged a false failure when the editor's
`project_ready` acknowledgement arrived before the engine cache cycle was fixed
(0.2.1). A new optional `controller_poweroff_cmd` config field was added to allow the
controller to reboot instead of powering off, enabling safe end-to-end testing when
controller Wake-on-LAN from S5 is unreliable (Realtek r8169 known issue). `/usr/bin/`
shims were added so operator CLIs are on PATH. The systemd unit was moved from
`cuems-common` into this package. The Shelly mJS template default `BRIDGE` constant was
corrected from a raw bond0 IP to `http://controller.local:8478` (verified: Shelly's mJS
HTTP client resolves `.local` cleanly). A stray em-dash in a mJS comment that tripped
Shelly's ASCII-only `Script.PutCode` validator was replaced with `--`.

**v0.2.0 — 2026-05-27**

First complete release of the `cuems-power-bridge` daemon and the full Debian package. The
bridge provides a single asyncio HTTP server on `:8478` that coordinates orderly cluster
shutdown for a Shelly Pro 1 flip-switch and a Bitfocus Companion Stream Deck. It maintains
two persistent WebSocket connections (engine binary-OSC on `:9190`, editor JSON on `:9092`)
with reconnect-with-backoff, implements a refuse-if-running guard against the engine status
cache, SSH-fans-out `sudo poweroff` to every `NodeType.slave` from `network_map.xml`, polls
reachability until all nodes are silent, arms the Shelly hardware safety timer, and issues
`systemctl poweroff --no-block` on the controller. An auto-load feature configures a project
to load on every boot via the editor's `project_ready` action. All side-effects are
dry-run-capable. The operator tooling (`deploy-keys`, `install-mjs`) is included.

**Pre-v0.2.0 — legacy wsclient era**

The repository originally contained a two-file stand-alone Python client (`wsclient.py`,
`osclistener.py`) for a specific venue deployment. `wsclient.py` connected to the editor and
engine WebSockets to load a project and send GO. `osclistener.py` provided a UDP OSC server
for venue-specific commands (afrucat deployment). These files are retained in the package for
backwards compatibility.

[↑ Back to Table of Contents](#table-of-contents)

---

## Future developments

The following items are planned but not yet implemented. They are documented here so they can
be wired in incrementally.

### Automated test suite

The `tests/` directory, `pytest`, `pytest-asyncio`, and `pytest-mock` are already declared
in `pyproject.toml` and the test runner is configured (`asyncio_mode = "auto"`). The planned
test matrix covers:

* Unit tests for `Config.validate()` (out-of-range values, bad URL schemes)
* Unit tests for `network_map.parse()` (namespace variants, missing fields, missing file)
* Unit tests for `osc_parse.parse_osc_message()` (all supported type tags, truncated frames)
* Async unit tests for `EngineClient` and `EditorClient` using `pytest-mock` to simulate
  WebSocket frames and disconnect events
* Async unit tests for `Bridge._try_auto_load()` covering the race between
  `wait_engine` and `wait_editor_error`
* Async integration test for the full `handle_shutdown` flow with mocked SSH, Shelly RPC,
  and subprocess calls

The `dry_run = true` path provides an integration smoke-test surface on a real cluster
without risk; the unit tests cover the branches that dry-run cannot reach.

### CI/CD pipeline

A GitHub Actions workflow (`tests.yml`) targeting `ubuntu-latest` with Python 3.11:

```yaml
name: Tests
on:
  push:
    branches: [main]
  pull_request:
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: "pip"
      - run: pip install poetry
      - run: poetry install
      - run: poetry run pytest --cov=src --cov-report=xml
      - uses: codecov/codecov-action@v4
        with:
          files: coverage.xml
          fail_ci_if_error: false
        env:
          CODECOV_TOKEN: ${{ secrets.CODECOV_TOKEN }}
```

After the first successful run, activate the repository at
[codecov.io/gh/stagesoft/cuems-wsclient](https://codecov.io/gh/stagesoft/cuems-wsclient)
to enable the coverage badge.

### Documentation site

A MkDocs site (`mkdocs.yml`) following the `cuems-utils` pattern, with a
`gh-pages.yml` workflow deploying to
[stagesoft.github.io/cuems-wsclient](https://stagesoft.github.io/cuems-wsclient/).
Module pages would use `mkdocstrings` to render docstrings for `Bridge`, `Config`,
`EngineClient`, `EditorClient`, `ShellyClient`, `Node`, `SshTarget`, and `PollResult`.

### Packaging and release

* Automate the Python package version bump in `pyproject.toml` and `__init__.py` in
  lockstep with the Debian package revision.
* A `pypi-publish.yml` GitHub Actions workflow to publish to PyPI on tagged releases,
  enabling `pip install cuemswsclient`.
* A signed Debian `.deb` release workflow.

### Target badge set

Once the above pipelines are live, the README badge line will become:

```markdown
[![License: GPL v3](https://img.shields.io/badge/License-GPLv3-blue.svg)](https://www.gnu.org/licenses/gpl-3.0)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org)
[![Tests](https://github.com/stagesoft/cuems-wsclient/actions/workflows/tests.yml/badge.svg)](https://github.com/stagesoft/cuems-wsclient/actions/workflows/tests.yml)
[![Coverage](https://codecov.io/gh/stagesoft/cuems-wsclient/graph/badge.svg)](https://codecov.io/gh/stagesoft/cuems-wsclient)
[![Deploy MkDocs site](https://github.com/stagesoft/cuems-wsclient/actions/workflows/gh-pages.yml/badge.svg)](https://github.com/stagesoft/cuems-wsclient/actions/workflows/gh-pages.yml)
[![Upload Python Package](https://github.com/stagesoft/cuems-wsclient/actions/workflows/pypi-publish.yml/badge.svg)](https://github.com/stagesoft/cuems-wsclient/actions/workflows/pypi-publish.yml)
```

[↑ Back to Table of Contents](#table-of-contents)

---

## Copyright notice

```
cuems-wsclient
Copyright (C) 2026  Stagelab Coop SCCL

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program. If not, see <https://www.gnu.org/licenses/>.
```

[↑ Back to Table of Contents](#table-of-contents)

---

## License

`cuems-wsclient` is free software, distributed under the
[GNU General Public License v3.0 or later](https://www.gnu.org/licenses/gpl-3.0.html)
(`GPL-3.0-or-later`).

You are free to use, modify, and distribute this software under the terms of the GPL v3.
Any derivative work distributed to others must also be licensed under the GPL v3 or later
and must make its source code available.

The full licence text is available at
[https://www.gnu.org/licenses/gpl-3.0.html](https://www.gnu.org/licenses/gpl-3.0.html).

[↑ Back to Table of Contents](#table-of-contents)
