"""Tests for the out-of-bounds scrub: tools/scrub_out_of_bounds.py.

This edits a live database that the collector is writing to, and the values it
edits are gone once it has run. So the cases below are about the two ways it
could do harm — touching a reading that was fine, and turning an absence into a
zero — rather than about whether it finds the obvious offender.

The numbers come from the fault that prompted it: five rows written on
2026-08-07 by a build whose registry scaled volts by 1000 where it now scales by
10, so a 245.0 V grid reading was stored as 245000 and decodes as 24,500 V.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arraysense.metrics import INVERTER_METRICS, lookup
from arraysense.store.sqlite_store import SqliteStore
from scrub_out_of_bounds import Column, clear, columns_to_check, find, stored_bounds

# The instant of the first bad row, and the integer an old registry stored for a
# 245.0 V grid reading when volts scaled by 1000.
BAD_AT = 1786061378
LEGACY_VOLTS = 245000


def _database(tmp_path: Path) -> Path:
    """Create an empty database with the current schema, and close the store."""
    path = tmp_path / "arraysense.db"
    store = SqliteStore(str(path))
    store.close()
    return path


def _row(conn: sqlite3.Connection, table: str, timestamp: int, column: str) -> object:
    """Read one stored integer straight out of a tier table."""
    row = conn.execute(f"SELECT {column} FROM {table} WHERE timestamp = ?", (timestamp,)).fetchone()
    assert row is not None
    return row[0]


def test_a_reading_stored_at_a_legacy_scale_is_found_and_reported_decoded(tmp_path: Path) -> None:
    # Nothing flagged these on the way in: 245.0 V is a perfectly plausible grid
    # reading, and the corruption happened in the encode step. The only way to
    # see it is to decode what is stored and check that.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v) VALUES (?, ?)",
        (BAD_AT, LEGACY_VOLTS),
    )
    conn.commit()

    found = find(conn)

    assert len(found) == 1
    assert found[0].column.column == "grid_voltage_v"
    assert found[0].column.table == "inverter_raw"
    assert found[0].timestamp == BAD_AT
    assert found[0].decoded == 24500.0


def test_a_reading_inside_its_bounds_is_never_touched(tmp_path: Path) -> None:
    # The same reading at the corrected scale. Clearing must be able to run over
    # a database full of these and change nothing.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v, battery_voltage_v, pv1_voltage_v) "
        "VALUES (?, ?, ?, ?)",
        (BAD_AT, 2450, 530, 2153),
    )
    conn.commit()

    assert find(conn) == []
    assert clear(conn, find(conn)) == 0
    assert _row(conn, "inverter_raw", BAD_AT, "grid_voltage_v") == 2450


def test_no_registered_bound_is_itself_reported_as_impossible(tmp_path: Path) -> None:
    # The bounds are inclusive and several of them are load-bearing at exactly
    # that edge: a fully charged cell sits on 4.2 V, and during a power cut the
    # grid genuinely reads 0 V. The search narrows on integers computed from the
    # bound and then re-checks the decoded value, and the two have to agree at
    # the edge or the tool offers to erase the extremes the registry admits.
    conn = sqlite3.connect(_database(tmp_path))
    for offset, edge in enumerate(("lower", "upper")):
        specs = [(s.name, s.encode(getattr(s, edge))) for s in INVERTER_METRICS]
        names = ", ".join(name for name, _ in specs)
        holes = ", ".join("?" for _ in specs)
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, {names}) VALUES (?, {holes})",
            (BAD_AT + offset, *(value for _, value in specs)),
        )
    conn.commit()

    assert find(conn) == []


def test_one_step_past_a_bound_is_reported(tmp_path: Path) -> None:
    spec = lookup("battery_max_cell_voltage_v")
    assert stored_bounds(spec) == (2000, 4200)

    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, battery_max_cell_voltage_v) VALUES (?, ?)",
        (BAD_AT, 4201),
    )
    conn.commit()

    found = find(conn)
    assert [(o.column.column, o.decoded) for o in found] == [("battery_max_cell_voltage_v", 4.201)]


def test_a_grid_voltage_of_zero_is_kept_because_an_outage_reads_zero(tmp_path: Path) -> None:
    # The registry floors grid voltage at zero on purpose: during a power cut
    # the inverter genuinely measures 0 V, and that is the event the owner most
    # wants recorded.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute("INSERT INTO inverter_raw (timestamp, grid_voltage_v) VALUES (?, 0)", (BAD_AT,))
    conn.commit()

    assert find(conn) == []


def test_an_absent_reading_is_not_an_offender(tmp_path: Path) -> None:
    # A NULL is a reading the inverter never reported. It is neither out of
    # bounds nor a zero, and a predicate that coalesced it to zero would report
    # every unreported metric in the database as a fault.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute("INSERT INTO inverter_raw (timestamp) VALUES (?)", (BAD_AT,))
    conn.commit()

    assert find(conn) == []


def test_clearing_writes_null_and_never_zero(tmp_path: Path) -> None:
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v) VALUES (?, ?)",
        (BAD_AT, LEGACY_VOLTS),
    )
    conn.commit()

    assert clear(conn, find(conn)) == 1
    assert _row(conn, "inverter_raw", BAD_AT, "grid_voltage_v") is None


def test_clearing_is_re_runnable(tmp_path: Path) -> None:
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v, battery_soc_pct) VALUES (?, ?, 66)",
        (BAD_AT, LEGACY_VOLTS),
    )
    conn.commit()

    assert clear(conn, find(conn)) == 1
    assert find(conn) == []
    assert clear(conn, find(conn)) == 0
    # The neighbouring good reading survived both runs.
    assert _row(conn, "inverter_raw", BAD_AT, "battery_soc_pct") == 66


def test_a_row_rewritten_since_the_scan_is_left_alone(tmp_path: Path) -> None:
    # The collector keeps writing while this runs. An update that named only the
    # row would blank a value somebody had just corrected, so each one names the
    # exact integer it expects to replace.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v) VALUES (?, ?)",
        (BAD_AT, LEGACY_VOLTS),
    )
    conn.commit()
    stale = find(conn)

    conn.execute("UPDATE inverter_raw SET grid_voltage_v = 2450 WHERE timestamp = ?", (BAD_AT,))
    conn.commit()

    assert clear(conn, stale) == 0
    assert _row(conn, "inverter_raw", BAD_AT, "grid_voltage_v") == 2450


def test_every_tier_is_covered_not_only_the_raw_one(tmp_path: Path) -> None:
    # The coarse tiers are the ones kept indefinitely, so a fault left in them
    # outlives the raw rows that explain it.
    tables = {c.table for c in columns_to_check()}
    assert tables == {
        "inverter_raw",
        "inverter_minute",
        "inverter_hourly",
        "module_raw",
        "module_hourly",
    }

    conn = sqlite3.connect(_database(tmp_path))
    for table in ("inverter_minute", "inverter_hourly"):
        conn.execute(
            f"INSERT INTO {table} (timestamp, grid_voltage_v, sample_count) VALUES (?, ?, 1)",
            (BAD_AT, LEGACY_VOLTS),
        )
    conn.commit()

    assert {o.column.table for o in find(conn)} == {"inverter_minute", "inverter_hourly"}
    assert clear(conn, find(conn)) == 2
    assert find(conn) == []


def test_a_module_reading_is_cleared_on_its_own_row_only(tmp_path: Path) -> None:
    # Module tables are keyed by (timestamp, module_id), so an update that
    # matched on the timestamp alone would blank every pack in the bank.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute("INSERT INTO serials (id, serial) VALUES (1, 'BA00000001')")
    conn.execute("INSERT INTO serials (id, serial) VALUES (2, 'BA00000002')")
    # voltage_v scales by 100, so a legacy scale of 1000 is ten times too large.
    # Both packs carry the identical bad integer, so nothing but the module id
    # can tell the two rows apart.
    for module_id in (1, 2):
        conn.execute(
            "INSERT INTO module_raw (timestamp, module_id, voltage_v) VALUES (?, ?, 53000)",
            (BAD_AT, module_id),
        )
    conn.commit()

    found = find(conn)
    assert [(o.module_id, o.decoded) for o in found] == [(1, 530.0), (2, 530.0)]

    assert clear(conn, [o for o in found if o.module_id == 1]) == 1

    stored = conn.execute(
        "SELECT module_id, voltage_v FROM module_raw WHERE timestamp = ? ORDER BY module_id",
        (BAD_AT,),
    ).fetchall()
    assert stored == [(1, None), (2, 53000)]


def test_the_invalid_readings_flags_are_left_alone(tmp_path: Path) -> None:
    # Clearing removes the number; the flag is the record that it was ever
    # taken. Erasing both leaves no trace that anything happened.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v) VALUES (?, ?)",
        (BAD_AT, LEGACY_VOLTS),
    )
    conn.execute(
        "INSERT INTO invalid_readings (timestamp, metric, value, serial) "
        "VALUES (?, 'battery_temperature_c', 11880.0, NULL)",
        (BAD_AT,),
    )
    conn.commit()

    clear(conn, find(conn))

    assert conn.execute("SELECT COUNT(*) FROM invalid_readings").fetchone()[0] == 1


def test_a_database_missing_a_registry_column_is_scrubbed_not_refused(tmp_path: Path) -> None:
    # A database made before a metric was added lacks that column until the
    # store next opens it. The tool should scrub what is there rather than fail.
    conn = sqlite3.connect(_database(tmp_path))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, grid_voltage_v) VALUES (?, ?)",
        (BAD_AT, LEGACY_VOLTS),
    )
    conn.commit()

    absent = Column("inverter_raw", "not_a_column_yet", lookup("grid_voltage_v"), False)
    found = find(conn, (*columns_to_check(), absent))

    assert [o.column.column for o in found] == ["grid_voltage_v"]


def test_every_registered_metric_is_covered_by_the_scan(tmp_path: Path) -> None:
    # Adding a metric to the registry must not need an edit here. If a column
    # were listed by hand instead, the newest metric — the likeliest place for a
    # fresh scale mistake — would be the one nothing checked.
    from arraysense.metrics import INVERTER_METRICS
    from arraysense.store.schema import module_metric_columns

    raw = {c.column for c in columns_to_check() if c.table == "inverter_raw"}
    assert raw == {spec.name for spec in INVERTER_METRICS}

    modules = {c.column for c in columns_to_check() if c.table == "module_raw"}
    assert modules == set(module_metric_columns())
