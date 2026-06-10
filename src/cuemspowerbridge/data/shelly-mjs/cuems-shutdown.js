// cuems-shutdown.js -- Shelly Pro 1 (Gen 2) mJS script.
//
// Installs into the Shelly's Scripts tab via the device web UI (or the
// cuems-power-bridge-install-mjs helper). Edit BRIDGE and TOKEN below for
// your deployment, then click "Save" and "Start". The script then sleeps
// until one of two triggers asks the controller's power bridge for an
// orderly cluster shutdown:
//
//   1. The wired flip-switch on SW input 0 transitions to OFF (after a
//      short cancellable grace window -- flip back ON to cancel).
//   2. The "SHUTDOWN CLUSTER" web-UI toggle is flipped ON -- a boolean
//      virtual component surfaced as a clickable switch on the Home page
//      via the "SHUTDOWN" group; the script springs it back to OFF so it
//      acts as a momentary trigger.
//
// FORCE controls whether those triggers shut the cluster down even while a
// project is running. Default true => the Shelly ALWAYS turns the cluster
// off (a true hardware kill). Set false (installer flag --safe) to make the
// bridge refuse with 409 while a project is running.
//
// The bridge arms a hardware safety timer on this Shelly which opens the
// relay (cuts mains) after the configured number of seconds -- even if the
// bridge itself disappears mid-shutdown, the Shelly will still cut power.
//
// SW0 must be configured as a DETACHED input (it triggers this script;
// it does NOT directly switch the relay) -- otherwise flipping OFF would
// cut the controller's mains instantly, defeating the orderly shutdown.
// On the flip back to ON (no shutdown pending) this script re-closes the
// relay to restore mains, so the cluster powers on again (with BIOS
// power-on-after-AC-loss = ON). That is the "relay on -> machines boot" path.
//
// SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
// SPDX-License-Identifier: GPL-3.0-or-later

// BRIDGE: today controller.local is the bond0 avahi alias and is what
// the Shelly (on the production LAN) reaches. There's a planned migration
// where bond0 will be renamed to formitgo.local and controller.local will
// scope to a non-bond0 interface (the "ipv4all" iface) -- when that lands,
// flip this to http://formitgo.local:8478 (and update Companion's URL).
// Always use the alias of the interface that's on the Shelly's L2.
let BRIDGE = "http://controller.local:8478";
let TOKEN  = "REPLACE-ME";                 // matches power-bridge.conf shared_token
let TOGGLE_ID = 200;                       // web-UI toggle (boolean) id; patched by the installer
let FORCE  = true;                         // patched by the installer (--safe sets false)
let CANCEL_GRACE_S = 5;                    // after SW0 -> OFF, wait this many
                                           // seconds before asking the bridge to
                                           // shut down; flip SW0 back ON within
                                           // the window to cancel (accidental or
                                           // changed-mind flip -> nothing happens).
let inflight = false;                      // guard while a /shutdown POST is in flight
let graceTimer = null;                     // pending grace countdown (null = none)

// Shared trigger path. force=true => POST /shutdown?force=1 (bypasses the
// bridge's refuse-if-running guard, so the cluster goes down even mid-show);
// false => plain /shutdown (the bridge 409s while a project is running).
function sendShutdown(force) {
  // Consume any pending grace countdown (e.g. the web-UI toggle firing while
  // an SW0 grace window is still ticking) so it can't fire a second time.
  if (graceTimer !== null) { Timer.clear(graceTimer); graceTimer = null; }
  if (inflight) { print("[cuems] shutdown already in flight -- skipping"); return; }
  inflight = true;
  // Fail-safe: if the HTTP callback never fires (Shelly internal hang),
  // clear the lock after 10 s so future flips aren't permanently blocked.
  Timer.set(10000, false, function () { inflight = false; });

  // NB: HTTP.Request, NOT HTTP.POST. Shelly's HTTP.POST only honors a
  // `content_type` field and SILENTLY DROPS a custom `headers` map -- so
  // X-Auth-Token never gets sent and the bridge rejects with 401 bad_token.
  // HTTP.Request is the only HTTP method that transmits arbitrary headers.
  Shelly.call("HTTP.Request", {
    method: "POST",
    url: BRIDGE + "/shutdown" + (force ? "?force=1" : ""),
    body: "{}",
    headers: {"X-Auth-Token": TOKEN, "Content-Type": "application/json"}
  }, function (r, err_code, err_msg) {
    inflight = false;
    if (err_code !== 0) {
      print("[cuems] bridge unreachable: " + err_msg);
      return;
    }
    if (r.code === 200) {
      // Bridge accepted. It will arm Switch.Set toggle_after, which
      // opens our relay when ready. We do nothing more here.
      print("[cuems] shutdown accepted");
      return;
    }
    if (r.code === 409) { print("[cuems] shutdown refused (project running or in progress): " + r.body); return; }
    if (r.code === 401) { print("[cuems] bad token"); return; }
    if (r.code === 503) { print("[cuems] engine state unknown -- try again later"); return; }
    if (r.code === 502) { print("[cuems] bridge could not reach engine/Shelly: " + r.body); return; }
    print("[cuems] unexpected response " + r.code + ": " + r.body);
  });
}

Shelly.addStatusHandler(function (ev) {
  if (ev.component !== "input:0") return;
  if (ev.delta.state === undefined) return;

  if (ev.delta.state === false) {
    // SW0 -> OFF: (re)start the cancellable grace countdown. Each OFF
    // restarts it, so a flip OFF/ON/OFF settles on a fresh window.
    // If a shutdown is already in flight, don't queue another (the bridge
    // would 409 it anyway) -- log so the operator gets feedback.
    if (inflight) {
      print("[cuems] SW0 OFF -- shutdown already in flight, ignoring");
      return;
    }
    if (graceTimer !== null) Timer.clear(graceTimer);
    print("[cuems] SW0 OFF -- shutting down in " + CANCEL_GRACE_S + "s (flip ON to cancel)");
    graceTimer = Timer.set(CANCEL_GRACE_S * 1000, false, function () { sendShutdown(FORCE); });
    return;
  }

  // ev.delta.state === true here (undefined + false already returned above).
  if (graceTimer !== null) {
    // SW0 -> ON within the grace window: cancel the pending shutdown. The
    // bridge was never asked and the relay was never opened, so mains is
    // still on -- nothing else to do.
    Timer.clear(graceTimer);
    graceTimer = null;
    print("[cuems] SW0 back ON within grace -- shutdown cancelled");
    return;
  }

  // SW0 -> ON with no pending shutdown: this is a power-ON request. The
  // cluster was previously shut down and the bridge opened the relay (mains
  // cut), so re-close the relay to restore mains. With BIOS power-on-after-
  // AC-loss = ON the controller + nodes boot, the bridge autoloads the
  // project and the projectors wake. (If mains is already on, harmless
  // no-op.) Requires SW0 DETACHED so the input doesn't fight this Set.
  print("[cuems] SW0 ON -- closing relay to restore mains");
  Shelly.call("Switch.Set", {id: 0, on: true});
});

// Trigger 2: web-UI toggle (boolean virtual component, shown on Home via the
// "SHUTDOWN" group) flipped ON. Spring it straight back to OFF so it acts as
// a momentary push, then fire. No grace window (a UI flip is deliberate).
Shelly.addStatusHandler(function (ev) {
  if (ev.component !== "boolean:" + TOGGLE_ID) return;
  if (ev.delta === undefined || ev.delta.value !== true) return;
  print("[cuems] web UI toggle -- initiating shutdown");
  Shelly.call("Boolean.Set", {id: TOGGLE_ID, value: false});  // spring back to OFF
  sendShutdown(FORCE);
});

print("[cuems] cuems-shutdown.js armed (" + (FORCE ? "force" : "safe") + ") -- flip SW0 OFF (" + CANCEL_GRACE_S + "s grace) or flip the web-UI toggle to initiate orderly shutdown");
