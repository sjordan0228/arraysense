"""pylxp_source.py — adapter from pylxpweb's data classes to the wire-independent Sample.

This is the only module that knows what the inverter library calls things.
Everything downstream sees the metric names registered in arraysense.metrics,
so a change of library, transport or inverter family stops here.

Two properties of the library shape the whole mapping.

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
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from pylxpweb.transports.exceptions import TransportError
from pylxpweb.transports.factory import create_dongle_transport

from arraysense.config import Config
from arraysense.models import BatteryModuleSample, Sample

logger = logging.getLogger(__name__)

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
# the BMS figures it relays. They survive a CAN dropout that silences the bank
# object, which is why they are mapped at all; the bank's own figures below
# overwrite them whenever the BMS answered.
#
# battery_temperature_c comes from the hottest cell, not from the library's
# ``battery_temperature`` field. That field returned 11880 against real
# hardware — an undecoded register, not a temperature — while the cell extremes
# beside it read a correct 39 and 38 °C.
_RUNTIME_BATTERY_METRICS: tuple[tuple[str, str], ...] = (
    ("battery_voltage_v", "battery_voltage"),
    ("battery_current_a", "battery_current"),
    ("battery_soc_pct", "battery_soc"),
    ("battery_soh_pct", "battery_soh"),
    ("battery_voltage_inv_sample_v", "battery_voltage_inv_sample"),
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
    ("battery_soh_pct", "soh"),
    ("battery_voltage_inv_sample_v", "battery_voltage_inv_sample"),
    ("battery_temperature_c", "max_cell_temperature"),
    ("battery_min_cell_temperature_c", "min_cell_temperature"),
    ("battery_max_cell_voltage_v", "max_cell_voltage"),
    ("battery_min_cell_voltage_v", "min_cell_voltage"),
    ("battery_cycle_count", "cycle_count"),
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

# How long a cached energy read stays usable when a later one fails. A daily
# counter moves by hundredths of a kWh a minute, so a value a couple of minutes
# old is still today's total; one nobody has managed to read for an hour is not,
# and reporting it as current would be a stale number wearing a fresh timestamp.
ENERGY_MAX_AGE = timedelta(minutes=5)

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

    # The library indexes modules from zero; slots are 1-based to match the
    # battery_module1..4 columns in the registry. The library's names for the
    # cell extremes also do not follow its own bank naming, hence the spelling
    # differences below.
    return BatteryModuleSample(
        serial=serial,
        slot=index + 1,
        soc_pct=_reading(module, "soc"),
        soh_pct=_reading(module, "soh"),
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
        remaining_capacity_ah=_reading(module, "current_capacity"),
        full_capacity_ah=_reading(module, "max_capacity"),
        charge_current_limit_a=_reading(module, "charge_current_limit"),
        discharge_current_limit_a=_reading(module, "discharge_current_limit"),
        status_code=_int_reading(module, "status"),
        fault_code=_int_reading(module, "fault_code"),
        warning_code=_int_reading(module, "warning_code"),
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
    rather than a failed poll. The sample then carries the inverter's own
    terminal readings, which survive the dropout, with no battery state
    invented to fill the hole.

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

    battery_power = _signed_pair(runtime, "battery_charge_power", "battery_discharge_power")
    modules: tuple[BatteryModuleSample, ...] = ()

    if bank is not None:
        _collect(bank, _BANK_METRICS, readings)
        _collect_flags(bank, _BANK_FLAGS, readings)
        bank_power = _signed_pair(bank, "charge_power", "discharge_power")
        if bank_power is not None:
            battery_power = bank_power
        records = getattr(bank, "batteries", None) or ()
        modules = tuple(
            sample for sample in (_module_sample(record) for record in records) if sample
        )

    if battery_power is not None:
        readings["battery_power_w"] = battery_power

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

    async def read(self) -> Sample:
        """Read one sample of inverter and battery state.

        A read that reaches the inverter but finds no battery bank still
        returns the inverter's own readings; the missing bank is an absence,
        not a failure. A read that does not reach the inverter raises
        ConnectionError instead, and the caller turns that into a gap — drawn
        as a break in the chart rather than smoothed over.
        """
        try:
            runtime = await self._transport.read_runtime()
            bank = await self._transport.read_battery()
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
                data = await self._transport.read_energy()
            except (TransportError, OSError) as exc:
                logger.warning("energy counters unavailable: %s", exc)
            else:
                fresh: dict[str, float] = {}
                _collect(data, _ENERGY_METRICS, fresh)
                self._energy, self._energy_at = fresh, now
        if self._energy_at is None or now - self._energy_at > ENERGY_MAX_AGE:
            return {}
        return self._energy
