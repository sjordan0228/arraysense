"""metrics.py — the one place a metric's name, unit, scale and bounds are defined.

The database schema, the validation layer and the HTTP API all derive from this
list, so adding a metric is a one-line change here rather than an edit in four
files that drift apart the first time one of them is forgotten.

Two decisions shape every spec. Values are stored as scaled integers rather
than floats — SQLite encodes small integers compactly, roughly halving row
size — and each metric carries plausible bounds so a decode error is
detectable on the way in. The reference product recorded a battery power of
25,583 W as fact, about double what an 18kPV can deliver into a 48 V bank;
bounds exist to stop that. Bounds are generous enough that a real reading is
never rejected and tight enough that a garbage one is.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Aggregation = Literal["mean", "min", "max", "last"]


@dataclass(frozen=True)
class MetricSpec:
    """One stored measurement: column name, unit, integer scale, bounds.

    Immutable because the schema and the validation layer both depend on a
    stable description of every column. A real-world value is encoded to its
    stored integer form by multiplying by ``scale`` and rounding to nearest,
    and decoded back by dividing.

    ``aggregation`` says how the metric collapses when a rollup covers many
    readings. Most measurements average, but averaging is wrong for some: a
    cycle counter only ever climbs, an extreme should stay an extreme, and the
    average of two cell *numbers* is a cell that does not exist.
    """

    name: str
    unit: str
    scale: int
    lower: float
    upper: float
    aggregation: Aggregation = "mean"

    def encode(self, value: float) -> int:
        """Encode a reading, in this metric's own unit, to the integer that is stored.

        Rounding is to nearest rather than truncating, so a half unit is not
        silently dropped on the way in: 21.7 °C at a scale of 10 stores as 217.
        Every write goes through here so the multiplier lives in one place, and
        correcting a scale is an edit to one line of the registry rather than a
        hunt through the code that writes it.
        """
        return round(value * self.scale)

    def decode(self, stored: int) -> float:
        """Decode a stored integer back to a reading in this metric's own unit.

        The inverse of ``encode``, and the only place that division belongs. What
        comes out of the database is a bare integer carrying no record of what it
        was scaled by, so code that divides by a hard-coded constant is one
        corrected scale away from being quietly wrong — and the scales here are
        not uniform. Currents alone span three: tenths for the bank, hundredths
        per string, and tenths again for the BMS limits.

        The result is a float even for the metrics that are conceptually whole
        numbers — fault codes, flags, cell indices, module counts — because their
        scale of 1 still goes through a true division. Anything rendering those
        has to do its own formatting.
        """
        return stored / self.scale

    def within_bounds(self, value: float) -> bool:
        """Report whether a reading is plausible for this metric.

        The test is on the real-world value rather than the stored integer, so a
        decode error is caught as it arrives from the transport and never reaches
        the database — a chart that has to explain a battery delivering 25 kW is
        already too late. Both ends are inclusive, which is load-bearing at the
        floors chosen deliberately below: during a power cut the inverter really
        does measure 0 V on the grid, and that reading has to pass.
        """
        return self.lower <= value <= self.upper


# Each scale below matches the resolution the register actually carries, taken
# from pylxpweb's register table rather than guessed. A voltage the inverter
# reports to a tenth gains nothing from being stored to a thousandth; it only
# makes the integer wider on every row for the life of the database.
INVERTER_METRICS: tuple[MetricSpec, ...] = (
    # --- Solar strings ------------------------------------------------------
    MetricSpec("pv_total_power_w", "W", 1, 0.0, 20000.0),
    MetricSpec("pv1_power_w", "W", 1, 0.0, 8000.0),
    MetricSpec("pv2_power_w", "W", 1, 0.0, 8000.0),
    MetricSpec("pv3_power_w", "W", 1, 0.0, 8000.0),
    MetricSpec("pv1_voltage_v", "V", 10, 0.0, 600.0),
    MetricSpec("pv2_voltage_v", "V", 10, 0.0, 600.0),
    MetricSpec("pv3_voltage_v", "V", 10, 0.0, 600.0),
    # String current alongside string power is what separates a shaded panel
    # from a failing one. Power falling with voltage held up means current is
    # gone — a shadow or a soiled panel. Power falling with current held up
    # means a string has partly disconnected. Power alone cannot tell them
    # apart.
    MetricSpec("pv1_current_a", "A", 100, 0.0, 30.0),
    MetricSpec("pv2_current_a", "A", 100, 0.0, 30.0),
    MetricSpec("pv3_current_a", "A", 100, 0.0, 30.0),
    # --- House load and the grid --------------------------------------------
    # Load can read slightly negative on some inverters; 65 such readings appear
    # in 22 months of reference data, so zero is the wrong floor.
    MetricSpec("load_power_w", "W", 1, -2000.0, 20000.0),
    # Grid power is signed: export below zero, import above. Battery power is
    # signed too — charge above the line, discharge below — so each chart needs
    # one axis rather than two. Both lower bounds are negative by design.
    MetricSpec("grid_power_w", "W", 1, -20000.0, 20000.0),
    # The same quantity per leg. A split-phase service can be importing on one
    # leg while exporting on the other, and the combined figure above nets that
    # out to something close to zero — which reads as a balanced house when it
    # is in fact a badly balanced one.
    MetricSpec("grid_power_l1_w", "W", 1, -20000.0, 20000.0),
    MetricSpec("grid_power_l2_w", "W", 1, -20000.0, 20000.0),
    # Grid voltage and frequency floor at zero, not at a "normal" value: during a
    # power cut the inverter genuinely measures 0, and a hybrid system exists to
    # ride those out. Flagging an outage as a decode error would hide the event
    # the owner most wants recorded.
    MetricSpec("grid_voltage_v", "V", 10, 0.0, 300.0),
    MetricSpec("grid_frequency_hz", "Hz", 100, 0.0, 65.0),
    MetricSpec("inverter_current_a", "A", 100, 0.0, 150.0),
    # The two directions the inverter's AC side can work in, each reported as
    # its own always-positive register: selling to the grid, and charging the
    # battery from it.
    MetricSpec("inverter_power_w", "W", 1, 0.0, 20000.0),
    MetricSpec("rectifier_power_w", "W", 1, 0.0, 20000.0),
    MetricSpec("ac_couple_power_w", "W", 1, 0.0, 20000.0),
    # Real power over apparent power. A house full of motors and switch-mode
    # supplies sits below 1, and a figure that drifts down over months is a
    # load profile changing rather than an inverter fault.
    MetricSpec("power_factor", "", 1000, -1.0, 1.0),
    # --- Backup output (EPS) ------------------------------------------------
    # What the protected loads are actually drawing. On this hardware the two
    # legs are wildly unequal in normal use — 1937 W against 456 W in one
    # reference read — so the per-leg figures are the point, not a refinement.
    MetricSpec("eps_power_w", "W", 1, 0.0, 20000.0),
    MetricSpec("eps_l1_power_w", "W", 1, 0.0, 20000.0),
    MetricSpec("eps_l2_power_w", "W", 1, 0.0, 20000.0),
    # Apparent power beside real power gives the power factor of each leg,
    # which is how an inductive load shows up as a leg working harder than its
    # watts suggest.
    MetricSpec("eps_apparent_power_va", "VA", 1, 0.0, 25000.0),
    MetricSpec("eps_l1_apparent_power_va", "VA", 1, 0.0, 25000.0),
    MetricSpec("eps_l2_apparent_power_va", "VA", 1, 0.0, 25000.0),
    MetricSpec("eps_l1_voltage_v", "V", 10, 0.0, 200.0),
    MetricSpec("eps_l2_voltage_v", "V", 10, 0.0, 200.0),
    # The backup output measured line to line, which is the ~240 V figure an
    # owner reads off a meter. Not the sum of the two legs above: it is a
    # separate register measured at a different point, and the product being
    # replaced records this one rather than the legs.
    MetricSpec("eps_voltage_v", "V", 10, 0.0, 300.0),
    MetricSpec("eps_frequency_hz", "Hz", 100, 0.0, 65.0),
    # --- Generator ----------------------------------------------------------
    # Zero on a system with no generator, and structurally so — but plenty of
    # off-grid installations have one, and an owner who fits a generator should
    # not discover the monitor never recorded it running.
    MetricSpec("generator_power_w", "W", 1, 0.0, 20000.0),
    MetricSpec("generator_voltage_v", "V", 10, 0.0, 300.0),
    MetricSpec("generator_frequency_hz", "Hz", 100, 0.0, 65.0),
    # --- Battery bank -------------------------------------------------------
    MetricSpec("battery_power_w", "W", 1, -20000.0, 20000.0),
    MetricSpec("battery_voltage_v", "V", 10, 35.0, 65.0),
    # Scale 10: the BMS reports current to tenths. The vendor document claims
    # hundredths and the hardware does not deliver them — pylxpweb's register
    # definition records the discrepancy as "empirical: 0.1A scale, doc says
    # 0.01A". Scaling finer than the source resolution only inflates the stored
    # integer.
    MetricSpec("battery_current_a", "A", 10, -400.0, 400.0),
    # The inverter's own measurement of the same voltage, taken at its
    # terminals rather than at the cells. The gap between this and
    # battery_voltage_v is the drop across the cabling, so a busbar or lug
    # going high-resistance shows up as that gap widening under load. Neither
    # reading alone can show it.
    MetricSpec("battery_voltage_inv_sample_v", "V", 10, 35.0, 65.0),
    MetricSpec("battery_soc_pct", "%", 1, 0.0, 100.0),
    MetricSpec("battery_soh_pct", "%", 1, 0.0, 100.0),
    # State of charge as a percentage hides how much energy that is. The bank
    # reports both the amp-hours left and the amp-hours it holds when full, and
    # the second of those falling over years is the honest measure of ageing —
    # more honest than a state-of-health figure the BMS computes to its own
    # rules.
    MetricSpec("battery_remaining_capacity_ah", "Ah", 10, 0.0, 5000.0),
    MetricSpec("battery_full_capacity_ah", "Ah", 10, 0.0, 5000.0, "last"),
    # How many modules the bank is answering for. A pack that drops off the CAN
    # bus takes this down with it, which is a clearer alarm than noticing one
    # module's readings have gone stale.
    MetricSpec("battery_module_count", "modules", 1, 0.0, 16.0, "last"),
    MetricSpec("battery_cycle_count", "cycles", 1, 0.0, 20000.0, "max"),
    # The bank's hottest and coldest cell, and its highest and lowest. These
    # average over a rollup window rather than taking the extreme: they are the
    # headline trend, and the per-module columns below already keep the
    # extremes with max and min aggregation.
    #
    # The PowerPro BMS measures -40..100 °C. Bounds must span the full sensor
    # range: a pack in thermal protection is exactly the event worth keeping, and
    # clipping it would discard the reading that explains a shutdown.
    MetricSpec("battery_temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 100.0),
    MetricSpec("battery_min_cell_temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 100.0),
    MetricSpec("battery_max_cell_voltage_v", "V", 1000, 2.0, 4.2),
    MetricSpec("battery_min_cell_voltage_v", "V", 1000, 2.0, 4.2),
    # --- BMS limits and permissions -----------------------------------------
    # The ceiling the BMS is currently imposing, which is not a constant: it
    # drops when the pack is cold, nearly full or nearly empty. Aggregated with
    # min so a rollup shows the tightest limit in the window — an average would
    # smooth away the throttling that explains why charging stalled.
    MetricSpec("bms_charge_current_limit_a", "A", 10, 0.0, 2000.0, "min"),
    MetricSpec("bms_discharge_current_limit_a", "A", 10, 0.0, 2000.0, "min"),
    MetricSpec("bms_charge_voltage_ref_v", "V", 10, 40.0, 70.0, "last"),
    MetricSpec("bms_discharge_cutoff_v", "V", 10, 30.0, 60.0, "last"),
    # Flags, stored as 0 or 1. The permissions aggregate with min and the force
    # request with max, so any minute in which the BMS refused a direction, or
    # demanded a charge, survives into the hourly and daily tiers instead of
    # being averaged into a fraction that means nothing.
    MetricSpec("bms_allow_charge", "flag", 1, 0.0, 1.0, "min"),
    MetricSpec("bms_allow_discharge", "flag", 1, 0.0, 1.0, "min"),
    MetricSpec("bms_force_charge", "flag", 1, 0.0, 1.0, "max"),
    # --- Inverter internals -------------------------------------------------
    # Three separate thermal sensors, and they disagree by a dozen degrees:
    # 59 °C internal against 68 and 71 °C on the two heatsinks in one reference
    # read. There is no fan-speed register to read, so heatsink temperature
    # climbing while power output holds steady is the only signal available
    # that a fan is failing or an intake is blocked.
    MetricSpec("inverter_temperature_c", "\N{DEGREE SIGN}C", 1, -40.0, 150.0),
    MetricSpec("radiator1_temperature_c", "\N{DEGREE SIGN}C", 1, -40.0, 150.0),
    MetricSpec("radiator2_temperature_c", "\N{DEGREE SIGN}C", 1, -40.0, 150.0),
    MetricSpec("board_temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 150.0),
    # The DC link between the PV/battery side and the AC side. The two halves
    # should track each other; a widening split points at the inverter's own
    # power stage rather than at anything outside it.
    MetricSpec("bus_voltage_1_v", "V", 10, 0.0, 600.0),
    MetricSpec("bus_voltage_2_v", "V", 10, 0.0, 600.0),
    # --- Energy -------------------------------------------------------------
    # The inverter counts its own energy, and these are those counters rather
    # than anything computed here. Integrating stored power would work on a
    # good day and quietly undercount on any day with a gap in collection: a
    # poll missed at peak is energy that never gets counted, and the resulting
    # total looks entirely plausible. Against the reference system these read
    # within half a percent of an integration over a day with no gaps, and they
    # keep counting through our downtime, which is the whole point.
    #
    # Both families aggregate with max. The daily counters reset at midnight
    # and climb through the day, so the largest value in a rollup window is the
    # one at the end of it; the lifetime counters only ever climb. An average
    # of either would be a number that never existed.
    MetricSpec("pv_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("pv1_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("pv2_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("pv3_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("load_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("eps_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    # Charge and discharge are counted separately by the hardware, and that
    # separation is worth keeping: their difference across a full cycle is
    # round-trip loss, which a single signed figure nets away for good.
    MetricSpec("battery_charge_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("battery_discharge_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("grid_import_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("grid_export_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("ac_charge_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    MetricSpec("inverter_energy_today_kwh", "kWh", 10, 0.0, 2000.0, "max"),
    # Lifetime counters. These carry history from before this software existed
    # — the reference inverter reported 36,246 kWh of production on the first
    # day it was read — and because they are monotonic, the energy over any
    # period is the value at the end minus the value at the start. That holds
    # even across a collection outage, which no integration can manage.
    MetricSpec("pv_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("pv1_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("pv2_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("pv3_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("load_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("eps_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("battery_charge_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("battery_discharge_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("grid_import_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("grid_export_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("ac_charge_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    MetricSpec("inverter_energy_total_kwh", "kWh", 10, 0.0, 1000000.0, "max"),
    # --- Status and diagnostics ---------------------------------------------
    # Bitfields, not measurements. They aggregate with max rather than mean
    # because the average of two bitfields is not a bitfield, and because a
    # fault that lasted ten seconds must still be visible in the daily tier.
    #
    # Three sources raise codes — the inverter, the bank, and each module — so
    # all three are named for their source. The per-module columns are the bare
    # ones, because they are already qualified by the module they belong to.
    MetricSpec("device_status", "code", 1, 0.0, 65535.0, "last"),
    MetricSpec("inverter_fault_code", "code", 1, 0.0, 4294967295.0, "max"),
    MetricSpec("inverter_warning_code", "code", 1, 0.0, 4294967295.0, "max"),
    # The BMS raises its own faults, separately from the inverter's. A pack
    # complaining while the inverter reports no fault of its own is the case
    # these two exist to make visible.
    MetricSpec("battery_fault_code", "code", 1, 0.0, 65535.0, "max"),
    MetricSpec("battery_warning_code", "code", 1, 0.0, 65535.0, "max"),
    # Total powered-on seconds, monotonic until the counter is reset. Stored so
    # a restart is distinguishable from a gap in collection: our own downtime
    # leaves this climbing across the hole, the inverter's resets it.
    MetricSpec("inverter_run_time_s", "s", 1, 0.0, 4294967295.0, "max"),
)

_MODULE_SLOTS = 4

# Per-module metrics share one template each; the registry expands each across
# the four slots the inverter exposes. Adding a per-module metric is one line
# here rather than four edits.
_MODULE_METRIC_SPECS: tuple[MetricSpec, ...] = (
    MetricSpec("soc_pct", "%", 1, 0.0, 100.0),
    MetricSpec("soh_pct", "%", 1, 0.0, 100.0),
    MetricSpec("voltage_v", "V", 100, 35.0, 65.0),
    MetricSpec("current_a", "A", 10, -200.0, 200.0),
    MetricSpec("temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 100.0),
    MetricSpec("cycle_count", "cycles", 1, 0.0, 10000.0, "max"),
    # Amp-hours, not just percent. Four modules can sit at the same state of
    # charge while holding different energy, and a module whose full capacity
    # has quietly fallen is the one dragging the bank down.
    MetricSpec("remaining_capacity_ah", "Ah", 10, 0.0, 1000.0),
    MetricSpec("full_capacity_ah", "Ah", 10, 0.0, 1000.0, "last"),
    # The limit each module's own BMS is imposing. The bank-level limit is
    # their sum, so it says charging was throttled without saying which module
    # did the throttling; these say which. Aggregated with min for the same
    # reason as the bank's.
    MetricSpec("charge_current_limit_a", "A", 10, 0.0, 500.0, "min"),
    MetricSpec("discharge_current_limit_a", "A", 10, 0.0, 500.0, "min"),
    # Per-module status and alarms. A single pack in protection is invisible at
    # bank level until it has pulled the whole bank's limits down with it.
    MetricSpec("status_code", "code", 1, 0.0, 65535.0, "last"),
    MetricSpec("fault_code", "code", 1, 0.0, 65535.0, "max"),
    MetricSpec("warning_code", "code", 1, 0.0, 65535.0, "max"),
    MetricSpec("cell_max_voltage_v", "V", 1000, 2.0, 4.2, "max"),
    MetricSpec("cell_min_voltage_v", "V", 1000, 2.0, 4.2, "min"),
    MetricSpec("cell_max_temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 100.0, "max"),
    MetricSpec("cell_min_temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 100.0, "min"),
    # Which cell carried each extreme. Without these, a rising cell delta says
    # a pack is drifting but not which cell to look at — and the inverter
    # reports the indices, so dropping them loses information for good.
    MetricSpec("cell_max_voltage_num", "cell", 1, 0.0, 32.0, "last"),
    MetricSpec("cell_min_voltage_num", "cell", 1, 0.0, 32.0, "last"),
    MetricSpec("cell_max_temperature_num", "cell", 1, 0.0, 32.0, "last"),
    MetricSpec("cell_min_temperature_num", "cell", 1, 0.0, 32.0, "last"),
)

BATTERY_MODULE_METRICS: tuple[MetricSpec, ...] = tuple(
    MetricSpec(
        name=f"battery_module{slot}_{template.name}",
        unit=template.unit,
        scale=template.scale,
        lower=template.lower,
        upper=template.upper,
        aggregation=template.aggregation,
    )
    for slot in range(1, _MODULE_SLOTS + 1)
    for template in _MODULE_METRIC_SPECS
)

# Every stored column, in registry order. Names must stay unique across the
# whole list, including the per-module expansions; a duplicate would silently
# corrupt the wide-row schema.
ALL_METRICS: tuple[MetricSpec, ...] = INVERTER_METRICS + BATTERY_MODULE_METRICS

_BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in ALL_METRICS}


def column_names() -> tuple[str, ...]:
    """Return the column name of every stored metric, in registry order.

    Flattens the inverter metrics and the per-module expansions into the one
    sequence a caller can ask "does this name exist" against, without having to
    know how the registry is assembled. Nothing is validated on the way out —
    the names are returned as registered — so a caller checking for duplicates
    has to do it itself.
    """
    return tuple(spec.name for spec in ALL_METRICS)


def lookup(name: str) -> MetricSpec:
    """Return the spec registered under ``name``, raising KeyError if it is unknown.

    A miss raises rather than returning a sentinel, and the error names the
    offending string. Every caller — validation, the rollup SQL, and the store's
    encode and decode paths — is asking about a name it composed from this same
    registry, often by prefixing a bare module metric with its slot. So a miss
    is a typo in our own code rather than a metric that happens to be absent,
    and treated as "no such metric" it would come out the far end as a column
    quietly missing from every row.
    """
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise KeyError(f"unknown metric name: {name!r}") from exc
