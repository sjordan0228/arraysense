"""test_energy.py — calendar-day and calendar-month energy from the stored counters.

Every fixture here writes lifetime counters and asserts kWh totals, because the
whole module exists to avoid integrating power. The awkward cases are the ones
worth having: the two days a year that are not 24 hours long, a counter that
resets to zero under a firmware update, an outage that straddles midnight, and
a bucket the owner is still living through.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from arraysense.api.app import create_app
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.energy import (
    ENERGY_FIELDS,
    MAX_EDGE_GAP,
    EnergyBucket,
    attribute_energy,
    bucket_edges,
    bucket_totals,
    counter_kwh,
    energy_totals,
    host_zone,
    resolve_zone,
)
from arraysense.models import Sample
from arraysense.settings import SETTING_TIMEZONE, SettingsStore
from arraysense.store.rollup import rebuild_inverter_hourly
from arraysense.store.sqlite_store import SqliteStore
from arraysense.tariff import SETTING_BANDS
from conftest import TEST_DEVICE

NY = ZoneInfo("America/New_York")

LOAD = "load_energy_total_kwh"
EXPORT = "grid_export_energy_total_kwh"


def _counters(
    first: datetime,
    hours: int,
    start_value: float = 1000.0,
    metric: str = LOAD,
) -> list[Sample]:
    """Return one sample an hour, the counter climbing by 1 kWh each hour.

    Stepping in absolute time rather than wall-clock time is deliberate: adding
    an hour to a local datetime across a daylight saving change moves the
    instant by two hours, which would quietly undo the very thing the DST tests
    are checking.
    """
    when = first.astimezone(UTC)
    return [
        Sample(timestamp=when + timedelta(hours=i), readings={metric: start_value + i})
        for i in range(hours + 1)
    ]


def _store(tmp_path: Path, samples: Iterable[Sample]) -> SqliteStore:
    """Open a temporary store holding ``samples``."""
    store = SqliteStore(str(tmp_path / "energy.db"), device=TEST_DEVICE)
    for sample in samples:
        store.append(sample)
    return store


def _by_start(buckets: Sequence[EnergyBucket], fmt: str = "%Y-%m-%d") -> dict[str, EnergyBucket]:
    """Index buckets by the local date they start on."""
    return {bucket.start.strftime(fmt): bucket for bucket in buckets}


# --- bucket boundaries ------------------------------------------------------


def test_day_edges_are_local_midnights_not_utc_days() -> None:
    edges = bucket_edges(
        datetime(2026, 7, 1, 12, tzinfo=NY), datetime(2026, 7, 3, 12, tzinfo=NY), "day", NY
    )
    assert edges[0] == datetime(2026, 7, 1, tzinfo=NY)
    assert edges[-1] == datetime(2026, 7, 4, tzinfo=NY)
    # Local midnight in July is 04:00 UTC, so a day here is not a UTC day.
    assert edges[0].astimezone(UTC).hour == 4


def _elapsed(first: datetime, second: datetime) -> timedelta:
    """Return real elapsed time between two boundaries.

    Subtracting two datetimes that share a tzinfo subtracts wall clocks, which
    on a daylight saving day is the one answer that is definitely wrong.
    """
    return second.astimezone(UTC) - first.astimezone(UTC)


def test_spring_forward_day_is_twenty_three_hours() -> None:
    edges = bucket_edges(
        datetime(2026, 3, 8, 12, tzinfo=NY), datetime(2026, 3, 8, 13, tzinfo=NY), "day", NY
    )
    assert _elapsed(edges[0], edges[1]) == timedelta(hours=23)


def test_fall_back_day_is_twenty_five_hours() -> None:
    edges = bucket_edges(
        datetime(2026, 11, 1, 12, tzinfo=NY), datetime(2026, 11, 1, 13, tzinfo=NY), "day", NY
    )
    assert _elapsed(edges[0], edges[1]) == timedelta(hours=25)


def test_month_edges_are_local_month_starts() -> None:
    edges = bucket_edges(
        datetime(2026, 1, 20, tzinfo=NY), datetime(2026, 3, 2, tzinfo=NY), "month", NY
    )
    assert edges == [
        datetime(2026, 1, 1, tzinfo=NY),
        datetime(2026, 2, 1, tzinfo=NY),
        datetime(2026, 3, 1, tzinfo=NY),
        datetime(2026, 4, 1, tzinfo=NY),
    ]


# --- daily totals -----------------------------------------------------------


def test_a_clean_day_totals_the_owners_day_not_a_utc_day(tmp_path: Path) -> None:
    # A hundred kWh lands in the hour ending 21:00 on the 2nd, local time —
    # which is 01:00 UTC on the 3rd. Bucketing by UTC would file it a day late.
    samples = []
    value = 1000.0
    when = datetime(2026, 6, 30, tzinfo=NY).astimezone(UTC)
    jump = datetime(2026, 7, 2, 20, tzinfo=NY)
    for _ in range(24 * 5 + 1):
        samples.append(Sample(timestamp=when, readings={LOAD: value}))
        value += 100.0 if when == jump else 1.0
        when += timedelta(hours=1)
    store = _store(tmp_path, samples)

    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 4, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-01"].totals["load_kwh"] == 24.0
    assert days["2026-07-02"].totals["load_kwh"] == 123.0
    assert days["2026-07-03"].totals["load_kwh"] == 24.0
    assert days["2026-07-02"].complete is True


def test_the_spring_forward_day_holds_twenty_three_hours_of_energy(tmp_path: Path) -> None:
    store = _store(tmp_path, _counters(datetime(2026, 3, 6, tzinfo=NY), hours=24 * 5))
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 3, 7, tzinfo=NY),
            datetime(2026, 3, 10, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-03-07"].totals["load_kwh"] == 24.0
    assert days["2026-03-08"].totals["load_kwh"] == 23.0
    assert days["2026-03-09"].totals["load_kwh"] == 24.0


def test_the_fall_back_day_holds_twenty_five_hours_of_energy(tmp_path: Path) -> None:
    store = _store(tmp_path, _counters(datetime(2026, 10, 30, tzinfo=NY), hours=24 * 5))
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 10, 31, tzinfo=NY),
            datetime(2026, 11, 3, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-10-31"].totals["load_kwh"] == 24.0
    assert days["2026-11-01"].totals["load_kwh"] == 25.0
    assert days["2026-11-02"].totals["load_kwh"] == 24.0


def test_a_gap_inside_a_day_loses_no_energy(tmp_path: Path) -> None:
    # Collection stops at 10:00 and resumes at 16:00. The inverter counted
    # through the outage, which is the entire reason these are counters and not
    # an integration of power.
    samples = [
        s
        for s in _counters(datetime(2026, 7, 1, tzinfo=NY), hours=48)
        if not (
            datetime(2026, 7, 2, 10, tzinfo=NY) < s.timestamp < datetime(2026, 7, 2, 16, tzinfo=NY)
        )
    ]
    store = _store(tmp_path, samples)
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 2, tzinfo=NY),
            datetime(2026, 7, 3, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-02"].totals["load_kwh"] == 24.0
    assert days["2026-07-02"].complete is True


def test_a_reading_on_the_boundary_starts_the_day_even_before_a_long_gap(tmp_path: Path) -> None:
    # Three readings for the whole day, one of them exactly at each midnight.
    # Both midnights are known exactly, so the day is measured exactly, however
    # little was recorded in between.
    samples = [
        Sample(timestamp=when.astimezone(UTC), readings={LOAD: value})
        for when, value in (
            (datetime(2026, 7, 1, tzinfo=NY), 1000.0),
            (datetime(2026, 7, 1, 5, tzinfo=NY), 1005.0),
            (datetime(2026, 7, 2, tzinfo=NY), 1024.0),
        )
    ]
    store = _store(tmp_path, samples)
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 2, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-01"].totals["load_kwh"] == 24.0
    assert days["2026-07-01"].complete is True


def test_an_outage_across_midnight_is_not_charged_to_the_day_it_ended(tmp_path: Path) -> None:
    # Down from 22:00 on the 1st to 03:00 on the 2nd. Five hours of energy
    # happened and cannot be attributed to either day, so it belongs to
    # neither and both days say so.
    samples = [
        s
        for s in _counters(datetime(2026, 7, 1, tzinfo=NY), hours=48)
        if not (
            datetime(2026, 7, 1, 22, tzinfo=NY) < s.timestamp < datetime(2026, 7, 2, 3, tzinfo=NY)
        )
    ]
    store = _store(tmp_path, samples)
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 3, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-02"].totals["load_kwh"] == 21.0
    assert days["2026-07-02"].complete is False
    assert days["2026-07-01"].complete is False


def test_a_day_with_no_data_is_absent_rather_than_zero(tmp_path: Path) -> None:
    samples = _counters(datetime(2026, 7, 1, tzinfo=NY), hours=24)
    samples += _counters(datetime(2026, 7, 5, tzinfo=NY), hours=24, start_value=2000.0)
    store = _store(tmp_path, samples)
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 7, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert "2026-07-03" not in days
    assert "2026-07-04" not in days
    assert days["2026-07-01"].totals["load_kwh"] == 24.0


def test_a_range_with_no_data_at_all_returns_no_buckets(tmp_path: Path) -> None:
    store = _store(tmp_path, _counters(datetime(2026, 7, 1, tzinfo=NY), hours=24))
    buckets = energy_totals(
        store,
        datetime(2025, 1, 1, tzinfo=NY),
        datetime(2025, 1, 10, tzinfo=NY),
        period="day",
        zone=NY,
    )
    store.close()

    assert buckets == []


def test_the_current_bucket_is_marked_incomplete(tmp_path: Path) -> None:
    # Data stops at 14:00 on the 2nd: today, from the store's point of view.
    store = _store(tmp_path, _counters(datetime(2026, 7, 1, tzinfo=NY), hours=38))
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 3, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-01"].complete is True
    assert days["2026-07-02"].totals["load_kwh"] == 14.0
    assert days["2026-07-02"].complete is False


def test_the_first_bucket_is_incomplete_when_collection_started_inside_it(tmp_path: Path) -> None:
    store = _store(tmp_path, _counters(datetime(2026, 7, 1, 9, tzinfo=NY), hours=40))
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 3, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-01"].complete is False
    assert days["2026-07-01"].totals["load_kwh"] == 15.0
    assert days["2026-07-02"].complete is True


# --- counter resets ---------------------------------------------------------


def test_a_counter_reset_does_not_produce_a_negative_day(tmp_path: Path) -> None:
    # A firmware update takes the lifetime counter back to zero at noon on the
    # 2nd. Subtracting start from end would report about minus a thousand kWh.
    samples = []
    value = 1000.0
    when = datetime(2026, 7, 1, tzinfo=NY).astimezone(UTC)
    reset_at = datetime(2026, 7, 2, 12, tzinfo=NY)
    for _ in range(24 * 3 + 1):
        if when == reset_at:
            value = 0.0
        samples.append(Sample(timestamp=when, readings={LOAD: value}))
        value += 1.0
        when += timedelta(hours=1)
    store = _store(tmp_path, samples)

    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 4, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-02"].totals["load_kwh"] == 23.0
    # The hour the counter restarted in is energy nobody can account for, so
    # the day is not presented as a whole day.
    assert days["2026-07-02"].complete is False
    assert days["2026-07-01"].totals["load_kwh"] == 24.0
    assert days["2026-07-03"].totals["load_kwh"] == 24.0
    assert days["2026-07-03"].complete is True


def test_a_step_backwards_that_is_not_a_reset_is_not_counted(tmp_path: Path) -> None:
    # A monotonic counter that drops by one kWh is a bad reading, not a reset.
    # Crediting the whole new value would add a thousand kWh to the day.
    samples = []
    value = 1000.0
    when = datetime(2026, 7, 1, tzinfo=NY).astimezone(UTC)
    dip_at = datetime(2026, 7, 2, 12, tzinfo=NY)
    for _ in range(24 * 3 + 1):
        if when == dip_at:
            value -= 2.0
        samples.append(Sample(timestamp=when, readings={LOAD: value}))
        value += 1.0
        when += timedelta(hours=1)
    store = _store(tmp_path, samples)

    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 4, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-02"].totals["load_kwh"] == 23.0
    assert days["2026-07-02"].complete is False


# --- absence -----------------------------------------------------------------


def test_a_counter_that_never_moved_is_zero_and_one_never_reported_is_null(
    tmp_path: Path,
) -> None:
    samples = [
        Sample(
            timestamp=(datetime(2026, 7, 1, tzinfo=NY) + timedelta(hours=i)).astimezone(UTC),
            readings={LOAD: 1000.0 + i, EXPORT: 40.0},
        )
        for i in range(49)
    ]
    store = _store(tmp_path, samples)
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 2, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    # Exported nothing all day: a measured zero.
    assert days["2026-07-01"].totals["grid_exported_kwh"] == 0.0
    # Never reported at all: absent, and absent is not zero.
    assert days["2026-07-01"].totals["solar_kwh"] is None


def test_a_failed_poll_is_not_a_reading_of_zero(tmp_path: Path) -> None:
    samples: list[Sample] = []
    for i in range(49):
        when = (datetime(2026, 7, 1, tzinfo=NY) + timedelta(hours=i)).astimezone(UTC)
        if i == 5:
            samples.append(Sample.failed(when, "connection refused"))
            continue
        samples.append(Sample(timestamp=when, readings={LOAD: 1000.0 + i}))
    store = _store(tmp_path, samples)
    days = _by_start(
        energy_totals(
            store,
            datetime(2026, 7, 1, tzinfo=NY),
            datetime(2026, 7, 2, tzinfo=NY),
            period="day",
            zone=NY,
        )
    )
    store.close()

    assert days["2026-07-01"].totals["load_kwh"] == 24.0


# --- monthly totals ----------------------------------------------------------


def test_monthly_totals_mark_a_month_collection_started_inside(tmp_path: Path) -> None:
    store = _store(tmp_path, _counters(datetime(2026, 1, 20, tzinfo=NY), hours=24 * 42))
    months = _by_start(
        energy_totals(
            store,
            datetime(2026, 1, 1, tzinfo=NY),
            datetime(2026, 3, 3, tzinfo=NY),
            period="month",
            zone=NY,
        ),
        fmt="%Y-%m",
    )
    store.close()

    assert months["2026-01"].complete is False
    assert months["2026-01"].totals["load_kwh"] == 24.0 * 12
    # February is whole, and it is 28 days long in 2026.
    assert months["2026-02"].complete is True
    assert months["2026-02"].totals["load_kwh"] == 24.0 * 28
    assert months["2026-03"].complete is False


def test_monthly_totals_read_from_the_hourly_rollup(tmp_path: Path) -> None:
    # Months reach back past the raw tier's retention, so once a database is
    # older than thirty days the hourly rollup is the only thing left holding
    # them. Raw is emptied here to prove the answer does not come from it.
    store = _store(tmp_path, _counters(datetime(2026, 1, 20, tzinfo=NY), hours=24 * 42))
    conn = sqlite3.connect(str(tmp_path / "energy.db"))
    rebuild_inverter_hourly(
        conn,
        int(datetime(2026, 1, 1, tzinfo=NY).timestamp()),
        int(datetime(2026, 4, 1, tzinfo=NY).timestamp()),
    )
    conn.execute("DELETE FROM inverter_raw")
    conn.commit()
    conn.close()

    months = _by_start(
        energy_totals(
            store,
            datetime(2026, 1, 1, tzinfo=NY),
            datetime(2026, 3, 3, tzinfo=NY),
            period="month",
            zone=NY,
        ),
        fmt="%Y-%m",
    )
    store.close()

    assert months["2026-02"].totals["load_kwh"] == 24.0 * 28
    assert months["2026-02"].complete is True


# --- what the walk drops -------------------------------------------------------
#
# The pairwise walk cannot attribute a long pair that crosses a bucket edge, a
# reset, or a pair straddling the final edge. It used to discard them; money
# needs their energy, because a figure priced without it has to say so (#23).


def _reading(when: datetime, value: float, metric: str = LOAD) -> dict[str, object]:
    return {"timestamp": when.astimezone(UTC), metric: value}


def _july_days(first: int, last: int) -> list[datetime]:
    edges = bucket_edges(
        datetime(2026, 7, first, tzinfo=NY), datetime(2026, 7, last, 12, tzinfo=NY), "day", NY
    )
    return edges


def test_a_long_pair_crossing_an_edge_reports_the_energy_it_drops() -> None:
    # Readings stop at 22:00 and resume at 03:00 across midnight. The five
    # hours span the edge, so the 7 kWh between them belongs to neither day —
    # and it used to vanish. Both readings exist, so the amount is exact.
    edges = _july_days(1, 2)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [
        _reading(day1 + timedelta(hours=21), 100.0),
        _reading(day1 + timedelta(hours=22), 102.0),
        _reading(day1 + timedelta(hours=27), 109.0),
        _reading(day1 + timedelta(hours=28), 110.0),
    ]
    got = attribute_energy(rows, edges)
    assert len(got.dropped) == 1
    span = got.dropped[0]
    assert span.field == "load_kwh"
    assert span.start == day1.astimezone(UTC) + timedelta(hours=22)
    assert span.end == day1.astimezone(UTC) + timedelta(hours=27)
    assert span.kwh == pytest.approx(7.0)
    # And the buckets are exactly what bucket_totals says: the walk changed
    # nothing about attribution, it only stopped discarding what it knew.
    assert got.buckets == bucket_totals(rows, edges)


def test_the_same_gap_inside_one_bucket_drops_nothing() -> None:
    # The identical five-hour hole in the middle of one day: the counter delta
    # spans it exactly, nothing is lost, so there is nothing to report.
    edges = _july_days(1, 1)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [
        _reading(day1 + timedelta(hours=2), 100.0),
        _reading(day1 + timedelta(hours=3), 101.0),
        _reading(day1 + timedelta(hours=8), 108.0),
        _reading(day1 + timedelta(hours=9), 109.0),
    ]
    got = attribute_energy(rows, edges)
    assert got.dropped == ()


def test_a_reset_inside_a_dropped_pair_reports_no_amount() -> None:
    # The counter restarted somewhere inside a pair that also crosses the
    # edge. What it held before the restart is gone, so the span carries no
    # number — reporting the post-reset climb as "the missing energy" would
    # understate it by the whole pre-reset stretch.
    edges = _july_days(1, 2)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [
        _reading(day1 + timedelta(hours=21), 100.0),
        _reading(day1 + timedelta(hours=22), 102.0),
        _reading(day1 + timedelta(hours=27), 3.0),
        _reading(day1 + timedelta(hours=28), 4.0),
    ]
    got = attribute_energy(rows, edges)
    assert len(got.dropped) == 1
    assert got.dropped[0].kwh is None


def test_a_reset_that_crosses_no_edge_is_still_reported() -> None:
    # A firmware update at noon. The post-reset climb is attributed exactly as
    # before, but the hour the counter restarted in lost an unknowable amount,
    # and a money figure for the day has to be able to say so.
    edges = _july_days(1, 1)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [
        _reading(day1 + timedelta(hours=11), 1000.0),
        _reading(day1 + timedelta(hours=12), 0.5),
        _reading(day1 + timedelta(hours=13), 1.5),
    ]
    got = attribute_energy(rows, edges)
    assert len(got.dropped) == 1
    span = got.dropped[0]
    assert span.kwh is None
    assert span.start == day1.astimezone(UTC) + timedelta(hours=11)
    assert span.end == day1.astimezone(UTC) + timedelta(hours=12)
    # Attribution itself is unchanged: the climb since the restart still counts.
    assert got.buckets[0].totals["load_kwh"] == pytest.approx(1.5)


def test_a_long_pair_straddling_the_final_edge_is_recorded_not_swallowed() -> None:
    # Readings at 21:30 and 00:45 bracket the range's last midnight across a
    # real outage — yet the walk used to skip the pair entirely, so the
    # 21:30-to-midnight energy was in no bucket and no flag. The buckets must
    # not move; the record is what money reads.
    edges = _july_days(1, 1)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [_reading(day1 + timedelta(hours=h), 100.0 + h) for h in range(22)]
    rows.append(_reading(day1 + timedelta(hours=21, minutes=30), 121.5))
    rows.append(_reading(day1 + timedelta(hours=24, minutes=45), 124.0))
    got = attribute_energy(rows, edges)
    assert len(got.dropped) == 1
    span = got.dropped[0]
    assert span.start == day1.astimezone(UTC) + timedelta(hours=21, minutes=30)
    assert span.kwh == pytest.approx(2.5)
    assert got.buckets == bucket_totals(rows, edges)


def test_an_ordinary_pair_straddling_the_final_edge_records_nothing() -> None:
    # Every historical range asked about while later data exists has some
    # pair straddling its final edge — at the poll cadence, an eleven-second
    # one. Recording those flagged the last day of perfectly clean months, so
    # within the edge tolerance the straddle is accepted exactly as the first
    # edge's carry-in is, and only a straddle wider than the gap tolerance is
    # a record.
    edges = _july_days(1, 1)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [_reading(day1 + timedelta(hours=h), 100.0 + h) for h in range(24)]
    rows.append(_reading(day1 + timedelta(hours=23, minutes=45), 123.75))
    rows.append(_reading(day1 + timedelta(hours=24, minutes=15), 124.25))
    got = attribute_energy(rows, edges)
    assert got.dropped == ()
    assert got.buckets[0].complete is True


def test_a_pair_that_moved_no_energy_is_recorded_as_proof_not_loss() -> None:
    # A five-hour outage across midnight during which the counter did not
    # move: the counter is monotonic, so zero between the readings proves
    # zero everywhere between them. The span is kept with its zero — dropping
    # it entirely lost the very evidence that the stretch was accounted for,
    # and downstream a band emptied by a flat gap was flagged though its
    # figure was provably exact.
    edges = _july_days(1, 2)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [
        _reading(day1 + timedelta(hours=21), 100.0),
        _reading(day1 + timedelta(hours=22), 100.0),
        _reading(day1 + timedelta(hours=27), 100.0),
        _reading(day1 + timedelta(hours=28), 100.5),
    ]
    got = attribute_energy(rows, edges)
    assert len(got.dropped) == 1
    assert got.dropped[0].kwh == 0.0


def test_bounds_say_how_far_each_counter_actually_reached() -> None:
    # Export stops answering at 04:00 while load carries on. No later export
    # reading exists, so no pair does either — the bounds are the only
    # evidence that the export figure covers four hours of a full day.
    edges = _july_days(1, 1)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows: list[dict[str, object]] = []
    for hour in range(25):
        row = _reading(day1 + timedelta(hours=hour), 100.0 + hour)
        if hour <= 4:
            row[EXPORT] = 50.0 + hour
        rows.append(row)
    got = attribute_energy(rows, edges)
    assert got.bounds["load_kwh"] == (
        day1.astimezone(UTC),
        day1.astimezone(UTC) + timedelta(hours=24),
    )
    assert got.bounds["grid_exported_kwh"] == (
        day1.astimezone(UTC),
        day1.astimezone(UTC) + timedelta(hours=4),
    )
    assert "solar_kwh" not in got.bounds
    assert got.dropped == ()


def test_a_clean_day_drops_nothing_and_buckets_are_identical() -> None:
    edges = _july_days(1, 1)
    day1 = datetime(2026, 7, 1, tzinfo=NY)
    rows = [_reading(day1 + timedelta(hours=h), 100.0 + h) for h in range(25)]
    got = attribute_energy(rows, edges)
    assert got.dropped == ()
    assert got.buckets == bucket_totals(rows, edges)
    assert got.buckets[0].complete is True
    assert got.buckets[0].totals["load_kwh"] == pytest.approx(24.0)


# --- timezone resolution ------------------------------------------------------


def test_host_zone_prefers_the_tz_environment_variable() -> None:
    assert host_zone(env={"TZ": "Asia/Tokyo"}) == ZoneInfo("Asia/Tokyo")


def test_host_zone_reads_etc_localtime_when_tz_is_unset() -> None:
    assert host_zone(env={}, localtime=Path("/usr/share/zoneinfo/Pacific/Auckland")) == ZoneInfo(
        "Pacific/Auckland"
    )


def test_host_zone_falls_back_to_utc_rather_than_failing() -> None:
    assert host_zone(env={"TZ": "Nowhere/Atall"}, localtime=Path("/dev/null")) == ZoneInfo("UTC")


def test_resolve_zone_rejects_an_unknown_zone() -> None:
    with pytest.raises(KeyError):
        resolve_zone("Mars/Olympus_Mons")


def test_resolve_zone_falls_back_to_the_host_when_asked_for_nothing() -> None:
    assert isinstance(resolve_zone(None), ZoneInfo)


def test_an_install_with_no_configured_zone_resolves_exactly_as_before() -> None:
    # The precedence change must be invisible to every install that has not
    # set the setting. Both ways of saying "nothing configured" have to give
    # the same answer the one-argument call always gave: the caller's zone,
    # then the host's.
    for asked in (None, "", "  ", "UTC", "America/New_York", "Pacific/Auckland"):
        expected = ZoneInfo(asked.strip()) if asked and asked.strip() else host_zone()
        assert resolve_zone(asked) == expected
        assert resolve_zone(asked, "") == expected
        assert resolve_zone(asked, None) == expected
        assert resolve_zone(asked, "   ") == expected


def test_the_configured_zone_wins_over_the_browsers() -> None:
    # The inverter is in one place. A phone that has travelled must not get a
    # different day, because a bill drawn against the wrong midnight looks
    # entirely normal.
    assert resolve_zone("Asia/Tokyo", "America/New_York") == ZoneInfo("America/New_York")
    assert resolve_zone(None, "America/New_York") == ZoneInfo("America/New_York")


def test_a_stale_unresolvable_configured_zone_falls_back_rather_than_failing() -> None:
    # The setting is checked where it is typed, so this is a value stored
    # before that check or by a hand-edited database. Refusing every request
    # over it would take the whole site down; the caller's zone and then the
    # host still answer, and the warning says why.
    assert resolve_zone("Asia/Tokyo", "Mars/Olympus_Mons") == ZoneInfo("Asia/Tokyo")
    assert isinstance(resolve_zone(None, "Mars/Olympus_Mons"), ZoneInfo)


# --- the endpoint --------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Any:
    store = SqliteStore(str(tmp_path / "api.db"), device=TEST_DEVICE)
    for sample in _counters(datetime(2026, 7, 1, tzinfo=NY), hours=38):
        store.append(sample)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "api.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        yield c
    store.close()


def test_energy_endpoint_returns_local_day_buckets(client: Any) -> None:
    body = client.get(
        "/api/energy",
        params={
            "start": "2026-07-01T00:00:00-04:00",
            "end": "2026-07-03T00:00:00-04:00",
            "period": "day",
            "tz": "America/New_York",
        },
    ).json()

    assert body["period"] == "day"
    assert body["timezone"] == "America/New_York"
    first, second = body["buckets"]
    assert first["start"] == "2026-07-01T00:00:00-04:00"
    assert first["load_kwh"] == 24.0
    assert first["complete"] is True
    assert second["complete"] is False
    # Nothing was reported for solar, and absent must not arrive as zero.
    assert second["solar_kwh"] is None


def test_energy_endpoint_reads_naive_timestamps_in_the_requested_zone(client: Any) -> None:
    body = client.get(
        "/api/energy",
        params={
            "start": "2026-07-01T00:00:00",
            "end": "2026-07-02T00:00:00",
            "tz": "America/New_York",
        },
    ).json()

    assert body["buckets"][0]["start"] == "2026-07-01T00:00:00-04:00"
    assert body["buckets"][0]["load_kwh"] == 24.0


def test_energy_endpoint_prefers_the_configured_zone_over_the_browsers(client: Any) -> None:
    # The browser says Tokyo; the installation says New York. New York decides
    # where the day is cut, and the reply says so rather than answering in a
    # zone the owner never chose.
    SettingsStore(client.app.state.store).set(SETTING_TIMEZONE, "America/New_York")
    body = client.get(
        "/api/energy",
        params={
            "start": "2026-07-01T00:00:00",
            "end": "2026-07-02T00:00:00",
            "tz": "Asia/Tokyo",
        },
    ).json()

    assert body["timezone"] == "America/New_York"
    assert body["buckets"][0]["start"] == "2026-07-01T00:00:00-04:00"
    assert body["buckets"][0]["load_kwh"] == 24.0


def test_energy_endpoint_still_follows_the_browser_when_nothing_is_configured(
    client: Any,
) -> None:
    # The setting empty is the default, and an install that has not set it must
    # answer exactly as it did before the setting existed.
    SettingsStore(client.app.state.store).set(SETTING_TIMEZONE, "")
    body = client.get(
        "/api/energy",
        params={
            "start": "2026-07-01T00:00:00",
            "end": "2026-07-02T00:00:00",
            "tz": "Asia/Tokyo",
        },
    ).json()
    assert body["timezone"] == "Asia/Tokyo"


def test_costs_endpoint_prefers_the_configured_zone_over_the_browsers(client: Any) -> None:
    # Rate bands are wall-clock hours in the owner's zone. This is the endpoint
    # where getting it wrong costs money rather than a chart.
    settings = SettingsStore(client.app.state.store)
    settings.set(SETTING_BANDS, "Peak | 0.34 | 16:00-21:00; Off-peak | 0.11 | 21:00-16:00")
    settings.set(SETTING_TIMEZONE, "America/New_York")
    body = client.get(
        "/api/costs",
        params={
            "start": "2026-07-01T00:00:00",
            "end": "2026-07-02T00:00:00",
            "tz": "Asia/Tokyo",
        },
    ).json()
    assert body["timezone"] == "America/New_York"


def test_energy_endpoint_rejects_an_unknown_timezone(client: Any) -> None:
    response = client.get(
        "/api/energy",
        params={"start": "2026-07-01T00:00:00Z", "end": "2026-07-02T00:00:00Z", "tz": "Mars/Base"},
    )
    assert response.status_code == 400


def test_energy_endpoint_rejects_an_unknown_period(client: Any) -> None:
    response = client.get(
        "/api/energy",
        params={"start": "2026-07-01T00:00:00Z", "end": "2026-07-02T00:00:00Z", "period": "week"},
    )
    assert response.status_code == 422


def test_energy_endpoint_rejects_a_backwards_range(client: Any) -> None:
    response = client.get(
        "/api/energy",
        params={"start": "2026-07-03T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
    )
    assert response.status_code == 400


# --- tier equivalence -----------------------------------------------------------
#
# A day's kWh telescopes — the counter at the end minus the counter at the
# start — so the daily totals from the hourly tier must be identical to the
# same totals from the minute tier. This test pins that property against a
# future change that silently trades accuracy for speed.


def test_daily_totals_are_identical_from_hourly_and_minute_tiers(
    tmp_path: Path,
) -> None:
    """Daily totals from the hourly tier match those from the minute tier.

    Covers normal days and the 23-hour day the US spring-forward makes, so the
    assertion holds across the calendar shapes that matter. A single day broken
    here would put the lie to the claim that the tier does not affect the
    answer.
    """
    from arraysense.store.rollup import rebuild_inverter_hourly, rebuild_inverter_minute

    zone = ZoneInfo("America/New_York")
    # Straddling the spring-forward, which is the second Sunday of March in the
    # US: 14 Mar 2027, a day 23 hours long.
    start = datetime(2027, 3, 12, tzinfo=zone)
    end = datetime(2027, 3, 16, tzinfo=zone)
    edges = bucket_edges(start, end, "day", zone)

    # Write a counter climbing 1 kWh an hour for 10 days so both tiers see the
    # same underlying measurements.
    samples = _counters(datetime(2027, 3, 10, tzinfo=zone), hours=24 * 10)
    store = _store(tmp_path, samples)

    # Roll up to both coarse tiers.
    db_path = str(tmp_path / "energy.db")
    conn = sqlite3.connect(db_path)
    data_start = int(samples[0].timestamp.timestamp())
    data_end = int(samples[-1].timestamp.timestamp())
    rebuild_inverter_minute(conn, data_start, data_end)
    rebuild_inverter_hourly(conn, data_start, data_end)
    conn.commit()
    conn.close()

    # Read from each tier directly, widened as _counter_rows does.
    query_start = edges[0] - MAX_EDGE_GAP
    query_end = edges[-1] + MAX_EDGE_GAP
    hourly_rows = store.query(list(ENERGY_FIELDS.values()), query_start, query_end, tier="hourly")
    minute_rows = store.query(list(ENERGY_FIELDS.values()), query_start, query_end, tier="minute")

    store.close()

    hourly_buckets = bucket_totals(hourly_rows, edges)
    minute_buckets = bucket_totals(minute_rows, edges)

    # Same number of buckets.
    assert len(hourly_buckets) == len(minute_buckets)

    for hb, mb in zip(hourly_buckets, minute_buckets, strict=True):
        assert hb.start == mb.start
        assert hb.end == mb.end
        assert hb.complete == mb.complete
        for field in ENERGY_FIELDS:
            hv = hb.totals.get(field)
            mv = mb.totals.get(field)
            if hv is None or mv is None:
                assert hv is mv, f"{field} at {hb.start}: hourly={hv} minute={mv}"
            else:
                assert hv == pytest.approx(mv), f"{field} at {hb.start}: hourly={hv} minute={mv}"


# --- a counter over an exact window -----------------------------------------
#
# The calendar path widens outward to whole days, which is right for a history
# table and wrong for a window on screen. These pin the exact-window read, and
# every case where the honest answer is None.


def test_counter_kwh_reads_the_climb_between_two_exact_instants(tmp_path: Path) -> None:
    # Not widened to a calendar day. The Circuits tab asks about the hours on
    # screen, and a day's answer to a three-hour question understates the
    # circuits' share several times over.
    start = datetime(2026, 8, 16, 12, tzinfo=UTC)
    store = _store(tmp_path, _counters(start, hours=6))

    got = counter_kwh(store, start, start + timedelta(hours=3))
    store.close()

    # The counter climbs 1 kWh an hour, so three hours is 3 kWh — not the
    # twenty-four a calendar-widened read would return.
    assert got == pytest.approx(3.0)


def test_counter_kwh_is_none_when_a_bound_is_not_bracketed(tmp_path: Path) -> None:
    # A ten-hour hole across the window's start means the energy in the hole
    # belongs to nobody, and the shortfall is unknown rather than zero. A number
    # here would be a partial figure presented as a complete one.
    samples = [
        Sample(timestamp=datetime(2026, 8, 16, 4, tzinfo=UTC), readings={LOAD: 90.0}),
        Sample(timestamp=datetime(2026, 8, 16, 14, tzinfo=UTC), readings={LOAD: 104.5}),
        Sample(timestamp=datetime(2026, 8, 16, 15, tzinfo=UTC), readings={LOAD: 105.5}),
    ]
    store = _store(tmp_path, samples)

    got = counter_kwh(
        store, datetime(2026, 8, 16, 12, tzinfo=UTC), datetime(2026, 8, 16, 15, tzinfo=UTC)
    )
    store.close()

    assert got is None


def test_counter_kwh_is_none_across_a_counter_reset(tmp_path: Path) -> None:
    # load_energy_total_kwh is a lifetime counter, so it does not reset nightly
    # — but a firmware update or a replaced BMS restarts it, and what it held
    # before the restart is not knowable. The coverage fraction must go null
    # rather than take the post-reset climb as though it were the whole window.
    samples = [
        Sample(timestamp=datetime(2026, 8, 16, 12, tzinfo=UTC), readings={LOAD: 950.0}),
        Sample(timestamp=datetime(2026, 8, 16, 13, tzinfo=UTC), readings={LOAD: 2.0}),
        Sample(timestamp=datetime(2026, 8, 16, 14, tzinfo=UTC), readings={LOAD: 3.0}),
    ]
    store = _store(tmp_path, samples)

    got = counter_kwh(
        store, datetime(2026, 8, 16, 12, tzinfo=UTC), datetime(2026, 8, 16, 14, tzinfo=UTC)
    )
    store.close()

    assert got is None


def test_counter_kwh_is_none_when_the_counter_stepped_backwards(tmp_path: Path) -> None:
    # A small backwards step is a bad reading rather than a restart, and _delta
    # attributes nothing to it. Crediting the new value as post-reset energy
    # would add the inverter's whole lifetime to one window.
    samples = [
        Sample(timestamp=datetime(2026, 8, 16, 12, tzinfo=UTC), readings={LOAD: 950.0}),
        Sample(timestamp=datetime(2026, 8, 16, 13, tzinfo=UTC), readings={LOAD: 948.0}),
        Sample(timestamp=datetime(2026, 8, 16, 14, tzinfo=UTC), readings={LOAD: 949.0}),
    ]
    store = _store(tmp_path, samples)

    got = counter_kwh(
        store, datetime(2026, 8, 16, 12, tzinfo=UTC), datetime(2026, 8, 16, 14, tzinfo=UTC)
    )
    store.close()

    assert got is None


def test_counter_kwh_is_none_when_the_counter_never_reported(tmp_path: Path) -> None:
    # An absent counter is not a house that used nothing. Zero here would make
    # the coverage fraction divide by zero or, worse, read as full coverage.
    store = _store(tmp_path, [])

    got = counter_kwh(
        store, datetime(2026, 8, 16, 12, tzinfo=UTC), datetime(2026, 8, 16, 15, tzinfo=UTC)
    )
    store.close()

    assert got is None


def test_counter_kwh_rejects_a_backwards_range(tmp_path: Path) -> None:
    store = _store(tmp_path, _counters(datetime(2026, 8, 16, 12, tzinfo=UTC), hours=6))
    with pytest.raises(ValueError, match="end must be after start"):
        counter_kwh(
            store, datetime(2026, 8, 16, 15, tzinfo=UTC), datetime(2026, 8, 16, 12, tzinfo=UTC)
        )
    store.close()
