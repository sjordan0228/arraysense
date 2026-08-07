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
    EnergyBucket,
    bucket_edges,
    energy_totals,
    host_zone,
    resolve_zone,
)
from arraysense.models import Sample
from arraysense.store.rollup import rebuild_inverter_hourly
from arraysense.store.sqlite_store import SqliteStore

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
    store = SqliteStore(str(tmp_path / "energy.db"))
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


# --- the endpoint --------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Any:
    store = SqliteStore(str(tmp_path / "api.db"))
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
