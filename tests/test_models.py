"""Tests for the wire-independent sample model: arraysense.models."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arraysense.models import BatteryModuleSample, Sample


def _ts() -> datetime:
    return datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def test_sample_holds_inverter_readings() -> None:
    sample = Sample(
        timestamp=_ts(),
        readings={"pv_total_power_w": 1200.0, "battery_power_w": -500.0},
    )
    assert sample.readings["pv_total_power_w"] == 1200.0
    assert sample.readings["battery_power_w"] == -500.0
    assert sample.timestamp == _ts()
    assert sample.battery_modules == ()


def test_sample_carries_battery_modules() -> None:
    module = BatteryModuleSample(serial="BA12345678", slot=1, soc_pct=87.0)
    sample = Sample(timestamp=_ts(), readings={}, battery_modules=(module,))
    assert sample.battery_modules == (module,)


def test_cell_delta_computed_from_extremes() -> None:
    module = BatteryModuleSample(
        serial="BA12345678",
        slot=1,
        cell_max_voltage_v=3.5,
        cell_min_voltage_v=3.2,
    )
    assert module.cell_delta_v == pytest.approx(0.3)


def test_cell_delta_unknown_when_bms_reports_nothing() -> None:
    module = BatteryModuleSample(serial="BA12345678", slot=1)
    # No extremes reported: delta is unknown, never a zeroed reading.
    assert module.cell_delta_v is None


def test_cell_delta_unknown_when_only_one_extreme_reported() -> None:
    module = BatteryModuleSample(serial="BA12345678", slot=1, cell_max_voltage_v=3.5)
    assert module.cell_delta_v is None


def test_cell_delta_zero_when_extremes_equal() -> None:
    module = BatteryModuleSample(
        serial="BA12345678",
        slot=1,
        cell_max_voltage_v=3.3,
        cell_min_voltage_v=3.3,
    )
    # A balanced pack is a real zero, not an unknown.
    assert module.cell_delta_v == 0.0


def test_cell_delta_zero_is_distinct_from_unknown() -> None:
    balanced = BatteryModuleSample(
        serial="BA12345678", slot=1, cell_max_voltage_v=3.3, cell_min_voltage_v=3.3
    )
    unreported = BatteryModuleSample(serial="BA12345678", slot=1)
    assert balanced.cell_delta_v == 0.0
    assert unreported.cell_delta_v is None


def test_failed_poll_is_identifiable_and_carries_reason() -> None:
    failed = Sample.failed(_ts(), "inverter unreachable")
    assert failed.is_failed
    assert failed.error == "inverter unreachable"
    # A failed poll has no readings — not zeroed readings.
    assert failed.readings == {}
    assert failed.battery_modules == ()


def test_successful_poll_is_not_mistaken_for_failed() -> None:
    sample = Sample(timestamp=_ts(), readings={})
    assert not sample.is_failed
    assert sample.error is None


def test_naive_timestamp_is_rejected() -> None:
    with pytest.raises(ValueError):
        Sample(timestamp=datetime(2026, 8, 6, 12, 0), readings={})


def test_slot_must_be_one_based_within_range() -> None:
    # The registry names columns battery_module1..4; pylxpweb reports a 0-based
    # battery_index, so an adapter must add one. Catch the off-by-one here
    # rather than writing to a column that does not exist.
    for bad in (0, 5, -1):
        with pytest.raises(ValueError, match="slot"):
            BatteryModuleSample(serial="BA12345678", slot=bad)


def test_empty_serial_is_rejected() -> None:
    # Serial is the identity; an empty one would collapse distinct modules.
    with pytest.raises(ValueError, match="serial"):
        BatteryModuleSample(serial="", slot=1)


def test_cell_extreme_indices_are_carried() -> None:
    module = BatteryModuleSample(
        serial="BA12345678",
        slot=1,
        cell_max_voltage_v=3.451,
        cell_min_voltage_v=3.383,
        cell_max_voltage_num=7,
        cell_min_voltage_num=2,
    )
    assert module.cell_delta_v == pytest.approx(0.068)
    assert module.cell_max_voltage_num == 7
    assert module.cell_min_voltage_num == 2


def test_a_failed_poll_cannot_carry_readings() -> None:
    # A failure is a recorded gap, not a partial result. Allowing both would
    # produce a row that is simultaneously a measurement and an absence.
    with pytest.raises(ValueError, match="failed poll"):
        Sample(timestamp=_ts(), readings={"pv_total_power_w": 123.0}, error="timeout")


def test_a_failed_poll_cannot_carry_battery_modules() -> None:
    module = BatteryModuleSample(serial="BA12345678", slot=1, soc_pct=87.0)
    with pytest.raises(ValueError, match="failed poll"):
        Sample(timestamp=_ts(), readings={}, battery_modules=(module,), error="timeout")
