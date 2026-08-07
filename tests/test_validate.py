"""Tests for plausibility checking: arraysense.validate."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arraysense.models import BatteryModuleSample, Sample
from arraysense.validate import validate_sample


def _ts() -> datetime:
    return datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _healthy_sample(**overrides: float) -> Sample:
    readings = {
        "pv_total_power_w": 6400.0,
        "load_power_w": 1850.0,
        "grid_power_w": -3200.0,
        "battery_power_w": 4550.0,
        "battery_voltage_v": 53.4,
        "battery_soc_pct": 71.0,
        "battery_temperature_c": 21.7,
    }
    readings.update(overrides)
    return Sample(
        timestamp=_ts(),
        readings=readings,
        battery_modules=(
            BatteryModuleSample(
                serial="BA12345671", slot=1, soc_pct=71.0, voltage_v=53.4, temperature_c=21.5
            ),
            BatteryModuleSample(
                serial="BA12345672", slot=2, soc_pct=70.0, voltage_v=53.3, temperature_c=21.9
            ),
        ),
    )


def test_plausible_sample_reports_no_failures() -> None:
    result = validate_sample(_healthy_sample())
    assert result.ok
    assert result.failures == ()


def test_implausible_battery_power_is_flagged() -> None:
    # The reference product recorded 25,583 W as fact, about double what the
    # hardware can deliver. Nothing checked it; this is that check.
    result = validate_sample(_healthy_sample(battery_power_w=25583.0))

    assert not result.ok
    assert len(result.failures) == 1
    failure = result.failures[0]
    assert failure.metric == "battery_power_w"
    assert failure.value == 25583.0
    assert failure.serial is None
    assert "25583" in failure.reason
    assert "20000" in failure.reason


def test_implausible_value_is_flagged_not_dropped_or_clamped() -> None:
    # An implausible value is evidence of a decode bug, so the sample keeps it
    # exactly as received; validation reports, it never edits.
    sample = _healthy_sample(battery_power_w=25583.0)
    validate_sample(sample)
    assert sample.readings["battery_power_w"] == 25583.0


def test_module_failure_names_the_module_and_the_slot_metric() -> None:
    sample = Sample(
        timestamp=_ts(),
        readings={},
        battery_modules=(
            BatteryModuleSample(serial="BA12345671", slot=1, soc_pct=68.0),
            BatteryModuleSample(serial="BA12345673", slot=3, soc_pct=137.0),
        ),
    )

    result = validate_sample(sample)

    assert len(result.failures) == 1
    failure = result.failures[0]
    # A module in slot 3 checks against the slot-3 spec, but is attributed to
    # its serial: slots rotate, serials do not.
    assert failure.metric == "battery_module3_soc_pct"
    assert failure.serial == "BA12345673"
    assert failure.value == 137.0
    assert "BA12345673" in failure.reason
    assert "100" in failure.reason


def test_same_module_flagged_by_serial_after_rotating_slots() -> None:
    # The inverter rotates modules through four register slots. The spec used
    # follows the slot; the attribution follows the serial.
    in_slot_2 = Sample(
        timestamp=_ts(),
        readings={},
        battery_modules=(BatteryModuleSample(serial="BA12345673", slot=2, soh_pct=142.0),),
    )
    in_slot_4 = Sample(
        timestamp=_ts(),
        readings={},
        battery_modules=(BatteryModuleSample(serial="BA12345673", slot=4, soh_pct=142.0),),
    )

    first = validate_sample(in_slot_2).failures[0]
    second = validate_sample(in_slot_4).failures[0]

    assert first.metric == "battery_module2_soh_pct"
    assert second.metric == "battery_module4_soh_pct"
    assert first.serial == second.serial == "BA12345673"


def test_failed_poll_validates_trivially() -> None:
    # A failed poll is a recorded gap carrying no readings: there is nothing to
    # check, and the gap itself is not a plausibility failure.
    result = validate_sample(Sample.failed(_ts(), "inverter unreachable"))
    assert result.ok
    assert result.failures == ()


def test_absent_inverter_reading_is_not_a_failure() -> None:
    # A metric the inverter did not report is absent, not zero, and absence is
    # not implausible.
    result = validate_sample(Sample(timestamp=_ts(), readings={"battery_soc_pct": 64.0}))
    assert result.ok


def test_module_reading_of_none_is_not_a_failure() -> None:
    # CAN comms down: the BMS reported nothing. That is a gap, not a 0% SOC and
    # not a bounds failure.
    sample = Sample(
        timestamp=_ts(),
        readings={},
        battery_modules=(BatteryModuleSample(serial="BA12345671", slot=1),),
    )
    result = validate_sample(sample)
    assert result.ok
    assert result.failures == ()


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("battery_soc_pct", 0.0),
        ("battery_soc_pct", 100.0),
        ("battery_power_w", -20000.0),
        ("battery_power_w", 20000.0),
        ("grid_voltage_v", 0.0),
    ],
)
def test_value_exactly_on_a_bound_is_accepted(metric: str, value: float) -> None:
    # Bounds are inclusive: a pack at a true 100% must not be flagged, and a
    # grid voltage of 0 during an outage is the event worth recording.
    result = validate_sample(Sample(timestamp=_ts(), readings={metric: value}))
    assert result.ok


@pytest.mark.parametrize("value", [0.0, 100.0])
def test_module_value_exactly_on_a_bound_is_accepted(value: float) -> None:
    sample = Sample(
        timestamp=_ts(),
        readings={},
        battery_modules=(BatteryModuleSample(serial="BA12345672", slot=2, soc_pct=value),),
    )
    assert validate_sample(sample).ok


def test_inverter_and_module_failures_are_partitioned() -> None:
    sample = Sample(
        timestamp=_ts(),
        readings={"battery_power_w": 25583.0, "battery_voltage_v": 53.4},
        battery_modules=(
            BatteryModuleSample(serial="BA12345671", slot=1, soc_pct=71.0),
            BatteryModuleSample(serial="BA12345672", slot=2, voltage_v=94.0),
        ),
    )

    result = validate_sample(sample)

    assert [failure.metric for failure in result.inverter_failures] == ["battery_power_w"]
    assert [failure.metric for failure in result.module_failures] == ["battery_module2_voltage_v"]
    assert result.module_failures[0].serial == "BA12345672"


def test_failure_reason_names_both_bounds_and_the_unit() -> None:
    result = validate_sample(_healthy_sample(battery_temperature_c=250.0))
    reason = result.failures[0].reason
    assert "-40" in reason
    assert "100" in reason
    assert "\N{DEGREE SIGN}C" in reason


def test_failure_carries_the_spec_it_was_checked_against() -> None:
    result = validate_sample(_healthy_sample(battery_power_w=25583.0))
    spec = result.failures[0].spec
    assert spec.name == "battery_power_w"
    assert (spec.lower, spec.upper) == (-20000.0, 20000.0)


def test_unknown_metric_name_raises() -> None:
    # A reading naming no registered metric is a caller bug, not a gap: it must
    # surface rather than be silently skipped as "nothing to check".
    sample = Sample(timestamp=_ts(), readings={"battery_powr_w": 100.0})
    with pytest.raises(KeyError):
        validate_sample(sample)
