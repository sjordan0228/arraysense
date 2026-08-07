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

# Which tier answers which period. Both are chosen for the cost of the read
# rather than for accuracy — the totals telescope, so a coarser tier moves at
# most one bucket-edge's worth of energy between neighbours and never loses
# any. Thirty days of raw is a quarter of a million rows to produce thirty
# numbers; the minute tier is a fifth of that and is retained for a year, and
# the hourly tier is the only one that outlives a year at all.
_PERIOD_TIER: Mapping[Period, str] = {"day": "minute", "month": "hourly"}

# What to fall back to when the preferred tier has nothing. The coarse tiers
# are rollup destinations, so a database whose rollup has not run yet has empty
# ones, and answering "no energy" from an empty tier while the readings sit in
# the raw table would be the worst of both.
_FALLBACK_TIER = "full"


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


def resolve_zone(name: str | None) -> ZoneInfo:
    """Return the zone a caller asked for, falling back to the machine's own.

    Raises:
        KeyError: ``name`` is not a zone in the tz database. A typo has to be
            refused rather than quietly answered in some other zone, since
            every total in the reply depends on which midnight it was cut at.
    """
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
        return datetime(year, month, 1, tzinfo=zone)
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
    reads low and a day that reads like the outage never happened.

    ``rows`` carry a ``timestamp`` and whichever of the counters named in
    ``ENERGY_FIELDS`` were reported; each counter is followed independently, so
    one going absent for an hour does not disturb the others. Rows may arrive
    in any order. A bucket that ends with nothing to report at all is left out
    of the result rather than returned as zero.
    """
    if len(edges) < 2:
        return []
    ordered = sorted(rows, key=_row_time)
    count = len(edges) - 1
    totals: list[dict[str, float | None]] = [dict.fromkeys(ENERGY_FIELDS) for _ in range(count)]
    lost = [False] * count

    for field, metric in ENERGY_FIELDS.items():
        readings = [
            (_row_time(row), float(value))
            for row in ordered
            if isinstance(value := row.get(metric), int | float)
        ]
        for (before, previous), (after, current) in pairwise(readings):
            index = bisect_left(edges, after) - 1
            if not 0 <= index < count:
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
                continue
            energy, incomplete = _delta(previous, current)
            lost[index] = lost[index] or incomplete
            if energy is None:
                continue
            running = totals[index][field]
            totals[index][field] = energy if running is None else running + energy

    moments = _reported_moments(ordered)
    return [
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


def _reported_moments(ordered: Sequence[Mapping[str, object]]) -> list[datetime]:
    """Return the instants at which any energy counter was actually reported.

    A failed poll is stored as a row carrying its reason and no readings, and
    an inverter can answer a poll without answering the energy registers.
    Neither is evidence that a bucket boundary is covered, so both are dropped
    here — counting them would let an outage's own gap markers certify the very
    boundary they are a gap in.
    """
    return [
        _row_time(row)
        for row in ordered
        if any(isinstance(row.get(metric), int | float) for metric in ENERGY_FIELDS.values())
    ]


def energy_totals(
    store: SqliteStore,
    start: datetime,
    end: datetime,
    period: Period = "day",
    zone: ZoneInfo | None = None,
    max_gap: timedelta = MAX_EDGE_GAP,
) -> list[EnergyBucket]:
    """Return the energy in each calendar day or month between ``start`` and ``end``.

    The range is widened outward to whole buckets in ``zone``, defaulting to
    the machine's own, and the read is widened again by ``max_gap`` at both
    ends so the first and last buckets have something to be measured from
    rather than starting at whatever reading happens to fall inside them.

    Which tier answers depends on the period, for cost rather than accuracy:
    thirty days of raw readings is a quarter of a million rows to produce
    thirty numbers. A coarser tier moves at most one bucket edge's worth of
    energy between neighbouring buckets and loses none, since the totals
    telescope. An empty coarse tier means the rollup has not run, and the read
    falls back to raw rather than reporting a year of nothing.
    """
    zone = zone or host_zone()
    start, end = with_zone(start, zone), with_zone(end, zone)
    edges = bucket_edges(start, end, period, zone)
    metrics = list(ENERGY_FIELDS.values())
    tier = _PERIOD_TIER[period]
    rows = store.query(metrics, edges[0] - max_gap, edges[-1] + max_gap, tier=tier)
    if not rows and tier != _FALLBACK_TIER:
        logger.debug("%s tier empty over the range; falling back to %s", tier, _FALLBACK_TIER)
        rows = store.query(metrics, edges[0] - max_gap, edges[-1] + max_gap, tier=_FALLBACK_TIER)
    return bucket_totals(rows, edges, max_gap)
