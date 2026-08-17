"""tariff.py — price the energy the inverter counted, and say what the solar saved.

The economic case for a solar and battery system is a number nobody can read off
an inverter: what the array and the bank earned against the bill that would have
arrived without them. That number needs a tariff, and a real bill is not one rate
per kilowatt-hour. The reference installation pays a fixed connection charge every
month whatever it uses, and two different rates depending on the hour. So the model
here is a fixed monthly charge plus a list of rate bands, each with a price and the
hours it applies to — shaped so a third band, or a schedule that differs at the
weekend, is another entry rather than a rewrite.

Three rules run through everything below.

*Never invent a rate.* With nothing configured there is no tariff, and with no
tariff every money figure is absent rather than zero. A dashboard showing $0.00
because the owner has not entered a tariff is lying more convincingly than one
showing nothing at all.

*The fixed charge is not a saving.* It is payable whether the roof is covered in
panels or moss, so it belongs in the estimated bill and nowhere near the savings
figure. Including it would make the solar look worse the more it saved.

*A month is a wall-clock month.* Everything is stored as UTC epoch seconds, but the
owner's November has 721 hours in it, not 720, and their March has 743. Elapsed time
is therefore measured in UTC and month boundaries in local time — and never by
dividing by 86400, which misprices the fixed charge twice a year by an hour.

On top of the bands sit the monthly adjustment factors, PCRF and SCRF, which the
supplier re-sets every month and charges on every kilowatt-hour. They are a table
keyed by billing month rather than a pair of numbers, because a pair would be
overwritten each month and re-pricing July in September would then charge July at
September's factors — restating a bill that has already been paid. A month with
nothing recorded is priced at the base rate and reported as unadjusted, never as
adjusted by zero: zero is a measurement, and nobody took it.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal

logger = logging.getLogger(__name__)

SETTING_BANDS = "tariff.bands"
SETTING_FIXED_MONTHLY = "tariff.fixed_monthly"
SETTING_CURRENCY = "tariff.currency"
SETTING_EXPORT_PER_KWH = "tariff.export_per_kwh"
SETTING_ADJUSTMENTS = "tariff.adjustments"

# Shown in the settings help as the format's only documentation, and parsed by a
# test so the example can never drift into one the parser would refuse.
EXAMPLE_BANDS = "Peak | 0.34 | 16:00-21:00; Off-peak | 0.11 | 21:00-16:00"
EXAMPLE_ADJUSTMENTS = "2026-07 | -0.001230 | 0.004560"

# Used when nothing is configured, purely so money has something in front of it.
# The point of the setting is that the next person's bill is not in dollars.
DEFAULT_CURRENCY = "$"

_MINUTES_PER_DAY = 24 * 60


def _money(amount: float) -> float:
    """Round a figure to the minor unit, once, at the point it is reported.

    Pricing each day and summing the rounded values drifts from pricing the
    total: three bands at 0.334 are 1.00 together and 0.99 apiece.
    """
    return round(amount, 2)


def _elapsed(start: datetime, end: datetime) -> float:
    """Return the real seconds between two aware datetimes, across a clock change.

    Both are converted to UTC first, and that conversion is the whole function.
    Subtracting two datetimes that share a ``tzinfo`` ignores the zone entirely —
    Python treats them as naive — so ``Dec 1 - Nov 1`` in New York comes back as
    720 hours when 721 of them really passed. Every duration here goes through
    this, because the one that does not is the one that misprices a bill.
    """
    return (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds()


def _require_aware(moment: datetime, what: str) -> None:
    """Refuse a naive datetime.

    Bands are wall-clock hours in the owner's zone. A datetime with no zone
    cannot be placed against them, and reading a UTC instant as a wall clock
    prices four in the afternoon as nine at night.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")


def _month_start(moment: datetime) -> datetime:
    """Return local midnight on the first of ``moment``'s month, keeping its zone."""
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0, fold=0)


def _next_month_start(month_start: datetime) -> datetime:
    """Return the first instant of the month after ``month_start``."""
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def _months_spanned(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """Every calendar month a period touches, oldest first, as (year, month).

    Read off the wall clock in whatever zone the bounds carry, because that is
    what a season and a billing month are. Stepped a day at a time rather than
    by arithmetic on month numbers, which keeps a period of a few hours from
    claiming a month it never entered.

    ``end`` is exclusive, so the final month is taken from the last instant
    *inside* the period. Adding ``end``'s own month instead made a query for
    the whole of October claim November as well — which nulled a seasonal
    tariff's every completed month, and would do the same to a rider recorded
    for October and not yet for November.
    """
    if end <= start:
        return []
    seen: list[tuple[int, int]] = []
    moment = start
    while moment < end:
        key = (moment.year, moment.month)
        if key not in seen:
            seen.append(key)
        moment += timedelta(days=1)
    last = end - timedelta(microseconds=1)
    if (last.year, last.month) not in seen:
        seen.append((last.year, last.month))
    return seen


@dataclass(frozen=True)
class TimeRange:
    """One stretch of the clock, which may run through midnight.

    Held as wall-clock times rather than minute offsets because that is how a
    person writes a tariff down. ``end`` is exclusive so two adjacent bands do
    not both claim the instant between them, and a range whose start is at or
    after its end wraps: 21:00-06:00 is the night, not an empty set. Getting
    that wrong puts every overnight kilowatt-hour in no band at all.
    """

    start: time
    end: time

    def contains(self, moment: time) -> bool:
        """Whether a wall-clock time falls in this range."""
        if self.start < self.end:
            return self.start <= moment < self.end
        # Wrapping, or a full day when the two ends coincide.
        return moment >= self.start or moment < self.end

    @property
    def label(self) -> str:
        """How to write this range for a reader.

        A whole day is stored with both ends at midnight, which spelled out
        literally reads "00:00-00:00" — an empty range, and the opposite of
        what it means.
        """
        if self.start == self.end:
            return "all day"
        return f"{self.start:%H:%M}\N{EN DASH}{self.end:%H:%M}"


@dataclass(frozen=True)
class RateBand:
    """A price per kilowatt-hour and the hours it applies to.

    ``months`` carries the season. Real tariffs are seasonal before they are
    time-of-use: the reference installation's is time-of-use from May to
    October and a single flat rate from November to April, so a model that
    knows only hours would price a January evening at a summer peak rate.
    None means every month.

    ``days`` is the same extension point for a weekend schedule: None means
    every day, and a set of ``date.weekday()`` numbers means only those.
    Nothing populates it yet — the parser has no syntax for it — but every
    decision about which band a moment belongs to goes through ``applies_at``,
    so adding weekends is a parser change and not a rewrite of the pricing.
    """

    name: str
    price_per_kwh: float
    hours: tuple[TimeRange, ...]
    months: frozenset[int] | None = None
    days: frozenset[int] | None = None

    @property
    def key(self) -> str:
        """The name in the form energy is matched against, ignoring case and space."""
        return self.name.strip().casefold()

    def applies_at(self, moment: datetime) -> bool:
        """Whether this band prices the given moment, read as a wall clock.

        The moment must already be in the owner's zone; see ``Tariff.band_at``.
        """
        _require_aware(moment, "moment")
        if self.months is not None and moment.month not in self.months:
            return False
        if self.days is not None and moment.weekday() not in self.days:
            return False
        clock = moment.time()
        return any(span.contains(clock) for span in self.hours)


AdjustmentStatus = Literal["none", "unknown", "applied"]


@dataclass(frozen=True)
class MonthlyAdjustment:
    """The per-kilowatt-hour riders one billing month was charged at.

    PCRF is a power cost recovery factor and SCRF a securitized charges
    recovery factor. Both sit on top of the band rate, both are charged on
    every metered kilowatt-hour, and both are re-set by the supplier every
    month — PCRF routinely to a *negative* number, which is a refund of
    over-recovered fuel cost and not a data error.

    The month is part of the value rather than context around it. Two settings
    holding this month's pair would be overwritten every month, and re-pricing
    July in September would then price July at September's factors and restate
    a bill the owner has already paid.

    Either factor may be None, meaning the supplier has published one and not
    the other. None is not nought: nought is a measurement saying the rider was
    zero that month, and ``per_kwh`` refuses to invent one.
    """

    year: int
    month: int
    pcrf_per_kwh: float | None
    scrf_per_kwh: float | None

    @property
    def per_kwh(self) -> float | None:
        """The two riders together, or None if either was never recorded."""
        if self.pcrf_per_kwh is None or self.scrf_per_kwh is None:
            return None
        return self.pcrf_per_kwh + self.scrf_per_kwh


@dataclass(frozen=True)
class AdjustmentRate:
    """The rider a period is charged at, and whether it is known at all.

    Three states, and conflating any two of them tells the owner something
    false. ``none`` is a tariff with no riders configured, where there is
    nothing to say and nothing to add. ``unknown`` is a tariff that has them
    but not for this period's month, where the base rate is used and the page
    has to say the adjustment is missing. ``applied`` is the only state that
    puts money on the bill.
    """

    status: AdjustmentStatus
    per_kwh: float | None = None
    pcrf_per_kwh: float | None = None
    scrf_per_kwh: float | None = None


_NO_ADJUSTMENT = AdjustmentRate(status="none")
_UNKNOWN_ADJUSTMENT = AdjustmentRate(status="unknown")


@dataclass(frozen=True)
class Tariff:
    """What the owner pays: a fixed monthly charge and the rate bands.

    ``export_per_kwh`` is None when the tariff pays nothing for export, which is
    different from paying zero only in what gets shown — a credit line reading
    nothing at all beats one reading $0.00 on a system that has exported 4.7 kWh
    in its life.

    Bands are tried in the order they were written and the first match wins, so
    an overlap resolves predictably rather than by whichever the dictionary
    happened to yield first. An hour no band claims has no price; see
    ``uncovered_minutes``.
    """

    bands: tuple[RateBand, ...]
    fixed_monthly: float = 0.0
    currency: str = DEFAULT_CURRENCY
    export_per_kwh: float | None = None
    # The riders, one entry per billing month. Empty means the supplier charges
    # none — a positive claim, which is why the flag beside it exists: stored
    # text that no longer parses is not evidence of anything, and reading it as
    # an empty tuple would assert an adjustment of zero nobody measured.
    adjustments: tuple[MonthlyAdjustment, ...] = ()
    adjustments_unreadable: bool = False

    def adjustment_at(self, start: datetime, end: datetime) -> AdjustmentRate:
        """The rider charged over a period, or why there is none to charge.

        A ``PeriodEnergy`` carries no split of its kilowatt-hours across
        months, so a period touching two months can only be charged a single
        rate. Where every month it touches was recorded and they all agree — a
        supplier that left the factors alone, or the ordinary case of a period
        inside one month — that rate is known. Where they differ, or where any
        month has no line, the answer is ``unknown`` and the caller prices at
        the base rate and says the adjustment is missing.

        Months are read off the wall clock of the bounds handed in, which are
        expected to be in the owner's zone: a billing month is a local month,
        and reading it off UTC moves the boundary by the offset.
        """
        if self.adjustments_unreadable:
            return _UNKNOWN_ADJUSTMENT
        if not self.adjustments:
            return _NO_ADJUSTMENT
        by_month = {(entry.year, entry.month): entry for entry in self.adjustments}
        pairs: set[tuple[float, float]] = set()
        wanted = _months_spanned(start, end)
        if not wanted:
            return _UNKNOWN_ADJUSTMENT
        for key in wanted:
            entry = by_month.get(key)
            if entry is None or entry.pcrf_per_kwh is None or entry.scrf_per_kwh is None:
                logger.info("no PCRF/SCRF recorded for %04d-%02d; pricing at the base rate", *key)
                return _UNKNOWN_ADJUSTMENT
            pairs.add((entry.pcrf_per_kwh, entry.scrf_per_kwh))
        if len(pairs) != 1:
            return _UNKNOWN_ADJUSTMENT
        pcrf, scrf = pairs.pop()
        return AdjustmentRate(
            status="applied", per_kwh=pcrf + scrf, pcrf_per_kwh=pcrf, scrf_per_kwh=scrf
        )

    def bands_in_effect(self, start: datetime, end: datetime) -> tuple[RateBand, ...]:
        """The bands that could apply at all between two instants.

        A seasonal band outside its season is not an unknown quantity, it is an
        inapplicable one, and the difference decides whether a bill can be
        priced. Treating a winter rate as unmeasured all summer nulls the whole
        period — which is how the estimated bill came to read as a dash every
        month of the year, telling the owner to come back tomorrow.

        Months are compared rather than the clock, since a band's hours can be
        empty on a given day without the band being out of season.
        """
        months = {month for _, month in _months_spanned(start, end)}
        if not months:
            return ()
        return tuple(band for band in self.bands if band.months is None or band.months & months)

    def band_at(self, moment: datetime) -> RateBand | None:
        """Return the band pricing this moment, or None if no band covers it.

        ``moment`` must be timezone-aware and already converted to the owner's
        zone, because the bands are wall-clock hours: handing this a UTC
        timestamp from the store prices the evening peak at whatever hour the
        offset makes it.
        """
        _require_aware(moment, "moment")
        for band in self.bands:
            if band.applies_at(moment):
                return band
        return None

    def price_at(self, moment: datetime) -> float | None:
        """Return the price per kilowatt-hour at this moment, or None if unpriced."""
        band = self.band_at(moment)
        return None if band is None else band.price_per_kwh

    def uncovered_minutes(self) -> int:
        """How many minutes of an ordinary day no band prices.

        Anything above zero means energy used in those hours has no band to be
        counted in, and a cost built from per-band totals would quietly come out
        low. Worth surfacing on the settings page rather than discovering as a
        bill that never matches.
        """
        day = datetime(2001, 1, 1, tzinfo=UTC)
        return sum(
            1
            for minute in range(_MINUTES_PER_DAY)
            if self.band_at(day.replace(hour=minute // 60, minute=minute % 60)) is None
        )


@dataclass(frozen=True)
class EnergyShortfall:
    """How much of one counter's energy the band split could not account for.

    ``attributed_kwh`` is the sum of the band mapping's known values, written
    once where the split happens so nobody sums the mapping a second time.
    ``unattributed_kwh`` is energy the meter counted that no band interval
    could take — a counter is monotonic, so this is always genuinely missing
    from the figures and a label may say so. ``unknowable`` marks loss whose
    size cannot be stated: a reset, a stretch reaching past the counter's
    readings, a span straddling the period's own edge. It is deliberately
    direction-neutral — a backstep inflates the next delta and an edge
    straddle carries foreign energy *in* — so anything rendered from it says
    the figure may not be exact, never that an amount is missing.

    This is what both reverted attempts at #23 lacked: attempt one had no way
    to show a figure at all, and attempt two derived its labels from minutes
    watched, which read 100% while a counter sat silent.

    ``bands_possibly_short`` names the bands whose windows were partly unmeasured,
    which is all that can be said and rather less than it sounds. It does not say
    the band is missing energy: a window can go unmeasured with nothing unplaced
    at all, as it does when a counter simply never reported over it. Nor does it
    say which band any unplaced energy belongs to — inside a dropped span the
    amount is exact in total and unlocatable within it, so the answer is a set of
    candidates and never a number. It exists to decide which band rows carry a
    qualification, and anything rendered from it must claim no amount (#31).
    """

    attributed_kwh: float
    unattributed_kwh: float
    unknowable: bool
    bands_possibly_short: frozenset[str] = frozenset()

    @property
    def short(self) -> bool:
        """Whether a figure built on this counter must not be read as whole."""
        return self.unattributed_kwh > 0 or self.unknowable


@dataclass(frozen=True)
class PeriodEnergy:
    """The energy a period used, already split into the tariff's bands.

    Time-of-use pricing needs energy per band, not per day: a daily counter
    cannot be split into peak and off-peak after the fact. Whoever fills this in
    reads the inverter's lifetime counters at each band boundary and differences
    them, which stays right across a collection gap in a way that integrating
    power does not.

    Both mappings are keyed by band name and their values are kilowatt-hours. A
    band that is missing, or present with None, is *unknown* rather than zero.
    Unknown propagates — a cost that cannot be told is reported as absent
    rather than as a total that quietly leaves a band out — unless the
    counter's ``shortfall`` entry accounts for what the total would be
    missing, in which case the measured part prices and is flagged (#23).
    ``load_kwh`` is the whole house, which is what the counterfactual bill is
    priced from; ``grid_export_kwh`` needs no band split until a tariff pays
    differently for export by hour.

    Nobody meters the bank, so ``battery_discharge_kwh`` is priced by nothing —
    but it is what the system carried through the expensive hours, and valuing
    it at the band's own rate is the only way to say what those hours would
    otherwise have cost.

    Three durations, and they answer three different questions. ``elapsed`` is
    how long the period was. ``measured`` is how much of it anybody watched,
    which is what a coverage figure is drawn from, so a total shortened by an
    outage can be shown as short rather than as a quiet month. ``counted`` is
    the span the energy totals actually account for — first reading to last —
    and it is the only one a projection may divide by. A counter delta spans an
    outage by design, so six kilowatt-hours read across a four-hour gap belong
    to six hours and not to the two the collector was awake for; dividing by
    ``measured`` there projected a month at three times the real rate.
    """

    start: datetime
    end: datetime
    grid_import_kwh: Mapping[str, float | None]
    load_kwh: Mapping[str, float | None] | None = None
    grid_export_kwh: float | None = None
    battery_discharge_kwh: Mapping[str, float | None] | None = None
    measured_minutes: float | None = None
    elapsed_minutes: float | None = None
    counted_minutes: float | None = None
    # Per-counter accounting of what the split could not place, keyed
    # "grid_import", "load", "grid_export", "battery_discharge". None means
    # nobody accounted for it, and None keeps the old rule: an unknown band
    # poisons its total, because a partial figure whose shortfall nobody
    # computed cannot say what it covers. Whoever fills the bands from real
    # readings fills this beside them; see costs.period_energy.
    shortfall: Mapping[str, EnergyShortfall] | None = None

    def __post_init__(self) -> None:
        """Reject naive bounds and a period that runs backwards.

        A naive bound cannot be placed against wall-clock bands or a local
        month. A backwards period would apportion a negative fixed charge, which
        would show up as a refund nobody is getting.
        """
        _require_aware(self.start, "period start")
        _require_aware(self.end, "period end")
        if _elapsed(self.start, self.end) < 0:
            raise ValueError("a period must not end before it starts")


@dataclass(frozen=True)
class BandCost:
    """What one band contributed, for a breakdown a person can check against a bill.

    ``kwh`` and ``cost`` are None together when the band reported nothing. The
    cost is rounded for display; the total it feeds is not built from these.
    """

    name: str
    price_per_kwh: float
    kwh: float | None
    cost: float | None


@dataclass(frozen=True)
class CostResult:
    """What a period cost, what it would have cost without the system, and the gap.

    ``savings`` deliberately excludes ``fixed_charge``. The connection charge is
    unavoidable, so counting it against the array would make the solar look
    worse the more it saved. ``no_solar_cost`` is the counterfactual — the whole
    house load priced at the same tariff — and is the figure the savings ribbon
    of the energy-flow diagram is drawn from.

    A money field whose inputs were not all reported is None rather than zero
    when nothing accounts for the difference, so a caption with a hole in it
    does not appear at all. Under shortfall accounting it is instead the
    measured part with its ``*_is_short`` flag raised — the labelled partial
    the owner chose over a dash in #23.

    Each field is rounded once, from unrounded inputs, which is why a breakdown
    can be a cent away from adding up to ``cost``. That is the right way round:
    pricing the total is exact and summing rounded parts drifts from it.

    ``adjustment`` is the PCRF and SCRF riders in money, and it is a component
    of ``cost`` in the same way ``fixed_charge`` is: ``cost`` is
    ``energy_cost + fixed_charge + adjustment``. It can be negative, since PCRF
    can be. ``no_solar_cost`` carries the same riders on the whole house load —
    without the system every one of those kilowatt-hours would have been
    metered and charged them — so ``savings`` remains ``no_solar_cost`` less
    the energy and the rider actually paid, and leaving them out of the
    counterfactual would understate what the array saved.

    ``adjustment_status`` says whether the riders were charged, are not charged
    at all, or are simply not recorded for this month; see ``AdjustmentRate``.
    In the last case every figure here is at the base rate and the caller has
    to say so rather than let it read as a finished bill.

    The ``*_is_short`` flags say which figures must not be read as whole,
    decided here from the period's shortfall accounting because which counter
    feeds which figure is pricing knowledge: ``cost`` rides on grid import,
    ``no_solar_cost`` on house load, ``savings`` on both, ``export_credit``
    on export. A page draws its qualification from these and its wording from
    the shortfall itself — a known number of missing kilowatt-hours reads
    differently from a measurement that may not be exact.
    """

    currency: str
    start: datetime
    end: datetime
    bands: tuple[BandCost, ...]
    energy_cost: float | None
    fixed_charge: float
    cost: float | None
    export_credit: float | None
    no_solar_cost: float | None
    savings: float | None
    adjustment_status: AdjustmentStatus = "none"
    pcrf_per_kwh: float | None = None
    scrf_per_kwh: float | None = None
    adjustment: float | None = None
    cost_is_short: bool = False
    no_solar_is_short: bool = False
    savings_is_short: bool = False
    export_is_short: bool = False


@dataclass(frozen=True)
class BillEstimate:
    """This month's bill, extrapolated from what the month has done so far.

    It is an estimate and says so: ``is_partial`` is true until the month is
    over, and ``fraction_elapsed`` is how much of it has passed. The projection
    assumes the rest of the month looks like the part already measured, which is
    wrong in a heatwave and wrong in a cloudy fortnight — hence a figure that is
    labelled rather than one that is quietly presented as the bill.

    ``fixed_charge`` is the whole monthly charge, added once, never apportioned:
    the bill being estimated is a whole month's.

    ``projected_adjustment`` is the PCRF and SCRF riders scaled to the whole
    month exactly as the energy cost is, since they are charged per kilowatt-
    hour and the kilowatt-hours are what is being projected. Where the month's
    factors are not recorded it is absent and ``adjustment_status`` says
    ``unknown``, which is the difference between a bill quoted at the base rate
    and a bill quoted as final.

    ``is_short`` says the projection's inputs were not whole — energy the
    meter counted never reached a band, or a loss whose size nobody can
    state. ``assumed_kwh`` is the import energy the projection restored at the
    blended rate of the energy it did price; None when there was nothing to
    restore or no observed blend to restore it at, in which case the flag
    alone carries the warning. The bill is the figure most sensitive to
    coverage and was the only money figure without a coverage statement; its
    projection was also the figure the second reverted attempt understated by
    $23.54 through a minutes-based denominator, which is finding 1 of #23.

    ``is_projected`` is a different question from ``fraction_elapsed`` reaching
    1.0, and the page that read them as the same thing lied: ``fraction_elapsed``
    is 1.0 for any month the calendar has finished, whether or not the collector
    was recording for all of it, while ``is_projected`` is false only once the
    counted span itself reaches the whole month — a collector that started on
    the tenth still scales its estimate up on the thirty-first.
    """

    currency: str
    month_start: datetime
    month_end: datetime
    fraction_elapsed: float
    is_partial: bool
    energy_cost_so_far: float | None
    projected_energy_cost: float | None
    projected_export_credit: float | None
    fixed_charge: float
    estimated_total: float | None
    season_changes: bool = False
    adjustment_status: AdjustmentStatus = "none"
    projected_adjustment: float | None = None
    is_short: bool = False
    assumed_kwh: float | None = None
    is_projected: bool = False


def _parse_clock(token: str, entry: str) -> time:
    """Read one end of a time range, accepting ``16``, ``16:00`` and ``24:00``.

    Midnight at the far end is written 24:00 by most tariffs and 00:00 by the
    clock, so both are accepted and both mean the same instant.
    """
    text = token.strip()
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text) if minute_text else 0
    except ValueError as exc:
        raise ValueError(f"{entry!r}: {text!r} is not a time like 16:00") from exc
    if (hour, minute) == (24, 0):
        return time(0, 0)
    if not 0 <= hour < 24 or not 0 <= minute < 60:
        raise ValueError(f"{entry!r}: {text!r} is not a time of day")
    return time(hour, minute)


def _ends_the_day(token: str) -> bool:
    """Whether this end-of-range token is the midnight that closes a day.

    Distinguishes 00:00-24:00, which is a band applying all day, from
    16:00-16:00, which is a person having written something they did not mean.
    """
    return token.strip().partition(":")[0].strip() == "24"


def _parse_hours(text: str, entry: str) -> tuple[TimeRange, ...]:
    """Read the comma-separated time ranges a band applies to."""
    ranges: list[TimeRange] = []
    for chunk in text.split(","):
        span = chunk.strip()
        if not span:
            continue
        start_text, sep, end_text = span.partition("-")
        if not sep:
            raise ValueError(f"{entry!r}: {span!r} is not a range like 16:00-21:00")
        start = _parse_clock(start_text, entry)
        end = _parse_clock(end_text, entry)
        if start == end and not _ends_the_day(end_text):
            raise ValueError(
                f"{entry!r}: {span!r} starts and ends at the same time. "
                "Write 00:00-24:00 for a band that applies all day."
            )
        ranges.append(TimeRange(start=start, end=end))
    if not ranges:
        raise ValueError(f"{entry!r}: a band needs at least one time range")
    return tuple(ranges)


_MONTH_NAMES = {
    m: n
    for n, names in enumerate(
        [
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ],
        start=1,
    )
    for m in names
}


def _parse_months(text: str, entry: str = "") -> frozenset[int] | None:
    """Read the season a band applies to, as month names or a range of them.

    Accepts "May-Oct", "Nov-Apr" — which wraps the year end and must, since a
    winter season always does — or a comma-separated list. Empty means every
    month, so a tariff with no season needs no extra field.

    Raises:
        ValueError: a name is not a month, with the offending text quoted.
    """
    text = text.strip()
    if not text:
        return None
    months: set[int] = set()
    for piece in text.split(","):
        piece = piece.strip().casefold()
        if not piece:
            continue
        if "-" in piece:
            first, last = (x.strip() for x in piece.split("-", 1))
            if first not in _MONTH_NAMES or last not in _MONTH_NAMES:
                raise ValueError(f"{entry or text!r}: {piece!r} is not a range of months")
            a, b = _MONTH_NAMES[first], _MONTH_NAMES[last]
            # A season may wrap the year end; November to April is the common case.
            months.update(range(a, b + 1) if a <= b else list(range(a, 13)) + list(range(1, b + 1)))
        else:
            if piece not in _MONTH_NAMES:
                raise ValueError(f"{entry or text!r}: {piece!r} is not a month")
            months.add(_MONTH_NAMES[piece])
    return frozenset(months) or None


def parse_bands(text: str) -> tuple[RateBand, ...]:
    """Read the rate bands a person typed into the settings page.

    The format is one band per entry, entries separated by a semicolon or a
    newline, fields separated by a pipe::

        Peak | 0.34 | 16:00-21:00; Off-peak | 0.11 | 21:00-16:00

    A band may list several ranges, comma separated, and a range may run through
    midnight. Bands are kept in the order written and the first match wins.

    Everything here refuses rather than guesses. A price that will not parse, a
    range that is not a range, two bands sharing a name — each raises, because a
    half-understood tariff prices a bill wrongly and says nothing about it,
    while one that was refused gets fixed.

    Raises:
        ValueError: the text is not a tariff, with the offending entry quoted.
    """
    bands: list[RateBand] = []
    seen: dict[str, str] = {}
    # Semicolons and newlines both separate bands, so a single-line form field
    # and a multi-line textarea each work without the owner being told which.
    for raw in text.replace("\n", ";").split(";"):
        entry = raw.strip()
        if not entry:
            continue
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) not in (3, 4):
            raise ValueError(
                f"{entry!r}: a band is 'name | price | hours' with an optional "
                "'| months', separated by |"
            )
        name, price_text, hours_text = parts[:3]
        months = _parse_months(parts[3], entry) if len(parts) == 4 else None
        if not name:
            raise ValueError(f"{entry!r}: a band needs a name; energy is reported against it")
        try:
            price = float(price_text)
        except ValueError as exc:
            raise ValueError(f"{entry!r}: {price_text!r} is not a price per kWh") from exc
        if not math.isfinite(price) or price < 0:
            raise ValueError(f"{entry!r}: a price per kWh cannot be {price_text!r}")
        band = RateBand(
            name=name,
            price_per_kwh=price,
            hours=_parse_hours(hours_text, entry),
            months=months,
        )
        if band.key in seen:
            raise ValueError(
                f"two rate bands are both called {name!r} (already have {seen[band.key]!r}); "
                "energy is reported per band by name, so the names have to differ"
            )
        seen[band.key] = name
        bands.append(band)
    if not bands:
        raise ValueError("no rate bands in that text")
    return tuple(bands)


def _parse_billing_month(token: str, entry: str) -> tuple[int, int]:
    """Read the ``YYYY-MM`` a line of factors belongs to."""
    year_text, sep, month_text = token.strip().partition("-")
    try:
        year, month = int(year_text), int(month_text)
    except ValueError as exc:
        raise ValueError(f"{entry!r}: {token.strip()!r} is not a month like 2026-07") from exc
    if not sep or not 1 <= month <= 12 or year < 1:
        raise ValueError(f"{entry!r}: {token.strip()!r} is not a month like 2026-07")
    return year, month


def _parse_factor(token: str, entry: str, what: str) -> float | None:
    """Read one rider, or None where the field was left empty.

    Empty means the supplier has not published that factor for the month.
    Reading it as nought would be a measurement nobody took, and it is the
    measurement that decides whether the bill is adjusted at all.
    """
    text = token.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise ValueError(f"{entry!r}: {text!r} is not a {what} per kWh") from exc
    if not math.isfinite(value):
        raise ValueError(f"{entry!r}: a {what} per kWh cannot be {text!r}")
    return value


def parse_adjustments(text: str) -> tuple[MonthlyAdjustment, ...]:
    """Read the per-month PCRF and SCRF factors a person typed into settings.

    One month per entry, entries separated by a semicolon or a newline, fields
    separated by a pipe::

        2026-07 | -0.001230 | 0.004560
        2026-08 |  0.002100 | 0.004560

    The fields are the billing month, the PCRF and the SCRF, in that order.
    Either factor may be negative — PCRF regularly is — and either may be left
    empty for a month the supplier has only published one of.

    Refuses rather than guesses, exactly as ``parse_bands`` does: a month that
    is not a month, a factor that is not a number, or the same month written
    twice each raise. Entries come back in calendar order however they were
    typed, so a list appended to over years still reads as one.

    Raises:
        ValueError: the text is not a table of factors, with the entry quoted.
    """
    entries: dict[tuple[int, int], MonthlyAdjustment] = {}
    for raw in text.replace("\n", ";").split(";"):
        entry = raw.strip()
        if not entry:
            continue
        parts = [part.strip() for part in entry.split("|")]
        if len(parts) != 3:
            raise ValueError(
                f"{entry!r}: a line is 'YYYY-MM | PCRF | SCRF', separated by |. "
                "Leave a factor empty if the supplier has not published it."
            )
        year, month = _parse_billing_month(parts[0], entry)
        if (year, month) in entries:
            raise ValueError(
                f"{entry!r}: {year:04d}-{month:02d} already has factors; "
                "one line per billing month, or the bill depends on which is read first"
            )
        entries[year, month] = MonthlyAdjustment(
            year=year,
            month=month,
            pcrf_per_kwh=_parse_factor(parts[1], entry, "PCRF"),
            scrf_per_kwh=_parse_factor(parts[2], entry, "SCRF"),
        )
    if not entries:
        raise ValueError("no monthly adjustments in that text")
    return tuple(entries[key] for key in sorted(entries))


def _as_float(value: object, key: str, fallback: float) -> float:
    """Read a number out of a stored setting, falling back rather than failing.

    A setting that has gone unreadable is worth a line in the log and a return
    to the default. Refusing to price anything because the export field picked
    up a stray character helps nobody, and the fallback here is zero rather than
    a rate, so nothing is invented by taking it.
    """
    if isinstance(value, bool) or value is None:
        return fallback
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except ValueError:
        logger.warning("setting %s holds %r, which is not a number; using %s", key, value, fallback)
        return fallback


def _load_adjustments(
    values: Mapping[str, object],
) -> tuple[tuple[MonthlyAdjustment, ...], bool]:
    """Read the monthly riders, saying whether the stored text was readable.

    Text that will not parse gives no factors *and* the flag, because the two
    have to stay distinguishable: an empty table means the supplier charges no
    rider, and quietly returning one for a value that merely failed to read
    would turn a corrupted setting into a confident claim that a bill needs no
    adjustment. A bad tariff nulls all money; a bad rider only makes the rider
    unknown, so this survives rather than refusing.
    """
    text = str(values.get(SETTING_ADJUSTMENTS) or "")
    if not text.strip():
        return (), False
    try:
        return parse_adjustments(text), False
    except ValueError as exc:
        logger.error(
            "monthly adjustments not understood, so no bill will be adjusted "
            "and none will be reported as unadjusted: %s",
            exc,
        )
        return (), True


def load_tariff(values: Mapping[str, object]) -> Tariff | None:
    """Build a tariff from stored settings, or None if none has been entered.

    None is the important return. An install with no tariff shows energy and no
    money at all — not zero, and not a guessed national average — so every
    caller of this gets an answer it cannot mistake for a priced one.

    A tariff that will not parse is also None, and loudly logged. The settings
    page cannot validate the band text through the registry's scalar types, so
    an unreadable one has to be survivable; pricing a bill against a
    half-understood tariff would be worse than pricing nothing.
    """
    text = str(values.get(SETTING_BANDS) or "")
    if not text.strip():
        return None
    try:
        bands = parse_bands(text)
    except ValueError as exc:
        logger.error("tariff not understood, so no money will be shown: %s", exc)
        return None

    export = _as_float(values.get(SETTING_EXPORT_PER_KWH), SETTING_EXPORT_PER_KWH, 0.0)
    adjustments, unreadable = _load_adjustments(values)
    tariff = Tariff(
        bands=bands,
        fixed_monthly=_as_float(values.get(SETTING_FIXED_MONTHLY), SETTING_FIXED_MONTHLY, 0.0),
        currency=str(values.get(SETTING_CURRENCY) or DEFAULT_CURRENCY),
        export_per_kwh=export if export > 0 else None,
        adjustments=adjustments,
        adjustments_unreadable=unreadable,
    )
    uncovered = tariff.uncovered_minutes()
    if uncovered:
        logger.warning(
            "the rate bands leave %d minutes of the day unpriced; energy used then "
            "belongs to no band and will be missing from the cost",
            uncovered,
        )
    return tariff


def apportion_fixed(monthly: float, start: datetime, end: datetime) -> float:
    """Share a monthly connection charge across the part of each month a period covers.

    A fortnight of January carries fifteen thirty-firsts of the charge, and a
    period crossing a month boundary carries a slice of each month measured
    against that month's own length. The lengths are real elapsed time, so the
    month a clock change lengthens is 721 hours rather than 720 — dividing by
    86400 instead is out by an hour twice a year, which reads as a data bug
    rather than the rounding it is.

    The result is unrounded, because it is one input to a total that gets
    rounded once.
    """
    _require_aware(start, "period start")
    _require_aware(end, "period end")
    if monthly == 0 or _elapsed(start, end) <= 0:
        return 0.0

    total = 0.0
    cursor = _month_start(start)
    while _elapsed(cursor, end) > 0:
        following = _next_month_start(cursor)
        overlap = _elapsed(max(start, cursor), min(end, following))
        if overlap > 0:
            total += monthly * overlap / _elapsed(cursor, following)
        cursor = following
    return total


def counter_delta(first: float | None, last: float | None) -> float | None:
    """Return the energy between two readings of the same lifetime counter.

    Lifetime counters are monotonic, so the energy over any period is simply the
    end minus the start — right even if the collector was down for half of it,
    which is exactly why energy here never comes from integrating power.

    Two cases give no answer rather than a wrong one. A reading that was never
    taken is unknown. And a counter that has gone *backwards* has been reset,
    which firmware updates do: how much it had accumulated before the reset is
    unknowable, so the period is unknown. Subtracting anyway would produce a
    large negative that reads as a refund and poisons every total it reaches.
    """
    if first is None or last is None:
        return None
    if last < first:
        logger.warning(
            "energy counter went backwards, %s to %s; treating the period as unknown "
            "rather than negative (a firmware update resets these)",
            first,
            last,
        )
        return None
    return last - first


def merge_shortfalls(spans: Sequence[PeriodEnergy]) -> Mapping[str, EnergyShortfall] | None:
    """Combine a run of buckets' accounting into one period's.

    Kilowatt-hours add and unknowability spreads: a counter unknowable in one
    bucket is unknowable over any run containing it. The answer is None the
    moment any span carries no accounting at all — a total over buckets one
    of which nobody accounted for cannot claim to know what it is missing,
    and None is what makes the pricing below fall back to poisoning rather
    than showing that total as a labelled partial.

    This exists for the History footer, which prices thirty-one days as one
    period; without the merge the footer would revert to dashing under a
    column of flagged numbers, and no clean-data test would ever notice.
    """
    collected = [span.shortfall for span in spans]
    if not collected or any(mapping is None for mapping in collected):
        return None
    present: list[Mapping[str, EnergyShortfall]] = [m for m in collected if m is not None]
    out: dict[str, EnergyShortfall] = {}
    for key in {name for mapping in present for name in mapping}:
        entries = [mapping.get(key) for mapping in present]
        out[key] = EnergyShortfall(
            attributed_kwh=sum(e.attributed_kwh for e in entries if e is not None),
            unattributed_kwh=sum(e.unattributed_kwh for e in entries if e is not None),
            # A counter one bucket never accounted for is not a clean merge.
            unknowable=any(e is None or e.unknowable for e in entries),
            # Candidate bands union: a band short in any one bucket is short over
            # the run containing it, the same way unknowability spreads. Dropping
            # them here would hand the History footer an empty set, so a month
            # holding an outage would flag its totals and price every band row as
            # though every window had been measured.
            bands_possibly_short=frozenset().union(
                *(e.bands_possibly_short for e in entries if e is not None)
            ),
        )
    return out


def _by_band(
    values: Mapping[str, float | None] | None, bands: Sequence[RateBand], what: str
) -> dict[str, float] | None:
    """Key reported energy by band, dropping anything the tariff has no band for.

    An unrecognised name is logged and skipped rather than raised on: a band
    renamed on the settings page must not take the page down until the next
    boundary sample catches up. Nothing is lost quietly by doing so — the band
    the energy should have landed in is then missing, and missing means the
    whole figure comes back absent.
    """
    if values is None:
        return None
    known = {band.key for band in bands}
    out: dict[str, float] = {}
    for name, kwh in values.items():
        key = name.strip().casefold()
        if key not in known:
            logger.warning("%s reported for %r, which is not a band in this tariff", what, name)
            continue
        if kwh is not None:
            out[key] = float(kwh)
    return out


def _price(
    bands: Sequence[RateBand], reported: Mapping[str, float] | None, partial: bool = False
) -> tuple[list[BandCost], float | None, float | None]:
    """Price every band: the breakdown, the exact total, and the energy behind it.

    Without ``partial``, the two totals are None together as soon as one band
    is unknown — a total that quietly leaves a band out is a missing reading
    rendered as a smaller number. With it, the totals sum the bands that
    reported and are None only when none did: the caller has shortfall
    accounting saying exactly what the sum is missing, so the figure can be
    shown with a label instead of withheld (#23). The breakdown carries None
    for an unknown band either way.

    Both totals are unrounded — the caller rounds once, at the end. The
    kilowatt-hour total comes back from here rather than being summed again by
    whoever needs it, because the rule for when it is unknown is the same
    rule, and a second copy of it would be a second place for a missing band
    to turn into a small number. It is what the per-kWh riders are charged on.
    """
    breakdown: list[BandCost] = []
    total: float | None = 0.0
    energy: float | None = 0.0
    known = 0
    for band in bands:
        kwh = None if reported is None else reported.get(band.key)
        cost = None if kwh is None else kwh * band.price_per_kwh
        breakdown.append(
            BandCost(
                name=band.name,
                price_per_kwh=band.price_per_kwh,
                kwh=kwh,
                cost=None if cost is None else _money(cost),
            )
        )
        if cost is None or kwh is None:
            if not partial:
                total, energy = None, None
        elif total is not None and energy is not None:
            total += cost
            energy += kwh
            known += 1
    if partial and known == 0:
        return breakdown, None, None
    return breakdown, total, energy


def compute_cost(
    tariff: Tariff | None,
    energy: PeriodEnergy,
    fixed_charge: float | None = None,
) -> CostResult | None:
    """Price a period: what the grid cost, what it saved, and what export earned.

    Returns None when there is no tariff, which is how an install that has never
    entered one shows energy and no money rather than a confident zero.

    Savings is the counterfactual — the whole house load priced at the same
    tariff, as it would have arrived with no solar and no battery — less what
    the grid actually cost. The fixed charge appears in ``cost`` and never in
    ``savings``. Nor is the export credit folded into either: it is money the
    tariff pays rather than money the array avoided spending, and adding it to a
    saving that already counts the same sunlight would count it twice.

    A negative saving is reported as it stands. Importing more than the house
    used means the bank was charged from the grid, and if that happened at the
    peak rate it genuinely cost money — clamping it to zero would hide a tariff
    setting that is losing the owner money every night.

    The PCRF and SCRF riders are charged on every metered kilowatt-hour, so
    they ride on both sides: on what was imported, and on the whole house load
    the counterfactual would have imported. A month with no factors recorded is
    priced at the base rate and labelled, never at a rider of zero.
    """
    if tariff is None:
        return None

    # Only the bands the period could actually have entered. A band out of
    # season has nothing to report and reporting nothing is not the same as
    # failing to measure it.
    active = tariff.bands_in_effect(energy.start, energy.end) or tariff.bands

    # Partial pricing is authorised per counter, never by the mapping merely
    # existing: an accounted import must not license a partial house figure
    # whose own shortfall nobody computed.
    entries = energy.shortfall or {}
    import_entry = entries.get("grid_import")
    load_entry = entries.get("load")
    export_entry = entries.get("grid_export")

    imported = _by_band(energy.grid_import_kwh, active, "grid import")
    breakdown, energy_cost, imported_kwh = _price(
        active, imported, partial=import_entry is not None
    )

    consumed = _by_band(energy.load_kwh, active, "house load")
    _, no_solar, consumed_kwh = _price(active, consumed, partial=load_entry is not None)

    rider = tariff.adjustment_at(energy.start, energy.end)
    unknown_rider = rider.status == "unknown"
    adjustment = (
        None if rider.per_kwh is None or imported_kwh is None else imported_kwh * rider.per_kwh
    )
    if no_solar is not None and rider.per_kwh is not None and consumed_kwh is not None:
        no_solar += consumed_kwh * rider.per_kwh
    # An unknown rider poisons the total rather than counting as nothing. The
    # two are not unknown together: energy_cost comes from the bands and the
    # rider from a month's published factors, so a period can price perfectly
    # while nobody has entered the factors it needs. Treating that as zero
    # states a bill the supplier will not send, and states it confidently —
    # which is the one thing a money figure here must never do.
    billed = None if energy_cost is None or unknown_rider else (energy_cost + (adjustment or 0.0))

    # Apportioned by default, because most callers are pricing a bucket: a day
    # inside a month owes a day's share of the connection charge, and charging
    # each of thirty-one days the whole of it would be absurd.
    #
    # A caller pricing a *billing month* passes the charge it wants instead. The
    # charge falls due once for the month whatever day it is read on, so a
    # month-to-date bill that shows three dollars of fifteen is describing an
    # instalment nobody is billed — the fifteen is owed, and showing part of it
    # understates what the month will cost.
    fixed = (
        apportion_fixed(tariff.fixed_monthly, energy.start, energy.end)
        if fixed_charge is None
        else fixed_charge
    )

    credit: float | None = None
    if tariff.export_per_kwh is not None and energy.grid_export_kwh is not None:
        credit = _money(energy.grid_export_kwh * tariff.export_per_kwh)

    cost_is_short = import_entry is not None and import_entry.short
    no_solar_is_short = load_entry is not None and load_entry.short
    return CostResult(
        currency=tariff.currency,
        start=energy.start,
        end=energy.end,
        bands=tuple(breakdown),
        energy_cost=None if energy_cost is None else _money(energy_cost),
        fixed_charge=_money(fixed),
        cost=None if billed is None else _money(billed + fixed),
        export_credit=credit,
        no_solar_cost=None if no_solar is None else _money(no_solar),
        savings=None if no_solar is None or billed is None else _money(no_solar - billed),
        adjustment_status=rider.status,
        pcrf_per_kwh=rider.pcrf_per_kwh,
        scrf_per_kwh=rider.scrf_per_kwh,
        adjustment=None if adjustment is None else _money(adjustment),
        cost_is_short=cost_is_short,
        no_solar_is_short=no_solar_is_short,
        savings_is_short=cost_is_short or no_solar_is_short,
        export_is_short=export_entry is not None and export_entry.short,
    )


def estimate_bill(tariff: Tariff | None, energy: PeriodEnergy) -> BillEstimate | None:
    """Project a month's bill from the part of the month already measured.

    ``energy`` is expected to cover the month so far. The projection scales what
    it cost by the month's length over the period's, so a collector that only
    started on the tenth still extrapolates from the rate it actually saw rather
    than pretending the first nine days were free. The scale never goes below
    one: a period that already covers the whole month is reported as it is
    rather than shrunk to fit.

    The fixed charge is added once, whole. The export credit, where the tariff
    pays one, is netted off — a bill that ignores money the supplier pays back
    is not the bill that arrives.

    ``season_changes`` warns that the projection's own premise fails: the rest
    of the month is priced by a different set of bands than the part measured,
    so scaling one to the other is arithmetic on two different tariffs.
    """
    if tariff is None:
        return None

    month_start = _month_start(energy.start)
    month_end = _next_month_start(month_start)
    month_seconds = _elapsed(month_start, month_end)
    covered = _elapsed(energy.start, energy.end)

    fraction = min(max(_elapsed(month_start, energy.end) / month_seconds, 0.0), 1.0)
    # Only the bands the measured period could have entered — the same set
    # compute_cost prices. Pricing all of them instead makes an out-of-season
    # band permanently unmeasured, which makes the total permanently absent:
    # on the reference installation's own seasonal tariff this endpoint could
    # never once produce an estimated bill.
    active = tariff.bands_in_effect(energy.start, energy.end) or tariff.bands
    entries = energy.shortfall or {}
    import_entry = entries.get("grid_import")
    export_entry = entries.get("grid_export")
    imported = _by_band(energy.grid_import_kwh, active, "grid import")
    _, so_far, imported_kwh = _price(active, imported, partial=import_entry is not None)
    whole_month = tariff.bands_in_effect(month_start, month_end) or tariff.bands

    # Scaled by the span the counters account for, not by the span requested
    # and not by the time observed. A collector that ran for twelve hours of a
    # day asked for in full measured half a day's energy, and dividing that by
    # the whole day projects a month at half the real rate; dividing it instead
    # by the minutes anybody watched overshoots the other way, because a
    # counter kept counting through every gap between those minutes.
    accounted = covered if energy.counted_minutes is None else energy.counted_minutes * 60
    scale = max(month_seconds / accounted, 1.0) if accounted > 0 else None
    # Whether the total below is a genuine projection or the month priced as
    # it stands. ``fraction_elapsed`` answers a different question — it is 1.0
    # for any finished calendar month regardless of what was actually
    # recorded in it — so an installation whose collection began mid-month
    # still has ``scale`` above 1.0 once the month is over: ``estimated_total``
    # is still being scaled up from the part that was measured, and calling
    # that "what it came to rather than a projection" would be false. Only a
    # ``scale`` of exactly 1.0 means the counted span already reaches the
    # whole month, which needs both the month to be over and the collector to
    # have covered all of it.
    is_projected = scale is not None and scale > 1.0

    # Then by the energy the bands account for, which is a different question
    # from the time: a counter can sit silent for a day while polls keep
    # arriving, and the time scale reads that day as covered while its energy
    # is in no band. The known-missing kilowatt-hours are restored at the
    # blended rate of the energy that was priced — the projection's own
    # premise, one step further — and only when there is a blend to use:
    # priced energy of zero has nothing to price the missing share at, and
    # dividing by it is how a solar fortnight becomes an error page.
    correction = 1.0
    assumed: float | None = None
    if (
        import_entry is not None
        and import_entry.unattributed_kwh > 0
        and imported_kwh is not None
        and imported_kwh > 0
    ):
        correction = (imported_kwh + import_entry.unattributed_kwh) / imported_kwh
        assumed = import_entry.unattributed_kwh

    projected = None if so_far is None or scale is None else so_far * scale * correction

    pays_for_export = tariff.export_per_kwh is not None
    # The credit and the riders are flat per-kilowatt-hour, so their missing
    # energy needs no blend at all — the kilowatt-hours are known and every
    # one of them will be metered. Restoring them is arithmetic, not
    # assumption, which is why neither shares the correction's guard.
    export_kwh = energy.grid_export_kwh
    if export_entry is not None and export_entry.unattributed_kwh > 0:
        export_kwh = (export_kwh or 0.0) + export_entry.unattributed_kwh
    credit: float | None = None
    if pays_for_export and export_kwh is not None and scale is not None:
        credit = export_kwh * tariff.export_per_kwh * scale  # type: ignore[operator]

    # The month being estimated, not the part measured: a bill covers the whole
    # month, so an August estimate wants August's factors even on the first.
    rider = tariff.adjustment_at(month_start, month_end)
    unknown_rider = rider.status == "unknown"
    rider_kwh = imported_kwh
    if import_entry is not None and import_entry.unattributed_kwh > 0:
        rider_kwh = (rider_kwh or 0.0) + import_entry.unattributed_kwh
    adjustment = (
        None
        if rider.per_kwh is None or rider_kwh is None or scale is None
        else rider_kwh * rider.per_kwh * scale
    )

    total: float | None = None
    # A tariff that pays for export and an export figure nobody has is not a
    # bill of zero credit — it is a bill that cannot be told. Netting off
    # nothing there would quote a total higher than the one that arrives, and
    # doing it silently is worse than not quoting one.
    # Same rule as the period total: an unknown rider cannot be spent as zero.
    # The estimate is the figure an owner plans against, so quoting one that
    # silently omits a rider is worse than quoting none.
    if projected is not None and not (pays_for_export and credit is None) and not unknown_rider:
        total = _money(projected + (adjustment or 0.0) + tariff.fixed_monthly - (credit or 0.0))

    return BillEstimate(
        currency=tariff.currency,
        month_start=month_start,
        month_end=month_end,
        fraction_elapsed=fraction,
        is_partial=fraction < 1.0,
        energy_cost_so_far=None if so_far is None else _money(so_far),
        projected_energy_cost=None if projected is None else _money(projected),
        projected_export_credit=None if credit is None else _money(credit),
        fixed_charge=_money(tariff.fixed_monthly),
        estimated_total=total,
        season_changes={b.key for b in whole_month} != {b.key for b in active},
        adjustment_status=rider.status,
        projected_adjustment=None if adjustment is None else _money(adjustment),
        is_short=(import_entry is not None and import_entry.short)
        or (pays_for_export and export_entry is not None and export_entry.short),
        assumed_kwh=assumed,
        is_projected=is_projected,
    )
