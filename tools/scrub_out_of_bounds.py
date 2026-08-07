"""scrub_out_of_bounds.py — report, and optionally clear, readings the registry calls impossible.

The store keeps an implausible reading rather than dropping it. The value is
written and flagged in ``invalid_readings``, because a decode fault is evidence
about the inverter or about our own scaling, and evidence thrown away cannot be
diagnosed six months later. Keeping it means something has to be able to clear
it up afterwards, once the fault is understood, and this is that something.

It is not redundant with the check the store already makes. That check runs on
the real-world value handed in, before encoding, so a fault in the *encode* step
walks straight past it. Five rows written on 2026-08-07 by a build whose
registry scaled volts by 1000 where it now scales by 10 stored an entirely
plausible 245.0 V grid reading as 245000, which decodes as 24,500 V. Nothing was
flagged, because nothing implausible was ever handed in — and the same is true
of any future scale correction, which is a class of fault the bounds guard on
the way in structurally cannot catch.

Three rules make it safe to run against a live database while the collector is
writing to it, and none of them is optional:

- Every candidate is decoded and re-checked in Python before it is touched, so
  the integer arithmetic that narrows the search can only ever offer too much,
  never too little. A value inside its bounds is never altered.
- Each update names the exact integer it expects to replace, so a row the
  collector rewrote in the meantime is left alone rather than blanked.
- Clearing is idempotent. A second run finds nothing and changes nothing, which
  is what lets this be scheduled rather than performed.

A cleared reading becomes NULL, never zero — an absent reading and a reading of
zero are different facts, and this project exists because the product it
replaces confused them.

The ``invalid_readings`` flags are deliberately left alone. Clearing removes the
number; the flag is the record that it was ever taken. Erasing both would leave
no trace that anything happened at all.
"""

from __future__ import annotations

import argparse
import logging
import math
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from arraysense.metrics import INVERTER_METRICS, MetricSpec, lookup
from arraysense.store.schema import INVERTER_TIERS, MODULE_TIERS, module_metric_columns

logger = logging.getLogger("scrub")


@dataclass(frozen=True)
class Column:
    """One metric column of one tier table, with the spec its values answer to.

    Pairing the two here is what lets the rest of this module stay ignorant of
    how the tiers are laid out. Module tables store the bare template name and
    identify the battery by serial, so a slot has to be borrowed to reach the
    registry; every slot shares one template, so slot 1 speaks for all four.
    """

    table: str
    column: str
    spec: MetricSpec
    keyed_by_module: bool


@dataclass(frozen=True)
class Offender:
    """One stored reading whose decoded value falls outside its metric's bounds.

    Holds the stored integer rather than only the decoded float, because the
    update that clears it names that integer to prove the row has not changed
    underneath us.
    """

    column: Column
    timestamp: int
    module_id: int | None
    stored: int

    @property
    def decoded(self) -> float:
        """The real-world value the stored integer decodes to, in the metric's unit."""
        return self.column.spec.decode(self.stored)


def stored_bounds(spec: MetricSpec) -> tuple[int, int]:
    """Return the range of stored integers used to narrow the search in SQL.

    This is a filter, not the verdict. Anything it lets through is decoded and
    put to ``within_bounds`` afterwards, so the only property required of it is
    that it never excludes an offender; offering a reading that turns out to be
    fine costs one comparison.

    The product is taken as an exact rational rather than in floating point so
    the threshold is derived from the declared bound itself and not from a
    rounded copy of it. On today's registry the two agree on every one of the
    175 specs — this was checked, not assumed — so the exact form is a guard on
    the arithmetic rather than a fix for a present fault. It errs the safe way:
    where the two differ, as they do for a bound of 0.3 at a scale of 10, the
    exact form is the tighter of the pair and produces a spare candidate that
    the re-check then keeps.
    """
    return (
        math.ceil(Fraction(spec.lower) * spec.scale),
        math.floor(Fraction(spec.upper) * spec.scale),
    )


def columns_to_check() -> tuple[Column, ...]:
    """Return every metric column of every tier, paired with its registry spec.

    Derived from the tier definitions and the registry, never listed out, so a
    metric added to ``arraysense.metrics`` is scrubbed from the day it exists
    and a tier added to the schema is covered without an edit here. A list
    written by hand is a list that silently stops covering the newest column,
    which is exactly the column a fresh scale mistake lands in.
    """
    columns: list[Column] = []
    for tier in INVERTER_TIERS:
        columns.extend(
            Column(tier.table, spec.name, spec, keyed_by_module=False) for spec in INVERTER_METRICS
        )
    for tier in MODULE_TIERS:
        columns.extend(
            Column(tier.table, name, lookup(f"battery_module1_{name}"), keyed_by_module=True)
            for name in module_metric_columns()
        )
    return tuple(columns)


def _existing_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    """Return the columns ``table`` actually has, empty if there is no such table.

    A database made before a metric was added to the registry is missing that
    column until the store next opens it and runs the migration. Asking the
    database what it has, rather than assuming it matches the registry, is what
    stops this tool failing outright on one — it should scrub what is there and
    say nothing about what is not.
    """
    return frozenset(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def scan_table(conn: sqlite3.Connection, table: str, columns: Sequence[Column]) -> list[Offender]:
    """Return every out-of-bounds reading in one table, found in a single pass.

    One query per table rather than one per metric. The minute tier holds half a
    million rows across ninety-one columns, and asking each column its own
    question is ninety-one scans of it; one OR chain over every column's stored
    bounds reads the table once.

    NULL is excluded for free, because a comparison against NULL is NULL and
    never true. That is load-bearing rather than incidental: an unreported
    reading must not be counted as an offender, and a predicate written to
    "handle" NULL by coalescing it to zero would report every absent reading in
    the database as a fault.

    Each candidate is decoded and re-checked against the spec before it is
    returned, so the integer narrowing above is only ever an optimisation. If it
    is too generous the row is dropped here; it can never be too strict, because
    the bounds it was built from are the same ones being re-checked.
    """
    present = _existing_columns(conn, table)
    checked = [c for c in columns if c.column in present]
    if not checked:
        return []

    keyed_by_module = checked[0].keyed_by_module
    key_columns = ["timestamp", "module_id"] if keyed_by_module else ["timestamp"]
    selected = [*key_columns, *(c.column for c in checked)]
    predicate = " OR ".join(f"({c.column} < ? OR {c.column} > ?)" for c in checked)
    params: list[int] = []
    for column in checked:
        params.extend(stored_bounds(column.spec))

    offenders: list[Offender] = []
    sql = f"SELECT {', '.join(selected)} FROM {table} WHERE {predicate}"
    for row in conn.execute(sql, params):
        timestamp = int(row[0])
        module_id = int(row[1]) if keyed_by_module else None
        for column, value in zip(checked, row[len(key_columns) :], strict=True):
            if value is None:
                continue
            stored = int(value)
            if column.spec.within_bounds(column.spec.decode(stored)):
                continue
            offenders.append(Offender(column, timestamp, module_id, stored))
    return offenders


def find(conn: sqlite3.Connection, columns: Sequence[Column] | None = None) -> list[Offender]:
    """Return every out-of-bounds reading across every tier, oldest table first.

    The default column set is the whole registry across every tier, which is the
    point of the tool: a scale corrected in one place shows up in the raw tier
    and in every rollup built from it, and a scrub that covered only the tier
    somebody happened to be looking at would leave the coarse tiers — the ones
    kept indefinitely — carrying the fault for good.
    """
    to_check = columns_to_check() if columns is None else columns
    by_table: dict[str, list[Column]] = {}
    for column in to_check:
        by_table.setdefault(column.table, []).append(column)
    offenders: list[Offender] = []
    for table, table_columns in by_table.items():
        offenders.extend(scan_table(conn, table, table_columns))
    return offenders


def clear(conn: sqlite3.Connection, offenders: Sequence[Offender]) -> int:
    """Set every listed reading to NULL, and return how many rows changed.

    Each update names the integer it expects to find. A row the collector
    rewrote between the scan and this call therefore matches nothing and is left
    exactly as it is, which is what makes the tool safe to run against a
    database that is being written to. The returned count is of rows actually
    changed, so a caller can tell a clean run from one that raced and lost.

    NULL, not zero. The whole point of clearing an impossible reading is to
    leave a hole where a chart will break the line, and a zero would instead
    draw the grid collapsing or the bank going flat.
    """
    changed = 0
    with conn:
        for offender in offenders:
            column = offender.column
            sql = f"UPDATE {column.table} SET {column.column} = NULL WHERE timestamp = ?"
            params: list[int] = [offender.timestamp]
            if column.keyed_by_module and offender.module_id is not None:
                sql += " AND module_id = ?"
                params.append(offender.module_id)
            sql += f" AND {column.column} = ?"
            params.append(offender.stored)
            changed += conn.execute(sql, params).rowcount
    return changed


def _report(offenders: Sequence[Offender]) -> None:
    """Log one line per affected column: how many, how far out, and when.

    Grouped by column rather than listed per row, because a scale mistake
    produces one line of real information and as many rows as the collector
    managed to write before it was noticed. The extremes and the time span are
    what distinguish a corrected scale from a single odd reading, so they are on
    the line rather than a row count on its own.
    """
    grouped: dict[tuple[str, str], list[Offender]] = {}
    for offender in offenders:
        grouped.setdefault((offender.column.table, offender.column.column), []).append(offender)
    for (table, column), found in sorted(grouped.items()):
        spec = found[0].column.spec
        values = [o.decoded for o in found]
        stamps = [o.timestamp for o in found]
        logger.info(
            "  %-16s %-38s %5d value(s) %g to %g %s, outside %g to %g, %s to %s",
            table,
            column,
            len(found),
            min(values),
            max(values),
            spec.unit or "-",
            spec.lower,
            spec.upper,
            datetime.fromtimestamp(min(stamps), UTC),
            datetime.fromtimestamp(max(stamps), UTC),
        )


def main() -> int:
    """Scan every tier, report what is impossible, and clear it if asked.

    Exits non-zero when out-of-bounds readings are still in the database once
    the run finishes, so this can be scheduled as a check rather than only run
    by hand. A report-only run that finds something therefore fails, which is
    the intended signal: something is in there that nothing downstream can draw.
    """
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, type=Path, help="path to the arraysense database")
    ap.add_argument(
        "--clear",
        action="store_true",
        help="set the offending readings to NULL instead of only reporting them",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="  %(message)s")

    # sqlite3.connect creates the file it cannot find, so a mistyped path would
    # otherwise open an empty database, find no columns to check, and report a
    # clean bill of health for a database it never looked at. The one thing this
    # tool must never do is say "nothing wrong" about the wrong file.
    if not args.db.exists():
        logger.error("no database at %s", args.db)
        return 2

    # Deliberately not SqliteStore: opening that runs the schema DDL and the
    # column migration, and a maintenance tool must leave everything it is not
    # fixing exactly as it found it. The busy timeout is what lets this run
    # while the collector holds the writer lock, so the service need not stop.
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        scanned = sorted(
            t for t in {c.table for c in columns_to_check()} if _existing_columns(conn, t)
        )
        if not scanned:
            logger.error("%s holds no tier table; is it an arraysense database?", args.db)
            return 2
        logger.info("scanning %s", ", ".join(scanned))
        offenders = find(conn)
        if not offenders:
            logger.info("no out-of-bounds readings in any tier")
            return 0
        logger.info("%s out-of-bounds reading(s):", f"{len(offenders):,}")
        _report(offenders)
        if not args.clear:
            logger.info("reporting only; pass --clear to set them to NULL")
            return 1
        changed = clear(conn, offenders)
        logger.info("cleared %s reading(s) to NULL", f"{changed:,}")
        remaining = find(conn)
        if remaining:
            logger.warning("%s reading(s) still out of bounds after clearing", len(remaining))
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
