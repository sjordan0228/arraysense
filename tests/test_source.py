"""Tests for the inverter source interface: arraysense.collector.source."""

from __future__ import annotations

import pytest

from arraysense.collector.source import FakeSource, InverterSource


async def test_fake_source_satisfies_the_interface() -> None:
    assert isinstance(FakeSource(), InverterSource)


async def test_fake_source_yields_a_plausible_sample() -> None:
    source = FakeSource()
    await source.connect()
    assert source.connected
    sample = await source.read()
    await source.disconnect()
    assert not source.connected
    assert sample.readings["pv_total_power_w"] == 7614.0
    assert len(sample.battery_modules) == 4
    assert not sample.is_failed


async def test_modules_are_distinct_and_slots_are_one_based() -> None:
    # The registry names columns battery_module1..4 and BatteryModuleSample
    # rejects anything else, so a 0-based slot would fail loudly.
    sample = await FakeSource().read()
    assert {m.serial for m in sample.battery_modules} == {
        "Battery_ID_01",
        "Battery_ID_02",
        "Battery_ID_03",
        "Battery_ID_04",
    }
    assert sorted(m.slot for m in sample.battery_modules) == [1, 2, 3, 4]


async def test_cell_delta_is_realistic() -> None:
    # Real packs on the reference system sit around 4 mV of spread.
    sample = await FakeSource().read()
    for module in sample.battery_modules:
        assert module.cell_delta_v == pytest.approx(0.004)


async def test_connect_failure_propagates() -> None:
    source = FakeSource(fail_on_connect=ConnectionError("dongle busy"))
    with pytest.raises(ConnectionError, match="dongle busy"):
        await source.connect()
    assert not source.connected


async def test_read_failure_propagates() -> None:
    source = FakeSource(fail_on_read=ConnectionError("stream closed"))
    await source.connect()
    with pytest.raises(ConnectionError, match="stream closed"):
        await source.read()


async def test_a_bank_without_can_reports_no_modules() -> None:
    # The battery register block is populated from the CAN bus; without closed
    # loop it is empty. That must read as no modules, not as modules at zero.
    sample = await FakeSource(modules=0).read()
    assert sample.battery_modules == ()
    assert sample.readings["battery_soc_pct"] == 64.0


async def test_reads_are_counted() -> None:
    source = FakeSource()
    for _ in range(3):
        await source.read()
    assert source.reads == 3
