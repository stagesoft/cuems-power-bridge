# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""Install (or re-install) the cuems-shutdown.js mJS script on a Shelly
Pro 1 (Gen 2) via its HTTP RPC. Patches BRIDGE + TOKEN inline before
upload. Uploads in 1024-byte chunks (Shelly's Script.PutCode requires
multiple PutCode calls for anything larger than its buffer).

The template MUST be ASCII-only -- Shelly's Script.PutCode rejects
non-ASCII bytes with `-103: Missing or bad argument 'code'!`. The
shipped template is ASCII-clean; if you edit it, keep it that way.

Usage:
    cuems-power-bridge-install-mjs --shelly http://10.16.8.10 \\
                                   --bridge http://controller.local:8478 \\
                                   --token mysecret

If --token is omitted, the script uploads an empty TOKEN (matches a
bridge configured without shared_token). Existing scripts named
"cuems-shutdown" on the Shelly are stopped + deleted first.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from importlib import resources
from pathlib import Path

# Single source of truth for the shipped grace default. Must match the
# `let CANCEL_GRACE_S = 5;` literal in data/shelly-mjs/cuems-shutdown.js.
DEFAULT_GRACE_S = 5


def _patched_code(template: str, bridge: str, token: str, toggle_id: int,
                  force: bool, cancel_grace: int = DEFAULT_GRACE_S) -> str:
    """Patch the BRIDGE + TOKEN + TOGGLE_ID + FORCE + CANCEL_GRACE_S literals
    in the template.

    Each substitution is guarded by a PRE-replace check that the expected
    literal is present, so a template drift (renamed/reformatted literal)
    raises rather than silently shipping an unpatched value — uniformly for
    all of them, including TOKEN and the no-op default cases.
    """
    subs = [
        ('let BRIDGE = "http://controller.local:8478";', f'let BRIDGE = "{bridge}";'),
        ('let TOKEN  = "REPLACE-ME";', f'let TOKEN  = "{token}";'),
        ('let TOGGLE_ID = 200;', f'let TOGGLE_ID = {int(toggle_id)};'),
        ('let FORCE  = true;', f'let FORCE  = {"true" if force else "false"};'),
        (f"let CANCEL_GRACE_S = {DEFAULT_GRACE_S};",
         f"let CANCEL_GRACE_S = {int(cancel_grace)};"),
    ]
    code = template
    for find, repl in subs:
        if find not in code:
            raise RuntimeError(
                f"patch failed: template is missing the expected literal {find!r} "
                "— did the template change? Update _patched_code to match."
            )
        code = code.replace(find, repl, 1)
    non_ascii = [c for c in code if ord(c) > 127]
    if non_ascii:
        raise RuntimeError(
            f"template contains {len(non_ascii)} non-ASCII chars; Shelly will "
            f"reject Script.PutCode. Replace these chars in the template first."
        )
    return code


def _load_template(custom_path: str | None) -> str:
    if custom_path:
        return Path(custom_path).read_text(encoding="utf-8")
    # Bundled with the package
    return (
        resources.files("cuemspowerbridge.data.shelly-mjs")
        .joinpath("cuems-shutdown.js")
        .read_text(encoding="utf-8")
    )


async def _rpc(session: aiohttp.ClientSession, base: str, method: str, params: dict):
    async with session.post(f"{base}/rpc/{method}", json=params) as r:
        text = await r.text()
        if r.status != 200:
            raise RuntimeError(f"{method} returned {r.status}: {text[:200]}")
        # Some endpoints return raw `null` on success; tolerate both
        if not text or text == "null":
            return None
        return await r.json(content_type=None) if r.content_type != "application/json" else None or __import__("json").loads(text)


# Web-UI shutdown control. A standalone virtual component is NOT clickable in
# this firmware's UI (the Components page is management-only; Home shows only
# physical switches/inputs). The control becomes a clickable switch on the Home
# page only when a *boolean* virtual component ("toggle" view) is placed inside
# a *group*. `name` is the idempotency key. Verified on a Pro1 fw 1.7.5:
# flipping the toggle emits a boolean status delta {value:true} that the mJS
# catches; the script springs it back to false so it acts as a momentary push.
TOGGLE_NAME = "SHUTDOWN CLUSTER"
TOGGLE_META = {"ui": {"view": "toggle"}}
GROUP_NAME = "SHUTDOWN"


async def _ensure_toggle(session: "aiohttp.ClientSession", base: str) -> int:
    """Ensure the web-UI shutdown control exists: a boolean toggle named
    TOGGLE_NAME placed in a group named GROUP_NAME so it renders as a clickable
    switch on the Home page. Idempotent (reuses existing toggle + group, never
    duplicates). Returns the boolean's numeric id. Requires fw with virtual
    components (>= 1.4)."""
    async def _components():
        async with session.post(
            f"{base}/rpc/Shelly.GetComponents", json={"dynamic_only": True}
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"GetComponents failed: {r.status} {await r.text()}")
            return (await r.json()).get("components", [])

    async def _add(vtype: str, cfg: dict) -> int:
        async with session.post(
            f"{base}/rpc/Virtual.Add", json={"type": vtype, "config": cfg}
        ) as r:
            if r.status != 200:
                raise RuntimeError(f"Virtual.Add {vtype} failed: {r.status} {await r.text()}")
            return (await r.json())["id"]

    def _find(comps, prefix, vname):
        return next((c["config"]["id"] for c in comps
                     if c.get("key", "").startswith(prefix)
                     and c.get("config", {}).get("name") == vname), None)

    comps = await _components()
    bid = _find(comps, "boolean:", TOGGLE_NAME)
    if bid is None:
        bid = await _add("boolean", {"name": TOGGLE_NAME, "meta": TOGGLE_META})
        print(f"  created toggle 'boolean:{bid}' (name={TOGGLE_NAME})")
    else:
        print(f"  reusing toggle 'boolean:{bid}' (name={TOGGLE_NAME})")

    gid = _find(comps, "group:", GROUP_NAME)
    if gid is None:
        gid = await _add("group", {"name": GROUP_NAME})
        print(f"  created group 'group:{gid}' (name={GROUP_NAME})")
    else:
        print(f"  reusing group 'group:{gid}' (name={GROUP_NAME})")

    # Associate the toggle with the group so it shows as a Home-page switch
    # (idempotent; Group.SetConfig doesn't echo membership but it does stick).
    async with session.post(
        f"{base}/rpc/Group.SetConfig",
        json={"id": gid, "config": {"components": [f"boolean:{bid}"]}},
    ) as r:
        if r.status != 200:
            raise RuntimeError(f"Group.SetConfig failed: {r.status} {await r.text()}")
    print(f"  group:{gid} <- boolean:{bid} (web-UI toggle on Home)")
    return bid


async def install(shelly_url: str, template: str, bridge: str, token: str,
                  force: bool = True, cancel_grace: int = DEFAULT_GRACE_S,
                  name: str = "cuems-shutdown") -> int:
    """Ensure the web-UI toggle + group, patch the template, upload + start the
    script. Returns the script id."""
    import aiohttp  # deferred: keeps _patched_code/_load_template importable without aiohttp
    async with aiohttp.ClientSession() as s:
        # 0. Ensure the web-UI toggle (boolean in a Home group) exists, learn
        #    its id, then patch BRIDGE/TOKEN/TOGGLE_ID/FORCE/CANCEL_GRACE_S.
        toggle_id = await _ensure_toggle(s, shelly_url)
        code = _patched_code(template, bridge, token, toggle_id, force, cancel_grace)
        print(f"  patched code: {len(code)} bytes, BRIDGE={bridge}, "
              f"TOKEN={'(set)' if token else '(empty)'}, TOGGLE_ID={toggle_id}, "
              f"FORCE={'true (always off)' if force else 'false (safe)'}, "
              f"cancel_grace={cancel_grace}s")

        # 1. Remove any existing script with the same name
        async with s.post(f"{shelly_url}/rpc/Script.List", json={}) as r:
            lst = await r.json()
        for entry in lst.get("scripts", []):
            if entry.get("name") == name:
                old = entry["id"]
                print(f"  removing existing '{name}' (id={old})")
                async with s.post(f"{shelly_url}/rpc/Script.Stop", json={"id": old}):
                    pass
                async with s.post(f"{shelly_url}/rpc/Script.Delete", json={"id": old}):
                    pass

        # 2. Create
        async with s.post(f"{shelly_url}/rpc/Script.Create", json={"name": name}) as r:
            c = await r.json()
        sid = c["id"]
        print(f"  created '{name}' (id={sid})")

        # 3. PutCode in chunks (1024 bytes — fits in Shelly's RPC buffer)
        CHUNK = 1024
        chunks = [code[i:i + CHUNK] for i in range(0, len(code), CHUNK)]
        for i, chunk in enumerate(chunks):
            async with s.post(
                f"{shelly_url}/rpc/Script.PutCode",
                json={"id": sid, "code": chunk, "append": i > 0},
            ) as r:
                if r.status != 200:
                    raise RuntimeError(f"PutCode chunk {i}: {r.status} {await r.text()}")
        print(f"  uploaded {len(code)} bytes in {len(chunks)} chunks")

        # 4. Enable in config (so it survives the Shelly's own reboot)
        async with s.post(
            f"{shelly_url}/rpc/Script.SetConfig",
            json={"id": sid, "config": {"enable": True}},
        ) as r:
            assert r.status == 200, await r.text()
        print(f"  enabled (auto-starts on Shelly boot)")

        # 5. Start
        async with s.post(f"{shelly_url}/rpc/Script.Start", json={"id": sid}) as r:
            print(f"  started: {await r.text()}")

        # 6. Confirm running
        async with s.post(f"{shelly_url}/rpc/Script.GetStatus", json={"id": sid}) as r:
            status = await r.json()
        print(f"  GetStatus: running={status['running']}, mem_used={status['mem_used']}")
        return sid


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="cuems-power-bridge-install-mjs",
        description="Install the cuems-shutdown.js mJS on a Shelly Pro 1.",
    )
    parser.add_argument("--shelly", required=True,
                        help="Shelly base URL (e.g. http://10.16.8.10)")
    parser.add_argument("--bridge", required=True,
                        help="Bridge URL the mJS will POST to (e.g. http://controller.local:8478)")
    parser.add_argument("--token", default="",
                        help="X-Auth-Token shared with the bridge (default: empty)")
    parser.add_argument("--template",
                        help="Path to custom .js template (default: bundled cuems-shutdown.js)")
    parser.add_argument("--name", default="cuems-shutdown",
                        help="Shelly script name (default: cuems-shutdown)")
    parser.add_argument("--cancel-grace", type=int, default=DEFAULT_GRACE_S,
                        metavar="SECONDS",
                        help="Grace window after SW0->OFF before /shutdown is sent; "
                             "flip SW0 back ON within it to cancel "
                             f"(default: {DEFAULT_GRACE_S})")
    parser.add_argument("--safe", action="store_true",
                        help="Both triggers respect a running project (bridge "
                             "409s while playing). Default: the Shelly always "
                             "shuts the cluster down, even mid-show.")
    args = parser.parse_args()

    if args.cancel_grace < 0:
        print("ERROR: --cancel-grace must be >= 0", file=sys.stderr)
        return 1

    template = _load_template(args.template)
    force = not args.safe
    print(f"Installing on {args.shelly} ... "
          f"({'FORCE: always shuts down' if force else 'SAFE: refuses while a project runs'})")
    try:
        asyncio.run(install(args.shelly, template, args.bridge, args.token,
                            force=force, cancel_grace=args.cancel_grace, name=args.name))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
