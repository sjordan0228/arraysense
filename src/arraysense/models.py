"""models.py — wire-independent sample model: Sample and BatteryModuleSample.

One poll of the inverter, plus one battery module at one instant. These types
are deliberately decoupled from the library that talks to the inverter; a later
task adapts that library's data classes into them, so a change of transport or
inverter family does not ripple through storage and the API.

Readings are carried as real-world values in the unit each metric registers
(see arraysense.metrics); the store layer encodes them to scaled integers on
the way in. Absent readings are None, never zero — a battery block that is
empty because CAN is down must not render as 0% SOC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BatteryModuleSample:
    """One battery module at one instant.

    Identity is the module's serial number, not its position. The inverter
    exposes only four battery register slots and rotates modules through them
    when more than four are present, so ``slot`` is not stable and must never
    be used as a key; it is carried because storage needs it.

    ``cell_delta_v`` is derived, not stored: the spread between the highest and
    lowest cell voltage, the earliest warning of a weak cell. The BMS does not
    always report cell extremes, and when it has not the delta is None — never
    a zero, so an unreported pack is distinguishable from one whose cells are
    perfectly balanced.
    """

    serial: str
    slot: int
    soc_pct: float | None = None
    soh_pct: float | None = None
    voltage_v: float | None = None
    current_a: float | None = None
    temperature_c: float | None = None
    cycle_count: int | None = None
    cell_max_voltage_v: float | None = None
    cell_min_voltage_v: float | None = None
    cell_max_temperature_c: float | None = None
    cell_min_temperature_c: float | None = None
    # Which cell carried each extreme. A rising delta tells you a pack is
    # drifting; these tell you which cell to inspect.
    cell_max_voltage_num: int | None = None
    cell_min_voltage_num: int | None = None
    cell_max_temperature_num: int | None = None
    cell_min_temperature_num: int | None = None
    # Energy in amp-hours rather than percent, and the ceiling this module's
    # own BMS is imposing. Two modules at the same SOC can hold different
    # energy, and a bank that stops charging early is usually one module
    # throttling rather than all four.
    remaining_capacity_ah: float | None = None
    full_capacity_ah: float | None = None
    charge_current_limit_a: float | None = None
    discharge_current_limit_a: float | None = None
    status_code: int | None = None
    fault_code: int | None = None
    warning_code: int | None = None

    def __post_init__(self) -> None:
        """Validate identity and slot.

        ``slot`` is 1-based to match the registry's ``battery_module1..4``
        column names, and it describes where the inverter placed the pack — not
        which pack it is. The inverter library reports a 0-based
        ``battery_index``, so an adapter must add one; validating here turns
        that off-by-one into an immediate error rather than a column name that
        silently does not exist. It is checked to be a positive integer but not
        capped, because a bank may hold more than four packs: the store resolves
        a reading's scale by template, so the slot number no longer bounds what
        can be stored. The old 1..4 cap existed when the registry expanded
        per-slot column names over exactly four slots and the write path looked
        a spec up by slot number — a slot outside that range located no column.
        The serial is the module's only stable identity, and a blank one would
        put readings from a real pack under a key nothing can match again.
        """
        if not self.serial:
            raise ValueError("serial must not be empty; it is the module identity")
        if self.slot < 1:
            raise ValueError(f"slot must be a positive integer (1-based), got {self.slot}")

    @property
    def cell_delta_v(self) -> float | None:
        """Spread between highest and lowest cell voltage, in volts.

        Derived here rather than stored, so a weak cell shows up the moment the
        extremes do and nothing downstream has to recompute it. None when the
        BMS reported no cell extremes — never a zero, which keeps a pack that
        said nothing distinguishable from one whose cells are perfectly
        balanced, since that pack genuinely reads 0.0.
        """
        if self.cell_max_voltage_v is None or self.cell_min_voltage_v is None:
            return None
        return self.cell_max_voltage_v - self.cell_min_voltage_v


@dataclass(frozen=True)
class Sample:
    """One poll of the inverter.

    Holds the poll timestamp, the inverter-level readings keyed by metric
    name, and whatever battery modules reported. A poll that could not reach
    the inverter is data too: it gets stored and later rendered as a break in
    the chart rather than smoothed over. Such a poll carries its reason in
    ``error`` and has no readings — not zeroed readings.
    """

    timestamp: datetime
    readings: dict[str, float]
    battery_modules: tuple[BatteryModuleSample, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        """Reject naive timestamps and contradictory failed polls.

        A naive datetime has no timezone and would be interpreted as local
        time inconsistently across polls. Timezone-aware timestamps are a
        contract for everything downstream.

        A poll cannot both have failed and carry readings. A failure is a
        recorded gap, rendered later as a break in the chart; allowing readings
        alongside it would produce a row that is simultaneously a measurement
        and an absence, and the store would write both.
        """
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.error is not None and (self.readings or self.battery_modules):
            raise ValueError(
                "a failed poll carries no readings; it is a recorded gap, not a partial result"
            )

    @property
    def is_failed(self) -> bool:
        """Report whether this poll produced no reading.

        Not only an unreachable inverter: a reply the driver could not turn into
        a sample is recorded the same way, and there the inverter answered. Both
        are holes in the history, which is what this field is asked about — the
        reason says which one it was.

        The test is the error reason, never the absence of readings: an
        inverter can legitimately report no battery modules, and treating that
        as a failure would punch a gap into a chart that has no gap in it.
        """
        return self.error is not None

    @classmethod
    def failed(cls, timestamp: datetime, reason: str) -> Sample:
        """Build a sample representing a poll that produced no reading.

        Usually an inverter that could not be reached, but also a reply that
        could not be turned into a sample — the reason distinguishes them, and
        the collector records both as gaps because both are holes in the history.

        The absence is data: it is stored and rendered as a break in the chart
        rather than smoothed over, so an outage stays visible instead of being
        interpolated away. The result carries the reason it failed and no
        readings — not zeroed readings — and its timestamp must be
        timezone-aware like any other sample's.
        """
        return cls(timestamp=timestamp, readings={}, error=reason)
