"""Overnight battery planner: the estimation core, as arithmetic.

Nothing in here opens a store, a route or a page. Callers read the rows and the
settings and hand the values over, so every decision the projection makes lives
here where a test can pin it: which nights count as comparable, how a silent
step is read, what one step costs the battery, and when the answer is not a
number at all.

Two conventions shape the arithmetic.

The typical curve is a median of whole nights rather than an average of all the
history. One night with a pool pump running would drag an average up for every
plan that followed it, and the floor of the whole plan would move because of
something that happened once. A median of whole nights lets that night sit at
the edge of the spread instead of in the middle of every answer.

A gap in the recorded load carries the last answered step forward. A step that
did not answer is not evidence that the house drew nothing: the minute tier was
queried and nothing came back. Turning that into 0 W invents a quiet house out
of a gap in the record, and the projection then reports a battery that lasted
longer than anything was measured to last. Carrying the last answered step
forward is the conservative reading. It keeps a dishwasher's worth of draw alive
across a hole in the record instead of switching the house off, and it costs a
plan that is too short rather than a plan that is too long. The carry starts
from the whole curve, read cyclically: a projection whose window answers nothing
still carries the nearest answered step of the day, and only a curve that
answers nothing anywhere projects a flat line.

Times are walked as real instants and looked up by local clock time, so the
spring gap is walked over rather than through, and a fall-back hour is served
twice because two real hours pass inside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from math import isfinite
from statistics import median
from zoneinfo import ZoneInfo

STEP_SECONDS = 300
NIGHTS = 7
MIN_USABLE_NIGHTS = 3

# The reference install is a 48 V nominal bus, which is what turns amp-hours
# into the watt-hours a step can spend. The SoC scale counts from the reserve
# floor, so the usable window is the whole span from min_soc up to 100.
NOMINAL_BUS_V = 48.0

_STEP = timedelta(seconds=STEP_SECONDS)

# Anything smaller than this is float noise from the step arithmetic, not a
# watt the household drew.
_EPS = 1e-9


@dataclass(frozen=True)
class PlanResult:
    """One projected curve, the two times worth reading off it, and the band."""

    status: str = "ok"
    reason: str | None = None
    trajectory: tuple[tuple[datetime, float], ...] = ()
    # Filled only when the calibration reports a known drift magnitude: one entry
    # per step, that step's instant with the low and high edges of the band the
    # state of charge could really be anywhere inside.
    trajectory_band: tuple[tuple[datetime, float, float], ...] = ()
    reserve_crossing: datetime | None = None
    import_start: datetime | None = None
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanStatus:
    """Whether there is an answer, and what is standing in the way."""

    status: str = "ok"
    reason: str | None = None


@dataclass(frozen=True)
class PlanSummary:
    """The central estimate, the range around it, and the scenarios.

    A range whose second entry is ``None`` is open at the later end: something
    inside the spread of comparable nights never reached the reserve floor
    before the horizon did, so the honest statement is a start and no end.
    """

    central: PlanResult = field(default_factory=PlanResult)
    reserve_window: tuple[datetime, datetime | None] | None = None
    range_basis: str = ""
    scenarios: dict[str, PlanResult] = field(default_factory=dict)
    status: str = "ok"
    reason: str | None = None
    assumptions: tuple[str, ...] = ()


def _instant(when: datetime, zone: ZoneInfo) -> datetime:
    """One instant, so a naive row and its zone-aware twin sort as the same moment.

    A row with no tzinfo is read as wall clock in the installation's zone, which
    is how the minute tier hands it over.
    """
    if when.tzinfo is None:
        return when.replace(tzinfo=zone).astimezone(UTC)
    return when.astimezone(UTC)


def _clock_key(instant: datetime, zone: ZoneInfo) -> int:
    """Local minutes past midnight for an instant that has already been walked."""
    local = instant.astimezone(zone)
    return local.hour * 60 + local.minute


def _floor_step(instant: datetime) -> datetime:
    """The step boundary at or below an instant, on the shared five-minute grid.

    The grid is the same everywhere in the planner: a curve keyed by clock time
    and a schedule that starts mid-minute both land on it, so a projection that
    begins at 22:02 still reads the 22:00 step.
    """
    epoch = instant.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % STEP_SECONDS), UTC)


def _night_step_count(night: date, zone: ZoneInfo) -> int:
    """How many five-minute steps a night actually costs, walked not assumed.

    A night runs noon to noon, which is 288 steps in the ordinary case, 300
    across a fall-back and 276 across a spring-forward. The count is walked from
    real instants because the answer belongs to the date and not to arithmetic: a
    fixed denominator of 288 would call 145 answered steps of a 25-hour night a
    usable record, and that night is missing five and a half hours of itself.
    """
    previous = night - timedelta(days=1)
    # Both ends are turned into UTC first. Two datetimes that share one ZoneInfo
    # subtract as wall clock, which would make every night 288 steps long and
    # hide the two dates a year where a night is not.
    start = datetime(previous.year, previous.month, previous.day, 12, 0, tzinfo=zone).astimezone(
        UTC
    )
    finish = datetime(night.year, night.month, night.day, 12, 0, tzinfo=zone).astimezone(UTC)
    return round((finish - start).total_seconds() / STEP_SECONDS)


def night_curves(
    rows: Sequence[tuple[datetime, float | None]], zone: ZoneInfo
) -> list[dict[int, float]]:
    """One curve per usable night, keyed by local minute-of-day.

    A night is cut on the installation's calendar and runs noon to noon, so the
    evening draw and the following morning's belong to the same night, and a
    25 hour fall-back night stays one night instead of splitting into two thin
    halves. Rows are bucketed on the shared five-minute grid before any
    counting, so the two passes through a fall-back clock minute are two
    answers of one night, and a step read twice is one answer at its mean and
    not two.

    A night is usable when it answers at least half of its own steps, counted
    by the five-minute steps it answers. Below that the record is mostly gaps,
    and a median taken over mostly gaps is a guess about a house nobody
    watched. Fewer than MIN_USABLE_NIGHTS usable nights is not a typical night
    yet, so nothing is returned rather than a profile built on a single night.

    The window is the NIGHTS consecutive calendar nights ending with the most
    recent night that has any rows. It is a window of dates, not of usable
    nights: a silent night sits in the window and makes itself unusable rather
    than letting the plan reach back past it into older history, which is a
    different season and not more evidence. A night whose rows all read
    silence still holds its slot in the window: it is a real night that answers
    nothing, not a night that never happened.
    """
    # Each row's night is named before its values are filtered, so a recent
    # night of pure silence still anchors the window instead of opening a gap
    # to reach past. A bucket is one five-minute step: the step's floored
    # instant is its identity, and everything read inside it is one answer.
    nights: dict[date, dict[datetime, list[float]]] = {}
    for when, watts in sorted(rows, key=lambda row: _instant(row[0], zone)):
        instant = _instant(when, zone)
        clock = instant.astimezone(zone)
        day = clock.date()
        night = day if clock.hour < 12 else day + timedelta(days=1)
        slots = nights.setdefault(night, {})
        if watts is None:
            continue
        slots.setdefault(_floor_step(instant), []).append(float(watts))

    if not nights:
        return []

    last = max(nights)
    window = [last - timedelta(days=offset) for offset in range(NIGHTS - 1, -1, -1)]

    curves: list[dict[int, float]] = []
    for night in window:
        bucket = nights.get(night)
        if not bucket:
            continue
        if len(bucket) * 2 < _night_step_count(night, zone):
            continue
        passes: dict[int, list[float]] = {}
        for bucket_start in sorted(bucket):
            samples = bucket[bucket_start]
            # The bucket's own mean first: a fall-back bucket holding many
            # readings must not outweigh the other pass just by count.
            passes.setdefault(_clock_key(bucket_start, zone), []).append(
                sum(samples) / len(samples)
            )
        # A repeated clock minute is averaged, not taken twice and not taken once.
        # The clock reading stands for both of the real passes it names, and their
        # mean is the value that spends the same energy across the two of them;
        # keeping only the first would let a quiet hour look typical because of the
        # order the rows happened to arrive in.
        curve = {minute: sum(values) / len(values) for minute, values in passes.items()}
        curves.append(curve)

    if len(curves) < MIN_USABLE_NIGHTS:
        return []
    return curves


def median_curve(curves: list[dict[int, float]]) -> dict[int, float]:
    """Element-wise median across usable nights.

    A clock minute some nights did not answer is the median over the nights that
    did, and a minute none of them answered stays out of the curve. It is not
    filled with zero and it is not read as no load: ``simulate`` carries the last
    answered step forward over it, which is the same rule the nights are built by.
    """
    support: dict[int, list[float]] = {}
    for curve in curves:
        for minute, watts in curve.items():
            support.setdefault(minute, []).append(watts)
    return {minute: float(median(values)) for minute, values in support.items()}


def plan_status(
    severity: str | None,
    usable_ah: float | None,
    usable_nights: int,
    efficiency: float = 1.0,
    min_soc_pct: float = 0.0,
    horizon_s: float = float(STEP_SECONDS),
) -> PlanStatus:
    """Whether there is an answer to give, before any arithmetic.

    Only the top two rungs of the calibration ladder refuse. A warning widens
    what the plan is willing to claim, which is a fact about the range rather
    than a reason to say nothing; elevated and alert mean the state of charge
    itself is untrustworthy, and a projection built on a number we do not
    believe would only dress up the doubt as a forecast.

    Drift is checked before capacity. Both refuse, and the reason a calibration
    reads as drifted is the reason every figure in the answer is soft, so it is
    the one worth reading first. The last three rungs are the ones a projection
    would otherwise discover for itself: a round-trip efficiency that is not a
    positive number, a reserve floor at or above full charge, and a horizon too
    short to hold a step. They are here so a caller can ask this question once
    and not simulate four empty trajectories to learn it.
    """
    if severity in ("elevated", "alert"):
        return PlanStatus(
            "estimate_unavailable",
            f"Calibration reads {severity}: the state of charge is not trustworthy "
            "enough to project a night on top of.",
        )
    if usable_ah is None or usable_ah <= 0:
        return PlanStatus(
            "estimate_unavailable",
            "No usable battery capacity was reported: set the pack capacity and the "
            "reserve floor, and the planner will have something to drain.",
        )
    if usable_nights < MIN_USABLE_NIGHTS:
        return PlanStatus(
            "estimate_unavailable",
            f"Only {usable_nights} comparable night"
            f"{'s' if usable_nights != 1 else ''} of history answered, and a typical "
            f"night needs at least {MIN_USABLE_NIGHTS}.",
        )
    if efficiency <= 0:
        return PlanStatus(
            "estimate_unavailable",
            "The round-trip efficiency is not a positive number, so a watt-hour spent "
            "and a watt-hour drawn have no honest exchange rate.",
        )
    if min_soc_pct >= 100.0:
        return PlanStatus(
            "estimate_unavailable",
            "The reserve floor sits at or above full charge, so the usable battery "
            "window is empty and there is nothing for a night to drain.",
        )
    if horizon_s < STEP_SECONDS:
        return PlanStatus(
            "estimate_unavailable",
            "The horizon is shorter than one five-minute step, so there is nothing to "
            "project against.",
        )
    return PlanStatus()


# What every projection assumes, stated rather than hidden. These are read out
# to the owner next to the curve, so they name what a reader could otherwise take
# for measured fact: that a gap in the record is not a quiet house, that the grid
# was assumed there, and where the two battery figures came from.
_SIMULATION_ASSUMPTIONS = (
    "A gap in the recorded load carries the last answered step forward: it is not "
    "read as a silent house. Solar is read as it is written, and there a gap is "
    "no sun.",
    "The grid is assumed available: at the reserve floor the battery holds and the "
    "household starts importing, which is an import, not an outage.",
    "A 48 V nominal bus is assumed: amp-hours are spent as watt-hours at 48 Wh "
    "per amp-hour, and the reserve floor is where the usable window ends.",
    "The discharge limit and the round-trip efficiency arrive from outside this "
    "calculation: the limit is meant to be the observed p95 of five-minute "
    "battery_discharge_power_w over the last seven nights and the efficiency the "
    "registry's round-trip figure, so a stale one is part of this answer.",
)

_RANGE_BASIS = (
    "Range is the p25 to p75 of the reserve crossing times recorded across the "
    "comparable nights, interpolated linearly between them, not their earliest and "
    "latest."
)

_RANGE_BASIS_DRIFT = (
    "Range is the p25 to p75 of the reserve crossing times recorded across the "
    "comparable nights, widened at both ends by the measured disagreement between "
    "the packs."
)


def scheduled_windows(
    start: datetime, duration_s: int, zone: ZoneInfo
) -> tuple[tuple[datetime, datetime], ...]:
    """The real instants the schedule runs, as the windows ``simulate`` intersects.

    The windows are instants, not clock readings, which is the whole point of
    them. A schedule keyed by the clock that falls inside a fall-back hour gets
    applied to both passes through it and pays for a load that runs twice; a
    window of instants covers the seconds the load actually has to run, which is
    exactly ``duration_s``. A spring-forward night is missing an hour of clock,
    and these windows step over it because they never name a clock time.

    A plain schedule is one interval, so one window: ``simulate`` prices each
    step by the seconds its own span overlaps the window, which prices a
    schedule that starts or ends mid-step for exactly ``duration_s`` across the
    steps it touches — never a whole step more at the front, and never twice
    inside a fall-back hour.
    """
    if duration_s <= 0:
        return ()
    origin = _instant(start, zone)
    return ((origin, origin + timedelta(seconds=duration_s)),)


def essential_profile(
    circuit_curves: Sequence[dict[int, float]] | None, allowance_w: float
) -> dict[int, float]:
    """Measured circuit curves summed step by step, plus an unmonitored allowance.

    A minute no circuit answered stays out of the profile rather than becoming
    the allowance alone: the allowance is a figure for what was never measured,
    and multiplying it across a whole day of unanswered minutes would build a
    load out of the absence of one.

    With no circuits to sum, the allowance is still an answer. The owner said
    these loads exist, so the profile carries them for the whole day instead of
    returning an empty curve the simulation would read as no essential load.
    """
    if not circuit_curves:
        return {minute: allowance_w for minute in range(0, 24 * 60, STEP_SECONDS // 60)}
    measured: dict[int, float] = {}
    for curve in circuit_curves:
        for minute, watts in curve.items():
            measured[minute] = measured.get(minute, 0.0) + watts
    return {minute: total + allowance_w for minute, total in measured.items()}


def _carried_reads(keys: Sequence[int], curve: dict[int, float]) -> list[float]:
    """Read ``curve`` along the walked clock keys, carrying every gap forward.

    A key the curve did not answer takes the value of the last key it did answer,
    and a run of unanswered keys in front of the first answer takes the nearest
    answered key *before* the walk's start, read cyclically back through the
    curve's own day: a hole in the record says nobody read the step, not that
    the house drew nothing. Seeding from a later answered key would price the
    early steps at a rate the house only reaches hours afterwards. Only a curve
    that answers nothing anywhere has nothing to carry, and inventing a load out
    of it would spend a battery on a night nobody watched.
    """
    if not keys or not curve:
        return [0.0] * len(keys)
    reads: list[float] = []
    last: float | None = None
    first = keys[0]
    for key in keys:
        value = curve.get(key)
        if value is not None:
            last = value
        elif last is None:
            # Leading gap: the nearest answered key cyclically before the walk's
            # start — not a later answered key, which would price the early
            # steps at a rate the house only reaches hours afterwards.
            seed = min(curve, key=lambda k: (first - k) % (24 * 60))
            last = curve[seed]
        reads.append(last)
    return reads


def _percentile(times: list[datetime], fraction: float, zone: ZoneInfo) -> datetime:
    """Linear interpolation between sorted instants, the p25 and p75 of a spread.

    Interpolating between the times rather than taking the earliest and the
    latest keeps two odd nights out of the job of setting the width of every
    answer. Sorting and measuring happen in real instants: two clock readings
    inside a fall-back hour share a clock hour and can sit two real hours
    apart, and subtracting same-zone wall clock would call that gap negative.
    """
    ordered = sorted(when.astimezone(UTC) for when in times)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    if lower + 1 >= len(ordered):
        return ordered[-1].astimezone(zone)
    gap = (ordered[lower + 1] - ordered[lower]).total_seconds()
    instant = ordered[lower].timestamp() + (position - lower) * gap
    return datetime.fromtimestamp(instant, UTC).astimezone(zone)


def simulate(
    soc_now_pct: float,
    usable_ah: float,
    min_soc_pct: float,
    efficiency: float,
    charge_limit_w: float,
    discharge_limit_w: float,
    load_curve: dict[int, float],
    solar_curve: dict[int, float] | None,
    now: datetime,
    end: datetime,
    zone: ZoneInfo,
    scheduled_windows: tuple[tuple[datetime, datetime, float], ...] = (),
) -> PlanResult:
    """Project the battery from ``now`` to ``end`` in step-sized pieces.

    The walk is over real instants and the load is looked up by the local clock
    reading of each instant. That is the whole reason the walk is written this
    way: on a spring-forward night the missing hour is stepped over, and on a
    fall-back night the repeated hour is served twice, because two real hours
    pass while the clock shows 01:xx twice.

    The walk starts on the grid. ``now`` is floored down to the step it sits in
    and ``end`` is ceiled up to the step it sits in, and every step is charged
    by its overlap with the real window: the seconds between the floored step
    and the real ``now`` are history, already spent, and are not projected, the
    step ``end`` falls inside is projected only for the seconds left in it, and
    a scheduled window adds its watts only for the seconds it actually covers.

    Energy, not power, decides what the battery can do. A step asks the battery
    for what the loads want minus what solar already covers, the discharge limit
    caps the rate, and the energy left above the reserve floor caps the total.
    Whatever the battery cannot give, the grid gives, and that is what
    ``import_start`` records: the first step where the household needed the grid,
    which can be the very first one when the discharge limit is the binding
    constraint rather than the charge in the bank.

    A battery that starts at the reserve floor has already crossed it, and says
    so at the instant the plan begins, as long as something is being drawn. With
    nothing to draw there is no crossing to report, only a flat line.
    """
    if usable_ah <= 0 or efficiency <= 0 or min_soc_pct >= 100.0:
        return PlanResult(
            status="estimate_unavailable",
            reason="The battery window is empty, so there is nothing for a night to drain.",
        )
    start = _instant(now, zone)
    finish = _instant(end, zone)
    if (finish - start).total_seconds() < STEP_SECONDS - _EPS:
        return PlanResult(
            status="estimate_unavailable",
            reason="The horizon is shorter than one step, so there is nothing to project.",
        )

    # A point of SoC is worth this many watt-hours. The usable window spans the
    # whole scale from the reserve floor up to full, so 4.8 kWh above a 10 percent
    # floor is 90 points of 53.33 Wh each. The floor is where usable capacity
    # ends, which is what the setting says it is.
    per_point = usable_ah * NOMINAL_BUS_V / (100.0 - min_soc_pct)

    steps: list[tuple[datetime, float]] = []
    step_start = _floor_step(start)
    while step_start < finish:
        step_end = step_start + _STEP
        covered = (min(step_end, finish) - max(step_start, start)).total_seconds()
        steps.append((step_start, covered / STEP_SECONDS))
        step_start = step_end

    keys = [_clock_key(instant, zone) for instant, _ in steps]
    reads = _carried_reads(keys, load_curve)

    soc = min(max(soc_now_pct, min_soc_pct), 100.0)
    trajectory: list[tuple[datetime, float]] = [(steps[0][0].astimezone(zone), soc)]
    # At the floor before a single watt has moved is a crossing, not a hold: the
    # battery is already at reserve as the plan begins, and the household needs
    # the grid from the first second of it. Only something has to need it.
    crossing: datetime | None = None
    horizon_first = start
    schedule_draws = any(
        min(finish.astimezone(UTC), window_end.astimezone(UTC))
        > max(horizon_first.astimezone(UTC), window_start.astimezone(UTC))
        for window_start, window_end, watts in scheduled_windows
        if watts > 0
    )
    if soc <= min_soc_pct + _EPS and (any(value > _EPS for value in reads) or schedule_draws):
        crossing = start.astimezone(zone)
    import_start: datetime | None = None

    for index, (instant, _weight) in enumerate(steps):
        # The step's projected span: the first step begins at the projection
        # start (which may sit mid-step), every span ends at the horizon at the
        # latest. Everything below - house rate, scheduled overlap, limits -
        # is priced against this span and nothing else.
        span_start = start if index == 0 else instant
        span_end = min(instant + _STEP, finish)
        span_h = (span_end - span_start).total_seconds() / 3600.0
        clock = _clock_key(instant, zone)
        solar = 0.0 if solar_curve is None else solar_curve.get(clock, 0.0)

        # The scheduled load is part of the step's load rate: it pays the same
        # inverter losses and the same discharge cap as the house does, priced
        # by the seconds its window overlaps this step's projected span.
        span_s = (span_end - span_start).total_seconds()
        sched_w = 0.0
        for window_start, window_end, watts in scheduled_windows:
            overlap = (min(span_end, window_end) - max(span_start, window_start)).total_seconds()
            if overlap > 0:
                sched_w += watts * overlap / span_s

        needs = (reads[index] + sched_w - solar) / efficiency
        drawn = 0.0
        short = False
        if needs > _EPS:
            wanted = min(needs, discharge_limit_w) * span_h
            available = (soc - min_soc_pct) * per_point
            if available > _EPS:
                drawn = min(wanted, available)
            short = wanted > drawn + _EPS or needs > discharge_limit_w + _EPS
            soc = max(min_soc_pct, soc - drawn / per_point)
        elif needs < -_EPS:
            charge = min(-needs, charge_limit_w) * span_h
            soc = min(100.0, soc + charge / per_point)
        boundary = min(instant + _STEP, finish)
        if short and import_start is None:
            import_start = span_start.astimezone(zone)
        if crossing is None and drawn > _EPS and soc <= min_soc_pct + _EPS:
            crossing = boundary.astimezone(zone)
        trajectory.append((boundary.astimezone(zone), soc))

    return PlanResult(
        trajectory=tuple(trajectory),
        reserve_crossing=crossing,
        import_start=import_start,
        assumptions=_SIMULATION_ASSUMPTIONS,
    )


def _openended(censoring: int, nights: int) -> bool:
    """Whether too many nights failed to reach the floor to report a later bound."""
    return censoring * 4 > nights


def _widened(
    window: tuple[datetime, datetime | None] | None,
    earlier: datetime | None,
    later: datetime | None,
    zone: ZoneInfo,
) -> tuple[datetime, datetime | None] | None:
    """Push the range out to what the drift band says the night could do.

    The lower edge takes the crossing of a battery that starts ``band`` lower, the
    upper edge the crossing of one that starts ``band`` higher. A band that never
    reaches the floor leaves the upper edge open: the honest width includes nights
    that outlast the horizon, and closing it at the horizon would be a claim.
    """
    if window is None:
        if earlier is None:
            return None
        return (earlier.astimezone(zone), None)
    # Compare as instants, converted out of the installation zone first:
    # inside a fall-back hour two readings can share a clock hour, and only
    # the instants order which one is really earlier.
    low = window[0].astimezone(UTC)
    high = None if window[1] is None else window[1].astimezone(UTC)
    if earlier is not None:
        low = min(low, earlier.astimezone(UTC))
    # A band that never reaches the floor leaves the upper edge open rather than
    # closed at the last crossing that did happen.
    high = None if high is None or later is None else max(high, later.astimezone(UTC))
    return (
        low.astimezone(zone),
        None if high is None else high.astimezone(zone),
    )


def build_plan(
    soc_now_pct: float,
    # None means the pack capacity was never reported, which is a refusal rather
    # than a zero: a planner that assumed a capacity would invent the answer.
    usable_ah: float | None,
    min_soc_pct: float,
    efficiency: float,
    charge_limit_w: float,
    discharge_limit_w: float,
    night_curves: list[dict[int, float]],
    essential_curve: dict[int, float] | None,
    scheduled: tuple[datetime, int, float] | None,
    solar_curve: dict[int, float] | None,
    now: datetime,
    end: datetime,
    zone: ZoneInfo,
    calibration_severity: str | None,
    # The largest state-of-charge disagreement between packs, from the calibration
    # payload. None means the payload named a warning without saying how wide it
    # is, and then the range stays narrow and says that it is.
    drift_band_pct: float | None = None,
) -> PlanSummary:
    """The orchestrator: a central estimate, a range around it, and the scenarios.

    The gate runs first and nothing is computed when it refuses. A plan built on
    a drifted state of charge, on a battery nobody sized, or on two nights of
    history would be a number that looks like an answer, and the owner cannot
    tell that apart from a real one. A projection that refuses for a reason of its
    own is propagated with that reason rather than wrapped in an "ok" summary: a
    range derived from a curve that never ran would be a spread of nothing.

    The range comes from the nights themselves: each comparable night is projected
    on its own, and the window spans the p25 to the p75 of those crossing times,
    interpolated between them. That is a spread of what the house actually did,
    and it stays the honest basis until replay says how wide the projection's own
    error is. A known drift magnitude is measured, not invented, and it widens the
    window at both ends as well as thickening the reported curve.
    """
    horizon_s = (_instant(end, zone) - _instant(now, zone)).total_seconds()
    gate = plan_status(
        calibration_severity,
        usable_ah,
        len(night_curves),
        efficiency,
        min_soc_pct,
        horizon_s,
    )
    if usable_ah is None or gate.status != "ok":
        reason = gate.reason or "The planner has no basis for an estimate."
        return PlanSummary(status="estimate_unavailable", reason=reason, assumptions=(reason,))

    args = (
        soc_now_pct,
        usable_ah,
        min_soc_pct,
        efficiency,
        charge_limit_w,
        discharge_limit_w,
    )
    typical = median_curve(night_curves)
    central = simulate(*args, typical, solar_curve, now, end, zone)
    if central.status != "ok":
        reason = central.reason or "The central projection refused to run."
        return PlanSummary(status="estimate_unavailable", reason=reason, assumptions=(reason,))

    crossings: list[datetime] = []
    for curve in night_curves:
        one = simulate(*args, curve, solar_curve, now, end, zone)
        if one.reserve_crossing is not None:
            crossings.append(one.reserve_crossing)

    censoring = len(night_curves) - len(crossings)
    window: tuple[datetime, datetime | None] | None = None
    if crossings:
        low = _percentile(crossings, 0.25, zone)
        high = _percentile(crossings, 0.75, zone)
        window = (low, None) if _openended(censoring, len(night_curves)) else (low, high)

    assumptions = list(central.assumptions)
    assumptions.append(
        f"Typical load is the median of {len(night_curves)} comparable nights aligned by "
        "clock time, not an average of all the history."
    )
    if len(crossings) == 1:
        assumptions.append(
            "One comparable night reached the reserve floor inside the horizon, so this "
            "range rests on that single night rather than on a spread."
        )
    if _openended(censoring, len(night_curves)):
        assumptions.append(
            "At least 25% of the comparable nights never reach the reserve floor inside "
            "the horizon, so the range is reported open at the later end: a night that "
            "outlasts the horizon is a wider answer than the horizon is."
        )

    basis = _RANGE_BASIS if window is not None else ""

    if calibration_severity == "warning":
        # A drift band is a magnitude: a negative or non-finite width says
        # nothing about how far apart the packs are, and goes the same path as
        # no width at all. A finite non-negative width is used as given.
        band = (
            drift_band_pct
            if drift_band_pct is not None and isfinite(drift_band_pct) and drift_band_pct >= 0.0
            else None
        )
        if band is None:
            assumptions.append(
                "Calibration reports a warning-level drift and no drift magnitude came "
                "with it, so this range spans only the spread between nights and does not "
                "include the disagreement between the packs."
            )
        else:
            source = (
                f"This curve is reported as a band of plus or minus {band:.1f} points "
                "of state of charge, which is the measured disagreement between the "
                "packs, and the range is widened by the crossings that band allows."
            )
            assumptions.append(source)
            low_run = simulate(
                max(min_soc_pct, soc_now_pct - band),
                *args[1:],
                typical,
                solar_curve,
                now,
                end,
                zone,
            )
            high_run = simulate(
                min(100.0, soc_now_pct + band),
                *args[1:],
                typical,
                solar_curve,
                now,
                end,
                zone,
            )
            central = replace(
                central,
                trajectory_band=tuple(
                    (when, max(min_soc_pct, value - band), min(100.0, value + band))
                    for when, value in central.trajectory
                ),
                assumptions=(*central.assumptions, source),
            )
            widened = _widened(window, low_run.reserve_crossing, high_run.reserve_crossing, zone)
            if widened is not None:
                window = widened
                basis = _RANGE_BASIS_DRIFT

    scenarios: dict[str, PlanResult] = {"typical": central}
    if essential_curve:
        caveat = (
            "Each circuit curve summed here is a measurement of a metered circuit. "
            "The watts added on top at every measured step are a stand-in for load "
            "nobody meters, and that part is an assumption, not a measurement."
        )
        scenarios["essential"] = replace(
            simulate(*args, essential_curve, solar_curve, now, end, zone),
            assumptions=(*central.assumptions, caveat),
        )
    if scheduled is not None:
        when, duration_s, watts = scheduled
        windows = tuple(
            (start, finish, watts) for start, finish in scheduled_windows(when, duration_s, zone)
        )
        caveat = (
            f"A scheduled load of {watts:.0f} W is added in full: without circuit history "
            "the planner cannot tell whether a typical night already carries part of it."
        )
        scenarios["scheduled"] = replace(
            simulate(*args, typical, solar_curve, now, end, zone, windows),
            assumptions=(*central.assumptions, caveat),
        )

    return PlanSummary(
        central=central,
        reserve_window=window,
        range_basis=basis,
        scenarios=scenarios,
        assumptions=tuple(assumptions),
    )


@dataclass(frozen=True)
class NightReplay:
    """What one replayed night says about its own projection.

    A None crossing is not a crossing at midnight and a None energy error is
    not a night that drew nothing: one means the reach of the floor could not
    be confirmed, the other that the night had no SoC record to compare with.
    """

    night: date
    projected_crossing: datetime | None
    actual_crossing: datetime | None
    wh_error: float | None


def _night_of(instant: datetime, zone: ZoneInfo) -> date:
    """The calendar night an instant belongs to, on the noon-to-noon cut."""
    clock = instant.astimezone(zone)
    day = clock.date()
    return day if clock.hour < 12 else day + timedelta(days=1)


def actual_crossing(
    soc_rows: Sequence[tuple[datetime, float]], min_soc_pct: float, zone: ZoneInfo
) -> datetime | None:
    """The first instant the recorded SoC reaches min_soc, or None.

    Inside the noon-to-noon night walk, that is: None when the record never
    touches the floor, or when it is empty. Rows are taken in chronological
    order, not arrival order: inside a fall-back hour two rows can carry one
    clock reading, and the earlier instant is the answer. The walk compares
    instants, converted out of the installation's zone first, for exactly
    that reason.
    """
    for when, soc in sorted(soc_rows, key=lambda row: _instant(row[0], zone)):
        if soc <= min_soc_pct + _EPS:
            return _instant(when, zone).astimezone(zone)
    return None


def _replay_window(night: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """The replayed projection's horizon: the night's 22:00 to its 07:00.

    Both ends are instants, not clock readings: the window over a fall-back
    night is ten real hours and the window over a spring-forward night is
    eight, because the walk runs on real time.
    """
    previous = night - timedelta(days=1)
    start = datetime(previous.year, previous.month, previous.day, 22, 0, tzinfo=zone)
    end = datetime(night.year, night.month, night.day, 7, 0, tzinfo=zone)
    return _instant(start, zone), _instant(end, zone)


def replay_nights(
    rows: Sequence[tuple[datetime, float | None]],
    soc_rows: Sequence[tuple[datetime, float]],
    zone: ZoneInfo,
    usable_ah: float,
    min_soc_pct: float,
    efficiency: float,
    charge_limit_w: float,
    discharge_limit_w: float,
) -> list[NightReplay]:
    """Replay every eligible night.

    A night is eligible when it answers at least half of its own walked
    steps, when it is past the first seven nights of the record, and when
    its typical curve can be built from the seven nights before it. The
    first seven nights are never replayed: the planner would not have had
    its seven-night window yet, and a replay without that history grades
    the planner on a curve it could not have drawn at the time. A night
    whose SoC record is empty is not replayed either, because there is no
    battery to project and nothing to compare the projection with.

    The projection runs from the night's local 22:00 to its 07:00 the next
    morning, on a median of the curves before it with the night's own load
    held out. Solar enters as nothing, not as a stand-in forecast: this
    call's seam carries load rows and state-of-charge rows only, archived
    forecasts do not exist, and a replay handed a sun of any kind would be
    graded against light the planner at 22:00 was never given.

    Silence discipline on the SoC side: a hole in the record is silence,
    never a slow night. A crossing is reported only when the walk step
    before the reach answered and still sat above the floor, which is the
    only way the record witnesses the floor being crossed; a reach that
    opens straight out of a hole could have happened anywhere inside it,
    so the night carries None. The energy side compares the readings that
    exist, first against last, so a hole makes that comparison thinner but
    never fills a gap with zeros.
    """
    if usable_ah <= 0 or efficiency <= 0 or min_soc_pct >= 100.0:
        return []
    per_point = usable_ah * NOMINAL_BUS_V / (100.0 - min_soc_pct)

    # Every row names its night before its values are filtered, so a silent
    # night still holds its place in the record. A night is counted once per
    # five-minute step it answers, the same measure the curve window uses.
    seen: set[date] = set()
    answered: dict[date, set[datetime]] = {}
    for when, value in rows:
        instant = _instant(when, zone)
        night = _night_of(instant, zone)
        seen.add(night)
        if value is not None:
            answered.setdefault(night, set()).add(_floor_step(instant))

    ordered = sorted(seen)
    replays: list[NightReplay] = []
    for index, night in enumerate(ordered):
        if index < NIGHTS:
            continue
        if len(answered.get(night, ())) * 2 < _night_step_count(night, zone):
            continue
        previous = night - timedelta(days=1)
        midnight_noon = datetime(previous.year, previous.month, previous.day, 12, 0, tzinfo=zone)
        cut = _instant(midnight_noon, zone)
        pre = [row for row in rows if _instant(row[0], zone) < cut]
        curves = night_curves(pre, zone)
        if not curves:
            continue
        start, end = _replay_window(night, zone)
        # The battery state at the boundary must come from a reading at or
        # before it: a later reading is information the 22:00 planner was
        # never given, and starting from it would violate causality.
        pre_boundary = sorted(
            ((when, soc) for when, soc in soc_rows if _instant(when, zone) <= start),
            key=lambda row: _instant(row[0], zone),
        )
        if not pre_boundary:
            continue
        starting_soc = pre_boundary[-1][1]
        window = sorted(
            ((when, soc) for when, soc in soc_rows if start <= _instant(when, zone) <= end),
            key=lambda row: _instant(row[0], zone),
        )
        if not window:
            continue
        # The projection starts from the SoC the 22:00 planner was handed: the
        # last reading at or before the boundary, not the first reading after
        # it (which would borrow a future answer).
        result = simulate(
            starting_soc,
            usable_ah,
            min_soc_pct,
            efficiency,
            charge_limit_w,
            discharge_limit_w,
            median_curve(curves),
            None,
            start,
            end,
            zone,
        )
        reading: dict[datetime, float] = {}
        for when, soc in window:
            reading.setdefault(_instant(when, zone), soc)
        crossing = actual_crossing(window, min_soc_pct, zone)
        actual: datetime | None = None
        if crossing is not None:
            # A crossing at the window's own start is witnessed by the
            # boundary reading itself: the recorded SoC at that instant IS
            # the evidence. The witness-above-floor check only guards
            # crossings that happen after the window opens, where a hole
            # in the record could have hidden the real reach.
            if crossing == start:
                actual = crossing
            else:
                witness = reading.get(crossing.astimezone(UTC) - _STEP)
                if witness is not None and witness > min_soc_pct + _EPS:
                    actual = crossing
        values = [point[1] for point in result.trajectory]
        projected_draw = sum(max(0.0, a - b) for a, b in pairwise(values))
        actual_draw = starting_soc - window[-1][1]
        replays.append(
            NightReplay(
                night=night,
                projected_crossing=result.reserve_crossing,
                actual_crossing=actual,
                wh_error=(projected_draw - actual_draw) * per_point,
            )
        )
    return replays


def crossing_errors(replays: Sequence[NightReplay]) -> list[timedelta]:
    """Signed per-night crossing errors (projected minus actual).

    Only the nights where both crossings exist are reported. The two sides
    are subtracted as instants, converted out of the zone first: two clock
    readings inside a fall-back hour are not one clock hour apart, and
    same-zone clock would hide that. A night with only one side is censored
    data, not a zero error: nights that never reach the floor inside the
    horizon, and nights whose SoC record cannot confirm the reach,
    contribute nothing.
    """
    errors: list[timedelta] = []
    for night in replays:
        if night.projected_crossing is not None and night.actual_crossing is not None:
            projected = night.projected_crossing.astimezone(UTC)
            actual = night.actual_crossing.astimezone(UTC)
            errors.append(projected - actual)
    return errors


def wh_errors(replays: Sequence[NightReplay]) -> list[float]:
    """Signed per-night energy errors for the nights where both sides exist.

    A None is not a measured error of zero: it says the night had no actual
    SoC to compare with, and it contributes nothing to the list.
    """
    errors: list[float] = []
    for replay in replays:
        if replay.wh_error is not None:
            errors.append(replay.wh_error)
    return errors
