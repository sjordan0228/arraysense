"""import_solar_assistant.py — one-time import of SolarAssistant's history.

Twenty-two months of the reference installation lived in SolarAssistant's
InfluxDB and would otherwise have been thrown away at the switchover. This
reads the line-protocol export, maps it onto the metric registry, and fills the
tiers as if the collector had been running all along.

Three things about the source shaped the whole design, and each was measured
rather than assumed:

*The file is series-major, not time-major.* InfluxDB exports one series at a
time within each seven-day shard, in ascending time order. So a point for a
given instant arrives 34 separate times, spread megabytes apart. Grouping them
into whole samples would need the file sorted, and sorting 141 million points
needs more scratch space than either machine has. Instead every point is
accumulated straight into its bucket as it is read, which needs one pass and
bounded memory.

*SolarAssistant kept no lifetime kWh counters.* It recorded instantaneous power
every eleven seconds and an hourly mean, and nothing else. Everything this
project computes about energy is a difference of two counter readings, so the
counters have to be reconstructed — see ``synthesise_counters``, which is the
one place in the import where a number is derived rather than copied.

*Its battery temperature is the raw register.* It ranges 32 to 491 over a year,
which is deci-degrees Celsius, and SolarAssistant displays it as Fahrenheit. Ten
degrees of that error is the difference between a warm bank and a fire.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import math
import sqlite3
import sys
import time
from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from arraysense.metrics import INVERTER_METRICS, Aggregation, lookup
from arraysense.store.rollup import collapse_policy
from arraysense.store.schema import INVERTER_TIERS

logger = logging.getLogger("import_sa")

HOUR = 3600
MINUTE = 60


def _as_is(value: float) -> float:
    """The identity, for a series already in the unit its column expects."""
    return value


def _fahrenheit(value: float) -> float:
    """Fahrenheit to Celsius.

    Established by comparison rather than by unit label. At 2026-08-06T20:44:56Z
    this project's own read of the inverter reported its two radiators at 68.0
    and 71.0 °C; SolarAssistant recorded 157.7 at the same moment, and
    (157.7 - 32) x 5/9 is 69.9 — between the two. Stored raw it would be an
    inverter running at 158 °C, which the registry rejects outright, and 22% of
    the imported values did fall outside the bounds before this was applied.
    """
    return (value - 32.0) * 5.0 / 9.0


@dataclass(frozen=True)
class Source:
    """One SolarAssistant series and where it lands in the registry.

    ``convert`` carries the unit correction. It is not decoration: without it
    the inverter temperature imports as 158 °C.
    """

    measurement: str
    field: str
    metric: str
    convert: Callable[[float], float] = _as_is


# Verified against the hardware on 7 August 2026 by reading a night of data out
# of InfluxDB and checking that the four powers balance hour by hour:
#
#   22:00Z  PV 4413  battery -2544  load 6743  grid 0     SOC 55.5%
#   02:00Z  PV    0  battery -7435  load 7121  grid 0     SOC 20.1%
#   03:00Z  PV    0  battery -3247  load 5705  grid 2622  SOC  8.8%
#
# so SolarAssistant signs battery power positive when charging and grid power
# positive when importing — the same way round as this project. Do not "fix"
# either sign; it was checked, and the unlabelled form of that query says the
# opposite.
SOURCES: tuple[Source, ...] = (
    Source("PV power", "combined", "pv_total_power_w"),
    Source("PV power 1", "inverter_0", "pv1_power_w"),
    Source("PV power 2", "inverter_0", "pv2_power_w"),
    Source("PV power 3", "inverter_0", "pv3_power_w"),
    Source("PV voltage 1", "inverter_0", "pv1_voltage_v"),
    Source("PV voltage 2", "inverter_0", "pv2_voltage_v"),
    Source("PV voltage 3", "inverter_0", "pv3_voltage_v"),
    Source("PV current 1", "inverter_0", "pv1_current_a"),
    Source("PV current 2", "inverter_0", "pv2_current_a"),
    Source("PV current 3", "inverter_0", "pv3_current_a"),
    # SolarAssistant's "Load power" is genuine house load. Note this is *not*
    # the trap pylxpweb sets, where its own load_power is register 27 and means
    # power imported from grid.
    Source("Load power", "combined", "load_power_w"),
    # The essential loads are the ones behind the backup panel, which is what
    # eps_power_w means here.
    Source("Load power essential", "inverter_0", "eps_power_w"),
    Source("Grid power", "combined", "grid_power_w"),
    Source("Grid voltage", "inverter_0", "grid_voltage_v"),
    Source("Grid frequency", "inverter_0", "grid_frequency_hz"),
    Source("AC output voltage", "inverter_0", "eps_voltage_v"),
    Source("Battery power", "combined", "battery_power_w"),
    Source("Battery voltage", "inverter_0", "battery_voltage_v"),
    Source("Battery current", "inverter_0", "battery_current_a"),
    Source("Battery state of charge", "combined", "battery_soc_pct"),
    # The heatsink, not the internal sensor, and the distinction is thirteen
    # degrees. At 2026-08-06T20:44:56Z SolarAssistant read 157.7 °F — 69.9 °C —
    # while this project's own read of the same inverter reported radiator 1 at
    # 68.0, radiator 2 at 71.0, and the internal sensor at 59.0. Live now, the
    # gap is the same shape: radiator 1 at 56 °C against an internal 45.
    # inverter_temperature_c is fed by the internal register from cutover
    # onwards, so putting the heatsink into it would leave a thirteen degree
    # step at the seam and present it as the inverter warming up.
    Source("Inverter temperature", "inverter_0", "radiator1_temperature_c", convert=_fahrenheit),
    Source("Generator power", "inverter_0", "generator_power_w"),
    # Weather, recorded by SolarAssistant from its own provider. Fahrenheit in
    # the export, Celsius in the registry — same correction as the inverter
    # temperature above. Recoverable at all because #5 gave them a metric.
    #
    # FIELD KEY "combined" IS UNVERIFIED: no SA export file was reachable in
    # this environment. The key follows the convention for aggregate
    # (non-inverter-specific) series. If a real export carries a different
    # field key (e.g. the measurement name alone), adjust both Source lines.
    # THE FAHRENHEIT ASSUMPTION IS ALSO UNVERIFIED: "Inverter temperature" was
    # confirmed in Fahrenheit, but "Outside temperature" has no such check. If
    # a Texas summer day imports near freezing, the export is already Celsius
    # and the convert=_fahrenheit must be removed.
    Source("Outside temperature", "combined", "outside_temperature_c", convert=_fahrenheit),
    Source("Cloud cover", "combined", "cloud_cover_pct"),
)

# SolarAssistant's hourly means, in watts. Divided by a thousand each is that
# hour's energy in kWh, and they are the only record of energy that exists.
HOURLY_ENERGY: dict[str, str] = {
    "PV power hourly": "pv_energy_total_kwh",
    "Load power hourly": "load_energy_total_kwh",
    "Grid power in hourly": "grid_import_energy_total_kwh",
    "Grid power out hourly": "grid_export_energy_total_kwh",
    "Battery power in hourly": "battery_charge_energy_total_kwh",
    "Battery power out hourly": "battery_discharge_energy_total_kwh",
}

# Series deliberately left on the floor, and why. Recorded here so the next
# person can see the decision was made rather than missed.
DROPPED: dict[str, str] = {
    "Load power non-essential": "no metric for it; load_power_w already holds the whole house",
    "Load power|inverter_0": "duplicate of the combined field",
    "PV power|inverter_0": "duplicate of the combined field",
    "PV power predicted": "a forecast, not a measurement; it must never sit in the same column",
    "PV power predicted hourly": "as above",
    "Grid power hourly": "unsigned; the in/out pair carries the direction",
    "Battery power hourly": "unsigned; the in/out pair carries the direction",
    "CPU temperature": "the Raspberry Pi's own health, which retires with it",
    "Free storage": "as above",
    "Auxiliary load power": "two points in twenty-two months",
    # Deliberately dropped, after checking it rather than assuming it. At
    # 2026-08-06T20:44:56Z the BMS reported its hottest cell at 39.0 °C and its
    # coldest at 38.0. SolarAssistant recorded 219.2 at the same moment: 21.9 °C
    # read as deci-Celsius, 104 °C read as Fahrenheit, -5.6 °C read as
    # deci-Fahrenheit. None of them is 39. The first is the only plausible one,
    # and 21.9 °C beside cells at 38 is an ambient sensor rather than the pack.
    # battery_temperature_c is fed by bms_max_cell_temperature from cutover
    # onwards, so importing a different sensor into it would put a seventeen
    # degree step in the chart at the seam and call it a temperature change.
    "Battery temperature": "an ambient sensor, not the cells; see the note above",
    # There is no SolarAssistant series for it. The internal sensor reads about
    # thirteen degrees below the heatsink, so nothing here can stand in for it.
    "(inverter_temperature_c)": "no source; SA's Inverter temperature is the heatsink",
}


def _lookup_index() -> dict[tuple[str, str], Source]:
    """Index the mapping by the pair a line actually carries."""
    return {(s.measurement, s.field): s for s in SOURCES}


@dataclass
class Accumulator:
    """One metric's buckets at one resolution.

    Flat arrays rather than a dict of dicts: a year of minutes across two dozen
    metrics is thirteen million buckets, and the dict overhead alone would not
    fit. Indexed by bucket number relative to ``origin``.
    """

    slots: int
    total: array[float] = field(init=False)
    count: array[int] = field(init=False)
    latest: array[float] = field(init=False)
    latest_at: array[int] = field(init=False)
    peak: array[float] = field(init=False)
    trough: array[float] = field(init=False)

    def __post_init__(self) -> None:
        """Allocate the flat arrays, one slot per bucket."""
        self.total = array("d", bytes(8 * self.slots))
        self.count = array("i", bytes(4 * self.slots))
        self.latest = array("d", bytes(8 * self.slots))
        self.latest_at = array("q", bytes(8 * self.slots))
        self.peak = array("d", [-math.inf]) * self.slots
        self.trough = array("d", [math.inf]) * self.slots

    def add(self, index: int, value: float, at: int) -> None:
        """Fold one reading into its bucket, keeping every statistic a policy might want."""
        self.total[index] += value
        self.count[index] += 1
        if value > self.peak[index]:
            self.peak[index] = value
        if value < self.trough[index]:
            self.trough[index] = value
        if at >= self.latest_at[index]:
            self.latest_at[index] = at
            self.latest[index] = value

    def value(self, index: int, how: str) -> float | None:
        """The bucket's value under the collapse policy ``tier_policy`` assigned it.

        An empty bucket is None and never zero: the import lands beside live
        readings in the same tables, and an hour SolarAssistant never recorded
        must not read as an hour in which nothing happened.

        The four policies are the rollup's four, and an unrecognised one is an
        error rather than a fallback. Falling through to the latest value is
        what let this quietly disagree with ``store.rollup``, which raises on
        exactly the same input.

        Raises:
            AssertionError: ``how`` is not one of the registry's four policies,
                which is a programming error, not a data condition.
        """
        if self.count[index] == 0:
            return None
        if how == "mean":
            return float(self.total[index]) / int(self.count[index])
        if how == "max":
            return float(self.peak[index])
        if how == "min":
            return float(self.trough[index])
        if how == "last":
            return float(self.latest[index])
        raise AssertionError(f"unhandled aggregation policy: {how!r}")


def parse(path: Path) -> Iterator[tuple[str, str, float, int]]:
    """Yield (measurement, field, value, epoch seconds) from a line-protocol file.

    Measurement names carry backslash-escaped spaces. The DDL lines have no
    trailing timestamp and are skipped by that fact rather than by matching text,
    since a comment marker does not cover all of them.
    """
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line or line[0] == "#":
                continue
            a = line.rfind(" ")
            if a < 0:
                continue
            tail = line[a + 1 : -1]
            if not tail.isdigit():
                continue
            b = line.rfind(" ", 0, a)
            eq = line.find("=", b)
            if b < 0 or eq < 0:
                continue
            measurement = line[:b].replace("\\ ", " ").replace("\\,", ",")
            key = line[b + 1 : eq]
            raw = line[eq + 1 : a]
            # Line protocol types an integer by suffixing it: `combined=4917i`.
            # Passing that straight to float() raises, and a parser that skips
            # what it cannot read drops every integer-typed series in silence —
            # here, every power reading and the state of charge, which is to say
            # the entire point of the import. It looked like a working parser
            # because the voltages and currents are floats and came through.
            if raw and raw[-1] in "iu":
                raw = raw[:-1]
            try:
                value = float(raw)
            except ValueError:
                continue
            yield measurement, key, value, int(tail) // 1_000_000_000


@dataclass
class Buckets:
    """Every accumulator, plus the hourly energy read straight from the source."""

    origin_hour: int
    hours: int
    origin_minute: int
    minutes: int
    hourly: dict[str, Accumulator]
    minutely: dict[str, Accumulator]
    # Hourly kWh, last write wins. The export holds several versions of each
    # bucket because SolarAssistant rewrites the current hour as it fills, and
    # the raw file keeps every version where a query would show only the last.
    energy: dict[str, dict[int, tuple[float, int]]]
    # Full-cadence readings for the recent window the raw tier retains, keyed by
    # the instant they were taken. Held whole rather than aggregated because the
    # raw tier is what the Graphs page draws when somebody zooms into an hour,
    # and a minute average is not that.
    raw: dict[int, dict[str, float]] = field(default_factory=dict)
    seen: int = 0
    used: int = 0
    dropped_out_of_range: int = 0


def scan(paths: list[Path], first: int, last: int, minute_from: int, raw_from: int) -> Buckets:
    """One pass over every export file, filling every bucket."""
    index = _lookup_index()

    origin_hour = first // HOUR
    hours = last // HOUR - origin_hour + 2
    origin_minute = minute_from // MINUTE
    minutes = last // MINUTE - origin_minute + 2

    buckets = Buckets(
        origin_hour=origin_hour,
        hours=hours,
        origin_minute=origin_minute,
        minutes=minutes,
        hourly={s.metric: Accumulator(hours) for s in SOURCES},
        minutely={s.metric: Accumulator(minutes) for s in SOURCES},
        energy={metric: {} for metric in HOURLY_ENERGY.values()},
    )
    logger.info(
        "buckets sized: %s hours, %s minutes, %s metrics", hours, minutes, len(buckets.hourly)
    )

    started = time.time()
    for path in paths:
        logger.info("reading %s", path.name)
        for measurement, key, value, at in parse(path):
            buckets.seen += 1
            if buckets.seen % 20_000_000 == 0:
                rate = buckets.seen / (time.time() - started) / 1e6
                logger.info("  %.0fM points, %.2fM/s", buckets.seen / 1e6, rate)

            energy_metric = HOURLY_ENERGY.get(measurement)
            if energy_metric is not None and key == "combined":
                hour = at // HOUR
                prior = buckets.energy[energy_metric].get(hour)
                if prior is None or at >= prior[1]:
                    buckets.energy[energy_metric][hour] = (value / 1000.0, at)
                buckets.used += 1
                continue

            source = index.get((measurement, key))
            if source is None:
                continue
            if not (first <= at <= last):
                buckets.dropped_out_of_range += 1
                continue

            reading = source.convert(value)
            hour_slot = at // HOUR - origin_hour
            if 0 <= hour_slot < hours:
                buckets.hourly[source.metric].add(hour_slot, reading, at)
            if at >= minute_from:
                minute_slot = at // MINUTE - origin_minute
                if 0 <= minute_slot < minutes:
                    buckets.minutely[source.metric].add(minute_slot, reading, at)
            if at >= raw_from:
                buckets.raw.setdefault(at, {})[source.metric] = reading
            buckets.used += 1

    logger.info(
        "read %s points in %.0fs; %s mapped, %s outside the range, %s full-cadence instants",
        f"{buckets.seen:,}",
        time.time() - started,
        f"{buckets.used:,}",
        f"{buckets.dropped_out_of_range:,}",
        f"{len(buckets.raw):,}",
    )
    return buckets


def synthesise_counters(
    buckets: Buckets, anchor: dict[str, float], anchor_hour: int
) -> dict[str, dict[int, float]]:
    """Turn SolarAssistant's hourly energy into monotonic lifetime counters.

    This is the only derived quantity in the import, and it exists because
    everything downstream reads energy as a difference of two counter readings.
    Each hour's kWh is real — SolarAssistant measured it — but the running total
    is not, so it is anchored: the counters are accumulated *backwards* from the
    values the inverter itself reports at the moment of cutover, so the last
    reconstructed hour lands exactly on the first real one. Every difference
    taken across the seam is then correct, and no chart shows a step.

    The absolute values before the seam are therefore a reconstruction. The
    differences, which are all this project ever computes, are SolarAssistant's
    own measurements.
    """
    out: dict[str, dict[int, float]] = {}
    for metric, per_hour in buckets.energy.items():
        if metric not in anchor:
            logger.warning("no anchor for %s; its counter cannot be placed", metric)
            continue
        hours = sorted(h for h in per_hour if h <= anchor_hour)
        running = anchor[metric]
        series: dict[int, float] = {}
        # Backwards from the anchor, so the join is exact rather than close.
        #
        # The subtraction comes *before* the assignment, so what lands at hour H
        # is the counter as it stood at the start of H rather than at its end.
        # The row is stamped at the start of the hour, and a counter written one
        # hour late shifts every reading into its neighbour: a day then totals
        # hours one to twenty-three and silently drops the first. It looked
        # right, too, because the hour it dropped is midnight, when solar is
        # zero — solar matched to a fiftieth of a percent while grid import,
        # which runs all night, came out fifteen percent short.
        for hour in reversed(hours):
            running -= per_hour[hour][0]
            series[hour] = running
        if running < 0:
            logger.warning(
                "%s would start at %.1f kWh, below zero — the anchor is lower than the "
                "history it has to cover, so the counter is shifted to start at zero",
                metric,
                running,
            )
            for hour in series:
                series[hour] -= running
        out[metric] = series
        if series:
            lowest = min(series.values())
            logger.info(
                "%s: %s hours, %.1f -> %.1f kWh", metric, len(series), lowest, anchor[metric]
            )
    return out


def tier_policy() -> dict[str, Aggregation]:
    """How every inverter metric collapses into a tier bucket.

    Deferred to ``store.rollup.collapse_policy`` rather than read off the
    registry here. The collector's rollup and this importer both write
    ``inverter_hourly`` and ``inverter_minute``, and while each decided for
    itself the same column carried two meanings depending on which had
    produced the row — the rollup put a counter's closing value on a bucket
    stamped with its opening instant, and the import put its opening one.
    """
    return {spec.name: collapse_policy(spec) for spec in INVERTER_METRICS}


def _encode(metric: str, value: float | None) -> int | None:
    """Scale a reading to the integer the column stores, or None if absent."""
    if value is None:
        return None
    spec = lookup(metric)
    if not spec.within_bounds(value):
        return None
    return int(spec.encode(value))


def write_tier(
    conn: sqlite3.Connection,
    table: str,
    device: str,
    origin: int,
    span: int,
    step: int,
    accumulators: dict[str, Accumulator],
    counters: dict[str, dict[int, float]],
    counter_step: int,
) -> int:
    """Write one tier's rows, oldest first, all under one inverter's serial.

    Out-of-bounds readings are stored as NULL rather than clamped. A value the
    registry calls impossible is not a reading, and clamping it into range would
    launder a decode fault into a plausible number.

    ``device`` is in the conflict target because it is in the key: an import
    run twice replaces its own rows, and never another inverter's readings for
    the same instant.
    """
    policy = tier_policy()
    metrics = sorted(accumulators)
    energy_metrics = sorted(counters)
    columns = ["timestamp", "device", *metrics, *energy_metrics, "sample_count"]
    sql = (
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))}) "
        f"ON CONFLICT(timestamp,device) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in columns[2:])
    )

    rows: list[tuple[object, ...]] = []
    written = 0
    for slot in range(span):
        at = (origin + slot) * step
        values: list[object] = [at, device]
        samples = 0
        for metric in metrics:
            acc = accumulators[metric]
            raw = acc.value(slot, policy[metric])
            samples = max(samples, acc.count[slot])
            values.append(_encode(metric, raw))
        hour = at // counter_step
        for metric in energy_metrics:
            values.append(_encode(metric, counters[metric].get(hour)))
        # From index 2: timestamp and device are always set, so a slice that
        # started at 1 would find the device string and never skip a row.
        if samples == 0 and all(v is None for v in values[2:]):
            continue
        values.append(samples)
        rows.append(tuple(values))
        written += 1
        if len(rows) >= 5000:
            conn.executemany(sql, rows)
            rows.clear()
    if rows:
        conn.executemany(sql, rows)
    conn.commit()
    return written


def write_raw(
    conn: sqlite3.Connection,
    table: str,
    device: str,
    readings: dict[int, dict[str, float]],
    counters: dict[str, dict[int, float]],
) -> int:
    """Write the full-cadence rows, one per instant a reading was taken.

    The counters are hourly, so every raw row inside an hour carries that hour's
    value. That is not a rounding compromise: energy is only ever read as the
    difference between two rows, and giving the whole hour one value means a
    difference taken inside it is zero — which is true, because nothing is known
    about how the hour's energy was distributed within it.
    """
    if not readings:
        return 0
    metrics = sorted({m for row in readings.values() for m in row})
    energy_metrics = sorted(counters)
    columns = ["timestamp", "device", *metrics, *energy_metrics]
    sql = (
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' * len(columns))}) "
        f"ON CONFLICT(timestamp,device) DO UPDATE SET "
        + ",".join(f"{c}=excluded.{c}" for c in columns[2:])
    )
    rows: list[tuple[object, ...]] = []
    written = 0
    for at in sorted(readings):
        row = readings[at]
        values: list[object] = [at, device]
        values.extend(_encode(m, row.get(m)) for m in metrics)
        hour = at // HOUR
        values.extend(_encode(m, counters[m].get(hour)) for m in energy_metrics)
        rows.append(tuple(values))
        written += 1
        if len(rows) >= 5000:
            conn.executemany(sql, rows)
            rows.clear()
    if rows:
        conn.executemany(sql, rows)
    conn.commit()
    return written


def read_anchor(db: Path, device: str) -> tuple[dict[str, float], int]:
    """The earliest real counter reading for one inverter, and the hour holding it.

    The import has to end where live collection begins, so the anchor is the
    *first* thing the collector recorded, not the newest. Without a live reading
    there is nothing to anchor to and the caller must be told rather than handed
    a counter starting at zero.

    Narrowed to the device being imported for. The counters are lifetime totals
    belonging to one inverter, and anchoring a stack's history to whichever unit
    happened to record first would shift every kilowatt-hour of it.
    """
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    wanted = sorted(HOURLY_ENERGY.values())
    where = " OR ".join(f"{c} IS NOT NULL" for c in wanted)
    row = conn.execute(
        f"SELECT timestamp,{','.join(wanted)} FROM inverter_raw WHERE device = ? AND ({where}) "
        f"ORDER BY timestamp ASC LIMIT 1",
        (device,),
    ).fetchone()
    if row is None:
        row = conn.execute(
            f"SELECT timestamp,{','.join(wanted)} FROM inverter_hourly "
            f"WHERE device = ? AND ({where}) ORDER BY timestamp ASC LIMIT 1",
            (device,),
        ).fetchone()
    conn.close()
    if row is None:
        raise SystemExit(
            "no live counter reading in the database to anchor the history to.\n"
            "Start the collector first, let it record one sample, then run this."
        )
    anchor = {c: lookup(c).decode(row[c]) for c in wanted if row[c] is not None}
    return anchor, int(row["timestamp"]) // HOUR


def main() -> int:
    """Read the exports, fill the tiers, and say what was written."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("exports", nargs="+", type=Path)
    ap.add_argument("--db", required=True, type=Path)
    ap.add_argument(
        "--device",
        required=True,
        help="serial of the inverter this history belongs to; must match the "
        "inverter_serial the collector runs with, or the imported years and the "
        "live readings become two separate machines",
    )
    ap.add_argument(
        "--raw-days",
        type=int,
        default=30,
        help="days of full-cadence history to import, matching the raw tier's retention",
    )
    ap.add_argument(
        "--minute-days",
        type=int,
        default=365,
        help="days of per-minute history to import, matching the minute tier's retention",
    )
    ap.add_argument(
        "--max-years",
        type=float,
        default=5.0,
        help="how far back to accept points at all, as a guard against a stray timestamp",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    anchor, anchor_hour = read_anchor(args.db, args.device)
    logger.info(
        "anchored to the first live reading at %s", datetime.fromtimestamp(anchor_hour * HOUR, UTC)
    )
    for metric, value in sorted(anchor.items()):
        logger.info("  %-38s %12.1f kWh", metric, value)

    last = anchor_hour * HOUR
    first = last - int(args.max_years * 365.25 * 86400)
    minute_from = last - args.minute_days * 86400
    raw_from = last - args.raw_days * 86400

    buckets = scan(args.exports, first, last, minute_from, raw_from)
    counters = synthesise_counters(buckets, anchor, anchor_hour)

    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    tiers = {t.name: t.table for t in INVERTER_TIERS}
    hourly_written = write_tier(
        conn,
        tiers["hourly"],
        args.device,
        buckets.origin_hour,
        buckets.hours,
        HOUR,
        buckets.hourly,
        counters,
        HOUR,
    )
    logger.info("hourly tier: %s rows", f"{hourly_written:,}")

    minute_written = write_tier(
        conn,
        tiers["minute"],
        args.device,
        buckets.origin_minute,
        buckets.minutes,
        MINUTE,
        buckets.minutely,
        counters,
        HOUR,
    )
    logger.info("minute tier: %s rows", f"{minute_written:,}")

    raw_written = write_raw(conn, tiers["full"], args.device, buckets.raw, counters)
    logger.info("raw tier: %s rows", f"{raw_written:,}")

    conn.close()
    logger.info("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
