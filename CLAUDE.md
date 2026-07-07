# cuems-power-bridge

Part of the **CUEMS** ecosystem — see the [`cuems-RELATIONS`](https://github.com/stagesoft/cuems-RELATIONS) repo for the system index, architecture diagram, and protocol/port map.

## Role

**Controller-only** power + showcontrol bridge: an asyncio HTTP coordinator on `:8478` fronting orderly cluster shutdown and GO/STOP. Triggered by a wired **Shelly Pro 1** flip-switch or a **Bitfocus Companion** Stream Deck. Holds persistent WebSockets to the engine (`:9190`, binary OSC — status cache + GO/STOP) and editor (`:9092`, JSON — `project_ready` boot auto-load). Python 3.11+, dh-virtualenv into the shared venv `/usr/lib/cuems/`. Service `cuems-power-bridge.service` (the unit is shipped by **cuems-common**), config `/etc/cuems/power-bridge.conf`. `dry_run=true` logs SSH/Shelly/poweroff instead of executing. **Commits are GPG-signed** (retry on "gpg failed to sign", never `--no-gpg-sign`).

**Renamed from cuems-wsclient** (2026-06, v0.2.6 — complete). Import package `cuemspowerbridge` (`src/cuemspowerbridge/`), Debian source+binary `cuems-power-bridge`, repo `stagesoft/cuems-power-bridge`. The **only** intentional legacy survivor is the backwards-compat console entry `cuems-wsclient` → `cuemspowerbridge.wsclient:main_cli` (the impl file is still `wsclient.py`). Notes pointing at `src/cuemswsclient/...` are stale → `src/cuemspowerbridge/...`.

## HTTP contract (`:8478`, JSON `{"ok": bool, "reason"?: "<token>"}`)

- `GET /status` → `state`, `engine_state`, `nodes_pending`, `shelly_timer_armed_s`, `last_error` (+ brightness/display keys).
- `POST /go` → `/engine/command/go` (409 `not_armed` if the engine isn't armed).
- `POST /stop` → `/engine/command/stop`.
- `POST /shutdown[?force=1]` → orderly shutdown. Refuses 409 `project_running` unless `force=1` or `refuse_if_running=false` (**consistent with the project-wide "never auto-stop a running project" rule**; a LOADED-but-not-playing engine is NOT "running"). Other codes: 409 `shutdown_already_in_progress`, 503 `engine_state_unknown`, 502 `shelly_unreachable`, 401 `bad_token`.
- `POST /setnextcue` / `POST /gocue` — cue triggering (shipped in 0.3.0-5).
- `POST /brightness?level=<name>` — projector brightness presets via Epson ESC/VP21 over ESC/VP.net (TCP 3629); 200 all / 207 partial / 502 all-failed / 400 unknown_level / 503 none.

All endpoints validate the optional `X-Auth-Token` header against `power-bridge.conf:shared_token` (`/status` is unauthenticated).

## Shutdown flow (fail-safe ordering)

asyncio-lock (concurrent calls → 409) → token + refuse-if-running guard → parse `network_map.xml` for every `NodeType.slave` avahi hostname (`role_id.local` / `alias.local` / `hostname.local`, **never** raw `<ip>`) → parallel `ssh cuems-admin@<host> sudo /sbin/poweroff` (fire-and-forget) → reachability poll (ICMP + TCP/22) until silent or `shutdown_max_wait_s` → Shelly `Switch.GetStatus` pre-check (abort if already off) → arm Shelly hardware safety timer `Switch.Set {on:true, toggle_after: shelly_safety_timer_s}` → local `sudo systemctl poweroff --no-block` → Shelly cuts mains on the already-off box. **Fail-safe:** if the Shelly RPC fails after 3 retries the bridge does NOT poweroff locally (returns 502) — staying up beats powering off without a confirmed mains-cut deadline.

## Boot auto-load

If `auto_load_project=<uuid>` is set, the bridge sends the editor `project_ready` when the engine reports an empty load state. `auto_load_persistent=false` (default) fires once per bridge process; `=true` on every observed empty-load. 3 consecutive failures disable auto-load for the session. `auto_load_node_settle_s` gates how long after seeing nodes on the hub the first `project_ready` fires — set it **above node player-registration time** (nodes register players ~28s after hub-join; a too-early settle arms only the controller and the load stalls on the engine's 120s arm watchdog). Proven config: `settle=45`, `armed_timeout=125`.

## SSH node poweroff

The poweroff SSH goes through the **`cuems-admin`** operator account, NOT the `cuems` service account (`cuems` is `/usr/sbin/nologin`). The dedicated bridge key `/etc/cuems/power-bridge.key` is written **locked** in each node's authorized_keys: `restrict,command="sudo /sbin/poweroff" ...` — a leaked key can do nothing but poweroff. Sudoers drop-in (cuems-common): `cuems-admin ALL=(root) NOPASSWD: /sbin/poweroff`. Distribute keys with `cuems-power-bridge-deploy-keys node01.local …` (run as the operator, writes the node's own authorized_keys with plain tee — no sudo, resolves `$HOME` remotely). NB: the ssh returns rc=255 as the node goes down — expected; success is judged by the reachability poll, not ssh rc.

## Shelly mJS

`/usr/share/cuems/shelly-mjs/cuems-shutdown.js` (`cuems-power-bridge-install-mjs`) is pasted into the Shelly Scripts tab; reacts to SW0 → OFF and POSTs `/shutdown`. The SW0 input is a **flip-switch** (not momentary) — `addStatusHandler` fires only on deltas. The mJS must be **ASCII-only** (`Script.PutCode` rejects non-ASCII, -103) and uses `controller.local`. `shelly_url` in the *bridge* config, by contrast, must be an **IP** (Shelly mDNS is unreliable). Shelly gotchas (learned wiring this):

- **`HTTP.POST` silently drops a custom `headers` map** — it only honors `content_type`, so `X-Auth-Token` never goes out and every `/shutdown` 401s. Use `Shelly.call("HTTP.Request", {method:"POST", url, body, headers:{...}})` — the only Shelly HTTP call that transmits arbitrary headers.
- **KVS is not a JS global** — call `Shelly.call("KVS.Set", ...)`.
- **A standalone virtual component is not clickable on Home** (fw 1.7.5). To get a clickable control, create a **boolean toggle** (`view:"toggle"`) **inside a group** — the group tile renders it as a flippable switch. The badajoz web-UI shutdown is a `boolean:200` in `group:200` → flip ON → mJS catches the delta, springs it back, POSTs `/shutdown?force=1`. `FORCE` constant default `true` (hardware kill even mid-show); installer `--safe` patches `FORCE=false`.
- **`in_mode` must be `detached`** (not `follow`, which cuts power instantly on SW0 OFF) with `initial_state=restore_last`; `match_input` is an invalid combo with detached (-103). The SW0→ON branch must re-close the relay (`Switch.Set {on:true}`) so "flip up → boot" works.

## Field notes / gotchas

- **Upgrading power-bridge can silently break the ENGINE via the shared venv.** Old `.deb`s (0.3.0-1) **bundled** `python-osc` into `/usr/lib/cuems/.../site-packages`; the engine and the bridge both depend on it, but on hosts where the engine runs *editable from source* that dep was never installed into the venv — the bridge's bundle was the only copy. A newer build whose `debian/rules` **strips** pythonosc removes those files on `dpkg -i` → `ModuleNotFoundError: pythonosc.osc_message_builder` → bridge crash-loops AND the engine's OSC breaks on its next restart. Fix: restore pythonosc into the venv as **non-dpkg-owned** files (extract from the old `.deb`, `sudo cp` back). Check for this whenever bumping power-bridge. General rule: the bridge must not bundle anything another cuems package ships (websockets from cuems-utils, pythonosc from cuems-engine were both latent collisions) — `debian/rules override_dh_fixperms` strips them + `export PYTHONNOUSERSITE=1` to block `~/.local` pollution. Unique-to-bridge deps that MUST stay bundled: the aiohttp stack + typing_extensions.
- **Build on the box** (`dpkg-buildpackage -b -uc -us`; `.deb` lands in the **parent** dir). `dpkg -i` needs the operator (dpkg is not NOPASSWD on hardened controllers). Pre-flight the target for venv file collisions before installing (`dpkg-deb -c | ... dpkg -S`). Live hotfixes: the venv `.py` files are writable and `sudo cp` is NOPASSWD, but that yields an untracked hybrid — prefer the `.deb`.
- **Autoload vs node-boot race**: in a WoL recovery where the controller boots alone, autoload fires before nodes are up → engine load stalls after 120s → engine goes `project_status=none` but keeps broadcasting a stale "loaded" over OSC. A plain bridge restart then skips autoload. Recovery: `stop cuems-power-bridge; restart both engines; start cuems-power-bridge`.
- **Review against the LIVE Shelly script (`Script.GetCode`) and current upstream**, not a local checkout — the casas checkout was once 23 commits behind and still on the old name.

## Future migration

The SSH-fanout poweroff is intended to be replaced by an engine-native `/engine/command/shutdown` broadcasting COMMAND/SHUTDOWN over the existing NNG bus; the external contract (HTTP shape, auth, Shelly RPC, mJS, Companion) stays the same — only `_shutdown_nodes()` is rewritten. (The original design plan `we-need-shelly-pro-jolly-chipmunk.md` is historical and no longer on disk.)
