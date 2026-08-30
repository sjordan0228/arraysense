"""retention.py — prune raw tiers only after their durable copies are present.

The raw tiers make an outage inspectable at poll cadence, but keeping every
poll forever grows an unattended database without bound. This module deletes
only bounded batches whose coarse buckets already exist, so an interrupted
pass loses neither a range nor the raw material needed to rebuild one. A pure
failed-poll row is deliberately not a coverage witness: rollups represent an
outage as a missing bucket, and pruning later necessarily loses its exact
timing. A same-second sky reading changes that: it is evidence that must stay
until the coarse tiers hold it, even if the inverter half of the row failed.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arraysense.metrics import SITE_METRICS
from arraysense.settings import (
    BACKUP_DIRECTORY_KEY,
    RETENTION_ENABLED_KEY,
    RETENTION_MINUTE_DAYS_KEY,
    RETENTION_RAW_DAYS_KEY,
    SettingsStore,
)
from arraysense.store.rollup import bucket_sql
from arraysense.store.schema import PENDING_TABLE

logger = logging.getLogger(__name__)

BATCH_ROWS = 5000
MAX_BATCHES_PER_PASS = 20


@dataclass(frozen=True)
class RetentionPolicy:
    """The settings that determine whether a retention pass may delete rows."""

    enabled: bool
    raw_days: int
    minute_days: int
    backup_directory: str


@dataclass(frozen=True)
class TablePrune:
    """The result of considering one source table for deletion."""

    table: str
    cutoff: datetime
    rows: int
    oldest: datetime | None
    blocked: str | None


@dataclass(frozen=True)
class RetentionReport:
    """The observable outcome of one real or dry-run retention pass."""

    dry_run: bool
    ran: bool
    reason: str | None
    tables: tuple[TablePrune, ...]


@dataclass(frozen=True)
class _Destination:
    """A coarse table that must hold every source bucket before pruning."""

    table: str
    period: int
    keep_days: int | None = None


@dataclass(frozen=True)
class _PruneTable:
    """One prunable source and the coverage it must retain behind it."""

    table: str
    days: int
    destinations: tuple[_Destination, ...]
    has_error: bool
    # What identifies a row besides its time. Coverage is checked against the
    # destination's exact primary key, so this has to be the rest of that key:
    # the device for an inverter tier, the device and the module for a pack,
    # the circuit alone for a circuit reading — which carries no device at all,
    # because a Vue monitor is not an inverter.
    key_columns: tuple[str, ...] = ("device",)
    has_pending_hours: bool = False


@dataclass(frozen=True)
class _PruneResult:
    """Keep a table's post-walk floor for destinations pruned later."""

    source: _PruneTable
    report: TablePrune
    remaining_oldest: datetime | None


def policy_from_settings(settings: SettingsStore) -> RetentionPolicy:
    """Read the retention settings once for callers that initiate a pass.

    The collector and manual CLI path must use exactly the same policy; copying
    this mapping into either would make a dry run answer a different question
    from the next scheduled pass.
    """
    enabled = settings.get(RETENTION_ENABLED_KEY)
    raw_days = settings.get(RETENTION_RAW_DAYS_KEY)
    minute_days = settings.get(RETENTION_MINUTE_DAYS_KEY)
    backup_directory = settings.get(BACKUP_DIRECTORY_KEY)
    if not isinstance(enabled, bool):
        raise ValueError(f"{RETENTION_ENABLED_KEY} did not decode as a boolean")
    if not isinstance(raw_days, int):
        raise ValueError(f"{RETENTION_RAW_DAYS_KEY} did not decode as an integer")
    if not isinstance(minute_days, int):
        raise ValueError(f"{RETENTION_MINUTE_DAYS_KEY} did not decode as an integer")
    if not isinstance(backup_directory, str):
        raise ValueError(f"{BACKUP_DIRECTORY_KEY} did not decode as text")
    return RetentionPolicy(enabled, raw_days, minute_days, backup_directory)


def run_retention(
    conn: sqlite3.Connection,
    policy: RetentionPolicy,
    *,
    now: datetime,
    dry_run: bool = False,
    batch_rows: int = BATCH_ROWS,
    max_batches: int = MAX_BATCHES_PER_PASS,
) -> RetentionReport:
    """Prune one bounded pass, or report the identical work without deleting.

    A current backup is checked before even the first table, because rows are
    irreversible once SQLite reuses their pages. Each table then independently
    verifies every destination bucket in its next batch, so a missing coarse
    bucket stops at the first unsafe range rather than silently skipping it.
    """
    if batch_rows < 1:
        raise ValueError("batch_rows must be positive")
    if max_batches < 1:
        raise ValueError("max_batches must be positive")
    if not policy.enabled:
        return RetentionReport(dry_run, False, "retention.enabled is false", ())

    backup_mtime, backup_reason = _newest_backup_mtime(policy.backup_directory)
    if backup_reason is not None:
        logger.warning("retention did not run: %s", backup_reason)
        return RetentionReport(dry_run, False, backup_reason, ())
    assert backup_mtime is not None

    # Order matters twice over. Each source's cutoff is clamped against what
    # earlier ones left behind, so a destination that is itself pruned must
    # come after the source that depends on it — and tests/test_retention.py
    # indexes this tuple by position, so append rather than insert.
    tables = (
        _PruneTable(
            "inverter_raw",
            policy.raw_days,
            (
                _Destination("inverter_minute", 60, policy.minute_days),
                _Destination("inverter_hourly", 3600),
            ),
            has_error=True,
            has_pending_hours=True,
        ),
        _PruneTable(
            "module_raw",
            policy.raw_days,
            (_Destination("module_hourly", 3600),),
            has_error=False,
            key_columns=("device", "module_id"),
        ),
        _PruneTable(
            "inverter_minute",
            policy.minute_days,
            (_Destination("inverter_hourly", 3600),),
            has_error=True,
        ),
        # Circuits are core storage even though only an optional module writes
        # them, so they are pruned here rather than by the module: an
        # installation that switched Emporia off must still have its readings
        # aged out, and the backup check that guards every deletion above is
        # the one thing this table must not do without.
        _PruneTable(
            "circuit_reading",
            policy.raw_days,
            (_Destination("circuit_hourly", 3600),),
            has_error=False,
            key_columns=("circuit_id",),
        ),
    )
    results: list[_PruneResult] = []
    for source in tables:
        cutoff = _clamped_cutoff(now - timedelta(days=source.days), source, tuple(results), now=now)
        results.append(
            _prune_table(
                conn,
                source,
                cutoff,
                backup_mtime,
                now=now,
                dry_run=dry_run,
                batch_rows=batch_rows,
                max_batches=max_batches,
            )
        )
    pruned = tuple(result.report for result in results)
    backup_blocked = [
        table.table for table in pruned if table.blocked and "backup" in table.blocked
    ]
    if backup_blocked:
        logger.warning(
            "retention did not prune %s: backup is older than the cutoff", ", ".join(backup_blocked)
        )
    return RetentionReport(
        dry_run,
        True,
        None,
        pruned,
    )


def _clamped_cutoff(
    cutoff: datetime,
    source: _PruneTable,
    previous: tuple[_PruneResult, ...],
    *,
    now: datetime,
) -> datetime:
    """Keep a destination at or before the oldest finer row that still needs it.

    A capped finer walk can leave more history than its own configured window.
    Its destination must not pass that surviving floor, or the next walk loses
    the coverage witness it needs and cannot make forward progress. Reading the
    links from ``destinations`` keeps that guard correct when another tier is
    added without teaching this function its table name.
    """
    for result in previous:
        if result.remaining_oldest is None:
            continue
        for destination in result.source.destinations:
            if destination.table != source.table:
                continue
            coverage_start = _coverage_start(destination, now)
            bucket = (
                int(result.remaining_oldest.timestamp()) // destination.period * destination.period
            )
            if coverage_start is not None and bucket < int(coverage_start.timestamp()):
                continue
            cutoff = min(cutoff, datetime.fromtimestamp(bucket, tz=UTC))
    return cutoff


def _newest_backup_mtime(directory: str) -> tuple[float | None, str | None]:
    """Return the newest archive mtime, or why no archive can be trusted."""
    path = Path(directory)
    try:
        if not path.is_dir():
            return None, f"backup directory {directory!r} cannot be read"
        archives = list(path.glob("arraysense-*.db.gz"))
        mtimes = [archive.stat().st_mtime for archive in archives]
    except OSError:
        return None, f"backup directory {directory!r} cannot be read"
    if not mtimes:
        return None, f"no backup archive exists in {directory!r}"
    return max(mtimes), None


def _prune_table(
    conn: sqlite3.Connection,
    source: _PruneTable,
    cutoff: datetime,
    backup_mtime: float,
    *,
    now: datetime,
    dry_run: bool,
    batch_rows: int,
    max_batches: int,
) -> _PruneResult:
    """Walk one source table forward, stopping at its first unsafe batch."""
    cutoff_epoch = int(cutoff.timestamp())
    oldest = _oldest(conn, source.table)
    if backup_mtime < cutoff.timestamp():
        backup_block = "newest backup archive is older than the retention cutoff"
        return _PruneResult(
            source,
            TablePrune(source.table, cutoff, 0, oldest, backup_block),
            oldest,
        )
    floor = _first_candidate(conn, source.table, cutoff_epoch)
    deleted = 0
    blocked: str | None = None
    batches = 0
    while floor is not None and floor < cutoff_epoch and batches < max_batches:
        if dry_run:
            boundary = _batch_end(conn, source.table, floor, cutoff_epoch, batch_rows)
            destination = _uncovered_destination(conn, source, floor, boundary, now=now)
            count = _batch_count(conn, source.table, floor, boundary)
        else:
            boundary, destination, count = _delete_covered_batch(
                conn, source, floor, cutoff_epoch, batch_rows, now=now
            )
        if destination is not None:
            blocked = (
                "rollup_pending has unfinished work"
                if destination == PENDING_TABLE
                else f"{destination} does not cover every source bucket"
            )
            break
        deleted += count
        batches += 1
        floor = boundary
    return _PruneResult(
        source,
        TablePrune(source.table, cutoff, deleted, oldest, blocked),
        _remaining_oldest(conn, source.table, floor),
    )


def _delete_covered_batch(
    conn: sqlite3.Connection,
    source: _PruneTable,
    floor: int,
    cutoff: int,
    batch_rows: int,
    *,
    now: datetime,
) -> tuple[int, str | None, int]:
    """Delete one covered batch while its coverage snapshot has the write lock.

    A deferred transaction acquires its snapshot on the coverage read, then
    can fail to upgrade after a backfill writer commits. ``BEGIN IMMEDIATE``
    obtains the write lock first, so the count and delete describe the exact
    set of rows no other writer can change before this transaction commits.
    """
    conn.execute("BEGIN IMMEDIATE")
    with conn:
        boundary = _batch_end(conn, source.table, floor, cutoff, batch_rows)
        destination = _uncovered_destination(conn, source, floor, boundary, now=now)
        count = _batch_count(conn, source.table, floor, boundary)
        if destination is None:
            conn.execute(
                f"DELETE FROM {source.table} WHERE timestamp >= ? AND timestamp < ?",
                (floor, boundary),
            )
    return boundary, destination, count


def _remaining_oldest(conn: sqlite3.Connection, table: str, floor: int | None) -> datetime | None:
    """Return the real or dry-run oldest row left after a forward walk."""
    if floor is None:
        return _oldest(conn, table)
    row = conn.execute(
        f"SELECT timestamp FROM {table} WHERE timestamp >= ? ORDER BY timestamp LIMIT 1", (floor,)
    ).fetchone()
    return None if row is None else datetime.fromtimestamp(int(row[0]), tz=UTC)


def _oldest(conn: sqlite3.Connection, table: str) -> datetime | None:
    """Return the oldest timestamp retained in a table, if it has one."""
    row = conn.execute(f"SELECT timestamp FROM {table} ORDER BY timestamp LIMIT 1").fetchone()
    if row is None:
        return None
    return datetime.fromtimestamp(int(row[0]), tz=UTC)


def _first_candidate(conn: sqlite3.Connection, table: str, cutoff: int) -> int | None:
    """Return the oldest row below the cutoff, the leading edge of the walk."""
    row = conn.execute(
        f"SELECT timestamp FROM {table} WHERE timestamp < ? ORDER BY timestamp LIMIT 1", (cutoff,)
    ).fetchone()
    return None if row is None else int(row[0])


def _batch_end(conn: sqlite3.Connection, table: str, floor: int, cutoff: int, rows: int) -> int:
    """Return the exclusive edge of the next bounded batch.

    Rows sharing a timestamp must go together because time leads every raw
    primary key. Moving the edge one second past such a group is the smallest
    forward step and keeps a pass from selecting the same batch forever.
    """
    row = conn.execute(
        f"SELECT timestamp FROM {table} WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp LIMIT 1 OFFSET ?",
        (floor, cutoff, rows),
    ).fetchone()
    boundary = cutoff if row is None else int(row[0])
    return floor + 1 if boundary <= floor else boundary


def _batch_count(conn: sqlite3.Connection, table: str, floor: int, boundary: int) -> int:
    """Count a batch before deleting it, which makes dry runs equivalent."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE timestamp >= ? AND timestamp < ?", (floor, boundary)
    ).fetchone()
    return int(row[0])


def _uncovered_destination(
    conn: sqlite3.Connection,
    source: _PruneTable,
    floor: int,
    boundary: int,
    *,
    now: datetime,
) -> str | None:
    """Return the first coarse destination missing a source bucket, if any."""
    if source.has_pending_hours and _pending_hour_count(conn, source, floor, boundary):
        return PENDING_TABLE
    for destination in source.destinations:
        if _uncovered_count(
            conn,
            source,
            destination,
            floor,
            boundary,
            coverage_start=_coverage_start(destination, now),
        ):
            return destination.table
    return None


def _coverage_start(destination: _Destination, now: datetime) -> datetime | None:
    """Return the first bucket a destination's retention policy must still hold.

    A table removes bucket rows strictly before its timestamp cutoff. When that
    cutoff falls within a bucket, the partial bucket before it is gone too, so
    coverage must begin at the next boundary rather than demand a witness that
    the destination correctly pruned.
    """
    if destination.keep_days is None:
        return None
    cutoff = int((now - timedelta(days=destination.keep_days)).timestamp())
    bucket = cutoff // destination.period * destination.period
    if bucket < cutoff:
        bucket += destination.period
    return datetime.fromtimestamp(bucket, tz=UTC)


def _pending_hour_count(
    conn: sqlite3.Connection, source: _PruneTable, floor: int, boundary: int
) -> int:
    """Count source hours awaiting their queued backfill promotion."""
    bucket = bucket_sql("timestamp", 3600)
    row = conn.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT DISTINCT {bucket} AS hour FROM {source.table} "
        f"WHERE timestamp >= ? AND timestamp < ?"
        f") src WHERE EXISTS ("
        f"SELECT 1 FROM {PENDING_TABLE} pending WHERE pending.hour = src.hour"
        f")",
        (floor, boundary),
    ).fetchone()
    return int(row[0])


def _uncovered_count(
    conn: sqlite3.Connection,
    source: _PruneTable,
    destination: _Destination,
    floor: int,
    boundary: int,
    *,
    coverage_start: datetime | None,
) -> int:
    """Count source buckets that lack this destination's exact primary key."""
    bucket = bucket_sql("timestamp", destination.period)
    columns = ", ".join((f"{bucket} AS bucket", *source.key_columns))
    lookup = " AND ".join(
        ("d.timestamp = src.bucket", *(f"d.{name} = src.{name}" for name in source.key_columns))
    )
    coverage_filter = ""
    parameters: tuple[int, ...] = (floor, boundary)
    if coverage_start is not None:
        coverage_filter = f" AND {bucket} >= ?"
        parameters += (int(coverage_start.timestamp()),)
    error_filter = _coverage_error_filter(conn, source)
    row = conn.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT DISTINCT {columns} FROM {source.table} "
        f"WHERE timestamp >= ? AND timestamp < ?{coverage_filter}{error_filter}"
        f") src WHERE NOT EXISTS ("
        f"SELECT 1 FROM {destination.table} d WHERE {lookup}"
        f")",
        parameters,
    ).fetchone()
    return int(row[0])


def _coverage_error_filter(conn: sqlite3.Connection, source: _PruneTable) -> str:
    """Exclude only pure gaps, which have no reading a rollup could preserve."""
    if not source.has_error:
        return ""
    columns = [
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({source.table})")
        if str(row[1]) in SITE_METRICS
    ]
    if not columns:
        return " AND error IS NULL"
    carries_site = " OR ".join(f"{column} IS NOT NULL" for column in columns)
    return f" AND (error IS NULL OR ({carries_site}))"
