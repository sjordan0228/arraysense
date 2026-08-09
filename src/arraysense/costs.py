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
    EnergyAttribution,
    EnergyBucket,
    attribute_energy,
    with_zone,
)
from arraysense.tariff import (
    CostResult,
    EnergyShortfall,
    PeriodEnergy,
    Tariff,
    compute_cost,
)

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
    price_per_kwh: float | None = None


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
    current_band = tariff.band_at(local_start)
    for moment in _candidates(tariff, local_start, local_end, zone):
        next_band = tariff.band_at(moment)
        # Bands are compared as objects rather than by name so the price can be
        # carried out with each interval. That is only equivalent to comparing
        # names because two bands cannot share one: ``parse_bands`` refuses a
        # duplicate, saying energy is reported per band by name so the names have
        # to differ. Were that ever relaxed, two same-named bands in different
        # seasons would split here where they used to join — which changes where
        # energy is attributed, and therefore what it costs.
        if next_band != current_band:
            price = current_band.price_per_kwh if current_band else None
            out.append(
                BandInterval(
                    current_band.name if current_band else None,
                    edge,
                    moment,
                    price,
                )
            )
            edge = moment
            current_band = next_band
    price = current_band.price_per_kwh if current_band else None
    out.append(
        BandInterval(
            current_band.name if current_band else None,
            edge,
            local_end,
            price,
        )
    )
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
# See ``_whole_export`` for the totalling, which is not simply a sum.
_SPLIT_FIELDS = ("grid_imported_kwh", "load_kwh", "battery_discharged_kwh")


@dataclass(frozen=True)
class _BandSplit:
    """Grid import, house load and bank discharge per band, plus whole export.

    Every one of them is None where nobody measured it, and None here means the
    same thing it means everywhere else: unknown, not nothing.
    """

    imported: dict[str, float | None]
    load: dict[str, float | None]
    discharged: dict[str, float | None]
    exported: float | None


def _whole_export(buckets: Sequence[EnergyBucket | None]) -> float | None:
    """Total the export that was read, or None when none of it was.

    Export takes no band split — no tariff here prices it by the hour — so
    this is a sum of stretches. It used to null the moment any stretch went
    unread, which on a tariff paying for export nulled the estimated bill
    with it: the attempt-one shape the owner rejected in #23. The rule now is
    the owner's: the measured part stands, the shortfall entry beside it says
    what the meter counted that this sum does not, and only a period with
    nothing read at all is None. Nothing downstream *enforces* that for a
    scalar — the credit prices whatever export figure a period carries — so a
    hand-built ``PeriodEnergy`` with a partial sum here is the builder's own
    claim; every path from real readings attaches the accounting beside it.
    """
    total: float | None = None
    for bucket in buckets:
        value = None if bucket is None else bucket.totals.get("grid_exported_kwh")
        if value is not None:
            total = (total or 0.0) + value
    return total


def _band_split(
    intervals: Sequence[BandInterval], buckets: Sequence[EnergyBucket | None]
) -> _BandSplit:
    """Add each interval's energy into the band that was in force across it.

    Shared by the single-period and the per-bucket paths so there is one answer
    to what "the peak band used" means. Two of them would drift, and this is
    the step where kilowatt-hours become money.

    An interval nobody measured no longer nulls its band — the deliberate
    change of #23. Its energy is not lost from the books: it is in the dropped
    spans and bounds the shortfall is built from, so the band's measured
    stretches can stand and the figure priced from them can say what it is
    missing. The band stays None only when *no* interval of it reported,
    because "occurred and never measured" must stay distinguishable from a
    small number — that None is what keeps a wholly unwatched peak window from
    pricing as a cheap one, and downstream it is priced only under a shortfall
    that accounts for it — up to the same ``max_gap`` edge tolerances every
    figure here accepts: a band lying wholly inside the two hours past a
    counter's last reading is unmeasured and unflagged, exactly as that tail
    itself is.
    """
    totals: dict[str, dict[str, float | None]] = {field: {} for field in _SPLIT_FIELDS}
    for interval, bucket in zip(intervals, buckets, strict=True):
        if interval.band is None:
            logger.debug(
                "no band covers %s to %s; its energy is unpriced", interval.start, interval.end
            )
            continue
        for key, into in totals.items():
            value = None if bucket is None else bucket.totals.get(key)
            if value is None:
                # The band occurred here even if nothing was measured; the
                # key with None is that fact, and three consumers read it.
                into.setdefault(interval.band, None)
            else:
                running = into.get(interval.band)
                into[interval.band] = value if running is None else running + value
    return _BandSplit(
        imported=totals["grid_imported_kwh"],
        load=totals["load_kwh"],
        discharged=totals["battery_discharged_kwh"],
        exported=_whole_export(buckets),
    )


# Which shortfall entry each bucket field feeds. PeriodEnergy speaks the left
# names; the buckets and dropped spans speak ENERGY_FIELDS'.
_SHORTFALL_FIELDS: Mapping[str, str] = {
    "grid_import": "grid_imported_kwh",
    "load": "load_kwh",
    "grid_export": "grid_exported_kwh",
    "battery_discharge": "battery_discharged_kwh",
}


def _shortfall(
    attribution: EnergyAttribution,
    intervals: Sequence[BandInterval],
    buckets: Sequence[EnergyBucket | None],
    split: _BandSplit,
    start: datetime,
    end: datetime,
    max_gap: timedelta,
) -> dict[str, EnergyShortfall]:
    """Account, per counter, for the energy this period's figures do not hold.

    The quantified part is the dropped spans lying wholly inside the period:
    both their readings exist, so their energy is exact, and it is exactly
    what the band totals are short by. Everything else is a flag, because a
    number would be invented — a span straddling the period's edge carries
    foreign energy, a reset lost an unknowable amount, and a period reaching
    more than ``max_gap`` past the counter's first or last reading has
    stretches no reading ever bounded.

    The last check walks the intervals that reported nothing and asks whether
    something already accounts for each — a dropped span bracketing it, or
    the counter's reach ending before it. One shape has neither: a pair
    within ``max_gap`` crossing a short band's edges hands the whole delta to
    the neighbouring interval, so the kilowatt-hours conserve, no span
    exists, and the band that occurred priced nothing. That was a permanent
    dash before #23 and would otherwise become a confidently wrong figure;
    the flag is what makes it a labelled one.

    Everything is compared as instants. The spans carry the rows' own
    timestamps and the period bounds arrive in the owner's zone, and two
    aware datetimes only compare correctly across a fold when they do not
    share a tzinfo — going through UTC sidesteps the question entirely.

    Returns a mapping keyed by counter name, each entry holding the attributed
    and unattributed kilowatt-hours, the unknowable flag, and the set of band
    names whose windows were partly unmeasured (#31).
    """
    lo, hi = start.astimezone(UTC), end.astimezone(UTC)
    values: dict[str, Mapping[str, float | None]] = {
        "grid_import": split.imported,
        "load": split.load,
        "battery_discharge": split.discharged,
    }
    out: dict[str, EnergyShortfall] = {}
    for name, field in _SHORTFALL_FIELDS.items():
        spans = [s for s in attribution.dropped if s.field == field]
        unattributed = 0.0
        unknowable = False
        inside: list[tuple[datetime, datetime]] = []
        # Bands whose windows were partly unmeasured, gathered from all three
        # things that can say so below: an interval that reported nothing, a
        # dropped span overlapping one, and an interval outside the counter's
        # reach. Any one of them is enough to qualify the row.
        implicated_bands: set[str] = set()
        for span in spans:
            span_lo, span_hi = span.start.astimezone(UTC), span.end.astimezone(UTC)
            if span_hi <= lo or span_lo >= hi:
                continue
            if span.kwh == 0.0:
                # Pure coverage. A monotone counter that did not move between
                # two readings proves zero energy everywhere between them, so
                # the window is accounted for wherever it falls — including
                # straddling the period's edge — and can never raise a flag.
                inside.append((span_lo, span_hi))
                continue
            if span.kwh is not None and lo <= span_lo and span_hi <= hi:
                unattributed += span.kwh
            else:
                unknowable = True
            inside.append((span_lo, span_hi))

        bounds = attribution.bounds.get(field)
        if bounds is None:
            unknowable = True
            reach = None
        else:
            first, last = bounds
            reach = (first.astimezone(UTC), last.astimezone(UTC))
            if reach[0] - lo > max_gap or hi - reach[1] > max_gap:
                unknowable = True

        if name == "grid_export":
            attributed = split.exported or 0.0
        else:
            attributed = sum(v for v in values[name].values() if v is not None)

        # Export takes no band split, so an interval-level emptiness means
        # nothing for it: a flat rate prices the total, and the total is
        # already accounted for by the spans and bounds above. Running the
        # check anyway flagged provably exact export credits.
        if not unknowable and name != "grid_export":
            covered = _coalesced(inside)
            for interval, bucket in zip(intervals, buckets, strict=True):
                if _unexplained(interval, bucket, field, covered, reach):
                    if interval.band is not None:
                        implicated_bands.add(interval.band)
                    unknowable = True
        # Two more ways a band's window goes unmeasured, both invisible to the
        # emptiness check above and both skipped for export — export takes no
        # band split, a flat rate prices the total, and naming bands for it
        # would qualify a credit the spans and bounds already proved exact.
        #
        # Neither is guarded on ``unknowable``: a period already flagged for
        # another reason still has bands whose rows need marking, and the flag
        # is period-level in any case.
        if name != "grid_export":
            # A dropped span overlapping an interval. A band with intervals on
            # other days that did report is handed 0.0 rather than None, so it
            # never looks empty — and the whole defect in #31 was a peak window
            # reading 0.0 kWh with no mark on it.
            implicated_bands |= _bands_overlapping(
                intervals,
                [
                    (span.start.astimezone(UTC), span.end.astimezone(UTC))
                    for span in spans
                    # A pure coverage span proves zero energy; it implicates nobody.
                    if span.kwh != 0.0
                ],
            )
            # An interval outside the counter's reach. No span can say so: a span
            # needs a reading on both sides of the hole, and past the last answer
            # there is none. ``_unexplained`` calls such an interval explained for
            # exactly that reason, leaving the bounds check to raise the flag — but
            # that flag says nothing about which rows to mark, so a counter that
            # died mid-month marked the totals and left every band row clean.
            implicated_bands |= _bands_overlapping(intervals, _unreached(reach, lo, hi, max_gap))
        out[name] = EnergyShortfall(
            attributed_kwh=attributed,
            unattributed_kwh=unattributed,
            unknowable=unknowable,
            bands_possibly_short=frozenset(implicated_bands),
        )
    return out


def _coalesced(spans: Sequence[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """Merge touching spans into one stretch of accounted-for time.

    Two outages separated by a single reading are two spans sharing that
    reading's instant, and together they account for everything between their
    outer readings — their energies were counted separately and sum exactly.
    Asking whether one span alone brackets an interval said no, and flagged a
    shortfall that was in fact exact.
    """
    merged: list[tuple[datetime, datetime]] = []
    for lo, hi in sorted(spans):
        if merged and lo <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(hi, merged[-1][1]))
        else:
            merged.append((lo, hi))
    return merged


def _unexplained(
    interval: BandInterval,
    bucket: EnergyBucket | None,
    field: str,
    spans: Sequence[tuple[datetime, datetime]],
    reach: tuple[datetime, datetime] | None,
) -> bool:
    """Whether an interval that reported nothing has no account of its energy.

    Explained means a dropped span brackets the whole interval — the energy
    is in the shortfall already — or the interval lies wholly beyond the
    counter's readings, where the period-edge tolerance governs. What is left
    is energy that conserved into a neighbouring interval at a different
    rate, which no number can state and only a flag can say.
    """
    value = None if bucket is None else bucket.totals.get(field)
    if value is not None:
        return False
    interval_lo = interval.start.astimezone(UTC)
    interval_hi = interval.end.astimezone(UTC)
    if reach is None or interval_hi <= reach[0] or interval_lo >= reach[1]:
        return False
    return not any(span_lo <= interval_lo and interval_hi <= span_hi for span_lo, span_hi in spans)


def _bands_overlapping(
    intervals: Sequence[BandInterval], windows: Sequence[tuple[datetime, datetime]]
) -> set[str]:
    """Names of the bands whose intervals share any time with one of the windows.

    The windows are stretches nobody measured, and this is the whole of what can
    honestly be said about where their energy went: a band whose window overlaps
    one *could* be holding some of it. Exact attribution is not achievable —
    inside a dropped span the energy is known in total and unlocatable — so the
    answer is a set of candidates rather than a number, and every caller here
    unions into the same set for that reason.

    Comparison is in UTC because the intervals arrive in the owner's zone: two
    aware datetimes sharing a tzinfo compare as naive across a fold.
    """
    if not windows:
        return set()
    found: set[str] = set()
    for interval in intervals:
        if interval.band is None or interval.band in found:
            continue
        interval_lo = interval.start.astimezone(UTC)
        interval_hi = interval.end.astimezone(UTC)
        if any(interval_lo < hi and interval_hi > lo for lo, hi in windows):
            found.add(interval.band)
    return found


def _unreached(
    reach: tuple[datetime, datetime] | None,
    lo: datetime,
    hi: datetime,
    max_gap: timedelta,
) -> list[tuple[datetime, datetime]]:
    """The stretches of a period lying outside what the counter actually read.

    A counter that never answered reached nothing, so the whole period is
    unmeasured. One that stopped early leaves everything past its last reading
    unmeasured, and one that started late everything before its first.

    The same ``max_gap`` tolerance the bounds check uses governs here, and it
    has to: a counter answering a few minutes after the period opens is normal
    on an 11-second poll, and treating that as an outage would put a mark on
    every band row of every clean month.
    """
    if reach is None:
        return [(lo, hi)]
    out: list[tuple[datetime, datetime]] = []
    if reach[0] - lo > max_gap:
        out.append((lo, reach[0]))
    if hi - reach[1] > max_gap:
        out.append((reach[1], hi))
    return out


def _log_dropped(attribution: EnergyAttribution) -> None:
    """Say what could not be placed, so a labelled figure can be traced.

    The page shows a total; the log answers "why is this labelled short",
    which otherwise takes a debugger and a copy of the walk. Debug rather
    than info: every open page re-prices its period every few minutes, so an
    old outage would otherwise re-announce itself forever — and the label on
    the page is the durable trace, not the log line.
    """
    for span in attribution.dropped:
        if span.kwh == 0.0:
            # Coverage evidence, not loss; announcing it as unattributable
            # would be the log contradicting the arithmetic.
            continue
        amount = "an unknowable amount" if span.kwh is None else f"{span.kwh:.1f} kWh"
        logger.debug(
            "%s of %s between %s and %s could not be attributed",
            amount,
            span.field,
            span.start,
            span.end,
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
    attribution = attribute_energy(rows, edges, max_gap)
    aligned = _aligned(intervals, attribution.buckets)
    split = _band_split(intervals, aligned)
    moments = _reading_moments(rows, local_start, local_end)
    _log_dropped(attribution)

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
        shortfall=_shortfall(
            attribution, intervals, aligned, split, intervals[0].start, intervals[-1].end, max_gap
        ),
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
    attribution = attribute_energy(rows, sub_edges, max_gap)
    aligned = _aligned(flat, attribution.buckets)
    _log_dropped(attribution)

    out: list[PeriodEnergy] = []
    at = 0
    for (first, last), group in zip(spans, groups, strict=True):
        chunk = aligned[at : at + len(group)]
        split = _band_split(group, chunk)
        at += len(group)
        # ``last`` is always after ``first``: the edges come from the calendar
        # and ``until`` is the end of the range they were built to cover, so
        # every bucket begins before it.
        #
        # An outage crossing a *bucket* boundary reads differently here than
        # the same outage asked of the whole range at once: its span is wholly
        # inside neither bucket, so each is unknowable where the range would
        # quantify it exactly. Both are true at their own grain — which day
        # paid for an overnight gap genuinely is not knowable.
        out.append(
            PeriodEnergy(
                start=_local(first, zone),
                end=_local(last, zone),
                grid_import_kwh=split.imported,
                load_kwh=split.load or None,
                grid_export_kwh=split.exported,
                battery_discharge_kwh=split.discharged or None,
                shortfall=(
                    _shortfall(
                        attribution, group, chunk, split, group[0].start, group[-1].end, max_gap
                    )
                    if group
                    else None
                ),
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
    nobody measured it. What that means downstream moved with #23: under a
    shortfall that accounts for the hole, a completed day with an unwatched
    peak window prices its measured part and is flagged, where it used to
    null; without one it still nulls. Either way the band's None survives —
    it is what keeps "occurred and unmeasured" from becoming a small number.
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
