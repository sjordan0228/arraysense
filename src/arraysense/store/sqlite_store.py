"""sqlite_store.py — the SQLite store: samples in as scaled integers, floats back out.

The tables come from ``arraysense.store.schema``, the scales from
``arraysense.metrics``, and nothing here hardcodes either — a metric added to the
registry gets its column, its encoding and its bounds check without this file
being touched.

What survives the round trip unchanged is absence. A metric the inverter did not
report is NULL going in and None coming out, never zero, and a failed poll is
stored as its reason with no readings at all. A bank that has gone quiet because
CAN is down and a bank that is genuinely flat look nothing alike on a chart, and
conflating them is the bug this project exists to stop repeating. A reading
outside its plausible bounds is kept too — stored and flagged in
``invalid_readings`` rather than dropped, because a decode error is evidence
about the inverter or about our own scaling, and evidence that was thrown away
cannot be diagnosed six months later.

Both halves of a sample are written in one transaction: the wide inverter row
and, beside it, one normalised row per battery module keyed by an integer id
into the serials table. A crash can lose the poll in flight but cannot leave a
timestamp half-written.

Every row is stamped with a device — the serial of the inverter that produced
it. A store is opened for one device and every method defaults to it, so a
single-inverter installation reads and writes exactly as it always did; a
caller naming a different device gets that device's rows and nothing else. The
default is a default and not an assumption: the ``device`` argument reaches
every read and every write, because a parallel stack writes through one store
and a row that took the store's default would be filed under whichever
inverter happened to open it.

A store can also be opened for what its driver declares. The declared metric
set narrows the schema a fresh database is created with and the columns a
sample may write, so a device that cannot produce a reading has no column for
it — while reads answer from whatever columns the tables actually have, since
an older database may carry the full registry and its history must stay
readable. A metric the registry knows but this database has no column for
reads back as None, exactly like a reading the inverter never sent, because to
the caller both are the same fact: nothing was measured here.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType

from arraysense.metrics import lookup
from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.migrate import needs_device_migration
from arraysense.store.schema import (
    FOREIGN_KEYS_PRAGMA,
    INVERTER_TIERS,
    MODULE_TIERS,
    expected_columns,
    inverter_metric_columns,
    migration_ddl,
    module_metric_columns,
    schema_ddl,
)


def _is_memory_path(path: str) -> bool:
    """Say whether ``path`` names a database that lives only in memory.

    It matters because a memory database cannot be reopened: every
    ``sqlite3.connect(":memory:")`` makes a *new* empty one. Code that opens a
    second connection expecting to reach the same data would instead find an
    empty database, roll nothing up, and report success — which is the silent
    kind of wrong this project treats as a bug. Callers check this first.

    The URI forms are included because ``file::memory:`` and a ``mode=memory``
    query string reach the same place by a different spelling.
    """
    if path in {":memory:", ""}:
        return True
    return path.startswith("file::memory:") or "mode=memory" in path


def _inverter_table(tier: str) -> str:
    """Return the inverter table backing ``tier``, raising KeyError if there is none.

    Tier names arrive from the API as strings and end up interpolated into SQL as
    a table name. Resolving them against the tier definitions here is what keeps
    that interpolation safe: an unrecognised name never reaches the query.
    """
    for t in INVERTER_TIERS:
        if t.name == tier:
            return t.table
    raise KeyError(f"unknown inverter tier: {tier!r}")


def _module_table(tier: str) -> str:
    """Return the module table backing ``tier``, raising KeyError if there is none.

    Separate from the inverter lookup because the two sets of tiers differ: there is
    no module minute tier, so ``minute`` names a real inverter tier and no module
    table at all, and it has to fail here rather than resolve to the wrong one.
    """
    for t in MODULE_TIERS:
        if t.name == tier:
            return t.table
    raise KeyError(f"unknown module tier: {tier!r}")


class SqliteStore:
    """Persist inverter samples to a SQLite database, and read them back out.

    One instance owns one primary connection, which belongs to the collector's
    writes and to the cheap single-row reads that stay on the event loop. Reads
    that scan tiers go through ``read_view`` instead — the store bound to its
    own short-lived connection — and maintenance gets ``maintenance_connection``,
    so neither can hold the primary while the collector needs it. Writes go to
    the full-cadence tiers and commit per sample; the coarse tiers are filled
    later by ``arraysense.store.rollup`` and read back through the same query
    methods.

    Every metric name a caller passes is checked against the registry before any
    SQL is built. A typo therefore raises KeyError instead of returning a column
    of Nones, which would otherwise read exactly like an inverter that never
    reported the metric at all.

    One asymmetry to know before opening a database this class did not just
    create: ``metrics=None`` means the whole registry, so opening an existing
    *narrowed* database without the argument re-widens it — ``migration_ddl``
    adds every registry column the tables lack, permanently, since nothing here
    ever drops one. No production caller does that; the service always passes
    its driver's declaration. A tool that opens somebody's database read-mostly
    must pass the declared set (or query through a bare connection) rather than
    take the default and quietly grow ninety columns.
    """

    def __init__(
        self,
        path: str,
        device: str,
        metrics: Iterable[str] | None = None,
        *,
        synchronous: str = "full",
    ) -> None:
        """Open the database for one inverter, creating file and schema if absent.

        ``device`` is that inverter's serial and becomes the default for every
        read and every write. It is required rather than defaulted because
        there is no honest default: a row stamped with a placeholder identity
        looks attributed and is not, and the next unit added to the stack would
        inherit its history.

        ``metrics`` is the set of metric names the installed driver declares —
        ``Capabilities.metrics``, handed through untouched — and None means the
        whole registry. It governs what a fresh database gets columns for and
        what a sample may write; it never governs reads, which answer from the
        columns the tables actually have, so a database from before drivers
        declared subsets keeps its full history readable. A declared metric an
        older database is missing gains its column here, exactly as a metric
        newly added to the registry always has.

        The generated DDL is idempotent, so opening is also the upgrade path: a
        database made before a metric was added to the registry gains the missing
        columns here (see ``arraysense.store.schema.migration_ddl``), which is what
        lets adding a metric stay a one-line change instead of a migration. Foreign
        keys are enabled because the module tables' serial reference is decorative
        until they are, and WAL journalling is what makes a commit per sample cheap
        enough to do at all.

        What opening deliberately does *not* do is add the device column. That
        needs every table recreated, which is not something to run as a side
        effect of a constructor; a database still keyed on time alone is
        refused here with the command to fix it, rather than opened and then
        failing on the first write with "no such column".

        Raises:
            ValueError: ``device`` is blank, or the database predates device
                identity and has not been migrated.
            KeyError: ``metrics`` names something the registry does not hold.
        """
        # Normalised on the way in, not merely validated. The migration
        # resolves the same serial and strips it, so a stored setting with a
        # stray space would have the migration stamp CE12345678 while the
        # service read " CE12345678 " — every migrated row orphaned by a
        # character nobody can see.
        device = device.strip()
        if not device:
            raise ValueError("device must be a non-empty inverter serial")
        if needs_device_migration(path):
            raise ValueError(
                f"{path} was written before readings carried a device and must be "
                "migrated before it can be opened. Run `arraysense --migrate`."
            )
        self.device = device
        declared = None if metrics is None else frozenset(metrics)
        # What this store writes: the declared metrics, in registry order so
        # every database created for one driver lays its columns down the same
        # way. The full-registry name sets beside them answer "is this a
        # registry metric at all", which is a different question from "does
        # this driver produce it" — a typo and an undeclared reading both fail
        # loudly, but they are different mistakes and get different messages.
        self._columns = inverter_metric_columns(declared)
        self._specs = tuple(lookup(name) for name in self._columns)
        self._writable = frozenset(self._columns)
        self._module_columns = module_metric_columns(declared)
        self._inverter_names = frozenset(inverter_metric_columns())
        self._module_names = frozenset(module_metric_columns())
        self._undeclared_module_fields = tuple(
            name for name in module_metric_columns() if name not in set(self._module_columns)
        )
        # check_same_thread=False because the collector writes from the event
        # loop while the web server answers requests on a threadpool, and a
        # connection bound to its creating thread refuses the second one
        # outright. SQLite's default threading mode serialises access, and WAL
        # lets a reader work while a write is in flight.
        # Held absolute so a second connection reaches the same file even if the
        # working directory moves under a long-running service. A memory
        # database has no file to reopen and is recorded as such rather than
        # being turned into a path that would silently address a different,
        # empty database.
        self.is_memory_backed = _is_memory_path(path)
        self._path = path if self.is_memory_backed else os.path.abspath(path)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._synchronous = synchronous
        self._apply_connection_pragmas(self._conn, establish_wal=True)
        self._conn.executescript(schema_ddl(declared))
        existing = {
            table: tuple(r[1] for r in self._conn.execute(f"PRAGMA table_info({table})"))
            for table in expected_columns(declared)
        }
        for statement in migration_ddl(existing, declared):
            self._conn.execute(statement)
        # What each tier actually has, read back after the migrations so a
        # column just added is counted. Reads answer from this rather than from
        # the declaration: an older database carries columns this driver never
        # declared, and the history in them belongs to whoever asks.
        self._present: Mapping[str, frozenset[str]] = MappingProxyType(
            {
                table: frozenset(r[1] for r in self._conn.execute(f"PRAGMA table_info({table})"))
                for table in expected_columns()
            }
        )

    def close(self) -> None:
        """Release the database connection.

        This is not a flush point, whatever the name suggests. Python's sqlite3
        does not commit an open transaction on close — it discards it — so
        anything not already inside a committed block is lost here. Every write
        path in this class commits before returning for that reason, and any
        batched or deferred write added later has to do the same rather than
        rely on shutdown to save it.
        """
        self._conn.close()

    def _apply_connection_pragmas(self, conn: sqlite3.Connection, *, establish_wal: bool) -> None:
        """Apply the safety settings and durability every connection here gets.

        Foreign keys because the module tables' serial reference is decorative
        until they are on; WAL because it is what makes a commit per sample cheap
        enough to do at all, and what lets a reader work while a write is in
        flight; and a busy timeout so a connection waits rather than failing
        outright while another is writing — the alternative is an intermittent
        "database is locked" under load.

        WAL mode is persistent database state, so only the primary connection
        establishes it at startup. Reissuing ``journal_mode = WAL`` from every
        maintenance connection is not connection setup: it asks SQLite to set
        persistent state again, takes locks to do so, and happens on the exact
        sixty-second cadence maintenance is meant to keep cheap. The temporary
        connection still needs foreign keys and its busy timeout, which really
        are per-connection settings.

        Every connection sets its ``synchronous`` explicitly, never inheriting
        it. Relying on SQLite's compile-time default would silently weaken the
        guarantee in a build configured with NORMAL as its default, while a test
        running against the usual FULL default would never expose it — and when
        only the primary set it, a deployment's choice never reached the
        maintenance connection at all: measured on the production Pi, a store
        configured "normal" ran its once-a-minute passes at the distro default
        of FULL. The value is a deployment choice — see ``Config.synchronous`` —
        and defaults to FULL, which is what every installation had before it
        could be chosen.

        Measured on the reference Raspberry Pi, through this class's own
        ``append``: 200 polls cost 207 fsyncs at FULL and 7 at NORMAL, so the
        difference is about thirtyfold. What NORMAL trades away is bounded loss
        rather than integrity — SQLite stays consistent either way — and at that
        checkpoint rate an abrupt power cut discards roughly the last five
        minutes of readings.

        This helper keeps the shared rules together. A second copy drifts, and
        the drift that costs most is a maintenance connection opened without
        the busy timeout: it turns ordinary contention with the collector's own
        writes into a hard failure.
        """
        conn.execute(FOREIGN_KEYS_PRAGMA)
        if establish_wal:
            conn.execute("PRAGMA journal_mode = WAL")
        # Durability is per-connection state, and every connection this store
        # opens carries the configured value. It used to be set only on the
        # primary, which silently ignored a deployment's choice on the one
        # connection doing the bulk of the writing: measured on the production
        # Pi, a store configured "normal" ran its maintenance passes at the
        # distro default of FULL, fsyncing the SD card once a minute for a
        # setting the owner had traded away.
        conn.execute(f"PRAGMA synchronous = {self._synchronous.upper()}")
        conn.execute("PRAGMA busy_timeout = 5000")

    @contextlib.contextmanager
    def read_view(self) -> Iterator[SqliteStore]:
        """A store bound to its own connection, for reads that must not share.

        The HTTP handlers that scan tiers used to run their queries on the
        primary connection, from ``async def`` handlers — synchronous SQLite on
        the event loop. Measured on the production Pi, ``/api/calibration``
        held the loop for 1.6 to 3.2 seconds, and every concurrent response
        waited for it: the once-a-minute freeze issue #63 chased through the
        rollup pass was the dashboard's own refresh timers all along.

        A view is the measured way out. A reader on its own connection sees
        zero interference from writers under WAL — probed at 50 ms intervals
        through full maintenance passes on both reference filesystems without a
        single stall — and opening a configured connection costs 0.07 ms, so a
        view per request is cheap. The view shares every piece of the store's
        derived state except the connection, which it opens on entry and closes
        on exit.

        A memory-backed store cannot be reopened — every connect() makes a new
        empty database — so it yields the store itself, and the caller must not
        assume the view outlives the block either way. Views are for reading;
        nothing stops a write technically, but a writer on a view forfeits the
        one-writer reasoning the primary connection's callers rely on.
        """
        if self.is_memory_backed:
            yield self
            return
        # The fresh connection is created and closed here, never through the
        # view, so a failure partway can never close the primary by mistake.
        conn = sqlite3.connect(self._path, check_same_thread=False)
        try:
            self._apply_connection_pragmas(conn, establish_wal=False)
            view = object.__new__(SqliteStore)
            # Wholesale, not attribute by attribute: a hand-picked copy is an
            # invariant nobody maintains, and an attribute added to __init__
            # later would be silently absent from every view until some read
            # path raised AttributeError in production. Everything shared this
            # way is immutable after __init__; only the connection is replaced.
            view.__dict__.update(self.__dict__)
            view._conn = conn
            yield view
        finally:
            conn.close()

    def maintenance_connection(self) -> sqlite3.Connection:
        """Open a second connection to this database, for work done off the loop.

        Long housekeeping — the rollup pass is the one that prompted this — has
        to run in a thread or it freezes every HTTP response for its duration.
        A thread alone is not enough: on a ``sqlite3.Connection`` ``with conn:``
        is transaction state rather than a lock, so two threads entering it on
        one connection share a single commit and a single rollback. The
        collector's per-sample commit would then land a rollup's half-built
        tiers, and a failed rollup would roll back a reading that had been
        stored successfully. Losing a reading is the failure this project exists
        to prevent, so the work gets its own connection and its own transactions.

        WAL is what makes that affordable: readers are never blocked by the
        writer, so the API keeps answering throughout. What remains is writer
        contention with the collector itself, which the busy timeout absorbs.

        The caller closes it, and should do so on the same thread that used it.

        Raises:
            ValueError: the store is memory-backed, where a second connection
                would address a different and empty database rather than this
                one — see ``is_memory_backed``.
        """
        if self.is_memory_backed:
            raise ValueError(
                "a memory-backed store has no second connection: ':memory:' opens a "
                "new empty database each time. Check is_memory_backed and run inline."
            )
        conn = sqlite3.connect(self._path, check_same_thread=False)
        self._apply_connection_pragmas(conn, establish_wal=False)
        return conn

    def _device(self, device: str | None) -> str:
        """Resolve a caller's device against the one this store was opened for.

        None means "the inverter this store is for", which is what every
        single-inverter caller wants and what keeps the existing pages
        unchanged. Naming one explicitly is how a parallel stack writes several
        inverters through one store.

        A blank name is refused rather than treated as absent, matching what
        ``__init__`` already does with one. The two disagreeing is how
        ``?device=`` on a query string — an empty value the browser sends
        readily — became a device nothing has ever recorded, answering with no
        rows and no error, which reads as an inverter that stopped reporting.
        """
        if device is None:
            return self.device
        cleaned = device.strip()
        if not cleaned:
            raise ValueError("device must not be blank")
        return cleaned

    def append(self, sample: Sample, device: str | None = None) -> None:
        """Append one poll: the wide inverter row, and one row per battery module.

        Reported values encode to scaled integers, unreported ones store as NULL,
        an implausible one is stored anyway and flagged, and a failed poll keeps
        its reason and no readings — the module docstring says why each of those
        matters. Modules go to the full-cadence module tier keyed by
        (timestamp, device, module_id), all inside the one transaction this call
        commits.

        Writing a timestamp that already exists *for the same device* overwrites
        it in place, later write winning, so a collector retrying after a partial
        failure repairs its row rather than doubling it — and two inverters
        polled at the same instant write two rows rather than one overwriting the
        other. The bounds flags for that timestamp are cleared before being
        rewritten for the same reason: otherwise a retry would count a fault that
        happened once as several.

        The sample's timestamp must be timezone-aware. A naive one is read as local
        time on the way to epoch seconds, which files the row hours from where it
        belongs and puts it out of order with its neighbours.

        Raises:
            KeyError: a reading names something that is not an inverter metric —
                a typo, or a per-module metric that belongs in
                ``battery_modules``; a reading names a registry metric outside
                the set this store was opened for; or a battery module carries a
                value for an undeclared template. The last two are declaration
                drift — the driver emits what it did not declare. Note that
                KeyError is not among the collector's ``STORE_ERRORS``, so a
                sample that raises here stops the poll loop (logged, and the
                watchdog restarts the process) rather than being recorded as a
                gap. That is deliberate: every one of these is a deterministic
                programming error that would fail every sample identically, and
                recording it as a gap per poll would leave a service that looks
                like it is riding out an outage while it writes nothing, forever.
        """
        epoch = int(sample.timestamp.timestamp())
        unit = self._device(device)
        self._validate_reading_names(sample.readings)
        with self._conn:
            cur = self._conn.cursor()
            # The row upsert is idempotent, so the flags must be too. Without
            # clearing first, a collector retrying the same timestamp would
            # inflate the failure count for a fault that happened once. Scoped
            # to this device, or one inverter's retry would erase another's
            # record of the same instant.
            cur.execute(
                "DELETE FROM invalid_readings "
                "WHERE timestamp = ? AND device = ? AND serial IS NULL",
                (epoch, unit),
            )
            for name, value in sample.readings.items():
                spec = lookup(name)
                if not spec.within_bounds(value):
                    cur.execute(
                        "INSERT INTO invalid_readings (timestamp, device, metric, value, serial) "
                        "VALUES (?, ?, ?, ?, NULL)",
                        (epoch, unit, name, value),
                    )
            values: list[int | str | None] = [
                spec.encode(sample.readings[spec.name]) if spec.name in sample.readings else None
                for spec in self._specs
            ]
            values.append(sample.error)
            self._upsert_inverter_row(cur, epoch, unit, self._columns, values)
            self._append_modules(cur, epoch, unit, sample.battery_modules)

    def query(
        self,
        metrics: Sequence[str],
        start: datetime,
        end: datetime,
        tier: str = "full",
        device: str | None = None,
    ) -> list[dict[str, object]]:
        """Read one inverter's metrics over a time range, oldest first.

        Values come back as real-world floats, decoded through each metric's scale,
        and a metric the inverter never reported stays None rather than becoming
        zero. Every row carries ``timestamp`` and ``error``: a row with ``error``
        set is a recorded gap and a caller drawing it must break the line there,
        because an outage smoothed into a straight segment is an outage nobody ever
        notices. Rows from a rollup tier carry ``sample_count`` as well, so a bucket
        built from three readings can be told from one built from three hundred.

        One device, always: ``device`` defaults to the one this store was opened
        for, and there is no way to ask for all of them at once. Two inverters'
        power readings interleaved in one series is not a chart of anything, and
        a caller that wants both asks twice and draws two lines.

        Both ends of the range are included and both must be timezone-aware. An
        unknown metric name or tier raises KeyError before any SQL is built; see
        ``store.tiers`` for picking the tier from a range and a pixel width.
        """
        table = _inverter_table(tier)
        names = self._check_inverter_names(metrics)
        counted = tier != "full"
        columns = ["timestamp", *names, "error"] + (["sample_count"] if counted else [])
        selected = ["timestamp", *(self._selected(table, n) for n in names), "error"] + (
            ["sample_count"] if counted else []
        )
        rows = self._conn.execute(
            f"SELECT {', '.join(selected)} FROM {table} "
            "WHERE timestamp BETWEEN ? AND ? AND device = ? ORDER BY timestamp",
            (int(start.timestamp()), int(end.timestamp()), self._device(device)),
        ).fetchall()
        return [self._decode_row(columns, row, names) for row in rows]

    def query_modules(
        self,
        metrics: Sequence[str],
        start: datetime,
        end: datetime,
        tier: str = "full",
        serial: str | None = None,
        device: str | None = None,
    ) -> list[dict[str, object]]:
        """Read one inverter's per-module battery readings over a range, oldest first.

        Rows are identified by ``serial``, never by slot: the inverter rotates
        modules through its register slots, so a caller plotting one battery asks
        for its serial and the rotation neither splits that battery's series in two
        nor averages two batteries into one. Naming a serial narrows the read to
        that module; None returns them all, ascending by time then serial.

        ``device`` defaults to the store's own and narrows to that inverter's
        bank. Serial is unique per device rather than globally, so a bank filtered
        by serial alone could return two inverters' rows under one name.

        Metric names here are the bare module ones, ``soc_pct`` rather than the
        per-slot registry key. As with ``query``, both ends of the range are
        included, both must be timezone-aware, and an unknown name or tier raises
        KeyError.
        """
        table = _module_table(tier)
        names = self._check_module_names(metrics)
        counted = tier != "full"
        selected = ["m.timestamp", "s.serial", *(self._selected(table, n, "m.") for n in names)]
        if counted:
            selected.append("m.sample_count")
        sql = (
            f"SELECT {', '.join(selected)} FROM {table} m "
            "JOIN serials s ON s.id = m.module_id "
            "WHERE m.timestamp BETWEEN ? AND ? AND m.device = ?"
        )
        params: list[object] = [
            int(start.timestamp()),
            int(end.timestamp()),
            self._device(device),
        ]
        if serial is not None:
            sql += " AND s.serial = ?"
            params.append(serial)
        sql += " ORDER BY m.timestamp, s.serial"
        columns = ["timestamp", "serial", *names] + (["sample_count"] if counted else [])
        return [
            self._decode_row(columns, row, names, module=True)
            for row in self._conn.execute(sql, params).fetchall()
        ]

    def latest(
        self,
        metrics: Sequence[str],
        device: str | None = None,
        *,
        include_gaps: bool = True,
    ) -> dict[str, object] | None:
        """Return one inverter's most recent reading, or None if it has stored none.

        This is what a live view asks for on every refresh, so it rides the primary
        key backwards and stops at the first row for this device. The key leads with
        timestamp, so that walk skips whatever other inverters recorded more recently
        — a handful of rows in a parallel stack, and measured at 0.004 ms against a
        792,510-row database. A device with *no* rows at all is the bad case, because
        the walk then has nothing to stop at and reads the whole tier: 9 ms on that
        same database. That is a misconfigured serial rather than ordinary use, and
        the module docstring in ``store.schema`` says why the key is ordered this way
        regardless.

        The row it returns may be a recorded gap, carrying ``error`` and no readings;
        recency is not health.

        A row that carries *none* of the requested metrics and no error is not a
        reading of them and is skipped, because two writers share this tier: the
        weather poller lands a row every fifteen minutes whose inverter columns
        are all null, and returning it here blanked the live dashboard until the
        next inverter poll — an absence drawn where data exists, the exact
        mistake this project forbids. The mirror holds too: a request for the
        weather metrics walks past the inverter rows to the last sky reading.
        Gap rows still answer every request, because recency is not health and a
        gap is information — unless the caller opts out with ``include_gaps=False``,
        which asks for the newest actual reading of the named metrics and walks
        past gaps. With no metrics named there is nothing to qualify a reading,
        so ``include_gaps`` has no effect and the newest row of any kind answers.
        """
        names = self._check_inverter_names(metrics)
        columns = ["timestamp", *names, "error"]
        selected = ["timestamp", *(self._selected("inverter_raw", n) for n in names), "error"]
        # Asked for no metrics, the question is "when did any row land" and the
        # newest row of any kind is the answer — the row-based contract callers
        # like the gap search rely on. Names are whitelist-checked above, so
        # interpolating them into the predicate is safe.
        carries = ""
        if names:
            # include_gaps=False asks for the newest actual reading of these
            # metrics and nothing else. The default surfaces gap rows because
            # for the dashboard recency is not health — but a reader of the
            # site metrics (the sky readout) must not blank every time an
            # inverter gap lands newer than the last weather tick, so it opts
            # out and the walk continues past the gaps to the last real value.
            witness = [f"{n} IS NOT NULL" for n in names]
            if include_gaps:
                witness.append("error IS NOT NULL")
            terms = " OR ".join(witness)
            carries = f"AND ({terms}) "
        row = self._conn.execute(
            f"SELECT {', '.join(selected)} FROM inverter_raw WHERE device = ? "
            f"{carries}"
            "ORDER BY timestamp DESC LIMIT 1",
            (self._device(device),),
        ).fetchone()
        return None if row is None else self._decode_row(columns, row, names)

    def latest_modules(
        self, metrics: Sequence[str], device: str | None = None
    ) -> list[dict[str, object]]:
        """Return one inverter's battery modules, each with its last reading, by serial.

        Every module the store has ever seen appears at most once, carrying whatever
        it last reported — including a module that fell off the CAN bus a week ago,
        whose final reading stands here indefinitely. There is no time bound at all,
        so anything comparing packs against each other has to check the timestamps
        itself; ``arraysense.calibration`` drops stale packs before comparing
        voltages for exactly this reason.

        A module that has never reported is absent from the result rather than
        present with zeroes. Narrowed to one inverter's bank, because the packs on
        a second inverter are a second bank: they are not in parallel with these,
        so nothing that compares packs against each other may see both at once.
        """
        names = self._check_module_names(metrics)
        selected = [
            "m.timestamp",
            "s.serial",
            *(self._selected("module_raw", n, "m.") for n in names),
        ]
        rows = self._conn.execute(
            f"SELECT {', '.join(selected)} FROM module_raw m "
            "JOIN serials s ON s.id = m.module_id "
            "WHERE m.device = ? AND m.timestamp = ("
            "  SELECT MAX(timestamp) FROM module_raw WHERE module_id = m.module_id"
            ") ORDER BY s.serial",
            (self._device(device),),
        ).fetchall()
        columns = ["timestamp", "serial", *names]
        return [self._decode_row(columns, row, names, module=True) for row in rows]

    def _check_inverter_names(self, metrics: Sequence[str]) -> list[str]:
        """Return ``metrics`` unchanged, raising if any is not an inverter metric.

        Checked against the whole registry rather than the declared set: a read
        is a question about history, and an older database may hold columns for
        metrics this driver never declared. A registry metric this database has
        no column for is answered with None per row instead — see ``_selected``.
        """
        unknown = [m for m in metrics if m not in self._inverter_names]
        if unknown:
            raise KeyError(f"unknown inverter metric(s): {unknown}")
        return list(metrics)

    def _check_module_names(self, metrics: Sequence[str]) -> list[str]:
        """Return ``metrics`` unchanged, raising if any is not a module metric."""
        unknown = [m for m in metrics if m not in self._module_names]
        if unknown:
            raise KeyError(f"unknown module metric(s): {unknown}")
        return list(metrics)

    def _selected(self, table: str, name: str, prefix: str = "") -> str:
        """Return the SELECT expression for one metric column of ``table``.

        A registry metric the table has no column for — this driver never
        declared it, so no schema was ever laid down for it — selects as
        ``NULL AS name``: the same None a reading nobody took decodes to, and
        to the caller the same fact. Raising instead would make the live page,
        which asks for every registry metric whatever the device, fail on
        precisely the hardware this narrowing exists for.
        """
        if name in self._present[table]:
            return f"{prefix}{name}"
        return f"NULL AS {name}"

    def _decode_row(
        self,
        columns: Sequence[str],
        row: tuple[object, ...],
        metric_names: Sequence[str],
        module: bool = False,
    ) -> dict[str, object]:
        """Turn one stored row into real-world values.

        Scaled integers decode through their metric's scale; a NULL stays None
        so absent data never arrives as a zero. Timestamps come back
        timezone-aware in UTC.
        """
        out: dict[str, object] = {}
        for name, value in zip(columns, row, strict=True):
            if name == "timestamp":
                assert isinstance(value, int)
                out[name] = datetime.fromtimestamp(value, tz=UTC)
            elif name in metric_names and value is not None:
                assert isinstance(value, int)
                # Module tables store the bare template name; the registry
                # keys per slot, and every slot shares one scale, so slot 1
                # answers for all of them.
                spec = lookup(f"battery_module1_{name}" if module else name)
                out[name] = spec.decode(value)
            else:
                out[name] = value
        return out

    def _validate_reading_names(self, readings: dict[str, float]) -> None:
        """Raise KeyError for any reading this store has no column to write.

        A name outside the inverter registry — a typo, or a per-module metric that
        belongs in ``battery_modules`` — is a programming error, and the row builder
        would never go looking for it: the value would vanish without a trace and
        the metric would read for all time as one the inverter never reported.
        Checking up front is what turns that into a loud failure, and doing it
        before the transaction opens is what stops a half-written sample.

        A registry metric outside the declared set fails just as loudly, as its
        own mistake: the driver's declaration and its output have drifted, and
        a declaration that under-claims silently drops real measurements — the
        same vanishing, one layer up.
        """
        unknown = sorted(set(readings) - self._writable)
        if not unknown:
            return
        name = unknown[0]
        if name not in self._inverter_names:
            raise KeyError(f"unknown metric name: {name!r}")
        raise KeyError(f"metric {name!r} is not among those declared for this store")

    def _upsert_inverter_row(
        self,
        cur: sqlite3.Cursor,
        epoch: int,
        device: str,
        columns: tuple[str, ...],
        values: list[int | str | None],
    ) -> None:
        """Write one wide row to inverter_raw, the later write winning.

        ``ON CONFLICT(timestamp, device) DO UPDATE`` is what makes a collector retry
        after a partial failure idempotent: the row is never duplicated, and the
        second write's values — NULLs included, for metrics it did not report —
        replace the first's rather than merging with them. The device is in the
        conflict target because it is in the key: without it, two inverters polled
        at the same second would take turns overwriting one row.

        ``values`` runs positionally against ``columns``, with the error reason
        appended as one final extra element. That is why ``append`` builds the pair
        together straight from the registry: a list assembled any other way is still
        the right length, so it writes cleanly and files every metric under the
        wrong column name.

        ``columns`` may be empty — a declaration with no inverter metric is
        legal, and a gap row has a reason to store regardless — so the update
        list is joined as one list with ``error`` inside it. Concatenating the
        error clause onto a joined string put a bare leading comma in the SQL
        whenever the columns were empty, which broke gap recording for exactly
        the device class the empty declaration describes.
        """
        all_cols = ("timestamp", "device", *columns, "error")
        cols_sql = ", ".join(all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        updates = ", ".join([*(f"{c}=excluded.{c}" for c in columns), "error=excluded.error"])
        cur.execute(
            f"INSERT INTO inverter_raw ({cols_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT(timestamp, device) DO UPDATE SET {updates}",
            (epoch, device, *values),
        )

    def _append_modules(
        self,
        cur: sqlite3.Cursor,
        epoch: int,
        device: str,
        modules: tuple[BatteryModuleSample, ...],
    ) -> None:
        """Write one normalised row per battery module to module_raw.

        A module's identity is its serial, never its slot. The inverter rotates
        modules through a fixed set of register slots, which a bank may outnumber,
        so a slot is positional metadata; keying rows by it would hand one battery's
        history to another every time the rotation moved. Each serial resolves to a
        stable integer id and the row is keyed by (timestamp, module_id).

        The scale does not depend on which slot the pack sat in. The registry
        expands one template across slots and every slot's spec comes from that
        same template, so the first slot's spec answers for all of them — exactly
        as the read path already resolves a module column — and a fifth pack
        encodes by the same scale as the first, keeping one unit per column. A
        bank larger than four needs no new column because a pack is a row, not a
        column.

        The slot does still name the reading in ``invalid_readings``, which is a
        label rather than a lookup and so is not bound to the slots the registry
        expands. ``validate.py`` reports the same reading the same way; the two
        resolve bounds from one template and name the pack from its slot, and
        would otherwise give one database two names for one condition. An
        unreported field stores as NULL never zero, and an out-of-bounds value is
        stored anyway and flagged against the serial, so a suspect reading stays
        attributable to the pack that produced it.

        A failed poll carries no modules, so nothing is written for one.

        A module reading outside the declared templates raises KeyError. The
        check runs per module, so the inverter row — and any earlier module's
        rows — may already have executed when a later module raises; it is the
        transaction around the whole sample that makes the outcome clean,
        rolling every one of them back. The raise itself is the point: the
        driver's declaration and its output have drifted, and writing around
        the missing column would drop the measurement without a trace.
        """
        columns = self._module_columns
        for module in modules:
            for name in self._undeclared_module_fields:
                if getattr(module, name) is not None:
                    raise KeyError(f"metric {name!r} is not among those declared for this store")
            module_id = self._resolve_serial(cur, device, module.serial)
            # The row upsert is idempotent, so the flags must be too. Clearing
            # this module's flags for the timestamp before rewriting makes a
            # retry of the same timestamp idempotent, and a later valid reading
            # clears a stale flag.
            cur.execute(
                "DELETE FROM invalid_readings WHERE timestamp = ? AND device = ? AND serial = ?",
                (epoch, device, module.serial),
            )
            values: list[int | None] = []
            for name in columns:
                value = getattr(module, name)
                if value is None:
                    values.append(None)
                    continue
                spec = lookup(f"battery_module1_{name}")
                if not spec.within_bounds(value):
                    cur.execute(
                        "INSERT INTO invalid_readings (timestamp, device, metric, value, serial) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            epoch,
                            device,
                            f"battery_module{module.slot}_{name}",
                            value,
                            module.serial,
                        ),
                    )
                values.append(spec.encode(value))
            self._upsert_module_row(cur, epoch, device, module_id, columns, values)

    def _resolve_serial(self, cur: sqlite3.Cursor, device: str, serial: str) -> int:
        """Return the serials-table id for this device's ``serial``, registering it once.

        This runs for every module of every poll, so it has to be cheap and
        endlessly repeatable: ``INSERT OR IGNORE`` is a no-op once the serial
        exists, and the id is read back through the UNIQUE index either way.
        Registering on sight is also what lets a battery added to the bank start
        recording immediately, with no configuration step to forget.

        The uniqueness is (device, serial), so a pack reported by two inverters
        takes two ids. What that keeps apart is two banks, which is the common
        case; what it costs is a pack physically moved between inverters, whose
        history then reads as two, and joining on the serial column is what
        recovers it.

        Raises:
            AssertionError: the serial neither registered nor resolved. That cannot
                happen after an INSERT OR IGNORE into a UNIQUE column, so it means
                the serials table is not the table this code believes it is.
        """
        cur.execute(
            "INSERT OR IGNORE INTO serials (device, serial) VALUES (?, ?)", (device, serial)
        )
        cur.execute("SELECT id FROM serials WHERE device = ? AND serial = ?", (device, serial))
        row = cur.fetchone()
        if row is None:
            raise AssertionError(f"serial {serial!r} did not resolve to an id")
        return int(row[0])

    def _upsert_module_row(
        self,
        cur: sqlite3.Cursor,
        epoch: int,
        device: str,
        module_id: int,
        columns: tuple[str, ...],
        values: list[int | None],
    ) -> None:
        """Write one normalised row to module_raw, the later write winning.

        ``ON CONFLICT(timestamp, device, module_id) DO UPDATE`` makes a retry after
        a partial failure idempotent exactly as it does on the inverter row: never
        a duplicate, and the second write's values — NULLs included, for fields it
        did not report — replace the first's. ``values`` is positional against
        ``columns``, and the caller builds both from the store's declared module
        templates in one pass so the row's order is always the table's order.

        With no template declared at all the row is identity and a timestamp,
        and a retry of it has nothing to update: the conflict clause becomes
        DO NOTHING, which for identical rows is the same idempotency. DO UPDATE
        with an empty assignment list is a syntax error, not a no-op.
        """
        all_cols = ("timestamp", "device", "module_id", *columns)
        cols_sql = ", ".join(all_cols)
        placeholders = ", ".join("?" for _ in all_cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in columns)
        conflict = f"DO UPDATE SET {updates}" if columns else "DO NOTHING"
        cur.execute(
            f"INSERT INTO module_raw ({cols_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT(timestamp, device, module_id) {conflict}",
            (epoch, device, module_id, *values),
        )
