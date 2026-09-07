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

Silence is skipped, never read as a zero. A step that did not answer is not
evidence that the house drew nothing: the minute tier was queried and nothing
came back. Turning that into 0 W invents a quiet house out of a gap in the
record, and the projection then reports a battery that lasted longer than
anything was measured to last.

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

# A night answers one step per five minutes of clock. A 25 hour fall-back night
# answers more than this and is never charged for the extra hour.
NIGHT_STEPS = (24 * 60 * 60) // STEP_SECONDS
MIN_ANSWERED = NIGHT_STEPS // 2

_STEP = timedelta(seconds=STEP_SECONDS)

# Anything smaller than this is float noise from the step arithmetic, not a
# watt the household drew.
_EPS = 1e-9


@dataclass(frozen=True)
class PlanResult:
    """One projected curve and the two times worth reading off it."""

    status: str = "ok"
    reason: str | None = None
    trajectory: tuple[tuple[datetime, float], ...] = ()
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
    """The central estimate, the range around it, and the scenarios."""

    central: PlanResult = field(default_factory=PlanResult)
    reserve_window: tuple[datetime, datetime] | None = None
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


def night_curves(
    rows: Sequence[tuple[datetime, float | None]], zone: ZoneInfo
) -> list[dict[int, float]]:
    """One curve per usable night, keyed by local minute-of-day.

    A night is cut on the installation's calendar and runs noon to noon, so the
    evening draw and the following morning's belong to the same night, and a
    25 hour fall-back night stays one night instead of splitting into two thin
    halves.

    A night is usable when it answers at least half of a night's steps. Below
    that the record is mostly gaps, and a median taken over mostly gaps is a
    guess about a house nobody watched. Fewer than MIN_USABLE_NIGHTS usable
    nights is not a typical night yet, so nothing is returned rather than a
    profile built on a single night.

    Only the NIGHTS most recent usable nights count: history older than the
    window the owner set is a different season, not more evidence.
    """
    nights: dict[date, list[tuple[int, float]]] = {}
    for when, watts in sorted(rows, key=lambda row: _instant(row[0], zone)):
        if watts is None:
            continue
        clock = _instant(when, zone).astimezone(zone)
        day = clock.date()
        night = day if clock.hour < 12 else day + timedelta(days=1)
        # Sorted by instant, so the first reading of a clock minute is the one
        # that happened first: a fall-back hour is served twice, and the second
        # pass is not new information about what a night looks like.
        nights.setdefault(night, []).append((clock.hour * 60 + clock.minute, float(watts)))

    # The window is the NIGHTS most recent nights; they are then walked oldest
    # first so a caller reads the curves in the order they happened.
    window = sorted(nights, reverse=True)[:NIGHTS]
    curves: list[dict[int, float]] = []
    for night in sorted(window):
        steps = nights[night]
        if len(steps) * 2 < NIGHT_STEPS:
            continue
        curve: dict[int, float] = {}
        for minute, watts in steps:
            curve.setdefault(minute, watts)
        curves.append(curve)

    if len(curves) < MIN_USABLE_NIGHTS:
        return []
    return curves


def median_curve(curves: list[dict[int, float]]) -> dict[int, float]:
    """Element-wise median across usable nights.

    A clock minute some nights did not answer is the median over the nights that
    did. Filling it with zero would credit the plan with a load nobody drew;
    dropping it would leave a hole in the curve the simulation then reads as no
    load, which is the same error wearing a different hat.
    """
    support: dict[int, list[float]] = {}
    for curve in curves:
        for minute, watts in curve.items():
            support.setdefault(minute, []).append(watts)
    return {minute: float(median(values)) for minute, values in support.items()}


def plan_status(severity: str | None, usable_ah: float | None, usable_nights: int) -> PlanStatus:
    """Whether there is an answer to give, before any arithmetic.

    Only the top two rungs of the calibration ladder refuse. A warning widens
    what the plan is willing to claim, which is a fact about the range rather
    than a reason to say nothing; elevated and alert mean the state of charge
    itself is untrustworthy, and a projection built on a number we do not
    believe would only dress up the doubt as a forecast.

    Drift is checked before capacity. Both refuse, and the reason a calibration
    reads as drifted is the reason every figure in the answer is soft, so it is
    the one worth reading first.
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
    return PlanStatus()


# What every projection assumes, stated rather than hidden. These are read out
# to the owner next to the curve, so they name the two things a reader could
# otherwise take for measured fact: that a gap in the record is not a quiet
# house, and that the grid was assumed there.
_SIMULATION_ASSUMPTIONS = (
    "Load comes from the recorded steps only: a step that did not answer is "
    "skipped, not read as a silent house.",
    "The grid is assumed available: at the reserve floor the battery holds and the "
    "household starts importing, which is an import, not an outage.",
    "A 48 V nominal bus is assumed: amp-hours are spent as watt-hours at 48 Wh "
    "per amp-hour, and the reserve floor is where the usable window ends.",
)


def scheduled_delta(
    base: dict[int, float], start: datetime, duration_s: int, watts: float, zone: ZoneInfo
) -> dict[int, float]:
    """``base`` with ``watts`` added over [start, start + duration) in local minutes.

    The window is floored back to the step grid and its last step is the one the
    schedule ends inside. A 21:37 start therefore covers the 21:35 step and a
    sixty minute run covers thirteen steps rather than twelve: a load that
    starts mid-step draws during that step, and a curve keyed by clock time has
    to put those watts somewhere or the plan quietly loses them.
    """
    raised = dict(base)
    if watts <= 0 or duration_s <= 0:
        return raised
    minutes = start.hour * 60 + start.minute
    lead = minutes % (STEP_SECONDS // 60)
    # Instants, not clock times, so a run that crosses a transition keeps its
    # real length; the key is the local clock reading of each step.
    first = _instant(start, zone) - timedelta(minutes=lead)
    finish = _instant(start, zone) + timedelta(seconds=duration_s)
    step = first
    while step < finish:
        key = _clock_key(step, zone)
        raised[key] = raised.get(key, 0.0) + watts
        step += _STEP
    return raised


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
) -> PlanResult:
    """Project the battery from ``now`` to ``end`` in step-sized pieces.

    The walk is over real instants and the load is looked up by the local clock
    reading of each instant. That is the whole reason the walk is written this
    way: on a spring-forward night the missing hour is stepped over, and on a
    fall-back night the repeated hour is served twice, because two real hours
    pass while the clock shows 01:xx twice.

    Energy, not power, decides what the battery can do. A step asks the battery
    for what the loads want minus what solar already covers, the discharge limit
    caps the rate, and the energy left above the reserve floor caps the total.
    Whatever the battery cannot give, the grid gives, and that is what
    ``import_start`` records: the first step where the household needed the grid,
    which can be the very first one when the discharge limit is the binding
    constraint rather than the charge in the bank.
    """
    if usable_ah <= 0 or efficiency <= 0 or min_soc_pct >= 100.0:
        return PlanResult(
            status="estimate_unavailable",
            reason="The battery window is empty, so there is nothing for a night to drain.",
        )
    start = _instant(now, zone)
    finish = _instant(end, zone)
    steps = int((finish - start).total_seconds() // STEP_SECONDS)
    if steps < 1:
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
    instants = [start + timedelta(seconds=STEP_SECONDS * i) for i in range(steps + 1)]

    soc = min(max(soc_now_pct, min_soc_pct), 100.0)
    trajectory: list[tuple[datetime, float]] = [(instants[0].astimezone(zone), soc)]
    crossing: datetime | None = None
    import_start: datetime | None = None

    for index in range(steps):
        key = _clock_key(instants[index], zone)
        load = load_curve.get(key, 0.0)
        solar = 0.0 if solar_curve is None else solar_curve.get(key, 0.0)
        needs = (load - solar) / efficiency
        drawn = 0.0
        short = False
        if needs > _EPS:
            wanted = min(needs, discharge_limit_w) * step_hours
            available = (soc - min_soc_pct) * per_point
            if available > _EPS:
                drawn = min(wanted, available)
            short = needs - min(needs, discharge_limit_w) > _EPS or drawn + _EPS < wanted
            soc = max(min_soc_pct, soc - drawn / per_point)
        else:
            surplus = min(-needs, charge_limit_w) * step_hours
            room = (100.0 - soc) * per_point
            soc = min(100.0, soc + min(surplus, max(0.0, room)) / per_point)
        if short and import_start is None:
            import_start = instants[index].astimezone(zone)
        if crossing is None and drawn > _EPS and soc <= min_soc_pct + _EPS:
            crossing = instants[index + 1].astimezone(zone)
        trajectory.append((instants[index + 1].astimezone(zone), soc))

    return PlanResult(
        trajectory=tuple(trajectory),
        reserve_crossing=crossing,
        import_start=import_start,
        assumptions=_SIMULATION_ASSUMPTIONS,
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
) -> PlanSummary:
    """The orchestrator: a central estimate, a range around it, and the scenarios.

    The gate runs first and nothing is computed when it refuses. A plan built on
    a drifted state of charge, on a battery nobody sized, or on two nights of
    history would be a number that looks like an answer, and the owner cannot
    tell that apart from a real one.

    The range comes from the nights themselves: each comparable night is
    projected on its own, and the window spans the earliest crossing to the
    latest one. That is a spread of what the house actually did, not a
    percentile, and it stays the honest basis until replay says how wide the
    projection's own error is.
    """
    gate = plan_status(calibration_severity, usable_ah, len(night_curves))
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

    crossings: list[datetime] = []
    for curve in night_curves:
        one = simulate(*args, curve, solar_curve, now, end, zone)
        if one.reserve_crossing is not None:
            crossings.append(one.reserve_crossing)

    scenarios: dict[str, PlanResult] = {"typical": central}
    assumptions = list(central.assumptions)
    assumptions.append(
        f"Typical load is the median of {len(night_curves)} comparable nights aligned by "
        "clock time, not an average of all the history."
    )
    if essential_curve:
        caveat = (
            "Essential loads are an assumption, not a measurement: the circuit curves "
            "that were summed here stand in for the loads nobody meters."
        )
        scenarios["essential"] = replace(
            simulate(*args, essential_curve, solar_curve, now, end, zone),
            assumptions=(*central.assumptions, caveat),
        )
    if scheduled is not None:
        when, duration_s, watts = scheduled
        curve = scheduled_delta(typical, when, duration_s, watts, zone)
        caveat = (
            f"A scheduled load of {watts:.0f} W is added in full: without circuit history "
            "the planner cannot tell whether a typical night already carries part of it."
        )
        scenarios["scheduled"] = replace(
            simulate(*args, curve, solar_curve, now, end, zone),
            assumptions=(*central.assumptions, caveat),
        )
    if calibration_severity == "warning":
        assumptions.append(
            "Calibration reports a warning-level drift, and this range only spans the "
            "spread between nights, so read it as narrower than the answer really is."
        )

    basis = ""
    if crossings:
        basis = (
            "Range is the first and last reserve crossing across the comparable nights, "
            "not a percentile of them."
        )

    return PlanSummary(
        central=central,
        reserve_window=(min(crossings), max(crossings)) if crossings else None,
        range_basis=basis,
        scenarios=scenarios,
        assumptions=tuple(assumptions),
    )
