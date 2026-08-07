"""pylxp_source.py — adapter from pylxpweb's data classes to the wire-independent Sample.

This is the only module that knows what the inverter library calls things.
Everything downstream sees the metric names registered in arraysense.metrics,
so a change of library, transport or inverter family stops here.

Three properties of the library shape the whole mapping.

Its runtime object declares a field for every register any supported inverter
might expose, and a real read populates a handful of them: the reference 18kPV
answered with PV power, load power and grid frequency, and left most of the
rest None. So the mapping asks for attributes rather than reading them, and
emits only the ones that came back as numbers. A metric the inverter did not
report is absent from the sample, never a zero.

Its per-module battery records, by contrast, default to zero rather than None,
so an unpopulated slot arrives looking like a healthy module sitting at 0% SOC.
The distinguishing mark is the serial number, which is empty for a slot that
holds nothing — and identity is the serial anyway, so a module without one is
dropped.

And a few of the numbers it hands over are not readings at all. It substitutes
100 for a state of health of 0, defaults per-module fault and warning codes to
0 and never fills them, and computes remaining capacity from state of charge.
The first two are absences wearing the most reassuring value available, so they
are refused here — a metric that is never written stores as NULL, which is what
"nobody measured this" looks like. The third is real arithmetic on real inputs
and is kept, labelled at its mapping so it is not mistaken for a second opinion.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pylxpweb.transports.exceptions import TransportError, TransportResponseMismatchError
from pylxpweb.transports.factory import create_dongle_transport

from arraysense.config import Config
from arraysense.models import BatteryModuleSample, Sample

logger = logging.getLogger(__name__)

# The word pylxpweb puts on every reply that answers a question nobody asked.
# The dongle serves its vendor's cloud on the same socket and the replies cross,
# so a read of register 32 comes back carrying 127.
#
# The library names four ways to notice that, all of them the same event and all
# of them TransportResponseMismatchError: the wrong inverter serial, the wrong
# function code, the wrong start register, and a frame whose TCP function says
# it is a heartbeat rather than a reply. Three end "likely a misrouted cloud
# response" and the fourth "misrouted/unsolicited frame", so this one word
# catches all four where an earlier match on "register mismatch" caught one.
#
# Matched by message as well as by type because the type does not survive the
# trip: read_runtime and read_energy go through a group reader that catches
# every failure and re-raises a plain TransportReadError carrying the original
# only as text and as __cause__.
_MISROUTED = "misrouted"

# Inverter-level readings, as (metric name, library attribute).
#
# Grid voltage comes from the R-phase register, which on the split-phase
# hardware this targets is the line-to-line measurement — the ~240 V figure an
# owner expects to see, not one 120 V leg.
#
# Some of the library's fields are deliberately absent from this list. Its
# S-phase and T-phase registers return 6.4 V and 1545.9 V on split-phase
# hardware, because there is no third phase for them to measure; temperature
# sensors T2 to T5 are documented as reserved and read a flat zero. Mapping
# either would put a plausible-looking number on a chart where there is no
# measurement behind it.
_RUNTIME_METRICS: tuple[tuple[str, str], ...] = (
    ("pv_total_power_w", "pv_total_power"),
    ("pv1_power_w", "pv1_power"),
    ("pv2_power_w", "pv2_power"),
    ("pv3_power_w", "pv3_power"),
    ("pv1_voltage_v", "pv1_voltage"),
    ("pv2_voltage_v", "pv2_voltage"),
    ("pv3_voltage_v", "pv3_voltage"),
    ("pv1_current_a", "pv1_current"),
    ("pv2_current_a", "pv2_current"),
    ("pv3_current_a", "pv3_current"),
    ("grid_voltage_v", "grid_voltage_r"),
    ("grid_frequency_hz", "grid_frequency"),
    ("inverter_current_a", "inverter_rms_current_r"),
    ("inverter_power_w", "inverter_power"),
    ("rectifier_power_w", "rectifier_power"),
    ("ac_couple_power_w", "ac_couple_power"),
    ("power_factor", "power_factor"),
    ("eps_power_w", "eps_power"),
    ("eps_l1_power_w", "eps_l1_power"),
    ("eps_l2_power_w", "eps_l2_power"),
    ("eps_apparent_power_va", "eps_apparent_power"),
    ("eps_l1_apparent_power_va", "eps_l1_apparent_power"),
    ("eps_l2_apparent_power_va", "eps_l2_apparent_power"),
    ("eps_l1_voltage_v", "eps_l1_voltage"),
    ("eps_l2_voltage_v", "eps_l2_voltage"),
    ("eps_voltage_v", "eps_voltage_r"),
    ("eps_frequency_hz", "eps_frequency"),
    ("generator_power_w", "generator_power"),
    ("generator_voltage_v", "generator_voltage"),
    ("generator_frequency_hz", "generator_frequency"),
    ("inverter_temperature_c", "internal_temperature"),
    ("radiator1_temperature_c", "radiator_temperature_1"),
    ("radiator2_temperature_c", "radiator_temperature_2"),
    ("board_temperature_c", "temperature_t1"),
    ("bus_voltage_1_v", "bus_voltage_1"),
    ("bus_voltage_2_v", "bus_voltage_2"),
    ("device_status", "device_status"),
    ("inverter_fault_code", "fault_code"),
    ("inverter_warning_code", "warning_code"),
    ("inverter_run_time_s", "inverter_on_time"),
)

# Battery readings as the *inverter* measures them, at its own terminals, and
# the BMS figures it relays. They arrive on the runtime read, which does not
# depend on the bank object existing at all — a poll that comes back with no
# bank still carries them, which is why they are mapped; the bank's own figures
# below overwrite them whenever the BMS answered.
#
# What they hold while the BMS is silent is *not* established. The library
# decodes registers rather than reporting absence, and no capture in this
# repository covers a CAN dropout, so treat "these survive a dropout" as
# unproven rather than as a reason to trust them during one.
#
# battery_temperature_c comes from the hottest cell, not from the library's
# ``battery_temperature`` field. That field returned 11880 against real
# hardware — an undecoded register, not a temperature — while the cell extremes
# beside it read a correct 39 and 38 °C.
#
# State of health is deliberately not here; see ``_measured_soh``.
_RUNTIME_BATTERY_METRICS: tuple[tuple[str, str], ...] = (
    ("battery_voltage_v", "battery_voltage"),
    ("battery_current_a", "battery_current"),
    ("battery_soc_pct", "battery_soc"),
    ("battery_temperature_c", "bms_max_cell_temperature"),
    ("battery_min_cell_temperature_c", "bms_min_cell_temperature"),
    ("battery_max_cell_voltage_v", "bms_max_cell_voltage"),
    ("battery_min_cell_voltage_v", "bms_min_cell_voltage"),
    ("battery_cycle_count", "bms_cycle_count"),
    ("battery_full_capacity_ah", "battery_capacity_ah"),
    ("battery_module_count", "battery_parallel_num"),
    ("bms_charge_current_limit_a", "bms_charge_current_limit"),
    ("bms_discharge_current_limit_a", "bms_discharge_current_limit"),
    ("bms_charge_voltage_ref_v", "bms_charge_voltage_ref"),
    ("bms_discharge_cutoff_v", "bms_discharge_cutoff"),
)

# The same readings from the BMS via the bank object. Preferred where both
# exist: these come from the cells rather than from the inverter's terminals.
# The bank spells several of them differently from the runtime object, which is
# why this is a separate table rather than the same one applied twice.
_BANK_METRICS: tuple[tuple[str, str], ...] = (
    ("battery_voltage_v", "voltage"),
    ("battery_current_a", "current"),
    ("battery_soc_pct", "soc"),
    ("battery_voltage_inv_sample_v", "battery_voltage_inv_sample"),
    ("battery_temperature_c", "max_cell_temperature"),
    ("battery_min_cell_temperature_c", "min_cell_temperature"),
    ("battery_max_cell_voltage_v", "max_cell_voltage"),
    ("battery_min_cell_voltage_v", "min_cell_voltage"),
    ("battery_cycle_count", "cycle_count"),
    # Not a register. The library computes it as
    # ``round(max_capacity * battery_soc / 100)`` (0.9.38, data.py:1670-1672),
    # so it is battery_soc_pct restated in amp-hours and cannot disagree with
    # the column above it. Useful as a size, useless as corroboration — never
    # read it as a second opinion on state of charge.
    ("battery_remaining_capacity_ah", "current_capacity"),
    ("battery_full_capacity_ah", "max_capacity"),
    ("battery_module_count", "battery_count"),
    ("bms_charge_current_limit_a", "bms_charge_current_limit"),
    ("bms_discharge_current_limit_a", "bms_discharge_current_limit"),
    ("bms_charge_voltage_ref_v", "bms_charge_voltage_ref"),
    ("bms_discharge_cutoff_v", "bms_discharge_cutoff"),
    # The BMS raises faults of its own, separately from the inverter's. These
    # deliberately do not share the inverter's fault_code and warning_code
    # columns — a pack complaining while the inverter is content is exactly the
    # case worth being able to see.
    ("battery_fault_code", "fault_code"),
    ("battery_warning_code", "warning_code"),
)

# Flags, as (metric name, library attribute), stored as 1 or 0. Kept apart from
# the tables above because the reader used for measurements rejects booleans on
# purpose: True quietly becoming 1.0 in a column of watts would be a bug, while
# here it is the whole intent.
_RUNTIME_FLAGS: tuple[tuple[str, str], ...] = (
    ("bms_allow_charge", "bms_allow_charge"),
    ("bms_allow_discharge", "bms_allow_discharge"),
    ("bms_force_charge", "bms_force_charge"),
)

_BANK_FLAGS: tuple[tuple[str, str], ...] = (
    ("bms_allow_charge", "allow_charge"),
    ("bms_allow_discharge", "allow_discharge"),
    ("bms_force_charge", "force_charge"),
)

# The inverter's own energy counters, as (metric name, library attribute).
# Read separately from the runtime block and on their own cadence — see
# PylxpSource. The library reports None for counters this hardware has no
# source for: strings four to six on a three-MPPT unit, and the generator when
# none is fitted. Those stay absent rather than becoming zero, which would say
# the generator ran and produced nothing.
_ENERGY_METRICS: tuple[tuple[str, str], ...] = (
    ("pv_energy_today_kwh", "pv_energy_today"),
    ("pv1_energy_today_kwh", "pv1_energy_today"),
    ("pv2_energy_today_kwh", "pv2_energy_today"),
    ("pv3_energy_today_kwh", "pv3_energy_today"),
    ("load_energy_today_kwh", "load_energy_today"),
    ("eps_energy_today_kwh", "eps_energy_today"),
    ("battery_charge_energy_today_kwh", "charge_energy_today"),
    ("battery_discharge_energy_today_kwh", "discharge_energy_today"),
    ("grid_import_energy_today_kwh", "grid_import_today"),
    ("grid_export_energy_today_kwh", "grid_export_today"),
    ("ac_charge_energy_today_kwh", "ac_charge_energy_today"),
    ("inverter_energy_today_kwh", "inverter_energy_today"),
    ("pv_energy_total_kwh", "pv_energy_total"),
    ("pv1_energy_total_kwh", "pv1_energy_total"),
    ("pv2_energy_total_kwh", "pv2_energy_total"),
    ("pv3_energy_total_kwh", "pv3_energy_total"),
    ("load_energy_total_kwh", "load_energy_total"),
    ("eps_energy_total_kwh", "eps_energy_total"),
    ("battery_charge_energy_total_kwh", "charge_energy_total"),
    ("battery_discharge_energy_total_kwh", "discharge_energy_total"),
    ("grid_import_energy_total_kwh", "grid_import_total"),
    ("grid_export_energy_total_kwh", "grid_export_total"),
    ("ac_charge_energy_total_kwh", "ac_charge_energy_total"),
    ("inverter_energy_total_kwh", "inverter_energy_total"),
)

# How long past its due refresh a cached energy read stays usable. Measured from
# when the next read *should* have happened, not from the last successful one —
# otherwise any interval longer than this expires its own cache before it ever
# refreshes, and the counters vanish from every sample in between.
#
# A daily counter moves by hundredths of a kWh a minute, so a value a few
# minutes past due is still today's total. One nobody has managed to read for
# an hour is not, and serving it would be a stale number wearing a fresh
# timestamp.
ENERGY_STALE_AFTER_DUE = timedelta(minutes=5)

# Directional register pairs that collapse into one signed reading, as
# (metric name, positive attribute, negative attribute).
_RUNTIME_SIGNED_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("grid_power_w", "power_from_grid", "power_to_grid"),
    # The same quantity per leg. A split-phase service importing on one leg
    # while exporting on the other nets out to nearly zero in the combined
    # figure, which reads as a balanced house when it is the opposite.
    ("grid_power_l1_w", "grid_import_power_l1", "grid_export_power_l1"),
    ("grid_power_l2_w", "grid_import_power_l2", "grid_export_power_l2"),
)


def _reading(source: object, attribute: str) -> float | None:
    """Read one numeric attribute from a library object, or None if it is absent.

    Asks with a default rather than reading the attribute directly for two
    reasons: the library leaves a field None whenever the inverter did not
    answer for that register, and a field renamed in a library upgrade should
    leave a gap in the chart rather than break every poll. A field that is
    missing, None or not a number all come back the same way, because none of
    the three is a measurement and the store has one way to say so.

    Booleans are rejected explicitly. The library carries flags such as
    ``bms_allow_charge`` alongside its measurements, and Python would otherwise
    store True as 1.0 in a column of watts. Flags that genuinely belong in
    storage go through ``_collect_flags`` instead, where turning one into a 1 is
    the stated intent rather than an accident of the type system.
    """
    value = getattr(source, attribute, None)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _int_reading(source: object, attribute: str) -> int | None:
    """Read one whole-number attribute, or None if it is absent.

    Counts and cell numbers are integers downstream: the average of two cell
    *numbers* is a cell that does not exist. Rounding rather than truncating,
    because a scaled register that decodes to 2.9999 is three; and an absent
    reading stays absent on exactly the terms ``_reading`` already set.
    """
    value = _reading(source, attribute)
    return None if value is None else round(value)


# The state of health the library will invent, and the only one it invents:
# every rewrite named in _measured_soh assigns this literal.
_FABRICATED_SOH = 100.0


def _measured_soh(source: object, attribute: str) -> float | None:
    """Read a state of health, refusing the one value the library fabricates.

    pylxpweb turns a reported 0 into 100 on every path that produces SOH. In
    0.9.38: ``transports/data.py:635-636`` on the runtime object, ``:1292`` per
    module, ``:1705-1707`` on the bank, and ``:1048`` once more in
    ``BatteryData.__post_init__``, where ``_clamp_percentage(...) or 100``
    converts any 0 that got that far. Two of the four say why in a comment —
    "Default to 100% if not reported" and "0 is invalid, assume healthy".

    Zero is what a BMS that is not answering reports, so the rewrite fires in
    precisely the case where nothing is known and substitutes the most
    reassuring number available. On the one column whose job is to warn about a
    degrading bank, that is this project's central rule inverted.

    It cannot be undone downstream — the fabrication is a 100 and a healthy bank
    is also a 100 — so the value is refused rather than reconstructed. Every
    other reading is kept, because the rewrite only ever assigns the literal
    100. That leaves a column where anything stored is a measurement, and where
    the falling figure the column exists for still arrives.

    What it costs is real and worth stating plainly: a bank whose BMS genuinely
    reports 100% stores NULL and the page shows a dash. The alternative is a
    column reading 100 whether the packs are new or the CAN link is down, and a
    dash meaning "not established" is the honest one of the two.

    Zero is refused at this end too, and for the same reason rather than a
    different one. The rewrite is what turns most zeros into 100, but not every
    path applies it, so a raw 0 still arrives sometimes — and a bank reporting
    0% state of health is the reading that means the BMS said nothing, not a
    bank with no health left. Storing it would be the original error wearing the
    opposite number: refusing 100 while keeping 0 rejects the reassuring
    fabrication and keeps the alarming one.

    One helper serves all three objects, but not because they share a register.
    The runtime and bank paths do — both decode ``soc_soh_packed``, register 5,
    high byte — while a module's comes from offset 8 of its own register block.
    What they share is the rewrite.
    """
    soh = _reading(source, attribute)
    if soh is None or soh == _FABRICATED_SOH or soh <= 0.0:
        return None
    return soh


def _signed_pair(source: object, positive: str, negative: str) -> float | None:
    """Combine a directional pair of registers into one signed value.

    The library reports flow in each direction as its own always-positive
    register: charge and discharge for the battery, import and export for the
    grid. Storing one signed number instead means a chart needs one axis and
    one series, and the sign says which way the energy went. The first of the
    two attributes is the direction counted as positive — charging the battery,
    importing from the grid — so the result is ``positive - negative``, with
    discharge and export falling below zero.

    Both halves must be present. The pair describes a single quantity, and
    treating a missing half as zero would invent a reading — the failure this
    project exists to avoid.
    """
    into = _reading(source, positive)
    out_of = _reading(source, negative)
    if into is None or out_of is None:
        return None
    return into - out_of


def _collect(source: object, mapping: tuple[tuple[str, str], ...], into: dict[str, float]) -> None:
    """Copy every present reading in ``mapping`` from ``source`` into ``into``.

    Each entry of the mapping pairs a registry metric name with the library's
    own name for the field, and the readings dict is filled in place. Absent
    readings are left out of it entirely rather than stored as None, so the
    store writes no column for them at all and a gap stays a gap.
    """
    for metric, attribute in mapping:
        value = _reading(source, attribute)
        if value is not None:
            into[metric] = value


def _collect_flags(
    source: object, mapping: tuple[tuple[str, str], ...], into: dict[str, float]
) -> None:
    """Copy every present boolean in ``mapping`` from ``source`` into ``into`` as 1 or 0.

    Only genuine booleans are taken, from the same (metric name, library
    attribute) pairs the measurement tables use. A field the library left None
    means the inverter did not report that permission, which is not the same as
    reporting it withheld — storing a 0 there would show the BMS blocking
    charge during every CAN dropout.
    """
    for metric, attribute in mapping:
        value = getattr(source, attribute, None)
        if isinstance(value, bool):
            into[metric] = 1.0 if value else 0.0


def _house_load(source: object) -> float | None:
    """Work out what the house is actually drawing, in watts.

    Not as simple as it should be. The library exposes a field called
    ``load_power``, and it is not the house load: it is derived from register
    27, which the vendor documents as "power imported from grid". On an
    installation running on solar and battery it reads zero all day, so a chart
    fed from it shows a house consuming nothing while the lights are on.

    Register 170 — the library's ``output_power`` — is the inverter's own total
    load figure and is the right source. Against real hardware it read 2357 W
    at an instant when the reference product independently reported 2405 W and
    ``load_power`` reported 0.

    Older firmware omits register 170, in which case the EPS output is the best
    available answer: on this class of installation the whole house sits behind
    the backup panel, so the two agree closely, 2391 W against that same 2357.
    With neither register reported there is no answer to give, and None is the
    honest one.
    """
    total = _reading(source, "output_power")
    return total if total is not None else _reading(source, "eps_power")


def _collect_signed(
    source: object, mapping: tuple[tuple[str, str, str], ...], into: dict[str, float]
) -> None:
    """Collapse every directional register pair in ``mapping`` into one signed reading.

    The table-driven counterpart of ``_signed_pair``: each entry is a metric
    name followed by the two attributes carrying flow in each direction, the
    positive one first. A pair that did not arrive whole contributes nothing,
    because filling the missing half with a zero is what turns a house
    exporting hard into a house apparently sitting idle.
    """
    for metric, positive, negative in mapping:
        value = _signed_pair(source, positive, negative)
        if value is not None:
            into[metric] = value


def _bms_is_answering(source: object) -> bool:
    """Whether the BMS block holds real readings or a CAN-down block of zeroes.

    This is the guard the project's central rule depends on. The library's data
    classes default to None, but the decoder does not: it reads registers, and
    a BMS whose CAN link is down leaves those registers zero-filled, so a bank
    arrives with ``soc=0``, ``soh=100``, zero capacities and zero cell voltages
    — a full set of numbers, all of them false. Emitted as-is that renders a
    healthy bank at 0% state of charge, which is precisely the failure this
    project was built to avoid.

    Cell voltage is the discriminator. A lithium cell that is connected and
    measurable is never 0.000 V; the bank's own reported capacity is the same
    kind of witness. If neither says anything, nothing in the block is a
    measurement and all of it is dropped rather than any of it being trusted.
    """
    for attribute in ("max_cell_voltage", "min_cell_voltage", "max_capacity", "current_capacity"):
        value = _reading(source, attribute)
        if value is not None and value > 0:
            return True
    return False


def _module_sample(module: object) -> BatteryModuleSample | None:
    """Convert one library battery record into a BatteryModuleSample.

    A record that cannot be identified is dropped instead. The library defaults
    a module's fields to zero rather than None, so a register slot holding no
    module arrives as a full set of zeroes with an empty serial; recording that
    would put a phantom pack at 0% SOC on the chart. Since a module is
    identified by serial and never by slot, a record missing its serial — or
    missing the index that says which slot it sat in — has nothing to attach
    its history to.

    Raises:
        ValueError: the record claims a slot outside the four the inverter
            exposes. Loud is right here: silently dropping a real module would
            lose its history for good.
    """
    serial = getattr(module, "serial_number", None)
    index = _int_reading(module, "battery_index")
    if not isinstance(serial, str) or not serial or index is None:
        return None
    # A slot can carry a serial from a previous read and still hold nothing but
    # zeroes once its CAN link drops. A pack reading 0.000 V per cell is not a
    # flat pack, it is a pack that is not answering.
    if not _bms_is_answering(module):
        logger.debug("module %s reports no cell data; treating the block as absent", serial)
        return None

    # The library indexes modules from zero; slots are 1-based to match the
    # battery_module1..4 columns in the registry. The library's names for the
    # cell extremes also do not follow its own bank naming, hence the spelling
    # differences below.
    return BatteryModuleSample(
        serial=serial,
        slot=index + 1,
        soc_pct=_reading(module, "soc"),
        soh_pct=_measured_soh(module, "soh"),
        voltage_v=_reading(module, "voltage"),
        current_a=_reading(module, "current"),
        temperature_c=_reading(module, "temperature"),
        cycle_count=_int_reading(module, "cycle_count"),
        cell_max_voltage_v=_reading(module, "max_cell_voltage"),
        cell_min_voltage_v=_reading(module, "min_cell_voltage"),
        cell_max_temperature_c=_reading(module, "max_cell_temperature"),
        cell_min_temperature_c=_reading(module, "min_cell_temperature"),
        cell_max_voltage_num=_int_reading(module, "max_cell_num_voltage"),
        cell_min_voltage_num=_int_reading(module, "min_cell_num_voltage"),
        cell_max_temperature_num=_int_reading(module, "max_cell_num_temp"),
        cell_min_temperature_num=_int_reading(module, "min_cell_num_temp"),
        # Derived, not measured: ``max_capacity * soc / 100`` (data.py:1281).
        # It is this module's SOC in amp-hours and cannot corroborate it.
        remaining_capacity_ah=_reading(module, "current_capacity"),
        full_capacity_ah=_reading(module, "max_capacity"),
        charge_current_limit_a=_reading(module, "charge_current_limit"),
        discharge_current_limit_a=_reading(module, "discharge_current_limit"),
        status_code=_int_reading(module, "status"),
        # fault_code and warning_code are deliberately not mapped. BatteryData
        # declares both as ``int = 0`` (data.py:1031-1032) and
        # ``from_modbus_registers`` passes neither, because none of the 21
        # entries in BATTERY_REGISTERS carries either quantity. Every module of
        # every poll therefore arrives with a zero that means "never read",
        # while a stored 0 in a fault column means "no fault" — health asserted
        # about a pack nobody asked. ``status`` above is different: the
        # constructor sets it from the slot's status header register.
    )


def sample_from_pylxp(
    runtime: object,
    bank: object | None,
    timestamp: datetime | None = None,
) -> Sample:
    """Map one library read of runtime and battery data into a Sample.

    Pure: no I/O, no clock beyond the default timestamp, and no knowledge of
    the transport. The library's own objects, or anything shaped like them,
    both work — the mapping asks each object for the fields it wants and
    accepts that most may be missing, so the sample it builds holds every
    reading that was actually reported and nothing beyond them.

    The library stamps its data objects with a naive local-time timestamp,
    which cannot be compared across a daylight-saving boundary, so the poll
    time is taken here instead and an explicit one has to be timezone-aware.

    A bank whose CAN link is down arrives as None, and that is an absence
    rather than a failed poll. The sample then carries whatever the inverter's
    own runtime read returned, with no battery state invented to fill the hole.

    Raises:
        ValueError: a battery record claims a slot outside 1-4, or an explicit
            ``timestamp`` is naive.
    """
    readings: dict[str, float] = {}
    _collect(runtime, _RUNTIME_METRICS, readings)
    _collect(runtime, _RUNTIME_BATTERY_METRICS, readings)
    _collect_flags(runtime, _RUNTIME_FLAGS, readings)
    _collect_signed(runtime, _RUNTIME_SIGNED_PAIRS, readings)

    load = _house_load(runtime)
    if load is not None:
        readings["load_power_w"] = load

    # State of health is filtered rather than mapped, on both objects, so it
    # sits outside the tables above. See ``_measured_soh``.
    soh = _measured_soh(runtime, "battery_soh")

    battery_power = _signed_pair(runtime, "battery_charge_power", "battery_discharge_power")
    modules: tuple[BatteryModuleSample, ...] = ()

    if bank is not None:
        # The bank block and the per-module blocks fail independently: the CAN
        # link can drop the summary while the modules still answer, or the
        # reverse. Each is judged on its own rather than one vetoing the other.
        if _bms_is_answering(bank):
            _collect(bank, _BANK_METRICS, readings)
            _collect_flags(bank, _BANK_FLAGS, readings)
            bank_power = _signed_pair(bank, "charge_power", "discharge_power")
            if bank_power is not None:
                battery_power = bank_power
            bank_soh = _measured_soh(bank, "soh")
            if bank_soh is not None:
                soh = bank_soh
        records = getattr(bank, "batteries", None) or ()
        modules = tuple(
            sample for sample in (_module_sample(record) for record in records) if sample
        )

    if battery_power is not None:
        readings["battery_power_w"] = battery_power
    if soh is not None:
        readings["battery_soh_pct"] = soh

    return Sample(
        timestamp=timestamp if timestamp is not None else datetime.now(tz=UTC),
        readings=readings,
        battery_modules=modules,
    )


class _Transport(Protocol):
    """The slice of the library's transport this source uses.

    Narrower than the library's own transport class so a test can drive the
    source with a stand-in, and so swapping the dongle for the wired RS485 path
    — the dongle's TCP port is being removed in newer firmware — is a change of
    constructor rather than a change of code.
    """

    async def connect(self) -> None:
        """Open the connection."""
        ...

    async def disconnect(self) -> None:
        """Close the connection."""
        ...

    async def read_runtime(self) -> object:
        """Read inverter runtime data."""
        ...

    async def read_battery(self) -> object:
        """Read battery bank data, or None if no bank is present."""
        ...

    async def read_energy(self) -> object:
        """Read the inverter's own kWh counters."""
        ...


class PylxpSource:
    """An InverterSource backed by pylxpweb's WiFi dongle transport.

    Holds the dongle's single TCP client slot for as long as it is connected,
    so exactly one of these may exist per inverter. All the interesting work is
    in ``sample_from_pylxp``; this class exists to own the socket and to
    translate the library's failures into the ConnectionError the polling
    service expects.
    """

    def __init__(
        self,
        config: Config,
        transport: _Transport | None = None,
        energy_interval: float = 60.0,
    ) -> None:
        """Build a source for the inverter described by ``config``.

        The read timeout is the poll interval: a read still outstanding when
        the next poll is due has already missed its slot, and the service
        records the gap and tries again rather than queueing reads behind a
        wedged socket.

        Handing in a transport skips dialling the configured dongle entirely,
        which is how a test drives this without hardware and how the wired
        RS485 path will arrive later. Production leaves it None.
        """
        self._config = config
        self._energy_interval = timedelta(seconds=energy_interval)
        self._energy: dict[str, float] = {}
        self._energy_at: datetime | None = None
        self._transport: _Transport = transport or create_dongle_transport(
            host=config.dongle_host,
            dongle_serial=config.dongle_serial,
            inverter_serial=config.inverter_serial,
            port=config.dongle_port,
            timeout=config.poll_interval,
        )
        # How many times a crossed reply was retried. Counted rather than only
        # logged, because a retry that succeeds logs at DEBUG and the service
        # runs at INFO, so the healthy and failing cases look identical from
        # outside. What rate is normal on this hardware has not been measured.
        self._misroutes = 0

    @property
    def device(self) -> str:
        """The configured inverter serial, which is what stored rows are filed under.

        Not read off the wire, though the wire could answer: the transport has
        a ``read_serial_number()`` and holding register 112 carries the system
        type, and the collector reads no holding registers at all today.
        Trusting the configured value is safe here for a reason particular to
        this transport — it authenticates every read with that serial, so a
        wrong one fails the read rather than filing this inverter's data under
        another unit's identity.
        """
        return self._config.inverter_serial

    async def connect(self) -> None:
        """Claim the inverter's single client slot.

        Failing here is routine rather than exceptional: the dongle may be
        unreachable, or something else — the vendor's app, a second copy of
        this service — may already be holding its one TCP slot. Either way the
        library's TransportError becomes the ConnectionError the polling
        service knows to record as a gap and back off from.
        """
        try:
            await self._transport.connect()
        except (TransportError, OSError) as exc:
            raise ConnectionError(
                f"cannot reach inverter at {self._config.dongle_host}:"
                f"{self._config.dongle_port}: {exc}"
            ) from exc

    async def disconnect(self) -> None:
        """Release the connection and the client slot it holds."""
        await self._transport.disconnect()

    @property
    def misroutes(self) -> int:
        """How many crossed replies this source has retried since it was built.

        Readable because the rate is the diagnostic and a single event is not:
        the dongle crossing a reply now and then is the dongle being itself, and
        a sharp rise is a fault worth chasing. A retry that succeeds logs at
        DEBUG and the service runs at INFO, so without a counter the healthy
        case and the failing case look the same from outside.

        Counts both paths that retry — the runtime and bank reads, and the
        energy read. It is a count of retries attempted, not of every crossed
        reply the library ever saw: pylxpweb absorbs its own before we hear
        about them.
        """
        return self._misroutes

    @staticmethod
    def _is_misrouted(exc: BaseException) -> bool:
        """Whether this failure is the dongle answering the wrong question.

        Judged on the cause chain rather than on the exception in hand. What
        pylxpweb raises at the socket is a ``TransportResponseMismatchError``,
        but ``read_runtime`` and ``read_energy`` reach the socket through a
        group reader that catches everything and re-raises a flat
        ``TransportReadError("Failed to read register group '...': ...")``
        ``from`` the original, so the type is gone by the time it arrives and
        only the text and the ``__cause__`` link remain.

        Type and message are both consulted for opposite reasons. The type is
        the library's own statement of what happened and cannot drift with a
        reworded message; the message survives a wrapper that raised without
        chaining.

        Only ``__cause__`` is followed, never ``__context__``. Implicit context
        is whatever happened to be in flight when this was raised, so following
        it could turn a dead socket that failed while a mismatch was being
        handled into a retry — and a dead socket is what backing off is for.
        """
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, TransportResponseMismatchError):
                return True
            if _MISROUTED in str(current):
                return True
            current = current.__cause__
        return False

    async def _retrying[T](self, read: Callable[[], Awaitable[T]]) -> T:
        """Run one transport read, trying again once if the dongle misanswers.

        The dongle serves its vendor's cloud while it serves us, and replies
        cross: a read of register 32 comes back carrying 127, or 170 comes back
        as 254. A crossed reply is not a broken connection, and that
        distinction is the whole point of retrying here rather than backing off.

        This sits on top of pylxpweb's own retrying rather than duplicating it.
        Its ``_send_receive`` makes three attempts half a second apart, and one
        ``read_runtime()`` is eight such reads, so what reaches us has already
        failed inside the library and is the residue rather than the whole
        population. How much of that residue one more attempt recovers has not
        been measured — a successful retry logs at DEBUG and the service runs at
        INFO, which is what the counter above exists to make visible.

        Exactly one retry, immediately, and a count rather than a clock is what
        bounds it. A deadline recomputed at the top of each read cannot limit
        anything a poll repeats every interval — each poll simply gets a fresh
        budget — which is why an attempt at one was removed rather than kept.
        Whether a second mismatch is worth a third attempt has not been
        measured; one is the conservative choice on a socket that admits a
        single client.

        Anything that is not a crossed reply — a closed socket, a timeout —
        propagates untouched on the first failure, because retrying those is
        how a poll loop turns an unreachable inverter into a busy wait.
        """
        try:
            return await read()
        except TransportError as exc:
            if not self._is_misrouted(exc):
                raise
            self._misroutes += 1
            logger.debug("dongle answered the wrong question, retrying once: %s", exc)
            return await read()

    async def read(self) -> Sample:
        """Read one sample of inverter and battery state.

        A read that reaches the inverter but finds no battery bank still
        returns the inverter's own readings; the missing bank is an absence,
        not a failure. A read that does not reach the inverter raises
        ConnectionError instead, and the caller turns that into a gap — drawn
        as a break in the chart rather than smoothed over.
        """
        try:
            runtime = await self._retrying(self._transport.read_runtime)
            bank = await self._retrying(self._transport.read_battery)
        except (TransportError, OSError) as exc:
            raise ConnectionError(f"reading from inverter failed: {exc}") from exc
        sample = sample_from_pylxp(runtime, bank)
        energy = await self._read_energy(sample.timestamp)
        if not energy:
            return sample
        return replace(sample, readings={**sample.readings, **energy})

    async def _read_energy(self, now: datetime) -> dict[str, float]:
        """Return the inverter's kWh counters, refreshing them when they are due.

        Kept on its own clock rather than the poll clock. The dongle takes one
        client, so every extra round trip competes with the read that matters
        every few seconds, and a counter measured in kWh moves by hundredths of
        a unit in a minute — polling it as often as power would spend the
        connection on a number that has not changed.

        A failure here never fails the poll. Energy supplements the power
        readings and is not worth losing them over, so a failed read falls back
        to the last one until that goes stale, and then to nothing at all rather
        than to a number that stopped being true an hour ago.
        """
        due = self._energy_at is None or now - self._energy_at >= self._energy_interval
        if due:
            try:
                data = await self._retrying(self._transport.read_energy)
            except (TransportError, OSError) as exc:
                logger.warning("energy counters unavailable: %s", exc)
            else:
                fresh: dict[str, float] = {}
                _collect(data, _ENERGY_METRICS, fresh)
                self._energy, self._energy_at = fresh, now
        if self._energy_at is None or now - self._energy_at > (
            self._energy_interval + ENERGY_STALE_AFTER_DUE
        ):
            return {}
        # A cached read from before midnight must not be attached to a sample
        # after it. The daily counters reset at the turn of the day, and the
        # daily metrics roll up with max — so a 23:59 total carried into 00:00
        # becomes the new day's high-water mark and stays there. The lifetime
        # counters are monotonic and cross the boundary safely.
        if self._energy_at.date() != now.date():
            return {k: v for k, v in self._energy.items() if k.endswith("_total_kwh")}
        return self._energy
