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
"""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

logger = logging.getLogger(__name__)

SETTING_BANDS = "tariff.bands"
SETTING_FIXED_MONTHLY = "tariff.fixed_monthly"
SETTING_CURRENCY = "tariff.currency"
SETTING_EXPORT_PER_KWH = "tariff.export_per_kwh"

# Shown in the settings help as the format's only documentation, and parsed by a
# test so the example can never drift into one the parser would refuse.
EXAMPLE_BANDS = "Peak | 0.34 | 16:00-21:00; Off-peak | 0.11 | 21:00-16:00"

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
        if end <= start:
            return ()
        months: set[int] = set()
        moment = start
        while moment < end:
            months.add(moment.month)
            moment += timedelta(days=1)
        months.add(end.month)
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
class PeriodEnergy:
    """The energy a period used, already split into the tariff's bands.

    Time-of-use pricing needs energy per band, not per day: a daily counter
    cannot be split into peak and off-peak after the fact. Whoever fills this in
    reads the inverter's lifetime counters at each band boundary and differences
    them, which stays right across a collection gap in a way that integrating
    power does not.

    Both mappings are keyed by band name and their values are kilowatt-hours. A
    band that is missing, or present with None, is *unknown* rather than zero,
    and unknown propagates: a cost that cannot be told is reported as absent
    rather than as a total that quietly leaves a band out. ``load_kwh`` is the
    whole house, which is what the counterfactual bill is priced from;
    ``grid_export_kwh`` needs no band split until a tariff pays differently for
    export by hour.
    """

    start: datetime
    end: datetime
    grid_import_kwh: Mapping[str, float | None]
    load_kwh: Mapping[str, float | None] | None = None
    grid_export_kwh: float | None = None

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

    Every money field is None rather than zero when its inputs were not all
    reported, so a caption with a hole in it does not appear at all.

    Each field is rounded once, from unrounded inputs, which is why a breakdown
    can be a cent away from adding up to ``cost``. That is the right way round:
    pricing the total is exact and summing rounded parts drifts from it.
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
    tariff = Tariff(
        bands=bands,
        fixed_monthly=_as_float(values.get(SETTING_FIXED_MONTHLY), SETTING_FIXED_MONTHLY, 0.0),
        currency=str(values.get(SETTING_CURRENCY) or DEFAULT_CURRENCY),
        export_per_kwh=export if export > 0 else None,
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
    bands: Sequence[RateBand], reported: Mapping[str, float] | None
) -> tuple[list[BandCost], float | None]:
    """Price every band, returning the per-band breakdown and the exact total.

    The total is None as soon as one band is unknown, and it is unrounded: the
    caller rounds once, at the end. Returns the breakdown first and the total
    second — both are needed, and the breakdown is what a person checks.
    """
    breakdown: list[BandCost] = []
    total: float | None = 0.0
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
        if cost is None or total is None:
            total = None
        else:
            total += cost
    return breakdown, total


def compute_cost(tariff: Tariff | None, energy: PeriodEnergy) -> CostResult | None:
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
    """
    if tariff is None:
        return None

    # Only the bands the period could actually have entered. A band out of
    # season has nothing to report and reporting nothing is not the same as
    # failing to measure it.
    active = tariff.bands_in_effect(energy.start, energy.end) or tariff.bands

    imported = _by_band(energy.grid_import_kwh, active, "grid import")
    breakdown, energy_cost = _price(active, imported)

    consumed = _by_band(energy.load_kwh, active, "house load")
    _, no_solar = _price(active, consumed)

    fixed = apportion_fixed(tariff.fixed_monthly, energy.start, energy.end)

    credit: float | None = None
    if tariff.export_per_kwh is not None and energy.grid_export_kwh is not None:
        credit = _money(energy.grid_export_kwh * tariff.export_per_kwh)

    return CostResult(
        currency=tariff.currency,
        start=energy.start,
        end=energy.end,
        bands=tuple(breakdown),
        energy_cost=None if energy_cost is None else _money(energy_cost),
        fixed_charge=_money(fixed),
        cost=None if energy_cost is None else _money(energy_cost + fixed),
        export_credit=credit,
        no_solar_cost=None if no_solar is None else _money(no_solar),
        savings=None if no_solar is None or energy_cost is None else _money(no_solar - energy_cost),
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
    """
    if tariff is None:
        return None

    month_start = _month_start(energy.start)
    month_end = _next_month_start(month_start)
    month_seconds = _elapsed(month_start, month_end)
    covered = _elapsed(energy.start, energy.end)

    fraction = min(max(_elapsed(month_start, energy.end) / month_seconds, 0.0), 1.0)
    imported = _by_band(energy.grid_import_kwh, tariff.bands, "grid import")
    _, so_far = _price(tariff.bands, imported)

    scale = max(month_seconds / covered, 1.0) if covered > 0 else None
    projected = None if so_far is None or scale is None else so_far * scale

    credit: float | None = None
    if (
        tariff.export_per_kwh is not None
        and energy.grid_export_kwh is not None
        and scale is not None
    ):
        credit = energy.grid_export_kwh * tariff.export_per_kwh * scale

    total: float | None = None
    if projected is not None:
        total = _money(projected + tariff.fixed_monthly - (credit or 0.0))

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
    )
