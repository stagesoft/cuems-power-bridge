# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""PJLinkDriver tests against a fake in-process PJLink server (no hardware).

Covers: command framing, no-auth + auth (blank AND real password) digest
construction, CRLF-tolerant parsing, ERR3 retry→warn semantics, and the
single-wait_for timeout bound on a hung projector.
"""

import asyncio
import hashlib

import cuemspowerbridge.displays.pjlink as pjlink_mod
from cuemspowerbridge.displays.base import DeviceDef, PowerState
from cuemspowerbridge.displays.pjlink import PJLinkDriver, PJLinkError

import pytest


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _driver(port, password="", timeout=2.0):
    dev = DeviceDef(name="t", host="127.0.0.1", driver="pjlink",
                    port=port, password=password)
    return PJLinkDriver(dev, timeout_s=timeout)


async def test_no_auth_power_on_framing():
    seen = {}

    async def handler(reader, writer):
        writer.write(b"PJLINK 0\r")
        await writer.drain()
        seen["cmd"] = await reader.readuntil(b"\r")
        writer.write(b"%1POWR=OK\r")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port).power_on()
    assert seen["cmd"] == b"%1POWR 1\r"


async def test_auth_blank_password_still_digests():
    rand = "12345678"
    seen = {}

    async def handler(reader, writer):
        writer.write(f"PJLINK 1 {rand}\r".encode())
        await writer.drain()
        seen["cmd"] = await reader.readuntil(b"\r")
        writer.write(b"%1POWR=OK\r")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port, password="").power_off()
    digest = hashlib.md5((rand + "").encode()).hexdigest()
    assert seen["cmd"] == f"{digest}%1POWR 0\r".encode()


async def test_auth_with_password_digest():
    rand, pw = "abcdef12", "secret"
    seen = {}

    async def handler(reader, writer):
        writer.write(f"PJLINK 1 {rand}\r".encode())
        await writer.drain()
        seen["cmd"] = await reader.readuntil(b"\r")
        writer.write(b"%1POWR=OK\r")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port, password=pw).power_on()
    digest = hashlib.md5((rand + pw).encode()).hexdigest()
    assert seen["cmd"] == f"{digest}%1POWR 1\r".encode()


async def test_bad_password_raises():
    async def handler(reader, writer):
        writer.write(b"PJLINK 1 deadbeef\r")
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b"PJLINK ERRA\r")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        with pytest.raises(PJLinkError):
            await _driver(port, password="wrong").power_on()


async def test_crlf_terminators_status():
    # Greeting AND response use CRLF; the leftover LF must not corrupt parse.
    async def handler(reader, writer):
        writer.write(b"PJLINK 0\r\n")
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b"%1POWR=1\r\n")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        state = await _driver(port).power_status()
    assert state == PowerState.ON


async def test_err3_retries_then_warns(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", (0, 0))
    calls = {"n": 0}

    async def handler(reader, writer):
        calls["n"] += 1
        writer.write(b"PJLINK 0\r")
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b"%1POWR=ERR3\r")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        # ERR3 to exhaustion is "issued, verify later" — must NOT raise.
        await _driver(port).power_on()
    assert calls["n"] == 3  # initial + 2 retries (connect-per-command)


async def test_hard_error_raises(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", ())  # no retries

    async def handler(reader, writer):
        writer.write(b"PJLINK 0\r")
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b"%1POWR=ERR2\r")  # bad parameter — hard error
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        with pytest.raises(PJLinkError):
            await _driver(port).power_on()


async def test_hang_bounded_by_timeout(monkeypatch):
    monkeypatch.setattr(pjlink_mod, "_RETRY_DELAYS", ())  # single attempt

    async def handler(reader, writer):
        await asyncio.sleep(5)  # accept socket, never greet

    server, port = await _serve(handler)
    async with server:
        with pytest.raises(PJLinkError):
            await _driver(port, timeout=0.3).power_on()


async def test_dry_run_makes_no_connection():
    # Nothing listening on port 1; dry_run must not attempt to connect.
    dev = DeviceDef(name="x", host="127.0.0.1", driver="pjlink", port=1)
    drv = PJLinkDriver(dev, timeout_s=0.5, dry_run=True)
    await drv.power_on()
    await drv.power_off()
    assert await drv.power_status() == PowerState.UNKNOWN
