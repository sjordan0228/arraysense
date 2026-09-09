"""Tests for rollup maintenance: the coarse tiers must actually get built."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
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
from arraysense.modules.emporia.parse import Circuit, Reading
from arraysense.modules.emporia.repository import CircuitRepository
from arraysense.settings import (
    BACKUP_DIRECTORY_KEY,
    EMPORIA_INTERVAL_KEY,
    RETENTION_ENABLED_KEY,
    RETENTION_RAW_DAYS_KEY,
    SettingsStore,
)
from arraysense.store.retention import RetentionReport, TablePrune
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


async def test_changing_the_interval_does_not_rewrite_hours_already_measured(
    tmp_path: Path,
) -> None:
    # The pass rebuilds the previous three hours from the interval in force
    # *now*. Lowering emporia.interval from sixty seconds to ten re-capped three
    # hours of sixty-second readings at ten seconds apiece and rewrote them as a
    # sixth of the energy they recorded — readings measured honestly, overwritten
    # by a setting they were never collected under.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    hour = now - timedelta(hours=2)
    repo = CircuitRepository(store)
    repo.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], hour)
    for minute in range(60):
        repo.append_readings([Reading(100000, "1", 500)], hour + timedelta(minutes=minute))

    settings = SettingsStore(store)
    settings.set(EMPORIA_INTERVAL_KEY, 60)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)
    measured = store._conn.execute(
        "SELECT covered_seconds FROM circuit_hourly WHERE timestamp = ?",
        (int(hour.timestamp()),),
    ).fetchone()
    assert measured == (3600,), "an hour of one-minute polls is a whole hour"

    settings.set(EMPORIA_INTERVAL_KEY, 10)
    await service.maintain_rollups(now=now)

    again = store._conn.execute(
        "SELECT covered_seconds FROM circuit_hourly WHERE timestamp = ?",
        (int(hour.timestamp()),),
    ).fetchone()
    store.close()
    assert again == (3600,), "600 is what the new setting says, not what was recorded"


# --- what a pass cost, recorded ------------------------------------------------
#
# Issue #63 is a stall rather than a fault: roughly one run in three the API
# stops answering for 100 to 160 ms near the start of the sixty-second cycle, and
# the log holds nothing that can be read beside it. Timing the passes separately
# is the issue's next step, so each one now records what it did and what it
# cost. These tests pin those lines: at INFO, because that is the only level a
# stall recorded last week is still readable at, and stage by stage for the
# rollup, because a total on its own names no suspect.

_ROLLUP_PASS = re.compile(
    r"rollup pass: inverter_minute=(\d+)ms inverter_hourly=(\d+)ms "
    r"module_hourly=(\d+)ms circuit_hourly=(\d+)ms promote=(\d+)ms stages_total=(\d+)ms"
)
_RETENTION_PASS = re.compile(
    r"retention pass: run=(\d+)ms, (\d+) rows in (\d+) tables, (\d+) blocked"
)


def _pass_records(caplog: pytest.LogCaptureFixture, *prefixes: str) -> list[logging.LogRecord]:
    """The pass lines the collector service logged, and nothing else."""
    return [
        record
        for record in caplog.records
        if record.name == "arraysense.collector.service"
        and any(record.getMessage().startswith(prefix) for prefix in prefixes)
    ]


async def test_a_rollup_pass_logs_what_each_stage_cost(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A file-backed store with rows in it, so the pass runs in its worker as it
    # does on an installation, and every one of the five stages has work to time.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    for i in range(120):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"battery_voltage_v": 55.9, "pv_total_power_w": 4000.0},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    with caplog.at_level(logging.INFO, logger="arraysense.collector.service"):
        await service.maintain_rollups(now=now)

    records = _pass_records(caplog, "rollup pass:")
    assert records, "the rollup pass logged nothing about what it cost"
    assert len(records) == 1, "one line per pass, not one line per stage"
    assert records[0].levelno == logging.INFO, (
        "a debug line is hidden at the level the service runs at, which is the"
        " only moment the stall can still be read against it"
    )
    matched = _ROLLUP_PASS.fullmatch(records[0].getMessage())
    assert matched is not None, f"the line names no stages: {records[0].getMessage()!r}"
    costs = [int(value) for value in matched.groups()]
    assert costs[-1] >= max(costs[:-1]), "the pass is shorter than one of its own stages"
    store.close()


async def test_the_inline_path_logs_the_same_rollup_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # An in-memory store has no second connection, so its pass runs inline
    # instead of in a worker. An instrument fitted to the threaded path alone
    # goes quiet there, and it is the path every in-memory test takes.
    store = SqliteStore(":memory:", device=TEST_DEVICE)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    with caplog.at_level(logging.INFO, logger="arraysense.collector.service"):
        await service.maintain_rollups(now=datetime.now(tz=UTC))

    records = _pass_records(caplog, "rollup pass:")
    assert records, "the inline path logged no rollup pass line"
    assert len(records) == 1
    assert _ROLLUP_PASS.fullmatch(records[0].getMessage()) is not None
    store.close()


async def test_a_retention_pass_logs_what_it_removed_and_what_it_blocked(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The blocked case rather than a clean prune, because it is the shape the
    # line has to carry: a pass that deleted nothing still says how long it spent
    # finding that out, and the block keeps its own line as well.
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

    with caplog.at_level(logging.INFO, logger="arraysense.collector.service"):
        await service.maintain_retention(now=now)

    records = _pass_records(caplog, "retention pass:")
    assert records, "the retention pass logged nothing about what it cost or removed"
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    matched = _RETENTION_PASS.fullmatch(records[0].getMessage())
    assert matched is not None, f"the line carries no counts: {records[0].getMessage()!r}"
    _cost, rows, tables, blocked = (int(value) for value in matched.groups())
    # The three counts are the report's own, and the pass deleted nothing: the
    # raw tier blocked before its first batch was taken, so rows are rows removed
    # rather than rows looked at, and the four are the tables walked to find out.
    assert (rows, tables, blocked) == (0, 4, 1)
    blocks = [
        record for record in caplog.records if record.getMessage().startswith("retention blocked")
    ]
    assert len(blocks) == blocked, "the count is the per-table blocks this pass logged"
    assert (
        "retention blocked for inverter_raw: inverter_minute does not cover every source bucket"
        in caplog.text
    )
    store.close()


async def test_a_pass_that_raises_logs_the_failure_not_a_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A duration beside a failure reads as a measurement of work that never
    # finished. The warning already says the pass did not complete, so nothing
    # is added to the log but that warning.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    prefixes = ("rollup pass:", "retention pass:")
    with caplog.at_level(logging.INFO, logger="arraysense.collector.service"):
        await service.maintain_rollups(now=now)
        await service.maintain_retention(now=now)
        clean = len(_pass_records(caplog, *prefixes))
        assert clean == 2, "a clean pass of each kind logs one line"

        monkeypatch.setattr(service_module, "rebuild_inverter_minute", locked)
        monkeypatch.setattr(service_module, "run_retention", locked)
        await service.maintain_rollups(now=now)
        await service.maintain_retention(now=now)

    assert "rollup maintenance failed, will retry" in caplog.text
    assert "retention maintenance failed, will retry" in caplog.text
    assert len(_pass_records(caplog, *prefixes)) == clean, (
        "a pass that raised reports the failure through its warning, not a duration"
    )
    store.close()


async def test_the_rollup_line_pins_each_stage_to_its_own_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Real timings are honest but unlabelled: a swapped pair of stage names or
    # one stage's number pasted into two slots reads the same as a correct line
    # when every value is whatever the machine felt like. Deterministic stages
    # and a deterministic clock pin the label-to-argument mapping exactly.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    for name in (
        "rebuild_inverter_minute",
        "rebuild_inverter_hourly",
        "rebuild_module_hourly",
        "rebuild_circuit_hourly",
        "promote_pending_hours",
    ):
        monkeypatch.setattr(service_module, name, lambda *args, **kwargs: None)
    readings = iter([5, 3, 7, 2, 1, 18])
    monkeypatch.setattr(service_module, "_elapsed_ms", lambda began: next(readings))

    with caplog.at_level(logging.INFO, logger="arraysense.collector.service"):
        await service.maintain_rollups(now=now)

    records = _pass_records(caplog, "rollup pass:")
    assert len(records) == 1
    assert records[0].getMessage() == (
        "rollup pass: inverter_minute=5ms inverter_hourly=3ms module_hourly=7ms "
        "circuit_hourly=2ms promote=1ms stages_total=18ms"
    )
    store.close()


async def test_the_retention_line_carries_the_report_it_was_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The line's counts are the report's own, so the mapping is pinned with a
    # report this test constructed: rows are the per-table deletions summed,
    # blocked is the count of blocked tables, and neither is re-derived from
    # anything the pass looked at without deleting.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)

    def fabricated(conn: object, policy: object, now: datetime) -> RetentionReport:
        return RetentionReport(
            dry_run=False,
            ran=True,
            reason=None,
            tables=(
                TablePrune(table="inverter_raw", cutoff=now, rows=40, oldest=None, blocked=None),
                TablePrune(
                    table="inverter_minute", cutoff=now, rows=0, oldest=None, blocked="covered"
                ),
            ),
        )

    monkeypatch.setattr(service_module, "run_retention", fabricated)
    monkeypatch.setattr(service_module, "_elapsed_ms", lambda began: 33)

    with caplog.at_level(logging.INFO, logger="arraysense.collector.service"):
        await service.maintain_retention(now=now)

    records = _pass_records(caplog, "retention pass:")
    assert len(records) == 1
    assert records[0].getMessage() == "retention pass: run=33ms, 40 rows in 2 tables, 1 blocked"
    store.close()
