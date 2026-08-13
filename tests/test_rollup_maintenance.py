"""Tests for rollup maintenance: the coarse tiers must actually get built."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.collector import service as service_module
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.models import Sample
from arraysense.settings import (
    BACKUP_DIRECTORY_KEY,
    RETENTION_ENABLED_KEY,
    RETENTION_RAW_DAYS_KEY,
    SettingsStore,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "roll.db"), device=TEST_DEVICE)


async def test_the_coarse_tiers_are_built_without_anyone_asking(tmp_path: Path) -> None:
    # Nothing scheduled the rollups, so on a live install the minute and hourly
    # tiers stayed empty forever. /api/history serves the minute tier for
    # anything over six hours and calibration reads it exclusively, so history
    # charts were blank and the drift warning was permanently stuck at
    # "no full charge found".
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    for i in range(400):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"battery_voltage_v": 55.9, "pv_total_power_w": 4000.0},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)

    minute = store.query(["battery_voltage_v"], now - timedelta(days=1), now, tier="minute")
    hourly = store.query(["battery_voltage_v"], now - timedelta(days=1), now, tier="hourly")
    assert minute, "the minute tier is what history and calibration read"
    assert hourly
    store.close()


async def test_maintenance_is_idempotent(tmp_path: Path) -> None:
    # It runs on a timer forever, so running it twice over the same range must
    # not duplicate a bucket or change a value.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    for i in range(200):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"pv_total_power_w": 4000.0},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)
    first = store.query(["pv_total_power_w"], now - timedelta(days=1), now, tier="minute")
    first_hourly = store.query(["pv_total_power_w"], now - timedelta(days=1), now, tier="hourly")
    await service.maintain_rollups(now=now)
    second = store.query(["pv_total_power_w"], now - timedelta(days=1), now, tier="minute")
    second_hourly = store.query(["pv_total_power_w"], now - timedelta(days=1), now, tier="hourly")
    assert [r["timestamp"] for r in first] == [r["timestamp"] for r in second]
    assert [r["pv_total_power_w"] for r in first] == [r["pv_total_power_w"] for r in second]
    assert first_hourly == second_hourly
    store.close()


async def test_maintenance_drops_a_recent_hour_when_its_raw_rows_are_gone(
    tmp_path: Path,
) -> None:
    # Maintenance is delete-and-reinsert, not an upsert over whatever happened
    # to be there. If retention or a repair removes every source row in a
    # covered bucket, the stale average must disappear with them.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    sample_at = now - timedelta(minutes=30)
    store.append(Sample(timestamp=sample_at, readings={"pv_total_power_w": 4000.0}))
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    await service.maintain_rollups(now=now)
    before = store.query(["pv_total_power_w"], now - timedelta(hours=3), now, tier="hourly")
    assert before

    with store._conn:
        store._conn.execute(
            "DELETE FROM inverter_raw WHERE timestamp = ? AND device = ?",
            (int(sample_at.timestamp()), TEST_DEVICE),
        )
    await service.maintain_rollups(now=now)

    after = store.query(["pv_total_power_w"], now - timedelta(hours=3), now, tier="hourly")
    assert after == []
    store.close()


async def test_hourly_maintenance_rebuilds_only_the_justified_three_hour_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqliteStore(":memory:", device=TEST_DEVICE)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    now = datetime(2026, 8, 9, 12, 34, 56, tzinfo=UTC)
    calls: list[tuple[str, int, int]] = []

    def record(name: str) -> Callable[[sqlite3.Connection, int, int], None]:
        def rebuild(conn: sqlite3.Connection, start: int, end: int) -> None:
            calls.append((name, start, end))

        return rebuild

    monkeypatch.setattr(service_module, "rebuild_inverter_minute", record("minute"))
    monkeypatch.setattr(service_module, "rebuild_inverter_hourly", record("inverter_hourly"))
    monkeypatch.setattr(service_module, "rebuild_module_hourly", record("module_hourly"))

    await service.maintain_rollups(now=now)

    end = int(now.timestamp()) + 60
    assert calls == [
        ("minute", end - 3 * 3600, end),
        ("inverter_hourly", end - 3 * 3600, end),
        ("module_hourly", end - 3 * 3600, end),
    ]
    store.close()


async def test_maintenance_does_not_fail_the_poll_loop(tmp_path: Path) -> None:
    # Rollup maintenance is housekeeping. A failure in it must never stop the
    # collector, which is the thing that cannot be caught up on later.
    #
    # Since #30 the pass opens its own connection, so closing the store no
    # longer makes the rebuilds raise — what this now pins is that a pass
    # against a closed store is still survivable, which is the shape of a
    # shutdown racing a timer. The unopenable-database path is covered by
    # test_a_pass_that_cannot_open_its_database_is_survivable.
    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    store.close()
    await service.maintain_rollups(now=datetime.now(tz=UTC))


async def test_an_empty_database_is_not_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=datetime.now(tz=UTC))
    store.close()


async def test_retention_maintenance_logs_every_coverage_block(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A stalled retention pass must be visible without someone running the CLI."""
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    store.append(Sample(timestamp=now - timedelta(days=3), readings={"pv_total_power_w": 4000.0}))
    SettingsStore(store).update(
        {
            RETENTION_ENABLED_KEY: True,
            RETENTION_RAW_DAYS_KEY: 2,
            BACKUP_DIRECTORY_KEY: str(tmp_path),
        }
    )
    archive = tmp_path / "arraysense-current.db.gz"
    archive.touch()
    os.utime(archive, (now.timestamp(), now.timestamp()))
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    with caplog.at_level(logging.WARNING, logger="arraysense.collector.service"):
        await service.maintain_retention(now=now)

    assert (
        "retention blocked for inverter_raw: inverter_minute does not cover every source bucket"
        in caplog.text
    )
    store.close()


async def test_the_running_collector_builds_the_tiers_on_its_own(tmp_path: Path) -> None:
    # The point of the whole exercise: a service that is merely started, with
    # nobody calling anything, must end up with populated coarse tiers.
    import asyncio

    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=0.01)
    await service.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = store.query(
                ["pv_total_power_w"],
                datetime.now(tz=UTC) - timedelta(hours=1),
                datetime.now(tz=UTC) + timedelta(minutes=1),
                tier="minute",
            )
            if rows:
                break
        assert rows, "the collector ran but never built the minute tier"
    finally:
        await service.stop()
        store.close()


# The pass must not block the event loop it shares with the API (#30). Measured
# on the reference LXC it costs 830 ms, and because it is synchronous SQLite in
# an `async def` with no thread, every open page's status poll and every chart
# request stalls for that whole time — once a minute, on the same loop.
async def test_maintenance_lets_the_event_loop_keep_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    # Stand in for the real cost. Synchronous, exactly as the rebuilds are.
    def slow_rebuild(conn: sqlite3.Connection, start: int, end: int) -> None:
        time.sleep(0.3)

    monkeypatch.setattr(service_module, "rebuild_inverter_minute", slow_rebuild)

    # Anything else wanting the loop while the pass runs — an API request, in
    # production. It should get many turns during a 300 ms rebuild, not none.
    ticks = 0

    async def other_work() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(other_work())
    await service.maintain_rollups(now=datetime.now(tz=UTC))
    ticker.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ticker

    assert ticks >= 5, (
        f"the loop got {ticks} turns during a 300 ms pass — a synchronous "
        "rollup is blocking every HTTP response for its whole duration"
    )
    store.close()


async def test_a_sample_stored_while_a_pass_is_in_flight_survives_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The pass must not put the collector's own writes inside its transaction.
    # On a sqlite3.Connection `with conn:` is transaction state, not a lock, so
    # two threads entering it on ONE connection share a single commit and a
    # single rollback: the collector's commit would land the rollup's half-built
    # tiers, and a failed rollup would discard a sample that stored fine.
    #
    # The write has to happen while the pass is genuinely under way, or this
    # proves nothing — an earlier version used `asyncio.sleep(0)` and passed
    # against the old wholly synchronous code, because the pass simply finished
    # first. The two events below pin the ordering instead of hoping for it.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    for i in range(120):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"battery_voltage_v": 55.9},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    started = threading.Event()
    may_finish = threading.Event()

    def blocking_rebuild(conn: sqlite3.Connection, start: int, end: int) -> None:
        started.set()
        may_finish.wait(10)

    monkeypatch.setattr(service_module, "rebuild_inverter_minute", blocking_rebuild)

    task = asyncio.create_task(service.maintain_rollups(now=now))
    assert await asyncio.to_thread(started.wait, 10), "the pass never started"
    assert not task.done(), "the pass finished before the concurrent write — it ran inline"

    store.append(Sample(timestamp=now + timedelta(seconds=1), readings={"battery_voltage_v": 56.4}))
    may_finish.set()
    await task

    rows = store.query(["battery_voltage_v"], now, now + timedelta(minutes=1), tier="full")
    assert any(r["battery_voltage_v"] == 56.4 for r in rows), (
        "a sample stored while the rollup was in flight was lost with it"
    )
    store.close()


async def test_a_pass_that_cannot_open_its_database_is_survivable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Housekeeping failure must never stop the collector, which is the thing
    # that cannot be caught up on later. Closing the store no longer produces a
    # failure — the pass opens its own connection now — so this points the
    # store at something unopenable instead, which is what a vanished mount or a
    # permission change looks like from here.
    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    monkeypatch.setattr(store, "_path", str(tmp_path))  # a directory, not a file

    await service.maintain_rollups(now=datetime.now(tz=UTC))  # must not raise
    store.close()


async def test_a_memory_backed_store_still_builds_its_tiers(tmp_path: Path) -> None:
    # ":memory:" cannot be reopened — every connect() makes a new empty database
    # — so a threaded pass would rebuild an unrelated one, find no rows, and
    # report success while the real tiers stayed empty. Silent and total.
    store = SqliteStore(":memory:", device=TEST_DEVICE)
    assert store.is_memory_backed
    now = datetime.now(tz=UTC)
    for i in range(400):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"battery_voltage_v": 55.9},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)

    minute = store.query(["battery_voltage_v"], now - timedelta(days=1), now, tier="minute")
    assert minute, "the pass reported success but rolled up a different database"
    store.close()


# --- archive weather written for a past hour ----------------------------------
#
# POST /api/efficiency/backfill fetches past conditions from the weather archive
# and appends one sample per past hour, which lands in the raw tier alone. The
# efficiency engine reads irradiance from the hourly tier and nothing else, and
# maintenance rebuilds only the last three hours, so every backfilled hour older
# than that was invisible to the feature the backfill exists to feed — while the
# route reported the hours as written.
#
# Promotion cannot be a rebuild of those hours. A rebuild deletes the
# destination buckets and refills them from raw, and raw keeps thirty days
# against an hourly tier kept forever: rebuilding an hour from 2024 would delete
# a year of the inverter's own history to write one temperature into it.


def _archive_hour(when: datetime, ghi: float, air_c: float) -> Sample:
    """One hour as the weather archive answers it: site readings, no inverter."""
    return Sample(
        timestamp=when,
        readings={"ghi_wm2": ghi, "outside_temperature_c": air_c},
    )


async def test_archive_weather_for_a_past_hour_reaches_the_tier_that_reads_it(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    five_days_back = now - timedelta(days=5)
    for hour in range(24):
        store.append(_archive_hour(five_days_back + timedelta(hours=hour), 400.0, 30.0))

    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)

    rows = store.query(
        ["ghi_wm2", "outside_temperature_c"],
        five_days_back,
        five_days_back + timedelta(hours=23),
        tier="hourly",
    )
    store.close()
    assert len(rows) == 24
    assert all(r["ghi_wm2"] == 400.0 for r in rows)
    assert all(r["outside_temperature_c"] == 30.0 for r in rows)


async def test_promoting_a_backfilled_hour_keeps_the_inverter_history_beside_it(
    tmp_path: Path,
) -> None:
    # The hazard the promotion is shaped around. An hourly row from before the
    # raw tier's retention window holds readings raw can no longer supply;
    # writing an hour's weather into it must leave every one of them standing.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    long_ago = now - timedelta(days=400)
    conn = store._conn
    with conn:
        conn.execute(
            "INSERT INTO inverter_hourly (timestamp, device, sample_count, pv_total_power_w) "
            "VALUES (?, ?, ?, ?)",
            (int(long_ago.timestamp()), TEST_DEVICE, 300, 8000),
        )
    store.append(_archive_hour(long_ago, 550.0, 25.0))

    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)

    rows = store.query(
        ["pv_total_power_w", "ghi_wm2"],
        long_ago,
        long_ago + timedelta(minutes=30),
        tier="hourly",
    )
    store.close()
    assert len(rows) == 1
    assert rows[0]["pv_total_power_w"] == 8000.0
    assert rows[0]["ghi_wm2"] == 550.0
    assert rows[0]["sample_count"] == 300


async def test_a_second_pass_has_nothing_left_to_promote(tmp_path: Path) -> None:
    # The queue is drained, not re-read forever: a backfill of two years must
    # not leave every later pass rebuilding the same eighteen thousand hours.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    store.append(_archive_hour(now - timedelta(days=9), 300.0, 20.0))
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    await service.maintain_rollups(now=now)
    pending = store._conn.execute("SELECT COUNT(*) FROM rollup_pending").fetchone()[0]
    store.close()
    assert pending == 0


async def test_a_live_weather_tick_is_not_queued_for_promotion(tmp_path: Path) -> None:
    # The ordinary path already covers it: maintenance rebuilds the last three
    # hours from raw on every pass, so queueing a fresh sky reading would be
    # work for nothing on every tick forever.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    store.append(_archive_hour(now, 700.0, 33.0))
    pending = store._conn.execute("SELECT COUNT(*) FROM rollup_pending").fetchone()[0]
    store.close()
    assert pending == 0


async def test_an_hour_written_through_a_write_connection_is_promoted_too(tmp_path: Path) -> None:
    # The backfill route holds its own write connection, not the primary, so the
    # queue has to be written by the append itself rather than by whichever
    # connection the collector happens to own.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    when = now - timedelta(days=6)
    with store.write_connection() as writer:
        writer.append(_archive_hour(when, 250.0, 18.0))

    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)

    rows = store.query(["ghi_wm2"], when, when + timedelta(minutes=30), tier="hourly")
    store.close()
    assert len(rows) == 1
    assert rows[0]["ghi_wm2"] == 250.0
