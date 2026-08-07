"""Tests for the pylxpweb adapter: arraysense.collector.pylxp_source.

The values here are from one real read of an EG4 18kPV with four PowerPro
modules, taken at 2026-08-06T20:44:56Z. They are kept because several of the
mappings this file guards were wrong in ways only real hardware exposed.
"""

from __future__ import annotations

from types import SimpleNamespace

from arraysense.collector.pylxp_source import sample_from_pylxp
from arraysense.metrics import lookup


def _runtime(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "pv1_power": 2253.0,
        "pv1_voltage": 372.9,
        "pv1_current": 6.04,
        "output_power": 2357.0,
        "eps_power": 2391.0,
        "eps_l1_power": 1937,
        "eps_l2_power": 456,
        # Register 27. The library calls this load_power; the vendor calls it
        # power imported from grid, and the vendor is right.
        "load_power": 0.0,
        "power_from_grid": 0.0,
        "power_to_grid": 0.0,
        "radiator_temperature_1": 68.0,
        "radiator_temperature_2": 71.0,
        "internal_temperature": 59.0,
        "bms_max_cell_temperature": 39.0,
        "bms_min_cell_temperature": 38.0,
        # The library's own battery_temperature field, which returns an
        # undecoded register rather than a temperature.
        "battery_temperature": 11880.0,
        "bms_allow_charge": True,
        "bms_allow_discharge": True,
        "bms_force_charge": False,
        # Three-phase registers on split-phase hardware: there is no third
        # phase for them to measure and they report nonsense.
        "eps_voltage_s": 6.4,
        "eps_voltage_t": 1545.9,
        "temperature_t2": 0.0,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_house_load_comes_from_register_170_not_the_libraries_load_field() -> None:
    # The library's load_power is grid import. Reading it as house load shows a
    # house consuming nothing whenever the system runs off solar and battery,
    # which is most of a sunny day.
    sample = sample_from_pylxp(_runtime(), bank=None)
    assert sample.readings["load_power_w"] == 2357.0


def test_house_load_falls_back_to_eps_when_firmware_omits_register_170() -> None:
    runtime = _runtime()
    del runtime.output_power
    sample = sample_from_pylxp(runtime, bank=None)
    assert sample.readings["load_power_w"] == 2391.0


def test_house_load_is_absent_when_the_inverter_reports_neither() -> None:
    # Absent, not zero. A missing reading is a gap in the chart; a zero is a
    # claim that the house drew nothing.
    runtime = _runtime()
    del runtime.output_power
    del runtime.eps_power
    assert "load_power_w" not in sample_from_pylxp(runtime, bank=None).readings


def test_battery_temperature_comes_from_the_hottest_cell() -> None:
    # Not from the library's battery_temperature, which reads 11880 on real
    # hardware while the cell extremes beside it are correct.
    readings = sample_from_pylxp(_runtime(), bank=None).readings
    assert readings["battery_temperature_c"] == 39.0
    assert readings["battery_min_cell_temperature_c"] == 38.0


def test_bms_permissions_are_stored_as_flags() -> None:
    readings = sample_from_pylxp(_runtime(), bank=None).readings
    assert readings["bms_allow_charge"] == 1.0
    assert readings["bms_allow_discharge"] == 1.0
    assert readings["bms_force_charge"] == 0.0


def test_an_unreported_permission_is_absent_rather_than_denied() -> None:
    # A CAN dropout means the BMS said nothing, not that it withheld consent.
    # Storing 0 would show the pack blocking charge during every dropout.
    runtime = _runtime(bms_allow_charge=None)
    assert "bms_allow_charge" not in sample_from_pylxp(runtime, bank=None).readings


def test_the_bank_overrides_the_inverters_own_battery_readings() -> None:
    # Both sources report the same quantities; the BMS measures at the cells
    # and the inverter at its terminals, so the BMS wins where both answered.
    bank = SimpleNamespace(
        max_cell_temperature=41.0,
        min_cell_temperature=40.0,
        current_capacity=717.0,
        max_capacity=1120.0,
        battery_count=4,
        allow_charge=False,
        batteries=[],
    )
    readings = sample_from_pylxp(_runtime(), bank).readings
    assert readings["battery_temperature_c"] == 41.0
    assert readings["battery_remaining_capacity_ah"] == 717.0
    assert readings["battery_module_count"] == 4.0
    assert readings["bms_allow_charge"] == 0.0


def test_split_phase_grid_flow_is_signed_per_leg() -> None:
    # Import on one leg and export on the other nets to nearly zero in the
    # combined figure, which reads as a balanced house when it is the opposite.
    runtime = _runtime(
        grid_import_power_l1=1200,
        grid_export_power_l1=0,
        grid_import_power_l2=0,
        grid_export_power_l2=900,
    )
    readings = sample_from_pylxp(runtime, bank=None).readings
    assert readings["grid_power_l1_w"] == 1200.0
    assert readings["grid_power_l2_w"] == -900.0


def test_three_phase_registers_are_not_mapped_on_split_phase_hardware() -> None:
    # eps_voltage_s reads 6.4 V and eps_voltage_t reads 1545.9 V because there
    # is no third phase. Neither may reach a chart.
    readings = sample_from_pylxp(_runtime(), bank=None).readings
    assert not any(v in (6.4, 1545.9) for v in readings.values())


def test_every_mapped_reading_is_within_its_registered_bounds() -> None:
    for name, value in sample_from_pylxp(_runtime(), bank=None).readings.items():
        spec = lookup(name)
        assert spec.within_bounds(value), f"{name}={value} outside {spec.lower}..{spec.upper}"


def test_module_capacity_and_limits_are_carried_through() -> None:
    # Percent hides energy: two modules at the same state of charge can hold
    # different amp-hours, and a module throttling its own charge current is
    # what stalls the whole bank.
    record = SimpleNamespace(
        serial_number="Battery_ID_03",
        battery_index=2,
        soc=57,
        current_capacity=159.6,
        max_capacity=280.0,
        charge_current_limit=200.0,
        discharge_current_limit=200.0,
        status=49156,
        fault_code=0,
        warning_code=0,
    )
    bank = SimpleNamespace(batteries=[record])
    (module,) = sample_from_pylxp(_runtime(), bank).battery_modules
    assert module.slot == 3
    assert module.remaining_capacity_ah == 159.6
    assert module.full_capacity_ah == 280.0
    assert module.charge_current_limit_a == 200.0
    assert module.status_code == 49156


def test_a_can_down_bank_does_not_report_zero_percent() -> None:
    # The library's decoder reads registers, and a BMS whose CAN link is down
    # leaves them zero-filled — so the bank arrives with soc=0, soh=100 and
    # zero capacities. A full set of numbers, all of them false.
    dead = SimpleNamespace(
        voltage=53.7,
        soc=0,
        soh=100,
        max_capacity=0.0,
        current_capacity=0.0,
        max_cell_voltage=0.0,
        min_cell_voltage=0.0,
        battery_count=0,
        charge_power=0.0,
        discharge_power=0.0,
        batteries=[],
    )
    readings = sample_from_pylxp(_runtime(), dead).readings
    assert "battery_soc_pct" not in readings
    assert "battery_module_count" not in readings
    # The inverter's own readings are unaffected — only the BMS block is dropped.
    assert readings["pv1_power_w"] == 2253.0


def test_a_module_holding_only_zeroes_is_dropped_even_with_a_serial() -> None:
    # A slot keeps its serial from an earlier read after its link drops. A pack
    # reading 0.000 V per cell is not flat, it is silent.
    bank = SimpleNamespace(
        max_cell_voltage=3.36,
        min_cell_voltage=3.35,
        max_capacity=1120.0,
        batteries=[
            SimpleNamespace(
                serial_number="Battery_ID_01",
                battery_index=0,
                soc=64,
                max_cell_voltage=3.364,
                min_cell_voltage=3.358,
                max_capacity=280.0,
            ),
            SimpleNamespace(
                serial_number="Battery_ID_02",
                battery_index=1,
                soc=0,
                max_cell_voltage=0.0,
                min_cell_voltage=0.0,
                max_capacity=0.0,
            ),
        ],
    )
    modules = sample_from_pylxp(_runtime(), bank).battery_modules
    assert [m.serial for m in modules] == ["Battery_ID_01"]
