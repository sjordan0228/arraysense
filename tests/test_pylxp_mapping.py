"""Tests for the pylxpweb adapter: arraysense.collector.pylxp_source.

The values here are from one real read of an EG4 18kPV with four PowerPro
modules, taken at 2026-08-06T20:44:56Z. They are kept because several of the
mappings this file guards were wrong in ways only real hardware exposed.
"""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest
from pylxpweb.transports.data import (
    BatteryBankData,
    InverterEnergyData,
    InverterRuntimeData,
)
from pylxpweb.transports.exceptions import TransportError

from arraysense.collector import pylxp_source
from arraysense.collector.pylxp_source import PylxpSource, sample_from_pylxp
from arraysense.config import Config
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


def test_a_state_of_health_of_exactly_100_is_not_stored() -> None:
    # pylxpweb rewrites a reported 0 to 100 on every path that produces SOH.
    # In 0.9.38: transports/data.py:635-636 on the runtime path, :1292 per
    # module, :1705-1707 on the bank. Zero is what a silent BMS reports, so a
    # stored 100 could be a healthy bank or one that answered nothing, and
    # nothing downstream can tell which.
    runtime = _runtime(battery_soh=100)
    assert "battery_soh_pct" not in sample_from_pylxp(runtime, bank=None).readings


def test_a_state_of_health_below_100_is_stored() -> None:
    # The rewrite only ever produces the literal 100, so every other value is a
    # measurement — including the falling one the column exists to show.
    readings = sample_from_pylxp(_runtime(battery_soh=97), bank=None).readings
    assert readings["battery_soh_pct"] == 97.0


def _healthy_bank(**overrides: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "max_cell_voltage": 3.364,
        "min_cell_voltage": 3.358,
        "max_capacity": 1120.0,
        "batteries": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_the_banks_state_of_health_is_refused_on_the_same_terms() -> None:
    # The bank decodes the same register 5 the runtime path does, through the
    # same rewrite, so it inherits the same ambiguity.
    assert "battery_soh_pct" not in sample_from_pylxp(_runtime(), _healthy_bank(soh=100)).readings
    kept = sample_from_pylxp(_runtime(), _healthy_bank(soh=94)).readings
    assert kept["battery_soh_pct"] == 94.0


def test_a_modules_state_of_health_is_refused_on_the_same_terms() -> None:
    def module(soh: int) -> SimpleNamespace:
        return SimpleNamespace(
            serial_number="Battery_ID_01",
            battery_index=0,
            soc=64,
            soh=soh,
            max_cell_voltage=3.364,
            min_cell_voltage=3.358,
            max_capacity=280.0,
        )

    (silent,) = sample_from_pylxp(
        _runtime(), _healthy_bank(batteries=[module(100)])
    ).battery_modules
    assert silent.soh_pct is None
    (worn,) = sample_from_pylxp(_runtime(), _healthy_bank(batteries=[module(91)])).battery_modules
    assert worn.soh_pct == 91.0


def test_module_fault_and_warning_codes_are_not_stored_at_all() -> None:
    # BatteryData declares both as int = 0 (data.py:1031-1032) and
    # from_modbus_registers passes neither, because no entry in
    # BATTERY_REGISTERS carries either quantity. Storing the default asserts
    # "no fault" about a pack nobody asked.
    record = SimpleNamespace(
        serial_number="Battery_ID_03",
        battery_index=2,
        soc=57,
        max_cell_voltage=3.364,
        min_cell_voltage=3.358,
        max_capacity=280.0,
        fault_code=0,
        warning_code=0,
    )
    (module,) = sample_from_pylxp(_runtime(), _healthy_bank(batteries=[record])).battery_modules
    assert module.fault_code is None
    assert module.warning_code is None


def test_every_mapped_attribute_exists_on_the_class_it_is_read_from() -> None:
    # _reading asks with a default, so a mapping naming an attribute that does
    # not exist contributes nothing and says nothing about it — for as long as
    # nobody checks. battery_voltage_inv_sample sat in the runtime table that
    # way: register 107 reaches us only through the bank, and RUNTIME_FIELD
    # maps its name to None.
    runtime = {f.name for f in fields(InverterRuntimeData)}
    bank = {f.name for f in fields(BatteryBankData)}
    energy = {f.name for f in fields(InverterEnergyData)}
    tables: tuple[tuple[str, tuple[tuple[str, str], ...], set[str]], ...] = (
        ("_RUNTIME_METRICS", pylxp_source._RUNTIME_METRICS, runtime),
        ("_RUNTIME_BATTERY_METRICS", pylxp_source._RUNTIME_BATTERY_METRICS, runtime),
        ("_RUNTIME_FLAGS", pylxp_source._RUNTIME_FLAGS, runtime),
        ("_BANK_METRICS", pylxp_source._BANK_METRICS, bank),
        ("_BANK_FLAGS", pylxp_source._BANK_FLAGS, bank),
        ("_ENERGY_METRICS", pylxp_source._ENERGY_METRICS, energy),
    )
    for label, table, declared in tables:
        for _, attribute in table:
            assert attribute in declared, f"{label} names {attribute}, which does not exist"
    for _, positive, negative in pylxp_source._RUNTIME_SIGNED_PAIRS:
        assert positive in runtime and negative in runtime


class _CrossedTransport:
    """A dongle that answers the wrong register a fixed number of times first.

    This is the real behaviour of the reference hardware, not an invention: it
    serves its vendor's cloud on the same socket and the replies cross, so a
    read of register 32 comes back carrying 127.
    """

    def __init__(self, misroutes: int, then: object = None) -> None:
        self.misroutes = misroutes
        self.then = then if then is not None else _runtime()
        self.calls = 0

    async def _answer(self) -> object:
        self.calls += 1
        if self.misroutes > 0:
            self.misroutes -= 1
            raise TransportError(
                "Failed to read register group 'status_energy': [1234567890] Response "
                "register mismatch: expected [tcp_func=0xc2 func=0x04 register=32], "
                "received [func=0x04 register=127] — likely a misrouted cloud response"
            )
        return self.then

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def read_runtime(self) -> object:
        return await self._answer()

    async def read_battery(self) -> object:
        return None

    async def read_energy(self) -> object:
        return None


def _source(transport: object) -> PylxpSource:
    return PylxpSource(
        Config(
            dongle_host="127.0.0.1",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path=":memory:",
        ),
        transport=transport,  # type: ignore[arg-type]
    )


async def test_a_crossed_reply_is_retried_rather_than_losing_the_sample() -> None:
    # A misrouted reply is not a broken connection. Treating it as one cost
    # 5.5% of polls in the first hour of live collection, against the 0.01%
    # the tool this replaced lost on the same dongle.
    transport = _CrossedTransport(misroutes=1)
    sample = await _source(transport).read()
    assert transport.calls == 2
    assert sample.readings["pv1_power_w"] == 2253.0


async def test_a_second_crossed_reply_is_not_retried_again() -> None:
    # Once is transient; twice is a fault, and the caller's backoff is the
    # right answer to a fault. Retrying harder here is how a poll loop turns an
    # unhappy inverter into a busy wait.
    transport = _CrossedTransport(misroutes=5)
    with pytest.raises(ConnectionError, match="reading from inverter failed"):
        await _source(transport).read()
    assert transport.calls == 2


async def test_a_closed_socket_is_not_retried_at_all() -> None:
    # Only the crossed-reply case is worth an immediate second attempt. A dead
    # connection retried immediately is a busy wait against an inverter that is
    # not there.
    class _Dead(_CrossedTransport):
        async def _answer(self) -> object:
            self.calls += 1
            raise TransportError("connection reset by peer")

    transport = _Dead(misroutes=0)
    with pytest.raises(ConnectionError):
        await _source(transport).read()
    assert transport.calls == 1


def test_a_state_of_health_of_zero_is_absence_not_a_dead_bank() -> None:
    # The filter refused the library's fabricated 100 and let a raw 0 through,
    # which is the same error wearing the opposite number. Zero is what a BMS
    # that answered nothing reports, not a bank with no health left — and
    # storing it would put an alarming figure on a column nobody measured.
    from arraysense.collector.pylxp_source import _measured_soh

    assert _measured_soh(SimpleNamespace(soh=0.0), "soh") is None
    assert _measured_soh(SimpleNamespace(soh=100.0), "soh") is None
    assert _measured_soh(SimpleNamespace(soh=None), "soh") is None
    # Everything a real degrading bank reports still arrives.
    assert _measured_soh(SimpleNamespace(soh=97.0), "soh") == 97.0
    assert _measured_soh(SimpleNamespace(soh=1.0), "soh") == 1.0
