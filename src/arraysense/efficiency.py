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


def _hourly_rows(
    store: SqliteStore,
    day_start: datetime,
    day_end: datetime,
    mppt_indices: list[int],
    tz: tzinfo,
) -> dict[int, dict[str, object]]:
    """Return the hourly-tier rows indexed by hour offset from ``day_start``.

    An hour with no inverter reading is a collector gap and is excluded from
    both sides; an hour with no irradiance inputs is one the model cannot run.

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
        if 0 <= hour_offset < 24:
            by_hour[hour_offset] = row
    return by_hour


def compute_day(
    store: SqliteStore,
    settings: SettingsStore,
    day_start: datetime,
    day_end: datetime,
    strings: tuple[StringSpec, ...],
    config_version: int,
) -> list[EfficiencyRow]:
    """Score one day against the conditions it had.

    Every hour that has both inverter readings and irradiance data contributes
    to both sides of the comparison. An hour whose inverter was silent — a
    collector gap — is excluded from expected and actual alike, so downtime
    never reads as a loss.

    The day is measured in the installation's own timezone. ``day_start`` and
    ``day_end`` must be aware datetimes in that zone; they are converted to UTC
    only for querying the store.

    Returns one row per string plus the ``""`` total. Curtailed energy is left
    at zero — the curtailment detector fills it in a later pass.
    """
    latitude = settings.get(SETTING_LATITUDE)
    longitude = settings.get(SETTING_LONGITUDE)
    if not isinstance(latitude, float) or not isinstance(longitude, float):
        logger.debug("no location set; cannot compute efficiency for %s", day_start.date())
        return []

    tz = day_start.tzinfo
    if tz is None:
        raise ValueError("day_start must be timezone-aware")

    # Nothing configured is nothing to score, and a row of zeros would be the
    # absent-as-zero mistake in its purest form: expected 0.0 and actual 0.0
    # reads as "the array was meant to make nothing and made nothing", which
    # is a claim about an array nobody has described.
    if not strings:
        logger.debug("no array configured; nothing to score for %s", day_start.date())
        return []

    # What the sun offers this day, hour by hour, for the coverage gate.
    daylight = _daylight_weights(day_start, latitude, longitude)
    if not daylight:
        return []
    total_daylight = sum(daylight.values())

    mppt_indices = sorted({s.mppt for s in strings})
    rows_by_hour = _hourly_rows(store, day_start, day_end, mppt_indices, tz)

    # Per-string accumulators, keyed by string name (empty for total)
    expected: dict[str, float] = {}
    actual: dict[str, float] = {}
    modelled: dict[str, int] = {}

    for s in strings:
        expected[s.name] = 0.0
        actual[s.name] = 0.0
        modelled[s.name] = 0
    expected[""] = 0.0
    actual[""] = 0.0
    modelled[""] = 0
    # Weighted by each hour's energy share; modelled[""] stays an honest count
    # of hours, because that is what the row reports.
    modelled_weight = 0.0
    curtailed: dict[str, float] = dict.fromkeys([s.name for s in strings] + [""], 0.0)

    # Each string is judged against its own ordinary operating point, fitted
    # from this day's readings, and the bank against its own widest charge
    # limit. Both are per-installation facts that no constant could stand in
    # for -- see curtailment.py for what a shared threshold would do to a
    # string that runs at a higher voltage than its neighbours.
    baselines: dict[str, StringBaseline | None] = {}
    for s in strings:
        pairs: list[tuple[float, float]] = []
        for candidate in rows_by_hour.values():
            volts = candidate.get(f"pv{s.mppt}_voltage_v")
            amps = candidate.get(f"pv{s.mppt}_current_a")
            if isinstance(volts, float) and isinstance(amps, float):
                pairs.append((volts, amps))
        baselines[s.name] = baseline_for(s.name, pairs)
    limit_anchor = window_max_limit(
        [
            limit
            if isinstance(limit := candidate.get("bms_charge_current_limit_a"), float)
            else None
            for candidate in rows_by_hour.values()
        ]
    )

    for hour_offset in range(24):
        when = day_start + timedelta(hours=hour_offset)
        elevation, sun_azimuth = solar_position(when.astimezone(UTC), latitude, longitude)
        if elevation <= 0.0:
            continue

        row = rows_by_hour.get(hour_offset)
        if row is None:
            continue

        ghi = row.get("ghi_wm2")
        dni = row.get("dni_wm2")
        dhi = row.get("dhi_wm2")
        wind = row.get("wind_speed_ms")
        air_c = row.get("outside_temperature_c")

        if any(v is None for v in (ghi, dni, dhi, air_c)):
            continue

        assert isinstance(ghi, float)
        assert isinstance(dni, float)
        assert isinstance(dhi, float)
        assert isinstance(air_c, float)
        wind_val: float = wind if isinstance(wind, float) else 0.0

        day_of_year = when.timetuple().tm_yday

        # Each string's share of this hour
        hour_total_expected = 0.0
        hour_total_actual = 0.0

        for s in strings:
            # POA per string uses that string's tilt and azimuth
            string_poa = poa_irradiance(
                ghi, dni, dhi, elevation, sun_azimuth, s.tilt, s.azimuth, day_of_year
            )
            cell_c = cell_temperature(string_poa, air_c, wind_val, s.mounting)
            exp_w = expected_watts(s, string_poa, cell_c, when.astimezone(UTC))

            # Actual: the hourly-tier mean power in watts
            pv_key = f"pv{s.mppt}_power_w"
            act_w = row.get(pv_key)
            if not isinstance(act_w, float):
                continue

            # kWh from a one-hour bucket: watts / 1000
            exp_kwh = exp_w / 1000.0
            act_kwh = act_w / 1000.0
            expected[s.name] += exp_kwh
            actual[s.name] += act_kwh
            hour_total_expected += exp_kwh
            hour_total_actual += act_kwh

            # Was this hour's shortfall the inverter refusing energy, or the
            # array losing it? Both halves must agree, or it stays unexplained.
            soc = row.get("battery_soc_pct")
            limit = row.get("bms_charge_current_limit_a")
            volts = row.get(f"pv{s.mppt}_voltage_v")
            amps = row.get(f"pv{s.mppt}_current_a")
            refused = curtailed_kwh_for_hour(
                exp_kwh,
                act_kwh,
                gate_open=gate_is_open(
                    soc if isinstance(soc, float) else None,
                    limit if isinstance(limit, float) else None,
                    limit_anchor,
                ),
                signature_seen=signature_matches(
                    volts if isinstance(volts, float) else None,
                    amps if isinstance(amps, float) else None,
                    baselines.get(s.name),
                ),
            )
            curtailed[s.name] += refused
            curtailed[""] += refused

        modelled_hour = hour_total_expected > 0.0 or hour_total_actual > 0.0
        if modelled_hour:
            modelled_weight += daylight.get(hour_offset, 0.0)
            modelled[""] += 1
            for s in strings:
                pv_key = f"pv{s.mppt}_power_w"
                if isinstance(row.get(pv_key), float):
                    modelled[s.name] += 1
            expected[""] += hour_total_expected
            actual[""] += hour_total_actual

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
