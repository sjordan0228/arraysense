"""rollup.py — collapse full-cadence readings into the coarse tiers, from raw only.

``Tier.keep_days`` declares 30 days for the full-cadence tier and a year for
the minute tier, and nothing enforces either today — no tier is pruned, and the
database grows about 5 MB a day. That is tracked in issue #135. What is true
regardless is the asymmetry this file turns on: the coarse tiers are the ones
kept longest, hourly indefinitely, so a rollup mistake outlives the raw rows it
was derived from in a way a mistake in the raw tier does not.

These functions rebuild a destination tier from the raw tier over a time range,
reading each metric's collapse policy from
``collapse_policy`` rather than inferring it from a name: most measurements
average, a bitfield takes its maximum, an extreme stays an extreme, and a cell
*number* field takes the latest value because the average of two cell numbers
is a cell that does not exist. ``collapse_policy`` is also what the
SolarAssistant importer asks, so the two writers of these tables cannot end up
putting different things in the same column — with one gap, recorded here
rather than left to be discovered: the importer holds no per-reading timestamp
to rank by, so where these rebuilds give an energy counter's bucket its
earliest reading the importer gives it the smallest. The two agree unless a
counter goes backwards inside a bucket.

Every tier derives from raw; no tier derives from another. Recombining minute
buckets into hourly would need each bucket weighted by how many readings it
covers, but metrics can be absent independently — ``AVG`` skips NULLs while
``sample_count`` counts every row — so a metric present in one row of a
two-row bucket would be weighted as though it appeared twice, and weighting
would multiply already-rounded minute values. Building from raw lets ``AVG``
ignore absent readings per metric, correctly and without weighting.

Every rebuild groups by device as well as by bucket. Two inverters averaged
into one row is worse than no rollup at all: the coarse tiers outlive the raw
rows behind them, so nothing downstream could ever tell that the hour it is
drawing is the mean of two machines, and no later pass would undo it. The same
applies one level down to modules, which group by device, bucket and pack.

Two rules keep a rebuild correct and repeatable.

- A bucket's timestamp is the start of the period it covers, derived
  arithmetically as ``epoch // period * period`` so two writers agree on the
  boundary without coordinating. SQLite's ``/`` truncates toward zero, so the
  floor is computed arithmetically; a negative epoch floors downward. An
  hourly row therefore falls exactly on an hour boundary (UTC), never on a
  rounded local time. The timestamp is a claim about the row's contents as
  well as its position: a row stamped 13:00 describes 13:00, which is why the
  energy counters collapse to their opening value rather than their closing
  one — see ``collapse_policy`` for the rule and ``_first_expr`` for how the
  opening reading is found cheaply.
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

import logging
import sqlite3
from collections.abc import Sequence

from arraysense.metrics import SITE_METRICS, Aggregation, MetricSpec, lookup
from arraysense.store.schema import (
    PENDING_TABLE,
    inverter_metric_columns,
    module_metric_columns,
)

logger = logging.getLogger(__name__)

# A failed poll is stored with its reason and no readings (see models.Sample).
# Excluding it from the aggregate stops it being counted as a reading of zero.
#
# Weather rows pass this filter — they carry no error — so sample_count counts
# them alongside inverter readings: a minute holding five polls and one weather
# tick records six. Known and accepted: the values themselves aggregate
# correctly (AVG/MAX/MIN skip the nulls, and the energy openers filter to real
# counter values), the skew is one row in ~82 at the fifteen-minute cadence,
# and a per-metric count would cost a column per metric to be exact.
_ERROR_FILTER = "error IS NULL"


def _columns_present(
    conn: sqlite3.Connection, source: str, dest: str, candidates: tuple[str, ...]
) -> tuple[str, ...]:
    """Return the candidate metric columns present in both source and destination.

    The registry says which columns *could* exist; the tables say which ones
    this installation's driver declared when they were created. A database laid
    down for a driver that declares a subset has columns for that subset alone,
    so a rebuild naming the whole registry would die on its first pass there —
    while an older database carries every registry column and must keep rolling
    all of them up. Reading the columns off the tables serves both without
    either being configured, and the intersection guards the one mismatch a
    half-run migration could leave: a column that exists to read but not to
    write is skipped rather than fatal.
    """
    src = {row[1] for row in conn.execute(f"PRAGMA table_info({source})")}
    dst = {row[1] for row in conn.execute(f"PRAGMA table_info({dest})")}
    return tuple(name for name in candidates if name in src and name in dst)


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


def is_energy_counter(spec: MetricSpec) -> bool:
    """Whether this metric is a counter, whose bucket carries its opening reading.

    ``kWh`` is the test because it is a declared registry field and it selects
    exactly the energy counters: all twenty-four kWh metrics are counters and no
    energy counter is anything else.

    The other ``max`` metrics are deliberately not swept along. For the fault
    bitfields and the force-charge flag the extreme is the whole point, since a
    fault raised for ten seconds has to survive into the coarse tiers. Cycle
    count and run time climb the way the counters do, but nothing differences
    them across a bucket edge; both are drawn as a level, so the reading a
    bucket ends on is the useful one.
    """
    return spec.unit == "kWh" and spec.aggregation == "max"


def pack_scale(spec: MetricSpec) -> int:
    """Return the multiplier that lifts a row's position clear of its reading.

    A bucket's opening reading is found by ranking one packed integer per row —
    the row's offset into its bucket above, the reading below — so the
    multiplier has to exceed every reading the metric can hold. The registry's
    own upper bound answers that, rounded up to the next power of two, which is
    an exact way to clear it without picking a number by eye: a lifetime counter
    tops out at 10,000,000 stored tenths against a multiplier of 16,777,216, and
    a daily one at 20,000 against 32,768. Deriving it rather than fixing it is
    what makes a widened bound a one-line change in the registry instead of a
    quietly truncated column here.
    """
    return 1 << spec.encode(spec.upper).bit_length()


def collapse_policy(spec: MetricSpec) -> Aggregation:
    """Return how one metric collapses into a bucket, for every writer of a tier.

    The registry says what a metric *is*; this says what a bucket of them
    means, and it is deliberately the only place that second question is
    answered. The importer fills the same tiers from a different source, and
    while it carried its own copy the two put different numbers in the same
    column of the same table depending on which had written the row.

    Every energy counter collapses to its opening reading rather than to the
    largest, which is what its registry entry declares. A bucket's timestamp is
    the start of the period it covers, so the row labelled 13:00 has to describe
    the counter at 13:00; taking the maximum puts the value from 14:00 there and
    shifts every reading one bucket later than its label.

    ``Aggregation`` has no name for "the value at the bucket's start", so this
    returns ``min``, which is what a source holding no per-reading timestamps
    can offer: a counter that only climbs opens on its smallest reading. Where
    the timestamps *are* present — every rollup here — ``_first_expr`` finds the
    opening reading properly, by position rather than by size, because the two
    part company exactly when a counter goes backwards. A fifth policy in the
    registry would be the tidier home for both.
    """
    return "min" if is_energy_counter(spec) else spec.aggregation


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


def bucket_sql(column: str, seconds: int) -> str:
    """Return SQL placing an epoch column at the start of its UTC bucket.

    Retention has to prove that a coarse row exists for every source bucket
    before raw data is removed. Keeping its boundary expression here means it
    cannot quietly disagree with the rebuilds below zero, where SQLite's
    ordinary integer division truncates toward zero rather than flooring.
    """
    return f"({_floor_div(column, seconds)} * {seconds})"


def _agg_expr(column: str, aggregation: str, last_rn: str) -> str:
    """Return the SQL aggregate producing one metric's rolled-up value.

    The policy comes from ``collapse_policy`` and is dispatched here, never
    inferred from the column name, so a metric that collapses unusually cannot
    be given the wrong aggregate by being named like an ordinary measurement.

    ``mean`` rounds to the nearest integer — the STRICT tables reject a real.
    SQLite's ``AVG`` returns a real even from integer inputs, so no integer
    division truncates before rounding. ``max`` and ``min`` keep the extreme,
    and nothing else is folded into them: the energy counters do not come
    through here at all, since the reading their bucket wants is the earliest
    rather than the smallest and ``_first_expr`` is what finds it.

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


def _first_expr(column: str, spec: MetricSpec, bucket_seconds: int) -> str:
    """Return the SQL producing the bucket's earliest reported value of ``column``.

    This is what an energy counter collapses to, and it is deliberately not
    ``MIN``. The two agree while a counter climbs and part company the moment it
    does not: a lifetime total that opens at 1000.0 kWh, misdecodes to 900.0
    mid-hour and recovers to 1000.5 has an opening reading of 1000.0 and a
    smallest reading of 900.0, and the hourly tier is kept for ever, so the
    glitch would sit there as a hole in that hour and a spike in the next for
    the life of the database. ``MAX`` was immune to a low glitch and exposed to
    a high one; ranking by position is immune to both.

    A counter that genuinely restarts is a third case, and it is handled by the
    same rule rather than by a special case for it. The daily counters restart
    at the owner's midnight, which lands inside an hourly bucket in any zone
    offset by a half hour, and a lifetime counter restarts on a firmware update
    or a replaced BMS. Either way the row is stamped at the start of its bucket,
    so it holds what the counter read then, which is the pre-reset value; the
    restart then appears in the step to the next bucket, where it happened, and
    anything differencing the counter sees it as the reset it was rather than a
    bucket early. Reading the smallest value instead moves it a bucket back and
    charges the interval twice.

    The rank is found without a window function, because ranking each counter
    with one is what made the first attempt at this correction six times slower
    than the pass it replaced. Instead each row is packed into a single integer
    — its offset into its own bucket multiplied by ``pack_scale``, plus the
    reading — so an ordinary ``MIN`` orders by time first and returns the
    reading in its low bits, which ``%`` recovers. That costs a few per cent
    over the bare ``MIN`` it replaces rather than the five-fold the ranked
    version does.

    The ``FILTER`` is what makes that arithmetic safe rather than assumed. An
    implausible reading is *stored* and flagged in ``invalid_readings`` rather
    than rejected, so a misdecoded register really can put a negative or an
    enormous number in a counter column, and either one packs into a value that
    unpacks as something else entirely — a decode error in one row would come
    back as a fabricated reading in the tier that is kept for ever. Restricting
    the candidates to values the encoding can carry means such a row cannot be
    the bucket's opening reading; if it is the only reading there, the bucket
    rolls up NULL, which is the truthful answer and the one ``invalid_readings``
    already has the evidence for. The bound here protects the arithmetic and
    says nothing about plausibility, which is ``validate``'s question.

    The offset comes from ``bucket``, the floored division the grouping already
    computed, so it lies in ``[0, bucket_seconds)`` below zero as well as above
    it and needs no second opinion about where a boundary is. A NULL reading
    packs to NULL and ``MIN`` skips it, so the bucket opens on the earliest row
    that *reported* the counter rather than on the earliest row — the same
    distinction the ``last`` policy rests on, at the other end of the bucket.
    """
    scale = pack_scale(spec)
    offset = f"(timestamp - bucket * {bucket_seconds})"
    carries = f"FILTER (WHERE {column} BETWEEN 0 AND {scale - 1})"
    return f"MIN({offset} * {scale} + {column}) {carries} % {scale}"


def _bucket_expr(column: str, spec: MetricSpec, bucket_seconds: int) -> str:
    """Return the SQL collapsing one metric into its bucket, whatever its policy.

    One door for both tiers, so an energy counter cannot be routed through the
    plain aggregates by one rebuild and through the opening-value expression by
    the other. Everything but a counter is dispatched on ``collapse_policy``.
    """
    if is_energy_counter(spec):
        return _first_expr(column, spec, bucket_seconds)
    return _agg_expr(column, collapse_policy(spec), f"last_{column}")


def _module_spec(column: str) -> MetricSpec:
    """Return the registry entry answering for a bare per-module column name.

    The normalised module tables hold one set of columns rather than one per
    slot, so the bare names they carry are not registry names. Every slot
    shares a template, which makes slot 1's entry the one that answers for all
    of them.
    """
    return lookup(f"battery_module1_{column}")


def _module_policy(column: str) -> Aggregation:
    """Return the collapse policy for a bare per-module column name."""
    return collapse_policy(_module_spec(column))


def _last_rn_expr(column: str, bucket_seconds: int, module: bool) -> str:
    """Return a SQL row number marking the latest row that reported ``column``.

    Rows are ordered by timestamp descending with rows missing the metric
    (sort key NULL) sorted last, so ``rn = 1`` is the latest source row that
    carried a value rather than merely the latest row. The "last" collapse
    policy rests on that distinction: it keeps a value an earlier row reported
    even when the closing row omits it.

    The partition always carries ``device``: a metric one inverter stopped
    reporting must not take its rolled-up value from another inverter that
    kept reporting it. Module tiers are normalised, so ``module`` adds
    ``module_id`` too; without it one pack's reading would decide another
    pack's rolled-up value. The expression comes back unaliased because the
    alias has to match the one handed to ``_agg_expr``, and only the caller
    knows it. Only the ``last`` policy needs this; the energy counters find
    their opening reading with a plain aggregate instead, which is what keeps
    the pass one scan (see ``_first_expr``).
    """
    part = f"{_floor_div('timestamp', bucket_seconds)}, device"
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
    on the way to ``_bucket_expr``, so a bare module metric handed to this
    function raises rather than rolling up under the wrong policy.

    Deletes the destination buckets and reinserts them within one transaction,
    so a rebuild is idempotent and a bucket whose source rows have gone is
    dropped rather than left behind as a stale average nothing supports. Each
    bucket's timestamp is the start of the period it covers, derived by floor
    division.

    Failed-poll rows are excluded from the source (see ``_ERROR_FILTER``), so
    an hour in which every poll failed produces no row at all. That gap is the
    honest answer: a bucket averaging an outage into the readings around it
    would draw a chart that never lost contact with the inverter.

    Every device in the range is rebuilt, not one: the delete clears the whole
    range and the insert refills it for whichever devices have source rows,
    which is what keeps the pass a single scan however many inverters are
    stacked. A per-device pass would read the raw tier once per unit.

    ``sample_count`` records how many successful source rows the bucket
    covers, and is a record of coverage only — every tier is built from raw, so
    nothing downstream ever weights by it.

    An empty ``columns`` skips the rebuild entirely: a device that declares no
    metric at this level has nothing to roll up, and the SQL for zero metric
    columns is not SQL at all — the empty select list left ``sample_count,
    FROM``, a syntax error that every sixty-second maintenance pass re-raised
    for the life of the service.
    """
    if not columns:
        return
    aligned_start, aligned_end = _bucket_bounds(bucket_seconds, start, end)
    part = _floor_div("timestamp", bucket_seconds)
    last_aliases = ", ".join(
        f"{_last_rn_expr(column, bucket_seconds, False)} AS last_{column}"
        for column in columns
        if collapse_policy(lookup(column)) == "last"
    )
    agg = ", ".join(
        f"{_bucket_expr(column, lookup(column), bucket_seconds)} AS {column}" for column in columns
    )
    inner = f"SELECT {part} AS bucket, *"
    if last_aliases:
        inner += f", {last_aliases}"
    inner += f" FROM {source} WHERE timestamp >= ? AND timestamp < ? AND {_ERROR_FILTER}"
    select = (
        f"SELECT {part} * {bucket_seconds} AS timestamp, device, "
        f"COUNT(*) AS sample_count, {agg} "
        f"FROM ({inner}) GROUP BY bucket, device"
    )
    cols_sql = ", ".join(("timestamp", "device", "sample_count", *columns))
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
    aggregation groups by module and by device as well as by time: four packs
    in one hour produce four rows, and one pack's readings never average into
    another's. Grouping by device is belt as well as braces here, since a
    serials id already belongs to one inverter, but the destination key holds
    the device and a rollup that did not select it could not fill it.
    Module source rows are all successful ones, because a failed poll writes
    none, so no error filter applies here. As with the inverter tiers the
    delete and the reinsert share one transaction, which is what makes a
    rebuild idempotent and what drops buckets whose source rows have gone.

    ``columns`` carries bare metric names with no slot prefix, because the
    normalised table has one set of columns rather than one per slot;
    ``_module_policy`` is what turns one back into a registry name.

    ``sample_count`` records how many source rows the bucket covers, as a
    record of coverage only; every tier is built from raw, so nothing weights
    by it.

    An empty ``columns`` skips the rebuild entirely, exactly as the inverter
    rebuild does: a device reporting only a bank-level summary declares no
    per-module template, has nothing to roll up here, and the SQL for zero
    metric columns is a syntax error rather than a no-op.
    """
    if not columns:
        return
    aligned_start, aligned_end = _bucket_bounds(bucket_seconds, start, end)
    part = _floor_div("timestamp", bucket_seconds)
    last_aliases = ", ".join(
        f"{_last_rn_expr(column, bucket_seconds, True)} AS last_{column}"
        for column in columns
        if _module_policy(column) == "last"
    )
    agg = ", ".join(
        f"{_bucket_expr(column, _module_spec(column), bucket_seconds)} AS {column}"
        for column in columns
    )
    inner = f"SELECT {part} AS bucket, *"
    if last_aliases:
        inner += f", {last_aliases}"
    inner += f" FROM {source} WHERE timestamp >= ? AND timestamp < ?"
    select = (
        f"SELECT {part} * {bucket_seconds} AS timestamp, device, module_id, "
        f"COUNT(*) AS sample_count, {agg} "
        f"FROM ({inner}) GROUP BY bucket, device, module_id"
    )
    cols_sql = ", ".join(("timestamp", "device", "module_id", "sample_count", *columns))
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
        _columns_present(conn, "inverter_raw", "inverter_minute", inverter_metric_columns()),
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
        _columns_present(conn, "inverter_raw", "inverter_hourly", inverter_metric_columns()),
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
        _columns_present(conn, "module_raw", "module_hourly", module_metric_columns()),
    )


# How old a raw row has to be for its hour to need promoting by hand. Anything
# newer is already covered: maintenance rebuilds the last three hours from raw
# on every pass. Two hours keeps this strictly inside that window — a test pins
# the relationship — so a row written just outside the live path's reach is
# still caught by the rebuild if the queue is drained late.
LATE_APPEND_SECONDS = 2 * 3600

# How many queued hours one pass promotes. A two-year archive backfill queues
# eighteen thousand of them, and the queue drains a batch a minute rather than
# holding the write lock for the whole of it in one go. Also keeps the hour
# list inside SQLite's bound-parameter limit by a wide margin.
PROMOTE_BATCH = 500


def rebuild_circuit_hourly(
    conn: sqlite3.Connection, start: int, end: int, *, cadence_seconds: int
) -> None:
    """Aggregate ``circuit_reading`` into ``circuit_hourly`` over [start, end).

    Simpler than the tiers above because there is nothing to consult the metric
    registry about: a circuit reading is one number, and the only sensible
    collapse of power over an hour is its mean.

    ``sample_count`` counts readings rather than rows, which is the one place
    this parts company with the module tier. There a failed poll writes no row
    at all, so rows and readings are the same thing; here a circuit that was
    listed but did not answer writes a row with a NULL in it, and an hour built
    from two readings must not claim the coverage of one built from sixty. The
    stage that prices a circuit depends on telling those apart — coverage in
    minutes watched is not coverage in energy accounted for.

    ``covered_seconds`` is that coverage measured rather than inferred, and it
    is why ``cadence_seconds`` has no default. This runs on the hourly clock
    over readings minutes old, so the poll interval it is handed genuinely
    describes them; a reader arriving a month later has only the interval in
    force *then*, which is a different number the moment the owner touches the
    setting. Passing today's interval to yesterday's rows is what doubled the
    energy of every stored hour when the bench interval was raised from ten
    seconds to sixty. The caller knows the interval; an hourly row cannot be
    asked, so it is written down here.

    Each reading accounts for the distance to the next one inside its own hour,
    capped at one poll interval, and the last reading of the hour accounts for
    one interval. Capped because a reading covers the period it was taken for
    and no more — the stretch after a poller stopped belongs to nobody — and
    measured from the real stamps rather than multiplying the count, so a
    retried poll landing five seconds after its predecessor adds five seconds
    and not a whole interval. The total is clamped to the 3,600 seconds an hour
    holds. An hour with no readings stores 0, never NULL: NULL is reserved for a
    row written before the column existed, and the two must stay
    distinguishable.

    An hour in which nothing was heard averages to NULL rather than to zero, and
    lands with a sample count of nought. Deleting and reinserting inside one
    transaction is what makes the rebuild idempotent, and what drops a bucket
    whose source rows have since gone.

    The multiplier is deliberately not applied. Readings are stored raw and
    scaled on the way out, so a clamp corrected upstream fixes the whole history
    rather than only what was written after the correction.
    """
    aligned_start, aligned_end = _bucket_bounds(3600, start, end)
    part = _floor_div("timestamp", 3600)
    # The window function partitions by the bucket as well as the circuit, so
    # each hour is measured from its own readings alone and a rebuild gives the
    # same answer whatever range it was asked for. A reading with nothing after
    # it in its hour takes the full interval.
    spans = (
        f"SELECT {part} * 3600 AS bucket, circuit_id, watts, "
        "MIN(COALESCE("
        f"  LEAD(timestamp) OVER (PARTITION BY circuit_id, {part} ORDER BY timestamp) - timestamp,"
        "  ?"
        "), ?) AS covered "
        "FROM circuit_reading WHERE timestamp >= ? AND timestamp < ?"
    )
    select = (
        "SELECT bucket AS timestamp, circuit_id, "
        "CAST(ROUND(AVG(watts)) AS INTEGER) AS watts, "
        "COUNT(watts) AS sample_count, "
        # COALESCE, not a bare SUM: an hour holding only unanswered readings
        # sums to NULL, and NULL here would be indistinguishable from a row
        # written before this column existed. The outer MIN is the hour itself —
        # at a long interval the last reading's own period can run past the end
        # of the hour it was taken in, and an hour holds 3,600 seconds however
        # the polls fell inside it.
        "MIN(COALESCE(SUM(CASE WHEN watts IS NULL THEN NULL ELSE covered END), 0), 3600) "
        "AS covered_seconds "
        f"FROM ({spans}) GROUP BY 1, circuit_id"
    )
    with conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM circuit_hourly WHERE timestamp >= ? AND timestamp < ?",
            (aligned_start, aligned_end),
        )
        cur.execute(
            "INSERT INTO circuit_hourly"
            " (timestamp, circuit_id, watts, sample_count, covered_seconds) " + select,
            (cadence_seconds, cadence_seconds, aligned_start, aligned_end),
        )


def merge_site_hours(conn: sqlite3.Connection, hours: Sequence[int]) -> int:
    """Fold recorded sky readings for whole past hours into the hourly tier.

    Deliberately not a rebuild, and the difference is the point. A rebuild
    deletes the destination buckets and refills them from raw, which is right
    while raw still holds the hour and catastrophic once it does not: raw keeps
    thirty days, the hourly tier is kept for ever, and rebuilding an hour from
    two years ago would delete that hour's inverter history to write one
    temperature into it. This writes only the site columns, and only where the
    raw rows have something to say — a site column raw cannot answer for is
    left exactly as it stands, because absence in a thirty-day tier is no
    evidence at all about an hour outside it.

    What it is for: the archive backfill appends one sample per past hour into
    the raw tier, and the efficiency engine reads irradiance from the hourly
    tier alone. Without this every backfilled hour older than the maintenance
    window is invisible to the feature the backfill exists to feed, while the
    route reports the hours as written.

    Returns how many hourly rows were touched, for the log line that says a
    backfill has landed.
    """
    if not hours:
        return 0
    columns = _columns_present(conn, "inverter_raw", "inverter_hourly", tuple(sorted(SITE_METRICS)))
    if not columns:
        return 0
    buckets = sorted({hour // 3600 * 3600 for hour in hours})
    part = _floor_div("timestamp", 3600)
    agg = ", ".join(
        f"{_bucket_expr(column, lookup(column), 3600)} AS {column}" for column in columns
    )
    # Only rows carrying at least one site reading are counted, so an hour that
    # holds nothing but inverter polls produces no row here and its stored
    # readings are never disturbed.
    carries_site = " OR ".join(f"{column} IS NOT NULL" for column in columns)
    placeholders = ", ".join("?" for _ in buckets)
    select = (
        f"SELECT {part} * 3600 AS timestamp, device, COUNT(*) AS sample_count, {agg} "
        f"FROM inverter_raw WHERE {part} * 3600 IN ({placeholders}) "
        f"AND {_ERROR_FILTER} AND ({carries_site}) "
        # Grouped by the bucket expression, never by the output name: SQLite
        # resolves "timestamp" to the source column, which puts every raw row
        # in a group of its own and leaves the hour holding whichever of them
        # the upsert wrote last instead of its mean.
        f"GROUP BY {part}, device"
    )
    cols_sql = ", ".join(("timestamp", "device", "sample_count", *columns))
    # COALESCE, not a plain assignment: the aggregate is NULL for a metric no
    # raw row in the hour reported, and the archive answers one hour in two
    # pieces — the means over the hour and the readings at its label — so the
    # first piece must not be erased when the second arrives.
    updates = ", ".join(
        f"{column} = COALESCE(excluded.{column}, inverter_hourly.{column})" for column in columns
    )
    with conn:
        cur = conn.execute(
            f"INSERT INTO inverter_hourly ({cols_sql}) {select} "
            f"ON CONFLICT (timestamp, device) DO UPDATE SET {updates}",
            buckets,
        )
    return cur.rowcount


def promote_pending_hours(conn: sqlite3.Connection, limit: int = PROMOTE_BATCH) -> int:
    """Promote a batch of queued past hours into the hourly tier; return how many.

    The queue is written by the store as it appends a site reading stamped
    outside the maintenance window — see ``SqliteStore.append`` — so this knows
    exactly which buckets to bring forward instead of scanning a month of raw
    to find out.

    Rows are claimed by rowid and deleted by rowid, so an append landing
    between the read and the delete queues a new row and is promoted on the
    next pass rather than being cleared unpromoted. Duplicate hours are
    harmless: the merge collapses them.
    """
    claimed = conn.execute(
        f"SELECT rowid, hour FROM {PENDING_TABLE} ORDER BY hour LIMIT ?", (limit,)
    ).fetchall()
    if not claimed:
        return 0
    merged = merge_site_hours(conn, [int(row[1]) for row in claimed])
    ids = [int(row[0]) for row in claimed]
    with conn:
        conn.execute(
            f"DELETE FROM {PENDING_TABLE} WHERE rowid IN ({', '.join('?' for _ in ids)})",
            ids,
        )
    logger.info("promoted %d backfilled hour(s) into the hourly tier", merged)
    return merged
