"""Tests for the energy counter read: arraysense.collector.pylxp_source."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arraysense.collector.pylxp_source import PylxpSource
from arraysense.config import Config

CFG = Config(
    dongle_host="h",
    dongle_serial="s",
    inverter_serial="i",
    database_path=":memory:",
    poll_interval=11.0,
)


class FakeTransport:
    """Counts how often each read happens, so cadence is observable."""

    def __init__(self, energy: object | None = None, energy_fails: bool = False) -> None:
        self.runtime_reads = 0
        self.energy_reads = 0
        self._energy = energy
        self._fails = energy_fails

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def read_runtime(self) -> Any:
        self.runtime_reads += 1
        return SimpleNamespace(pv_total_power_w=None, pv_total_power=7614.0)

    async def read_battery(self) -> Any:
        return None

    async def read_energy(self) -> Any:
        self.energy_reads += 1
        if self._fails:
            raise OSError("dongle busy")
        return self._energy


def _energy(**kw: float) -> SimpleNamespace:
    base = dict(pv_energy_today=75.6, load_energy_today=95.3, pv_energy_total=36246.4)
    base.update(kw)
    return SimpleNamespace(**base)


async def test_energy_is_read_and_mapped() -> None:
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t)
    sample = await s.read()
    assert sample.readings["pv_energy_today_kwh"] == 75.6
    assert sample.readings["load_energy_today_kwh"] == 95.3
    assert sample.readings["pv_energy_total_kwh"] == 36246.4


async def test_energy_is_not_re_read_on_every_poll() -> None:
    # The dongle takes one client and every extra round trip competes with the
    # poll that matters. A daily counter moves by hundredths of a kWh a minute,
    # so reading it at the poll rate buys nothing.
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t, energy_interval=60.0)
    for _ in range(6):
        await s.read()
    assert t.runtime_reads == 6
    assert t.energy_reads == 1


async def test_the_cached_value_is_reused_between_reads() -> None:
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t, energy_interval=60.0)
    first = await s.read()
    second = await s.read()
    assert second.readings["pv_energy_today_kwh"] == first.readings["pv_energy_today_kwh"]
    assert t.energy_reads == 1


async def test_the_cache_expires_and_is_re_read() -> None:
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t, energy_interval=0.0)
    await s.read()
    await s.read()
    assert t.energy_reads == 2


async def test_a_failed_energy_read_does_not_fail_the_poll() -> None:
    # Energy is a supplement. Losing it must not cost the power readings, which
    # are the ones that matter every second.
    t = FakeTransport(energy_fails=True)
    s = PylxpSource(CFG, transport=t, energy_interval=0.0)
    sample = await s.read()
    assert sample.readings["pv_total_power_w"] == 7614.0
    assert "pv_energy_today_kwh" not in sample.readings
    assert not sample.is_failed


async def test_a_stale_cache_is_dropped_rather_than_reported_as_current() -> None:
    # A counter nobody has been able to read for an hour is not today's total.
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t, energy_interval=60.0)
    await s.read()
    s._energy_at = datetime.now(tz=UTC) - timedelta(hours=1)
    t._fails = True
    sample = await s.read()
    assert "pv_energy_today_kwh" not in sample.readings


async def test_absent_counters_are_omitted_not_zeroed() -> None:
    # Strings 4-6 and the generator do not exist on this hardware and report
    # None. A zero would say the generator ran and made nothing.
    t = FakeTransport(energy=SimpleNamespace(pv_energy_today=75.6, generator_energy_today=None))
    s = PylxpSource(CFG, transport=t, energy_interval=0.0)
    sample = await s.read()
    assert sample.readings["pv_energy_today_kwh"] == 75.6
    assert "generator_energy_today_kwh" not in sample.readings


async def test_every_mapped_energy_metric_is_registered() -> None:
    from arraysense.collector.pylxp_source import _ENERGY_METRICS
    from arraysense.metrics import lookup

    for name, _ in _ENERGY_METRICS:
        lookup(name)


@pytest.mark.parametrize("value", [-1.0, 2_000_001.0])
async def test_an_implausible_counter_is_still_captured_for_the_store_to_flag(
    value: float,
) -> None:
    # Bounds are enforced in the store, which records the reading and flags it.
    # The adapter's job is to report what the inverter said.
    t = FakeTransport(energy=SimpleNamespace(pv_energy_today=value))
    s = PylxpSource(CFG, transport=t, energy_interval=0.0)
    sample = await s.read()
    assert sample.readings["pv_energy_today_kwh"] == value


async def test_a_counter_cached_before_midnight_is_not_used_after_it() -> None:
    # The daily counters reset at the turn of the day and roll up with max, so a
    # 23:59 total attached to a 00:00 sample becomes the new day's high-water
    # mark and stays there for the rest of it.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Chicago")
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t, energy_interval=3600.0)
    before = datetime(2026, 8, 6, 23, 59, tzinfo=tz)
    after = datetime(2026, 8, 7, 0, 1, tzinfo=tz)
    assert await s._read_energy(before)
    carried = await s._read_energy(after)
    assert "pv_energy_today_kwh" not in carried
    # The lifetime counters are monotonic and cross the boundary safely.
    assert carried["pv_energy_total_kwh"] == 36246.4


async def test_a_counter_cached_earlier_the_same_day_is_still_used() -> None:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Chicago")
    t = FakeTransport(energy=_energy())
    s = PylxpSource(CFG, transport=t, energy_interval=3600.0)
    await s._read_energy(datetime(2026, 8, 6, 14, 0, tzinfo=tz))
    carried = await s._read_energy(datetime(2026, 8, 6, 14, 30, tzinfo=tz))
    assert carried["pv_energy_today_kwh"] == 75.6
