"""Tests for the storage schema: arraysense.store.schema.

The schema is DDL text generated from the metric registry. No database is
opened in production code; the only database here is the in-memory SQLite
connection in the execution test, which proves the DDL is valid SQL.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from arraysense.metrics import BATTERY_MODULE_METRICS, INVERTER_METRICS
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


def _table_columns(ddl: str, table: str) -> set[str]:
    """Return the column names of ``table`` as declared in ``ddl``.

    Parses the generated DDL rather than trusting a helper, so the test asserts
    what the text actually contains.
    """
    match = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\((.*?)\n\)",
        ddl,
        re.DOTALL,
    )
    assert match is not None, f"{table} table missing from DDL"
    return {line.strip().split()[0] for line in match.group(1).splitlines() if line.strip()}


def _all_table_columns(ddl: str, tables: set[str]) -> set[str]:
    """Union of the column names of every table in ``tables``."""
    cols: set[str] = set()
    for table in tables:
        cols |= _table_columns(ddl, table)
    return cols


def _inverter_table_names() -> set[str]:
    return {tier.table for tier in INVERTER_TIERS}


def _module_table_names() -> set[str]:
    return {tier.table for tier in MODULE_TIERS}


def _inverter_metric_names() -> set[str]:
    return {spec.name for spec in INVERTER_METRICS}


def _bare_module_metric_names() -> set[str]:
    # Module tables store the shared metric template, not the per-slot
    # expansion: soc_pct, never battery_module1_soc_pct.
    return {re.sub(r"^battery_module\d+_", "", spec.name) for spec in BATTERY_MODULE_METRICS}


def test_every_tier_appears_in_ddl() -> None:
    ddl = schema_ddl()
    for tier in (*INVERTER_TIERS, *MODULE_TIERS):
        assert f"CREATE TABLE IF NOT EXISTS {tier.table}" in ddl


def test_inverter_metrics_have_columns_in_inverter_tables() -> None:
    ddl = schema_ddl()
    for table in _inverter_table_names():
        cols = _table_columns(ddl, table)
        for name in _inverter_metric_names():
            assert name in cols


def test_module_metrics_have_columns_named_without_slot() -> None:
    ddl = schema_ddl()
    module_cols = _all_table_columns(ddl, _module_table_names())
    for name in _bare_module_metric_names():
        assert name in module_cols
        assert not re.search(r"battery_module\d", name)


def test_no_metric_in_the_wrong_table() -> None:
    ddl = schema_ddl()
    module_cols = _all_table_columns(ddl, _module_table_names())
    inverter_cols = _all_table_columns(ddl, _inverter_table_names())
    for name in _inverter_metric_names():
        assert name not in module_cols
    for name in _bare_module_metric_names():
        assert name not in inverter_cols


def test_serials_and_failed_readings_tables_present() -> None:
    ddl = schema_ddl()
    assert "CREATE TABLE IF NOT EXISTS serials" in ddl
    assert "CREATE TABLE IF NOT EXISTS invalid_readings" in ddl


def test_inverter_tables_mark_failed_polls() -> None:
    # A failed poll is data, not a hole: it carries its reason in the row.
    ddl = schema_ddl()
    for table in _inverter_table_names():
        assert "error" in _table_columns(ddl, table)


def test_module_tables_reference_a_module() -> None:
    # Modules are normalised: the row references an integer id, not a slot.
    ddl = schema_ddl()
    for table in _module_table_names():
        assert "module_id" in _table_columns(ddl, table)


def test_ddl_runs_twice_safely() -> None:
    ddl = schema_ddl()
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)
    conn.executescript(ddl)  # second run must not raise
    conn.close()


def test_executing_ddl_creates_expected_tables() -> None:
    ddl = schema_ddl()
    conn = sqlite3.connect(":memory:")
    conn.executescript(ddl)
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    tables = {row[0] for row in rows}
    expected = _inverter_table_names() | _module_table_names() | {"serials", "invalid_readings"}
    assert tables == expected
    conn.close()


def test_inverter_tiers_reject_duplicate_timestamps() -> None:
    # Without a primary key a collector retry would double-write a sample and
    # the rollups would then double-count it into a corrupted average.
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_ddl())
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) VALUES (100, 'CE0', 5)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) VALUES (100, 'CE0', 9)"
        )
    conn.close()


def test_module_tiers_reject_duplicate_timestamp_and_module() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_ddl())
    conn.execute("INSERT INTO serials (id, device, serial) VALUES (1, 'CE0', 'BA00000001')")
    conn.execute(
        "INSERT INTO module_raw (timestamp, device, module_id, soc_pct) VALUES (100, 'CE0', 1, 94)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO module_raw (timestamp, device, module_id, soc_pct) "
            "VALUES (100, 'CE0', 1, 93)"
        )
    conn.close()


def test_same_timestamp_different_modules_is_allowed() -> None:
    # Four modules report at the same instant; only (timestamp, module) is unique.
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_ddl())
    for i in range(1, 5):
        conn.execute(
            "INSERT INTO serials (id, device, serial) VALUES (?, 'CE0', ?)", (i, f"BA0000000{i}")
        )
        conn.execute(
            "INSERT INTO module_raw (timestamp, device, module_id, soc_pct) "
            "VALUES (100, 'CE0', ?, ?)",
            (i, 90 + i),
        )
    assert conn.execute("SELECT COUNT(*) FROM module_raw WHERE timestamp=100").fetchone()[0] == 4
    conn.close()


def _open(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute(FOREIGN_KEYS_PRAGMA)
    conn.executescript(schema_ddl())
    return conn


def test_null_timestamp_is_rejected() -> None:
    # INTEGER PRIMARY KEY aliases the rowid and would silently assign one.
    conn = _open()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) VALUES (NULL, 'CE0', 5)"
        )
    conn.close()


def test_non_integer_values_are_rejected() -> None:
    # Column types are affinities, not constraints, unless the table is STRICT.
    conn = _open()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO inverter_raw (timestamp, pv_total_power_w) VALUES (1, 'abc')")
    with pytest.raises(sqlite3.IntegrityError):
        # A rollup average must be rounded before storage, not written as a real.
        conn.execute("INSERT INTO inverter_raw (timestamp, pv_total_power_w) VALUES (2, 5.5)")
    conn.close()


def test_orphan_module_reference_is_rejected() -> None:
    # SQLite leaves foreign keys off per connection; an orphan module_id would
    # detach readings from any battery.
    conn = _open()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO module_raw (timestamp, module_id, soc_pct) VALUES (1, 999, 50)")
    conn.close()


def test_migration_adds_a_metric_missing_from_an_old_database() -> None:
    # Adding a metric to the registry is meant to be a one-line change; a
    # database made before that must gain the column rather than fail on write.
    dropped = "pv_total_power_w"
    existing = {t: tuple(c for c in cols if c != dropped) for t, cols in expected_columns().items()}
    stmts = migration_ddl(existing)
    assert any("ADD COLUMN pv_total_power_w" in s for s in stmts)
    assert all(s.startswith("ALTER TABLE ") for s in stmts)


def test_migration_is_empty_for_a_current_database() -> None:
    assert migration_ddl(expected_columns()) == ()


def test_migration_statements_execute() -> None:
    conn = _open()
    conn.execute("ALTER TABLE inverter_raw DROP COLUMN pv_total_power_w")
    existing = {
        t: tuple(r[1] for r in conn.execute(f"PRAGMA table_info({t})")) for t in expected_columns()
    }
    for stmt in migration_ddl(existing):
        conn.execute(stmt)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(inverter_raw)")}
    assert "pv_total_power_w" in cols
    conn.close()


def test_migration_adds_sample_count_to_a_rollup_table() -> None:
    # A database created before sample_count existed must gain the NOT NULL
    # column — with a default, since SQLite rejects a NOT NULL column added to
    # a non-empty table without one — rather than fail on the first rollup.
    conn = _open()
    conn.execute("ALTER TABLE inverter_minute DROP COLUMN sample_count")
    existing = {
        t: tuple(r[1] for r in conn.execute(f"PRAGMA table_info({t})")) for t in expected_columns()
    }
    stmts = migration_ddl(existing)
    assert any("ADD COLUMN sample_count INTEGER NOT NULL DEFAULT 0" in s for s in stmts)
    for stmt in stmts:
        conn.execute(stmt)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(inverter_minute)")}
    assert "sample_count" in cols
    conn.close()


def test_invalid_readings_delete_predicate_is_indexed() -> None:
    # Appending clears a timestamp's stale flags first; unindexed that is a full
    # scan, and a sustained decoder fault would slow every later write.
    conn = _open()
    plan = conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM invalid_readings WHERE timestamp = 1 AND serial IS NULL"
    ).fetchall()
    assert any("USING INDEX" in str(row[-1]) for row in plan), plan
    conn.close()


def test_rollup_tiers_carry_sample_count() -> None:
    # A rollup bucket must record how many source rows it covers, or a bucket
    # built from three readings is indistinguishable from one built from three
    # hundred once the raw tier is pruned.
    ddl = schema_ddl()
    for tier in (*INVERTER_TIERS, *MODULE_TIERS):
        if tier.name == "full":
            continue
        assert "sample_count" in _table_columns(ddl, tier.table)


def test_raw_tiers_have_no_sample_count() -> None:
    # Raw rows are one sample each; the count column belongs only to rollups.
    ddl = schema_ddl()
    for tier in (*INVERTER_TIERS, *MODULE_TIERS):
        if tier.name == "full":
            assert "sample_count" not in _table_columns(ddl, tier.table)


def test_rollup_rows_require_sample_count() -> None:
    # The count is a real invariant, not an optional column: a rollup row with
    # no count would make a gap look like a full bucket.
    conn = _open()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO inverter_minute (timestamp, pv_total_power_w) VALUES (60, 5)")
    conn.close()


# --- schemas narrowed to what a driver declares ------------------------------
#
# The registry says what a metric *is*; a driver says which of them its device
# produces. A schema generated from the whole registry gives a one-string
# inverter a column for a third string it does not have, and a NULL there means
# two different things at once. These tests pin the narrowing — and pin that an
# existing database, whose tables may carry the full column set, is left alone.


def test_declared_set_narrows_new_inverter_tables() -> None:
    declared = frozenset({"pv_total_power_w", "battery_soc_pct"})
    ddl = schema_ddl(declared)
    for table in _inverter_table_names():
        cols = _table_columns(ddl, table)
        assert "pv_total_power_w" in cols
        assert "battery_soc_pct" in cols
        assert "pv1_power_w" not in cols


def test_declared_set_narrows_module_tables() -> None:
    # Any slot's expansion declares the template: the module tables hold one
    # bare column per template, never one per slot.
    declared = frozenset({"battery_module2_soc_pct"})
    ddl = schema_ddl(declared)
    for table in _module_table_names():
        cols = _table_columns(ddl, table)
        assert "soc_pct" in cols
        assert "voltage_v" not in cols


def test_battery_module_count_is_an_inverter_metric_not_a_template() -> None:
    # The one registry name that starts with "battery_module" without being a
    # per-slot expansion. A prefix test wrongly files it with the module
    # templates; only the slot-number pattern splits correctly.
    declared = frozenset({"battery_module_count"})
    assert inverter_metric_columns(declared) == ("battery_module_count",)
    assert module_metric_columns(declared) == ()


def test_no_declared_set_means_the_whole_registry() -> None:
    # The default is every metric, which is what every caller before drivers
    # declared subsets relied on — and what keeps the device migration
    # rebuilding pre-declaration databases in their original full shape.
    assert inverter_metric_columns() == tuple(spec.name for spec in INVERTER_METRICS)
    all_names = frozenset(spec.name for spec in INVERTER_METRICS + BATTERY_MODULE_METRICS)
    assert schema_ddl() == schema_ddl(all_names)


def test_two_drivers_declaring_different_subsets_produce_two_schemas() -> None:
    from arraysense.drivers import eg4_luxpower, fake

    a = schema_ddl(eg4_luxpower.CAPABILITIES.metrics)
    b = schema_ddl(fake.CAPABILITIES.metrics)
    assert a != b
    # Concretely: the fake reports no per-string readings, the real driver does.
    assert "pv1_power_w" in _table_columns(a, "inverter_raw")
    assert "pv1_power_w" not in _table_columns(b, "inverter_raw")
    # Neither driver declares the per-module fault codes: pylxpweb's
    # inverter-register path — the one the eg4 driver reads — never fills
    # them (its direct-BMS transports do, but nothing here speaks those), so
    # neither schema carries a column that could only ever hold an asserted
    # "no fault" about a pack nobody asked.
    assert "fault_code" not in _table_columns(a, "module_raw")
    assert "fault_code" not in _table_columns(b, "module_raw")
    assert "status_code" in _table_columns(a, "module_raw")
    assert "status_code" not in _table_columns(b, "module_raw")


def test_a_declared_name_outside_the_registry_fails_loudly() -> None:
    with pytest.raises(KeyError, match="pv9_flux_capacitance"):
        schema_ddl(frozenset({"pv_total_power_w", "pv9_flux_capacitance"}))


def test_narrowed_ddl_leaves_an_existing_full_table_alone() -> None:
    # CREATE IF NOT EXISTS is the whole upgrade contract: a database created
    # before drivers declared subsets has every registry column, and a narrower
    # declaration must not drop, rebuild or otherwise touch what exists.
    conn = sqlite3.connect(":memory:")
    conn.executescript(schema_ddl())
    before = {r[1] for r in conn.execute("PRAGMA table_info(inverter_raw)")}
    conn.executescript(schema_ddl(frozenset({"pv_total_power_w"})))
    after = {r[1] for r in conn.execute("PRAGMA table_info(inverter_raw)")}
    conn.close()
    assert after == before


def test_migration_over_a_full_database_under_a_narrow_set_adds_nothing() -> None:
    # The declared set governs what must exist, never what must not: a full
    # column set already covers any subset, so there is nothing to run.
    existing = expected_columns()
    declared = frozenset({"pv_total_power_w", "battery_module1_soc_pct"})
    assert migration_ddl(existing, declared) == ()


def test_migration_adds_only_declared_missing_columns() -> None:
    declared = frozenset(
        {
            "pv_total_power_w",
            "battery_soc_pct",
            "battery_module1_soc_pct",
            "battery_module1_voltage_v",
        }
    )
    existing: dict[str, tuple[str, ...]] = {
        table: ("timestamp", "device", "pv_total_power_w", "soc_pct", "error")
        for table in expected_columns(declared)
    }
    statements = migration_ddl(existing, declared)
    added = {s.split(" ADD COLUMN ")[1].split()[0] for s in statements}
    # battery_soc_pct and voltage_v are declared and missing; sample_count is
    # structural on the rollup tiers. Nothing undeclared is added.
    assert added == {"battery_soc_pct", "voltage_v", "sample_count"}
    assert not any("pv1_power_w" in s for s in statements)
