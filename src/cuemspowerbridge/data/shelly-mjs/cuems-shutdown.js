// cuems-shutdown.js -- Shelly Pro 1 (Gen 2) mJS script.
//
// Installs into the Shelly's Scripts tab via the device web UI. Edit
// BRIDGE and TOKEN below for your deployment, then click "Save" and
// "Start". The script then sleeps until the wired flip-switch on SW
// input 0 transitions to OFF, waits a short cancellable grace window,
// and then asks the controller's power bridge to do an orderly cluster
// shutdown. The bridge arms a hardware safety timer on this Shelly which
// opens the relay (cuts mains) after the configured number of seconds --
// even if the bridge itself disappears mid-shutdown, the Shelly will
// still cut power.
//
// SW0 must be configured as a DETACHED input (it triggers this script;
// it does NOT directly switch the relay) -- otherwise flipping OFF would
// cut the controller's mains instantly, defeating the orderly shutdown.
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
let CANCEL_GRACE_S = 5;                    // after SW0 -> OFF, wait this many
                                           // seconds before asking the bridge to
                                           // shut down; flip SW0 back ON within
                                           // the window to cancel (accidental or
                                           // changed-mind flip -> nothing happens).
let inflight = false;                      // guard while a /shutdown POST is in flight
let graceTimer = null;                     // pending grace countdown (null = none)

function sendShutdown() {
  graceTimer = null;
  if (inflight) return;
  inflight = true;
  // Fail-safe: if the HTTP callback never fires (Shelly internal hang),
  // clear the lock after 10 s so future flips aren't permanently blocked.
  Timer.set(10000, false, function () { inflight = false; });

  Shelly.call("HTTP.POST", {
    url: BRIDGE + "/shutdown",
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
    if (graceTimer !== null) Timer.clear(graceTimer);
    print("[cuems] SW0 OFF -- shutting down in " + CANCEL_GRACE_S + "s (flip ON to cancel)");
    graceTimer = Timer.set(CANCEL_GRACE_S * 1000, false, sendShutdown);
    return;
  }

  if (ev.delta.state === true) {
    // SW0 -> ON within the grace window: cancel the pending shutdown.
    // (If the window already elapsed and the bridge was asked, graceTimer
    // is null and there's nothing to cancel here -- the bridge is committed.)
    if (graceTimer !== null) {
      Timer.clear(graceTimer);
      graceTimer = null;
      print("[cuems] SW0 back ON within grace -- shutdown cancelled");
    }
    return;
  }
});

print("[cuems] cuems-shutdown.js armed -- flip SW0 OFF to initiate orderly shutdown (" + CANCEL_GRACE_S + "s grace)");
