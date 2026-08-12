"""Tests for the energy counter read: arraysense.drivers.eg4_luxpower.source."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arraysense.config import Config
from arraysense.drivers.eg4_luxpower.source import Eg4LuxPowerSource

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
    s = Eg4LuxPowerSource(CFG, transport=t)
    sample = await s.read()
    assert sample.readings["pv_energy_today_kwh"] == 75.6
    assert sample.readings["load_energy_today_kwh"] == 95.3
    assert sample.readings["pv_energy_total_kwh"] == 36246.4


async def test_energy_is_not_re_read_on_every_poll() -> None:
    # The dongle takes one client and every extra round trip competes with the
    # poll that matters. A daily counter moves by hundredths of a kWh a minute,
    # so reading it at the poll rate buys nothing.
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=60.0)
    for _ in range(6):
        await s.read()
    assert t.runtime_reads == 6
    assert t.energy_reads == 1


async def test_the_cached_value_is_reused_between_reads() -> None:
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=60.0)
    first = await s.read()
    second = await s.read()
    assert second.readings["pv_energy_today_kwh"] == first.readings["pv_energy_today_kwh"]
    assert t.energy_reads == 1


async def test_the_cache_expires_and_is_re_read() -> None:
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=0.0)
    await s.read()
    await s.read()
    assert t.energy_reads == 2


async def test_a_failed_energy_read_does_not_fail_the_poll() -> None:
    # Energy is a supplement. Losing it must not cost the power readings, which
    # are the ones that matter every second.
    t = FakeTransport(energy_fails=True)
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=0.0)
    sample = await s.read()
    assert sample.readings["pv_total_power_w"] == 7614.0
    assert "pv_energy_today_kwh" not in sample.readings
    assert not sample.is_failed


async def test_a_stale_cache_is_dropped_rather_than_reported_as_current() -> None:
    # A counter nobody has been able to read for an hour is not today's total.
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=60.0)
    await s.read()
    s._energy_at = datetime.now(tz=UTC) - timedelta(hours=1)
    t._fails = True
    sample = await s.read()
    assert "pv_energy_today_kwh" not in sample.readings


async def test_absent_counters_are_omitted_not_zeroed() -> None:
    # Strings 4-6 and the generator do not exist on this hardware and report
    # None. A zero would say the generator ran and made nothing.
    t = FakeTransport(energy=SimpleNamespace(pv_energy_today=75.6, generator_energy_today=None))
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=0.0)
    sample = await s.read()
    assert sample.readings["pv_energy_today_kwh"] == 75.6
    assert "generator_energy_today_kwh" not in sample.readings


async def test_every_mapped_energy_metric_is_registered() -> None:
    from arraysense.drivers.eg4_luxpower.source import _ENERGY_METRICS
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
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=0.0)
    sample = await s.read()
    assert sample.readings["pv_energy_today_kwh"] == value


async def test_a_counter_cached_before_midnight_is_not_used_after_it() -> None:
    # The daily counters reset at the turn of the day and roll up with max, so a
    # 23:59 total attached to a 00:00 sample becomes the new day's high-water
    # mark and stays there for the rest of it.
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/Chicago")
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=3600.0)
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
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=3600.0)
    await s._read_energy(datetime(2026, 8, 6, 14, 0, tzinfo=tz))
    carried = await s._read_energy(datetime(2026, 8, 6, 14, 30, tzinfo=tz))
    assert carried["pv_energy_today_kwh"] == 75.6


# --- The day the counters reset on -----------------------------------------
#
# Every instant below is UTC, because that is what the poll loop hands the
# driver. The tests above that pass an already-local datetime cannot see this
# fault at all: .date() on a Chicago-zoned datetime is the Chicago date whether
# the guard converts or not.

_CHICAGO = replace(CFG, timezone="America/Chicago")


async def test_a_counter_cached_before_local_midnight_is_dropped_after_it() -> None:
    """The counters reset at the inverter's midnight, five hours before UTC's.

    Between the two midnights a guard comparing UTC dates believes it is still
    yesterday, so the cache carries the old day's totals into the new day —
    and the daily metrics roll up with max, so that stale high-water mark
    stands for the rest of the day.
    """
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(_CHICAGO, transport=t, energy_interval=3600.0)
    before = datetime(2026, 8, 7, 4, 59, tzinfo=UTC)  # 23:59 the previous day, in Chicago
    after = datetime(2026, 8, 7, 5, 1, tzinfo=UTC)  # 00:01, the new day
    assert await s._read_energy(before)
    carried = await s._read_energy(after)
    assert "pv_energy_today_kwh" not in carried, "yesterday's daily total crossed local midnight"
    assert carried["pv_energy_total_kwh"] == 36246.4


async def test_a_counter_cached_before_utc_midnight_survives_it() -> None:
    """UTC's midnight is not the owner's, and must not drop a live cache.

    The mirror of the fault above: an evening in Chicago spans UTC midnight, so
    a UTC comparison throws away counters that are still today's.
    """
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(_CHICAGO, transport=t, energy_interval=10800.0)
    evening = datetime(2026, 8, 7, 23, 0, tzinfo=UTC)  # 18:00 in Chicago
    later = datetime(2026, 8, 8, 1, 0, tzinfo=UTC)  # 20:00, the same Chicago day
    assert await s._read_energy(evening)
    carried = await s._read_energy(later)
    assert carried["pv_energy_today_kwh"] == 75.6, "the cache was dropped at UTC's midnight"


async def test_the_boundary_follows_a_23_hour_day() -> None:
    """Spring forward: the owner's midnight arrives an hour earlier in UTC.

    8 March 2026 is 23 hours long in Chicago. Its midnight is 06:00 UTC, and
    the guard has to cut there rather than at a fixed offset held from the day
    before.
    """
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(_CHICAGO, transport=t, energy_interval=3600.0)
    before = datetime(2026, 3, 8, 5, 59, tzinfo=UTC)  # 23:59 on the 7th, CST
    after = datetime(2026, 3, 8, 6, 1, tzinfo=UTC)  # 00:01 on the 8th, CST
    assert await s._read_energy(before)
    carried = await s._read_energy(after)
    assert "pv_energy_today_kwh" not in carried, "the 23-hour day's midnight was missed"


async def test_the_boundary_follows_a_25_hour_day() -> None:
    """Fall back: the same day holds 01:30 twice, and ends an hour later in UTC.

    1 November 2026 is 25 hours long in Chicago. The repeated hour is still the
    same local day — the cache must survive it — and the day does not end until
    06:00 UTC on the 2nd.
    """
    # Three hours of cache lifetime, so an hour's gap neither refreshes the
    # read nor expires it: what is under test is the boundary, not the clock.
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(_CHICAGO, transport=t, energy_interval=10800.0)
    first = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)  # 01:30 CDT
    repeated = datetime(2026, 11, 1, 7, 30, tzinfo=UTC)  # 01:30 again, CST
    assert await s._read_energy(first)
    assert (await s._read_energy(repeated))["pv_energy_today_kwh"] == 75.6, (
        "the repeated hour was read as a new day"
    )

    t2 = FakeTransport(energy=_energy())
    s2 = Eg4LuxPowerSource(_CHICAGO, transport=t2, energy_interval=10800.0)
    late = datetime(2026, 11, 2, 5, 30, tzinfo=UTC)  # 23:30 on the 1st, CST
    next_day = datetime(2026, 11, 2, 6, 30, tzinfo=UTC)  # 00:30 on the 2nd
    assert await s2._read_energy(late)
    carried = await s2._read_energy(next_day)
    assert "pv_energy_today_kwh" not in carried, "the 25-hour day's midnight was missed"


async def test_an_unconfigured_installation_follows_the_hosts_clock() -> None:
    """With no zone stated, the day is the one the machine itself keeps.

    Every installation is unconfigured until the wizard asks, and the collector
    runs at the site — on the reference Pi the two agree. Read live rather than
    captured as an offset, so it stays right across a daylight-saving change.
    """
    t = FakeTransport(energy=_energy())
    s = Eg4LuxPowerSource(CFG, transport=t, energy_interval=3600.0)
    moment = datetime(2026, 8, 7, 4, 59, tzinfo=UTC)
    assert s._local_day(moment) == moment.astimezone().date()
