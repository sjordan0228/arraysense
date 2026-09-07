"""The overnight planner's estimation core, tested as arithmetic.

Nothing here opens a store or a page: rows go in, curves and trajectories come
out. That split is the point. Every question this feature answers -- how long
the battery lasts, when the household starts importing, which nights count as
comparable -- is decided by bookkeeping in ``overnight.py``, and a change to
that bookkeeping has to surface as a failed assertion rather than as a page that
looks slightly different.

Four conventions get pinned here because they are decisions rather than
incidents:

* A night is cut on the installation's local calendar and runs noon to noon, so
  a 25 hour fall-back night stays one comparable night.
* A row that reads ``None`` is silence. It is skipped, never turned into a zero,
  because an unanswered step is not evidence that the house drew nothing.
* The typical curve is a median of whole nights. One unusual night must not move
  the floor of the whole plan.
* The simulation walks real instants and looks up load by local clock time, so a
  spring-forward night is shorter than its clock times suggest, and a repeated
  hour is served twice.

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
    scheduled_delta,
    simulate,
)

NY = ZoneInfo("America/New_York")
GRID = list(range(0, 1440, 5))


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
    )


def plan(
    curves: list[dict[int, float]],
    essential: dict[int, float] | None = None,
    scheduled: tuple[datetime, int, float] | None = None,
    *,
    solar: dict[int, float] | None = None,
    severity: str | None = None,
    ah: float | None = 100.0,
    soc: float = 55.0,
    start: str = "2026-01-05T22:00",
    end: str = "2026-01-06T08:00",
) -> PlanSummary:
    return build_plan(
        soc,
        ah,
        10.0,
        1.0,
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
    # Reading those gaps as zero would fill a night with a load nobody drew. A
    # night that went unanswered for a third of its clock is not a quiet house,
    # it is a gap, and two comparable nights are not yet enough to call anything
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


def test_a_repeated_clock_time_keeps_its_first_occurrence() -> None:
    # The two passes through 01:xx differ by an hour of real time: the first is
    # still on summer time, so the offsets name which pass a reading came from.
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
    assert all(curves[1][minute] == 100.0 for minute in range(60, 120, 5))


def test_the_typical_curve_is_a_median_not_a_mean() -> None:
    typical = median_curve([flat(100.0), flat(100.0), flat(4000.0)])
    assert all(value == 100.0 for value in typical.values())
    # The mean would sit at 1400 W and spend the whole battery by teatime because
    # somebody ran a pool pump once.
    assert median_curve([flat(100.0), flat(200.0)])[0] == 150.0
    assert median_curve([]) == {}


def test_a_minute_some_nights_lack_is_median_over_the_nights_that_answer_it() -> None:
    assert median_curve([{600: 100.0}, {600: 200.0, 605: 500.0}]) == {600: 150.0, 605: 500.0}


def test_scheduled_load_adds_over_its_window_and_nowhere_else() -> None:
    base = flat(200.0)
    raised = scheduled_delta(base, at("2026-01-05T21:37"), 3600, 1500.0, NY)
    window = set(range(1295, 1360, 5))
    assert {minute for minute, value in raised.items() if value != 200.0} == window
    assert all(raised[minute] == 1700.0 for minute in window)
    assert base[1295] == 200.0


def test_scheduled_load_wraps_past_midnight() -> None:
    raised = scheduled_delta({}, at("2026-01-05T23:30"), 3600, 1200.0, NY)
    assert set(raised) == set(range(1410, 1440, 5)) | set(range(0, 30, 5))
    assert all(value == 1200.0 for value in raised.values())


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
    # walks through 01:xx twice: two real hours pass while the clock shows the
    # same hour, and the loads draw in both. Reading it once understates the night
    # by 960 Wh.
    load = {minute: 480.0 for minute in range(60, 120, 5)}
    result = sim(load, start="2026-10-31T22:00", end="2026-11-01T07:00")
    assert len(result.trajectory) == 121
    assert result.trajectory[-1][1] == pytest.approx(37.0)
    twice = [when for when, _ in result.trajectory if when.hour == 1 and when.minute == 30]
    assert len(twice) == 2
    assert twice[0].utcoffset() != twice[1].utcoffset()


def test_a_spring_forward_night_never_consults_a_clock_time_that_did_not_happen() -> None:
    # 02:00 to 03:00 local does not exist on March 8. A huge load parked in that
    # hour is the trap: a projection that walked clock times would drink it and
    # report a reserve crossing no battery could produce.
    load = {minute: 6000.0 for minute in range(120, 180, 5)}
    result = sim(load, start="2026-03-08T00:00", end="2026-03-08T04:00")
    assert len(result.trajectory) == 37
    assert all(soc == pytest.approx(55.0) for _, soc in result.trajectory)
    assert result.reserve_crossing is None
    assert not [when for when, _ in result.trajectory if when.hour == 2]


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


def test_the_range_spans_the_per_night_crossings() -> None:
    curves = [flat(700.0), flat(500.0), flat(350.0)]
    summary = plan(curves)
    assert summary.status == "ok"
    assert list(summary.scenarios) == ["typical"]
    assert summary.scenarios["typical"] is summary.central
    assert summary.central.reserve_crossing == at("2026-01-06T02:50")
    assert summary.reserve_window == (at("2026-01-06T01:30"), at("2026-01-06T04:55"))
    # The width is the spread of the nights themselves. Nothing here is a
    # percentile, and the field that says so has to keep saying it.
    assert "p25" not in summary.range_basis.lower()
    assert "night" in summary.range_basis.lower()
    assert not any("drift" in text for text in summary.assumptions)


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
    crossed = summary.scenarios["scheduled"].reserve_crossing
    central = summary.central.reserve_crossing
    assert crossed is not None and central is not None
    assert crossed < central
    assert any("already" in text for text in summary.scenarios["scheduled"].assumptions)
    assert any("assumption" in text for text in summary.scenarios["essential"].assumptions)


def test_the_assumptions_say_what_the_model_assumed() -> None:
    summary = plan([flat(500.0)] * 3)
    joined = " ".join(summary.assumptions)
    assert "silent" in joined
    assert "grid is assumed available" in joined
    assert any("48" in text for text in summary.assumptions)
    warned = plan([flat(500.0)] * 3, severity="warning")
    assert warned.status == "ok"
    assert any("drift" in text for text in warned.assumptions)
