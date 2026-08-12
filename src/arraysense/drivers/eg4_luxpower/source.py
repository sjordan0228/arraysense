"""source.py — the EG4/LuxPower driver: pylxpweb's data classes to a Sample.

This is the only module in the project that knows what the inverter library
calls things. Everything downstream sees the metric names registered in
arraysense.metrics, so a change of library, transport or inverter family stops
here — which is the whole reason it sits in a driver package rather than in the
collector, where it began.

One package per protocol family, not per model. LuxPower's 141-register surface
is shared by the 18kPV, the 6000XP and the 12kPV, so all three read through this
same mapping; a device that answers differently gets its own package rather than
a branch in this one.

Four properties of the library shape the whole mapping.

Its runtime object declares a field for every register any supported inverter
might expose, and a real read populates a handful of them: the reference 18kPV
answered with PV power, load power and grid frequency, and left most of the
rest None. So the mapping asks for attributes rather than reading them, and
emits only the ones that came back as numbers. A metric the inverter did not
report is absent from the sample, never a zero.

That only covers a register the inverter left out of its reply. A register that
is in the reply and reads 0 decodes to 0.0, which is a measurement as far as
anything downstream can tell — and the BMS block is exactly where that happens,
because the inverter answers for those registers whether or not the CAN link
behind them is up. Absence there has to be inferred rather than read, which is
what ``_bms_is_answering`` is for: a cell voltage of 0.000 V is not a cell, so
the whole block it stands for is dropped. The readings the inverter takes at its
own terminals are not gated with it, because a silent BMS does not stop the
inverter measuring its own busbar.

Its per-module battery records go further and default to zero rather than None,
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
from pylxpweb.transports.factory import create_dongle_transport, create_serial_transport
from pylxpweb.transports.modbus_serial import ModbusSerialTransport

from arraysense.config import Config
from arraysense.drivers.base import (
    Capabilities,
    DeviceIdentity,
    DeviceIdentityError,
    EnergyReporting,
    ModelSpec,
    SampleBuildError,
    expand_module_metrics,
    resolve_model,
)
from arraysense.models import BatteryModuleSample, Sample

logger = logging.getLogger(__name__)

# The name this driver is registered and configured under. Kept here beside the
# mapping so the identity a source reports and the key the registry answers to
# are one string rather than two that have to agree.
NAME = "eg4_luxpower"

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
# The inverter's serial lives in five input registers holding ten ASCII
# characters — pylxpweb's own SERIAL_NUMBER_REGISTER_COUNT and the width it
# decodes to. Anything else is an answer this cannot identify anybody from: the
# decoder drops bytes outside printable ASCII and the input path accepts a short
# register list without complaint, so a wrong length may be a reply cut short or
# a reply that arrived whole and decoded to nonsense. Either way it says nothing
# about who answered, which is why it is a gap to retry rather than the mismatch
# that stops the service.
_SERIAL_REGISTER_WIDTH = 10

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

# Battery readings the inverter takes at its own DC terminals, with its own
# sensors. They arrive on the runtime read and are written whatever the BMS
# did, because nothing about a silent BMS stops the inverter measuring the
# voltage on its own busbar. Register 4 is the only one that is a plain reading;
# battery power is the signed pair of registers 10 and 11, below.
#
# The split from the table beneath is the library's classification, not a
# judgement made here. Every register definition carries a category, and
# ``battery_voltage`` (reg 4), ``charge_power`` (10) and ``discharge_power``
# (11) are tagged ``runtime`` while everything in _RUNTIME_BMS_METRICS is tagged
# ``bms``. test_eg4_luxpower_mapping pins that, so a library that reclassifies
# one fails rather than moving a reading silently to the wrong side.
_RUNTIME_TERMINAL_METRICS: tuple[tuple[str, str], ...] = (("battery_voltage_v", "battery_voltage"),)

# The figures the inverter relays from the BMS rather than measuring: cell
# extremes, state of charge, capacities and the BMS's own current and voltage
# limits. Written only when ``_bms_is_answering`` says the BMS answered — see
# _RUNTIME_WITNESSES for why the gate is needed and what it cannot cover.
#
# They are mapped at all because the runtime read does not depend on the bank
# object existing: a poll that comes back with no bank still carries them. The
# bank's own copies overwrite them whenever it answered too.
#
# battery_temperature_c comes from the hottest cell, not from the library's
# ``battery_temperature`` field. That field returned 11880 against real
# hardware — an undecoded register, not a temperature — while the cell extremes
# beside it read a correct 39 and 38 °C.
#
# State of health is deliberately not here; see ``_measured_soh``.
_RUNTIME_BMS_METRICS: tuple[tuple[str, str], ...] = (
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
    # DERIVED, NOT MEASURED. The library computes it as
    # ``round(max_capacity * battery_soc / 100)`` (0.9.38, data.py:1670-1672),
    # so it is battery_soc_pct restated in amp-hours. It cannot disagree with
    # the column above it, which means it cannot corroborate it either: two
    # numbers that are the same number cross-check nothing. calibration.py is
    # where that matters — its whole job is noticing a reported state of charge
    # drifting from reality, and this is the reported state of charge again.
    # Kept because the amp-hours are a useful size. Never a second opinion.
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
#
# All three come from register 95, which the library decodes as a permission
# bitmap. A register that reads 0 decodes to three Falses, so these are gated
# with the BMS table rather than collected unconditionally: "the BMS refuses to
# charge or discharge" is a serious claim to make about a pack that said
# nothing.
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
# Eg4LuxPowerSource. The library reports None for counters this hardware has no
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

# How old a module's ``last_seen`` stamp may be before its block counts as held
# rather than read this poll. pylxpweb's round-robin accumulator serves every
# pack it has ever seen on every read, freezing the registers of a pack the
# firmware did not surface this cycle at their last real values, so the only way
# to tell a held block from a read one is this stamp against the poll time.
#
# Tighter than calibration.PACK_COMPARE_MAX_SKEW (two minutes), so that any
# pack passing this gate is also inside the window the comparison guards
# assume — a held voltage must not reach a spread that is only meant to compare
# packs read at the same instant. Loose enough to survive a slow read and
# ordinary clock jitter: the reference system polls every eleven seconds.
#
# Measured on the reference bank rather than assumed: over 107 consecutive polls
# every one carried all four packs, so a healthy pack there is never more than a
# single poll old and this threshold drops nothing real. It is deliberately not
# tightened towards one poll interval, because the library rotates packs through
# pages on larger banks and a gate near the cadence would drop packs that were
# read perfectly well, one page at a time. What it does not catch is a bank whose
# rotation dwells between a poll and a minute: those blocks are held but young
# enough to pass. That is a narrower hole than the fifteen minutes to nine hours
# this was written for, not a closed one.
MODULE_STALE_AFTER = timedelta(seconds=60)

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

# The three readings assembled rather than copied, so they appear in no table
# above and would otherwise be missing from what this driver claims to produce.
# House load is picked between two registers by ``_house_load``, battery power
# is a signed pair taken from whichever of the runtime object and the bank
# answered, and state of health is filtered by ``_measured_soh``.
_ASSEMBLED_METRICS: tuple[str, ...] = (
    "load_power_w",
    "battery_power_w",
    "battery_soh_pct",
)

# The per-module metric templates ``_module_sample`` fills, named as
# arraysense.metrics names them before the registry expands each across the four
# register slots. fault_code and warning_code are absent on purpose: pylxpweb
# declares both as ``int = 0`` and none of its 21 battery registers carries
# either, so every poll would assert "no fault" about a pack nobody asked.
#
# This list can only drift from ``_module_sample`` by someone editing one and
# not the other, which is what test_drivers pins by mapping a fully populated
# record and comparing what came through.
_MODULE_TEMPLATES: tuple[str, ...] = (
    "soc_pct",
    "soh_pct",
    "voltage_v",
    "current_a",
    "temperature_c",
    "cycle_count",
    "remaining_capacity_ah",
    "full_capacity_ah",
    "charge_current_limit_a",
    "discharge_current_limit_a",
    "status_code",
    "cell_max_voltage_v",
    "cell_min_voltage_v",
    "cell_max_temperature_c",
    "cell_min_temperature_c",
    "cell_max_voltage_num",
    "cell_min_voltage_num",
    "cell_max_temperature_num",
    "cell_min_temperature_num",
)


def _declared_metrics() -> frozenset[str]:
    """Every metric name this driver can produce, read off the mapping tables.

    Derived rather than listed. The tables are already the statement of what
    this driver emits, and a second list beside them would be a copy that goes
    stale the first time a mapping changes — the failure mode is a column the
    schema never creates and a reading silently dropped on the way to the store.

    The per-module names are read out of the registry rather than composed from
    a slot count kept here. How many slots the inverter exposes is the
    registry's business — it is the thing that expands each template — and a
    count copied into this module would be one more number to keep in step.
    """
    names: set[str] = set(_ASSEMBLED_METRICS)
    for table in (
        _RUNTIME_METRICS,
        _RUNTIME_TERMINAL_METRICS,
        _RUNTIME_BMS_METRICS,
        _RUNTIME_FLAGS,
        _BANK_METRICS,
        _BANK_FLAGS,
        _ENERGY_METRICS,
    ):
        names.update(metric for metric, _ in table)
    names.update(metric for metric, _, _ in _RUNTIME_SIGNED_PAIRS)
    names.update(expand_module_metrics(_MODULE_TEMPLATES))
    return frozenset(names)


# What a LuxPower-protocol inverter can do, as this driver reads it.
#
# Three PV strings, a backup panel, split-phase service and a generator input
# describe the 18kPV and equally the 6000XP and 12kPV, which share its register
# surface. Energy is COUNTED and that is the important line: these units keep 31
# kWh counters of their own, which is what the whole energy model reads. A
# family without them — the 3000 EHV — would declare ESTIMATED and everything
# above would know not to present its totals as meter readings.
CAPABILITIES = Capabilities(
    pv_strings=3,
    energy=EnergyReporting.COUNTED,
    metrics=_declared_metrics(),
    backup_output=True,
    generator_input=True,
    split_phase=True,
    parallel_capable=True,
    per_module_battery=True,
    relays_battery=True,
    battery_module_slots=4,
    transport="dongle",
)


# The models this family covers, and how much is actually known about each.
#
# Two groups sit behind one protocol, and the difference matters more than the
# shared register map suggests. The hybrids — 18kPV, 12kPV, FlexBOSS21,
# FlexBOSS18 — are one device-type family in pylxpweb (EG4_HYBRID) and agree
# about what their registers hold. The off-grid machines (6000XP, 12000XP;
# EG4_OFFGRID) answer at the same addresses with different meanings.
#
# Where to check that, because the obvious place says otherwise and a reviewer
# has already been misled by it: the register DEFINITION for 123 carries
# models=ALL and reads "Generator power" for every family. The divergence is
# recorded one layer up, in the docstring of the ac_couple_power property at
# pylxpweb/devices/inverters/_runtime_properties.py — "On EG4_OFFGRID
# (12000XP/6000XP), register 123 is a seconds counter — only register 153 is
# correct." Registers 124-126 do carry their caveat on the definition itself.
#
# That split is the whole problem. The library keeps its family-aware knowledge
# in the device property layer, and this driver reads the transport's register
# decode directly through _reading(), so none of it runs. A wrong meaning would
# be stored as a reading rather than caught.
#
# So an off-grid model carries a caveat rather than a citation. It is offered,
# because refusing it outright would break an installation that already chose
# it, and labelled, because a machine whose generator power is a seconds
# counter must not be presented as supported. See issue #122.
MODELS: tuple[ModelSpec, ...] = (
    ModelSpec(name="18kPV", pv_strings=3, citation="measured on the reference installation"),
    ModelSpec(
        name="12kPV",
        pv_strings=3,
        citation=(
            "pylxpweb DEVICE_TYPE_CODE_PV_STRING_COUNT: live-confirmed for device type "
            "code 2092, which covers the 18kPV and the 12kPV"
        ),
    ),
    ModelSpec(
        name="FlexBOSS21",
        pv_strings=3,
        citation=(
            "pylxpweb DEVICE_TYPE_CODE_PV_STRING_COUNT: live-confirmed for device type "
            "code 10284, which covers FlexBOSS21 and FlexBOSS18"
        ),
    ),
    ModelSpec(
        name="FlexBOSS18",
        pv_strings=3,
        citation=(
            "pylxpweb DEVICE_TYPE_CODE_PV_STRING_COUNT: live-confirmed for device type "
            "code 10284, which covers FlexBOSS21 and FlexBOSS18"
        ),
    ),
    ModelSpec(
        name="6000XP",
        caveat=(
            "Off-grid family: several registers this driver reads mean something "
            "different here than on the hybrids, and its PV string count is "
            "unconfirmed upstream. Readings may be wrong rather than missing. "
            "See issue #122."
        ),
    ),
)


def find_model_by_name(name: str) -> ModelSpec:
    """The module's own model lookup, so the source needs no registry import."""
    for model in MODELS:
        if model.name == name:
            return model
    raise ValueError(f"no model {name!r} in this family")


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


# The fields consulted to decide whether a BMS answered at all, spelled once
# per object because the bank and the runtime block name the same registers
# differently. Both lists are cell voltages plus a capacity: a lithium cell that
# is connected and measurable is never 0.000 V, and a BMS that is talking always
# publishes a capacity.
#
# The runtime list has no ``current_capacity`` equivalent because the runtime
# block carries no such register — the library computes that figure on the bank
# only.
_BANK_WITNESSES: tuple[str, ...] = (
    "max_cell_voltage",
    "min_cell_voltage",
    "max_capacity",
    "current_capacity",
)
_RUNTIME_WITNESSES: tuple[str, ...] = (
    "bms_max_cell_voltage",
    "bms_min_cell_voltage",
    "battery_capacity_ah",
)


def _bms_is_answering(source: object, witnesses: tuple[str, ...]) -> bool:
    """Whether a block of BMS readings holds measurements or a CAN-down block of zeroes.

    This is the guard the project's central rule depends on. The library's data
    classes default to None, but the decoder does not: it reads registers, and
    a BMS whose CAN link is down leaves those registers zero-filled, so a block
    arrives with ``soc=0``, ``soh=100``, zero capacities and zero cell voltages
    — a full set of numbers, all of them false. Emitted as-is that renders a
    healthy bank at 0% state of charge, which is precisely the failure this
    project was built to avoid.

    ``witnesses`` names the fields that decide it, because the readings
    themselves cannot: 0.0 A is what an idle bank draws and 0 cycles is what a
    new pack has done, so neither can be refused on its own without throwing
    away the real reading of the same shape. A cell voltage can, and it stands
    for the whole block — if nothing in the block is a measurement, none of it
    is trusted.

    Two things it does not do. It is all-or-nothing per block, so a BMS that
    answers some of its registers and zero-fills the rest still writes those
    zeros — the witness passes and every field goes through. And it cannot
    separate a silent BMS from one genuinely reporting zero on a witness field,
    which is why the witnesses are quantities that have no true zero.

    No capture in this repository covers a CAN dropout on this hardware, so the
    premise that these registers read 0 during one is inherited from what the
    library's decoder does with an unanswered register rather than measured on
    the wire.
    """
    for attribute in witnesses:
        value = _reading(source, attribute)
        if value is not None and value > 0:
            return True
    return False


def _held_for(module: object, timestamp: datetime) -> timedelta | None:
    """How long a battery block's data has been held, or None if it was read this poll.

    pylxpweb's round-robin accumulator serves every pack it has ever seen on
    every read, and a pack the firmware did not surface this cycle comes back
    with its registers frozen at their last real values. That held block would
    pass ``_bms_is_answering`` — the held cell voltages are the last real ones,
    and so non-zero — and would then be stamped with the current poll's time.
    Every guard downstream filters on that timestamp, so the block always looks
    freshly read: a held voltage compared against a live one raises a wiring
    fault that is not there, and the spread bands satisfy their "every known
    pack reported at this instant" rule using stale values. The library's
    ``last_seen`` stamp is the only thing that tells the two apart.

    Returns None when the block should be kept — either ``last_seen`` is the
    dataclass default, or the reading is inside ``MODULE_STALE_AFTER``. That is a
    minute rather than one poll, so "kept" means recent, not necessarily read on
    this very cycle; see the constant for why it is not tightened further.
    Otherwise it returns the age of the held data, for the caller to log.

    ``None`` counts as fresh. It is the dataclass default, so a library build
    that does not stamp it must not make every pack vanish — that would trade a
    wrong reading for no readings at all, which is worse. It looks like an
    oversight otherwise.

    A naive stamp keeps the block and says so loudly, rather than being converted.
    Verified in the installed library it is stamped ``datetime.now(UTC)`` at both
    sites, aware on purpose and commented as such, so a naive one would mean the
    library changed underneath us and this gate is judging something it no longer
    understands. Attaching a zone to guess at it is the worse move of the two:
    assume UTC against a stamp that is really local time and every reading looks
    hours old, so a healthy bank loses every pack and the charts empty out. Being
    wrong in the direction of keeping data is recoverable; being wrong in the
    direction of discarding it is not. The warning is deliberately per call — if
    the library ever does change this, a log line every poll is what gets it
    noticed.

    Two ways this gate fails open, both preferred to their alternatives and
    neither silent in effect: the ``None`` above, and the library's own
    bookkeeping, which sets ``b.last_seen = last_seen.get(b.battery_index, now)``
    and so stamps the current time wherever it holds no record for an index. The
    gate is only as good as that bookkeeping — strictly better than trusting every
    block, and not a guarantee.
    """
    last_seen = getattr(module, "last_seen", None)
    if last_seen is None:
        return None
    if last_seen.tzinfo is None:
        logger.warning(
            "module %s carries a naive last_seen (%s); keeping the block, "
            "the staleness gate cannot judge it without a zone",
            getattr(module, "serial_number", None),
            last_seen,
        )
        return None
    age = timestamp - last_seen
    return None if age <= MODULE_STALE_AFTER else age


def _module_sample(module: object, timestamp: datetime | None = None) -> BatteryModuleSample | None:
    """Convert one library battery record into a BatteryModuleSample.

    A record that cannot be identified is dropped instead. The library defaults
    a module's fields to zero rather than None, so a register slot holding no
    module arrives as a full set of zeroes with an empty serial; recording that
    would put a phantom pack at 0% SOC on the chart. Since a module is
    identified by serial and never by slot, a record missing its serial — or
    missing the index that says which slot it sat in — has nothing to attach
    its history to.

    A record whose data is held rather than read is dropped too, not kept and
    back-dated: a frozen block is not a measurement. ``timestamp`` is the poll
    time the sample is being built for, and the block's ``last_seen`` is judged
    against it — see ``_held_for`` for what counts as held and why ``None`` and
    naive stamps are each treated as they are. When no poll time is given the
    staleness gate is skipped, because there is no clock to hold against;
    ``to_sample`` always passes one.

    Raises:
        SampleBuildError: the record's 0-based index maps to a slot below one —
            a negative index from the library, since the adjustment to 1-based
            always happens here. Loud is right: silently dropping a real module
            would lose its history for good.
            There is no upper bound any more — a bank may hold more packs than
            the registry names columns for, and storage is keyed on the serial.
    """
    serial = getattr(module, "serial_number", None)
    index = _int_reading(module, "battery_index")
    if not isinstance(serial, str) or not serial or index is None:
        return None
    # A slot can carry a serial from a previous read and still hold nothing but
    # zeroes once its CAN link drops. A pack reading 0.000 V per cell is not a
    # flat pack, it is a pack that is not answering.
    if not _bms_is_answering(module, _BANK_WITNESSES):
        logger.debug("module %s reports no cell data; treating the block as absent", serial)
        return None
    # A block whose registers are held rather than read is not a reading either,
    # however non-zero they look. The witness gate above catches a CAN drop,
    # where the values go to *zero*; this catches the accumulator serving a
    # pack it did not surface, where they are merely old. Only judged when a
    # poll time is supplied — ``to_sample`` always supplies one; a caller with
    # no clock cannot tell a held block from a read one.
    if timestamp is not None:
        held = _held_for(module, timestamp)
        if held is not None:
            logger.debug("module %s last seen %s ago; dropping as held", serial, held)
            return None

    # The library indexes modules from zero; slots are 1-based to match the
    # battery_module1..4 columns in the registry. The library's names for the
    # cell extremes also do not follow its own bank naming, hence the spelling
    # differences below.
    #
    # Every argument is evaluated into a local here, before the try, so the
    # try covers only the constructor call. A ValueError raised while reading
    # one of these — a bug in _reading, _int_reading or _measured_soh, not a
    # refusal from the model — must propagate as itself rather than being
    # recorded as an inverter gap it is not.
    slot: int = index + 1
    soc_pct: float | None = _reading(module, "soc")
    soh_pct: float | None = _measured_soh(module, "soh")
    voltage_v: float | None = _reading(module, "voltage")
    current_a: float | None = _reading(module, "current")
    temperature_c: float | None = _reading(module, "temperature")
    cycle_count: int | None = _int_reading(module, "cycle_count")
    cell_max_voltage_v: float | None = _reading(module, "max_cell_voltage")
    cell_min_voltage_v: float | None = _reading(module, "min_cell_voltage")
    cell_max_temperature_c: float | None = _reading(module, "max_cell_temperature")
    cell_min_temperature_c: float | None = _reading(module, "min_cell_temperature")
    cell_max_voltage_num: int | None = _int_reading(module, "max_cell_num_voltage")
    cell_min_voltage_num: int | None = _int_reading(module, "min_cell_num_voltage")
    cell_max_temperature_num: int | None = _int_reading(module, "max_cell_num_temp")
    cell_min_temperature_num: int | None = _int_reading(module, "min_cell_num_temp")
    # Derived, not measured: ``max_capacity * soc / 100`` (data.py:1281).
    # It is this module's SOC in amp-hours and cannot corroborate it.
    remaining_capacity_ah: float | None = _reading(module, "current_capacity")
    full_capacity_ah: float | None = _reading(module, "max_capacity")
    charge_current_limit_a: float | None = _reading(module, "charge_current_limit")
    discharge_current_limit_a: float | None = _reading(module, "discharge_current_limit")
    status_code: int | None = _int_reading(module, "status")
    # fault_code and warning_code are deliberately not mapped. BatteryData
    # declares both as ``int = 0`` (data.py:1031-1032) and
    # ``from_modbus_registers`` passes neither, because none of the 21
    # entries in BATTERY_REGISTERS carries either quantity. Every module of
    # every poll therefore arrives with a zero that means "never read",
    # while a stored 0 in a fault column means "no fault" — health asserted
    # about a pack nobody asked. ``status`` above is different: the
    # constructor sets it from the slot's status header register.

    try:
        return BatteryModuleSample(
            serial=serial,
            slot=slot,
            soc_pct=soc_pct,
            soh_pct=soh_pct,
            voltage_v=voltage_v,
            current_a=current_a,
            temperature_c=temperature_c,
            cycle_count=cycle_count,
            cell_max_voltage_v=cell_max_voltage_v,
            cell_min_voltage_v=cell_min_voltage_v,
            cell_max_temperature_c=cell_max_temperature_c,
            cell_min_temperature_c=cell_min_temperature_c,
            cell_max_voltage_num=cell_max_voltage_num,
            cell_min_voltage_num=cell_min_voltage_num,
            cell_max_temperature_num=cell_max_temperature_num,
            cell_min_temperature_num=cell_min_temperature_num,
            remaining_capacity_ah=remaining_capacity_ah,
            full_capacity_ah=full_capacity_ah,
            charge_current_limit_a=charge_current_limit_a,
            discharge_current_limit_a=discharge_current_limit_a,
            status_code=status_code,
        )
    except ValueError as exc:
        raise SampleBuildError(f"BatteryModuleSample refused the reply: {exc}") from exc


def to_sample(
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
    The same poll time ages the per-module blocks: a pack the accumulator
    served without the firmware reading it this cycle is dropped by comparing
    its ``last_seen`` stamp against it (see ``_module_sample``).

    A bank whose CAN link is down arrives as None, and that is an absence
    rather than a failed poll. The sample then carries whatever the inverter's
    own runtime read returned, with no battery state invented to fill the hole
    — which includes holding back the BMS figures the runtime read relays,
    since those come back as a block of zeroes rather than as nothing.

    Raises:
        SampleBuildError: a battery record claims a slot below one, which means the
            library's 0-based index was not adjusted, or an explicit
            ``timestamp`` is naive. There is no upper bound: a bank may hold more
            packs than the registry names columns for.
    """
    readings: dict[str, float] = {}
    # One poll time serves the whole sample: the store writes one timestamp per
    # poll, and the module staleness check compares each block's ``last_seen``
    # against the same clock. A naive explicit timestamp is rejected here so the
    # comparison cannot be silently made against a naive clock.
    if timestamp is not None and timestamp.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    now = timestamp if timestamp is not None else datetime.now(tz=UTC)
    _collect(runtime, _RUNTIME_METRICS, readings)
    _collect(runtime, _RUNTIME_TERMINAL_METRICS, readings)
    _collect_signed(runtime, _RUNTIME_SIGNED_PAIRS, readings)

    # Everything the inverter relays from the BMS waits on the BMS having
    # answered. Those registers are in the reply whether or not the CAN link
    # behind them is up, and read 0 when it is not, so collecting them
    # unconditionally wrote thirteen measurements and three permission flags —
    # state of charge and the cell extremes among them — about a bank that had
    # reported nothing.
    soh: float | None = None
    if _bms_is_answering(runtime, _RUNTIME_WITNESSES):
        _collect(runtime, _RUNTIME_BMS_METRICS, readings)
        _collect_flags(runtime, _RUNTIME_FLAGS, readings)
        # State of health is filtered rather than mapped, on both objects, so
        # it sits outside the tables above. See ``_measured_soh``.
        soh = _measured_soh(runtime, "battery_soh")

    load = _house_load(runtime)
    if load is not None:
        readings["load_power_w"] = load

    battery_power = _signed_pair(runtime, "battery_charge_power", "battery_discharge_power")
    modules: tuple[BatteryModuleSample, ...] = ()

    if bank is not None:
        # The bank block and the per-module blocks fail independently: the CAN
        # link can drop the summary while the modules still answer, or the
        # reverse. Each is judged on its own rather than one vetoing the other.
        if _bms_is_answering(bank, _BANK_WITNESSES):
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
            sample for sample in (_module_sample(record, now) for record in records) if sample
        )

    if battery_power is not None:
        readings["battery_power_w"] = battery_power
    if soh is not None:
        readings["battery_soh_pct"] = soh

    try:
        return Sample(
            timestamp=now,
            readings=readings,
            battery_modules=modules,
        )
    except ValueError as exc:
        raise SampleBuildError(f"Sample refused the reply: {exc}") from exc


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


class Eg4LuxPowerSource:
    """An InverterSource backed by one of pylxpweb's local transports.

    Which one is a configuration choice: the WiFi dongle's TCP port, or a
    USB-to-RS485 adapter. Either way exactly one of these may exist per
    inverter — the dongle has a single client slot, and a serial port accepts
    a single client too. All the interesting work is
    in ``to_sample``; this class exists to own the socket and to
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

        Handing in a transport skips building one from the configuration
        entirely, which is how a test drives this without hardware. Production
        leaves it None and lets ``transport`` in the config choose between the
        dongle and a serial adapter.

        One consequence worth knowing: an injected transport is not identified.
        ``_confirm_identity`` only checks what this class built itself, so a
        caller handing in a serial transport is vouching for which inverter is
        on the other end.
        """
        self._config = config
        self._energy_interval = timedelta(seconds=energy_interval)
        self._energy: dict[str, float] = {}
        self._energy_at: datetime | None = None
        self._serial: ModbusSerialTransport | None = None
        self._identity_verified = False
        # Choose the transport factory based on config.transport. An explicitly
        # injected transport argument still wins over the config, as the tests
        # depend on it.
        if transport is not None:
            self._transport = transport
        elif config.transport == "modbus_serial":
            # Kept as its own attribute as well as the transport: confirming the
            # inverter's identity needs a method the transport Protocol does not
            # declare, and an injected transport is a caller's own business.
            self._serial = create_serial_transport(
                port=config.serial_device,
                serial=config.inverter_serial,
                baudrate=config.serial_baud,
                unit_id=config.serial_unit_id,
                timeout=config.poll_interval,
            )
            self._transport = self._serial
        else:
            self._transport = create_dongle_transport(
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

        How the configured value earns that trust depends on the transport.
        The dongle authenticates every read with the serial, so a wrong one
        fails the read rather than misfiling data, and nothing more is needed.
        A Modbus request has no such notion — it selects a unit by address and
        whichever inverter holds that address answers — so on the serial
        transport the driver reads the serial off the wire once, before it takes
        any reading, and refuses to collect if it disagrees. What that does and
        does not promise is set out in ``_confirm_identity``.
        """
        return self._config.inverter_serial

    @property
    def identity(self) -> DeviceIdentity:
        """Which unit this is, for anything describing the device rather than polling it.

        The serial is ``device`` rather than a second reading of the same
        setting: the store files rows under one of them, and two spellings of
        one inverter would be two inverters as far as it is concerned.

        The model is what this installation is configured as, ``None`` when
        nothing configured one. Nobody has asked the inverter — the driver reads
        no holding registers — so a configured model that is wrong will not be
        caught here.
        """
        return DeviceIdentity(driver=NAME, serial=self.device, model=self._config.model or None)

    @property
    def capabilities(self) -> Capabilities:
        """What this family can do, and how this installation is actually reached.

        Everything but the transport is the family's declaration and is shared
        with the registry entry, so a caller reading it off a built source and
        one reading it off the entry cannot be told two different things about
        what the hardware can do.

        The transport is the exception, because it is not a property of the
        family at all — the same inverter is reached by a dongle at one
        installation and a serial adapter at the next. The registry entry can
        only carry the default; a built source knows which was configured, and a
        page telling somebody they are connected by dongle over a serial link
        would be worse than telling them nothing.
        """
        declared = CAPABILITIES
        if self._config.model:
            declared = resolve_model(declared, find_model_by_name(self._config.model))
        return replace(declared, transport=self._config.transport)

    async def connect(self) -> None:
        """Claim the inverter's single client slot, if it is not already held.

        The collector calls this before every poll. On the dongle that is
        harmless, but a serial port is opened *exclusively* and the library
        builds a fresh client on every connect without closing the last one, so
        forwarding each call locks the port against ourselves. Measured on the
        reference adapter: the first poll returned 90 readings and every poll
        after it failed with "Could not exclusively lock port ... Resource
        temporarily unavailable". Connecting only when not already connected is
        what makes a serial installation able to poll more than once.

        Failing here is routine rather than exceptional: the dongle may be
        unreachable, or something else — the vendor's app, a second copy of
        this service — may already be holding its one TCP slot. Either way the
        library's TransportError becomes the ConnectionError the polling
        service knows to record as a gap and back off from.
        """
        # Only the physical open is skipped. The identity check below still
        # runs: skipping it here meant a connection whose check had failed
        # stayed open, and the next poll returned early and went straight to
        # reading, storing rows under a serial nothing had confirmed.
        if self._serial is None or not self._serial.is_connected:
            try:
                await self._transport.connect()
            except (TransportError, OSError, UnicodeError) as exc:
                # UnicodeError joins the two: a host the settings page accepted
                # as a string but that resolves through IDNA to an over-long DNS
                # label ("label empty or too long") raises it here, not OSError.
                # That is a host that cannot be reached, which is exactly a gap
                # to record and back off from — not a crash that kills every
                # poll and every boot until someone SSHes in. Only the connect
                # site catches it: a UnicodeDecodeError at a register read below
                # is a decoding bug and must surface, not become a silent gap.
                # Name the endpoint configured, not always the dongle.
                if self._config.transport == "modbus_serial":
                    endpoint = self._config.serial_device
                else:
                    endpoint = f"{self._config.dongle_host}:{self._config.dongle_port}"
                raise ConnectionError(f"cannot reach inverter at {endpoint}: {exc}") from exc
        await self._confirm_identity()

    async def _confirm_identity(self) -> None:
        """Check the inverter on a serial bus is the one the settings name.

        Only for the serial transport. A Modbus request selects a unit by
        address, so nothing about the reply says which machine sent it — while
        the dongle refuses a reply whose serial does not match, making this
        unnecessary there. Without the check a mistyped address or a second
        inverter on the bus files its readings under this one's name, and every
        row looks perfectly ordinary afterwards.

        Asked until it is answered, then never again. In practice that is the
        first connect; a check that cannot complete leaves the question open and
        a later connect asks it once more, so a bus that is slow to wake does
        not permanently skip verification. Once an answer agrees, nothing asks
        again for the life of the process. That bound is forced by the library
        rather than chosen for economy. The serial lives in its own registers — the runtime
        read carries no serial at all, so there is no free answer — and *any*
        successful register read sets pylxpweb's consecutive-error count back to
        zero. That count is the sole trigger for its own reconnect, so a probe
        anywhere in the poll loop wipes out the evidence of the previous poll's
        failure on every pass: a bus gone deaf to the runtime or battery
        registers would never reach the threshold that repairs it. Every
        placement inside the loop trades a silent misattribution for a silent
        outage.

        What this therefore catches is a misconfiguration — a mistyped serial,
        or an address pointing at the wrong unit — and it catches it before a
        single row is written. What it does not catch is somebody rewiring the
        bus to a different inverter while the service keeps running, because
        pylxpweb reconnects itself after consecutive errors and pymodbus reopens
        the port beneath it, and neither tells this object. That is a real gap
        and it is stated rather than papered over.

        A failure to ask is a transport problem and becomes a gap; an answer
        that disagrees is a misconfiguration and stops the loop, because it
        cannot come right on its own and every poll it survives adds another row
        to the wrong history. Neither outcome depends on the connection being
        torn down: ``connect`` skips only the physical open when the port is
        already held, and still asks this question every time, so an unanswered
        one stays unanswered until it is answered.
        """
        if self._identity_verified:
            return
        if self._serial is None:
            return
        try:
            answered = await self._serial.read_serial_number()
        except (TransportError, OSError) as exc:
            raise ConnectionError(f"could not read the inverter's serial: {exc}") from exc

        expected = self._config.inverter_serial
        # Judged against the protocol's width, not against the configured
        # value. Measuring against the setting would let an eight character typo
        # match a shorter answer and call it agreement. What a wrong width means
        # is deliberately left open — see _SERIAL_REGISTER_WIDTH — because the
        # decoder cannot tell a truncated reply from a whole one carrying a byte
        # it dropped. Neither identifies anybody, and that is all this needs.
        #
        # A wrong width is treated as a gap because it accuses nobody, not
        # because it is known to pass. A reply carrying a byte the decoder drops
        # would come back short every time, and the collector would go on
        # retrying it — visibly, in the gap rows, rather than by filing readings
        # under a serial nothing confirmed.
        #
        # Reading the value rather than asking the library for a yes or no is
        # what separates the two failures: a short answer is a passing condition
        # and becomes a gap, a complete but different one cannot come right on
        # its own and must stop the loop. Treating them alike would restart the
        # service forever over one truncated reply.
        if len(answered) != _SERIAL_REGISTER_WIDTH:
            raise ConnectionError(
                f"the inverter's serial came back incomplete ({answered!r}); "
                "cannot confirm which unit answered"
            )
        if answered != expected:
            raise DeviceIdentityError(
                f"the inverter at {self._config.serial_device} unit "
                f"{self._config.serial_unit_id} is not {expected}; "
                "refusing to file its readings under that serial"
            )
        self._identity_verified = True

    async def disconnect(self) -> None:
        """Release the connection and the client slot it holds.

        The identity answer deliberately survives this. It is a statement about
        the configuration having matched the hardware at startup, not about the
        connection being continuously the same one — see ``_confirm_identity``
        for why the library makes the stronger claim impossible to keep
        honestly, and what that leaves uncovered.
        """
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
        sample = to_sample(runtime, bank)
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
