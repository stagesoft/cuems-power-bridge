# SPDX-FileCopyrightText: 2026 Stagelab Coop SCCL
# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileContributor: Ion Reguera <ion@stagelab.coop>

"""DisplayManager fan-out: per-device error isolation + snapshot."""

from cuemspowerbridge.displays.base import (
    DeviceDef, DisplayDriver, DisplayError, PowerState,
)
from cuemspowerbridge.displays.manager import DisplayManager


class _StubDriver(DisplayDriver):
    def __init__(self, dev, *, timeout_s=5.0, dry_run=False, fail=False):
        super().__init__(dev, timeout_s=timeout_s, dry_run=dry_run)
        self.fail = fail
        self.calls: list[str] = []

    async def power_on(self):
        self.calls.append("on")
        if self.fail:
            raise DisplayError("boom")

    async def power_off(self):
        self.calls.append("off")
        if self.fail:
            raise DisplayError("boom")

    async def power_status(self):
        return PowerState.ON


def _manager_with(drivers):
    m = DisplayManager([])  # real constructor, empty fleet
    m._drivers = drivers
    m._snapshot = {d.dev.label(): PowerState.UNKNOWN.value for d in drivers}
    return m


async def test_one_failure_does_not_sink_others():
    ok = _StubDriver(DeviceDef(name="ok", host="a"))
    bad = _StubDriver(DeviceDef(name="bad", host="b"), fail=True)
    m = _manager_with([ok, bad])

    await m.power_on_all()  # must NOT raise

    assert ok.calls == ["on"]
    assert bad.calls == ["on"]  # the failing one was still attempted
    snap = {d["name"]: d["power"] for d in m.snapshot()}
    assert snap["ok"] == "on"
    assert snap["bad"] == "unknown"  # failure leaves it unknown, not "on"


async def test_power_off_snapshot():
    ok = _StubDriver(DeviceDef(name="p", host="a"))
    m = _manager_with([ok])
    await m.power_off_all()
    assert ok.calls == ["off"]
    assert m.snapshot() == [{"name": "p", "power": "off"}]


async def test_status_all_refreshes_snapshot():
    a = _StubDriver(DeviceDef(name="a", host="x"))
    m = _manager_with([a])
    states = await m.status_all()
    assert states == {"a": PowerState.ON}
    assert m.snapshot() == [{"name": "a", "power": "on"}]


async def test_empty_fleet_is_noop():
    m = DisplayManager([])
    assert not m.configured
    await m.power_on_all()
    await m.power_off_all()
    assert await m.status_all() == {}
    assert m.snapshot() == []
