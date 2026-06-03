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
import shlex
import time
from datetime import datetime, timezone
from typing import Any

from aiohttp import web

from . import network_map
from .config import Config
from .displays.manager import DisplayManager
from .editor_client import EditorClient
from .engine_state import EngineClient
from .node_executor import SshTarget, poweroff_all
from .reachability import wait_until_all_down
from .shelly import ShellyClient, ShellyError

log = logging.getLogger(__name__)

# /status state machine
STATES = (
    "idle", "checking", "polling", "arming-shelly", "poweroff-issued",
    "done", "failed",
)


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
        self._project_loaded_seen = False
        self._shutdown_lock = asyncio.Lock()
        self._auto_load_done = False
        self._auto_load_failures = 0
        self._auto_load_disabled = False
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
        if "--no-block" not in cmd:
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
        """Watch engine cache; trigger auto-load when conditions met."""
        if not self.cfg.auto_load_project:
            return
        while True:
            await asyncio.sleep(2.0)
            if self._auto_load_disabled:
                return
            if not self.engine.is_known():
                continue
            if self.engine.project_loaded():
                continue
            if self._auto_load_done and not self.cfg.auto_load_persistent:
                # Once-only mode: operator may have intentionally unloaded.
                continue
            await self._try_auto_load()

    async def _try_auto_load(self) -> None:
        uuid = self.cfg.auto_load_project
        if not self.editor.connected:
            log.debug("auto-load: editor not connected, skipping this round")
            return
        log.info("auto-load: sending project_ready %s", uuid)
        sent = await self.editor.send_action("project_ready", uuid)
        if not sent:
            log.warning("auto-load: send failed")
            return

        # Race-aware wait: engine status flips first, editor ack follows.
        # We watch both; whichever comes first decides outcome.
        async def wait_engine() -> bool:
            for _ in range(120):  # 60s
                await asyncio.sleep(0.5)
                if self.engine.project_loaded():
                    return True
            return False

        async def wait_editor_error() -> bool:
            resp = await self.editor.wait_for_response("project_ready", timeout=60)
            if resp is None:
                return False
            return resp.get("type") == "error"

        engine_task = asyncio.create_task(wait_engine())
        editor_err_task = asyncio.create_task(wait_editor_error())
        try:
            done, pending = await asyncio.wait(
                {engine_task, editor_err_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=65,
            )
        finally:
            for t in (engine_task, editor_err_task):
                if not t.done():
                    t.cancel()

        # If editor error fired first → fail-fast.
        if editor_err_task in done and editor_err_task.result() is True:
            self._auto_load_failures += 1
            log.error("auto-load: editor returned error for uuid=%s (failure %d/3)",
                      uuid, self._auto_load_failures)
            if self._auto_load_failures >= 3:
                log.error("auto-load: 3 consecutive failures, disabling for session")
                self._auto_load_disabled = True
            return

        if engine_task in done and engine_task.result() is True:
            self._auto_load_done = True
            self._auto_load_failures = 0
            log.info("auto-load: project loaded (engine status confirms)")
            return

        # Editor responded with success BEFORE engine status arrived (the
        # broadcast and editor ack race; editor wins ~30% of the time on
        # localhost). Editor success means the project_ready handler ran
        # to completion — accept that as confirmation, with a final check
        # against the engine status cache.
        editor_resp_ok = (
            editor_err_task in done
            and editor_err_task.result() is False
            and self.editor.connected
        )
        if editor_resp_ok and self.engine.project_loaded():
            self._auto_load_done = True
            self._auto_load_failures = 0
            log.info("auto-load: project loaded (editor ack + engine cache confirm)")
            return
        if self.engine.project_loaded():
            # Race the other way: engine status arrived but our wait_engine
            # task got cancelled before its sleep cycle saw it. Still OK.
            self._auto_load_done = True
            self._auto_load_failures = 0
            log.info("auto-load: project loaded (engine cache confirms post-wait)")
            return

        self._auto_load_failures += 1
        log.warning("auto-load: no confirmation within 60s (failure %d/3)",
                    self._auto_load_failures)
        if self._auto_load_failures >= 3:
            log.error("auto-load: 3 consecutive failures, disabling for session")
            self._auto_load_disabled = True

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
            self._spawn_projector_power_on()
        elif not loaded:
            # Back to no project — re-arm so the next load powers on again.
            self._project_loaded_seen = False

    def _spawn_projector_power_on(self) -> None:
        if self._projector_on_task is not None and not self._projector_on_task.done():
            log.debug("projector power-on already in progress; skipping duplicate")
            return
        log.info("project loaded → powering on displays")
        self._projector_on_task = asyncio.create_task(
            self.displays.power_on_all(), name="projector-power-on"
        )

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

    def _on_engine_disconnect(self) -> None:
        """Re-arm the load edge-detector when the engine connection drops, so
        a reconnect's status dump is treated as a fresh load and re-asserts
        projector power (otherwise the stale 'seen' flag would suppress it)."""
        self._project_loaded_seen = False

    # ------------------- lifecycle -------------------

    async def start(self) -> web.AppRunner:
        await self.engine.start()
        await self.editor.start()
        if self.cfg.projector_power_on_on_load and self.displays.configured:
            self.engine.on_status(self._on_engine_status)
            self.engine.on_disconnect(self._on_engine_disconnect)
        asyncio.create_task(self._auto_load_loop(), name="auto-load")
        app = web.Application()
        app.router.add_get("/status", self.handle_status)
        app.router.add_post("/go", self.handle_go)
        app.router.add_post("/stop", self.handle_stop)
        app.router.add_post("/shutdown", self.handle_shutdown)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self.cfg.listen_host, self.cfg.listen_port)
        await site.start()
        log.info("bridge listening on %s:%d", self.cfg.listen_host, self.cfg.listen_port)
        return runner

    async def stop(self) -> None:
        await self._cancel_projector_on_task()
        await self.engine.stop()
        await self.editor.stop()
