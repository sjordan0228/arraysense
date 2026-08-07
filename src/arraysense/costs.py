"""costs.py — split stored energy across rate bands and price it.

The piece that was missing between ``energy`` and ``tariff``. ``tariff`` prices
energy it is handed per band; ``energy`` totals counters between arbitrary
instants. Nothing joined them, so the browser did the join in JavaScript — a
second implementation of the tariff grammar that promptly disagreed with the
first, rejecting the seasonal band format the Python parser accepts and then
pricing a January evening at a summer peak rate.

Doing it here means one implementation of what a band is and when it applies.
The page asks a question and renders an answer.

Bands are wall-clock, so every boundary is computed in the owner's zone. The
edges are where the band *changes*, which is not a fixed grid: a tariff with a
15:00-20:00 peak has four intervals on a summer day and one on a winter day,
and the turn of a season moves the pattern mid-month.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from arraysense.energy import MAX_EDGE_GAP, bucket_totals, with_zone
from arraysense.tariff import PeriodEnergy, RateBand, Tariff

logger = logging.getLogger(__name__)

# How finely the period is scanned for a change of band. Bands are expressed in
# whole minutes, so a minute is exact; the cost of scanning is one comparison
# per step and a month is only forty-odd thousand of them.
_SCAN_STEP = timedelta(minutes=1)

# A band boundary that lands mid-interval cannot be attributed, so the scan is
# capped rather than left to run over a year of history a minute at a time.
MAX_SCAN_DAYS = 70


@dataclass(frozen=True)
class BandInterval:
    """One stretch of time during which a single band was in force."""

    band: str | None
    start: datetime
    end: datetime


def band_intervals(
    tariff: Tariff, start: datetime, end: datetime, zone: ZoneInfo
) -> list[BandInterval]:
    """Cut [start, end) at every point the band in force changes.

    Walks the period rather than assuming a daily pattern, because the pattern
    is not fixed: a seasonal tariff changes shape at the turn of a month, and a
    band that runs through midnight changes it again. Anything clever enough to
    exploit the usual repetition would be wrong on exactly the days that matter.

    A stretch no band covers is returned with ``band`` None rather than dropped,
    so a hole in the schedule shows up as unpriced energy instead of quietly
    vanishing from the total.
    """
    if end <= start:
        return []
    if end - start > timedelta(days=MAX_SCAN_DAYS):
        raise ValueError(f"a costed period may not exceed {MAX_SCAN_DAYS} days")

    local_start = with_zone(start, zone)
    local_end = with_zone(end, zone)
    out: list[BandInterval] = []
    edge = local_start
    current = _band_name(tariff, local_start)
    moment = local_start + _SCAN_STEP
    while moment < local_end:
        name = _band_name(tariff, moment)
        if name != current:
            out.append(BandInterval(current, edge, moment))
            edge, current = moment, name
        moment += _SCAN_STEP
    out.append(BandInterval(current, edge, local_end))
    return out


def _band_name(tariff: Tariff, moment: datetime) -> str | None:
    band: RateBand | None = tariff.band_at(moment)
    return band.name if band is not None else None


def period_energy(
    tariff: Tariff,
    rows: Sequence[Mapping[str, object]],
    start: datetime,
    end: datetime,
    zone: ZoneInfo,
    max_gap: timedelta = MAX_EDGE_GAP,
) -> PeriodEnergy:
    """Total grid import and house load per band, from the lifetime counters.

    Counters, never integrated power, for the same reason everything else here
    uses them: a period is end minus start, which stays right across a gap in
    collection.

    A band the period never entered is absent rather than zero. The difference
    matters to the caller: zero means measured and nothing happened, absent
    means there is nothing to say — and a projection built on a band that has
    not occurred yet is a guess.
    """
    intervals = band_intervals(tariff, start, end, zone)
    if not intervals:
        return PeriodEnergy(start=start, end=end, grid_import_kwh={})

    edges = [intervals[0].start, *(i.end for i in intervals)]
    buckets = bucket_totals(rows, edges, max_gap)

    imported: dict[str, float | None] = {}
    load: dict[str, float | None] = {}
    exported: float | None = None
    for interval, bucket in zip(intervals, buckets, strict=False):
        if interval.band is None:
            logger.debug(
                "no band covers %s to %s; its energy is unpriced", interval.start, interval.end
            )
            continue
        for key, into in (("grid_imported_kwh", imported), ("load_kwh", load)):
            value = bucket.totals.get(key)
            if value is None:
                continue
            into[interval.band] = (into.get(interval.band) or 0.0) + value
    for bucket in buckets:
        value = bucket.totals.get("grid_exported_kwh")
        if value is not None:
            exported = (exported or 0.0) + value

    return PeriodEnergy(
        start=start,
        end=end,
        grid_import_kwh=imported,
        load_kwh=load or None,
        grid_export_kwh=exported,
    )
