# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""EscVpDriver tests against a fake in-process ESC/VP.net server (no hardware).

Covers: HELLO framing with/without password, trailing-header drain (byte15>0),
auth status codes, reply parsing (ERR vs bare ':'), CR/LF command rejection,
timeout bound, dry_run, and sequential-call reconnect.
"""

import asyncio

import pytest

import cuemspowerbridge.displays.escvp as escvp_mod
from cuemspowerbridge.displays.base import DeviceDef
from cuemspowerbridge.displays.escvp import EscVpDriver, EscVpError

_HELLO = b"ESC/VP.net"


def _resp(status: int, nhdr: int = 0) -> bytes:
    """Build a 16-byte ESC/VP.net response header (+ nhdr*18 garbage trailers)."""
    head = _HELLO + bytes([0x10, 0x03, 0x00, 0x00, status, nhdr])
    return head + (b"\x00" * (nhdr * 18))


async def _serve(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, port


def _driver(port, password="", timeout=2.0):
    dev = DeviceDef(name="t", host="127.0.0.1", brightness=True,
                    escvp_port=port, escvp_password=password)
    return EscVpDriver(dev, timeout_s=timeout)


async def test_hello_with_password_framing():
    seen = {}

    async def handler(reader, writer):
        hello = await reader.readexactly(16)
        seen["hello"] = hello
        seen["pwhdr"] = await reader.readexactly(hello[15] * 18)
        writer.write(_resp(0x20))
        await writer.drain()
        seen["cmd"] = await reader.readuntil(b"\r")
        writer.write(b":")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port, password="akuka1ak").send_command("BRIGHT 50")
    assert seen["hello"] == _HELLO + bytes([0x10, 0x03, 0x00, 0x00, 0x00, 0x01])
    # 18-byte password header: id 0x01, attr 0x01, 16-byte NUL-padded password.
    assert seen["pwhdr"] == bytes([0x01, 0x01]) + b"akuka1ak".ljust(16, b"\x00")
    assert seen["cmd"] == b"BRIGHT 50\r"


async def test_hello_empty_password_sends_no_trailing_header():
    seen = {}

    async def handler(reader, writer):
        hello = await reader.readexactly(16)
        seen["hello"] = hello
        # Respond to the HELLO first (the driver waits for this before sending
        # the command). With no password, NOTHING must follow the 16-byte hello
        # before the command — so reading until '\r' yields exactly the command,
        # not 18 stray password-header bytes.
        writer.write(_resp(0x20))
        await writer.drain()
        seen["cmd"] = await reader.readuntil(b"\r")
        writer.write(b":")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port, password="").send_command("BRIGHT 0")
    assert seen["hello"][15] == 0x00
    assert seen["cmd"] == b"BRIGHT 0\r"


async def test_response_trailing_headers_drained():
    # Server replies OK with nhdr=2 trailing headers; the driver must drain
    # 36 bytes before the command reply, else the next read is corrupted.
    seen = {}

    async def handler(reader, writer):
        await reader.readexactly(16)
        await reader.readexactly(18)  # password header
        writer.write(_resp(0x20, nhdr=2))
        await writer.drain()
        seen["cmd"] = await reader.readuntil(b"\r")
        writer.write(b":")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port, password="pw").send_command("BRIGHT 50")
    assert seen["cmd"] == b"BRIGHT 50\r"


@pytest.mark.parametrize("status", [0x41, 0x43, 0x45])
async def test_handshake_refusals_raise_and_dont_retry(monkeypatch, status):
    monkeypatch.setattr(escvp_mod, "_RETRY_DELAYS", (0, 0))
    calls = {"n": 0}

    async def handler(reader, writer):
        calls["n"] += 1
        await reader.readexactly(16)
        await reader.readexactly(18)
        writer.write(_resp(status))
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        with pytest.raises(EscVpError):
            await _driver(port, password="pw").send_command("BRIGHT 0")
    # Deterministic protocol errors are NOT retried.
    assert calls["n"] == 1


async def test_reply_err_raises():
    async def handler(reader, writer):
        await reader.readexactly(16)
        writer.write(_resp(0x20))
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b"ERR\r:")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        with pytest.raises(EscVpError):
            await _driver(port).send_command("BRIGHT 0")


async def test_reply_query_value_is_ok():
    # A 'VERB=value:' reply (no ERR) must succeed — send_command is generic.
    async def handler(reader, writer):
        await reader.readexactly(16)
        writer.write(_resp(0x20))
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b"BRIGHT=50:")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        await _driver(port).send_command("BRIGHT?")  # no exception


async def test_crlf_in_command_rejected_before_io():
    # Nothing listening; the CR/LF guard must trip before any connection.
    drv = EscVpDriver(DeviceDef(name="x", host="127.0.0.1", escvp_port=1),
                      timeout_s=0.5)
    with pytest.raises(ValueError):
        await drv.send_command("BRIGHT 0\rPWR OFF")


async def test_hang_bounded_by_timeout(monkeypatch):
    monkeypatch.setattr(escvp_mod, "_RETRY_DELAYS", ())  # single attempt

    async def handler(reader, writer):
        await asyncio.sleep(5)  # accept socket, never respond

    server, port = await _serve(handler)
    async with server:
        with pytest.raises(EscVpError):
            await _driver(port, timeout=0.3).send_command("BRIGHT 0")


async def test_dry_run_makes_no_connection():
    drv = EscVpDriver(DeviceDef(name="x", host="127.0.0.1", escvp_port=1),
                      timeout_s=0.5, dry_run=True)
    await drv.send_command("BRIGHT 0")  # must not attempt to connect


async def test_sequential_calls_reconnect():
    conns = {"n": 0}

    async def handler(reader, writer):
        conns["n"] += 1
        await reader.readexactly(16)
        writer.write(_resp(0x20))
        await writer.drain()
        await reader.readuntil(b"\r")
        writer.write(b":")
        await writer.drain()
        writer.close()

    server, port = await _serve(handler)
    async with server:
        drv = _driver(port)
        await drv.send_command("BRIGHT 0")
        await drv.send_command("BRIGHT 50")
    assert conns["n"] == 2  # one TCP session per command
