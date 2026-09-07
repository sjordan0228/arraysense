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
plan that is too short rather than a plan that is too long.

Times are walked as real instants and looked up by local clock time, so the
spring gap is walked over rather than through, and a fall-back hour is served
twice because two real hours pass inside it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
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
    halves. Rows are bucketed by the real instant they were taken at, so the two
    passes through a fall-back clock minute are two answers of one night, and a
    timestamp that arrived twice is one answer and not two.

    A night is usable when it answers at least half of its own steps, counted as
    the clock minutes it writes to the curve. Below that the record is mostly
    gaps, and a median taken over mostly gaps is a guess about a house nobody
    watched. Fewer than MIN_USABLE_NIGHTS usable nights is not a typical night
    yet, so nothing is returned rather than a profile built on a single night.

    The window is the NIGHTS consecutive calendar nights ending with the most
    recent night that has any rows. It is a window of dates, not of usable
    nights: a silent night sits in the window and makes itself unusable rather
    than letting the plan reach back past it into older history, which is a
    different season and not more evidence.
    """
    nights: dict[date, dict[datetime, float]] = {}
    for when, watts in sorted(rows, key=lambda row: _instant(row[0], zone)):
        if watts is None:
            continue
        instant = _instant(when, zone)
        clock = instant.astimezone(zone)
        day = clock.date()
        night = day if clock.hour < 12 else day + timedelta(days=1)
        nights.setdefault(night, {}).setdefault(instant, float(watts))

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
        for instant in sorted(bucket):
            passes.setdefault(_clock_key(instant, zone), []).append(bucket[instant])
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
    "calculation: the limit is meant to be the busiest five minutes the last seven "
    "nights recorded and the efficiency the registry's round-trip figure, so a "
    "stale one is part of this answer.",
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
    """The real step-sized windows a schedule covers, oldest window first.

    The windows are instants, not clock readings, which is the whole point of
    them. A schedule keyed by the clock that falls inside a fall-back hour gets
    applied to both passes through it and pays for a load that runs twice; a
    window of instants covers the seconds the load actually has to run, which is
    exactly ``duration_s``. A spring-forward night is missing an hour of clock,
    and these windows step over it because they never name a clock time.

    The first window starts at the step boundary below ``start``, and windows are
    added until ``duration_s`` of real time is covered, so a schedule that starts
    mid-step is charged for the whole step it starts in. ``simulate`` adds the
    watts to a step whose own instant falls inside a window.
    """
    if duration_s <= 0:
        return ()
    first = _floor_step(_instant(start, zone))
    finish = _instant(start, zone) + timedelta(seconds=duration_s)
    windows: list[tuple[datetime, datetime]] = []
    step = first
    while step < finish:
        windows.append((step, step + _STEP))
        step += _STEP
    return tuple(windows)


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
    and a run of unanswered keys in front of the first answer takes that answer:
    a hole in the record says nobody read the step, not that the house drew
    nothing. A curve that answers nothing anywhere has nothing to carry, and
    inventing a load out of it would spend a battery on a night nobody watched.
    """
    head: float | None = None
    for key in keys:
        value = curve.get(key)
        if value is not None:
            head = value
            break
    if head is None:
        return [0.0] * len(keys)

    reads: list[float] = []
    last = head
    for key in keys:
        value = curve.get(key)
        if value is None:
            reads.append(last)
        else:
            last = value
            reads.append(value)
    return reads


def _percentile(times: list[datetime], fraction: float, zone: ZoneInfo) -> datetime:
    """Linear interpolation between sorted instants, the p25 and p75 of a spread.

    Interpolating between the times rather than taking the earliest and the latest
    keeps two odd nights out of the job of setting the width of every answer.
    """
    ordered = sorted(times)
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
    and ``end`` is ceiled up to the step it sits in: the seconds between the
    floored step and the real ``now`` are history, already spent, and are not
    projected, while the step ``end`` falls inside is projected only for the
    seconds left in it and costs that fraction of its energy.

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
    step_hours = STEP_SECONDS / 3600.0

    steps: list[tuple[datetime, float]] = []
    step_start = _floor_step(start)
    while step_start < finish:
        remaining = min(float(STEP_SECONDS), (finish - step_start).total_seconds())
        steps.append((step_start, remaining / STEP_SECONDS))
        step_start += _STEP

    keys = [_clock_key(instant, zone) for instant, _ in steps]
    reads = _carried_reads(keys, load_curve)
    added: list[float] = []
    for instant, _ in steps:
        extra = 0.0
        for window_start, window_end, watts in scheduled_windows:
            if watts > 0 and window_start <= instant < window_end:
                extra += watts
        added.append(extra)

    soc = min(max(soc_now_pct, min_soc_pct), 100.0)
    trajectory: list[tuple[datetime, float]] = [(steps[0][0].astimezone(zone), soc)]
    # At the floor before a single watt has moved is a crossing, not a hold: the
    # battery is already at reserve as the plan begins, and the household needs
    # the grid from the first second of it. Only something has to need it.
    drawing = any(value > _EPS for value in reads) or any(value > 0 for value in added)
    crossing: datetime | None = None
    if soc <= min_soc_pct + _EPS and drawing:
        crossing = steps[0][0].astimezone(zone)
    import_start: datetime | None = None

    for index, (instant, weight) in enumerate(steps):
        load = reads[index] + added[index]
        solar = 0.0 if solar_curve is None else solar_curve.get(keys[index], 0.0)
        needs = (load - solar) / efficiency
        drawn = 0.0
        short = False
        if needs > _EPS:
            wanted = min(needs, discharge_limit_w) * step_hours * weight
            available = (soc - min_soc_pct) * per_point
            if available > _EPS:
                drawn = min(wanted, available)
            short = needs - min(needs, discharge_limit_w) > _EPS or drawn + _EPS < wanted
            soc = max(min_soc_pct, soc - drawn / per_point)
        else:
            surplus = min(-needs, charge_limit_w) * step_hours * weight
            room = (100.0 - soc) * per_point
            soc = min(100.0, soc + min(surplus, max(0.0, room)) / per_point)
        boundary = instant + _STEP
        if short and import_start is None:
            import_start = instant.astimezone(zone)
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
    low, high = window
    if earlier is not None:
        low = min(low, earlier)
    # A band that never reaches the floor leaves the upper edge open rather than
    # closed at the last crossing that did happen.
    high = None if high is None or later is None else max(high, later)
    return (low, high)


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
        if drift_band_pct is None:
            assumptions.append(
                "Calibration reports a warning-level drift and no drift magnitude came "
                "with it, so this range spans only the spread between nights and does not "
                "include the disagreement between the packs."
            )
        else:
            band = drift_band_pct
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
            (window_start, window_end, watts)
            for window_start, window_end in scheduled_windows(when, duration_s, zone)
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
