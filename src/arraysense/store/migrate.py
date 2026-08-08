"""migrate.py — give a database written before device identity its device column.

Every reading used to be keyed on time alone, because there had only ever been
one inverter. SQLite cannot ALTER a primary key, so adding the device to it
means recreating each table and copying every row across — which is why this is
an explicit function with a report rather than something that happens quietly
behind an open. The reference installation's database carries months of
SolarAssistant history that the Pi it came from will not hold forever and that
nothing can reproduce, so the failure this is written against is not "the
migration was slow" but "the migration lost rows".

Three properties do that work.

*One transaction.* Tables are created, filled, dropped and renamed inside a
single ``BEGIN IMMEDIATE``. A process killed at any point rolls the whole thing
back to the database it started from, which is still readable by the old code.
Nothing is ever left half-copied, and there is no resume state to get wrong.

*A counted copy.* Each table's rows are counted before the copy and the insert's
own count is compared against it. A mismatch aborts the transaction rather than
reporting success over a table that lost rows, because a silent short copy is
the one outcome nobody would notice until the history was already gone.

*Per-table detection.* A table that already has a device column is skipped, so
running this twice is a no-op and rows keep the identity they were first given.
Re-stamping on every run would hand one inverter's history to another the first
time somebody corrected a typo in the config.

What it costs, measured twice. Against a synthetic database with the reference
installation's row counts — 792,510 rows across seven tables — 1.4 s at 103 MB
and 2.5 s at 248 MB. Against a snapshot of the reference installation itself,
796,156 rows and 134 MB: 3.6 s. Call it a few seconds; the collector is stopped
for it and that is the whole downtime.

Disk is the part worth planning for, and it goes the wrong way twice. Copying
leaves the old tables' pages free rather than returning them, so the file
roughly doubles: 103 MB to 216 MB synthetic, and 134 MB to 277 MB on the real
snapshot. Those
pages are reused by later writes, so the file stops there rather than growing
again, and ``VACUUM`` reclaims them immediately for anyone who would rather
have the space back. Separately, a row written before a metric was added to
the registry physically stops at the last column it had — SQLite does not
rewrite rows on ALTER TABLE ADD COLUMN — and copying it writes it out at full
width. A database whose history predates most of the registry therefore grows
by more than the device column accounts for.

One trap when checking any of this: ``du`` on the reference installation reports
27 MB for the same file SQLite sees as 134 MB, because the filesystem there is
ZFS with compression on. Size the free space against the logical figure, not
against what ``du`` says, or the headroom calculation comes out five times too
optimistic.

Two SQLite mechanics are load-bearing and neither is obvious. Foreign keys are
turned off for the duration and checked before the commit — the module tiers
reference the serials table, and dropping it with enforcement on is refused.
``legacy_alter_table`` is turned on for the same window, because a modern
``ALTER TABLE ... RENAME`` reparses the whole schema and fails while the module
tiers still name a serials table that has been dropped and not yet replaced.
Both pragmas are no-ops inside a transaction, so both are set before it opens
and restored after it closes.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from arraysense.store.schema import (
    DEVICE,
    DEVICED_TABLES,
    SAMPLE_COUNT,
    ddl_for,
    indexes_for,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MigrationReport:
    """What one migration run did, table by table.

    Exists so the run can be judged rather than trusted: the caller sees how
    many rows each table carried across, and a table missing from ``rows`` is
    one that was already migrated. ``already_migrated`` distinguishes the
    second run of a successful migration from a database that had nothing in
    it — both move zero rows and they are not the same thing.
    """

    device: str
    already_migrated: bool
    rows: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        """How many rows the run moved altogether."""
        return sum(self.rows.values())


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    """Return the columns ``table`` currently has, empty if it does not exist."""
    return tuple(row[1] for row in conn.execute(f"PRAGMA table_info({table})"))


def _pending(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return the schema tables that exist and still lack a device column.

    A table that is absent is left out rather than created here: creating it is
    the store's job on open, and it will be created with the device column
    already in place.
    """
    pending = []
    for table in DEVICED_TABLES:
        columns = _columns(conn, table)
        if columns and DEVICE not in columns:
            pending.append(table)
    return tuple(pending)


def needs_device_migration(path: str) -> bool:
    """Whether the database at ``path`` still holds rows keyed without a device.

    Safe to call before anything has opened the database, which is the point:
    ``sqlite3.connect`` creates the file it cannot find, so a check that simply
    connected would leave an empty database behind on a fresh install and then
    report it clean. A path with no file is a first install and needs nothing.
    """
    if not Path(path).exists():
        return False
    conn = sqlite3.connect(path)
    try:
        return bool(_pending(conn))
    finally:
        conn.close()


def _copy_table(conn: sqlite3.Connection, table: str, staging: str, device: str) -> int:
    """Copy one table into its staged replacement, stamping every row.

    Only columns the old table actually has are carried, so a database made
    before a metric was added to the registry migrates cleanly and the new
    column simply arrives NULL — which is what an unreported reading is.
    ``SAMPLE_COUNT`` is the one exception, and it is worth being precise about
    why it is not the absent-as-zero mistake it resembles. The column is NOT
    NULL, so a legacy row has to carry something. Zero is the right something
    because it is unreachable for a real bucket — a bucket exists only because
    readings fell inside it, so a genuine count is always at least one. Zero
    therefore reads as "this row predates the column and its coverage is
    unknown", which is a sentinel and not a measurement, and it is the value the
    column-adding migration already uses for the same rows.

    A column the old table has and the new one does not aborts the whole
    migration rather than being dropped. Row counts cannot detect that kind of
    loss — every row survives, minus a column — so a run that quietly discarded
    one would report a success it had not achieved. Refusing is recoverable;
    the operator can add the metric back to the registry, or decide the column
    is genuinely unwanted and drop it deliberately. It can only happen if a
    metric was removed from the registry, which is a change nobody has made and
    which ``schema.migration_ddl`` deliberately refuses to automate.

    Raises:
        ValueError: the old table has a column the new schema does not, so
            copying would silently lose it.
        RuntimeError: fewer rows arrived than were counted. That cannot happen
            through a plain INSERT ... SELECT, so it means the copy was not the
            copy this code believes it was, and the caller rolls back rather
            than reporting a success that lost history.
    """
    old = set(_columns(conn, table))
    new = _columns(conn, staging)
    targets: list[str] = [DEVICE]
    sources: list[str] = ["?"]
    for column in new:
        if column == DEVICE:
            continue
        targets.append(column)
        if column in old:
            sources.append(column)
        elif column == SAMPLE_COUNT:
            sources.append("0")
        else:
            sources.append("NULL")

    dropped = sorted(old - set(new))
    if dropped:
        raise ValueError(
            f"{table} has {'a column' if len(dropped) == 1 else 'columns'} the current "
            f"schema does not: {', '.join(dropped)}. Migrating would copy every row "
            f"across without {'it' if len(dropped) == 1 else 'them'}, and a row count "
            "cannot tell you that happened. Add the metric back to the registry, or "
            "drop the column deliberately, then run this again."
        )

    expected = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    cur = conn.execute(
        f"INSERT INTO {staging} ({', '.join(targets)}) SELECT {', '.join(sources)} FROM {table}",
        (device,),
    )
    copied = cur.rowcount
    if copied != expected:
        raise RuntimeError(f"{table}: copied {copied} of {expected} rows")
    return copied


def migrate_devices(path: str, device: str) -> MigrationReport:
    """Stamp every stored reading with ``device``, recreating the tables to do it.

    ``device`` is the configured inverter's serial. It is written to every row
    of every tier, to the serials table so a pack is identified as that
    inverter's pack, and to the flagged-reading table so a decode fault stays
    attributable. Existing rows are moved, never dropped.

    Idempotent: a table that already carries a device column is skipped, so a
    second run reports ``already_migrated`` and touches nothing. Interrupting a
    run loses the run, not the database — see the module docstring for the
    transaction and the two pragmas that make that true.

    Raises:
        ValueError: ``device`` is blank. A row stamped with an empty identity
            is worse than an unmigrated one, because it looks migrated.
        sqlite3.Error: the database could not be rewritten. The transaction is
            rolled back first, so the database is the one the call started with.
    """
    if not device.strip():
        raise ValueError("device must be a non-empty inverter serial")
    if not Path(path).exists():
        # Checked before connecting, because sqlite3 creates the file it cannot
        # find: migrating a fresh install would otherwise leave an empty
        # database at the path and report it as already done.
        logger.info("no database at %s yet; nothing to migrate", path)
        return MigrationReport(device=device, already_migrated=True)

    conn = sqlite3.connect(path, isolation_level=None)
    try:
        pending = _pending(conn)
        if not pending:
            logger.info("database at %s already has device identity", path)
            return MigrationReport(device=device, already_migrated=True)

        logger.info("migrating %s to device %s: %s", path, device, ", ".join(pending))
        # Both pragmas are ignored inside a transaction, so they go first. The
        # module tiers reference serials, which has to be dropped and rebuilt.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("PRAGMA legacy_alter_table = ON")
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Asked again now the write lock is held. The first look was
            # outside it, so two migrations started together both saw the old
            # schema, and the one that waited would have re-stamped every row
            # with its own serial the moment the other committed — handing one
            # inverter's history to another, which is the single thing the
            # idempotence check exists to prevent.
            pending = _pending(conn)
            if not pending:
                conn.execute("ROLLBACK")
                logger.info("another migration finished first; nothing left to do")
                return MigrationReport(device=device, already_migrated=True)
            moved = _rewrite(conn, pending, device)
            broken = conn.execute("PRAGMA foreign_key_check").fetchall()
            if broken:
                raise RuntimeError(f"migration would orphan {len(broken)} row(s): {broken[:3]}")
            conn.execute("COMMIT")
        except BaseException:
            # BaseException, not Exception: a KeyboardInterrupt in the middle of
            # this has to roll back too, and it is not an Exception. Guarded on
            # the transaction still being open, because rolling back one SQLite
            # has already abandoned raises in its own right and would replace
            # the failure worth reading with a misleading one.
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute("PRAGMA foreign_keys = ON")
    finally:
        conn.close()

    for table, count in moved.items():
        logger.info("  %-18s %s row(s)", table, f"{count:,}")
    return MigrationReport(device=device, already_migrated=False, rows=moved)


def _rewrite(conn: sqlite3.Connection, pending: tuple[str, ...], device: str) -> dict[str, int]:
    """Create, fill, drop and rename each pending table, inside the caller's transaction.

    The four phases are separated rather than done a table at a time, because
    the module tiers reference the serials table: every staged table has to
    exist and be filled while the originals are still there, and every original
    has to be gone before any replacement takes its name.
    """
    staged = {table: f"_migrating_{table}" for table in pending}
    moved: dict[str, int] = {}
    for table, staging in staged.items():
        # A staged table exists only between the CREATE and the RENAME, both
        # inside this transaction, so no run can leave one behind. The drop is
        # here for the case that reasoning is wrong: a stale table would fail
        # the CREATE, and the recovery should not need somebody with a sqlite3
        # prompt and a copy of this file.
        conn.execute(f"DROP TABLE IF EXISTS {staging}")
        conn.execute(ddl_for(table, as_name=staging))
    for table, staging in staged.items():
        moved[table] = _copy_table(conn, table, staging, device)
    for table in staged:
        conn.execute(f"DROP TABLE {table}")
    for table, staging in staged.items():
        conn.execute(f"ALTER TABLE {staging} RENAME TO {table}")
        for statement in indexes_for(table):
            conn.execute(statement)
    return moved
