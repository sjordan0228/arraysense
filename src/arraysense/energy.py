"""energy.py — how much energy a calendar day or month held, from the inverter's counters.

The store keeps power in watts, and turning that into kWh means integrating it.
Integration agrees with the inverter on a clean day and quietly undercounts on
any day with a hole in collection: a poll missed at peak is energy nobody ever
counts, and the total that comes out still looks entirely plausible. The
inverter counts energy itself, and its lifetime counters are monotonic, so the
energy over any period is the value at the end minus the value at the start —
which stays right straight through a six-hour outage, because the counter kept
counting while we were not watching.

Two things make that subtraction less trivial than it sounds.

A counter can go back to zero. Firmware updates and BMS replacements do it, and
a naive end-minus-start across a reset produces a large negative that renders
as a spike. Totals are therefore accumulated from consecutive pairs, so a reset
costs the interval it happened in rather than the whole bucket.

A day is the owner's day. Storage is UTC epoch seconds and the owner lives in a
timezone with daylight saving, which gives them one 23-hour day and one 25-hour
day a year. Bucketing by dividing epochs by 86400 gets both of those wrong, and
the error looks exactly like a data bug. The boundaries here come from the
calendar in a named zone, so a 23-hour day holds 23 hours of energy and says so.

What is absent stays absent. A bucket nothing was recorded for is left out of
the result rather than reported as zero, and a counter the inverter never sent
comes back as None. A month the house used nothing did not happen; a month we
were not running did, and the two must not look alike.
"""

from __future__ import annotations

import logging
import os
from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from arraysense.metrics import lookup
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

Period = Literal["day", "month"]

# What a row of the history table holds, and which lifetime counter answers it.
# Lifetime rather than daily counters throughout: they are monotonic, so any
# period is end minus start, and they carry through a collection outage in a
# way summing the daily counters cannot.
#
# Charge and discharge stay separate, and so do import and export. The hardware
# counts each pair separately and the owner reads them separately — one is a
# bill and the other a credit, and the difference between the battery pair is
# round-trip loss. A single signed figure nets all of that away for good.
#
# eps_energy_total_kwh is deliberately absent. On the reference system it read
# 58.1 kWh on a day load read 95.3, with every instantaneous register saying
# the whole house runs through the backup panel. Until that is explained it is
# not a number to put in a table.
ENERGY_FIELDS: Mapping[str, str] = {
    "load_kwh": "load_energy_total_kwh",
    "solar_kwh": "pv_energy_total_kwh",
    "battery_charged_kwh": "battery_charge_energy_total_kwh",
    "battery_discharged_kwh": "battery_discharge_energy_total_kwh",
    "grid_imported_kwh": "grid_import_energy_total_kwh",
    "grid_exported_kwh": "grid_export_energy_total_kwh",
}

# A rename in the registry has to fail here, loudly, at import. Left unchecked
# it would surface as a column of nulls in every bucket, which reads exactly
# like an inverter that reports no energy at all.
for _metric in ENERGY_FIELDS.values():
    lookup(_metric)

# How far apart two readings may be and still bracket a bucket boundary. The
# hourly tier answers the longest ranges, so anything under an hour would mark
# every bucket incomplete; two hours clears that with room to spare and is
# still short enough that a real outage is reported rather than absorbed into
# the day it ended in.
MAX_EDGE_GAP = timedelta(hours=2)

# How far a counter must fall before the fall is read as a reset rather than a
# bad reading. A counter that restarts comes back near zero, so halving is
# already far beyond anything a decode glitch produces. The distinction is
# load-bearing in one direction: a lifetime counter at 36,246 kWh that dips to
# 36,245 is noise, and crediting the new value as post-reset energy would add
# thirty-six thousand kWh to somebody's month.
RESET_MAX_REMAINING = 0.5

# Energy counters carry one decimal place, so every difference between two of
# them is a whole number of tenths. Rounding to that removes the float noise
# that accumulates over a month of additions — 41.199999999999996 — without
# discarding anything the inverter actually measured.
_KWH_PLACES = 1

# Where the machine records its own zone, when TZ says nothing. Every packaging
# of the tz database keeps the zone name as the tail of the path, so the key is
# whatever follows the last "zoneinfo/" component.
_LOCALTIME = Path("/etc/localtime")
_ZONEINFO_DIR = "/zoneinfo/"

# Which tier answers which period. Both are chosen for the cost of the read.
# A day's kWh telescopes — the counter at the end minus the counter at the
# start — so a coarser tier moves at most one bucket-edge's worth of energy
# between neighbours and never loses any. The minute tier read 43,000 rows to
# produce thirty one-day numbers; the hourly tier reads roughly 720 for the
# same answer. Both periods now prefer hourly.
_PERIOD_TIER: Mapping[Period, str] = {"day": "hourly", "month": "hourly"}

# Where to look when the preferred tier has nothing, in order. Both fall
# straight to raw. A database whose hourly tier is empty also has an empty
# minute tier — both rollups run in the same maintenance pass — so skipping
# minute costs nothing and gets the query answered from the only tier that
# could actually have rows behind a stalled rollup.
_FALLBACK_TIERS: Mapping[Period, tuple[str, ...]] = {
    "day": ("full",),
    "month": ("full",),
}


@dataclass(frozen=True)
class DroppedSpan:
    """One stretch of a counter's climb that no bucket could take.

    ``start`` and ``end`` are the readings either side of the stretch, so the
    span is real measurement, not inference. ``kwh`` is the climb between
    them where the counter behaved, and None where it reset or stepped
    backwards inside the span — the pre-reset energy is unknowable, and
    reporting the post-reset climb as "the missing amount" would understate
    it by everything the counter held before the restart.

    The field is the bucket-total key from ``ENERGY_FIELDS``, not the metric
    name, because that is the vocabulary everything downstream of a bucket
    already speaks.
    """

    field: str
    start: datetime
    end: datetime
    kwh: float | None


@dataclass(frozen=True)
class EnergyAttribution:
    """What one walk of the counters attributed, and what it could not.

    The buckets are exactly ``bucket_totals``'s — attribution does not move.
    ``dropped`` is what used to be discarded: pairs too far apart across an
    edge to place, resets, and the pair straddling the final edge that the
    walk previously skipped without record. ``bounds`` is each counter's
    first and last reading, the only evidence of a counter that went quiet
    and never came back, since no pair exists past its last answer.

    Money is why this exists (#23): a cost priced from the buckets alone is
    short by every dropped span, and it was presented as whole because
    nothing carried what the walk knew it had dropped.
    """

    buckets: list[EnergyBucket]
    dropped: tuple[DroppedSpan, ...]
    bounds: Mapping[str, tuple[datetime, datetime]]


@dataclass(frozen=True)
class EnergyBucket:
    """One calendar day or month, and the energy that passed through it.

    ``start`` and ``end`` are timezone-aware in the owner's zone, and ``end``
    is exclusive, so consecutive buckets are contiguous and nothing falls
    between them. The pair is not always 24 hours apart even for a day: that is
    the point of holding both.

    ``complete`` says the total covers the whole bucket. It is false for the
    bucket the owner is still living through, for the first one if collection
    started partway into it, for one whose edge was lost to an outage, and for
    one in which a counter restarted — all cases where the number is real but
    is not the whole period, and presenting it as a low day would be a lie the
    data does not support.

    ``totals`` is keyed by the names in ``ENERGY_FIELDS`` — ``load_kwh``,
    ``solar_kwh``, ``battery_charged_kwh``, ``battery_discharged_kwh``,
    ``grid_imported_kwh``, ``grid_exported_kwh`` — each in kWh. A value of None
    means nothing was recorded to derive it from, which is not the same as
    0.0: a counter that sat still all month genuinely measured zero.
    """

    start: datetime
    end: datetime
    complete: bool
    totals: Mapping[str, float | None]


def host_zone(env: Mapping[str, str] = os.environ, localtime: Path = _LOCALTIME) -> ZoneInfo:
    """Return the timezone this machine keeps, for when a caller names none.

    The owner's day is the only day this page is about, so the fallback has to
    be their zone rather than UTC — on a UTC default an evening in New York
    lands in tomorrow, and every daily total is wrong by an evening. TZ wins
    where it is set, since that is how an operator overrides the machine, and
    otherwise the zone name is read off the ``/etc/localtime`` symlink.

    Never raises. A machine with no resolvable zone falls back to UTC with a
    warning, because a history page that renders in the wrong zone is a great
    deal better than one that does not render.
    """
    name = env.get("TZ", "").strip() or _zone_from_localtime(localtime)
    if not name:
        logger.warning("no host timezone found; falling back to UTC")
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("host timezone %r is not a known zone; falling back to UTC", name)
        return ZoneInfo("UTC")


def _zone_from_localtime(localtime: Path) -> str:
    """Return the zone key ``/etc/localtime`` points at, or empty if it points nowhere.

    The file is a symlink into the tz database, so the key is the path below
    the database's own directory: ``/usr/share/zoneinfo/America/New_York``
    yields ``America/New_York``. The directory differs between distributions
    and on macOS, which is why the marker is matched rather than a prefix
    stripped.
    """
    try:
        resolved = localtime.resolve().as_posix()
    except OSError:
        logger.warning("cannot resolve %s", localtime)
        return ""
    marker = resolved.rfind(_ZONEINFO_DIR)
    return resolved[marker + len(_ZONEINFO_DIR) :] if marker >= 0 else ""


def resolve_zone(name: str | None, configured: str | None = None) -> ZoneInfo:
    """Return the zone this answer is cut on: the installation's, the caller's, the host's.

    ``configured`` is the installation's own zone from the settings registry,
    and it wins because the inverter is in one place while the person looking
    at it may not be (#7). A phone that has travelled reports a different zone
    and would otherwise get a different answer for the same day — and the
    answer it gets is confidently wrong rather than obviously wrong, because a
    bill drawn against the wrong midnight looks entirely normal.

    ``name`` is what the browser asked for, and it still answers when the
    installation has stated no zone: that is every install today, and the empty
    setting is the default precisely so none of them moves. Where a zone *is*
    configured, ``name`` is not consulted at all — not even to reject it — so a
    stale or malformed zone from a client cannot refuse a request whose answer
    is already fully determined.

    A configured zone that does not resolve is warned about and stepped past
    rather than raised on. It is refused at the point it is typed, so this is a
    value stored before that check existed or written straight into the
    database; failing every request over it would take down a page that has a
    perfectly good answer available.

    Raises:
        KeyError: ``name`` is not a zone in the tz database, and no zone is
            configured to override it. A typo has to be refused rather than
            quietly answered in some other zone, since every total in the reply
            depends on which midnight it was cut at.
    """
    if configured is not None and configured.strip():
        try:
            return ZoneInfo(configured.strip())
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "configured timezone %r is not a known zone; falling back to the request's",
                configured,
            )
    if name is None or not name.strip():
        return host_zone()
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise KeyError(f"unknown timezone: {name!r}") from exc


def with_zone(moment: datetime, zone: ZoneInfo) -> datetime:
    """Return ``moment`` as an aware datetime, reading a naive one as local time.

    A query string carries "2026-07-01T00:00" as often as it carries an offset,
    and a naive datetime handed to the store is read as the *server's* local
    time on the way to epoch seconds. Reading it as the zone the request asked
    about instead is both what the caller meant and the only reading that does
    not depend on where the service happens to be installed.
    """
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment


def _period_start(moment: datetime, period: Period, zone: ZoneInfo) -> datetime:
    """Return the start of the calendar day or month containing ``moment``."""
    local = moment.astimezone(zone)
    if period == "month":
        return datetime(local.year, local.month, 1, tzinfo=zone)
    return datetime(local.year, local.month, local.day, tzinfo=zone)


def _next_edge(edge: datetime, period: Period, zone: ZoneInfo) -> datetime:
    """Return the boundary after ``edge``, one calendar day or month on.

    The arithmetic is on the calendar and never on the aware datetime. Adding
    ``timedelta(days=1)`` to a zoned midnight adds 24 hours of wall clock, so
    across a spring-forward it lands on 01:00 and every boundary after it is an
    hour out. Advancing the date and re-attaching the zone asks the zone what
    that day's midnight was, which is the question actually being asked.
    """
    if period == "month":
        year, month = (edge.year + 1, 1) if edge.month == 12 else (edge.year, edge.month + 1)
        try:
            return datetime(year, month, 1, tzinfo=zone)
        except ValueError:
            # ``datetime(10000, 1, 1, ...)`` raises ValueError because the
            # constructor sees the out-of-range year before any arithmetic.
            # Convert it so the single ``_inside_the_calendar`` guard catches
            # every shape the same mistake can take.
            raise OverflowError("date value out of range") from None
    day = date(edge.year, edge.month, edge.day) + timedelta(days=1)
    return datetime(day.year, day.month, day.day, tzinfo=zone)


def bucket_edges(start: datetime, end: datetime, period: Period, zone: ZoneInfo) -> list[datetime]:
    """Return the calendar boundaries covering ``start`` to ``end``, oldest first.

    There is one more edge than there are buckets: bucket *i* runs from
    ``edges[i]`` to ``edges[i + 1]``, half-open, so a reading exactly at
    midnight belongs to the day beginning and the day ending shares no instant
    with it. The range is widened outward to whole buckets, since a caller
    asking about "the last 30 days" means 30 of the owner's days and not a
    30-day window ending at whatever time they asked.

    A handful of zones — Santiago, historically São Paulo — move their clocks
    at midnight, so a local midnight can be a time that never happened. The
    zone resolves it to the instant before the change, which keeps every
    boundary an instant and keeps consecutive buckets contiguous; the day is
    then measured from an hour that did exist.

    Raises:
        ValueError: ``end`` is not after ``start``, or either is naive. A naive
            bound cannot be placed on the owner's calendar at all, and silently
            reading it as the server's local time is how a history page ends up
            an hour out for half the year.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("bucket_edges needs timezone-aware bounds; see with_zone")
    if end <= start:
        raise ValueError(f"end must be after start, got {start.isoformat()} to {end.isoformat()}")

    local_end = end.astimezone(zone)
    edges = [_period_start(start, period, zone)]
    while edges[-1] < local_end:
        edges.append(_next_edge(edges[-1], period, zone))
    return edges


def _delta(previous: float, current: float) -> tuple[float | None, bool]:
    """Return the energy between two counter readings, and whether any was lost.

    Three cases, and the middle one is why this is not a subtraction. A counter
    that climbed reports the climb. A counter that went back to near zero
    restarted — a firmware update or a replaced BMS — so what is known is the
    climb since the restart, and whatever it held before the restart in that
    interval is not knowable; the caller is told so. A counter that slipped
    backwards without restarting is a bad reading rather than a reset, and
    contributes nothing: crediting the new value as post-reset energy would add
    the whole lifetime of the inverter to one bucket.

    Returns the energy in kWh — None when nothing can be attributed — and
    whether the interval lost energy that no bucket will ever account for.
    """
    if current >= previous:
        return current - previous, False
    if current <= previous * RESET_MAX_REMAINING:
        logger.info("energy counter reset: %.1f -> %.1f kWh", previous, current)
        return current, True
    logger.warning("energy counter stepped backwards: %.1f -> %.1f kWh", previous, current)
    return None, True


def _covered(moments: Sequence[datetime], edge: datetime, max_gap: timedelta) -> bool:
    """Whether readings bracket ``edge`` closely enough to cut a bucket at it.

    A boundary is only usable if something was recorded on each side of it
    within ``max_gap``: a reading exactly at midnight brackets it perfectly, a
    reading either side a minute apart is as good, and a five-hour hole across
    it means the energy in the hole belongs to neither neighbour and both are
    short by an unknown amount. This is what stops an outage being billed to
    the day collection came back in.
    """
    after = bisect_left(moments, edge)
    if after == len(moments) or (after == 0 and moments[0] != edge):
        return False
    before = after if moments[after] == edge else after - 1
    return moments[after] - moments[before] <= max_gap


def bucket_totals(
    rows: Sequence[Mapping[str, object]],
    edges: Sequence[datetime],
    max_gap: timedelta = MAX_EDGE_GAP,
) -> list[EnergyBucket]:
    """Total each bucket's energy from counter readings, oldest bucket first.

    The buckets from ``attribute_energy``, for the caller that wants no more —
    the energy tables and charts, whose completeness question ``complete``
    already answers. Money wants the rest; see ``EnergyAttribution``.
    """
    return attribute_energy(rows, edges, max_gap).buckets


def attribute_energy(
    rows: Sequence[Mapping[str, object]],
    edges: Sequence[datetime],
    max_gap: timedelta = MAX_EDGE_GAP,
) -> EnergyAttribution:
    """Attribute every counter's climb to buckets, and account for what cannot be.

    Every consecutive pair of readings of one counter is one interval of
    energy, credited to the bucket the interval *ends* in — the bucket holding
    the instant just before the later reading, so a reading exactly at midnight
    closes the day it ends rather than opening the next one. Summed that way a
    bucket's total telescopes to end-minus-start across it, adjacent buckets
    share no energy and lose none between them, and a gap in collection *inside*
    one bucket costs nothing at all, because the counter went on counting while
    we were not watching. That last property is the whole reason for reading
    energy off the inverter instead of integrating power.

    An interval that crosses a boundary and is longer than ``max_gap`` is the
    one thing that cannot be attributed: the energy is real, but which side of
    midnight it happened on is not knowable. It is credited to neither bucket
    and both are marked incomplete, which is the difference between a day that
    reads low and a day that reads like the outage never happened. What is new
    here is that the pair is *recorded* rather than discarded — both readings
    exist, so the amount that went unplaced is exact, and it is precisely what
    a money figure priced from these buckets is short by. A reset is recorded
    the same way with no amount, and so is the pair straddling the final edge,
    which the walk previously skipped without a trace: its bucket could read
    complete while the last stretch of the range sat in no bucket at all.

    Attribution itself is unchanged by any of the recording, deliberately —
    the History page's energy columns render these buckets, and #23 is about
    the money, not about them.

    ``rows`` carry a ``timestamp`` and whichever of the counters named in
    ``ENERGY_FIELDS`` were reported; each counter is followed independently, so
    one going absent for an hour does not disturb the others. Rows may arrive
    in any order. A bucket that ends with nothing to report at all is left out
    of the result rather than returned as zero.
    """
    if len(edges) < 2:
        return EnergyAttribution(buckets=[], dropped=(), bounds={})
    ordered = sorted(rows, key=_row_time)
    count = len(edges) - 1
    totals: list[dict[str, float | None]] = [dict.fromkeys(ENERGY_FIELDS) for _ in range(count)]
    lost = [False] * count
    dropped: list[DroppedSpan] = []
    series, moments = _counter_series(ordered)

    for field in ENERGY_FIELDS:
        # Which bucket a reading lands in is found by walking the edges
        # alongside the readings rather than searching for each one. Both are
        # in time order, so the edge pointer only ever moves forward — and a
        # binary search per reading was measured at 0.8 s of the second this
        # took over a month of minute readings on the reference machine,
        # because every step of it compares two zone-aware datetimes.
        passed = 0
        for (before, previous), (after, current) in pairwise(series[field]):
            while passed < len(edges) and edges[passed] < after:
                passed += 1
            index = passed - 1
            if not 0 <= index < count:
                # A pair ending past the final edge still began before it when
                # ``before`` says so, and the stretch up to that edge belongs
                # to the range. Skipping it silently left the last bucket
                # short with no flag anywhere. Recorded — attributed nowhere,
                # exactly because where the energy fell is unknown — but only
                # beyond ``max_gap``: an ordinary cadence-width pair straddles
                # this edge on every historical range ever asked about while
                # later data exists, and recording those flagged the last day
                # of perfectly clean months. Within the tolerance this edge
                # loses at most what the first edge carries in, and both are
                # accepted for the same reason.
                if index == count and before < edges[-1] and after - before > max_gap:
                    dropped.append(_span(field, before, previous, after, current))
                continue
            # An interval that stays inside one bucket is attributable however
            # long it is; one that crosses a boundary is only attributable if
            # it is short enough that the boundary still means something. The
            # comparison is strict because an interval is (before, after]: one
            # starting exactly on the boundary lies wholly inside the bucket,
            # and a reading at midnight followed by a three-hour hole is a
            # measured day, not a lost one.
            if before < edges[index] and after - before > max_gap:
                lost[index] = True
                dropped.append(_span(field, before, previous, after, current))
                continue
            energy, incomplete = _delta(previous, current)
            lost[index] = lost[index] or incomplete
            if incomplete:
                # A reset or backstep loses an unknowable amount wherever it
                # happens; the post-reset climb below still counts, as ever.
                dropped.append(DroppedSpan(field=field, start=before, end=after, kwh=None))
            if energy is None:
                continue
            running = totals[index][field]
            totals[index][field] = energy if running is None else running + energy

    buckets = [
        EnergyBucket(
            start=edges[index],
            end=edges[index + 1],
            complete=(
                not lost[index]
                and _covered(moments, edges[index], max_gap)
                and _covered(moments, edges[index + 1], max_gap)
            ),
            totals={
                field: None if value is None else round(value, _KWH_PLACES)
                for field, value in totals[index].items()
            },
        )
        for index in range(count)
        if any(value is not None for value in totals[index].values())
    ]
    return EnergyAttribution(
        buckets=buckets,
        dropped=tuple(dropped),
        bounds={
            field: (readings[0][0], readings[-1][0])
            for field, readings in series.items()
            if readings
        },
    )


def _span(
    field: str, before: datetime, previous: float, after: datetime, current: float
) -> DroppedSpan:
    """Record one unattributable pair, with its energy only if the counter behaved.

    Not ``_delta``: that returns the post-reset climb for a reset, which is
    real energy for a bucket and the wrong number for a span — the span's
    question is how much passed between the two readings, and across a reset
    that includes everything the counter held before restarting, which is
    unknowable.

    A pair whose delta is exactly zero is recorded with ``kwh`` 0.0 and is
    evidence rather than loss: the counter is monotonic, so zero between the
    readings proves zero everywhere between them. Dropping such pairs
    entirely lost that proof — a band emptied by a flat stretch could no
    longer show its energy was accounted for, and exact figures were flagged.
    The shortfall arithmetic treats a zero span as pure coverage: it adds
    nothing and can never raise a flag.
    """
    return DroppedSpan(
        field=field,
        start=before,
        end=after,
        kwh=current - previous if current >= previous else None,
    )


def _row_time(row: Mapping[str, object]) -> datetime:
    """Return a row's timestamp.

    Raises:
        TypeError: the row has no timezone-aware timestamp. Every store read
            supplies one, so this is a hand-built row, and sorting a mix of
            naive and aware datetimes fails far from the cause.
    """
    when = row.get("timestamp")
    if not isinstance(when, datetime) or when.tzinfo is None:
        raise TypeError(f"row needs a timezone-aware timestamp, got {when!r}")
    return when


def _counter_series(
    ordered: Sequence[Mapping[str, object]],
) -> tuple[dict[str, list[tuple[datetime, float]]], list[datetime]]:
    """Pull every counter's readings, and the instants any of them arrived, out of the rows.

    Each counter is followed independently, so one going absent for an hour
    does not disturb the others, and the instants are collected alongside them
    because a bucket boundary is only usable if something was recorded either
    side of it.

    A failed poll is stored as a row carrying its reason and no readings, and
    an inverter can answer a poll without answering the energy registers.
    Neither is evidence that a boundary is covered, so a row with nothing on it
    contributes no instant — counting them would let an outage's own gap
    markers certify the very boundary they are a gap in.

    One pass rather than one per counter and another for the instants. That is
    seven walks of the same rows collapsed to one, and it is worth the shape of
    this function: the History page bucketed a month of minute readings twice,
    once for the energy and once for the bands the cost is split across, and
    the two passes cost a full second each on the reference machine.
    """
    series: dict[str, list[tuple[datetime, float]]] = {field: [] for field in ENERGY_FIELDS}
    moments: list[datetime] = []
    for row in ordered:
        when = _row_time(row)
        reported = False
        for field, metric in ENERGY_FIELDS.items():
            value = row.get(metric)
            if isinstance(value, int | float):
                series[field].append((when, float(value)))
                reported = True
        if reported:
            moments.append(when)
    return series, moments


def _counter_rows(
    store: SqliteStore,
    edges: Sequence[datetime],
    period: Period,
    max_gap: timedelta,
) -> list[dict[str, object]]:
    """Read the counter values covering these bucket boundaries, cheapest tier first.

    The read is widened by ``max_gap`` at both ends so the first and last
    buckets have something to be measured *from*, rather than starting at
    whatever reading happens to fall inside them.

    Which tier answers depends on the period, for cost rather than accuracy:
    thirty days of raw readings is a quarter of a million rows to produce
    thirty numbers. A coarser tier moves at most one bucket edge's worth of
    energy between neighbouring buckets and loses none, since the totals
    telescope. An empty preferred tier means either that the rollup has not run
    or that the range is older than that tier is kept for, so the read falls
    back through the coarser ones rather than reporting a year of nothing.
    """
    metrics = list(ENERGY_FIELDS.values())
    lo, hi = edges[0] - max_gap, edges[-1] + max_gap
    tier = _PERIOD_TIER[period]
    rows = store.query(metrics, lo, hi, tier=tier)
    for coarser in _FALLBACK_TIERS[period]:
        if rows or coarser == tier:
            break
        logger.debug("%s tier empty over the range; trying %s", tier, coarser)
        rows = store.query(metrics, lo, hi, tier=coarser)
    return rows


@dataclass(frozen=True)
class EnergyRead:
    """One read of the counters: the calendar it was cut on, the rows, the buckets.

    The rows come back with the buckets because the same readings answer two
    questions — how much energy a day held, and what that day cost — and the
    second is worked out from the band boundaries inside the day rather than
    from the day's total. Reading them twice would be a second trip through a
    month of rows to produce a figure that has to agree with the first, which
    is the shape of every disagreement this codebase has had.
    """

    edges: tuple[datetime, ...]
    rows: list[dict[str, object]]
    buckets: list[EnergyBucket]


def read_energy(
    store: SqliteStore,
    start: datetime,
    end: datetime,
    period: Period = "day",
    zone: ZoneInfo | None = None,
    max_gap: timedelta = MAX_EDGE_GAP,
) -> EnergyRead:
    """Read the energy in each calendar day or month between ``start`` and ``end``.

    The range is widened outward to whole buckets in ``zone``, defaulting to
    the machine's own, and the counters are read across them.

    The installation's configured zone is not consulted here. The caller
    resolves it once with ``resolve_zone`` and passes the answer, because the
    same zone has to cut the buckets and price the bands in a single request —
    two lookups is two chances to disagree, and money is the wrong thing to
    decide twice.
    """
    zone = zone or host_zone()
    start, end = with_zone(start, zone), with_zone(end, zone)
    edges = bucket_edges(start, end, period, zone)
    rows = _counter_rows(store, edges, period, max_gap)
    return EnergyRead(
        edges=tuple(edges),
        rows=rows,
        buckets=bucket_totals(rows, edges, max_gap),
    )


def energy_totals(
    store: SqliteStore,
    start: datetime,
    end: datetime,
    period: Period = "day",
    zone: ZoneInfo | None = None,
    max_gap: timedelta = MAX_EDGE_GAP,
) -> list[EnergyBucket]:
    """Return just the buckets from a ``read_energy``, for a caller wanting no more."""
    return read_energy(store, start, end, period, zone, max_gap).buckets


def counter_kwh(
    store: SqliteStore,
    start: datetime,
    end: datetime,
    field: str = "load_kwh",
    max_gap: timedelta = MAX_EDGE_GAP,
) -> float | None:
    """One counter's energy between two exact instants, or None if that is unknowable.

    Deliberately not ``read_energy`` with a narrow range. That widens outward
    to whole calendar buckets, which is right for a day or a month and wrong
    for a window: asked about six hours it answers about a day, and a caller
    dividing something by it — the Circuits tab's coverage share — would print
    a quarter of the truth as though it were the truth.

    Deliberately not integrated from power either. Every kWh this project shows
    is read from a counter the inverter keeps itself, and an integrated figure
    sitting beside counter-read ones is an estimate wearing a meter reading's
    clothes. A caller that cannot have a real figure is given None and can say
    so.

    Only the one counter is read, rather than all six. That is what makes the
    answer's completeness a statement about *this* counter: ``attribute_energy``
    marks a bucket incomplete for the whole bucket rather than per field, so a
    grid-export glitch would otherwise blank the house's load, and a boundary
    could be certified as bracketed by a reading of some other counter
    entirely.

    None in three cases, and they are one case: the number is not known. Either
    bound unbracketed by readings within ``max_gap`` means the energy at that
    edge belongs to nobody — which includes a window ending later than the
    newest reading, so a range running to "now" answers None until the next
    poll lands. A counter that went backwards is a reset or a bad reading and
    ``_delta`` already refuses to attribute it. And a counter that never
    reported is not a house that used nothing.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("counter_kwh needs timezone-aware bounds; see with_zone")
    if end <= start:
        raise ValueError(f"end must be after start, got {start.isoformat()} to {end.isoformat()}")
    if field not in ENERGY_FIELDS:
        raise ValueError(f"unknown energy counter {field!r}")

    # Widened at both ends so the window's own edges have readings to be
    # measured *from*, exactly as ``_counter_rows`` widens a calendar read. The
    # widening decides which rows are fetched and never which instants are
    # attributed: the edges handed to the bucketing below are the window as
    # asked for.
    metrics = [ENERGY_FIELDS[field]]
    tier = _window_tier(end - start)
    rows = store.query(metrics, start - max_gap, end + max_gap, tier=tier)
    if not rows:
        # Falling through to the finest tier, which is named "full" — the prose
        # around ``_FALLBACK_TIERS`` calls it raw and a query for "raw" reaches
        # no table at all. An empty preferred tier means either that the rollup
        # has not run or that the window is older than that tier is kept for,
        # and full is the one tier that could still hold rows behind a stalled
        # rollup.
        logger.debug("%s tier empty over the window; trying full", tier)
        rows = store.query(metrics, start - max_gap, end + max_gap, tier="full")

    # One bucket, cut at the window's own bounds. ``attribute_energy`` already
    # knows a reset from a bad reading, already refuses to attribute an
    # interval straddling an edge by more than ``max_gap``, and already leaves
    # out a bucket with nothing to report rather than returning it as zero.
    # Walking the readings here instead would be a second copy of the rule that
    # prices this project's money — and one that has to measure from the
    # widened rows, which is the very widening this function exists to avoid.
    buckets = bucket_totals(rows, [start, end], max_gap)
    if not buckets or not buckets[0].complete:
        return None
    return buckets[0].totals[field]


def _window_tier(span: timedelta) -> str:
    """Which tier answers a counter read over ``span``, trading cost against precision.

    Two days is the hinge, and precision is what sits on the near side of it. A
    tier can only cut a window at a reading it holds, so hourly rows place both
    edges on the whole hour: a six-hour window opening at 12:34 is measured
    from 12:00 to 18:00, and what it is wrong by is the difference between
    those two part-hours — up to an hour's energy, which on the reference
    house's 11 kW evening is up to 11 kWh. A coverage share computed against
    that denominator inherits all of it. Minute rows place the same edge to
    within a minute, and the read is cheap at this length: six hours widened by
    ``MAX_EDGE_GAP`` at each end is about six hundred rows.

    Past two days the trade reverses. Thirty days of minute rows is over forty
    thousand to produce one number, and an hour's imprecision against a month
    is a fraction of a percent.
    """
    return "minute" if span <= timedelta(days=2) else "hourly"
