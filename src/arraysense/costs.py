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

Two questions get asked of this. ``period_energy`` splits one stretch — a
month, a bill — and ``bucket_energy`` splits every calendar day or month in a
range at once, which is what puts a cost beside each row of the History page.
The second is not the first in a loop: it reads the counters once for the whole
range, because thirteen months answered a month at a time is thirteen passes
over the same rows.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import pairwise
from zoneinfo import ZoneInfo

from arraysense.energy import (
    ENERGY_FIELDS,
    MAX_EDGE_GAP,
    EnergyBucket,
    bucket_totals,
    with_zone,
)
from arraysense.tariff import CostResult, PeriodEnergy, RateBand, Tariff, compute_cost

logger = logging.getLogger(__name__)

# How finely a day whose UTC offset moves is scanned for a change of band.
# Bands are expressed in whole minutes, so a minute is exact. Only the two days
# a year a zone changes its clocks are walked this way; see ``_candidates``.
_SCAN_STEP = timedelta(minutes=1)

# How long one costed period may be. Not a limit on the scan any more — the
# scan is now proportional to the number of days rather than to the number of
# minutes — but on the read behind it: ``/api/costs`` answers from the minute
# tier, and a period of years is a request to page a year of rows in to produce
# one bill-shaped figure. Longer spans are asked for a bucket at a time
# instead, which is what ``bucket_energy`` is for.
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


def _clock_marks(tariff: Tariff) -> tuple[time, ...]:
    """Every wall-clock time at which some band's schedule can turn over.

    Which band applies at a moment is decided by three things and no others:
    the month, the weekday, and the time on the clock. The first two change at
    local midnight, which is why midnight is always in the set; the third
    changes only at the ends of the ranges a band was written with. Between two
    consecutive marks nothing that ``applies_at`` looks at has moved, so no
    boundary can hide in there.
    """
    marks = {time(0, 0)}
    for band in tariff.bands:
        for span in band.hours:
            marks.add(span.start)
            marks.add(span.end)
    return tuple(sorted(marks))


def _steady_offset(day: date, zone: ZoneInfo) -> bool:
    """Whether the zone holds one offset from UTC all through this local day.

    On a day that it does, a wall-clock time names exactly one instant and the
    two run in step, so the marks can be turned into instants directly. On the
    two days a year it does not, one wall-clock hour happens twice or not at
    all, and that assumption is the one that misprices them.
    """
    first = datetime.combine(day, time(0, 0), tzinfo=zone)
    noon = datetime.combine(day, time(12, 0), tzinfo=zone)
    following = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone)
    return first.utcoffset() == noon.utcoffset() == following.utcoffset()


def _swept(day: date, zone: ZoneInfo, after: datetime, before: datetime) -> Iterator[datetime]:
    """Walk one clock-change day a minute at a time, in absolute time.

    Stepped in UTC and read as a wall clock, never stepped in wall clock.
    Adding a minute to an aware local datetime does naive arithmetic: on the
    day the clocks go back it walks 01:00 to 01:59 once when those minutes
    happen twice, so the second pass is priced by whatever band the first pass
    ended in. Stepping through UTC visits every real minute exactly once.

    ``after`` and ``before`` are the period's own bounds, already in UTC.
    """
    instant = datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(UTC)
    stop = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=zone).astimezone(UTC)
    while instant < stop:
        if after < instant < before:
            yield instant.astimezone(zone)
        instant += _SCAN_STEP


def _candidates(
    tariff: Tariff, start: datetime, end: datetime, zone: ZoneInfo
) -> Iterator[datetime]:
    """Every instant strictly inside the period at which the band could change.

    A day at a time, so the pattern is never assumed to repeat: the season
    turns at the top of a month and a weekend schedule turns on a Saturday, and
    both are asked afresh for each day rather than inferred from the last one.
    What is assumed is only that a band cannot change in the middle of a
    stretch during which the month, the weekday and the clock have all stood
    still — which is what ``applies_at`` reads and all it reads.

    A day whose UTC offset moves gets the minute walk instead, because on that
    one day the clock does not run in step with real time. Two days a year cost
    a thousand comparisons each; the other three hundred and sixty-three cost
    one per mark.

    Every bound here is compared in UTC. Two datetimes that share a ``tzinfo``
    compare as though they were naive — Python documents this — and on a zone
    that turns its clocks at midnight the period's own start then sorts *before*
    the instant it actually is, which emitted a boundary at the start itself and
    with it an interval of zero length for the caller to divide by.
    """
    marks = _clock_marks(tariff)
    after, before = start.astimezone(UTC), end.astimezone(UTC)
    day, final = start.date(), end.date()
    while day <= final:
        if _steady_offset(day, zone):
            for mark in marks:
                moment = datetime.combine(day, mark, tzinfo=zone)
                if after < moment.astimezone(UTC) < before:
                    yield moment
        else:
            yield from _swept(day, zone, after, before)
        day += timedelta(days=1)


def band_intervals(
    tariff: Tariff, start: datetime, end: datetime, zone: ZoneInfo
) -> list[BandInterval]:
    """Cut [start, end) at every point the band in force changes.

    Goes day by day rather than assuming a daily pattern, because the pattern
    is not fixed: a seasonal tariff changes shape at the turn of a month, and a
    band that runs through midnight changes it again. Within a day it jumps
    between the wall-clock times a band can turn on instead of stepping through
    every minute — see ``_candidates`` for why that is exact, and for the one
    day a year on which it is not and the minute walk is used instead.

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
    out: list[BandInterval] = []
    edge = local_start
    current = _band_name(tariff, local_start)
    for moment in _candidates(tariff, local_start, local_end, zone):
        name = _band_name(tariff, moment)
        if name != current:
            out.append(BandInterval(current, edge, moment))
            edge, current = moment, name
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


# The energy-bucket fields that get split per band. Grid export is
# deliberately not among them: no tariff here pays differently for export by
# hour, so it is totalled whole and a band split would be invented precision.
_SPLIT_FIELDS = ("grid_imported_kwh", "load_kwh", "battery_discharged_kwh")


@dataclass(frozen=True)
class _BandSplit:
    """Grid import, house load and bank discharge per band, plus whole export."""

    imported: dict[str, float | None]
    load: dict[str, float | None]
    discharged: dict[str, float | None]
    exported: float | None


def _band_split(
    intervals: Sequence[BandInterval], buckets: Sequence[EnergyBucket | None]
) -> _BandSplit:
    """Add each interval's energy into the band that was in force across it.

    Shared by the single-period and the per-bucket paths so there is one answer
    to what "the peak band used" means. Two of them would drift, and this is
    the step where kilowatt-hours become money.
    """
    totals: dict[str, dict[str, float | None]] = {field: {} for field in _SPLIT_FIELDS}
    exported: float | None = None
    for interval, bucket in zip(intervals, buckets, strict=True):
        if interval.band is None:
            logger.debug(
                "no band covers %s to %s; its energy is unpriced", interval.start, interval.end
            )
            continue
        for key, into in totals.items():
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
    return _BandSplit(
        imported=totals["grid_imported_kwh"],
        load=totals["load_kwh"],
        discharged=totals["battery_discharged_kwh"],
        exported=exported,
    )


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
    split = _band_split(intervals, _aligned(intervals, bucket_totals(rows, edges, max_gap)))
    moments = _reading_moments(rows, local_start, local_end)

    return PeriodEnergy(
        start=local_start,
        end=local_end,
        grid_import_kwh=split.imported,
        load_kwh=split.load or None,
        grid_export_kwh=split.exported,
        battery_discharge_kwh=split.discharged or None,
        measured_minutes=_observed_minutes(moments, max_gap),
        counted_minutes=_counted_minutes(moments),
        elapsed_minutes=_real_minutes(intervals[0].start, intervals[-1].end),
    )


def bucket_energy(
    tariff: Tariff,
    rows: Sequence[Mapping[str, object]],
    edges: Sequence[datetime],
    zone: ZoneInfo,
    max_gap: timedelta = MAX_EDGE_GAP,
    until: datetime | None = None,
) -> list[PeriodEnergy]:
    """Split every calendar bucket's energy across the bands, in one pass.

    The History page wants a price against each of thirty days and each of
    thirteen months, and the obvious way to get one — ask the single-period
    path once per bucket — reads the whole range out of the store again for
    every bucket it answers. Thirteen months of that is thirteen reads of the
    same rows.

    So the bucket boundaries and the band boundaries inside them are gathered
    into one list of edges and the counters are differenced across all of them
    at once, which is a single walk of the rows however many buckets are asked
    for. Each bucket then keeps the stretches that belong to it.

    Returns one entry per bucket, in the order the edges give them, so a caller
    can line the money up with the energy by position. A band a bucket never
    entered is absent from that bucket rather than zero, exactly as it is for a
    single period: the day before the season turns has no peak band, and saying
    it used none would be a claim nobody measured.

    ``until`` cuts the last bucket short, and it is what makes the bucket the
    owner is living through priceable at all. A calendar month runs to the
    first of the next one, so most of the month in progress is hours that have
    not happened — no reading covers them, the peak band comes back unmeasured
    and the whole month prices as unknown. Handed the moment the question was
    asked about, this prices the part of the bucket that has, which is the same
    thing the single-period path does with the same bound and so gives the same
    answer for the month to date.
    """
    if len(edges) < 2:
        return []
    spans = [(edges[index], _capped(edges[index + 1], until)) for index in range(len(edges) - 1)]
    groups = [band_intervals(tariff, first, last, zone) for first, last in spans]
    flat = [interval for group in groups for interval in group]
    if not flat:
        return []

    # One list of edges covering every band change in every bucket, so the
    # counters are differenced once. The groups are contiguous — each bucket's
    # last interval ends where the next bucket's first begins — so this is
    # ascending and every bucket edge is in it.
    sub_edges = [flat[0].start, *(interval.end for interval in flat)]
    aligned = _aligned(flat, bucket_totals(rows, sub_edges, max_gap))

    out: list[PeriodEnergy] = []
    at = 0
    for (first, last), group in zip(spans, groups, strict=True):
        split = _band_split(group, aligned[at : at + len(group)])
        at += len(group)
        # ``last`` is always after ``first``: the edges come from the calendar
        # and ``until`` is the end of the range they were built to cover, so
        # every bucket begins before it.
        out.append(
            PeriodEnergy(
                start=_local(first, zone),
                end=_local(last, zone),
                grid_import_kwh=split.imported,
                load_kwh=split.load or None,
                grid_export_kwh=split.exported,
                battery_discharge_kwh=split.discharged or None,
            )
        )
    return out


def price_period(
    tariff: Tariff | None,
    energy: PeriodEnergy,
    fixed_charge: float | None = None,
) -> CostResult | None:
    """Price a split period against the bands it actually entered.

    ``compute_cost`` has to work out for itself which bands could apply, and
    all it has to go on is the tariff and two instants, so it compares months:
    a winter band is inapplicable in July rather than unmeasured, which is what
    stops a seasonal tariff nulling every summer bill. Months are as fine as
    that guess can get from where it stands.

    Here the period has already been cut at every band change, so what it
    entered is known rather than inferred — and the difference shows up every
    morning. A day asked about at nine has not reached its peak window, so the
    peak band has no interval, no reading, and under the month-level guess it
    is an unmeasured band that nulls the whole day. It is not unmeasured; it
    has not happened. The History page's top row read as a dash for the first
    fifteen hours of every day, and so did the Costs page asked about today.

    A band that *did* occur is still named by the split, carrying None when
    nobody measured it, so a completed day with a hole in its peak window
    prices as unknown exactly as before. That is the distinction worth keeping
    and the only one this changes.
    """
    if tariff is None or not energy.grid_import_kwh:
        return compute_cost(tariff, energy, fixed_charge)
    entered = {name.strip().casefold() for name in energy.grid_import_kwh}
    narrowed = tuple(band for band in tariff.bands if band.key in entered)
    # An empty set would leave compute_cost pricing nothing at all and calling
    # the answer zero, which is the one output this project never produces.
    return compute_cost(
        replace(tariff, bands=narrowed) if narrowed else tariff, energy, fixed_charge
    )


def _capped(edge: datetime, until: datetime | None) -> datetime:
    """The earlier of a bucket's closing edge and the moment asked about.

    Compared as instants. The two arrive with different zones attached — the
    edge from the owner's calendar, the bound from a query string ending in Z —
    and datetimes that share a ``tzinfo`` compare as though they were naive,
    which is the trap this whole module keeps stepping around.
    """
    if until is None or edge.astimezone(UTC) <= until.astimezone(UTC):
        return edge
    return until
