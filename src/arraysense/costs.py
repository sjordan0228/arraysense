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
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise
from zoneinfo import ZoneInfo

from arraysense.energy import (
    ENERGY_FIELDS,
    MAX_EDGE_GAP,
    EnergyBucket,
    bucket_totals,
    with_zone,
)
from arraysense.tariff import PeriodEnergy, RateBand, Tariff

logger = logging.getLogger(__name__)

# How finely the period is scanned for a change of band. Bands are expressed in
# whole minutes, so a minute is exact; the cost of scanning is one comparison
# per step and a month is only forty-odd thousand of them.
_SCAN_STEP = timedelta(minutes=1)

# A band boundary that lands mid-interval cannot be attributed, so the scan is
# capped rather than left to run over a year of history a minute at a time.
MAX_SCAN_DAYS = 70


def _local(moment: datetime, zone: ZoneInfo) -> datetime:
    """Read a moment as a wall clock in the owner's zone.

    Two steps, and both are load-bearing. A naive bound means the zone the
    request asked about, so it is attached. An aware one — which is what a
    query string ending in Z parses to — has to be *converted*, not merely
    labelled: attaching a zone to an aware datetime silently does nothing, and
    the bands were then matched against the UTC clock. On the reference
    installation that put the 15:00-20:00 peak window at 10:00-15:00 local and
    mispriced every hour of every day.
    """
    return with_zone(moment, zone).astimezone(zone)


def _real_minutes(start: datetime, end: datetime) -> float:
    """Minutes that actually passed between two instants, across a clock change.

    Converted to UTC first, and that conversion is the whole function. Two
    datetimes sharing a ``tzinfo`` subtract as though they were naive — Python
    documents this — so a 23-hour spring day measured 1440 minutes and a
    25-hour autumn day measured 1440 as well. The page divides one of these by
    the other and shows it as coverage, where the error reads as an hour of
    collection lost, or an hour of nothing presented as fully observed.
    """
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds() / 60


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

    local_start = _local(start, zone)
    local_end = _local(end, zone)
    # Stepped in absolute time and read as a wall clock, never stepped in wall
    # clock. Adding a minute to an aware local datetime does naive arithmetic:
    # on the day the clocks go back it walks 01:00 to 01:59 once when those
    # minutes happen twice, so the second pass is priced by whatever band the
    # first pass ended in. Stepping through UTC visits every real minute
    # exactly once and skips none.
    instant = local_start.astimezone(UTC)
    finish = local_end.astimezone(UTC)
    out: list[BandInterval] = []
    edge = local_start
    current = _band_name(tariff, local_start)
    instant += _SCAN_STEP
    while instant < finish:
        moment = instant.astimezone(zone)
        name = _band_name(tariff, moment)
        if name != current:
            out.append(BandInterval(current, edge, moment))
            edge, current = moment, name
        instant += _SCAN_STEP
    out.append(BandInterval(current, edge, local_end))
    return out


def unpriced_minutes(tariff: Tariff, start: datetime, end: datetime, zone: ZoneInfo) -> float:
    """How many minutes of the period no band prices at all.

    A tariff with a hole in it prices less energy than the period used, and the
    total then looks like a small bill rather than a wrong one. Counted in real
    minutes, so the answer does not gain or lose an hour at a clock change.
    """
    return sum(
        _real_minutes(i.start, i.end)
        for i in band_intervals(tariff, start, end, zone)
        if i.band is None
    )


def _band_name(tariff: Tariff, moment: datetime) -> str | None:
    band: RateBand | None = tariff.band_at(moment)
    return band.name if band is not None else None


def _aligned(
    intervals: Sequence[BandInterval], buckets: Sequence[EnergyBucket]
) -> list[EnergyBucket | None]:
    """Line each interval up with its own bucket, or with None if it has none.

    ``bucket_totals`` leaves out a bucket it had nothing to report for, so the
    result is a subsequence of the intervals rather than a parallel list.
    Pairing the two by position therefore slides every bucket after the gap one
    band to the left — reproduced as seven off-peak kilowatt-hours billed at
    the peak rate, on a total that reported itself confidently while a third of
    the period's energy had vanished from it.

    Both sequences are in time order and the buckets are a subsequence, so one
    pointer walk aligns them exactly. Edges are compared as absolute instants:
    two local datetimes an hour apart inside the repeated autumn hour compare
    equal when they share a ``tzinfo``, which would let the second occurrence
    of 01:00 claim the first one's bucket.
    """
    out: list[EnergyBucket | None] = []
    index = 0
    for interval in intervals:
        if index < len(buckets) and _same_edges(buckets[index], interval):
            out.append(buckets[index])
            index += 1
        else:
            out.append(None)
    return out


def _same_edges(bucket: EnergyBucket, interval: BandInterval) -> bool:
    """Whether a bucket spans exactly this interval, judged in absolute time."""
    return bucket.start.astimezone(UTC) == interval.start.astimezone(UTC) and bucket.end.astimezone(
        UTC
    ) == interval.end.astimezone(UTC)


def _reading_moments(
    rows: Sequence[Mapping[str, object]], start: datetime, end: datetime
) -> list[datetime]:
    """When a counter was actually read, in order, within the period.

    A row with no counter on it is a failed poll or a row from a tier that does
    not carry energy, and it is not evidence that anything was measured.
    Counting those made four failed polls a minute apart look like three
    minutes of collection.
    """
    fields = list(ENERGY_FIELDS.values())
    seen: list[datetime] = []
    for row in rows:
        if all(row.get(f) is None for f in fields):
            continue
        moment = _local_or_none(row.get("timestamp"), start.tzinfo)
        if moment is not None and moment <= end:
            seen.append(moment)
    seen.sort()
    inside = [m for m in seen if m >= start]
    # A reading from before the period still bounds it: the caller widens the
    # query precisely so the first interval has something to be measured from,
    # and dropping it outright loses the stretch between the period's start and
    # the first reading inside it.
    before = [m for m in seen if m < start]
    if before and inside:
        inside.insert(0, start)
    return inside


def _observed_minutes(moments: Sequence[datetime], max_gap: timedelta) -> float:
    """How many minutes of the period the collector was actually running for.

    Measured off the readings themselves rather than off the bands, because the
    question has nothing to do with the tariff. Deriving it from the bands
    instead said a whole interval was observed whenever any of it was — and
    under a single all-day band that is the whole day, so half a day of
    readings reported itself as complete coverage and the page drew "100%".

    Two readings more than ``max_gap`` apart bracket an outage rather than a
    quiet stretch, so the time between them is not counted. That makes this the
    wrong denominator for a projection and the right one for a coverage figure;
    see ``_counted_minutes`` for the other.
    """
    if len(moments) < 2:
        return 0.0
    total = 0.0
    for earlier, later in pairwise(moments):
        span = later.astimezone(UTC) - earlier.astimezone(UTC)
        if span <= max_gap:
            total += span.total_seconds() / 60
    return total


def _counted_minutes(moments: Sequence[datetime]) -> float:
    """The span the energy totals account for: first reading to last.

    Gaps included, deliberately. A counter delta covers the outage it spans —
    that is the entire reason this reads counters instead of integrating power
    — so the energy between the first and last reading belongs to all of the
    time between them, not only to the parts anybody watched.
    """
    if len(moments) < 2:
        return 0.0
    return _real_minutes(moments[0], moments[-1])


def _local_or_none(value: object, tz: tzinfo | None) -> datetime | None:
    """A row's timestamp as an aware datetime, or None if it is not one."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=tz)


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
    # Reported in the owner's zone, not the caller's. Downstream, the month a
    # bill belongs to and the season a band is in are both read off these
    # bounds, and both are wall-clock questions.
    local_start, local_end = _local(start, zone), _local(end, zone)
    if not intervals:
        return PeriodEnergy(start=local_start, end=local_end, grid_import_kwh={})

    edges = [intervals[0].start, *(i.end for i in intervals)]
    buckets = _aligned(intervals, bucket_totals(rows, edges, max_gap))

    imported: dict[str, float | None] = {}
    load: dict[str, float | None] = {}
    discharged: dict[str, float | None] = {}
    exported: float | None = None
    for interval, bucket in zip(intervals, buckets, strict=True):
        if interval.band is None:
            logger.debug(
                "no band covers %s to %s; its energy is unpriced", interval.start, interval.end
            )
            continue
        for key, into in (
            ("grid_imported_kwh", imported),
            ("load_kwh", load),
            ("battery_discharged_kwh", discharged),
        ):
            value = None if bucket is None else bucket.totals.get(key)
            if value is None:
                # Unknown, and unknown wins. A band occurs more than once in a
                # day — off-peak runs before the evening peak and again after
                # it — so keeping an earlier stretch as the band's total is a
                # missing reading rendered as zero, in the one place where it
                # becomes money.
                into[interval.band] = None
            elif into.get(interval.band, 0.0) is not None:
                into[interval.band] = (into.get(interval.band) or 0.0) + value
    for bucket in buckets:
        value = None if bucket is None else bucket.totals.get("grid_exported_kwh")
        if value is not None:
            exported = (exported or 0.0) + value

    moments = _reading_moments(rows, local_start, local_end)

    return PeriodEnergy(
        start=local_start,
        end=local_end,
        grid_import_kwh=imported,
        load_kwh=load or None,
        grid_export_kwh=exported,
        battery_discharge_kwh=discharged or None,
        measured_minutes=_observed_minutes(moments, max_gap),
        counted_minutes=_counted_minutes(moments),
        elapsed_minutes=_real_minutes(intervals[0].start, intervals[-1].end),
    )
