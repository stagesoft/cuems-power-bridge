# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""EditorClient request/reply matching (plan 869fghmxn §4.6, S1).

No socket: send_action is replaced by a recorder and replies are fed through
_resolve(), the same entry point _consume() uses."""

import asyncio

import pytest

from cuemspowerbridge.editor_client import (EditorClient, EditorDisconnected,
                                            RequestPending, match_project_ready)

U1 = "0123abcd-4567-89ab-cdef-0123456789ab"
U2 = "fedcba98-7654-3210-fedc-ba9876543210"


class _Client(EditorClient):
    def __init__(self, send_ok=True):
        super().__init__("ws://test")
        self.connected = True
        self.sent = []
        self.send_ok = send_ok

    async def send_action(self, action, value):
        self.sent.append((action, value))
        return self.send_ok


async def test_reply_before_wait_is_not_lost():
    c = _Client()
    req = await c.request("project_ready", U1, match=match_project_ready(U1))
    c._resolve({"type": "project_ready", "value": U1})   # before anyone waits
    assert await c.wait(req, 1) == {"type": "project_ready", "value": U1}
    assert c.outstanding() is None


async def test_second_single_flight_request_is_refused():
    c = _Client()
    await c.request("project_ready", U1, match=match_project_ready(U1))
    with pytest.raises(RequestPending):
        c.register("project_unload")
    with pytest.raises(RequestPending):
        c.register("project_ready", U2)
    # project_list is not single-flight
    assert await c.request("project_list") is not None


async def test_timeout_keeps_the_slot_and_the_late_reply_frees_it():
    c = _Client()
    req = await c.request("project_ready", U1, match=match_project_ready(U1))
    assert await c.wait(req, 0.01) is None          # waiter gave up
    assert c.outstanding() is req                   # slot still held
    with pytest.raises(RequestPending):
        c.register("project_ready", U2)
    c._resolve({"type": "project_ready", "value": U1})  # the late reply
    assert c.outstanding() is None
    req2 = await c.request("project_ready", U2, match=match_project_ready(U2))
    # the late reply was consumed by its own request, never by req2
    assert not req2.future.done()


async def test_late_reply_never_resolves_a_later_request():
    c = _Client()
    r1 = await c.request("project_unload")
    c._resolve({"type": "project_unload", "value": "OK"})
    assert (await c.wait(r1, 1))["value"] == "OK"
    r2 = await c.request("project_unload")
    # a stray duplicate of the first reply arrives: it is r2's by shape (no
    # id), which is why only one unload can be outstanding at a time
    assert not r2.future.done()


async def test_ready_matched_by_uuid():
    c = _Client()
    req = await c.request("project_ready", U1, match=match_project_ready(U1))
    c._resolve({"type": "project_ready", "value": U2})   # someone else's
    assert not req.future.done()
    c._resolve({"type": "error", "action": "project_ready", "uuid": U1,
                "value": "item with uuid: x does not exist"})
    assert (await c.wait(req, 1))["type"] == "error"


async def test_failed_send_unregisters():
    c = _Client(send_ok=False)
    assert await c.request("project_ready", U1) is None
    assert c.outstanding() is None
    c.send_ok = True
    assert await c.request("project_unload") is not None


async def test_send_exception_unregisters():
    c = _Client()

    async def boom(action, value):
        raise RuntimeError("socket gone")

    c.send_action = boom
    with pytest.raises(RuntimeError):
        await c.request("project_ready", U1)
    assert c.outstanding() is None


async def test_disconnect_fails_every_pending_request():
    c = _Client()
    r1 = await c.request("project_ready", U1)
    r2 = await c.request("project_list")
    c._fail_pending("editor socket closed")
    for r in (r1, r2):
        with pytest.raises(EditorDisconnected):
            await c.wait(r, 1)
    assert c.outstanding() is None


async def test_bare_error_resolves_nothing():
    c = _Client()
    req = await c.request("project_list")
    c._resolve({"type": "error", "value": "something"})
    assert not req.future.done()


async def test_list_reply_fans_out():
    c = _Client()
    a = await c.request("project_list")
    b = await c.request("project_list")
    c._resolve({"type": "project_list", "value": []})
    assert await c.wait(a, 1) == await c.wait(b, 1)


async def test_unregister_cancels_quietly():
    c = _Client()
    req = c.register("project_list")
    c.unregister(req)
    assert req.future.cancelled()
    await asyncio.sleep(0)
