"""efficiency.py — what the array should have produced, against what it did.

A performance score is a comparison, and a comparison needs both sides stored
once rather than recomputed on every page load. Each day gets one row per string
plus a total whose ``string_name`` is the empty string, written by the
maintenance clock after the hourly tier is rebuilt.

A row carries its config version so a day recomputed after the array changes
replaces the stale one rather than sitting beside it — the same approach the
forecast table takes, and for the same reason: the inputs changed under the
stored result.

Pure functions throughout. ``compute_day`` reads from the store but writes
nothing; the caller decides whether to persist the rows it returns.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from arraysense.curtailment import (
    StringBaseline,
    baseline_for,
    curtailed_kwh_for_hour,
    gate_is_open,
    signature_matches,
    window_max_limit,
)
from arraysense.panels import StringSpec
from arraysense.settings import (
    CONFIG_VERSION_KEY,
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    SettingsStore,
)

# Re-exported: the key is a setting and settings.py owns it, but the
# efficiency module is where a reader looks for it.
__all__ = ["CONFIG_VERSION_KEY", "EfficiencyRow", "compute_day"]
from arraysense.solar import cell_temperature, expected_watts, poa_irradiance, solar_position

if TYPE_CHECKING:
    from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


# A day whose modelled hours cover less than this fraction of its daylight hours
# is marked partial rather than presenting a confident performance ratio from a
# sliver of data.
_MIN_COVERAGE = 0.6


@dataclass(frozen=True)
class EfficiencyRow:
    """One day's expected and actual production for one string or the total.

    The total row has ``string_name`` of ``""`` and sums every string's figures.
    ``partial`` is true when fewer than 60% of daylight hours could be modelled,
    and a caller drawing a performance ratio from a partial day must label it
    rather than presenting the figure as measured.
    """

    day: datetime
    string_name: str
    expected_kwh: float
    actual_kwh: float
    curtailed_kwh: float
    unexplained_kwh: float
    modelled_hours: int
    partial: bool
    pr: float | None
    config_version: int


def _daylight_weights(
    day_start: datetime,
    latitude: float,
    longitude: float,
) -> dict[int, float]:
    """Each daylight hour of this day, weighted by the energy it can carry.

    Coverage is the question "how much of this day did we actually see", and
    counting hours answers a different one. An hour either side of sunrise
    carries a few percent of a day's energy; an hour at noon carries ten times
    that. Counting them equally misjudges completeness in both directions, and
    the dangerous direction is measured: on the reference site in August, an
    outage from noon to 17:00 leaves 64 % of the *hours* and so passes a
    sixty-percent floor, while 56 % of the day's energy was never observed —
    a confident performance ratio for a day that mostly went unwatched. That
    is the Costs completeness mistake again, which cost this project two
    reverted attempts: coverage in time watched is not coverage in energy
    accounted for, and every claim built on it depends on the second.

    The weight is the sine of solar elevation, which is what sets clear-sky
    irradiance on a horizontal surface, so it needs no reading from the hours
    that are missing — only where the sun was, which is pure geometry. It
    slightly *over*-values low sun, because it ignores the longer air mass a
    low beam travels through; that errs toward calling a day partial when it
    was nearly complete, never the reverse.

    Sampled once per hour, the resolution the hourly tier keeps. Keyed by
    offset from ``day_start`` so a DST day is still twenty-four buckets and
    the caller can look an hour up by the same index it loops over.
    """
    weights: dict[int, float] = {}
    for h in range(24):
        when = day_start + timedelta(hours=h)
        # solar_position wants UTC; the timestamp is zone-aware
        elevation, _ = solar_position(when.astimezone(UTC), latitude, longitude)
        if elevation > 0.0:
            weights[h] = math.sin(math.radians(elevation))
    return weights


def _wall_clock_hours(start: datetime, end: datetime) -> int:
    """How many hour slots the range covers as the wall clock reads them.

    Deliberately *not* elapsed time. Every hour in this module is addressed by
    its offset from the range's opening edge on the owner's own clock — that is
    what ``start + timedelta(hours=offset)`` produces and what the row index is
    built from — so the count of slots has to be read the same way or the two
    disagree by an hour twice a year. The zone is dropped explicitly rather
    than subtracted away, because CLAUDE.md records that subtracting two aware
    datetimes sharing a ``tzinfo`` silently ignores the zone: here that is the
    answer wanted, and saying so is what stops the next reader from "fixing" it
    into elapsed time.

    A spring-forward day therefore offers twenty-four slots of which one names
    an hour that did not happen; no row can carry it, so it is skipped. A
    fall-back day offers twenty-four for twenty-five real hours and the
    repeated hour keeps one of its two rows. Both were true before this
    function existed and neither is what the range length decides.
    """
    return round((end.replace(tzinfo=None) - start.replace(tzinfo=None)).total_seconds() / 3600)


def _hourly_rows(
    store: SqliteStore,
    day_start: datetime,
    day_end: datetime,
    mppt_indices: list[int],
    tz: tzinfo,
    span: int,
) -> dict[int, dict[str, object]]:
    """Return the hourly-tier rows indexed by hour offset from ``day_start``.

    An hour with no inverter reading is a collector gap and is excluded from
    both sides; an hour with no irradiance inputs is one the model cannot run.

    ``span`` is how many hour slots the caller means to score, and rows outside
    it are dropped. It is a parameter rather than a constant twenty-four
    because this is asked over a week and a month as well as a day: hard-coding
    the day threw away every hour after the first, which left the Efficiency
    page hunting a month's worst hour inside its first morning.

    Returns a dict of ``hour_offset -> row`` where row is the decoded query
    result keyed by metric name.
    """
    utc_start = day_start.astimezone(UTC)
    utc_end = day_end.astimezone(UTC)

    metrics: list[str] = []
    for idx in mppt_indices:
        metrics.append(f"pv{idx}_power_w")
        # Voltage and current are the curtailment signature: a throttled string
        # is held near open circuit while its current is strangled, and power
        # alone cannot tell that apart from a cloud.
        metrics.append(f"pv{idx}_voltage_v")
        metrics.append(f"pv{idx}_current_a")
    metrics.extend(["ghi_wm2", "dni_wm2", "dhi_wm2", "wind_speed_ms", "outside_temperature_c"])
    # The gate: whether there was anywhere for the power to go.
    metrics.extend(["battery_soc_pct", "bms_charge_current_limit_a"])

    raw = store.query(metrics, utc_start, utc_end, tier="hourly")

    # Index rows by the hour of day in the installation's own zone, which is
    # what the day's boundaries are expressed in. The row's own UTC timestamp
    # is converted back to local time to find its hour bucket; a DST transition
    # day has an odd-length bucket or two, and the offset-based indexing handles
    # that naturally.
    by_hour: dict[int, dict[str, object]] = {}
    for row in raw:
        ts = row["timestamp"]
        assert isinstance(ts, datetime)
        local = ts.astimezone(tz)
        hour_offset = (local - day_start).days * 24 + local.hour
        if 0 <= hour_offset < span:
            by_hour[hour_offset] = row
    return by_hour


@dataclass(frozen=True)
class StringHour:
    """One string's share of one hour."""

    name: str
    expected_kwh: float
    actual_kwh: float
    curtailed_kwh: float


@dataclass(frozen=True)
class HourEfficiency:
    """What one hour offered, what it delivered, and what was merely refused.

    This is the grain everything else is built from. The daily summary sums
    these; the endpoint renders them as a day's detail. Both read the same
    numbers because there is only one place that computes them, which is the
    rule this project learned the hard way when the Costs page kept its own
    tariff parser beside the Python one and the two disagreed within a day.
    Deriving expected production twice would be the same mistake against a
    harder sum.
    """

    hour: datetime
    offset: int
    strings: tuple[StringHour, ...]

    @property
    def expected_kwh(self) -> float:
        return sum(s.expected_kwh for s in self.strings)

    @property
    def actual_kwh(self) -> float:
        return sum(s.actual_kwh for s in self.strings)

    @property
    def curtailed_kwh(self) -> float:
        return sum(s.curtailed_kwh for s in self.strings)

    @property
    def unexplained_kwh(self) -> float:
        """Shortfall this hour with no cause attributed to it."""
        return max(0.0, self.expected_kwh - self.actual_kwh - self.curtailed_kwh)


def compute_hours(
    store: SqliteStore,
    settings: SettingsStore,
    start: datetime,
    end: datetime,
    strings: Sequence[StringSpec],
) -> list[HourEfficiency]:
    """Score every hour in a range against the sun it was offered.

    The single implementation of the model chain -- sun position, transposition,
    cell temperature, expected watts, then the curtailment rule -- and the only
    place any of it happens. An hour with no inverter reading or no irradiance
    is absent from the result rather than present as a zero, because a zero here
    would say the array produced nothing when the truth is that nobody looked.

    Baselines are fitted across the whole range rather than per day: a string's
    ordinary operating voltage is a property of how it is wired, not of the
    weather on one afternoon, and a single overcast day has too few good hours
    to fit from.
    """
    latitude = settings.get(SETTING_LATITUDE)
    longitude = settings.get(SETTING_LONGITUDE)
    if not isinstance(latitude, float) or not isinstance(longitude, float):
        return []
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    if not strings:
        return []

    tz = start.tzinfo
    mppt_indices = sorted({s.mppt for s in strings})
    span = _wall_clock_hours(start, end)
    rows_by_hour = _hourly_rows(store, start, end, mppt_indices, tz, span)
    if not rows_by_hour:
        return []

    # Each string against its own operating point, the bank against its own
    # widest charge limit. See curtailment.py for why neither may be a constant.
    baselines: dict[str, StringBaseline | None] = {}
    for s in strings:
        pairs: list[tuple[float, float]] = []
        for candidate in rows_by_hour.values():
            volts = candidate.get(f"pv{s.mppt}_voltage_v")
            amps = candidate.get(f"pv{s.mppt}_current_a")
            if isinstance(volts, float) and isinstance(amps, float):
                pairs.append((volts, amps))
        baselines[s.name] = baseline_for(s.name, pairs)
    seen_limits: list[float | None] = []
    for candidate in rows_by_hour.values():
        limit_reading = candidate.get("bms_charge_current_limit_a")
        seen_limits.append(limit_reading if isinstance(limit_reading, float) else None)
    limit_anchor = window_max_limit(seen_limits)

    hours: list[HourEfficiency] = []
    for offset in range(span):
        when = start + timedelta(hours=offset)
        elevation, sun_azimuth = solar_position(when.astimezone(UTC), latitude, longitude)
        if elevation <= 0.0:
            continue
        row = rows_by_hour.get(offset)
        if row is None:
            continue

        ghi = row.get("ghi_wm2")
        dni = row.get("dni_wm2")
        dhi = row.get("dhi_wm2")
        air_c = row.get("outside_temperature_c")
        if not (
            isinstance(ghi, float)
            and isinstance(dni, float)
            and isinstance(dhi, float)
            and isinstance(air_c, float)
        ):
            continue
        # Wind is not optional and must not default to still air. Faiman puts
        # the cells 13 C hotter at zero wind than at two metres per second,
        # which is five percent of this array's expected output -- understating
        # what the array should have made, and so flattering the ratio it is
        # judged by. Absent is absent: the hour goes unmodelled and says so
        # through the coverage figure, rather than being modelled from a wind
        # speed nobody measured.
        wind = row.get("wind_speed_ms")
        if not isinstance(wind, float):
            continue
        wind_val: float = wind
        day_of_year = when.timetuple().tm_yday

        soc = row.get("battery_soc_pct")
        limit = row.get("bms_charge_current_limit_a")
        gate_open = gate_is_open(
            soc if isinstance(soc, float) else None,
            limit if isinstance(limit, float) else None,
            limit_anchor,
        )

        share: list[StringHour] = []
        for s in strings:
            act_w = row.get(f"pv{s.mppt}_power_w")
            if not isinstance(act_w, float):
                continue
            string_poa = poa_irradiance(
                ghi, dni, dhi, elevation, sun_azimuth, s.tilt, s.azimuth, day_of_year
            )
            cell_c = cell_temperature(string_poa, air_c, wind_val, s.mounting)
            exp_kwh = expected_watts(s, string_poa, cell_c, when.astimezone(UTC)) / 1000.0
            act_kwh = act_w / 1000.0
            volts = row.get(f"pv{s.mppt}_voltage_v")
            refused = curtailed_kwh_for_hour(
                exp_kwh,
                act_kwh,
                gate_open=gate_open,
                signature_seen=signature_matches(
                    volts if isinstance(volts, float) else None, baselines.get(s.name)
                ),
            )
            share.append(
                StringHour(
                    name=s.name,
                    expected_kwh=exp_kwh,
                    actual_kwh=act_kwh,
                    curtailed_kwh=refused,
                )
            )

        if not share:
            continue
        hours.append(HourEfficiency(hour=when, offset=offset, strings=tuple(share)))
    return hours


def compute_day(
    store: SqliteStore,
    settings: SettingsStore,
    day_start: datetime,
    day_end: datetime,
    strings: Sequence[StringSpec],
    config_version: int,
) -> list[EfficiencyRow]:
    """Summarise one day: a row per string, plus a total row named "".

    Aggregation only. Every figure it reports comes from ``compute_hours``,
    which is the one place the model runs -- the day's totals and the day's
    hour-by-hour detail are therefore the same arithmetic seen at two
    resolutions rather than two implementations that agree until they do not.

    Returns nothing at all when there is nothing to say: no array described, no
    location, a day with no sun, or a day in which not one hour could be
    modelled. A row of zeros in any of those cases would be a claim that the
    array was expected to make nothing and made nothing.
    """
    latitude = settings.get(SETTING_LATITUDE)
    longitude = settings.get(SETTING_LONGITUDE)
    if not isinstance(latitude, float) or not isinstance(longitude, float):
        logger.debug("no location set; cannot compute efficiency for %s", day_start.date())
        return []
    if day_start.tzinfo is None:
        raise ValueError("day_start must be timezone-aware")
    if not strings:
        logger.debug("no array configured; nothing to score for %s", day_start.date())
        return []

    # What the sun offered this day, hour by hour, for the coverage gate.
    daylight = _daylight_weights(day_start, latitude, longitude)
    if not daylight:
        return []
    total_daylight = sum(daylight.values())

    hours = compute_hours(store, settings, day_start, day_end, strings)
    if not hours:
        return []

    expected: dict[str, float] = dict.fromkeys([s.name for s in strings] + [""], 0.0)
    actual: dict[str, float] = dict.fromkeys([s.name for s in strings] + [""], 0.0)
    curtailed: dict[str, float] = dict.fromkeys([s.name for s in strings] + [""], 0.0)
    modelled: dict[str, int] = dict.fromkeys([s.name for s in strings] + [""], 0)
    # Weighted by each hour's share of the day's energy; modelled[""] stays an
    # honest count of hours, because that is what the row reports.
    modelled_weight = 0.0

    for hour in hours:
        modelled_weight += daylight.get(hour.offset, 0.0)
        modelled[""] += 1
        expected[""] += hour.expected_kwh
        actual[""] += hour.actual_kwh
        curtailed[""] += hour.curtailed_kwh
        for share in hour.strings:
            expected[share.name] += share.expected_kwh
            actual[share.name] += share.actual_kwh
            curtailed[share.name] += share.curtailed_kwh
            modelled[share.name] += 1

    coverage = modelled_weight / total_daylight if total_daylight > 0.0 else 0.0
    partial = coverage < _MIN_COVERAGE

    rows: list[EfficiencyRow] = []
    for s in strings:
        exp = expected[s.name]
        act = actual[s.name]
        refused = curtailed[s.name]
        # Curtailed energy leaves the comparison on both sides: it was never
        # lost, so it must not count against the ratio, and the shortfall that
        # remains claims no cause of its own.
        unexplained = max(0.0, exp - act - refused)
        pr = act / (exp - refused) if (exp - refused) > 0.0 else None
        rows.append(
            EfficiencyRow(
                day=day_start,
                string_name=s.name,
                expected_kwh=exp,
                actual_kwh=act,
                curtailed_kwh=curtailed[s.name],
                unexplained_kwh=unexplained,
                modelled_hours=modelled.get(s.name, 0),
                partial=partial,
                pr=pr,
                config_version=config_version,
            )
        )

    # A day whose hours could none of them be modelled — no irradiance stored,
    # the collector down all day — has no expectation to compare against, and
    # saying "expected nothing, produced nothing, and it was partial" would
    # dress that silence up as a measurement.
    if modelled[""] == 0:
        return []

    # Total row
    total_expected = expected[""]
    total_actual = actual[""]
    total_curtailed = curtailed[""]
    unexplained_total = max(0.0, total_expected - total_actual - total_curtailed)
    total_pr = (
        total_actual / (total_expected - total_curtailed)
        if (total_expected - total_curtailed) > 0.0
        else None
    )
    rows.append(
        EfficiencyRow(
            day=day_start,
            string_name="",
            expected_kwh=total_expected,
            actual_kwh=total_actual,
            curtailed_kwh=total_curtailed,
            unexplained_kwh=unexplained_total,
            modelled_hours=modelled[""],
            partial=partial,
            pr=total_pr,
            config_version=config_version,
        )
    )

    return rows
