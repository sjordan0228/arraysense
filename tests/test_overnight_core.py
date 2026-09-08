"""The overnight planner's estimation core, tested as arithmetic.

Nothing here opens a store or a page: rows go in, curves and trajectories come
out. That split is the point. Every question this feature answers -- how long
the battery lasts, when the household starts importing, which nights count as
comparable -- is decided by bookkeeping in ``overnight.py``, and a change to
that bookkeeping has to surface as a failed assertion rather than as a page that
looks slightly different.

Four conventions get pinned here because they are decisions rather than
incidents:

* A night is cut on the installation's local calendar and runs noon to noon, and
  it is measured against its own walked step count: 288 steps normally, 300
  across a fall-back, 276 across a spring-forward.
* A row that reads ``None`` is silence. It is carried over -- the last answered
  step stays in force across it -- because an unanswered step is not evidence
  that the house drew nothing, and a zero is exactly that evidence invented.
* The typical curve is a median of whole nights. One unusual night must not move
  the floor of the whole plan.
* The simulation walks real instants, starts on the five-minute grid, and looks
  up load by the local clock reading of each instant, so a spring-forward night
  is shorter than its clock times suggest and a repeated hour is served twice.
  A step is charged only for the seconds of it that lie inside the window, and
  a scheduled load is priced by the exact seconds its windows cover.

The reference figures come from the design doc: a 100 Ah usable window on a
48 V nominal bus is 4.8 kWh, which is what the drain tests below count against.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from arraysense.overnight import (
    STEP_SECONDS,
    PlanResult,
    PlanSummary,
    build_plan,
    essential_profile,
    median_curve,
    night_curves,
    plan_status,
    scheduled_windows,
    simulate,
)

NY = ZoneInfo("America/New_York")
GRID = list(range(0, 1440, 5))

# One point of state of charge on the reference battery, in watt-hours: the
# 4.8 kWh window spread over the 90 points between a 10 percent floor and full.
PER_POINT = 100.0 * 48.0 / 90.0


def at(clock: str) -> datetime:
    """A New York clock reading, which is how the planner cuts its days."""
    return datetime.fromisoformat(clock).replace(tzinfo=NY)


def sweep(
    start: str,
    watts: float,
    count: int = 288,
    skip: frozenset[int] = frozenset(),
) -> list[tuple[datetime, float | None]]:
    """One row per step from ``start``, with ``None`` where the house did not answer.

    The steps walk real time from the instant ``start`` names rather than adding
    clock minutes, because that is what a night is: 300 steps is 25 real hours,
    which is what the night containing a fall-back costs.
    """
    first = at(start).astimezone(UTC)
    return [
        (
            (first + timedelta(seconds=STEP_SECONDS * i)).astimezone(NY),
            None if i in skip else watts,
        )
        for i in range(count)
    ]


def flat(watts: float) -> dict[int, float]:
    """A curve that draws ``watts`` at every clock time of the day."""
    return {minute: watts for minute in GRID}


def spent_wh(result: PlanResult) -> float:
    """How much of the reference window the projection spent, in watt-hours."""
    return (55.0 - result.trajectory[-1][1]) * PER_POINT


def sim(
    load: dict[int, float],
    solar: dict[int, float] | None = None,
    *,
    soc: float = 55.0,
    ah: float = 100.0,
    charge_w: float = 0.0,
    discharge_w: float = 5000.0,
    start: str = "2026-01-05T22:00",
    end: str = "2026-01-06T08:00",
    windows: tuple[tuple[datetime, datetime, float], ...] = (),
) -> PlanResult:
    return simulate(
        soc,
        ah,
        10.0,
        1.0,
        charge_w,
        discharge_w,
        load,
        solar,
        at(start),
        at(end),
        NY,
        windows,
    )


def plan(
    curves: list[dict[int, float]],
    essential: dict[int, float] | None = None,
    scheduled: tuple[datetime, int, float] | None = None,
    *,
    solar: dict[int, float] | None = None,
    severity: str | None = None,
    band: float | None = None,
    ah: float | None = 100.0,
    soc: float = 55.0,
    efficiency: float = 1.0,
    min_soc: float = 10.0,
    start: str = "2026-01-05T22:00",
    end: str = "2026-01-06T08:00",
) -> PlanSummary:
    return build_plan(
        soc,
        ah,
        min_soc,
        efficiency,
        0.0,
        5000.0,
        curves,
        essential,
        scheduled,
        solar,
        at(start),
        at(end),
        NY,
        severity,
        band,
    )


def test_night_curves_give_one_curve_per_usable_night() -> None:
    rows = (
        sweep("2026-01-03T12:00", 250.0)
        + sweep("2026-01-04T12:00", 250.0)
        + sweep("2026-01-05T12:00", 250.0)
    )
    curves = night_curves(rows, NY)
    assert len(curves) == 3
    assert set(curves[0]) == set(GRID)
    assert all(value == 250.0 for curve in curves for value in curve.values())


def test_a_thin_night_is_dropped_rather_than_averaged_in() -> None:
    good = [sweep(f"2026-01-0{day}T12:00", 250.0) for day in (3, 4, 5)]
    thin = sweep("2026-01-06T12:00", 999.0, skip=frozenset(range(100, 288)))
    curves = night_curves([row for night in good for row in night] + thin, NY)
    assert len(curves) == 3
    assert all(999.0 not in curve.values() for curve in curves)
    # Two answered nights are not enough to call anything typical.
    assert night_curves([row for night in good[:2] for row in night] + thin, NY) == []


def test_only_the_last_seven_nights_are_comparable() -> None:
    nights = [f"2026-01-{day:02d}T12:00" for day in range(1, 10)]
    rows = [row for index, night in enumerate(nights) for row in sweep(night, 100.0 + index)]
    curves = night_curves(rows, NY)
    assert len(curves) == 7
    assert all(100.0 not in curve.values() and 101.0 not in curve.values() for curve in curves)


def test_a_silent_night_still_holds_its_place_in_the_window() -> None:
    # Three complete nights end at Jan 3, then a week of dates nobody recorded
    # and a night whose every read came back silence. The silent night is a
    # real night and the most recent one, so it anchors the seven-date window
    # at its own date and only Jan 3's curve reaches the median. Filtering
    # unanswered rows before naming their night would end the window at the
    # older history instead and call a dead fortnight past.
    old = (
        sweep("2025-12-31T12:00", 250.0)
        + sweep("2026-01-01T12:00", 250.0)
        + sweep("2026-01-02T12:00", 250.0)
    )
    silent: list[tuple[datetime, float | None]] = [
        (when, None) for when, _ in sweep("2026-01-08T12:00", 250.0)
    ]
    assert night_curves(old + silent, NY) == []
    # The same three nights with nothing silent after them are three
    # comparable nights, which pins the silent week as the reason.
    assert len(night_curves(old, NY)) == 3


def test_two_reads_in_one_step_are_one_answer_at_their_mean() -> None:
    # Each pair of rows sits inside one five-minute step: 12:00 with 12:01,
    # 12:05 with 12:06, and so on. Counted to the second these are 288
    # answers of a 288-step night; counted by the step they are 144 -- still
    # half, still usable -- and the curve carries their mean at each step
    # rather than the two readings as two answers.
    good = (
        sweep("2026-01-03T12:00", 250.0)
        + sweep("2026-01-04T12:00", 250.0)
        + sweep("2026-01-05T12:00", 250.0)
    )
    pairs: list[tuple[datetime, float | None]] = []
    first = at("2026-01-06T12:00").astimezone(UTC)
    for step in range(144):
        start = (first + timedelta(seconds=STEP_SECONDS * step)).astimezone(NY)
        pairs.append((start, 100.0))
        pairs.append(((start + timedelta(minutes=1)).astimezone(NY), 300.0))
    curves = night_curves(good + pairs, NY)
    assert len(curves) == 4
    paired = curves[-1]
    assert len(paired) == 144
    assert all(value == pytest.approx(200.0) for value in paired.values())


def test_recent_silence_is_not_backfilled_from_older_history() -> None:
    # Three complete nights, four dates nobody recorded at all, then one thin
    # night. Seven consecutive nights ending at that thin night reach back only
    # as far as Jan 3, so two comparable nights is the answer and the page has to
    # say so. Reading the window as "the seven most recent nights with rows"
    # instead reaches past the silent fortnight and calls four dead dates evidence.
    old = [sweep(f"2026-01-{day:02d}T12:00", 250.0) for day in (1, 2, 3)]
    thin = sweep("2026-01-08T18:00", 999.0, count=6)
    assert night_curves([row for night in old for row in night] + thin, NY) == []
    # The same three nights with nothing silent after them are three comparable
    # nights, which is what pins the four empty dates as the reason.
    assert len(night_curves([row for night in old for row in night], NY)) == 3


def test_duplicated_timestamps_are_one_answer_and_not_two() -> None:
    # A minute tier that answered the same step twice is one step of evidence.
    # Counting the rows instead of the instants would let 100 real steps of a
    # 288 step night look like a usable night, at twice the weight it earns.
    thin = sweep("2026-01-06T12:00", 999.0, count=100)
    rows = [row for day in (3, 4, 5) for row in sweep(f"2026-01-0{day}T12:00", 250.0)]
    curves = night_curves(rows + thin + thin, NY)
    assert len(curves) == 3
    assert all(999.0 not in curve.values() for curve in curves)


def test_a_night_is_measured_against_its_own_step_count() -> None:
    # Nov 1 is the 25 hour night and Mar 8 the 23 hour one. Half of 300 is not
    # half of 288 and half of 276 is not either, so both denominators are walked
    # from the night's own real steps rather than assumed from a flat day.
    good = [sweep("2026-10-29T12:00", 250.0), sweep("2026-10-30T12:00", 250.0)]
    thin = sweep("2026-10-31T12:00", 999.0, count=145)
    assert night_curves([row for night in good for row in night] + thin, NY) == []
    enough = sweep("2026-10-31T12:00", 250.0, count=151)
    assert len(night_curves([row for night in good for row in night] + enough, NY)) == 3

    spring = [sweep("2026-03-05T12:00", 250.0), sweep("2026-03-06T12:00", 250.0)]
    short = sweep("2026-03-07T12:00", 250.0, count=140)
    assert len(night_curves([row for night in spring for row in night] + short, NY)) == 3


def test_silence_is_skipped_and_never_read_as_a_zero_load() -> None:
    skip = frozenset(step for step in range(288) if step % 4 == 3)
    rows = (
        sweep("2026-01-03T12:00", 250.0)
        + sweep("2026-01-04T12:00", 250.0)
        + sweep("2026-01-05T12:00", 250.0, skip=skip)
    )
    curves = night_curves(rows, NY)
    assert len(curves) == 3
    assert len(curves[-1]) == 288 - len(skip)
    assert 0.0 not in curves[-1].values()
    # A night that went unanswered for a third of its clock is a gap and not a
    # quiet house, and two comparable nights are not yet enough to call anything
    # typical.
    thin = sweep("2026-01-05T12:00", 250.0, skip=frozenset(range(200, 288)))
    assert night_curves(thin, NY) == []


def test_a_night_is_cut_on_the_local_calendar() -> None:
    # November 1, 2026 is the 25 hour night in New York: the clocks go back at
    # 02:00 that morning, so the night that starts at noon on Oct 31 costs 300
    # steps of real time and still shows every clock minute once. Cut on UTC or
    # on local midnight it splits in two, and both halves come out too thin.
    rows = (
        sweep("2026-10-30T12:00", 250.0)
        + sweep("2026-10-31T12:00", 250.0, count=300)
        + sweep("2026-11-01T12:00", 250.0)
    )
    curves = night_curves(rows, NY)
    assert len(curves) == 3
    # The middle night is the 25 hour one and it keeps all 288 clock minutes.
    assert set(curves[1]) == set(GRID)


def test_a_repeated_clock_minute_is_averaged_and_answers_twice() -> None:
    # The two passes through 01:xx differ by an hour of real time: the first is
    # still on summer time, so the offsets name which pass a reading came from.
    # Averaging them is the energy-honest reading -- the clock minute stands for
    # both passes and a mean spends the same watt-hours across the two of them --
    # and the night is charged for one minute of clock, not two.
    early = timedelta(hours=-4)
    rows = [
        (
            when,
            100.0
            if when.hour == 1 and when.utcoffset() == early
            else 300.0
            if when.hour == 1
            else 250.0,
        )
        for when, _ in sweep("2026-10-31T12:00", 250.0, count=300)
    ]
    twice = [row for row in rows if row[0].hour == 1 and row[0].minute == 30]
    assert len(twice) == 2
    assert twice[0][0].utcoffset() != twice[1][0].utcoffset()
    curves = night_curves(
        sweep("2026-10-30T12:00", 250.0) + rows + sweep("2026-11-01T12:00", 250.0),
        NY,
    )
    assert len(curves) == 3
    assert len(curves[1]) == 288
    assert all(curves[1][minute] == pytest.approx(200.0) for minute in range(60, 120, 5))


def test_the_typical_curve_is_a_median_not_a_mean() -> None:
    typical = median_curve([flat(100.0), flat(100.0), flat(4000.0)])
    assert all(value == 100.0 for value in typical.values())
    # The mean would sit at 1400 W and spend the whole battery by teatime because
    # somebody ran a pool pump once.
    assert median_curve([flat(100.0), flat(200.0)])[0] == 150.0
    assert median_curve([]) == {}


def test_a_minute_some_nights_lack_is_median_over_the_nights_that_answer_it() -> None:
    assert median_curve([{600: 100.0}, {600: 200.0, 605: 500.0}]) == {600: 150.0, 605: 500.0}


def test_a_gap_in_the_record_carries_the_last_answered_step_forward() -> None:
    # Every night answered 120 W until 02:00 and then went quiet, and the quiet
    # is not a quiet house. Read as a zero the battery would coast for the last
    # two hours of the night at 55 percent, which is a projection of nothing.
    load = {minute: 120.0 for minute in range(0, 120, 5)}
    result = sim(load, start="2026-01-06T00:00", end="2026-01-06T04:00")
    assert len(result.trajectory) == 49
    assert result.trajectory[-1][1] == pytest.approx(46.0)
    assert result.trajectory[30][1] < result.trajectory[24][1]


def test_a_leading_gap_is_read_from_the_first_step_that_did_answer() -> None:
    # The house was watched from 00:05 and not before. Silence at the front of
    # the record is the same absence of evidence as silence in the middle, so the
    # first answered step reaches backwards rather than the walk starting flat.
    load = {minute: 120.0 for minute in range(15, 240, 5)}
    result = sim(load, start="2026-01-06T00:00", end="2026-01-06T04:00")
    assert result.trajectory[1][1] < 55.0
    assert result.trajectory[-1][1] == pytest.approx(46.0)


def test_a_curve_that_never_answers_credits_no_load() -> None:
    # Nothing to carry is a different case from a gap to carry across: with no
    # answer anywhere there is no measured draw to keep alive, and inventing one
    # would drain a battery out of an empty record.
    assert sim({}, start="2026-01-06T00:00", end="2026-01-06T04:00").trajectory[-1][1] == 55.0


def test_a_window_answered_elsewhere_carries_that_answer_into_the_window() -> None:
    # The record answered 250 W at 20:00 and 100 W at 03:00 and nothing
    # between. A projection of the 22:00 hour reads the 20:00 answer -- the
    # last answered clock key before the walk starts, sought cyclically
    # through the whole curve -- rather than inventing a silent house out of a
    # two-hour hole in the record.
    result = sim({1200: 250.0, 180: 100.0}, start="2026-01-05T22:00", end="2026-01-05T23:00")
    assert spent_wh(result) == pytest.approx(250.0)
    # A curve whose only answer is 23:30 is still an answer before a 22:00
    # walk: read cyclically, it wrapped through midnight into the evening.
    wrapped = sim({1410: 300.0}, start="2026-01-05T22:00", end="2026-01-05T23:00")
    assert spent_wh(wrapped) == pytest.approx(300.0)


def test_a_projection_starts_and_stops_on_the_step_grid() -> None:
    # A 22:02 start on a curve keyed 22:00, 22:05 and so on must still read the
    # 22:00 step, and a horizon that stops at 08:02 still owes 120 seconds of the
    # step that runs to 08:05. Both edges belong on the grid the record is keyed
    # by, or the first and last minutes of a plan are guessed at.
    result = sim(flat(120.0), start="2026-01-05T22:02", end="2026-01-06T08:02")
    assert len(result.trajectory) == 122
    assert result.trajectory[0][0] == at("2026-01-05T22:00")
    assert result.trajectory[-1][0] == at("2026-01-06T08:05")
    # A fifth of a step at each end and one hundred and twenty whole steps
    # between them: 120.0 step-equivalents at 10 Wh a step, not the 120.4 that
    # charging the whole first step would cost.
    assert spent_wh(result) == pytest.approx(1200.0)
    assert result.trajectory[-1][1] == pytest.approx(32.5)


def test_a_late_start_pays_only_for_the_time_the_plan_actually_runs() -> None:
    # A constant 120 W from 22:02 to 23:02 is one hour of energy. Charging the
    # whole 22:00 step for it overstates by up to a step, which is what a
    # short projection was doing.
    result = sim(flat(120.0), start="2026-01-05T22:02", end="2026-01-05T23:02")
    assert spent_wh(result) == pytest.approx(120.0)


def test_a_scheduled_load_is_priced_by_instant_and_not_by_clock_time() -> None:
    windows = tuple((s, f, 120.0) for s, f in scheduled_windows(at("2026-01-05T21:37"), 3600, NY))
    assert len(windows) == 1
    assert windows[0][0] == at("2026-01-05T21:37").astimezone(UTC)
    assert windows[0][1] == at("2026-01-05T22:37").astimezone(UTC)

    # A run from 23:30 for an hour is twelve steps of real time that crosses
    # midnight as real time, not a lookup that has to remember the day changed.
    wrapped = scheduled_windows(at("2026-01-05T23:30"), 3600, NY)
    assert len(wrapped) == 1
    assert wrapped[0][0] == at("2026-01-05T23:30").astimezone(UTC)
    assert wrapped[0][1] == at("2026-01-06T00:30").astimezone(UTC)


def test_an_unaligned_schedule_costs_exactly_its_seconds() -> None:
    # A one-hour run from 21:37 covers parts of thirteen steps: three and a
    # half minutes in the first, whole minutes in the middle, two minutes in
    # the last. Charging whole windows priced it at 65/60 of an hour of the
    # load; carrying the exact seconds prices exactly one hour, however the
    # grid falls.
    windows = tuple((s, f, 120.0) for s, f in scheduled_windows(at("2026-01-05T21:37"), 3600, NY))
    result = sim({}, start="2026-01-05T21:30", end="2026-01-05T23:00", windows=windows)
    assert spent_wh(result) == pytest.approx(120.0)


def test_a_scheduled_window_is_not_charged_twice_across_the_repeated_hour() -> None:
    # The repeated 01:xx hour is the trap. A schedule keyed by clock minutes puts
    # its watts at 01:00 through 01:55, and the walk visits those minutes twice,
    # so an hour of pump pays for two hours of draw. Keyed by instants the
    # schedule costs exactly what it runs for, whether it starts inside the
    # repeated hour, ends inside it, or spans it.
    for start, seconds in (
        ("2026-11-01T01:15", 3600),
        ("2026-11-01T00:50", 3600),
        ("2026-11-01T01:00", 7200),
    ):
        windows = tuple((s, f, 120.0) for s, f in scheduled_windows(at(start), seconds, NY))
        result = sim({}, start="2026-10-31T22:00", end="2026-11-01T07:00", windows=windows)
        assert spent_wh(result) == pytest.approx(120.0 * seconds / 3600.0, abs=1e-6)


def test_a_scheduled_window_skips_a_clock_hour_that_never_happened() -> None:
    # The window is one real hour of instants: on the spring-forward morning it
    # starts in 01:xx EST and ends in 03:xx EDT, and the clock hour that never
    # happened is simply never visited.
    windows = scheduled_windows(at("2026-03-08T01:50"), 3600, NY)
    assert len(windows) == 1
    start, finish = windows[0]
    assert (finish - start).total_seconds() == 3600, "one real hour of schedule"
    hours = set()
    step = start
    while step < finish:
        hours.add(step.astimezone(NY).hour)
        step += timedelta(minutes=5)
    assert 2 not in hours and 3 in hours


def test_essential_profile_sums_circuits_and_adds_the_allowance() -> None:
    assert essential_profile([{600: 100.0}, {600: 50.0, 605: 20.0}], 25.0) == {
        600: 175.0,
        605: 45.0,
    }


def test_essential_profile_without_circuits_is_the_allowance_alone() -> None:
    assert essential_profile(None, 25.0) == {minute: 25.0 for minute in GRID}
    assert essential_profile([], 25.0) == essential_profile(None, 25.0)


def test_a_projection_needs_something_to_drain() -> None:
    assert plan_status(None, 100.0, 7).status == "ok"
    no_battery = plan_status(None, None, 7)
    assert no_battery.status == "estimate_unavailable"
    assert no_battery.reason is not None
    assert "capacity" in no_battery.reason
    assert plan_status(None, 0.0, 7).status == "estimate_unavailable"
    # Drift outranks a missing battery. Both are reasons to refuse, and the one
    # that makes every number untrustworthy is the one worth reading first.
    drifted = plan_status("elevated", None, 7)
    assert drifted.reason is not None
    assert "elevated" in drifted.reason


def test_the_gate_knows_what_a_projection_refuses_on() -> None:
    # The three figures below are the ones a projection refuses on inside
    # ``simulate``. The gate has to refuse on them too, or the page asks a
    # question, gets "ok" back, and then finds no curve where the curve should be.
    assert plan_status(None, 100.0, 7, 1.0, 10.0, 36000.0).status == "ok"
    no_rate = plan_status(None, 100.0, 7, 0.0, 10.0, 36000.0)
    assert no_rate.status == "estimate_unavailable"
    assert "efficiency" in (no_rate.reason or "")
    empty = plan_status(None, 100.0, 7, 1.0, 100.0, 36000.0)
    assert empty.status == "estimate_unavailable"
    assert "window is empty" in (empty.reason or "")
    stub = plan_status(None, 100.0, 7, 1.0, 10.0, 180.0)
    assert stub.status == "estimate_unavailable"
    assert "shorter than one" in (stub.reason or "")


def test_thin_history_refuses_to_look_like_a_typical_night() -> None:
    refused = plan_status(None, 100.0, 2)
    assert refused.status == "estimate_unavailable"
    assert refused.reason is not None
    assert "night" in refused.reason
    assert plan_status(None, 100.0, 3).status == "ok"


def test_drift_ladder_decides_whether_there_is_an_answer_at_all() -> None:
    for severity in (None, "none", "info", "warning"):
        assert plan_status(severity, 100.0, 7).status == "ok"
    for severity in ("elevated", "alert"):
        assert plan_status(severity, 100.0, 7).status == "estimate_unavailable"


def test_the_usable_window_drains_at_the_rate_the_load_draws() -> None:
    result = sim(flat(500.0))
    assert result.status == "ok"
    assert len(result.trajectory) == 121
    assert result.trajectory[0] == (at("2026-01-05T22:00"), 55.0)
    # 4.8 kWh of usable window and 90 points of SoC to spend it on, so a step
    # costs its watt-hours over 53.33. The 58th step is the one that runs short.
    assert result.reserve_crossing == at("2026-01-06T02:50")
    assert result.import_start == at("2026-01-06T02:45")
    assert result.trajectory[-1][1] == pytest.approx(10.0)
    assert all(soc == pytest.approx(10.0) for _, soc in result.trajectory[58:])


def test_a_discharge_cap_buys_import_before_the_floor_is_reached() -> None:
    result = sim(flat(500.0), discharge_w=200.0)
    assert result.reserve_crossing is None
    assert result.import_start == at("2026-01-05T22:00")
    assert result.trajectory[-1][1] == pytest.approx(17.5)


def test_surplus_solar_charges_only_within_the_charge_limit() -> None:
    load, solar = flat(300.0), flat(500.0)
    blocked = sim(load, solar, end="2026-01-06T02:00")
    assert all(soc == pytest.approx(55.0) for _, soc in blocked.trajectory)
    assert blocked.import_start is None
    charging = sim(load, solar, charge_w=1000.0, end="2026-01-06T02:00")
    assert charging.trajectory[-1][1] == pytest.approx(70.0)


def test_a_fall_back_night_serves_its_repeated_hour_twice() -> None:
    # The clocks go back at 02:00 on Nov 1, so a projection from 22:00 on Oct 31
    # walks 120 steps across what the clock only counts as nine hours. Walking
    # clock times instead would skip the second pass through 01:xx, end the
    # trajectory at the wrong minute, and understate the night.
    result = sim(flat(120.0), start="2026-10-31T22:00", end="2026-11-01T07:00")
    assert len(result.trajectory) == 121
    assert result.trajectory[-1][1] == pytest.approx(32.5)
    twice = [when for when, _ in result.trajectory if when.hour == 1 and when.minute == 30]
    assert len(twice) == 2
    assert twice[0].utcoffset() != twice[1].utcoffset()


def test_a_spring_forward_night_never_consults_a_clock_time_that_did_not_happen() -> None:
    # 02:00 to 03:00 local does not exist on March 8. A projection that walked
    # clock times would spend 48 steps over four clock hours; the walk over real
    # instants spends 36 and never asks what the missing hour was drawing.
    result = sim(flat(240.0), start="2026-03-08T00:00", end="2026-03-08T04:00")
    assert len(result.trajectory) == 37
    assert result.trajectory[-1][1] == pytest.approx(41.5)
    assert not [when for when, _ in result.trajectory if when.hour == 2]


def test_a_battery_at_the_floor_has_already_crossed_it() -> None:
    # At the reserve floor with the house drawing, the answer is not "no crossing":
    # the battery is at reserve as the plan begins and the grid takes the load
    # from the first second. A plan below the floor is clamped up and reads the
    # same way, because a floor is not a floor you can be under.
    crossed = sim(flat(500.0), soc=10.0)
    assert crossed.reserve_crossing == at("2026-01-05T22:00")
    assert crossed.import_start == at("2026-01-05T22:00")
    assert crossed.trajectory[-1][1] == pytest.approx(10.0)
    assert sim(flat(500.0), soc=4.0).reserve_crossing == at("2026-01-05T22:00")
    # Nothing to draw is not a crossing, it is a flat line.
    assert sim({}, soc=10.0).reserve_crossing is None


def test_the_gate_runs_before_any_arithmetic() -> None:
    curves = [flat(500.0)] * 3
    drifted = plan(curves, severity="alert")
    assert drifted.status == "estimate_unavailable"
    assert drifted.reason is not None and "alert" in drifted.reason
    assert drifted.scenarios == {}
    assert drifted.central.trajectory == ()
    assert drifted.central.reserve_crossing is None
    assert drifted.reserve_window is None
    assert drifted.range_basis == ""
    # A missing battery is refused with what to do about it, not with a number.
    bare = plan(curves, ah=None)
    assert bare.status == "estimate_unavailable"
    assert any("capacity" in text for text in bare.assumptions)
    assert plan(curves[:2]).status == "estimate_unavailable"
    assert plan([]).status == "estimate_unavailable"


def test_a_refusal_is_not_wrapped_in_an_ok_summary() -> None:
    # Each of these is a reason the arithmetic itself refuses. None of them may
    # come back as a summary that says "ok" with a range derived from a curve
    # that never ran, because that is a number that only looks like an answer.
    curves = [flat(500.0)] * 3
    for refused in (
        plan(curves, efficiency=0.0),
        plan(curves, min_soc=100.0),
        plan(curves, end="2026-01-05T22:03"),
    ):
        assert refused.status == "estimate_unavailable"
        assert refused.reason is not None
        assert refused.scenarios == {}
        assert refused.central.trajectory == ()
        assert refused.central.reserve_crossing is None
        assert refused.reserve_window is None
        assert refused.range_basis == ""


def test_the_range_is_the_p25_to_p75_of_the_night_crossings() -> None:
    # Four nights that reach the floor at 00:00, 01:00, 02:00 and 03:00. The
    # window is interpolated between them, so it starts at 00:45 and ends at
    # 02:15. Earliest and latest would have reported 00:00 to 03:00, which lets
    # two odd nights set the width of every plan that follows them.
    summary = plan([flat(480.0), flat(600.0), flat(800.0), flat(1200.0)])
    assert summary.status == "ok"
    assert list(summary.scenarios) == ["typical"]
    assert summary.scenarios["typical"] is summary.central
    assert summary.central.reserve_crossing == at("2026-01-06T01:30")
    assert summary.reserve_window == (at("2026-01-06T00:45"), at("2026-01-06T02:15"))
    assert "p25" in summary.range_basis
    assert "interpolated" in summary.range_basis
    assert not any("drift" in text for text in summary.assumptions)
    assert not any("25%" in text for text in summary.assumptions)


def test_the_range_interpolates_between_real_instants_across_the_fold() -> None:
    # Six nights cross the floor at 00:00, 01:30 (first pass), 01:20 (second
    # pass), 02:00, 03:00 and 03:55. Sorted and interpolated on New York wall
    # clock, the second-pass 01:20 sorts before the first-pass 01:30 and the
    # repeated hour reads as a negative gap, so the interpolated start lands
    # outside the two real crossings it is supposed to sit between. Sorted and
    # interpolated as instants it sits between them, which is where a p25 of
    # those crossings belongs.
    summary = plan(
        [flat(1200.0), flat(700.0), flat(560.0), flat(480.0), flat(400.0), flat(350.0)],
        start="2026-10-31T22:00",
        end="2026-11-01T07:00",
    )
    assert summary.status == "ok"
    assert summary.reserve_window is not None
    low, high = summary.reserve_window
    # The four crossings inside the fold are two pairs of one clock hour two
    # real hours wide: the window's ends interpolate through real time, not
    # through a wall-clock reading that names two different instants.
    assert low == at("2026-11-01T01:42:30")
    assert high == at("2026-11-01T02:45")


def test_nights_that_never_reach_the_floor_leave_the_range_open() -> None:
    # Two nights cross and two do not, which is half of the window, and a half is
    # more than a quarter. The later bound goes None rather than staying at the
    # last crossing that happened, because a night that outlasts the horizon is a
    # wider answer than the horizon is and has to be said out loud.
    summary = plan([flat(1200.0), flat(480.0), flat(120.0), flat(120.0)])
    assert summary.status == "ok"
    assert summary.reserve_window is not None
    low, high = summary.reserve_window
    assert low == at("2026-01-06T00:45")
    assert high is None
    assert any("25%" in text for text in summary.assumptions)


def test_a_range_on_one_crossing_night_says_so() -> None:
    summary = plan([flat(1200.0), flat(120.0), flat(120.0)])
    assert summary.reserve_window is not None
    low, high = summary.reserve_window
    assert low == at("2026-01-06T00:00")
    assert high is None
    assert any("single night" in text for text in summary.assumptions)


def test_no_night_reaches_the_floor_and_no_window_is_claimed() -> None:
    summary = plan([flat(120.0)] * 3)
    assert summary.status == "ok"
    assert summary.reserve_window is None
    assert summary.range_basis == ""
    assert summary.central.reserve_crossing is None
    assert any("reserve" in text for text in summary.assumptions)


def test_a_warning_widens_the_reported_curve_by_the_measured_band() -> None:
    summary = plan([flat(120.0)] * 3, severity="warning", band=4.0)
    assert summary.status == "ok"
    assert len(summary.central.trajectory_band) == len(summary.central.trajectory)
    widths = [high - low for _, low, high in summary.central.trajectory_band]
    assert all(width == pytest.approx(8.0) for width in widths)
    assert any("disagreement between the packs" in text for text in summary.assumptions)


def test_a_warning_widens_the_reserve_window_at_both_ends() -> None:
    curves = [flat(600.0)] * 3
    plain = plan(curves)
    assert plain.reserve_window == (at("2026-01-06T02:00"), at("2026-01-06T02:00"))
    assert plain.central.trajectory_band == ()

    banded = plan(curves, severity="warning", band=4.0)
    assert banded.reserve_window is not None
    low, high = banded.reserve_window
    assert low is not None and low < at("2026-01-06T02:00")
    assert high is not None and high > at("2026-01-06T02:00")
    assert "widened at both ends" in banded.range_basis


def test_an_invalid_drift_magnitude_goes_the_none_path() -> None:
    # A drift band is a magnitude: a negative or non-finite width says nothing
    # about how far apart the packs are and takes the same path as no width --
    # no band on the curve, no widening of the window, and an assumption
    # naming the missing figure. A negative one in particular must not invert
    # the reported band or the simulated runs.
    for invalid in (-4.0, float("inf"), float("nan")):
        refused = plan([flat(600.0)] * 3, severity="warning", band=invalid)
        assert refused.status == "ok"
        assert refused.central.trajectory_band == ()
        assert any("no drift magnitude" in text for text in refused.assumptions)
        assert not any("plus or minus" in text for text in refused.assumptions)
    # And a finite non-negative width is used exactly as given.
    used = plan([flat(600.0)] * 3, severity="warning", band=1.5)
    assert used.central.trajectory_band != ()


def test_a_warning_without_a_drift_magnitude_says_the_range_omits_it() -> None:
    warned = plan([flat(600.0)] * 3, severity="warning")
    assert warned.status == "ok"
    assert warned.central.trajectory_band == ()
    assert warned.reserve_window == (at("2026-01-06T02:00"), at("2026-01-06T02:00"))
    assert any("drift" in text for text in warned.assumptions)
    assert any("does not include" in text for text in warned.assumptions)


def test_the_three_scenarios_carry_their_own_caveats() -> None:
    curves = [flat(700.0), flat(500.0), flat(350.0)]
    summary = plan(
        curves,
        essential=flat(200.0),
        scheduled=(at("2026-01-06T02:00"), 3600, 1500.0),
    )
    assert set(summary.scenarios) == {"typical", "essential", "scheduled"}
    assert summary.scenarios["essential"].reserve_crossing is None
    assert summary.scenarios["essential"].import_start is None
    assert any("measurement" in text for text in summary.scenarios["essential"].assumptions)
    crossed = summary.scenarios["scheduled"].reserve_crossing
    central = summary.central.reserve_crossing
    assert crossed is not None and central is not None
    assert crossed < central
    assert any("already" in text for text in summary.scenarios["scheduled"].assumptions)


def test_the_assumptions_say_what_the_model_assumed() -> None:
    summary = plan([flat(500.0)] * 3)
    joined = " ".join(summary.assumptions)
    assert "silent" in joined
    assert "carries the last answered step forward" in joined
    assert "grid is assumed available" in joined
    assert any("48" in text for text in summary.assumptions)
    assert any("p95" in text and "five-minute" in text for text in summary.assumptions)
    assert any("round-trip" in text for text in summary.assumptions)
    warned = plan([flat(500.0)] * 3, severity="warning")
    assert warned.status == "ok"
    assert any("drift" in text for text in warned.assumptions)
