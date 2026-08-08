"""test_migrate.py — the device migration, against databases built the old way.

Every test here starts from a database with the pre-device schema, built from
the registry rather than from a checked-in dump so it cannot drift out of date
as metrics are added. That is the only shape that matters: the migration
exists for the one database that already holds months of imported
SolarAssistant history, and that history cannot be reproduced.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arraysense.metrics import INVERTER_METRICS
from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.migrate import migrate_devices, needs_device_migration
from arraysense.store.schema import (
    INVERTER_TIERS,
    MODULE_TIERS,
    SAMPLE_COUNT,
    module_metric_columns,
)
from arraysense.store.sqlite_store import SqliteStore

DEVICE = "CE12345678"


def _legacy_schema() -> str:
    """Return the schema as it stood before readings had a device.

    Generated from the registry, so a metric added later still appears here and
    the migration is exercised against the same column set the live database
    has. Only the keys and the two identity tables differ from today's DDL.
    """
    inverter = tuple(spec.name for spec in INVERTER_METRICS)
    module = module_metric_columns()
    statements = [
        "CREATE TABLE serials (id INTEGER PRIMARY KEY, serial TEXT NOT NULL UNIQUE)",
        "CREATE TABLE invalid_readings ("
        "timestamp INTEGER NOT NULL, metric TEXT NOT NULL, value REAL, serial TEXT)",
        "CREATE INDEX idx_invalid_readings_timestamp_serial "
        "ON invalid_readings (timestamp, serial)",
    ]
    for tier in INVERTER_TIERS:
        cols = ["timestamp INTEGER NOT NULL"]
        cols += [f"{name} INTEGER" for name in inverter]
        if tier.name != "full":
            cols.append(f"{SAMPLE_COUNT} INTEGER NOT NULL")
        cols += ["error TEXT", "PRIMARY KEY (timestamp)"]
        statements.append(f"CREATE TABLE {tier.table} ({', '.join(cols)}) STRICT, WITHOUT ROWID")
    for tier in MODULE_TIERS:
        cols = [
            "timestamp INTEGER NOT NULL",
            "module_id INTEGER NOT NULL REFERENCES serials(id)",
        ]
        cols += [f"{name} INTEGER" for name in module]
        if tier.name != "full":
            cols.append(f"{SAMPLE_COUNT} INTEGER NOT NULL")
        cols.append("PRIMARY KEY (timestamp, module_id)")
        statements.append(f"CREATE TABLE {tier.table} ({', '.join(cols)}) STRICT, WITHOUT ROWID")
        statements.append(
            f"CREATE INDEX idx_{tier.table}_module_id_timestamp "
            f"ON {tier.table} (module_id, timestamp)"
        )
    return ";\n".join(statements) + ";\n"


def _legacy_db(path: Path, rows: int = 50, serials: tuple[str, ...] = ("BM01", "BM02")) -> None:
    """Build a database with the old schema and fill every table."""
    conn = sqlite3.connect(path)
    conn.executescript(_legacy_schema())
    for index, serial in enumerate(serials, start=1):
        conn.execute("INSERT INTO serials (id, serial) VALUES (?, ?)", (index, serial))
    for tier, step in ((INVERTER_TIERS[0], 11), (INVERTER_TIERS[1], 60), (INVERTER_TIERS[2], 3600)):
        counted = tier.name != "full"
        cols = ["timestamp", "pv_total_power_w", "battery_voltage_v"]
        if counted:
            cols.append(SAMPLE_COUNT)
        marks = ",".join("?" * len(cols))
        for i in range(rows):
            values: list[object] = [1_700_000_000 + i * step, 5000 + i, 5190]
            if counted:
                values.append(5)
            conn.execute(f"INSERT INTO {tier.table} ({','.join(cols)}) VALUES ({marks})", values)
    for tier, step in ((MODULE_TIERS[0], 11), (MODULE_TIERS[1], 3600)):
        counted = tier.name != "full"
        cols = ["timestamp", "module_id", "soc_pct"]
        if counted:
            cols.append(SAMPLE_COUNT)
        marks = ",".join("?" * len(cols))
        for i in range(rows):
            for module_id in range(1, len(serials) + 1):
                values = [1_700_000_000 + i * step, module_id, 500 + module_id]
                if counted:
                    values.append(5)
                conn.execute(
                    f"INSERT INTO {tier.table} ({','.join(cols)}) VALUES ({marks})", values
                )
    conn.execute(
        "INSERT INTO invalid_readings (timestamp, metric, value, serial) VALUES (?,?,?,?)",
        (1_700_000_000, "battery_power_w", 25583.0, None),
    )
    conn.execute(
        "INSERT INTO invalid_readings (timestamp, metric, value, serial) VALUES (?,?,?,?)",
        (1_700_000_000, "battery_module1_soc_pct", 250.0, "BM01"),
    )
    conn.commit()
    conn.close()


def _count(path: Path, table: str) -> int:
    conn = sqlite3.connect(path)
    try:
        result: int = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return result
    finally:
        conn.close()


def test_legacy_database_is_detected_as_needing_migration(tmp_path: Path) -> None:
    """A database written before the change reports that it needs migrating."""
    path = tmp_path / "old.db"
    _legacy_db(path)
    assert needs_device_migration(str(path)) is True


def test_fresh_database_needs_no_migration(tmp_path: Path) -> None:
    """A store created today already has the device column everywhere."""
    path = tmp_path / "new.db"
    SqliteStore(str(path), device=DEVICE).close()
    assert needs_device_migration(str(path)) is False


def test_absent_database_needs_no_migration(tmp_path: Path) -> None:
    """A path with no file is a first install, not a migration.

    Checking has to be safe to do before the store exists, and sqlite3 creates
    the file it cannot find — so a check that connected would leave an empty
    database behind and report it clean.
    """
    assert needs_device_migration(str(tmp_path / "missing.db")) is False
    assert not (tmp_path / "missing.db").exists()


def test_every_row_survives_with_the_configured_device(tmp_path: Path) -> None:
    """Nothing is dropped, and everything carries the inverter's serial."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=200)
    before = {
        table: _count(path, table)
        for table in ("inverter_raw", "inverter_minute", "inverter_hourly", "module_raw")
    }

    report = migrate_devices(str(path), DEVICE)

    conn = sqlite3.connect(path)
    for table, count in before.items():
        assert _count(path, table) == count
        assert report.rows[table] == count
        distinct = conn.execute(f"SELECT DISTINCT device FROM {table}").fetchall()
        assert distinct == [(DEVICE,)]
    conn.close()


def test_readings_keep_their_values(tmp_path: Path) -> None:
    """The migration moves rows; it must not touch what they say."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=20)
    conn = sqlite3.connect(path)
    before = conn.execute(
        "SELECT timestamp, pv_total_power_w, battery_voltage_v FROM inverter_raw ORDER BY timestamp"
    ).fetchall()
    conn.close()

    migrate_devices(str(path), DEVICE)

    conn = sqlite3.connect(path)
    after = conn.execute(
        "SELECT timestamp, pv_total_power_w, battery_voltage_v FROM inverter_raw ORDER BY timestamp"
    ).fetchall()
    conn.close()
    assert after == before


def test_module_rows_still_point_at_their_own_serial(tmp_path: Path) -> None:
    """Serial ids are preserved, so no pack inherits another pack's history."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=5, serials=("BM01", "BM02", "BM03"))
    conn = sqlite3.connect(path)
    before = conn.execute(
        "SELECT m.timestamp, s.serial, m.soc_pct FROM module_raw m "
        "JOIN serials s ON s.id = m.module_id ORDER BY m.timestamp, s.serial"
    ).fetchall()
    conn.close()

    migrate_devices(str(path), DEVICE)

    conn = sqlite3.connect(path)
    after = conn.execute(
        "SELECT m.timestamp, s.serial, m.soc_pct FROM module_raw m "
        "JOIN serials s ON s.id = m.module_id ORDER BY m.timestamp, s.serial"
    ).fetchall()
    assert after == before
    assert conn.execute("SELECT DISTINCT device FROM serials").fetchall() == [(DEVICE,)]
    conn.close()


def test_flagged_readings_are_carried_across(tmp_path: Path) -> None:
    """A recorded decode fault is evidence and must not be dropped by a migration."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=5)

    report = migrate_devices(str(path), DEVICE)

    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT device, metric, value, serial FROM invalid_readings ORDER BY metric"
    ).fetchall()
    conn.close()
    assert rows == [
        (DEVICE, "battery_module1_soc_pct", 250.0, "BM01"),
        (DEVICE, "battery_power_w", 25583.0, None),
    ]
    assert report.rows["invalid_readings"] == 2


def test_running_it_twice_is_harmless(tmp_path: Path) -> None:
    """The second run finds nothing to do and changes nothing."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=30)
    first = migrate_devices(str(path), DEVICE)
    conn = sqlite3.connect(path)
    snapshot = conn.execute("SELECT * FROM inverter_raw ORDER BY timestamp").fetchall()
    conn.close()

    second = migrate_devices(str(path), DEVICE)

    assert first.already_migrated is False
    assert second.already_migrated is True
    assert second.rows == {}
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT * FROM inverter_raw ORDER BY timestamp").fetchall() == snapshot
    conn.close()


def test_a_second_run_with_a_different_serial_does_not_restamp(tmp_path: Path) -> None:
    """Once stamped, rows keep the identity they were given.

    A migration that re-stamped on every run would hand one inverter's whole
    history to another the first time somebody corrected a typo in the config,
    which is exactly what identifying by serial exists to prevent.
    """
    path = tmp_path / "old.db"
    _legacy_db(path, rows=5)
    migrate_devices(str(path), DEVICE)

    migrate_devices(str(path), "CE99999999")

    conn = sqlite3.connect(path)
    assert conn.execute("SELECT DISTINCT device FROM inverter_raw").fetchall() == [(DEVICE,)]
    conn.close()


def test_an_interrupted_migration_leaves_the_old_database_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process that dies mid-copy must lose nothing.

    The whole thing runs in one transaction, so the failure below stands in for
    a power cut: the tables that were already rewritten roll back with the one
    that failed, and every original row is still readable afterwards.
    """
    path = tmp_path / "old.db"
    _legacy_db(path, rows=40)
    before = _count(path, "inverter_raw")

    import arraysense.store.migrate as migrate_module

    real_copy = migrate_module._copy_table
    calls = {"n": 0}

    def explode(*args: object, **kwargs: object) -> int:
        calls["n"] += 1
        if calls["n"] > 2:
            raise sqlite3.OperationalError("disk I/O error")
        return int(real_copy(*args, **kwargs))  # type: ignore[arg-type]

    monkeypatch.setattr(migrate_module, "_copy_table", explode)
    with pytest.raises(sqlite3.OperationalError):
        migrate_devices(str(path), DEVICE)

    assert needs_device_migration(str(path)) is True
    assert _count(path, "inverter_raw") == before
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM serials").fetchone()[0] == 2
    conn.close()

    # And it is still migratable afterwards, which is the point of rolling back
    # rather than leaving half a schema behind.
    monkeypatch.setattr(migrate_module, "_copy_table", real_copy)
    report = migrate_devices(str(path), DEVICE)
    assert report.rows["inverter_raw"] == before


def test_a_migrated_database_reads_back_through_the_store(tmp_path: Path) -> None:
    """The end of the exercise: the old rows come back out of the new store."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=10)
    migrate_devices(str(path), DEVICE)

    store = SqliteStore(str(path), device=DEVICE)
    rows = store.query(
        ["pv_total_power_w"],
        datetime.fromtimestamp(1_700_000_000, tz=UTC),
        datetime.fromtimestamp(1_700_000_000 + 200, tz=UTC),
    )
    assert [r["pv_total_power_w"] for r in rows] == [float(5000 + i) for i in range(10)]
    modules = store.latest_modules(["soc_pct"])
    assert sorted(str(m["serial"]) for m in modules) == ["BM01", "BM02"]
    store.close()


def test_a_migrated_database_still_accepts_writes(tmp_path: Path) -> None:
    """Migration is not a museum: the collector has to carry on into it."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=5)
    migrate_devices(str(path), DEVICE)

    store = SqliteStore(str(path), device=DEVICE)
    when = datetime.fromtimestamp(1_700_009_999, tz=UTC)
    store.append(
        Sample(
            timestamp=when,
            readings={"pv_total_power_w": 7600.0},
            battery_modules=(BatteryModuleSample(serial="BM01", slot=1, soc_pct=55.0),),
        )
    )
    latest = store.latest(["pv_total_power_w"])
    assert latest is not None
    assert latest["pv_total_power_w"] == 7600.0
    assert _count(path, "serials") == 2
    store.close()


def test_migrating_a_path_with_no_database_creates_nothing(tmp_path: Path) -> None:
    """A fresh install has nothing to migrate and must be left with nothing."""
    path = tmp_path / "not-there.db"
    report = migrate_devices(str(path), DEVICE)
    assert report.already_migrated is True
    assert not path.exists()


def test_a_blank_device_is_refused(tmp_path: Path) -> None:
    """A row stamped with an empty identity looks attributed and is not."""
    path = tmp_path / "old.db"
    _legacy_db(path, rows=2)
    with pytest.raises(ValueError, match="device"):
        migrate_devices(str(path), "  ")
    assert needs_device_migration(str(path)) is True


def test_a_database_missing_a_registry_column_still_migrates(tmp_path: Path) -> None:
    """One written before a metric existed keeps its rows and gains a NULL column.

    The live database has been through several registry additions, and the
    column-adding migration runs when the store opens — which is after this. So
    this has to copy what is there rather than what the registry says is there.
    """
    path = tmp_path / "old.db"
    _legacy_db(path, rows=10)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE inverter_raw DROP COLUMN pv3_power_w")
    conn.commit()
    conn.close()

    report = migrate_devices(str(path), DEVICE)

    assert report.rows["inverter_raw"] == 10
    conn = sqlite3.connect(path)
    values = conn.execute("SELECT pv3_power_w FROM inverter_raw").fetchall()
    conn.close()
    assert values == [(None,)] * 10


def test_a_migrated_database_has_the_schema_a_fresh_one_would(tmp_path: Path) -> None:
    """The end state must be indistinguishable from a database created today.

    Not a tidiness check. A migrated tier that came back with a subtly
    different key, or without the index the old table carried, would work for
    months and then be found by somebody comparing two installations.
    """
    old = tmp_path / "old.db"
    _legacy_db(old, rows=3)
    migrate_devices(str(old), DEVICE)
    SqliteStore(str(old), device=DEVICE).close()
    SqliteStore(str(tmp_path / "fresh.db"), device=DEVICE).close()

    def objects(path: Path) -> set[tuple[str, str, str]]:
        # Two textual differences are SQLite's and mean nothing: a renamed
        # table comes back with its name quoted, and without the
        # "IF NOT EXISTS" the DDL was written with. Everything else has to
        # match character for character.
        conn = sqlite3.connect(path)
        try:
            return {
                (
                    str(t),
                    str(n),
                    " ".join(str(s).split())
                    .replace("IF NOT EXISTS ", "")
                    .replace(f'"{n}"', str(n)),
                )
                for t, n, s in conn.execute(
                    "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL"
                )
            }
        finally:
            conn.close()

    assert objects(old) == objects(tmp_path / "fresh.db")


def test_a_column_the_schema_no_longer_has_stops_the_migration(tmp_path: Path) -> None:
    """A column we would silently drop aborts, because no row count can see it.

    Every row survives a copy that omits one column, so the counted-copy check
    passes and the run reports a success it did not achieve. Refusing is the
    recoverable outcome: put the metric back, or drop the column on purpose.
    """
    db = tmp_path / "legacy.db"
    _legacy_db(db)
    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE inverter_raw ADD COLUMN a_retired_metric_w INTEGER")
    conn.commit()
    conn.close()

    with pytest.raises(ValueError, match="a_retired_metric_w"):
        migrate_devices(str(db), DEVICE)

    # And it rolled back rather than half-migrating.
    assert needs_device_migration(str(db))
