"""Tests for retention: raw data only goes after durable coverage exists."""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from arraysense.settings import SettingsStore
from arraysense.store import retention as retention_module
from arraysense.store.retention import RetentionPolicy, policy_from_settings, run_retention
from arraysense.store.rollup import promote_pending_hours, rebuild_circuit_hourly
from arraysense.store.schema import INVERTER_TIERS, MODULE_TIERS, PENDING_TABLE
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)
RAW_DAYS = 30
MINUTE_DAYS = 365


def _store(tmp_path: Path) -> SqliteStore:
    """Open a disk-backed store because retention looks beside its database."""
    store = SqliteStore(str(tmp_path / "retention.db"), device=TEST_DEVICE)
    store._conn.isolation_level = None
    return store


def _policy(tmp_path: Path, *, enabled: bool = True) -> RetentionPolicy:
    """Return a policy whose archive directory is isolated to this test."""
    return RetentionPolicy(
        enabled=enabled,
        raw_days=RAW_DAYS,
        minute_days=MINUTE_DAYS,
        backup_directory=str(tmp_path),
    )


def _epoch(moment: datetime) -> int:
    """Store the UTC instant in the database's integer representation."""
    return int(moment.timestamp())


def _archive(tmp_path: Path, when: datetime) -> Path:
    """Leave an archive whose mtime can prove the candidates were captured."""
    archive = tmp_path / "arraysense-2026-08-13.db.gz"
    archive.touch()
    stamp = when.timestamp()
    archive.chmod(0o600)
    os.utime(archive, (stamp, stamp))
    return archive


def _insert_inverter_raw(
    conn: sqlite3.Connection, timestamp: int, *, error: str | None = None
) -> None:
    """Put one inverter poll in raw, successful unless an outage is requested."""
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, device, error) VALUES (?, ?, ?)",
        (timestamp, TEST_DEVICE, error),
    )


def _insert_inverter_destination(
    conn: sqlite3.Connection, table: str, timestamp: int, *, device: str = TEST_DEVICE
) -> None:
    """Put the aggregate witness at its exact bucket boundary."""
    conn.execute(
        f"INSERT OR IGNORE INTO {table} (timestamp, device, sample_count) VALUES (?, ?, 1)",
        (timestamp, device),
    )


def _insert_module_raw(conn: sqlite3.Connection, timestamp: int, module_id: int) -> None:
    """Put one module row into raw for one physical pack."""
    conn.execute(
        "INSERT OR IGNORE INTO serials (id, device, serial) VALUES (?, ?, ?)",
        (module_id, TEST_DEVICE, f"test-module-{module_id}"),
    )
    conn.execute(
        "INSERT INTO module_raw (timestamp, device, module_id) VALUES (?, ?, ?)",
        (timestamp, TEST_DEVICE, module_id),
    )


def _insert_module_hourly(
    conn: sqlite3.Connection, timestamp: int, module_id: int, *, device: str = TEST_DEVICE
) -> None:
    """Put the matching hourly module witness in its primary-key slot."""
    conn.execute(
        "INSERT OR IGNORE INTO serials (id, device, serial) VALUES (?, ?, ?)",
        (module_id, device, f"test-module-{module_id}"),
    )
    conn.execute(
        "INSERT INTO module_hourly (timestamp, device, module_id, sample_count) "
        "VALUES (?, ?, ?, 1)",
        (timestamp, device, module_id),
    )


def _cover_inverter_raw(conn: sqlite3.Connection, timestamp: int) -> None:
    """Make one raw successful-poll bucket safe in both required destinations."""
    _insert_inverter_destination(conn, "inverter_minute", timestamp // 60 * 60)
    _insert_inverter_destination(conn, "inverter_hourly", timestamp // 3600 * 3600)


def _old_raw() -> int:
    """Return an instant plainly before the raw cutoff and off a bucket edge."""
    return _epoch(NOW - timedelta(days=RAW_DAYS + 1, minutes=3))


def _rows(conn: sqlite3.Connection, table: str) -> int:
    """Count rows directly, so an empty result cannot be confused with zero data."""
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def test_disabled_retention_does_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_inverter_raw(store._conn, _old_raw())
    report = run_retention(store._conn, _policy(tmp_path, enabled=False), now=NOW)
    assert not report.ran
    assert report.reason == "retention.enabled is false"
    assert _rows(store._conn, "inverter_raw") == 1
    store.close()


def test_missing_or_too_old_backup_refuses_the_whole_pass(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_inverter_raw(store._conn, _old_raw())
    missing = run_retention(store._conn, _policy(tmp_path), now=NOW)
    _archive(tmp_path, NOW - timedelta(days=RAW_DAYS + 1, seconds=1))
    stale = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert not missing.ran and "backup" in (missing.reason or "")
    assert stale.tables[0].blocked is not None and "backup" in stale.tables[0].blocked
    assert _rows(store._conn, "inverter_raw") == 1
    store.close()


def test_backup_at_or_after_cutoff_allows_a_covered_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _old_raw()
    _insert_inverter_raw(store._conn, timestamp)
    _cover_inverter_raw(store._conn, timestamp)
    _archive(tmp_path, NOW - timedelta(days=RAW_DAYS))
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.ran
    assert report.tables[0].rows == 1
    assert _rows(store._conn, "inverter_raw") == 0
    store.close()


def test_dry_run_reports_the_same_rows_without_deleting(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _old_raw()
    _insert_inverter_raw(store._conn, timestamp)
    _cover_inverter_raw(store._conn, timestamp)
    _archive(tmp_path, NOW)
    dry = run_retention(store._conn, _policy(tmp_path), now=NOW, dry_run=True)
    real = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert dry.dry_run and dry.tables[0].rows == real.tables[0].rows == 1
    assert _rows(store._conn, "inverter_raw") == 0
    store.close()


def test_inverter_raw_requires_every_minute_bucket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _old_raw()
    _insert_inverter_raw(store._conn, timestamp)
    _insert_inverter_destination(store._conn, "inverter_hourly", timestamp // 3600 * 3600)
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.tables[0].blocked == "inverter_minute does not cover every source bucket"
    assert _rows(store._conn, "inverter_raw") == 1
    store.close()


def test_inverter_raw_requires_every_hourly_bucket_even_when_minute_exists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _old_raw()
    _insert_inverter_raw(store._conn, timestamp)
    _insert_inverter_destination(store._conn, "inverter_minute", timestamp // 60 * 60)
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.tables[0].blocked == "inverter_hourly does not cover every source bucket"
    assert _rows(store._conn, "inverter_raw") == 1
    store.close()


def test_inverter_raw_goes_once_minute_and_hourly_hold_every_bucket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _old_raw()
    second = first + 61
    for timestamp in (first, second):
        _insert_inverter_raw(store._conn, timestamp)
        _cover_inverter_raw(store._conn, timestamp)
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.tables[0].rows == 2
    assert _rows(store._conn, "inverter_raw") == 0
    store.close()


def test_inverter_minute_requires_every_hourly_bucket(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _epoch(NOW - timedelta(days=MINUTE_DAYS + 1, minutes=3)) // 60 * 60
    _insert_inverter_destination(store._conn, "inverter_minute", timestamp)
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.tables[2].blocked == "inverter_hourly does not cover every source bucket"
    assert _rows(store._conn, "inverter_minute") == 1
    store.close()


def test_raw_outside_minute_retention_does_not_hold_the_minute_tier(tmp_path: Path) -> None:
    """Raw older than minute retention no longer needs a minute witness kept for it."""
    store = _store(tmp_path)
    timestamp = _epoch(NOW - timedelta(days=MINUTE_DAYS + 1, minutes=3)) // 60 * 60
    _insert_inverter_raw(store._conn, timestamp)
    _insert_inverter_destination(store._conn, "inverter_hourly", timestamp // 3600 * 3600)
    _insert_inverter_destination(store._conn, "inverter_minute", timestamp + 60)
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    # Four sources now: the two inverter tiers, the packs, and the circuit
    # readings the Emporia module writes.
    raw, _module, minute, _circuits = report.tables
    assert raw.blocked is None
    assert raw.rows == 1
    assert minute.rows == 1
    assert _rows(store._conn, "inverter_minute") == 0
    store.close()


def test_module_raw_coverage_keeps_each_module_separate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _old_raw()
    _insert_module_raw(store._conn, timestamp, 1)
    _insert_module_raw(store._conn, timestamp, 2)
    _insert_module_hourly(store._conn, timestamp // 3600 * 3600, 1)
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.tables[1].blocked == "module_hourly does not cover every source bucket"
    assert _rows(store._conn, "module_raw") == 2
    store.close()


def test_a_batch_of_failed_polls_does_not_require_coarse_coverage(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_inverter_raw(store._conn, _old_raw(), error="dongle unavailable")
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert report.tables[0].rows == 1
    assert _rows(store._conn, "inverter_raw") == 0
    store.close()


def test_an_error_row_with_a_site_reading_is_not_pruned_without_coverage(tmp_path: Path) -> None:
    """A weather/error collision must retain the sky reading until it has witnesses."""
    store = _store(tmp_path)
    timestamp = _old_raw()
    store._conn.execute(
        "INSERT INTO inverter_raw (timestamp, device, error, outside_temperature_c) "
        "VALUES (?, ?, ?, ?)",
        (timestamp, TEST_DEVICE, "dongle unavailable", 250),
    )
    _archive(tmp_path, NOW)

    report = run_retention(store._conn, _policy(tmp_path), now=NOW)

    assert report.tables[0].blocked == "inverter_minute does not cover every source bucket"
    assert store._conn.execute(
        "SELECT outside_temperature_c FROM inverter_raw WHERE timestamp = ?", (timestamp,)
    ).fetchone() == (250,)
    store.close()


def test_raw_older_than_minute_retention_needs_only_hourly_coverage(tmp_path: Path) -> None:
    """An archive row outside the minute window must not stall newer raw pruning."""
    store = _store(tmp_path)
    backfilled = _epoch(NOW - timedelta(days=500, minutes=3))
    _insert_inverter_raw(store._conn, backfilled)
    _insert_inverter_destination(store._conn, "inverter_hourly", backfilled // 3600 * 3600)
    for offset in range(200):
        timestamp = _old_raw() + offset
        _insert_inverter_raw(store._conn, timestamp)
        _cover_inverter_raw(store._conn, timestamp)
    _archive(tmp_path, NOW)

    report = run_retention(store._conn, _policy(tmp_path), now=NOW, batch_rows=500)

    assert report.tables[0].blocked is None
    assert report.tables[0].rows == 201
    assert _rows(store._conn, "inverter_raw") == 0
    store.close()


def test_a_pending_backfill_hour_keeps_its_raw_site_reading(tmp_path: Path) -> None:
    """Retention must wait for queued promotion before removing a backfilled sky row."""
    store = _store(tmp_path)
    timestamp = _epoch(NOW - timedelta(days=500, minutes=3))
    hour = timestamp // 3600 * 3600
    store._conn.execute(
        "INSERT INTO inverter_raw (timestamp, device, outside_temperature_c) VALUES (?, ?, ?)",
        (timestamp, TEST_DEVICE, 250),
    )
    _insert_inverter_destination(store._conn, "inverter_minute", timestamp // 60 * 60)
    _insert_inverter_destination(store._conn, "inverter_hourly", hour)
    store._conn.execute(f"INSERT INTO {PENDING_TABLE} (hour) VALUES (?)", (hour,))
    _archive(tmp_path, NOW)

    report = run_retention(store._conn, _policy(tmp_path), now=NOW)

    assert report.tables[0].blocked == "rollup_pending has unfinished work"
    assert _rows(store._conn, "inverter_raw") == 1
    assert promote_pending_hours(store._conn) == 1
    assert store._conn.execute(
        "SELECT outside_temperature_c FROM inverter_hourly WHERE timestamp = ?", (hour,)
    ).fetchone() == (250,)
    store.close()


def test_coverage_and_delete_begin_with_an_immediate_write_lock(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """A concurrent writer cannot slip an unchecked row between coverage and deletion."""
    store = _store(tmp_path)
    timestamp = _old_raw()
    _insert_inverter_raw(store._conn, timestamp)
    _cover_inverter_raw(store._conn, timestamp)
    _archive(tmp_path, NOW)
    statements: list[str] = []
    original = retention_module._uncovered_destination

    def assert_locked(
        conn: sqlite3.Connection, source: Any, floor: int, boundary: int, *, now: datetime
    ) -> str | None:
        assert conn.in_transaction
        return original(conn, source, floor, boundary, now=now)

    monkeypatch.setattr(retention_module, "_uncovered_destination", assert_locked)
    store._conn.set_trace_callback(statements.append)
    try:
        run_retention(store._conn, _policy(tmp_path), now=NOW)
    finally:
        store._conn.set_trace_callback(None)

    assert "BEGIN IMMEDIATE" in statements
    store.close()


def test_batches_are_bounded_and_next_pass_resumes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _old_raw()
    for offset in range(5):
        timestamp = first + offset
        _insert_inverter_raw(store._conn, timestamp)
        _cover_inverter_raw(store._conn, timestamp)
    _archive(tmp_path, NOW)
    deletes: list[tuple[int, int]] = []

    def record_delete(statement: str) -> None:
        match = re.match(
            r"DELETE FROM inverter_raw WHERE timestamp >= (\d+) AND timestamp < (\d+)", statement
        )
        if match is not None:
            deletes.append((int(match.group(1)), int(match.group(2))))

    store._conn.set_trace_callback(record_delete)
    first_pass = run_retention(store._conn, _policy(tmp_path), now=NOW, batch_rows=2, max_batches=2)
    store._conn.set_trace_callback(None)
    second_pass = run_retention(
        store._conn, _policy(tmp_path), now=NOW, batch_rows=2, max_batches=2
    )
    assert first_pass.tables[0].rows == 4
    assert second_pass.tables[0].rows == 1
    assert _rows(store._conn, "inverter_raw") == 0
    assert all(boundary - floor <= 2 for floor, boundary in deletes)
    store.close()


def test_capped_passes_keep_minute_only_where_its_policy_still_covers_raw(tmp_path: Path) -> None:
    """Capped raw walks do not preserve obsolete minute coverage indefinitely."""
    store = _store(tmp_path)
    start = _epoch(NOW - timedelta(days=3))
    end = _epoch(NOW)
    for timestamp in range(start, end + 1, 30):
        _insert_inverter_raw(store._conn, timestamp)
    for timestamp in range(start, end + 1, 60):
        _insert_inverter_destination(store._conn, "inverter_minute", timestamp)
    for timestamp in range(start, end + 1, 3600):
        _insert_inverter_destination(store._conn, "inverter_hourly", timestamp)
    _archive(tmp_path, NOW)
    policy = RetentionPolicy(True, raw_days=1, minute_days=2, backup_directory=str(tmp_path))
    previous_minute_floor = start

    for _ in range(10):
        report = run_retention(store._conn, policy, now=NOW, batch_rows=1000, max_batches=1)
        raw_floor = store._conn.execute(
            "SELECT timestamp FROM inverter_raw ORDER BY timestamp LIMIT 1"
        ).fetchone()
        minute_floor = store._conn.execute(
            "SELECT timestamp FROM inverter_minute ORDER BY timestamp LIMIT 1"
        ).fetchone()
        assert raw_floor is not None
        assert minute_floor is not None
        minute_coverage_start = _epoch(NOW - timedelta(days=policy.minute_days))
        if int(raw_floor[0]) >= minute_coverage_start:
            assert int(minute_floor[0]) <= int(raw_floor[0]) // 60 * 60
        else:
            assert int(minute_floor[0]) > previous_minute_floor
        previous_minute_floor = int(minute_floor[0])
        assert report.tables[0].blocked is None
        if int(raw_floor[0]) >= _epoch(NOW - timedelta(days=1)):
            break
    else:
        raise AssertionError("raw retention did not converge")

    assert int(raw_floor[0]) == _epoch(NOW - timedelta(days=1))
    store.close()


def test_capped_passes_cross_an_unaligned_minute_retention_boundary(tmp_path: Path) -> None:
    """Raw reaches its cutoff after minute drops the preceding partial bucket."""
    store = _store(tmp_path)
    now = NOW + timedelta(seconds=30)
    start = _epoch(now - timedelta(days=3))
    end = _epoch(now)
    for timestamp in range(start, end + 1, 30):
        _insert_inverter_raw(store._conn, timestamp)
    for timestamp in range(start // 60 * 60, end + 1, 60):
        _insert_inverter_destination(store._conn, "inverter_minute", timestamp)
    for timestamp in range(start // 3600 * 3600, end + 1, 3600):
        _insert_inverter_destination(store._conn, "inverter_hourly", timestamp)
    _archive(tmp_path, now)
    policy = RetentionPolicy(True, raw_days=1, minute_days=2, backup_directory=str(tmp_path))
    raw_cutoff = _epoch(now - timedelta(days=policy.raw_days))

    for _ in range(10):
        report = run_retention(store._conn, policy, now=now, batch_rows=1000, max_batches=1)
        raw_floor = store._conn.execute(
            "SELECT timestamp FROM inverter_raw ORDER BY timestamp LIMIT 1"
        ).fetchone()
        assert report.tables[0].blocked is None
        assert raw_floor is not None
        if int(raw_floor[0]) >= raw_cutoff:
            break
    else:
        raise AssertionError("raw retention did not converge")

    assert int(raw_floor[0]) == raw_cutoff
    store.close()


def test_raw_crosses_a_pruned_unaligned_minute_boundary(tmp_path: Path) -> None:
    """A missing partial minute bucket cannot stall raw retention forever."""
    store = _store(tmp_path)
    now = datetime(2026, 8, 13, 12, 0, 30, tzinfo=UTC)
    assert now.second != 0
    policy = _policy(tmp_path)
    minute_cutoff = _epoch(now - timedelta(days=policy.minute_days))
    straddling_minute = minute_cutoff // 60 * 60
    assert straddling_minute < minute_cutoff < straddling_minute + 60

    # The minute tier has already deleted the partial bucket before its cutoff.
    # Raw still has polls in that bucket, whose hourly witness is durable.
    for timestamp in (straddling_minute, minute_cutoff - 1):
        _insert_inverter_raw(store._conn, timestamp)
    _insert_inverter_destination(store._conn, "inverter_hourly", straddling_minute // 3600 * 3600)
    _insert_inverter_destination(store._conn, "inverter_minute", straddling_minute + 60)
    _archive(tmp_path, now)

    report = run_retention(store._conn, policy, now=now)

    # Four sources now: the two inverter tiers, the packs, and the circuit
    # readings the Emporia module writes.
    raw, _module, minute, _circuits = report.tables
    assert raw.blocked is None
    assert raw.rows == 2
    assert _rows(store._conn, "inverter_raw") == 0
    assert minute.blocked is None
    assert store._conn.execute(
        "SELECT timestamp FROM inverter_minute ORDER BY timestamp"
    ).fetchall() == [(straddling_minute + 60,)]
    store.close()


def test_same_timestamp_rows_make_progress_instead_of_looping(tmp_path: Path) -> None:
    store = _store(tmp_path)
    timestamp = _old_raw()
    for device in (TEST_DEVICE, "CE00000001", "CE00000002"):
        store._conn.execute(
            "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)", (timestamp, device)
        )
        for table, period in (("inverter_minute", 60), ("inverter_hourly", 3600)):
            _insert_inverter_destination(
                store._conn, table, timestamp // period * period, device=device
            )
    _archive(tmp_path, NOW)
    report = run_retention(store._conn, _policy(tmp_path), now=NOW, batch_rows=1, max_batches=3)
    assert report.tables[0].rows == 3
    assert _rows(store._conn, "inverter_raw") == 0
    store.close()


def test_hourly_tiers_and_recent_rows_are_never_touched(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = _old_raw()
    recent = _epoch(NOW - timedelta(days=1))
    _insert_inverter_raw(store._conn, old)
    _insert_inverter_raw(store._conn, recent)
    _cover_inverter_raw(store._conn, old)
    _insert_module_hourly(store._conn, old // 3600 * 3600, 1)
    _archive(tmp_path, NOW)
    run_retention(store._conn, _policy(tmp_path), now=NOW)
    assert _rows(store._conn, "inverter_hourly") == 1
    assert _rows(store._conn, "module_hourly") == 1
    assert store._conn.execute(
        "SELECT 1 FROM inverter_raw WHERE timestamp = ?", (recent,)
    ).fetchone()
    store.close()


def test_settings_policy_defaults_come_from_declared_tiers(tmp_path: Path) -> None:
    store = _store(tmp_path)
    policy = policy_from_settings(SettingsStore(store))
    raw_days = {tier.keep_days for tier in (*INVERTER_TIERS, *MODULE_TIERS) if tier.name == "full"}
    minute_days = next(tier.keep_days for tier in INVERTER_TIERS if tier.name == "minute")
    assert not policy.enabled
    assert raw_days == {policy.raw_days}
    assert policy.minute_days == minute_days
    store.close()


# --- circuits ------------------------------------------------------------


def _old_circuit_hour() -> int:
    """An hour comfortably past the raw cutoff, aligned to the bucket."""
    return int((NOW - timedelta(days=RAW_DAYS + 10)).timestamp()) // 3600 * 3600


def _a_circuit(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO circuit (id, device_gid, channel_num, name, multiplier, kind,"
        " first_seen, last_seen) VALUES (1, 100000, '5', 'Dryer', 2.0, 'circuit', 0, 0)"
    )


def test_circuit_readings_are_pruned_once_the_hourly_tier_covers_them(tmp_path: Path) -> None:
    # The fastest-growing table in the store — one row per circuit per minute —
    # and the same rule as everything else: nothing is deleted until a coarser
    # table already holds what it said.
    store = _store(tmp_path)
    _archive(tmp_path, NOW)
    _a_circuit(store._conn)
    hour = _old_circuit_hour()
    for minute in range(3):
        store._conn.execute(
            "INSERT INTO circuit_reading (timestamp, circuit_id, watts) VALUES (?, 1, 100)",
            (hour + minute * 60,),
        )
    rebuild_circuit_hourly(store._conn, hour, hour + 3600, cadence_seconds=60)

    report = run_retention(store._conn, _policy(tmp_path), now=NOW)

    pruned = {table.table: table for table in report.tables}
    assert "circuit_reading" in pruned, "circuit readings must be in the retention pass at all"
    assert pruned["circuit_reading"].blocked is None
    assert pruned["circuit_reading"].rows == 3
    assert _rows(store._conn, "circuit_reading") == 0
    assert _rows(store._conn, "circuit_hourly") == 1, "the coarse row outlives what it covers"
    store.close()


def test_uncovered_circuit_readings_are_left_alone(tmp_path: Path) -> None:
    # No rollup, no deletion. Without this, switching the module off and leaving
    # a final unrolled hour would let the next pass destroy it.
    store = _store(tmp_path)
    _archive(tmp_path, NOW)
    _a_circuit(store._conn)
    store._conn.execute(
        "INSERT INTO circuit_reading (timestamp, circuit_id, watts) VALUES (?, 1, 100)",
        (_old_circuit_hour(),),
    )

    report = run_retention(store._conn, _policy(tmp_path), now=NOW)

    pruned = {table.table: table for table in report.tables}
    assert pruned["circuit_reading"].rows == 0
    assert pruned["circuit_reading"].blocked is not None
    assert _rows(store._conn, "circuit_reading") == 1
    store.close()
