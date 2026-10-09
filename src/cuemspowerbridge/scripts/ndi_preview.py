# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""cuems-ndi-preview: put an NDI source on a node's output during a montaje.

A thin client of the power-bridge's /ndi/* endpoints (the same ones a
Companion button calls). Refused while a project is loaded or running.

    cuems-ndi-preview sources [--node node01] [--timeout 3]
    cuems-ndi-preview outputs [--node node01]
    cuems-ndi-preview show '#1' --node node01 --output HDMI-A-1 [--native] [--wait]
    cuems-ndi-preview stop [--node node01 | --all]
    cuems-ndi-preview status

The laptop must be cabled into the nodes' switch (adapter on DHCP/automatic,
or 169.254.0.0/16 on-link); unplug it before the show.
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


def _where(r: dict) -> str:
    node = r.get("node") or "controller (local VC)"
    via = r.get("via") or ""
    iface = f" on {r['iface']}" if r.get("iface") else ""
    return f"{node} at {r.get('address', '?')} ({via}{iface})"


def _print(status: int, body: dict, cmd: str) -> int:
    if not body.get("ok", False):
        reason = body.get("reason", "error")
        extra = {k: v for k, v in body.items() if k not in ("ok", "reason")}
        print(f"refused/failed ({status}): {reason}" + (f" {json.dumps(extra)}" if extra else ""),
              file=sys.stderr)
        return 1
    if cmd == "sources":
        print(f"NDI sources seen by {_where(body)}:")
        for s in body.get("sources", []):
            print(f"  #{s['n']}  {s['name']}" + (f"   ({s['addr']})" if s.get("addr") else ""))
        if body.get("hint"):
            print(f"  (none) {body['hint']}")
        print(f"('#n' indexes this list — {body.get('list')})")
    elif cmd == "outputs":
        print(f"outputs of {_where(body)}:")
        for o in body.get("outputs", []):
            print(f"  {o['name']:<10} {o.get('mode', ''):<16} canvas {o['region']}")
    elif cmd == "show":
        line = f"{body.get('source')} → {_where(body)} output={body.get('output') or '(first)'}"
        print(f"{line}: {body.get('confirm')}" + (f" ({body['reason']})" if body.get("reason") else ""))
        if body.get("fit"):
            f = body["fit"]
            print(f"  placed on {f['output']} ({f['mode']}), scale {f['scale']:g}, {f['basis']}")
        if body.get("confirm") == "pending":
            print("  check with: cuems-ndi-preview status")
    elif cmd == "stop":
        for t in body.get("stopped", []):
            print(f"stop sent to {_where(t)}")
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

    s = add("sources", help="list the NDI sources a VC sees")
    s.add_argument("--node")
    s.add_argument("--timeout", type=int, default=3)
    s = add("outputs", help="list a VC's outputs")
    s.add_argument("--node")
    s = add("show", help="show a source ('#n', exact name or substring)")
    s.add_argument("source")
    s.add_argument("--node")
    s.add_argument("--output")
    s.add_argument("--native", action="store_true", help="native pixel size instead of filling")
    s.add_argument("--wait", action="store_true", help="wait for the VC's confirmation (<=15 s)")
    s.add_argument("--force-unknown-engine", action="store_true",
                   help="only when the controller engine cannot be reached")
    s = add("stop", help="remove the preview")
    s.add_argument("--node")
    s.add_argument("--all", action="store_true")
    add("status")
    a = p.parse_args(argv)

    url, conf_token = _defaults(a.config)
    url = a.url or url
    token = a.token or os.environ.get("CUEMS_NDI_TOKEN") or conf_token

    if a.cmd == "sources":
        st, body = _call(url, token, "GET", "/ndi/sources",
                         {"node": a.node, "timeout": a.timeout}, timeout=a.timeout + 20)
    elif a.cmd == "outputs":
        st, body = _call(url, token, "GET", "/ndi/outputs", {"node": a.node})
    elif a.cmd == "show":
        st, body = _call(url, token, "POST", "/ndi/preview", body={
            "source": a.source, "node": a.node, "output": a.output,
            "mode": "native" if a.native else "fill", "wait": a.wait,
            "force_unknown_engine": a.force_unknown_engine}, timeout=60)
    elif a.cmd == "stop":
        st, body = _call(url, token, "POST", "/ndi/stop", body={"node": a.node, "all": a.all})
    else:
        st, body = _call(url, token, "GET", "/ndi/status")
    if a.json:
        print(json.dumps(body, indent=2))
        return 0 if body.get("ok") else 1
    return _print(st, body, a.cmd)


if __name__ == "__main__":
    sys.exit(main())
