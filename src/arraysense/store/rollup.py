"""rollup.py — collapse full-cadence readings into the coarse tiers, from raw only.

The full-cadence tier is kept for 30 days and the coarse tiers far longer —
hourly indefinitely — so a rollup mistake is permanent in a way a mistake in
the raw tier is not. These functions rebuild a destination tier from the raw
tier over a time range, reading each metric's collapse policy
(``MetricSpec.aggregation``) from the registry rather than inferring it from a
name: most measurements average, a cycle counter takes its maximum, an extreme
stays an extreme, and a cell *number* field takes the latest value because the
average of two cell numbers is a cell that does not exist.

Every tier derives from raw; no tier derives from another. Recombining minute
buckets into hourly would need each bucket weighted by how many readings it
covers, but metrics can be absent independently — ``AVG`` skips NULLs while
``sample_count`` counts every row — so a metric present in one row of a
two-row bucket would be weighted as though it appeared twice, and weighting
would multiply already-rounded minute values. Building from raw lets ``AVG``
ignore absent readings per metric, correctly and without weighting.

Two rules keep a rebuild correct and repeatable.

- A bucket's timestamp is the start of the period it covers, derived
  arithmetically as ``epoch // period * period`` so two writers agree on the
  boundary without coordinating. SQLite's ``/`` truncates toward zero, so the
  floor is computed arithmetically; a negative epoch floors downward. An
  hourly row therefore falls exactly on an hour boundary (UTC), never on a
  rounded local time.
- A rebuild deletes the destination range and reinserts it within one
  transaction. That is what makes it idempotent — running it twice over the
  same range leaves the same rows — and what stops a bucket that has lost its
  source rows from lingering as stale.

A row recording a failed poll carries an error reason and no readings
(``models.Sample.failed``); it must not be counted as a reading of zero, so
the inverter rollup excludes it from both the aggregate and the sample count.
Averages are rounded to integers before storage — the tables are STRICT and
reject a real.

Every range here is integer epoch seconds and half-open, ``[start, end)``, and
is widened outward to whole buckets before anything is deleted or read.
"""

from __future__ import annotations

import sqlite3

from arraysense.metrics import INVERTER_METRICS, lookup
from arraysense.store.schema import module_metric_columns

# A failed poll is stored with its reason and no readings (see models.Sample).
# Excluding it from the aggregate stops it being counted as a reading of zero.
_ERROR_FILTER = "error IS NULL"


def _bucket_bounds(bucket_seconds: int, start: int, end: int) -> tuple[int, int]:
    """Return the rebuild range expanded outward to whole buckets.

    Both the delete and the source filter use these bounds, so a rebuild
    affects exactly the buckets that overlap ``[start, end)`` and no others.
    Expanding outward is what keeps idempotency exact: a source row just
    before ``start`` still belongs to the first overlapping bucket, so it has
    to be read on every run or the same request would produce a different
    bucket each time. Python's ``//`` floors, so a negative boundary aligns
    downward.
    """
    return (
        start // bucket_seconds * bucket_seconds,
        ((end + bucket_seconds - 1) // bucket_seconds) * bucket_seconds,
    )


def _floor_div(column: str, divisor: int) -> str:
    """Return a SQL expression that floor-divides ``column`` by ``divisor``.

    Python's ``//`` and SQLite's ``/`` disagree below zero: SQLite truncates
    toward zero, so a pre-1970 timestamp would land in the bucket above the
    one Python computed, and a rebuild could insert a row outside the range it
    just deleted — the next rebuild over that range then hits the primary key
    instead of replacing it. The fix is to subtract the remainder before
    dividing, so the numerator is already an exact multiple of the divisor and
    truncation has nothing left to do. The inner ``+ divisor`` is what makes
    that remainder non-negative, since SQLite's ``%`` carries the sign of the
    left operand: at -100 with a divisor of 60, plain ``/`` gives -1 while this
    gives -2, matching Python. That holds the contract that a bucket boundary is
    ``epoch // period * period`` everywhere.
    """
    return f"({column} - (({column} % {divisor}) + {divisor}) % {divisor}) / {divisor}"


def _agg_expr(column: str, aggregation: str, last_rn: str) -> str:
    """Return the SQL aggregate producing one metric's rolled-up value.

    The policy is read from the registry and dispatched here, never inferred
    from the column name, so a metric that collapses unusually cannot be given
    the wrong aggregate by being named like an ordinary measurement.

    ``mean`` rounds to the nearest integer — the STRICT tables reject a real.
    SQLite's ``AVG`` returns a real even from integer inputs, so no integer
    division truncates before rounding. ``max`` and ``min`` keep the extreme.

    ``last`` takes the value from the latest source row that *reported* the
    metric, which is not the same as the bucket's last row: ``last_rn`` names a
    per-metric row number that ranks rows by timestamp with rows missing the
    metric sorted last, so ``last_rn = 1`` marks the latest reported row and
    ``MAX`` over that single candidate is the value. Without that, a metric the
    inverter reported all hour but omitted from the closing read would roll up
    as NULL. Absent values stay absent throughout — ``AVG``, ``MAX`` and
    ``MIN`` skip NULLs, and a ``last`` value that is NULL stays NULL — because
    a reading nobody took must never surface as a zero.

    Raises:
        AssertionError: ``aggregation`` is not one of the registry's four
            policies, which is a programming error, not a data condition.
    """
    if aggregation == "mean":
        return f"CAST(ROUND(AVG({column})) AS INTEGER)"
    if aggregation == "max":
        return f"MAX({column})"
    if aggregation == "min":
        return f"MIN({column})"
    if aggregation == "last":
        return f"MAX({column}) FILTER (WHERE {last_rn} = 1)"
    raise AssertionError(f"unhandled aggregation policy: {aggregation!r}")


def _last_rn_expr(column: str, bucket_seconds: int, module: bool) -> str:
    """Return a SQL row number marking the latest row that reported ``column``.

    Rows are ordered by timestamp descending with rows missing the metric
    (sort key NULL) sorted last, so ``rn = 1`` is the latest source row that
    carried a value rather than merely the latest row. The "last" collapse
    policy rests on that distinction: it keeps a value an earlier row reported
    even when the closing row omits it.

    Module tiers are normalised, so ``module`` adds ``module_id`` to the
    partition; without it one pack's reading would decide another pack's
    rolled-up value. The expression comes back unaliased because the alias has
    to match the one handed to ``_agg_expr``, and only the caller knows it.
    """
    part = _floor_div("timestamp", bucket_seconds)
    partition = f"{part}, module_id" if module else part
    return (
        f"ROW_NUMBER() OVER (PARTITION BY {partition} "
        f"ORDER BY CASE WHEN {column} IS NOT NULL THEN timestamp END DESC)"
    )


def _rebuild_inverter(
    conn: sqlite3.Connection,
    source: str,
    dest: str,
    bucket_seconds: int,
    start: int,
    end: int,
    columns: tuple[str, ...],
) -> None:
    """Rebuild a wide-row inverter tier from raw over [start, end).

    ``columns`` carries full registry names, unlike the bare ones its
    per-module counterpart takes below. Each is passed straight to ``lookup``
    for its aggregation policy, so a bare module metric handed to this function
    raises rather than rolling up under the wrong policy.

    Deletes the destination buckets and reinserts them within one transaction,
    so a rebuild is idempotent and a bucket whose source rows have gone is
    dropped rather than left behind as a stale average nothing supports. Each
    bucket's timestamp is the start of the period it covers, derived by floor
    division.

    Failed-poll rows are excluded from the source (see ``_ERROR_FILTER``), so
    an hour in which every poll failed produces no row at all. That gap is the
    honest answer: a bucket averaging an outage into the readings around it
    would draw a chart that never lost contact with the inverter.

    ``sample_count`` records how many successful source rows the bucket
    covers, and is a record of coverage only — every tier is built from raw, so
    nothing downstream ever weights by it.
    """
    aligned_start, aligned_end = _bucket_bounds(bucket_seconds, start, end)
    part = _floor_div("timestamp", bucket_seconds)
    last_aliases = ", ".join(
        f"{_last_rn_expr(column, bucket_seconds, False)} AS last_{column}"
        for column in columns
        if lookup(column).aggregation == "last"
    )
    agg = ", ".join(
        f"{_agg_expr(column, lookup(column).aggregation, f'last_{column}')} AS {column}"
        for column in columns
    )
    inner = f"SELECT {part} AS bucket, *"
    if last_aliases:
        inner += f", {last_aliases}"
    inner += f" FROM {source} WHERE timestamp >= ? AND timestamp < ? AND {_ERROR_FILTER}"
    select = (
        f"SELECT {part} * {bucket_seconds} AS timestamp, "
        f"COUNT(*) AS sample_count, {agg} "
        f"FROM ({inner}) GROUP BY bucket"
    )
    cols_sql = ", ".join(("timestamp", "sample_count", *columns))
    with conn:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM {dest} WHERE timestamp >= ? AND timestamp < ?",
            (aligned_start, aligned_end),
        )
        cur.execute(
            f"INSERT INTO {dest} ({cols_sql}) {select}",
            (aligned_start, aligned_end),
        )


def _rebuild_module(
    conn: sqlite3.Connection,
    source: str,
    dest: str,
    bucket_seconds: int,
    start: int,
    end: int,
    columns: tuple[str, ...],
) -> None:
    """Rebuild a normalised module tier from raw over [start, end).

    Module tables are normalised — one row per module per timestamp — so the
    aggregation groups by module as well as by time: four packs in one hour
    produce four rows, and one pack's readings never average into another's.
    Module source rows are all successful ones, because a failed poll writes
    none, so no error filter applies here. As with the inverter tiers the
    delete and the reinsert share one transaction, which is what makes a
    rebuild idempotent and what drops buckets whose source rows have gone.

    ``columns`` carries bare metric names with no slot prefix, because the
    normalised table has one set of columns rather than one per slot. Every
    slot shares a template, so slot 1's registry entry supplies the aggregation
    policy for each bare name.

    ``sample_count`` records how many source rows the bucket covers, as a
    record of coverage only; every tier is built from raw, so nothing weights
    by it.
    """
    aligned_start, aligned_end = _bucket_bounds(bucket_seconds, start, end)
    part = _floor_div("timestamp", bucket_seconds)
    # Every slot shares the same template, so slot 1's spec carries the
    # aggregation for each bare column name.
    last_aliases = ", ".join(
        f"{_last_rn_expr(column, bucket_seconds, True)} AS last_{column}"
        for column in columns
        if lookup(f"battery_module1_{column}").aggregation == "last"
    )
    agg = ", ".join(
        f"{_agg_expr(column, lookup(f'battery_module1_{column}').aggregation, f'last_{column}')} "
        f"AS {column}"
        for column in columns
    )
    inner = f"SELECT {part} AS bucket, *"
    if last_aliases:
        inner += f", {last_aliases}"
    inner += f" FROM {source} WHERE timestamp >= ? AND timestamp < ?"
    select = (
        f"SELECT {part} * {bucket_seconds} AS timestamp, module_id, "
        f"COUNT(*) AS sample_count, {agg} "
        f"FROM ({inner}) GROUP BY bucket, module_id"
    )
    cols_sql = ", ".join(("timestamp", "module_id", "sample_count", *columns))
    with conn:
        cur = conn.cursor()
        cur.execute(
            f"DELETE FROM {dest} WHERE timestamp >= ? AND timestamp < ?",
            (aligned_start, aligned_end),
        )
        cur.execute(
            f"INSERT INTO {dest} ({cols_sql}) {select}",
            (aligned_start, aligned_end),
        )


def rebuild_inverter_minute(conn: sqlite3.Connection, start: int, end: int) -> None:
    """Rebuild the inverter minute tier from full cadence over [start, end).

    Aggregates ``inverter_raw`` into ``inverter_minute``, one wide row per
    minute (60 s), timestamped at the start of the minute it covers. This is
    the tier that outlives the raw rows behind it: full cadence is discarded
    at 30 days, and once it is gone a minute row is the finest detail any
    chart of that day can still be drawn from.
    """
    _rebuild_inverter(
        conn,
        "inverter_raw",
        "inverter_minute",
        60,
        start,
        end,
        tuple(spec.name for spec in INVERTER_METRICS),
    )


def rebuild_inverter_hourly(conn: sqlite3.Connection, start: int, end: int) -> None:
    """Rebuild the inverter hourly tier from raw over [start, end).

    Aggregates ``inverter_raw`` into ``inverter_hourly``, one wide row per hour
    (3600 s), timestamped at the start of the hour it covers.

    It reads raw and not the minute tier, even though the minute rows covering
    that hour are usually sitting right there. Recombining sixty minute buckets
    means weighting each one by how many readings it holds, and metrics go
    absent independently — ``AVG`` skips a NULL while ``sample_count`` counts
    the row it sat in — so a metric reported once in a two-reading minute would
    be weighted as though it had appeared twice, and all of it computed over
    values already rounded once. Reading raw lets ``AVG`` weigh every metric by
    its own readings. This tier is kept indefinitely, so a mistake made here is
    never corrected by anything downstream.
    """
    _rebuild_inverter(
        conn,
        "inverter_raw",
        "inverter_hourly",
        3600,
        start,
        end,
        tuple(spec.name for spec in INVERTER_METRICS),
    )


def rebuild_module_hourly(conn: sqlite3.Connection, start: int, end: int) -> None:
    """Rebuild the module hourly tier from module full cadence over [start, end).

    Aggregates ``module_raw`` into ``module_hourly``, one normalised row per
    module per hour (3600 s), timestamped at the start of the hour it covers.
    Grouping by module as well as by time is what keeps four packs four rows
    rather than one averaged pack — a bank is diagnosed by how its modules
    differ, and an average of them hides exactly the module that is failing.
    There is no minute tier for module data; this rebuild also reads raw, for
    the same reason the hourly inverter tier does.
    """
    _rebuild_module(
        conn,
        "module_raw",
        "module_hourly",
        3600,
        start,
        end,
        module_metric_columns(),
    )
