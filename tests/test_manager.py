# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""DisplayManager fan-out: per-device error isolation, snapshot integrity."""

from cuemspowerbridge.displays.base import (
    DeviceDef, DisplayDriver, DisplayError, DisplayUnconfirmed, PowerState,
)
from cuemspowerbridge.displays.manager import DisplayManager


class _StubDriver(DisplayDriver):
    def __init__(self, dev, *, timeout_s=5.0, dry_run=False, exc=None):
        super().__init__(dev, timeout_s=timeout_s, dry_run=dry_run)
        self.exc = exc          # exception instance to raise, or None
        self.calls: list[str] = []

    async def power_on(self):
        self.calls.append("on")
        if self.exc:
            raise self.exc

    async def power_off(self):
        self.calls.append("off")
        if self.exc:
            raise self.exc

    async def power_status(self):
        return PowerState.ON


def _manager_with(drivers):
    m = DisplayManager([])  # real constructor, empty fleet
    m._drivers = drivers
    m._states = [PowerState.UNKNOWN] * len(drivers)
    return m


async def test_one_failure_does_not_sink_others():
    ok = _StubDriver(DeviceDef(name="ok", host="a"))
    bad = _StubDriver(DeviceDef(name="bad", host="b"), exc=DisplayError("boom"))
    m = _manager_with([ok, bad])

    await m.power_on_all()  # must NOT raise

    assert ok.calls == ["on"]
    assert bad.calls == ["on"]  # the failing one was still attempted
    snap = {d["name"]: d["power"] for d in m.snapshot()}
    assert snap["ok"] == "on"
    assert snap["bad"] == "unknown"  # failure leaves it unknown, not "on"


async def test_unconfirmed_marks_unknown_not_target():
    # DisplayUnconfirmed (e.g. ERR3 warmup) must NOT be recorded as the
    # optimistic target state.
    dev = _StubDriver(DeviceDef(name="warm", host="a"),
                      exc=DisplayUnconfirmed("busy"))
    m = _manager_with([dev])
    await m.power_off_all()
    assert m.snapshot() == [{"name": "warm", "power": "unknown"}]


async def test_duplicate_labels_not_collapsed():
    # Two devices sharing a label must remain distinct entries (regression:
    # a label-keyed dict would collapse them to one).
    a = _StubDriver(DeviceDef(name="Stage", host="a"))
    b = _StubDriver(DeviceDef(name="Stage", host="b"), exc=DisplayError("x"))
    m = _manager_with([a, b])
    await m.power_on_all()
    snap = m.snapshot()
    assert len(snap) == 2
    assert snap[0] == {"name": "Stage", "power": "on"}
    assert snap[1] == {"name": "Stage", "power": "unknown"}


async def test_power_off_snapshot():
    ok = _StubDriver(DeviceDef(name="p", host="a"))
    m = _manager_with([ok])
    await m.power_off_all()
    assert ok.calls == ["off"]
    assert m.snapshot() == [{"name": "p", "power": "off"}]


async def test_status_all_returns_snapshot_shape():
    a = _StubDriver(DeviceDef(name="a", host="x"))
    m = _manager_with([a])
    result = await m.status_all()
    assert result == [{"name": "a", "power": "on"}]
    assert m.snapshot() == result  # same shape + refreshed cache


async def test_empty_fleet_is_noop():
    m = DisplayManager([])
    assert not m.configured
    await m.power_on_all()
    await m.power_off_all()
    assert await m.status_all() == []
    assert m.snapshot() == []
