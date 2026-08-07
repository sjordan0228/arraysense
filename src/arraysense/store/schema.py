"""schema.py — tier definitions and DDL generation, derived from the metric registry.

The database schema is generated, never written out by hand: every metric
column comes from arraysense.metrics, so adding a metric there stays a
one-line change and this module follows. Hardcoding a column name here would
defeat that.

Two shapes are deliberate. Inverter metrics use a wide row — one row per
timestamp, one column per metric — because the main chart asks for solar,
load, battery and grid together, and a wide row costs the same as asking for
one. Per-module battery readings are normalised: one row per module per
timestamp, identified by an integer foreign key into the serials table. The
inverter exposes only four battery register slots and rotates modules through
them when more than four are present, so a slot is not a battery; slot-named
columns would let a long chart average two different physical batteries
together, and a rollup would make that permanent.

Retention tiers are described here and realised as one table per tier. The
DDL is idempotent — every statement is CREATE ... IF NOT EXISTS — so running
it twice is safe.

Timestamps are stored as INTEGER unix epochs (seconds): compact, ordered, and
unambiguous about timezone, matching the project's preference for small
integers in storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from arraysense.metrics import BATTERY_MODULE_METRICS, INVERTER_METRICS


@dataclass(frozen=True)
class Tier:
    """One resolution tier of the storage schema.

    Resolution and retention are described here and realised as one table per
    tier — "full", "minute" and "hourly". Holding that as data rather than as
    table names spelled out in each module is what lets DDL generation, store
    setup and tier selection read one list instead of keeping three copies of
    it in step by hand.

    ``keep_days`` is an age in days, and None for a tier kept indefinitely,
    which is how the coarsest tier outlives everything that feeds it.
    """

    name: str
    table: str
    keep_days: int | None


INVERTER_TIERS: tuple[Tier, ...] = (
    Tier("full", "inverter_raw", 30),
    Tier("minute", "inverter_minute", 365),
    Tier("hourly", "inverter_hourly", None),
)

MODULE_TIERS: tuple[Tier, ...] = (
    # No minute tier: state of charge, health, cycle count and cell voltages
    # move slowly, and a minute tier would cost roughly 250 MB a year to
    # record almost nothing.
    Tier("full", "module_raw", 30),
    Tier("hourly", "module_hourly", None),
)

_SERIALS_TABLE = "serials"
_INVALID_TABLE = "invalid_readings"

_MODULE_PREFIX = re.compile(r"^battery_module\d+_")

# How many source rows a rollup bucket covers. A record of coverage only:
# every tier derives from raw, so nothing weights by it.
SAMPLE_COUNT = "sample_count"

# STRICT makes the column types real constraints rather than affinities, so text
# cannot land in a numeric column and a rollup average cannot be stored as an
# unrounded float. WITHOUT ROWID stops an INTEGER PRIMARY KEY from aliasing the
# rowid, which would otherwise let a NULL timestamp be silently assigned one.
# Neither is enforced by SQLite's defaults; both are needed together.
_TABLE_OPTIONS = "STRICT, WITHOUT ROWID"

# SQLite disables foreign keys on every new connection. The module tables'
# reference into the serials table is decorative until a connection turns this
# on, and an orphaned module_id would detach readings from their battery.
FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys = ON"


def module_metric_columns() -> tuple[str, ...]:
    """Return the module metric column names, without slot numbers.

    The registry expands one template across the four battery slots
    (battery_module1_soc_pct, ...) because that is how the inverter presents
    them. Module tables store the template instead — plain ``soc_pct``, with
    the battery identified by serial — since a slot is not a battery, so the
    prefix has to come back off somewhere, and doing it here keeps the registry
    the only place the metric is named. Order follows first occurrence, which
    is template order.
    """
    seen: set[str] = set()
    cols: list[str] = []
    for spec in BATTERY_MODULE_METRICS:
        bare = _MODULE_PREFIX.sub("", spec.name)
        if bare not in seen:
            seen.add(bare)
            cols.append(bare)
    return tuple(cols)


def _serials_ddl() -> str:
    """Return the DDL for the serial-number-to-integer-id mapping table."""
    return (
        f"CREATE TABLE IF NOT EXISTS {_SERIALS_TABLE} (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    serial TEXT NOT NULL UNIQUE\n"
        ")"
    )


def _invalid_readings_ddl() -> str:
    """Return the DDL for the failed-plausibility-check table.

    Records the raw reading that failed its check, not the scaled integer —
    the check happens on the real-world value before encoding. ``serial`` is
    NULL for an inverter reading and set for a module reading.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {_INVALID_TABLE} (\n"
        "    timestamp INTEGER NOT NULL,\n"
        "    metric TEXT NOT NULL,\n"
        "    value REAL,\n"
        "    serial TEXT\n"
        ")"
    )


def _inverter_tier_ddl(tier: Tier, metric_names: tuple[str, ...]) -> str:
    """Return the DDL for one inverter-tier wide-row table.

    A wide row costs the same to ask for one metric as for all of them. The
    ``error`` column marks a failed poll and carries its reason; NULL means the
    poll succeeded.

    Every tier but the raw one is a rollup destination, so it carries
    ``SAMPLE_COUNT``: how many source rows the bucket covers. Without it a
    bucket built from three readings is indistinguishable from one built from
    three hundred once the raw tier is pruned. It is a record of coverage
    only — rollups build directly from raw, never by recombining counts, so
    nothing weights by it. Raw rows are one sample each and carry no such
    column.
    """
    cols = ["    timestamp INTEGER NOT NULL"]
    cols.extend(f"    {name} INTEGER" for name in metric_names)
    if tier.name != "full":
        cols.append(f"    {SAMPLE_COUNT} INTEGER NOT NULL")
    cols.append("    error TEXT")
    cols.append("    PRIMARY KEY (timestamp)")
    return (
        f"CREATE TABLE IF NOT EXISTS {tier.table} (\n" + ",\n".join(cols) + f"\n) {_TABLE_OPTIONS}"
    )


def _module_tier_ddl(tier: Tier, metric_names: tuple[str, ...]) -> str:
    """Return the DDL for one module-tier normalised table.

    One row per module per timestamp: the row references an integer id in the
    serials table, never a slot number. Rollup tiers (everything but raw)
    carry ``SAMPLE_COUNT`` for the same reason as the inverter tiers: a bucket
    must record how many source rows it covers. It is a record of coverage
    only — rollups build directly from raw, never by recombining counts, so
    nothing weights by it.
    """
    cols = ["    timestamp INTEGER NOT NULL"]
    cols.append(f"    module_id INTEGER NOT NULL REFERENCES {_SERIALS_TABLE}(id)")
    cols.extend(f"    {name} INTEGER" for name in metric_names)
    if tier.name != "full":
        cols.append(f"    {SAMPLE_COUNT} INTEGER NOT NULL")
    cols.append("    PRIMARY KEY (timestamp, module_id)")
    return (
        f"CREATE TABLE IF NOT EXISTS {tier.table} (\n" + ",\n".join(cols) + f"\n) {_TABLE_OPTIONS}"
    )


def _index_ddl(table: str, columns: str) -> str:
    """Return an idempotent index statement for ``table`` on ``columns``.

    The index name is derived from the table and the columns, with anything
    that is not an identifier character collapsed to an underscore — a comma
    between indexed columns must not leak into the name.
    """
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", columns)
    name = f"idx_{table}_{safe}"
    return f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"


def expected_columns() -> dict[str, tuple[str, ...]]:
    """Return the columns each tier table should have, keyed by table name.

    This is what a live database gets measured against, so one created before a
    metric was added to the registry — or before a rollup tier gained
    ``SAMPLE_COUNT`` — is detected on open and repaired, rather than accepted
    and then failing on the first write. Metric columns come out in registry
    order, with ``SAMPLE_COUNT`` appended for every tier but ``full``.
    """
    inverter = tuple(spec.name for spec in INVERTER_METRICS)
    module = module_metric_columns()
    tables: dict[str, tuple[str, ...]] = {}
    for tier in INVERTER_TIERS:
        cols = inverter
        if tier.name != "full":
            cols = (*cols, SAMPLE_COUNT)
        tables[tier.table] = cols
    for tier in MODULE_TIERS:
        cols = module
        if tier.name != "full":
            cols = (*cols, SAMPLE_COUNT)
        tables[tier.table] = cols
    return tables


def migration_ddl(existing: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Return ALTER statements adding registry columns an old database lacks.

    ``CREATE TABLE IF NOT EXISTS`` is idempotent only while the schema is
    unchanged: against a database made before a metric was added it does
    nothing, and the first write then fails with "no such column". Since adding
    a metric to the registry is meant to be a one-line change, the gap has to be
    closed on open; a database already current yields nothing to run.

    Only additions are handled. A removed or retyped metric needs a considered
    migration, not an automatic one, so those are left alone. ``SAMPLE_COUNT``
    is NOT NULL in the DDL, and SQLite rejects a NOT NULL column added to a
    non-empty table unless it carries a default; existing rows take 0, which a
    later rollup overwrites.

    Args:
        existing: table name to the columns that table currently has, as read
            from the live database.
    """
    statements: list[str] = []
    for table, wanted in expected_columns().items():
        have = set(existing.get(table, ()))
        for name in wanted:
            if name in have:
                continue
            if name == SAMPLE_COUNT:
                statements.append(
                    f"ALTER TABLE {table} ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
            else:
                statements.append(f"ALTER TABLE {table} ADD COLUMN {name} INTEGER")
    return tuple(statements)


def schema_ddl() -> str:
    """Return the complete storage schema as executable SQL text.

    Builds every table from the metric registry and the tier definitions, so a
    change in either flows through here untouched — which is the point, since
    the alternative is a hand-written schema that drifts away from the registry
    silently. The text is idempotent, every statement being
    CREATE ... IF NOT EXISTS, so it can be run on every startup rather than
    guarded by a "have we set up yet" flag that can be wrong. Each tier table
    carries an index on its timestamp so the time-series range queries the
    charts issue stay on an index.
    """
    inverter_columns = tuple(spec.name for spec in INVERTER_METRICS)
    module_columns = module_metric_columns()

    statements = [_serials_ddl(), _invalid_readings_ddl()]
    # Appending clears a timestamp's stale flags before rewriting them. Without
    # this index that delete scans the whole table, so a sustained decoder
    # fault — precisely what this table is here to preserve — would make every
    # later write slower while holding the single writer lock.
    statements.append(_index_ddl(_INVALID_TABLE, "timestamp, serial"))
    # Inverter tables need no separate timestamp index: the primary key on
    # timestamp already provides one, and a duplicate index would cost a write
    # on every sample for nothing.
    for tier in INVERTER_TIERS:
        statements.append(_inverter_tier_ddl(tier, inverter_columns))
    # Module tables key on (timestamp, module_id), which serves time-range
    # queries. The extra index reverses that order to serve "one module over
    # time", which is the per-module history view.
    for tier in MODULE_TIERS:
        statements.append(_module_tier_ddl(tier, module_columns))
        statements.append(_index_ddl(tier.table, "module_id, timestamp"))
    # Each statement ends on its own line; a trailing semicolon per statement
    # makes the whole text executable as a single script.
    return ";\n".join(statements) + ";\n"
