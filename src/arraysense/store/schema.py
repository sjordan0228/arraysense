"""schema.py — tier definitions and DDL generation, derived from the metric registry.

The database schema is generated, never written out by hand: every metric
column comes from arraysense.metrics, so adding a metric there stays a
one-line change and this module follows. Hardcoding a column name here would
defeat that.

The registry says what a metric *is*; it does not say this device produces it.
Every generator here therefore takes an optional ``declared`` set — the metric
names the installed driver's ``Capabilities`` carries — and narrows the columns
to it, so a one-string inverter does not get a column for a third string it
does not have. That column would otherwise hold a NULL meaning two different
things at once: there is no third string, and the third string reported
nothing. With no declared set the whole registry is generated, which is what
the device migration needs to rebuild a database from before drivers declared
anything.

What a declared set governs is tables that do not exist yet, and columns a
declared metric is missing. It never shrinks anything: ``CREATE TABLE IF NOT
EXISTS`` leaves an existing table exactly as it is, and ``migration_ddl`` only
ever ADDs. An installation whose tables carry the full registry — every one
from before this narrowing existed — keeps every column and every reading it
has; the undeclared columns simply stop being written. Dropping them would
destroy history to save bytes, and the store reads what is there rather than
what is declared for exactly that reason.

Two shapes are deliberate. Inverter metrics use a wide row — one row per
timestamp, one column per metric — because the main chart asks for solar,
load, battery and grid together, and a wide row costs the same as asking for
one. Per-module battery readings are normalised: one row per module per
timestamp, identified by an integer foreign key into the serials table. The
inverter exposes only four battery register slots and rotates modules through
them when more than four are present, so a slot is not a battery; slot-named
columns would let a long chart average two different physical batteries
together, and a rollup would make that permanent.

Every stored reading carries a ``device``: the serial of the inverter that
produced it. EG4 18kPVs stack in parallel, so a single installation can hold
several, and a schema keyed on time alone gives the second one's readings to
the first. It is the same rule that already governs battery modules — identity
is a serial, never a position — and it applies to the serials table too, so a
pack is "this device's pack with this serial" rather than a serial floating
free of the inverter that reported it.

``device`` comes *after* ``timestamp`` in every primary key. These are WITHOUT
ROWID tables, so the primary key is the storage order, and the rollup — the
one query that runs every sixty seconds while holding the write lock the poll
loop needs — asks a bare time range with no device in it. Timestamp first
keeps that a plain index range. Device first can answer it only by skipping
over each device prefix in turn, and SQLite picks that plan reliably only when
ANALYZE statistics say the leading column has few distinct values; nothing here
runs ANALYZE, so it does not get them.

Measured on a synthetic database with the reference installation's row counts
(792,510 rows, 103 MB), one maintenance pass: 220 ms with this key order,
219 ms device-first with ANALYZE run, 243 ms device-first without statistics —
which is the state a real database is in. So the penalty is about a tenth,
not the collapse the shape of the problem suggests: SQLite skip-scans it
competently even unaided. Timestamp first is still the right way round, but on
this evidence rather than on the argument above, and the difference is small
enough that it should not be treated as settling anything larger.

The cost of choosing this way is in ``latest``, which walks the key backwards
looking for one device's newest row. A device that has never recorded anything
walks the whole tier: 9 ms against 0.001 ms with a device-first key on the same
data. A device that is reporting costs 0.004 ms either way, so what this buys
the rollup is paid for only when asking about an inverter with no rows at all.

Retention tiers are described here and realised as one table per tier. The
DDL is idempotent — every statement is CREATE ... IF NOT EXISTS — so running
it twice is safe.

Timestamps are stored as INTEGER unix epochs (seconds): compact, ordered, and
unambiguous about timezone, matching the project's preference for small
integers in storage.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from arraysense.metrics import column_names


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

# The optional Emporia module's tiers. Raw and hourly only, for the reason the
# module tiers have no minute tier and the opposite reason besides: a circuit
# is polled once a minute already, so a minute tier would be a copy of the raw
# one under another name. Named here with the other two so tier selection reads
# one list rather than keeping a third copy of the table names in step by hand.
#
# ``keep_days`` describes the retention that is already running — the raw tier
# is pruned into the hourly one by store.retention, and the hourly tier appears
# in no prune table of its own, which is what None means here and why a
# thirteen-month circuit history is readable.
CIRCUIT_TIERS: tuple[Tier, ...] = (
    Tier("full", "circuit_reading", 30),
    Tier("hourly", "circuit_hourly", None),
)

SERIALS_TABLE = "serials"
INVALID_TABLE = "invalid_readings"
SETTINGS_TABLE = "settings"
FORECAST_TABLE = "forecast"
EFFICIENCY_TABLE = "efficiency_day"
PENDING_TABLE = "rollup_pending"
# The optional Emporia module's tables. Named here with the rest so the DDL
# builders, the dispatch and the startup schema all spell them once.
CIRCUIT_TABLE = "circuit"
CIRCUIT_READING_TABLE = "circuit_reading"
CIRCUIT_HOURLY_TABLE = "circuit_hourly"
CHARGER_CHANGE_TABLE = "charger_change"

_MODULE_PREFIX = re.compile(r"^battery_module\d+_")

# The column naming which inverter a row came from. Named once because the
# store, the rollups, the migration and the tools all have to spell it the
# same, and a second spelling is a silent second device.
DEVICE = "device"

# How many source rows a rollup bucket covers. A record of coverage only:
# every tier derives from raw, so nothing weights by it.
SAMPLE_COUNT = "sample_count"

# STRICT makes the column types real constraints rather than affinities, so text
# cannot land in a numeric column and a rollup average cannot be stored as an
# unrounded float. WITHOUT ROWID stops an INTEGER PRIMARY KEY from aliasing the
# rowid, which would otherwise let a NULL timestamp be silently assigned one.
# Neither is enforced by SQLite's defaults; both are needed together.
_TABLE_OPTIONS = "STRICT, WITHOUT ROWID"

# STRICT alone, for the one shape that cannot have the other half. A table whose
# key is a surrogate id needs SQLite to generate that id, and generating it is
# exactly the rowid aliasing WITHOUT ROWID removes: an insert that omits the id
# fails outright with "NOT NULL constraint failed" rather than counting up.
# Measured, not assumed — the circuit table was written with both options and
# refused its first row. Everything with a natural key keeps _TABLE_OPTIONS.
_TABLE_OPTIONS_ROWID = "STRICT"

# SQLite disables foreign keys on every new connection. The module tables'
# reference into the serials table is decorative until a connection turns this
# on, and an orphaned module_id would detach readings from their battery.
FOREIGN_KEYS_PRAGMA = "PRAGMA foreign_keys = ON"


def _declared_names(declared: Iterable[str]) -> frozenset[str]:
    """Normalise a driver's declared metric set, refusing names the registry lacks.

    ``Capabilities`` already checks a declaration at construction, so a
    stranger arriving here is a caller composing sets by hand — a test, or a
    future second entry point — and the failure has to be loud where the schema
    is generated. Passed through silently, an unknown name is simply a column
    that never appears, and the reading meant for it is dropped on the way to
    the store without a trace.
    """
    wanted = frozenset(declared)
    unknown = sorted(wanted - set(column_names()))
    if unknown:
        raise KeyError(f"no such metric(s) in the registry: {', '.join(unknown)}")
    return wanted


def inverter_metric_columns(declared: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return the inverter metric column names in registry order.

    These are the wide-row columns of every inverter tier. A ``declared`` set
    narrows them to what the installed driver produces; None means the whole
    registry. Order always follows the registry, never the declaration, so two
    databases created for the same driver lay their columns down identically.

    Read off ``column_names()`` at call time rather than bound at import, so
    the schema always reflects the registry as it stands — which is what lets
    a test extend the registry and watch the column arrive with nothing else
    edited. The per-slot pattern is what separates the two halves of the
    registry: ``battery_module_count`` starts with the same words as a module
    expansion and is an inverter metric, so a bare prefix test misfiles it.
    """
    names = tuple(name for name in column_names() if not _MODULE_PREFIX.match(name))
    if declared is None:
        return names
    wanted = _declared_names(declared)
    return tuple(name for name in names if name in wanted)


def module_metric_columns(declared: Iterable[str] | None = None) -> tuple[str, ...]:
    """Return the module metric column names, without slot numbers.

    The registry expands one template across the four battery slots
    (battery_module1_soc_pct, ...) because that is how the inverter presents
    them. Module tables store the template instead — plain ``soc_pct``, with
    the battery identified by serial — since a slot is not a battery, so the
    prefix has to come back off somewhere, and doing it here keeps the registry
    the only place the metric is named. Order follows first occurrence, which
    is template order.

    A ``declared`` set narrows the templates: one is kept when any slot's
    expansion of it is declared, because the normalised tables hold one column
    per template however many slots the registry expands it across. None means
    every template.
    """
    wanted = None if declared is None else _declared_names(declared)
    templates: list[str] = []
    kept: set[str] = set()
    for name in column_names():
        match = _MODULE_PREFIX.match(name)
        if not match:
            continue
        bare = name[match.end() :]
        if bare not in templates:
            templates.append(bare)
        if wanted is None or name in wanted:
            kept.add(bare)
    return tuple(bare for bare in templates if bare in kept)


def _serials_ddl(as_name: str) -> str:
    """Return the DDL for the serial-number-to-integer-id mapping table.

    A serial is unique per device rather than globally, so the same pack seen
    by two inverters gets two ids and two histories. That is deliberate: the
    row it identifies records what one inverter's BMS said, and merging two
    inverters' views of one pack under a single id would be the slot mistake
    at a larger scale. Serial is still a plain column, so a caller that wants
    to follow a pack across a swap can join on it.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    id INTEGER PRIMARY KEY,\n"
        f"    {DEVICE} TEXT NOT NULL,\n"
        "    serial TEXT NOT NULL,\n"
        f"    UNIQUE ({DEVICE}, serial)\n"
        ")"
    )


def _invalid_readings_ddl(as_name: str) -> str:
    """Return the DDL for the failed-plausibility-check table.

    Records the raw reading that failed its check, not the scaled integer —
    the check happens on the real-world value before encoding. ``serial`` is
    NULL for an inverter reading and set for a module reading, and ``device``
    names the inverter either way — a decode fault is evidence about one
    inverter or about our scaling of it, and evidence that cannot be
    attributed to a unit is evidence about nothing.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    timestamp INTEGER NOT NULL,\n"
        f"    {DEVICE} TEXT NOT NULL,\n"
        "    metric TEXT NOT NULL,\n"
        "    value REAL,\n"
        "    serial TEXT\n"
        ")"
    )


def _settings_ddl(as_name: str) -> str:
    """Return the DDL for installation-wide settings.

    Settings are part of store initialization even though they carry no
    readings. Creating this table lazily from ``SettingsStore`` made every
    settings read execute schema DDL; request handlers build that lightweight
    reader on the event-loop thread, so a read path was performing a
    lock-taking operation before its SELECT.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    key TEXT NOT NULL PRIMARY KEY,\n"
        "    value TEXT NOT NULL\n"
        f") {_TABLE_OPTIONS}"
    )


def _circuit_ddl(as_name: str) -> str:
    """One row per thing Emporia can report power for.

    Circuits are rows rather than columns, unlike battery modules, because they
    have no natural ceiling: the reference home has 32 across two monitors, an
    apartment might have 8, and a third monitor would make 48. A fixed slot
    count would need a migration the day somebody added a monitor.

    The unique key is the device and channel, never the name. The name belongs
    to the owner and they will change it; keying on it would orphan a year of
    history the first time somebody fixed a typo — the same reasoning that keys
    battery modules by serial rather than by slot.

    This is the one table without WITHOUT ROWID, and it has to be: its key is a
    surrogate id SQLite generates, and generating it *is* the rowid aliasing
    that option removes. With both, the first insert fails outright.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    device_gid INTEGER NOT NULL,\n"
        "    channel_num TEXT NOT NULL,\n"
        "    name TEXT NOT NULL,\n"
        "    multiplier REAL NOT NULL DEFAULT 1.0,\n"
        "    kind TEXT NOT NULL DEFAULT 'circuit',\n"
        # The owner's own category for the circuit, from Emporia's app. Nullable
        # because a clamp nobody has set up has none, and a zero here would be a
        # category rather than the absence of one.
        "    type_gid INTEGER,\n"
        # What contains this circuit, as Emporia's device record states it.
        # Nullable, and the null is the meaningful value: a device nothing else
        # contains is the only kind that may be added to a total. Without it a
        # subpanel's branches, a charger and a smart plug are summed on top of
        # the main-panel clamps already measuring them (#212, #219).
        "    parent_device_gid INTEGER,\n"
        "    parent_channel_num TEXT,\n"
        "    first_seen INTEGER NOT NULL,\n"
        "    last_seen INTEGER NOT NULL,\n"
        "    UNIQUE (device_gid, channel_num)\n"
        f") {_TABLE_OPTIONS_ROWID}"
    )


def _circuit_reading_ddl(as_name: str) -> str:
    """One circuit's power at one moment.

    ``watts`` is nullable and stays NULL when Emporia reported null, which an
    offline device does. Zero would be a claim that the circuit drew nothing,
    and that is a different statement from not having heard.

    The time column is ``timestamp`` like every other table's, and that is not
    only tidiness: the retention engine reads that name directly, so a table
    that spelled it otherwise could not be pruned by the machinery that prunes
    everything else — and this one grows by 56,000 rows a day on a house with
    two monitors.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    timestamp INTEGER NOT NULL,\n"
        "    circuit_id INTEGER NOT NULL,\n"
        "    watts INTEGER,\n"
        "    PRIMARY KEY (timestamp, circuit_id)\n"
        f") {_TABLE_OPTIONS}"
    )


def _circuit_hourly_ddl(as_name: str) -> str:
    """One circuit's average power over one hour.

    The coarse tier the raw readings are pruned into, and the reason they can be
    pruned at all: the retention engine refuses to delete a bucket this table
    does not already cover. A year of raw readings for 39 circuits is around 20
    million rows; the same year here is about 340,000.

    ``watts`` is nullable for the same reason it is in the raw tier — an hour in
    which a circuit reported nothing averages to nothing, and zero would be a
    claim. ``sample_count`` counts *readings*, not rows: an hour built from two
    readings and an hour built from sixty are different figures, and the second
    stage that prices a circuit has to be able to tell them apart. That differs
    deliberately from the module tier, which counts rows because a failed poll
    writes none there.

    ``covered_seconds`` is how much of the hour those readings actually account
    for, and it is the one figure a reader cannot derive. A sample count means
    nothing until something says what one sample covers, and the only moment
    anything knows that is the rollup — which runs on the hourly clock, minutes
    after the readings it summarises, while the poll interval that produced them
    is still the one in force. Read back later under an interval the owner has
    since changed, the same count says something else entirely: 180 samples is
    half an hour at ten seconds and a whole one at sixty, so raising the setting
    doubled the energy of every hour already stored. Measuring once, here, is
    what stops a setting from rewriting history.

    Nullable, and NULL means one specific thing: a row written before this
    column existed. Those cannot be repaired — their raw readings are pruned
    after thirty days, so for most of them the evidence is gone — and a reader
    falls back to the old guess for them, visibly. An hour genuinely holding no
    readings stores 0 rather than NULL, so the two are never confused.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    timestamp INTEGER NOT NULL,\n"
        "    circuit_id INTEGER NOT NULL,\n"
        "    watts INTEGER,\n"
        "    sample_count INTEGER NOT NULL,\n"
        "    covered_seconds INTEGER,\n"
        "    PRIMARY KEY (timestamp, circuit_id)\n"
        f") {_TABLE_OPTIONS}"
    )


def _charger_change_ddl(as_name: str) -> str:
    """Every change this service made to a charge rate, and every one it refused.

    The audit exists because the rate persists. A car found at 6 A in the
    morning is a number with no history attached, and the question that matters
    — did this service do that, and what for — cannot be answered from the
    charger, which remembers only its current value.

    Refused changes are recorded too. "Asked for 40 A, held at the 32 A ceiling"
    and "would have set 20 A, advisory so did not" are both things somebody
    debugging an unexpected rate needs to see; a log of successes only makes a
    module that never acted look identical to one that was never asked.

    ``from_a`` is nullable: the first write after a restart may find a charger
    whose previous rate this service never read. Zero there would be a claim
    that it had been off.

    The key is a surrogate id rather than the timestamp, because an audit has no
    natural key and must never lose a row. Keyed on (timestamp, device_gid) it
    did: stopping and starting a charger within the same second left one line
    where there had been two, which is precisely the record somebody would be
    reading to find out what happened.

    ``source`` is who decided, and it is not a refinement of ``applied``. The
    owner moving the slider is applied — it really did reach the charger — so a
    query asking only what was last applied answered with the owner's own
    number, and restore-on-startup concluded the rate was its own work and undid
    it. Nullable, because a row written before the column existed says who moved
    the rate no more than it says why, and an unknown provenance is not a
    showing that the rate was this service's.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    id INTEGER PRIMARY KEY,\n"
        "    timestamp INTEGER NOT NULL,\n"
        "    device_gid INTEGER NOT NULL,\n"
        "    from_a INTEGER,\n"
        "    to_a INTEGER,\n"
        "    reason TEXT NOT NULL,\n"
        "    applied INTEGER NOT NULL,\n"
        "    source TEXT\n"
        f") {_TABLE_OPTIONS_ROWID}"
    )


def _forecast_ddl(as_name: str) -> str:
    """Return the DDL for the production forecast, which is not a measurement.

    A prediction must never sit in a column a measured reading uses — the rule
    the importer has enforced since day one by refusing to import
    SolarAssistant's predicted series into the metric tables. So the forecast
    is its own table, append-only: every prediction keeps the moment it was
    made. The page draws only the newest of them, so nothing on screen depends
    on the older rows — they are kept because they are the only record of how a
    day's expectation moved, which is what scoring the forecast against what
    actually happened would have to read, and because overwriting is
    unrecoverable while keeping costs a few hundred pruned rows a day.
    Site-level like the weather itself, so it carries no device.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    target_hour INTEGER NOT NULL,\n"
        "    made_at INTEGER NOT NULL,\n"
        "    expected_w INTEGER NOT NULL,\n"
        "    PRIMARY KEY (target_hour, made_at)\n"
        f") {_TABLE_OPTIONS}"
    )


def _rollup_pending_ddl(as_name: str) -> str:
    """Return the DDL for the queue of past hours waiting to be rolled up.

    Maintenance rebuilds the last three hours, which covers every writer that
    stamps its rows at the moment it writes them. One writer does not: the
    archive backfill appends a sample per past hour, up to two years of them,
    and those landed in the raw tier where nothing ever promoted them — the
    efficiency engine reads irradiance from the hourly tier alone. The hour is
    queued as it is written, so the next maintenance pass knows exactly which
    buckets to bring forward instead of scanning a month of raw to find out.

    Deliberately a rowid table, which is why it does not take the shared
    options. A pass deletes the rows it claimed by rowid, so a write landing
    between the pass reading the queue and clearing it makes a new row and
    survives; keyed on the hour instead, that write would be swallowed by the
    delete and the hour would never be promoted at all.
    """
    return f"CREATE TABLE IF NOT EXISTS {as_name} (\n    hour INTEGER NOT NULL\n) STRICT"


def _efficiency_day_ddl(as_name: str) -> str:
    """Return the DDL for the per-day efficiency summary table.

    Site-level like the forecast: one row per string per day, plus a total row
    whose ``string_name`` is the empty string. The performance ratio is stored
    rather than computed on read so a day scored once keeps its score forever,
    and a row carries its config version so a day recomputed after the array
    changes replaces the stale one rather than sitting beside it.

    Curtailed energy is a separate column from unexplained shortfall because the
    two are different things with different remedies — a curtailed hour is the
    inverter protecting the battery, not a fault — and a page that cannot show
    them apart cannot show the owner where to look.
    """
    return (
        f"CREATE TABLE IF NOT EXISTS {as_name} (\n"
        "    day INTEGER NOT NULL,\n"
        "    string_name TEXT NOT NULL,\n"
        "    expected_kwh REAL NOT NULL,\n"
        "    actual_kwh REAL NOT NULL,\n"
        "    curtailed_kwh REAL NOT NULL,\n"
        "    unexplained_kwh REAL NOT NULL,\n"
        "    modelled_hours INTEGER NOT NULL,\n"
        "    partial INTEGER NOT NULL,\n"
        "    pr REAL,\n"
        "    config_version INTEGER NOT NULL,\n"
        "    PRIMARY KEY (day, string_name)\n"
        f") {_TABLE_OPTIONS}"
    )


def _inverter_tier_ddl(tier: Tier, metric_names: tuple[str, ...], as_name: str) -> str:
    """Return the DDL for one inverter-tier wide-row table.

    A wide row costs the same to ask for one metric as for all of them. The
    ``error`` column marks a failed poll and carries its reason; NULL means the
    poll succeeded.

    The key is (timestamp, device): one row per inverter per instant, so two
    units in a parallel stack neither overwrite each other nor need two
    databases. Why timestamp leads, and what that costs, is measured in the
    module docstring.

    Every tier but the raw one is a rollup destination, so it carries
    ``SAMPLE_COUNT``: how many source rows the bucket covers. Without it a
    bucket built from three readings is indistinguishable from one built from
    three hundred once the raw tier is pruned. It is a record of coverage
    only — rollups build directly from raw, never by recombining counts, so
    nothing weights by it. Raw rows are one sample each and carry no such
    column.
    """
    cols = ["    timestamp INTEGER NOT NULL", f"    {DEVICE} TEXT NOT NULL"]
    cols.extend(f"    {name} INTEGER" for name in metric_names)
    if tier.name != "full":
        cols.append(f"    {SAMPLE_COUNT} INTEGER NOT NULL")
    cols.append("    error TEXT")
    cols.append(f"    PRIMARY KEY (timestamp, {DEVICE})")
    return f"CREATE TABLE IF NOT EXISTS {as_name} (\n" + ",\n".join(cols) + f"\n) {_TABLE_OPTIONS}"


def _module_tier_ddl(tier: Tier, metric_names: tuple[str, ...], as_name: str) -> str:
    """Return the DDL for one module-tier normalised table.

    One row per module per timestamp per device: the row references an integer
    id in the serials table, never a slot number. ``device`` is in the key even
    though the serials id already implies it, because a key that has to be
    derived through a join is a key nothing can filter on cheaply — every read
    here narrows by device first.

    Rollup tiers (everything but raw) carry ``SAMPLE_COUNT`` for the same
    reason as the inverter tiers: a bucket must record how many source rows it
    covers. It is a record of coverage only — rollups build directly from raw,
    never by recombining counts, so nothing weights by it.
    """
    cols = ["    timestamp INTEGER NOT NULL", f"    {DEVICE} TEXT NOT NULL"]
    cols.append(f"    module_id INTEGER NOT NULL REFERENCES {SERIALS_TABLE}(id)")
    cols.extend(f"    {name} INTEGER" for name in metric_names)
    if tier.name != "full":
        cols.append(f"    {SAMPLE_COUNT} INTEGER NOT NULL")
    cols.append(f"    PRIMARY KEY (timestamp, {DEVICE}, module_id)")
    return f"CREATE TABLE IF NOT EXISTS {as_name} (\n" + ",\n".join(cols) + f"\n) {_TABLE_OPTIONS}"


def _index_ddl(table: str, columns: str) -> str:
    """Return an idempotent index statement for ``table`` on ``columns``.

    The index name is derived from the table and the columns, with anything
    that is not an identifier character collapsed to an underscore — a comma
    between indexed columns must not leak into the name.
    """
    safe = re.sub(r"[^A-Za-z0-9_]+", "_", columns)
    name = f"idx_{table}_{safe}"
    return f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"


def ddl_for(table: str, as_name: str | None = None, declared: Iterable[str] | None = None) -> str:
    """Return the CREATE TABLE for one table of the schema, under any name.

    Every table's shape is defined once and asked for by name here, so the
    startup DDL and the device migration in ``arraysense.store.migrate`` build
    the same table rather than two that drift. ``as_name`` is what the
    migration needs: it creates the new shape beside the old one under a
    temporary name, copies, and renames — and it leaves ``declared`` unset,
    because a database old enough to need that migration was written when
    every registry metric got a column, and rebuilding it must keep them all.

    ``declared`` narrows the metric columns to what the installed driver
    produces; see the module docstring for what that does and does not govern.

    Raises:
        KeyError: ``table`` is not part of the schema, or ``declared`` names a
            metric the registry does not hold.
    """
    name = table if as_name is None else as_name
    if table == SETTINGS_TABLE:
        return _settings_ddl(name)
    if table == FORECAST_TABLE:
        return _forecast_ddl(name)
    if table == EFFICIENCY_TABLE:
        return _efficiency_day_ddl(name)
    if table == PENDING_TABLE:
        return _rollup_pending_ddl(name)
    if table == CIRCUIT_TABLE:
        return _circuit_ddl(name)
    if table == CIRCUIT_READING_TABLE:
        return _circuit_reading_ddl(name)
    if table == CIRCUIT_HOURLY_TABLE:
        return _circuit_hourly_ddl(name)
    if table == CHARGER_CHANGE_TABLE:
        return _charger_change_ddl(name)
    if table == SERIALS_TABLE:
        return _serials_ddl(name)
    if table == INVALID_TABLE:
        return _invalid_readings_ddl(name)
    for tier in INVERTER_TIERS:
        if tier.table == table:
            return _inverter_tier_ddl(tier, inverter_metric_columns(declared), name)
    for tier in MODULE_TIERS:
        if tier.table == table:
            return _module_tier_ddl(tier, module_metric_columns(declared), name)
    raise KeyError(f"unknown table: {table!r}")


def indexes_for(table: str) -> tuple[str, ...]:
    """Return the index statements belonging to one table.

    Kept beside ``ddl_for`` because a table recreated by the migration has to
    get its indexes back — dropping the old table drops them with it, and a
    tier that came back without them would still answer every query, only
    slowly enough that nobody would connect it to a migration run months
    earlier.
    """
    if table in (CIRCUIT_READING_TABLE, CIRCUIT_HOURLY_TABLE):
        # Every read this module makes is "this circuit, over this window".
        # The primary key leads with timestamp, which serves a time range across
        # all circuits; this reverses it for the per-circuit history the page and
        # the later cost work both ask for.
        return (_index_ddl(table, "circuit_id, timestamp"),)
    if table == INVALID_TABLE:
        # Appending clears a timestamp's stale flags before rewriting them.
        # Without this index that delete scans the whole table, so a sustained
        # decoder fault — precisely what this table is here to preserve — would
        # make every later write slower while holding the single writer lock.
        return (_index_ddl(table, f"timestamp, {DEVICE}, serial"),)
    # Inverter tables need no separate timestamp index: the primary key leads
    # with timestamp and already provides one, and a duplicate index would cost
    # a write on every sample for nothing.
    if any(tier.table == table for tier in MODULE_TIERS):
        # The primary key serves time-range queries. This reverses it to serve
        # "one module over time", which is the per-module history view; the
        # serials id already implies the device, so device is not repeated.
        return (_index_ddl(table, "module_id, timestamp"),)
    return ()


# Every table that carries a device column, in an order safe to create in:
# the module tiers reference the serials table.
DEVICED_TABLES: tuple[str, ...] = (
    SERIALS_TABLE,
    INVALID_TABLE,
    *(tier.table for tier in INVERTER_TIERS),
    *(tier.table for tier in MODULE_TIERS),
)


def expected_columns(declared: Iterable[str] | None = None) -> dict[str, tuple[str, ...]]:
    """Return the metric columns each tier table should have, keyed by table name.

    This is what a live database gets measured against, so one created before a
    metric was added to the registry — or before a rollup tier gained
    ``SAMPLE_COUNT`` — is detected on open and repaired, rather than accepted
    and then failing on the first write. Metric columns come out in registry
    order, with ``SAMPLE_COUNT`` appended for every tier but ``full``.

    ``declared`` narrows the expectation to the installed driver's metrics.
    "Should have" then means "must at least have": a table carrying more —
    the full registry, from before drivers declared subsets — satisfies any
    declaration, because ``migration_ddl`` only asks what is missing.

    Only the columns ``migration_ddl`` can add with an ALTER are here.
    ``device`` is not among them: SQLite cannot ALTER a primary key, so giving
    a database its device identity means recreating every table, which is
    ``arraysense.store.migrate`` and not something to do behind an open.
    """
    inverter = inverter_metric_columns(declared)
    module = module_metric_columns(declared)
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


def migration_ddl(
    existing: dict[str, tuple[str, ...]], declared: Iterable[str] | None = None
) -> tuple[str, ...]:
    """Return ALTER statements adding registry columns an old database lacks.

    ``CREATE TABLE IF NOT EXISTS`` is idempotent only while the schema is
    unchanged: against a database made before a metric was added it does
    nothing, and the first write then fails with "no such column". Since adding
    a metric to the registry is meant to be a one-line change, the gap has to be
    closed on open; a database already current yields nothing to run.

    Only additions are handled, and under a ``declared`` set only declared
    additions: what the driver does not produce is not worth a column, and a
    column the table already has — declared or not — is left exactly as it is.
    A removed or retyped metric needs a considered migration, not an automatic
    one, so those are left alone too. ``SAMPLE_COUNT`` is NOT NULL in the DDL,
    and SQLite rejects a NOT NULL column added to a non-empty table unless it
    carries a default; existing rows take 0, which a later rollup overwrites.

    Args:
        existing: table name to the columns that table currently has, as read
            from the live database.
        declared: the metric names the installed driver produces, or None for
            the whole registry.
    """
    statements: list[str] = []
    for table, wanted in expected_columns(declared).items():
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


# Columns added to a fixed-shape table after that table first shipped, in the
# order they were added. ``CREATE TABLE IF NOT EXISTS`` is idempotent only while
# the shape is unchanged, so against a database that already has the table it
# does nothing at all and the first write fails with "no such column" — the same
# gap ``migration_ddl`` closes for the metric tiers, which cannot be listed here
# because their columns come from the registry rather than from a literal.
#
# Every one has to be nullable or carry a default: SQLite refuses to add a NOT
# NULL column to a table that already has rows.
LATE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    CHARGER_CHANGE_TABLE: (("source", "TEXT"),),
    # How much of each hour the readings behind it account for. Added after the
    # tier shipped, so an installation that has been recording circuits since
    # 1.1.0 has hours without one; those keep NULL and are read with the old
    # guess, because their raw readings are pruned at thirty days and the
    # measurement cannot be made after the fact.
    CIRCUIT_HOURLY_TABLE: (("covered_seconds", "INTEGER"),),
    # Added after the table shipped, so an installation recording circuits since
    # 1.1.0 has rows with neither. They stay NULL until the next sync, which
    # runs every poll and rewrites every circuit it is told about — so the gap
    # closes on its own within one interval rather than needing a migration.
    CIRCUIT_TABLE: (("parent_device_gid", "INTEGER"), ("parent_channel_num", "TEXT")),
}


def late_column_ddl(existing: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Return ALTER statements giving a live database the columns it is missing.

    The counterpart to ``migration_ddl`` for the tables whose shape is written
    out by hand rather than derived from the metric registry. A database already
    current yields nothing to run.
    """
    statements: list[str] = []
    for table, columns in LATE_COLUMNS.items():
        have = set(existing.get(table, ()))
        statements.extend(
            f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}"
            for name, sql_type in columns
            if name not in have
        )
    return tuple(statements)


def schema_ddl(declared: Iterable[str] | None = None) -> str:
    """Return the complete storage schema as executable SQL text.

    Builds every table from the metric registry and the tier definitions, so a
    change in either flows through here untouched — which is the point, since
    the alternative is a hand-written schema that drifts away from the registry
    silently. ``declared`` narrows the metric columns to what the installed
    driver produces, and governs only tables that do not exist yet; the module
    docstring says why an existing table keeps whatever it has. The text is
    idempotent, every statement being CREATE ... IF NOT EXISTS, so it can be
    run on every startup rather than guarded by a "have we set up yet" flag
    that can be wrong.

    Note what idempotent does not cover: a table that already exists with a
    different key — or with more columns than the declaration asks for — is
    left exactly as it is, silently. That is why the device column arrived as
    an explicit migration rather than as an edit here.
    """
    # Settings and the forecast carry no device and are deliberately separate
    # from DEVICED_TABLES, whose members the device-identity migration rebuilds.
    statements: list[str] = [
        ddl_for(SETTINGS_TABLE),
        ddl_for(FORECAST_TABLE),
        ddl_for(EFFICIENCY_TABLE),
        ddl_for(PENDING_TABLE),
        # The Emporia module's tables are created whether or not the module is
        # enabled. An empty table costs nothing and writes nothing, and the
        # alternative is DDL on a request path the first time somebody turns
        # the module on — which is the mistake the settings table already
        # documents here.
        ddl_for(CIRCUIT_TABLE),
        ddl_for(CIRCUIT_READING_TABLE),
        *indexes_for(CIRCUIT_READING_TABLE),
        ddl_for(CIRCUIT_HOURLY_TABLE),
        *indexes_for(CIRCUIT_HOURLY_TABLE),
        ddl_for(CHARGER_CHANGE_TABLE),
    ]
    for table in DEVICED_TABLES:
        statements.append(ddl_for(table, declared=declared))
        statements.extend(indexes_for(table))
    # Each statement ends on its own line; a trailing semicolon per statement
    # makes the whole text executable as a single script.
    return ";\n".join(statements) + ";\n"
