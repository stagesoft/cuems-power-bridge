# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""cuems-ndi-preview: put an NDI source on any screen of the cluster during a montaje.

A thin client of the power-bridge's /ndi/* endpoints (the same ones a
Companion button calls). Refused while a project is loaded or running.

    cuems-ndi-preview sources                       every source any machine sees
    cuems-ndi-preview outputs                       every screen of the cluster
    cuems-ndi-preview show <source|#n> <screen> [--native] [--wait]
    cuems-ndi-preview stop [<screen>]               no screen = all
    cuems-ndi-preview status                        what is on each screen

A screen is its UI name ("Monitor derecha"), <machine>_<connector>
("node01_HDMI-A-1") or its number in `outputs`. Which machine drives it, and
whether the source reaches it directly or through the controller's relay, is
worked out by the tool. Unplug the laptop before the show.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _defaults(conf: str | None) -> tuple[str, str]:
    """(url, token) from power-bridge.conf; best-effort."""
    try:
        from cuemspowerbridge.config import load
        cfg = load(conf)
        return f"http://127.0.0.1:{cfg.listen_port}", cfg.shared_token
    except Exception:
        return "http://127.0.0.1:8478", ""


def _call(base: str, token: str, method: str, path: str, query: dict | None = None,
          body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    url = base.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {"ok": False, "reason": e.reason}
    except urllib.error.URLError as e:
        return 0, {"ok": False, "reason": f"bridge unreachable at {base}: {e.reason}"}


def _screen(sc: dict) -> str:
    name = f"{sc['name']}  " if sc.get("name") else ""
    return f"{name}({sc['alias']})"


def _print(status: int, body: dict, cmd: str) -> int:
    if not body.get("ok", False):
        reason = body.get("reason", "error")
        extra = {k: v for k, v in body.items() if k not in ("ok", "reason")}
        print(f"refused/failed ({status}): {reason}" + (f" {json.dumps(extra)}" if extra else ""),
              file=sys.stderr)
        return 1
    if cmd == "sources":
        for src in body.get("sources", []):
            print(f"  #{src['n']}  {src['name']}   [seen by: {', '.join(src['seen_by'])}]")
        if body.get("hint"):
            print(f"  (none) {body['hint']}")
    elif cmd == "outputs":
        for sc in body.get("outputs", []):
            flag = "" if sc.get("present") in (None, True) else "   (not driven now)"
            print(f"  {sc['n']:>2}  {sc.get('name') or '-':<22} {sc['alias']}{flag}")
    elif cmd == "show":
        line = f"{body.get('source')} -> {_screen(body['screen'])}: {body.get('confirm')}"
        print(line + (f" ({body['reason']})" if body.get("reason") else ""))
        if body.get("route") == "relay":
            print("  (through the controller: this machine does not see the source itself)")
        if body.get("fit"):
            f = body["fit"]
            print(f"  {f['mode']}, scale {f['scale']:g}, {f['basis']}")
        if body.get("confirm") == "pending":
            print("  check with: cuems-ndi-preview status")
    elif cmd == "stop":
        print("stopped: " + ", ".join(_screen(sc) for sc in body.get("stopped", [])))
    elif cmd == "status":
        eng = body.get("engine", {})
        print(f"engine: load={eng.get('load')!r} running={eng.get('running')!r}")
        previews = body.get("previews", [])
        if not previews:
            print("no preview on any screen")
        for p in previews:
            state = p["confirm"] + (" WIPED (project loaded)" if p.get("wiped") else "") + \
                (" stopped" if p.get("stopped") else "")
            print(f"  {_screen(p['screen'])}: {p['source']} [{state}]"
                  + (" via controller" if p.get("route") == "relay" else ""))
    else:
        print(json.dumps(body, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="cuems-ndi-preview", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", help="bridge URL (default: from power-bridge.conf)")
    p.add_argument("--token", help="X-Auth-Token (default: $CUEMS_NDI_TOKEN, then the conf)")
    p.add_argument("-c", "--config", default=os.environ.get("CUEMS_POWER_BRIDGE_CONF"))
    p.add_argument("--json", action="store_true", help="print the raw JSON answer")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="print the raw JSON answer")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, **kw) -> argparse.ArgumentParser:
        return sub.add_parser(name, parents=[common], **kw)

    add("sources", help="every NDI source any machine of the cluster sees")
    add("outputs", help="every screen of the cluster")
    s = add("show", help="show a source ('#n', exact name or substring) on a screen")
    s.add_argument("source")
    s.add_argument("screen", help='UI name ("Monitor derecha"), machine_connector, or number')
    s.add_argument("--native", action="store_true", help="native pixel size instead of filling")
    s.add_argument("--wait", action="store_true", help="wait for the VC's confirmation (<=15 s)")
    s.add_argument("--force-unknown-engine", action="store_true",
                   help="only when the controller engine cannot be reached")
    s = add("stop", help="remove the preview from one screen, or from all")
    s.add_argument("screen", nargs="?")
    add("status")
    a = p.parse_args(argv)

    url, conf_token = _defaults(a.config)
    url = a.url or url
    token = a.token or os.environ.get("CUEMS_NDI_TOKEN") or conf_token

    if a.cmd == "sources":
        st, body = _call(url, token, "GET", "/ndi/sources", timeout=30)
    elif a.cmd == "outputs":
        st, body = _call(url, token, "GET", "/ndi/outputs", timeout=30)
    elif a.cmd == "show":
        st, body = _call(url, token, "POST", "/ndi/preview", body={
            "source": a.source, "output": a.screen,
            "mode": "native" if a.native else "fill", "wait": a.wait,
            "force_unknown_engine": a.force_unknown_engine}, timeout=60)
    elif a.cmd == "stop":
        st, body = _call(url, token, "POST", "/ndi/stop",
                         body={"output": a.screen} if a.screen else {})
    else:
        st, body = _call(url, token, "GET", "/ndi/status")
    if a.json:
        print(json.dumps(body, indent=2))
        return 0 if body.get("ok") else 1
    return _print(st, body, a.cmd)


if __name__ == "__main__":
    sys.exit(main())
