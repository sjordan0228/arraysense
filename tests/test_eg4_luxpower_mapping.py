"""Tests for the pylxpweb adapter: arraysense.drivers.eg4_luxpower.source.

The values here are from one real read of an EG4 18kPV with four PowerPro
modules, taken at 2026-08-06T20:44:56Z. They are kept because several of the
mappings this file guards were wrong in ways only real hardware exposed.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pylxpweb.registers.inverter_input import registers_for_model
from pylxpweb.transports._field_mappings import RUNTIME_FIELD
from pylxpweb.transports.data import (
    BatteryBankData,
    InverterEnergyData,
    InverterRuntimeData,
)
from pylxpweb.transports.exceptions import TransportError

from arraysense.calibration import PACK_COMPARE_MAX_SKEW
from arraysense.config import Config
from arraysense.drivers.base import SampleBuildError
from arraysense.drivers.eg4_luxpower import source as eg4_source
from arraysense.drivers.eg4_luxpower.source import Eg4LuxPowerSource, to_sample
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
        # Registers 101 and 102, the same pair the bank decodes. They are here
        # because they are what says the BMS answered at all: without them
        # every reading the BMS relays is held back, which is the point.
        "bms_max_cell_voltage": 3.364,
        "bms_min_cell_voltage": 3.358,
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
    sample = to_sample(_runtime(), bank=None)
    assert sample.readings["load_power_w"] == 2357.0


def test_house_load_falls_back_to_eps_when_firmware_omits_register_170() -> None:
    runtime = _runtime()
    del runtime.output_power
    sample = to_sample(runtime, bank=None)
    assert sample.readings["load_power_w"] == 2391.0


def test_house_load_is_absent_when_the_inverter_reports_neither() -> None:
    # Absent, not zero. A missing reading is a gap in the chart; a zero is a
    # claim that the house drew nothing.
    runtime = _runtime()
    del runtime.output_power
    del runtime.eps_power
    assert "load_power_w" not in to_sample(runtime, bank=None).readings


def test_battery_temperature_comes_from_the_hottest_cell() -> None:
    # Not from the library's battery_temperature, which reads 11880 on real
    # hardware while the cell extremes beside it are correct.
    readings = to_sample(_runtime(), bank=None).readings
    assert readings["battery_temperature_c"] == 39.0
    assert readings["battery_min_cell_temperature_c"] == 38.0


def test_bms_permissions_are_stored_as_flags() -> None:
    readings = to_sample(_runtime(), bank=None).readings
    assert readings["bms_allow_charge"] == 1.0
    assert readings["bms_allow_discharge"] == 1.0
    assert readings["bms_force_charge"] == 0.0


def test_an_unreported_permission_is_absent_rather_than_denied() -> None:
    # A CAN dropout means the BMS said nothing, not that it withheld consent.
    # Storing 0 would show the pack blocking charge during every dropout.
    runtime = _runtime(bms_allow_charge=None)
    assert "bms_allow_charge" not in to_sample(runtime, bank=None).readings


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
    readings = to_sample(_runtime(), bank).readings
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
    readings = to_sample(runtime, bank=None).readings
    assert readings["grid_power_l1_w"] == 1200.0
    assert readings["grid_power_l2_w"] == -900.0


def test_three_phase_registers_are_not_mapped_on_split_phase_hardware() -> None:
    # eps_voltage_s reads 6.4 V and eps_voltage_t reads 1545.9 V because there
    # is no third phase. Neither may reach a chart.
    readings = to_sample(_runtime(), bank=None).readings
    assert not any(v in (6.4, 1545.9) for v in readings.values())


def test_every_mapped_reading_is_within_its_registered_bounds() -> None:
    for name, value in to_sample(_runtime(), bank=None).readings.items():
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
    (module,) = to_sample(_runtime(), bank).battery_modules
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
    readings = to_sample(_runtime(), dead).readings
    assert "battery_soc_pct" not in readings
    assert "battery_module_count" not in readings
    # The inverter's own readings are unaffected — only the BMS block is dropped.
    assert readings["pv1_power_w"] == 2253.0


def _zero_filled_runtime() -> InverterRuntimeData:
    """The runtime object a read produces when every register answers zero.

    Built through the library's own factory rather than by hand, because the
    thing under test is what that factory does with a zero: ``read_scaled``
    returns 0.0 for a register that is present and reads 0, which is
    indistinguishable downstream from a register that measured zero. The
    address list comes from the library's register definitions so this stays
    true if it gains or loses registers.
    """
    addresses: set[int] = set()
    for definition in registers_for_model("EG4_HYBRID"):
        for offset in range(getattr(definition, "length", 1) or 1):
            addresses.add(definition.address + offset)
    return InverterRuntimeData.from_modbus_registers(
        dict.fromkeys(sorted(addresses), 0), split_phase=True
    )


def test_a_runtime_read_of_all_zeroes_stores_no_bms_reading() -> None:
    # Eighteen battery and BMS columns used to store 0.0 from this read —
    # fifteen measurements and three permission flags — because the registers
    # were present in the reply and read 0 rather than being left out of it.
    # State of charge was one of them, and a bank showing 0% is the failure
    # this project exists to avoid.
    readings = to_sample(_zero_filled_runtime(), bank=None).readings
    relayed = {metric for metric, _ in eg4_source._RUNTIME_BMS_METRICS}
    relayed |= {metric for metric, _ in eg4_source._RUNTIME_FLAGS}
    relayed.add("battery_soh_pct")
    assert not relayed & readings.keys()
    assert "battery_soc_pct" not in readings
    # The rest of the read is untouched: nothing about a silent BMS says the
    # inverter stopped reporting its own registers.
    assert "load_power_w" in readings


def test_only_the_inverters_own_terminal_readings_survive_a_silent_bms() -> None:
    # The cost of the gate, stated as a test rather than left to be discovered.
    # Registers 4 and 10/11 are the inverter's own measurements — the library
    # files them under category "runtime", not "bms" — so they are written
    # whatever the BMS did. On an all-zero read that means two battery columns
    # still store 0.0, and neither can be told from an idle bank at rest.
    readings = to_sample(_zero_filled_runtime(), bank=None).readings
    battery = {name for name in readings if name.startswith(("battery_", "bms_"))}
    assert battery == {"battery_voltage_v", "battery_power_w"}


def test_an_idle_bank_still_reports_zero_current_and_zero_power() -> None:
    # This is why the table is gated rather than unmapped. A bank sitting at
    # rest genuinely reads 0.0 A, and dropping every zero would throw that
    # away; the BMS having answered at all is what separates the two.
    readings = to_sample(_runtime(battery_current=0.0), bank=None).readings
    assert readings["battery_current_a"] == 0.0


def test_a_silent_bms_is_not_read_as_withholding_permission() -> None:
    # Register 95 decodes to three False flags when it reads 0, which stores as
    # "charge and discharge both refused" — a bank that answered nothing shown
    # as one that said no.
    runtime = _runtime(
        bms_max_cell_voltage=0.0,
        bms_min_cell_voltage=0.0,
        battery_capacity_ah=0.0,
        bms_allow_charge=False,
        bms_allow_discharge=False,
    )
    readings = to_sample(runtime, bank=None).readings
    assert "bms_allow_charge" not in readings
    assert "bms_allow_discharge" not in readings


def test_a_bank_answering_the_inverter_survives_a_missing_bank_object() -> None:
    # The runtime block is the only battery data a poll carries when the bank
    # read comes back None, so the gate must not cost it when the BMS is fine.
    readings = to_sample(_runtime(battery_soc=64, battery_capacity_ah=1120.0), bank=None).readings
    assert readings["battery_soc_pct"] == 64.0
    assert readings["battery_temperature_c"] == 39.0
    assert readings["battery_full_capacity_ah"] == 1120.0


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
    modules = to_sample(_runtime(), bank).battery_modules
    assert [m.serial for m in modules] == ["Battery_ID_01"]


# --- a pack the inverter did not surface this poll (#40) ------------------------
#
# The library's accumulator serves every pack it has ever seen on every read, with
# the registers of packs the firmware did not surface frozen at their last real
# values and `last_seen` left at the old stamp. A held block passes the witness
# gate above, because its cell voltages are the last real ones and so non-zero,
# and it is then written with the current poll's timestamp. Every guard downstream
# filters on that timestamp — calibration's 15-minute age and 2-minute skew — so a
# held pack always looks freshly read, and a stale voltage compared against a live
# one raises a wiring fault that is not there.

_POLL = datetime(2026, 8, 8, 20, 44, 56, tzinfo=UTC)


def _pack(serial: str, index: int, *, last_seen: datetime | None, soc: int = 64) -> SimpleNamespace:
    """One battery record shaped like the library's, with a staleness stamp."""
    return SimpleNamespace(
        serial_number=serial,
        battery_index=index,
        soc=soc,
        max_cell_voltage=3.364,
        min_cell_voltage=3.358,
        max_capacity=280.0,
        last_seen=last_seen,
    )


def _bank_of(*packs: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        max_cell_voltage=3.36,
        min_cell_voltage=3.35,
        max_capacity=1120.0,
        batteries=list(packs),
    )


def test_a_held_pack_is_absent_rather_than_stamped_with_this_poll() -> None:
    # Non-zero cell voltages, so the witness gate would pass it. What disqualifies
    # it is that the reading is a quarter of an hour old and would be stored as if
    # taken now.
    bank = _bank_of(_pack("Battery_ID_01", 0, last_seen=_POLL - timedelta(minutes=15)))
    assert to_sample(_runtime(), bank, _POLL).battery_modules == ()


def test_a_pack_read_this_poll_is_kept_with_its_readings() -> None:
    bank = _bank_of(_pack("Battery_ID_01", 0, last_seen=_POLL - timedelta(seconds=5)))
    modules = to_sample(_runtime(), bank, _POLL).battery_modules
    assert [m.serial for m in modules] == ["Battery_ID_01"]
    assert modules[0].soc_pct == 64.0


def test_a_pack_with_no_staleness_stamp_is_kept() -> None:
    # None is the dataclass default. A library build that does not stamp it must
    # not make every pack vanish — that trades a wrong reading for no reading,
    # which is worse.
    bank = _bank_of(_pack("Battery_ID_01", 0, last_seen=None))
    modules = to_sample(_runtime(), bank, _POLL).battery_modules
    assert [m.serial for m in modules] == ["Battery_ID_01"]


def test_only_the_live_pack_survives_a_bank_where_one_went_quiet() -> None:
    # Identified by serial, never by slot: the live pack is the second record.
    bank = _bank_of(
        _pack("Battery_ID_01", 0, last_seen=_POLL - timedelta(minutes=30)),
        _pack("Battery_ID_02", 1, last_seen=_POLL - timedelta(seconds=3)),
    )
    modules = to_sample(_runtime(), bank, _POLL).battery_modules
    assert [m.serial for m in modules] == ["Battery_ID_02"]


def test_the_gate_stays_inside_the_window_the_comparison_guards_assume() -> None:
    # The 60 seconds itself is not pinned: asserting a literal would be a change
    # detector that fails whenever the value is tuned and proves nothing about
    # whether the new value is right. The relationship is what has to hold — a
    # pack this driver calls fresh must also be inside calibration's comparison
    # skew, or a block that cleared this gate reaches a spread that is only
    # meant to compare packs read at the same instant, which is the false
    # wiring-fault verdict #40 exists to stop.
    assert eg4_source.MODULE_STALE_AFTER < PACK_COMPARE_MAX_SKEW


def test_the_threshold_boundary_is_inclusive() -> None:
    # Pins MODULE_STALE_AFTER's meaning at the boundary. Without this the
    # constant could drift to any value and every other test here would still
    # pass, since they all sit far to one side of it or the other.
    inside = _bank_of(_pack("Battery_ID_01", 0, last_seen=_POLL - eg4_source.MODULE_STALE_AFTER))
    outside = _bank_of(
        _pack(
            "Battery_ID_01",
            0,
            last_seen=_POLL - eg4_source.MODULE_STALE_AFTER - timedelta(seconds=1),
        )
    )
    assert len(to_sample(_runtime(), inside, _POLL).battery_modules) == 1
    assert to_sample(_runtime(), outside, _POLL).battery_modules == ()


def test_a_naive_stamp_keeps_the_block_rather_than_guessing_a_zone() -> None:
    # A naive stamp means the library changed under us: it stamps
    # datetime.now(UTC) at both sites, deliberately. Guessing a zone is the worse
    # error — assume UTC against a local-time stamp and a healthy bank looks
    # hours old, so every pack disappears. Keep the data and complain in the log.
    bank = _bank_of(_pack("Battery_ID_01", 0, last_seen=datetime(2026, 8, 8, 20, 44, 50)))
    modules = to_sample(_runtime(), bank, _POLL).battery_modules
    assert [m.serial for m in modules] == ["Battery_ID_01"]


def test_dropping_a_held_pack_leaves_the_inverter_readings_alone() -> None:
    # The inverter measures at its own terminals and does not stop doing so
    # because the BMS rotation skipped a pack. Dropping the pack must not drop
    # anything the inverter reported for itself.
    bank = _bank_of(_pack("Battery_ID_01", 0, last_seen=_POLL - timedelta(hours=9)))
    sample = to_sample(_runtime(), bank, _POLL)
    assert sample.battery_modules == ()
    # A reading the fixture genuinely carries, so this asserts the inverter's own
    # data survived rather than quietly requiring the driver to invent a figure
    # the reply did not contain.
    assert sample.readings["pv1_power_w"] == 2253.0
    assert not sample.is_failed


def test_a_state_of_health_of_exactly_100_is_not_stored() -> None:
    # pylxpweb rewrites a reported 0 to 100 on every path that produces SOH.
    # In 0.9.38: transports/data.py:635-636 on the runtime path, :1292 per
    # module, :1705-1707 on the bank. Zero is what a silent BMS reports, so a
    # stored 100 could be a healthy bank or one that answered nothing, and
    # nothing downstream can tell which.
    runtime = _runtime(battery_soh=100)
    assert "battery_soh_pct" not in to_sample(runtime, bank=None).readings


def test_a_state_of_health_below_100_is_stored() -> None:
    # The rewrite only ever produces the literal 100, so every other value is a
    # measurement — including the falling one the column exists to show.
    readings = to_sample(_runtime(battery_soh=97), bank=None).readings
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
    assert "battery_soh_pct" not in to_sample(_runtime(), _healthy_bank(soh=100)).readings
    kept = to_sample(_runtime(), _healthy_bank(soh=94)).readings
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

    (silent,) = to_sample(_runtime(), _healthy_bank(batteries=[module(100)])).battery_modules
    assert silent.soh_pct is None
    (worn,) = to_sample(_runtime(), _healthy_bank(batteries=[module(91)])).battery_modules
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
    (module,) = to_sample(_runtime(), _healthy_bank(batteries=[record])).battery_modules
    assert module.fault_code is None
    assert module.warning_code is None


# --- the SampleBuildError wrap covers construction only, not argument
# evaluation (#66) -----------------------------------------------------------
#
# _module_sample used to wrap the whole BatteryModuleSample(...) expression in
# one try, so a ValueError raised while evaluating a constructor argument —
# a bug in _reading, _int_reading or _measured_soh, not a refusal from the
# model — was converted to SampleBuildError and recorded as an inverter gap
# exactly like a genuine refusal. That is the defect #42 removed one layer up,
# reappearing here.


def test_a_bug_in_our_own_mapping_is_not_absorbed_as_a_samplebuilderror() -> None:
    # round() raising on a NaN is what the reviewer used to demonstrate this:
    # identity (serial, battery_index) and the BMS-answering witness both
    # pass, so evaluation proceeds to the hoisted argument locals — where
    # _int_reading's round(cycle_count) raises before the constructor is ever
    # called. That it raises during argument evaluation rather than inside
    # BatteryModuleSample is exactly the boundary #66 draws: a bug in this
    # driver's own mapping, not the model refusing anything.
    record = SimpleNamespace(
        serial_number="Battery_ID_07",
        battery_index=0,
        max_cell_voltage=3.364,
        cycle_count=float("nan"),
    )
    with pytest.raises(ValueError) as excinfo:
        eg4_source._module_sample(record)
    assert not isinstance(excinfo.value, SampleBuildError)


def test_a_refused_slot_still_reaches_the_constructor_as_samplebuilderror() -> None:
    # The other side of the same boundary: a ValueError raised by
    # BatteryModuleSample.__post_init__ itself is a genuine refusal and must
    # still come out as SampleBuildError. Nothing in _module_sample checks the
    # sign of battery_index before building slot = index + 1, so a negative
    # index passes every guard and reaches the constructor at slot 0, which is
    # what __post_init__ refuses.
    record = SimpleNamespace(
        serial_number="Battery_ID_08",
        battery_index=-1,
        max_cell_voltage=3.364,
    )
    with pytest.raises(SampleBuildError, match="slot"):
        eg4_source._module_sample(record)


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
        ("_RUNTIME_METRICS", eg4_source._RUNTIME_METRICS, runtime),
        ("_RUNTIME_TERMINAL_METRICS", eg4_source._RUNTIME_TERMINAL_METRICS, runtime),
        ("_RUNTIME_BMS_METRICS", eg4_source._RUNTIME_BMS_METRICS, runtime),
        ("_RUNTIME_FLAGS", eg4_source._RUNTIME_FLAGS, runtime),
        ("_BANK_METRICS", eg4_source._BANK_METRICS, bank),
        ("_BANK_FLAGS", eg4_source._BANK_FLAGS, bank),
        ("_ENERGY_METRICS", eg4_source._ENERGY_METRICS, energy),
    )
    for label, table, declared in tables:
        for _, attribute in table:
            assert attribute in declared, f"{label} names {attribute}, which does not exist"
    for _, positive, negative in eg4_source._RUNTIME_SIGNED_PAIRS:
        assert positive in runtime and negative in runtime


def test_the_split_between_gated_and_ungated_follows_the_librarys_own_categories() -> None:
    # Which battery readings the BMS relays and which the inverter measures at
    # its own terminals is not a judgement made here: pylxpweb tags every
    # register definition with a category, and "bms" versus "runtime" is that
    # tag. Pinning it means a library that reclassifies a register fails here
    # rather than quietly moving a reading to the wrong side of the gate.
    definitions = {d.canonical_name: d for d in registers_for_model("EG4_HYBRID")}
    field_of: dict[str, list[str]] = {}
    for canonical, field in RUNTIME_FIELD.items():
        # A register the library reads but does not surface maps to None.
        if field is not None:
            field_of.setdefault(field, []).append(canonical)

    def category(attribute: str) -> set[str]:
        return {definitions[c].category.value for c in field_of[attribute] if c in definitions}

    for _, attribute in eg4_source._RUNTIME_BMS_METRICS:
        if attribute in field_of:
            assert category(attribute) == {"bms"}, attribute
    for _, attribute in eg4_source._RUNTIME_TERMINAL_METRICS:
        assert category(attribute) == {"runtime"}, attribute
    for _, positive, negative in eg4_source._RUNTIME_SIGNED_PAIRS:
        if positive in field_of:
            assert "bms" not in category(positive) and "bms" not in category(negative)


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


def _source(transport: object) -> Eg4LuxPowerSource:
    return Eg4LuxPowerSource(
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
    from arraysense.drivers.eg4_luxpower.source import _measured_soh

    assert _measured_soh(SimpleNamespace(soh=0.0), "soh") is None
    assert _measured_soh(SimpleNamespace(soh=100.0), "soh") is None
    assert _measured_soh(SimpleNamespace(soh=None), "soh") is None
    # Everything a real degrading bank reports still arrives.
    assert _measured_soh(SimpleNamespace(soh=97.0), "soh") == 97.0
    assert _measured_soh(SimpleNamespace(soh=1.0), "soh") == 1.0


# --- identify_model: Detect decodes to the precision the registry trusts -------
#
# Register 19 names the family and registers 0-1 (HOLD_MODEL) carry the power
# rating; together they decode to an exact model name. The mapping is
# pylxpweb's own claim, cited in identify_model's docstring, and this project
# only asserts a model where its own MODELS table already carries a citation.
# That boundary is what keeps an off-grid or LXP family — which MODELS only
# carries a caveat for — from coming back from Detect as a confident match.


def test_identify_model_decodes_the_pv_series_to_an_exact_model() -> None:
    # Registers from pylxpweb's own InverterModelInfo docstring for the 18kPV
    # (raw HOLD_MODEL 0x986C0) and the 12kPV's other cited rating. Both come
    # back as the names the MODELS table carries.
    assert eg4_source.identify_model(2092, 0x86C0, 0x9) == "18kPV"
    assert eg4_source.identify_model(2092, 0x40, 0x0) == "12kPV"


def test_identify_model_decodes_flexboss_to_an_exact_model() -> None:
    # FlexBOSS ratings sit eight higher than the PV series': bit 8 of reg1
    # adds the offset, which is why 0x0/0x100 is 21 kW and 0x20/0x100 is 18 kW.
    assert eg4_source.identify_model(10284, 0x0, 0x100) == "FlexBOSS21"
    assert eg4_source.identify_model(10284, 0x20, 0x100) == "FlexBOSS18"


def test_identify_model_refuses_an_off_grid_family() -> None:
    # 54 is the SNA off-grid family and 38 its 6000XP variant. MODELS carries
    # them only as a caveat, and a caveat must not be handed back as a
    # detection — the wizard asks the owner to pick instead.
    assert eg4_source.identify_model(54, 0x86C0, 0x9) is None
    assert eg4_source.identify_model(38, 0x86C0, 0x9) is None


def test_identify_model_refuses_a_code_this_project_has_not_cited() -> None:
    assert eg4_source.identify_model(9999, 0x86C0, 0x9) is None


def test_identify_model_refuses_a_rating_its_own_family_does_not_carry() -> None:
    # 2092 is the PV family, but rating 4 is not one of its two cited ratings.
    # The family is recognized; the exact model is not — and that distinction
    # is the caller's, carried as family_recognized rather than folded here.
    assert eg4_source.identify_model(2092, 0x80, 0x0) is None
