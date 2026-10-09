# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""The NDI preview page and the rev 11.1 hardening of /ndi/* (ClickUp
869ekxuez; plan cuems-RELATIONS Plans/2026-10-08-ndi-preview-montajes.md §3.8).

Static checks keep the page honest: self-contained, no HTML parsing of
network-supplied names, Spanish text for every reason token and preview state
the bridge can return.
"""

import asyncio
import base64
import hashlib
import re
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from cuemspowerbridge import ndi_preview as ndi
from cuemspowerbridge.ndi_preview import PreviewError

from test_ndi_preview import NODE01, _Engine, _preview  # noqa: F401  (fixtures + helpers)

PKG = Path(ndi.__file__).parent
PAGE = (PKG / "data" / ndi.PAGE_FILE).read_text("utf-8")
# The script exactly as a browser hashes it: from the real opening tag (the
# last "<script>" in the file) to the closing one, comments never involved.
SCRIPT = PAGE[PAGE.rindex("<script>") + len("<script>"):PAGE.rindex("</script>")]
STYLE = PAGE[PAGE.rindex("<style>") + len("<style>"):PAGE.rindex("</style>")]


# --------------------------------------------------------------------------
# static checks on the page
# --------------------------------------------------------------------------

def _strip_comments(html: str) -> str:
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


def test_page_carries_the_spdx_header():
    assert "SPDX-License-Identifier: GPL-3.0-or-later" in PAGE
    assert "SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>" in PAGE


def test_page_is_self_contained():
    body = _strip_comments(PAGE)
    assert not re.search(r"https?://", body), "no external URL: a controller may have no internet"
    assert not re.search(r"""(src|href)\s*=\s*["']?//""", body)
    assert body.count("<script") == 1 and body.count("<style") == 1
    assert PAGE.count("<script") == 1 and PAGE.count("<style") == 1, \
        "a tag name inside a comment would confuse the CSP hash"
    assert not re.search(r"<script[^>]*\bsrc=", body)
    assert not re.search(r"<link\b", body)


def test_page_never_parses_strings_as_html_or_code():
    for banned in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write",
                   "eval(", "Function(", "setTimeout(\"", "setInterval(\""):
        assert banned not in SCRIPT, banned
    assert not re.search(r"""setAttribute\(\s*["']on""", SCRIPT)
    assert not re.search(r"\.(href|src)\s*=", SCRIPT)
    assert not re.search(r"""\sstyle\s*=\s*["']""", _strip_comments(PAGE)), \
        "inline style attributes would be blocked by the CSP"


def test_page_uses_relative_urls_only():
    for path in re.findall(r"""api\(\s*"[A-Z]+",\s*"([^"]+)\"""", SCRIPT):
        assert not path.startswith("/"), path


def test_page_sends_sources_by_name_and_all_only_from_quitar_todo():
    previews = re.findall(r'api\("POST", "preview", \{([^}]*)\}', SCRIPT)
    assert previews == [" source: selected, output: alias, mode: mode() "]
    assert re.search(r"selected = s\.name", SCRIPT)      # a name from the list, never "#n"
    # The only body without "output" is the Quitar todo one.
    stops = re.findall(r'api\("POST", "stop", ([^)]*)\)', SCRIPT)
    assert sorted(stops) == ["{ output: alias }", "{}"]


def _table(name: str) -> set[str]:
    block = re.search(rf"const {name} = \{{(.*?)\n\}};", SCRIPT, re.S).group(1)
    return set(re.findall(r'^\s*"?([a-z_-]+)"?\s*:', block, re.M))


def _emitted_reasons() -> set[str]:
    pat = re.compile(
        r'PreviewError\(\s*\d+,\s*"([a-z_]+)"'
        r'|ScreenError\(\s*\d+,\s*"([a-z_]+)"'
        r'|RelayError\(\s*"([a-z_]+)"'
        r'|_err\(\s*"([a-z_]+)"'
        r'|reason\s*=\s*(?:v\.reason\s+or\s+)?"([a-z_]+)"')
    found = set()
    for mod in ("ndi_preview.py", "ndi_screens.py", "ndi_relay.py"):
        for groups in pat.findall((PKG / mod).read_text()):
            found.update(g for g in groups if g)
    return found


def test_every_reason_token_has_spanish_text():
    emitted = _emitted_reasons()
    assert {"project_loaded", "bad_body", "relay_stalled", "load_failed"} <= emitted  # the regex works
    missing = emitted - _table("REASONS")
    assert not missing, f"reasons without Spanish text in the page: {sorted(missing)}"


def test_every_preview_state_has_spanish_text():
    src = (PKG / "ndi_preview.py").read_text()
    states = set(re.findall(r'state\s*=\s*"([a-z_-]+)"', src))
    assert {"frames", "failed", "unconfirmed", "superseded", "no_frames_yet"} <= states
    missing = states - _table("STATES")
    assert not missing, f"states without Spanish text in the page: {sorted(missing)}"


def test_every_guard_refusal_has_a_top_line():
    assert _table("BLOCKED") == {"project_loaded", "project_running", "engine_unknown",
                                 "auto_load_active"}


def test_token_field_hygiene():
    m = re.search(r'<input id="token"[^>]*>', PAGE, re.S)
    assert m and 'type="password"' in m.group(0) and 'autocomplete="off"' in m.group(0)
    assert "console." not in SCRIPT


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

async def _client(pv, token=""):
    pv.bridge.cfg.shared_token = token
    app = web.Application()
    pv.register(app)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def test_page_is_served_without_token_with_its_headers(tmp_path):
    pv, _ = _preview(tmp_path)
    client = await _client(pv, token="s3cret")
    try:
        r = await client.get("/ndi/")
        assert r.status == 200
        assert r.headers["Content-Type"].startswith("text/html")
        assert r.headers["X-Frame-Options"] == "DENY"
        assert r.headers["Referrer-Policy"] == "no-referrer"
        csp = r.headers["Content-Security-Policy"]
        digest = base64.b64encode(hashlib.sha256(SCRIPT.encode()).digest()).decode()
        assert f"script-src 'sha256-{digest}'" in csp
        digest = base64.b64encode(hashlib.sha256(STYLE.encode()).digest()).decode()
        assert f"style-src 'sha256-{digest}'" in csp
        assert "frame-ancestors 'none'" in csp and "default-src 'none'" in csp
        assert "SPDX" in await r.text()
        r = await client.get("/ndi", allow_redirects=False)
        assert r.status == 301 and r.headers["Location"] == "ndi/"
    finally:
        await client.close()


async def test_missing_page_is_a_404_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(ndi, "PAGE_FILE", "nope.html")
    pv, _ = _preview(tmp_path)
    client = await _client(pv)
    try:
        r = await client.get("/ndi/")
        assert r.status == 404 and (await r.json())["reason"] == "page_missing"
    finally:
        await client.close()


async def test_posts_need_json(tmp_path):
    pv, sent = _preview(tmp_path)
    client = await _client(pv)
    try:
        for path in ("/ndi/preview", "/ndi/stop"):
            r = await client.post(path, data='{"source":"#1","output":"1"}',
                                  headers={"Content-Type": "text/plain"})
            assert r.status == 415, path
            assert (await r.json())["reason"] == "bad_content_type"
            await asyncio.sleep(0.25)   # the bridge's 200 ms per-endpoint limiter
        r = await client.post("/ndi/preview?source=%231&output=1")
        assert r.status == 415          # the query form is gone with D27
        assert sent == []
    finally:
        await client.close()


async def test_stop_never_clears_everything_by_accident(tmp_path):
    pv, sent = _preview(tmp_path)
    client = await _client(pv)
    try:
        for body in ('{"output": ', '[1, 2]', '{"output": ""}', '{"output": null}'):
            r = await client.post("/ndi/stop", data=body,
                                  headers={"Content-Type": "application/json"})
            assert r.status == 400, body
            assert (await r.json())["reason"] in ("bad_body", "missing_output")
            await asyncio.sleep(0.25)
        assert sent == [], "nothing may be unloaded"
        r = await client.post("/ndi/stop", json={"output": "Monitor derecha"})
        assert r.status == 200
        assert [s["alias"] for s in (await r.json())["stopped"]] == ["node01_HDMI-A-1"]
        await asyncio.sleep(0.25)
        n = len(sent)
        r = await client.post("/ndi/stop", json={})
        assert r.status == 200 and len((await r.json())["stopped"]) == 5
        assert len(sent) > n
    finally:
        await client.close()


async def test_sources_and_live_outputs_are_not_asked_during_a_show(tmp_path):
    for engine, reason in ((_Engine(load="medina_cupula_1"), "project_loaded"),
                           (_Engine(running="yes", load="x"), "project_running"),
                           (_Engine(connected=False), "engine_unknown")):
        pv, sent = _preview(tmp_path, engine=engine)
        with pytest.raises(PreviewError) as e:
            await pv.sources()
        assert e.value.reason == reason
        client = await _client(pv)
        try:
            r = await client.get("/ndi/outputs")
            assert r.status == 200
            outs = (await r.json())["outputs"]
            assert len(outs) == 5 and all(o["present"] is None for o in outs)
        finally:
            await client.close()
        assert sent == [], "no OSC to any VC while a project is loaded"


async def test_status_cache_is_single_flight_and_kept_apart_per_authorization(tmp_path, monkeypatch):
    monkeypatch.setattr(ndi, "STATUS_CACHE_S", 60.0)
    pv, _ = _preview(tmp_path)
    calls = []

    async def fake(authorized):
        calls.append(authorized)
        await asyncio.sleep(0.05)
        return {"authorized": authorized}
    pv._status = fake
    a, b = await asyncio.gather(pv.status(False), pv.status(False))
    assert a == b == {"authorized": False} and calls == [False]
    assert await pv.status(True) == {"authorized": True}       # never the redacted one
    assert await pv.status(False) == {"authorized": False}     # nor the reverse
    assert calls == [False, True]
    await pv.stop("Monitor derecha")                           # a stop invalidates
    assert await pv.status(True) == {"authorized": True} and calls == [False, True, True]


async def test_status_says_why_previews_are_blocked(tmp_path):
    pv, _ = _preview(tmp_path, engine=_Engine(load="medina_cupula_1"))
    st = await pv.status(False)
    assert st["blocked"] == "project_loaded"
    pv2, _ = _preview(tmp_path, auto=True)
    assert (await pv2.status(False))["blocked"] == "auto_load_active"
    pv3, _ = _preview(tmp_path)
    assert (await pv3.status(False))["blocked"] is None
