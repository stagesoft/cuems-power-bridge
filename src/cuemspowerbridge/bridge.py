# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""HTTP server + shutdown coordinator. Single coordinator for both
Shelly mJS and Bitfocus Companion. See plan:
~/.claude/plans/we-need-shelly-pro-jolly-chipmunk.md
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from . import cluster_bus, network_map
from .config import Config
from .displays.manager import DisplayManager
from .editor_client import EditorClient
from .engine_state import UNKNOWN, EngineClient
from .node_executor import SshTarget, poweroff_all
from .reachability import wait_until_all_down
from .shelly import ShellyClient, ShellyError

log = logging.getLogger(__name__)

# Suppress duplicate projector power-on spawns within this window. A re-drive
# of auto-load can briefly flip the engine's `load` empty→non-empty again,
# re-firing the load edge-detector; this debounces those so a loaded project
# powers projectors on at most once per window (see _on_engine_status).
_PROJECTOR_ON_DEBOUNCE_S = 30.0

# /status state machine
STATES = (
    "idle", "checking", "polling", "arming-shelly", "poweroff-issued",
    "done", "failed",
)

# Auto-play: after a successful auto-load, wait for the engine to arm (load->arm
# is asynchronous) before sending GO. Polled at _AUTO_PLAY_POLL_S for at most
# _AUTO_PLAY_ARM_POLLS iterations. Module-level so tests can shrink them.
_AUTO_PLAY_POLL_S = 0.5
_AUTO_PLAY_ARM_POLLS = 60  # 60 * 0.5s = 30s


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _RateLimiter:
    """Per-endpoint min-interval gate (default 200 ms)."""

    def __init__(self, min_interval_s: float = 0.2):
        self.min_interval = min_interval_s
        self._last: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        if now - last < self.min_interval:
            return False
        self._last[key] = now
        return True


class Bridge:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.engine = EngineClient(cfg.engine_ws_url)
        self.editor = EditorClient(cfg.editor_ws_url)
        self.shelly = ShellyClient(
            base_url=cfg.shelly_url,
            switch_id=cfg.shelly_switch_id,
            username=cfg.shelly_username,
            password=cfg.shelly_password,
        )
        self.displays = DisplayManager.from_config(cfg)
        # Projector power-on is fired (fire-and-forget) when the engine
        # reports a project loaded; we keep the task handle to cancel it on
        # stop() and to suppress duplicate spawns on engine reconnects.
        self._projector_on_task: asyncio.Task | None = None
        # Auto-play GO task, spawned after a successful auto-load (fire-and-
        # forget); handle kept to dedup and to cancel on stop()/shutdown.
        self._auto_play_task: asyncio.Task | None = None
        self._project_loaded_seen = False
        self._last_projector_on_monotonic = 0.0
        self._shutdown_lock = asyncio.Lock()
        # Auto-load state.
        self._auto_load_task: asyncio.Task | None = None
        self._auto_load_done = False
        self._auto_load_failures = 0       # HARD (editor error) strikes → disable at 3
        self._auto_load_soft_attempts = 0  # SOFT (loaded-but-not-armed) retries; never disable
        self._auto_load_disabled = False
        self._auto_load_unix_name: str | None = None  # our own driven project's load name
        self._auto_load_ids_warned = False
        self._autoload_pending: list[str] = []  # role_ids still off the bus (degraded)
        # network_map.xml parse cache, keyed on (path, mtime): retries reuse it,
        # but an operator editing a stale <ip> mid-recovery bumps mtime → reparse.
        self._slave_ips_cache: list[tuple[str, str]] | None = None
        self._slave_ips_cache_key: tuple[str, float | None] | None = None
        self._state = "idle"
        self._state_since = _now()
        self._nodes_pending: list[str] = []
        self._last_error: str | None = None
        self._rate = _RateLimiter()

    # ------------------- state machine -------------------

    def _set_state(self, state: str, error: str | None = None) -> None:
        if state not in STATES:
            log.warning("unknown state requested: %s", state)
            return
        log.info("state: %s → %s", self._state, state)
        self._state = state
        self._state_since = _now()
        if error is not None:
            self._last_error = error

    def _status_payload(self) -> dict:
        eng = "unknown"
        if self.engine.is_known():
            if self.engine.project_running():
                eng = "running"
            elif self.engine.project_loaded():
                eng = "loaded"
            else:
                eng = "idle"
        return {
            "state": self._state,
            "since": self._state_since,
            "engine_state": eng,
            "nodes_pending": list(self._nodes_pending),
            # Auto-load bus-wait: role_ids whose node-engine is not yet on the
            # NNG hub. Distinct from shutdown's `nodes_pending` (avahi names).
            "autoload_pending": list(self._autoload_pending),
            "auto_load_armed": self.engine.armed,
            "shelly_timer_armed_s": self.cfg.shelly_safety_timer_s,
            "last_error": self._last_error,
            "displays": self.displays.snapshot(),
        }

    # ------------------- HTTP handlers -------------------

    def _check_token(self, request: web.Request) -> bool:
        if not self.cfg.shared_token:
            return True
        return request.headers.get("X-Auth-Token", "") == self.cfg.shared_token

    @staticmethod
    def _err(reason: str, status: int) -> web.Response:
        return web.json_response({"ok": False, "reason": reason}, status=status)

    @staticmethod
    def _ok(extra: dict | None = None) -> web.Response:
        body = {"ok": True}
        if extra:
            body.update(extra)
        return web.json_response(body)

    async def handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self._status_payload())

    async def handle_go(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return self._err("bad_token", 401)
        if not self._rate.allow("go"):
            return self._err("rate_limited", 429)
        if not self.engine.is_known():
            return self._err("engine_state_unknown", 503)
        if self.engine.armed != "yes":
            return self._err("not_armed", 409)
        sent = await self.engine.send_osc("/engine/command/go")
        if not sent:
            return self._err("engine_send_failed", 502)
        log.info("GO forwarded to engine")
        return self._ok()

    async def handle_stop(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return self._err("bad_token", 401)
        if not self._rate.allow("stop"):
            return self._err("rate_limited", 429)
        if not self.engine.is_known():
            return self._err("engine_state_unknown", 503)
        sent = await self.engine.send_osc("/engine/command/stop")
        if not sent:
            return self._err("engine_send_failed", 502)
        log.info("STOP forwarded to engine")
        return self._ok()

    async def handle_poweron(self, request: web.Request) -> web.Response:
        """Power displays ON on demand — symmetric with /shutdown's
        projectors-off. Fire-and-forget: schedules the shared power-on task
        and returns immediately (200 = accepted), so a slow/unreachable
        projector can't hold the HTTP response open (Shelly/Companion expect a
        quick reply). The `_projector_on_task` guard in _spawn_projector_power_on
        dedups concurrent calls, so no separate lock is needed."""
        if not self._check_token(request):
            return self._err("bad_token", 401)
        if not self._rate.allow("poweron"):
            return self._err("rate_limited", 429)
        if not self.displays.configured:
            return self._err("no_displays", 503)
        self._spawn_projector_power_on("/poweron request")
        return self._ok()

    async def handle_shutdown(self, request: web.Request) -> web.Response:
        if not self._check_token(request):
            return self._err("bad_token", 401)
        if self._shutdown_lock.locked():
            return self._err("shutdown_already_in_progress", 409)
        force = request.query.get("force") == "1"
        async with self._shutdown_lock:
            source = request.headers.get("User-Agent", "unknown")
            log.info("shutdown triggered (force=%s, source=%s)", force, source)
            self._set_state("checking")
            if not force and self.cfg.refuse_if_running:
                if not self.engine.is_known():
                    self._set_state("idle", error="engine_state_unknown")
                    return self._err("engine_state_unknown", 503)
                if self.engine.project_running():
                    log.info("refuse_if_running: project running, 409")
                    self._set_state("idle", error="project_running")
                    return self._err("project_running", 409)
            try:
                await self._run_shutdown()
                # If _run_shutdown returned without raising, poweroff was
                # issued. Status stays at "poweroff-issued" until the
                # process gets SIGTERM'd by systemd.
                return self._ok()
            except ShellyError as e:
                # Abort path: do NOT poweroff controller. Mains stay on.
                log.error("shutdown ABORTED: Shelly RPC failed: %s", e)
                self._set_state("failed", error=f"shelly: {e}")
                self._nodes_pending.clear()
                return self._err("shelly_unreachable", 502)
            except Exception as e:
                log.exception("shutdown failed unexpectedly")
                self._set_state("failed", error=str(e))
                return self._err("internal_error", 500)

    # ------------------- shutdown coordinator -------------------

    async def _run_shutdown(self) -> None:
        # Step 4 (unload) intentionally omitted: engine WS dispatcher has
        # no /engine/command/unload handler. See plan.

        # Step 5: build node target list.
        resolved, unresolvable = network_map.slave_avahi_names(
            self.cfg.network_map_path
        )
        for n in unresolvable:
            log.error(
                "network_map: node uuid=%s has no role_id/alias/hostname; "
                "skipping (it will not poweroff cleanly)", n.uuid,
            )
        self._nodes_pending = list(resolved)
        log.info("shutdown: %d nodes to power off: %s",
                 len(resolved), ", ".join(resolved) if resolved else "(none)")

        # Step 6: SSH-fanout poweroff.
        targets = [
            SshTarget(
                host=h,
                user=self.cfg.ssh_user,
                key_path=self.cfg.ssh_key,
                poweroff_cmd=self.cfg.poweroff_cmd,
            )
            for h in resolved
        ]
        await poweroff_all(targets, dry_run=self.cfg.dry_run)

        # Step 6b: power off projectors/displays. First cancel any in-flight
        # power-on so POWR 1 can't race our POWR 0 to the same device; then
        # run the power-off CONCURRENTLY with the reachability poll below, so
        # a slow/unreachable projector never adds latency to the
        # safety-critical sequence. power_off_all() isolates per-device errors.
        # Also cancel any pending auto-play so a GO can't fire mid-shutdown
        # (this path does NOT go through stop(); it cancels tasks directly).
        await self._cancel_auto_play_task()
        await self._cancel_projector_on_task()
        projector_off_task: asyncio.Task | None = None
        if self.cfg.projector_power_off_on_shutdown and self.displays.configured:
            projector_off_task = asyncio.create_task(
                self.displays.power_off_all(), name="projector-power-off"
            )

        # Step 7: reachability poll (projectors power off in parallel).
        if resolved:
            self._set_state("polling")
            result = await wait_until_all_down(
                resolved,
                interval_s=2.0,
                max_wait_s=self.cfg.shutdown_max_wait_s,
            )
            self._nodes_pending = list(result.stuck_hosts)
            if result.timed_out:
                log.warning(
                    "shutdown: reachability timeout (%.1fs), proceeding anyway "
                    "with stuck hosts: %s", result.elapsed_s,
                    ", ".join(result.stuck_hosts),
                )

        # Join the projector power-off, bounded so a hung projector cannot
        # stall the sequence (cap = its full retry budget + margin).
        if projector_off_task is not None:
            budget = 3 * self.cfg.projector_command_timeout_s + 5
            try:
                await asyncio.wait_for(projector_off_task, timeout=budget)
            except asyncio.TimeoutError:
                log.warning("projector power-off exceeded %.0fs; continuing shutdown",
                            budget)
            except Exception:
                log.exception("projector power-off failed; continuing shutdown")

        # Step 8: arm Shelly hardware safety timer.
        self._set_state("arming-shelly")
        if self.cfg.dry_run:
            log.info(
                "[dry_run] would Shelly GetStatus + Set on=true toggle_after=%d",
                self.cfg.shelly_safety_timer_s,
            )
        else:
            status = await self.shelly.get_status()
            output = status.get("output", True)
            if output is False:
                # Physically impossible while bridge is running off this Shelly.
                # Pre-existing fault: don't proceed.
                raise ShellyError("Shelly reports output=false; pre-existing fault")
            await self.shelly.arm_timer(self.cfg.shelly_safety_timer_s)
            log.info(
                "Shelly armed: relay opens in %d s",
                self.cfg.shelly_safety_timer_s,
            )

        # Step 10: local controller poweroff/reboot.
        # Use controller_poweroff_cmd if set (e.g. "sudo systemctl reboot"
        # for safe testing when WoL-from-S5 is unreliable), otherwise fall
        # back to poweroff_cmd (the same command used to SSH-poweroff nodes).
        self._set_state("poweroff-issued")
        local_cmd_str = self.cfg.controller_poweroff_cmd.strip() or self.cfg.poweroff_cmd
        cmd = local_cmd_str.split()
        # --no-block lets us flip /status to done before systemd reaps us.
        if "systemctl" in local_cmd_str and "--no-block" not in cmd:
            cmd = cmd + ["--no-block"]
        if self.cfg.dry_run:
            log.info("[dry_run] would exec local poweroff: %s",
                     " ".join(shlex.quote(c) for c in cmd))
            self._set_state("done")
            return
        log.info("issuing local poweroff: %s", " ".join(cmd))
        try:
            await asyncio.create_subprocess_exec(*cmd)
        except FileNotFoundError as e:
            log.error("poweroff cmd not found: %s", e)
            raise

    # ------------------- auto-load -------------------

    async def _auto_load_loop(self) -> None:
        """Watch the engine cache and drive auto-load to a fully-ready state.

        Self-serialized: the single ``await _try_auto_load()`` runs to
        completion before the next tick, so no in-flight flag is needed. The
        loop is cancellable (``stop()`` cancels the task) — it only ever
        blocks inside ``asyncio.sleep``/awaited probes.
        """
        if not self.cfg.auto_load_project:
            return
        delay = 2.0
        while True:
            await asyncio.sleep(delay)
            delay = 2.0
            if self._auto_load_disabled:
                return
            # Never act while the engine WS is down / armed is unknown.
            if not self.engine.is_known():
                continue
            # Already loaded AND armed → nothing to do.
            if self.engine.fully_ready():
                continue
            # Re-drive guard: don't clobber an operator's DIFFERENT load. We
            # only ever record `_auto_load_unix_name` on our own fully-ready
            # success, so a non-empty `load` that differs from it means an
            # operator loaded something else.
            if self._operator_clobber():
                log.warning(
                    "auto-load: engine.load=%r differs from our driven "
                    "project %r — operator loaded a different project; "
                    "backing off (disabled for session)",
                    self.engine.load, self._auto_load_unix_name,
                )
                self._auto_load_disabled = True
                return
            # Once-only mode: we already succeeded and operator likely
            # unloaded intentionally — don't re-drive.
            if self._auto_load_done and not self.cfg.auto_load_persistent:
                continue
            try:
                outcome = await self._try_auto_load()
            except asyncio.CancelledError:
                raise  # stop() cancelled us — propagate cleanly
            except Exception:
                # An attempt must never kill the loop: log and retry next tick
                # (the loop is the self-heal mechanism). Treat as a soft miss.
                log.exception("auto-load: attempt raised; retrying next tick")
                outcome = "not_armed"
            if outcome == "not_armed":
                self._auto_load_soft_attempts += 1
                if self._auto_load_soft_attempts >= self.cfg.auto_load_max_attempts:
                    log.error(
                        "auto-load: %d soft attempts without armed==yes; "
                        "slowing to 30 s self-heal cadence (will keep "
                        "retrying, never disables)",
                        self._auto_load_soft_attempts,
                    )
                    delay = 30.0

    def _operator_clobber(self) -> bool:
        """True iff a DIFFERENT project than the one we drove is loaded.

        Caller must have already passed ``is_known()``. Returns False during
        our own soft-retry window (``_auto_load_unix_name`` is None until a
        fully-ready success), so a loaded-but-not-yet-armed *own* project is
        never mistaken for an operator clobber."""
        load = self.engine.load
        return bool(
            self._auto_load_unix_name
            and load not in ("", UNKNOWN)
            and load != self._auto_load_unix_name
        )

    def _slave_ips_cached(self, path: str) -> list[tuple[str, str]]:
        """network_map.slave_ips(path) with an mtime-keyed cache.

        Runs in a worker thread (run_in_executor); only the serialized
        auto-load loop ever touches the cache attrs, so no locking is needed.
        A changed mtime (operator edited network_map.xml mid-recovery) forces a
        reparse; otherwise repeated retries reuse the prior result."""
        try:
            mtime: float | None = os.stat(path).st_mtime
        except OSError:
            mtime = None
        key = (path, mtime)
        if self._slave_ips_cache is not None and self._slave_ips_cache_key == key:
            return self._slave_ips_cache
        result = network_map.slave_ips(path)
        self._slave_ips_cache = result
        self._slave_ips_cache_key = key
        return result

    async def _expected_node_ips(self) -> list[tuple[str, str]]:
        """Resolve (ip, role_id) of the node-engines we must wait for.

        Default = every adopted slave with an ``<ip>``. If
        ``auto_load_node_ids`` is set, restrict to that subset (warn once on
        any id not present in the map — validate() can't see network_map)."""
        loop = asyncio.get_event_loop()
        slaves = await loop.run_in_executor(
            None, self._slave_ips_cached, self.cfg.network_map_path
        )
        wanted = self.cfg.node_ids_list()
        if not wanted:
            return slaves
        by_id = {label: ip for ip, label in slaves}
        result: list[tuple[str, str]] = []
        for rid in wanted:
            if rid in by_id:
                result.append((by_id[rid], rid))
            elif not self._auto_load_ids_warned:
                log.warning("auto-load: auto_load_node_ids id %r not found in "
                            "network_map (no <ip> to wait for)", rid)
        self._auto_load_ids_warned = True
        return result

    async def _try_auto_load(self) -> str:
        """One auto-load attempt. Returns a classification:
        "armed" | "editor_error" | "not_armed" | "skipped".
        """
        uuid = self.cfg.auto_load_project

        # (1) Resolve expected node IPs and (2) wait for them on the NNG hub.
        expected = await self._expected_node_ips()
        id_by_ip = {ip: label for ip, label in expected}
        expected_ips = set(id_by_ip)
        if self.cfg.auto_load_wait_nodes and expected_ips:
            log.info("auto-load: waiting for %d node-engine(s) on the NNG hub "
                     "(:%d): %s", len(expected_ips), self.cfg.nng_hub_port,
                     ", ".join(sorted(id_by_ip.values())))
            self._autoload_pending = sorted(id_by_ip.values())
            res = await cluster_bus.wait_until_engines_on_bus(
                expected_ips, self.cfg.nng_hub_port,
                interval_s=3.0, max_wait_s=self.cfg.auto_load_node_timeout_s,
                wait_all=not self.cfg.node_ids_list(),
                id_by_ip=id_by_ip,
            )
            if res.timed_out:
                pending = [id_by_ip.get(ip, ip) for ip in res.stuck]
                self._autoload_pending = pending
                log.warning(
                    "auto-load: DEGRADED after %.0fs — node-engine(s) never "
                    "joined the bus: %s; proceeding anyway",
                    res.elapsed_s, ", ".join(pending),
                )
            else:
                self._autoload_pending = []
            # (3) Small settle margin after bus-connect before loading.
            await asyncio.sleep(self.cfg.auto_load_node_settle_s)
        else:
            self._autoload_pending = []

        # (4) Fire project_ready.
        if not self.editor.connected:
            log.debug("auto-load: editor not connected, skipping this round")
            return "skipped"
        log.info("auto-load: sending project_ready %s", uuid)
        sent = await self.editor.send_action("project_ready", uuid)
        if not sent:
            log.warning("auto-load: send failed")
            return "skipped"

        # (5) Confirm armed==yes (or fail-fast on an editor error).
        outcome = await self._await_armed_or_error(uuid)

        # (6) Classify.
        if outcome == "armed":
            self._auto_load_done = True
            self._auto_load_unix_name = self.engine.load
            self._auto_load_failures = 0
            self._auto_load_soft_attempts = 0
            self._autoload_pending = []
            log.info("auto-load: project loaded AND armed (uuid=%s, load=%r)",
                     uuid, self.engine.load)
            # Optionally auto-start playback (GO). The project is already armed
            # here, so _maybe_auto_play sends GO promptly (it still re-checks
            # armed and skips if already running).
            self._maybe_auto_play()
            return "armed"
        if outcome == "editor_error":
            self._auto_load_failures += 1
            log.error("auto-load: editor returned error for uuid=%s "
                      "(hard %d/3)", uuid, self._auto_load_failures)
            if self._auto_load_failures >= 3:
                log.error("auto-load: 3 editor errors, disabling for session")
                self._auto_load_disabled = True
            return "editor_error"
        # not_armed (soft — loop retries; never disables)
        log.warning("auto-load: project loaded but armed!=yes within %ds "
                    "(soft retry)", self.cfg.auto_load_armed_timeout_s)
        return "not_armed"

    async def _await_armed_or_error(self, uuid: str) -> str:
        """Wait for ``fully_ready()`` (armed==yes) or an editor error.

        Polls the engine cache every 0.5 s up to ``auto_load_armed_timeout_s``.
        A single ``wait_for_response("project_ready")`` watches for a fail-fast
        editor error in parallel; an editor *success* (or timeout) is ignored
        — the engine `armed` cache is the authoritative readiness signal.
        Returns "armed" | "editor_error" | "not_armed".
        """
        timeout = self.cfg.auto_load_armed_timeout_s
        err_task = asyncio.create_task(self._wait_editor_error(uuid, timeout))

        def editor_errored() -> bool:
            # Exception-safe: a crashed/cancelled err_task must NOT re-raise
            # out of the poll (that would abort the whole attempt). Treat any
            # non-clean completion as "no editor error observed".
            if not err_task.done() or err_task.cancelled():
                return False
            if err_task.exception() is not None:
                log.warning("auto-load: editor-error watcher raised: %s",
                            err_task.exception())
                return False
            return bool(err_task.result())

        try:
            loops = max(1, int(timeout / 0.5))
            for _ in range(loops):
                await asyncio.sleep(0.5)
                # Record the unix_name our drive produced as soon as a project
                # loads — even before it arms — so the re-drive guard can spot
                # an operator loading a DIFFERENT project during the soft-retry
                # window (before we ever reach armed==yes).
                if self._auto_load_unix_name is None and self.engine.project_loaded():
                    self._auto_load_unix_name = self.engine.load
                if self.engine.fully_ready():
                    return "armed"
                if editor_errored():
                    return "editor_error"
            if self.engine.fully_ready():
                return "armed"
            if editor_errored():
                return "editor_error"
            return "not_armed"
        finally:
            if not err_task.done():
                err_task.cancel()
                try:
                    await err_task
                except asyncio.CancelledError:
                    pass

    async def _wait_editor_error(self, uuid: str, timeout: float) -> bool:
        """True iff the editor returns an error frame for project_ready."""
        resp = await self.editor.wait_for_response("project_ready", timeout=timeout)
        return bool(resp and resp.get("type") == "error")

    # ------------------- projector power-on -------------------

    def _on_engine_status(self, key: str, value: Any) -> None:
        """Engine status listener: power displays ON when a project loads.

        Called synchronously from the engine WS read loop for EVERY
        /engine/status/* key, so it must stay fast and not block. We act
        only on the `load` key, and read the COERCED cache (self.engine.load,
        set before listeners fire) rather than the raw `value` arg — `value`
        is None for empty/impulse frames.
        """
        if key != "load":
            return
        loaded = self.engine.project_loaded()
        if loaded and not self._project_loaded_seen:
            self._project_loaded_seen = True
            # Debounce: an auto-load re-drive can flip load empty→non-empty
            # repeatedly; only actually (re)spawn power-on if we haven't done
            # so within the debounce window. The spawn helper also guards
            # against a concurrent in-flight task.
            now = time.monotonic()
            if now - self._last_projector_on_monotonic >= _PROJECTOR_ON_DEBOUNCE_S:
                self._last_projector_on_monotonic = now
                self._spawn_projector_power_on()
            else:
                log.debug("projector power-on debounced (load re-flipped "
                          "within %.0fs)", _PROJECTOR_ON_DEBOUNCE_S)
        elif not loaded:
            # Back to no project — re-arm so the next load powers on again.
            self._project_loaded_seen = False

    def _spawn_projector_power_on(self, reason: str = "project loaded") -> None:
        if self._projector_on_task is not None and not self._projector_on_task.done():
            log.debug("projector power-on already in progress; skipping duplicate")
            return
        log.info("%s → powering on displays", reason)
        self._projector_on_task = asyncio.create_task(
            self.displays.power_on_all(), name="projector-power-on"
        )

    def _maybe_power_on_at_start(self) -> None:
        """Power displays ON at bridge startup, independent of project load.

        Gated only by `projector_power_on_on_start` (NOT by
        `projector_power_on_on_load`) so the two paths are independent: with
        on_load=False, on_start=True the fleet still powers on at boot. Spawns
        the shared fire-and-forget task, so it never blocks startup and the
        `_projector_on_task` guard dedups against a near-simultaneous on-load
        power-on. Does not touch `_project_loaded_seen` (the on-load
        edge-detector stays independent; a later real load is a harmless,
        idempotent POWR 1 if the projector is already on)."""
        if self.cfg.projector_power_on_on_start and self.displays.configured:
            self._spawn_projector_power_on("startup")

    async def _cancel_projector_on_task(self) -> None:
        """Cancel any in-flight power-on task and wait for it to unwind.

        Used on stop() and at the start of shutdown so a power-on in
        progress can't race the power-off (POWR 1 vs POWR 0 to the same
        device). Suppresses the cancelled task's CancelledError; logs any
        real error.
        """
        t = self._projector_on_task
        self._projector_on_task = None
        if t is None or t.done():
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("projector power-on task errored during cancel")

    # ------------------- auto-play -------------------

    def _maybe_auto_play(self) -> None:
        """After a successful auto-load, optionally auto-start playback.

        Gated by cfg.auto_play. Fire-and-forget; the task handle dedups so a
        re-driven auto-load (auto_load_persistent) can't stack GO waits.
        """
        if not self.cfg.auto_play:
            return
        if self._auto_play_task is not None and not self._auto_play_task.done():
            log.debug("auto-play already in flight; skipping duplicate")
            return
        self._auto_play_task = asyncio.create_task(
            self._auto_play_after_load(), name="auto-play"
        )

    async def _auto_play_after_load(self) -> None:
        """Wait (bounded) for the engine to arm, then send GO once.

        Never sends GO if the project is already running (so a re-driven
        auto-load while playing is a no-op). A failed send (engine dropped
        mid-arm) is accepted as final for this load cycle — no retry.
        """
        for _ in range(_AUTO_PLAY_ARM_POLLS):
            if not self.engine.is_known():
                await asyncio.sleep(_AUTO_PLAY_POLL_S)
                continue
            if self.engine.project_running():
                log.info("auto-play: project already running; not sending GO")
                return
            if self.engine.armed == "yes":
                sent = await self.engine.send_osc("/engine/command/go")
                if sent:
                    log.info("auto-play: GO sent after auto-load")
                else:
                    log.warning("auto-play: GO send failed (engine not connected)")
                return
            await asyncio.sleep(_AUTO_PLAY_POLL_S)
        log.warning("auto-play: engine never armed within %.0fs; GO not sent",
                    _AUTO_PLAY_ARM_POLLS * _AUTO_PLAY_POLL_S)

    async def _cancel_auto_play_task(self) -> None:
        """Cancel any in-flight auto-play wait and let it unwind.

        Used on stop() and at the start of shutdown so a pending GO can't be
        sent as the power-off sequence runs. Suppresses CancelledError; logs
        any real error.
        """
        t = self._auto_play_task
        self._auto_play_task = None
        if t is None or t.done():
            return
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("auto-play task errored during cancel")

    def _on_engine_disconnect(self) -> None:
        """Re-arm the load edge-detector when the engine connection drops, so
        a reconnect's status dump is treated as a fresh load and re-asserts
        projector power (otherwise the stale 'seen' flag would suppress it).

        Also clear the power-on debounce window: the debounce only exists to
        coalesce an auto-load re-drive's load flip-flops on a LIVE connection.
        A genuine disconnect/reconnect must always re-assert power, even within
        the window — otherwise a brief blip < _PROJECTOR_ON_DEBOUNCE_S after a
        power-on would leave displays off."""
        self._project_loaded_seen = False
        self._last_projector_on_monotonic = 0.0

    # ------------------- lifecycle -------------------

    async def start(self) -> web.AppRunner:
        await self.engine.start()
        await self.editor.start()
        if self.cfg.projector_power_on_on_load and self.displays.configured:
            self.engine.on_status(self._on_engine_status)
            self.engine.on_disconnect(self._on_engine_disconnect)
        # Independent of the on-load hook above: power displays on at startup
        # so they warm up in parallel with the node-wait / auto-load below.
        self._maybe_power_on_at_start()
        self._auto_load_task = asyncio.create_task(
            self._auto_load_loop(), name="auto-load"
        )
        app = web.Application()
        app.router.add_get("/status", self.handle_status)
        app.router.add_post("/go", self.handle_go)
        app.router.add_post("/stop", self.handle_stop)
        app.router.add_post("/poweron", self.handle_poweron)
        app.router.add_post("/shutdown", self.handle_shutdown)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.listen_host, self.cfg.listen_port)
        await site.start()
        log.info("bridge listening on %s:%d", self.cfg.listen_host, self.cfg.listen_port)
        return runner

    async def stop(self) -> None:
        # Cancel the auto-load loop FIRST so an in-flight long bus-wait (up to
        # auto_load_node_timeout_s) can't keep the event loop alive and delay
        # shutdown / risk a systemd SIGKILL. The loop only awaits sleeps/probes
        # and holds no lock, so cancellation is prompt and safe.
        if self._auto_load_task is not None:
            self._auto_load_task.cancel()
            try:
                await self._auto_load_task
            except asyncio.CancelledError:
                pass
            self._auto_load_task = None
        await self._cancel_auto_play_task()
        await self._cancel_projector_on_task()
        await self.engine.stop()
        await self.editor.stop()
