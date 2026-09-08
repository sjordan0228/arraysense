"""Replay: re-run the projection over nights that have already happened.

A projection is only as good as the error you can show for it. This file
holds the nights up against themselves: the typical curve is rebuilt from
the prior seven nights only, the projection runs from the night's 22:00 to
its 07:00 as it would have been run at the time, and the recorded battery
state of charge says what the night actually did. The per-night errors are
what later slices report as the planner's honest uncertainty.

Two things are decisions rather than incidents, and the tests pin them:

* A replayed projection sees only what the record held before it. The first
  seven nights of a record are never replayed because the planner would not
  have had its seven-night window yet, and the replayed night's own load is
  not allowed into the curve that projects it.
* A hole in the SoC record is silence, not a slow night. A crossing that
  falls inside a hole is reported as unknown rather than guessed at, and the
  night's energy comparison uses the readings that exist, never zeros.

Fixtures walk real time and use the real 2026 daylight dates: the
fall-back night is the one containing November 1 (the clocks go back at
02:00) and the spring night is the one containing March 8. The reference
battery is the one from the design doc: 100 Ah of usable window on a 48 V
nominal bus, 4.8 kWh spread over the 90 points between a 10 percent floor
and full charge.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from arraysense.overnight import (
    NightReplay,
    actual_crossing,
    crossing_errors,
    replay_nights,
    wh_errors,
)

NY = ZoneInfo("America/New_York")

# One point of state of charge on the reference battery, in watt-hours.
PER_POINT = 100.0 * 48.0 / 90.0
STEP = timedelta(minutes=5)

# Eight consecutive nights in May 2026: no clock change inside any of them.
MAY = [date(2026, 5, day) for day in range(3, 11)]


def at(clock: str) -> datetime:
    """A New York clock reading, which is how the planner cuts its days."""
    return datetime.fromisoformat(clock).replace(tzinfo=NY)


def walk(night: date) -> list[datetime]:
    """The real five-minute grid of one night's walk, endpoints exclusive.

    The walk runs on real time: the 25-hour night after the fall-back gives
    300 steps and the 23-hour night after the spring-forward gives 276, the
    count the night is measured against.
    """
    start = at(f"{night - timedelta(days=1)}T12:00").astimezone(UTC)
    finish = at(f"{night}T12:00").astimezone(UTC)
    count = round((finish - start).total_seconds() / 300)
    return [start + STEP * i for i in range(count)]


def record(
    nights: list[date], watts: float = 300.0, thin: frozenset[date] = frozenset()
) -> list[tuple[datetime, float | None]]:
    """Load rows for a run of consecutive nights, thin ones answering little.

    A thin night answers only its first ten steps, which is enough to hold
    its place in a window while staying unusable history.
    """
    rows: list[tuple[datetime, float | None]] = []
    for night in nights:
        instants = walk(night)
        if night in thin:
            instants = instants[:10]
        rows.extend((instant, watts) for instant in instants)
    return rows


def soc_path(
    night: date, fn: Callable[[float], float], step: float = 5.0
) -> list[tuple[datetime, float]]:
    """Recorded SoC over the night's 22:00-to-07:00 window, in real steps
    from 22:00 through 07:00 inclusive.

    Clock time is walked as real time, so the window over the fall-back night
    is ten real hours and the window over the spring-forward night is eight.
    """
    start = at(f"{night - timedelta(days=1)}T22:00").astimezone(UTC)
    finish = at(f"{night}T07:00").astimezone(UTC)
    rows: list[tuple[datetime, float]] = []
    minutes = 0.0
    while True:
        instant = start + timedelta(minutes=minutes)
        if instant > finish:
            break
        rows.append((instant, fn(minutes)))
        minutes += step
    return rows


def run(
    rows: list[tuple[datetime, float | None]], soc: list[tuple[datetime, float]]
) -> list[NightReplay]:
    """Replay on the reference battery: 100 Ah above a 10 percent floor, unit
    round-trip efficiency, no charge path, and a discharge cap that never binds
    a 300 W house."""
    return replay_nights(rows, soc, NY, 100.0, 10.0, 1.0, 0.0, 5000.0)


def test_the_actual_crossing_walks_chronology_not_arrival_order() -> None:
    # A row listed first is not always a row that happened first. The floor
    # was reached at 02:00 here, and the walk reports that instant even
    # though its witness and the reading after it arrive around it in no
    # particular order.
    rows = [
        (at("2026-05-10T02:05"), 9.0),
        (at("2026-05-10T02:00"), 8.0),
        (at("2026-05-10T01:55"), 10.2),
    ]
    assert actual_crossing(rows, 10.0, NY) == at("2026-05-10T02:00")

    # These rows share one clock reading, the 01:xx that comes twice on the
    # fall-back night. The rows listed first happened second in real time,
    # and the floor was already reached in the first pass through the hour,
    # which is the one to report.
    first_pass = [
        datetime(2026, 11, 1, 1, 30, tzinfo=NY),  # 05:30 UTC, still above
        datetime(2026, 11, 1, 1, 35, tzinfo=NY),  # 05:35 UTC, the reach
    ]
    second_pass = [
        datetime(2026, 11, 1, 1, 40, tzinfo=NY, fold=1),  # 06:40 UTC
        datetime(2026, 11, 1, 1, 45, tzinfo=NY, fold=1),  # 06:45 UTC
    ]
    assert second_pass[0].astimezone(UTC) > first_pass[-1].astimezone(UTC)
    rows = [
        (second_pass[0], 9.0),
        (second_pass[1], 8.5),
        (first_pass[0], 10.5),
        (first_pass[1], 9.5),
    ]
    got = actual_crossing(rows, 10.0, NY)
    assert got is not None and got.astimezone(UTC) == first_pass[1].astimezone(UTC)


def test_no_record_and_a_record_that_never_touches_the_floor() -> None:
    assert actual_crossing([], 10.0, NY) is None
    quiet = [(at("2026-05-10T01:00"), 10.5), (at("2026-05-10T02:00"), 11.0)]
    assert actual_crossing(quiet, 10.0, NY) is None
    # Control: the same walk does report a record that reaches the floor, so
    # the two lines above are answers, not a stub that never answers.
    crossed = [(at("2026-05-10T01:00"), 10.5), (at("2026-05-10T02:00"), 9.9)]
    assert actual_crossing(crossed, 10.0, NY) == at("2026-05-10T02:00")


def test_the_first_seven_nights_of_the_record_are_never_replayed() -> None:
    replays = run(record(MAY), soc_path(MAY[-1], lambda _minutes: 40.0))
    assert [item.night for item in replays] == [date(2026, 5, 10)]
    # A 300 W median draw against 30 usable points of battery crosses the
    # floor 64 steps in: 22:00 plus 5 hours 20 minutes of real time.
    assert replays[0].projected_crossing == at("2026-05-10T03:20")


def test_a_window_of_mostly_gaps_is_not_prior_history() -> None:
    thin = frozenset({date(2026, 5, day) for day in (3, 5, 6, 7, 9)})
    rows = record(MAY, thin=thin)
    assert run(rows, soc_path(MAY[-1], lambda _minutes: 40.0)) == []
    # Control: the same eight nights with every one answered do replay.
    assert len(run(record(MAY), soc_path(MAY[-1], lambda _minutes: 40.0))) == 1


def test_the_projection_is_built_from_prior_nights_only() -> None:
    # The replayed night drawing ten times its history must not leak into the
    # curve that projects it: the projection is what the planner would have
    # run at 22:00, and at 22:00 it had only seen the nights before.
    rows_b = record(MAY[:-1]) + [(instant, 3000.0) for instant in walk(MAY[-1])]
    soc = soc_path(MAY[-1], lambda _minutes: 40.0)
    seen_before = run(record(MAY), soc)
    loud_night = run(rows_b, soc)
    assert len(seen_before) == 1 and len(loud_night) == 1
    assert seen_before[0].projected_crossing is not None
    assert [item.projected_crossing for item in seen_before] == [
        item.projected_crossing for item in loud_night
    ]


def test_a_night_reports_the_gap_between_what_was_projected_and_what_happened() -> None:
    # The battery sat at 40 percent when the plan was made, crossed the floor
    # two hours before the projection said it would, and refilled to 30 by
    # morning. The projection spent 30 points of battery, the night spent 10.
    def recorded(minutes: float) -> float:
        if minutes <= 200.0:
            return 40.0 - 0.152 * minutes
        return 9.6 + 20.4 * (minutes - 200.0) / 340.0

    replays = run(record(MAY), soc_path(MAY[-1], recorded))
    assert len(replays) == 1
    night = replays[0]
    assert night.projected_crossing == at("2026-05-10T03:20")
    assert night.actual_crossing == at("2026-05-10T01:20")
    assert crossing_errors(replays) == [timedelta(hours=2)]
    assert night.wh_error == pytest.approx(20.0 * PER_POINT)
    assert wh_errors(replays) == [pytest.approx(20.0 * PER_POINT)]


def test_a_rising_soc_is_negative_draw() -> None:
    # Nothing ever touched the floor and the bank ended the window ten points
    # fuller than it began, so the night drew minus 10 points against the
    # projection's 30: the error is the whole 40, stated with its sign.
    def charging(minutes: float) -> float:
        return 40.0 + 10.0 * minutes / 540.0

    replays = run(record(MAY), soc_path(MAY[-1], charging))
    assert len(replays) == 1
    night = replays[0]
    assert night.actual_crossing is None
    assert night.wh_error == pytest.approx(40.0 * PER_POINT)
    assert crossing_errors(replays) == []


def test_a_hole_across_the_projected_crossing_leaves_the_crossing_unconfirmed() -> None:
    # The record answers at five-minute steps until 03:00, then only once
    # more, a minute after the crossing the projection named, and nothing
    # between: the crossing might have happened anywhere in that hole and
    # nobody saw it, so it is not reported at all, even though the later
    # readings sit under the floor. The energy side still compares the
    # readings that exist.
    start = at("2026-05-09T22:00").astimezone(UTC)
    dense = [(start + STEP * i, 40.0 - 0.05 * i) for i in range(61)]
    after = [(start + timedelta(minutes=321), 9.9)]
    replays = run(record(MAY), dense + after)
    assert len(replays) == 1
    night = replays[0]
    assert night.projected_crossing == at("2026-05-10T03:20")
    assert night.actual_crossing is None
    assert night.wh_error == pytest.approx(-0.1 * PER_POINT)
    assert crossing_errors(replays) == []


def test_a_night_without_a_battery_record_is_not_replayed() -> None:
    # Without a recorded state of charge there is no battery to project and no
    # night to compare it with: the replay leaves the night alone.
    assert run(record(MAY), []) == []
    assert len(run(record(MAY), soc_path(MAY[-1], lambda _minutes: 40.0))) == 1


def test_a_refused_battery_produces_no_replays() -> None:
    rows, soc = record(MAY), soc_path(MAY[-1], lambda _minutes: 40.0)
    assert replay_nights(rows, soc, NY, 0.0, 10.0, 1.0, 0.0, 5000.0) == []
    assert replay_nights(rows, soc, NY, 100.0, 10.0, 0.0, 0.0, 5000.0) == []
    assert replay_nights(rows, soc, NY, 100.0, 100.0, 1.0, 0.0, 5000.0) == []
    # Control: the same rows do replay on a battery the model accepts.
    assert len(run(rows, soc)) == 1


def test_a_fall_back_night_spends_its_repeated_hour_twice() -> None:
    # November 1, 2026 is the 25-hour night in New York: the clocks go back
    # at 02:00, and the 22:00-to-07:00 window over that night is ten real
    # hours, not nine. The projection is charged for 320 real minutes of a
    # 300 W draw, which lands after the repeated 01xx hour has run out; the
    # record crossed inside the first pass through that hour, 135 real
    # minutes earlier.
    nights = [date(2026, 10, day) for day in range(25, 32)] + [date(2026, 11, 1)]

    def recorded(minutes: float) -> float:
        return 40.0 if minutes < 185.0 else 9.0

    replays = run(record(nights), soc_path(date(2026, 11, 1), recorded))
    assert [item.night for item in replays] == [date(2026, 11, 1)]
    night = replays[0]
    start = at("2026-10-31T22:00").astimezone(UTC)
    assert night.projected_crossing == (start + timedelta(minutes=320)).astimezone(NY)
    assert night.actual_crossing == at("2026-11-01T01:05")
    assert crossing_errors(replays) == [timedelta(minutes=135)]


def test_a_spring_forward_night_is_measured_by_its_own_walked_steps() -> None:
    # March 8, 2026 is the 23-hour night in New York: its walk is 276 steps,
    # not 288, and its 22:00-to-07:00 window is eight real hours because the
    # 02xx hour never happened. The bank begins at 55 percent and the
    # projection drains all 45 usable points by the end of that walk, so the
    # error is the whole projected drain against an actual draw of nothing,
    # and the empty crossing side confirms the walk was charged for eight
    # real hours.
    nights = [date(2026, 3, day) for day in range(1, 9)]
    replays = run(record(nights), soc_path(date(2026, 3, 8), lambda _minutes: 55.0))
    assert [item.night for item in replays] == [date(2026, 3, 8)]
    night = replays[0]
    start = at("2026-03-07T22:00").astimezone(UTC)
    assert night.projected_crossing == (start + timedelta(minutes=480)).astimezone(NY)
    assert night.actual_crossing is None
    assert night.wh_error == pytest.approx(45.0 * PER_POINT)
    assert crossing_errors(replays) == []
    assert wh_errors(replays) == [pytest.approx(45.0 * PER_POINT)]


def test_the_error_helpers_ignore_nights_that_cannot_be_paired() -> None:
    paired = NightReplay(date(2026, 5, 10), at("2026-05-10T03:20"), at("2026-05-10T01:20"), 100.0)
    no_actual = NightReplay(date(2026, 5, 11), at("2026-05-11T04:00"), None, None)
    no_projected = NightReplay(date(2026, 5, 12), None, at("2026-05-12T05:00"), -50.0)
    quiet = NightReplay(date(2026, 5, 13), None, None, None)
    replays = [paired, no_actual, no_projected, quiet]
    assert crossing_errors(replays) == [timedelta(hours=2)]
    assert wh_errors(replays) == [100.0, -50.0]
