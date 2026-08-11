"""Tests for the SQLite store: arraysense.store.sqlite_store.

The store opens a database, lays down the schema, and appends inverter samples
to the full-cadence tier. Each test inspects the on-disk rows through a fresh
connection so the assertions target what was actually written, not what the
store's own handle would report. Databases come from ``tmp_path``, never a
fixed path.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE


def _ts() -> datetime:
    return datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a raw connection to ``path`` for inspecting what the store wrote."""
    return sqlite3.connect(path)


def _metric_columns(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return the inverter metric column names in table declaration order."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(inverter_raw)")]
    return tuple(c for c in cols if c not in ("timestamp", "device", "error"))


def test_opening_creates_schema(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.close()
    conn = _open_db(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "inverter_raw" in tables
    assert "invalid_readings" in tables
    assert "serials" in tables


def test_opening_existing_database_succeeds_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 500.0}))
    store.close()
    # Reopening runs the idempotent DDL again; the existing row must survive.
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT pv_total_power_w FROM inverter_raw").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == 500


def test_wal_journaling_is_enabled(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.close()
    conn = _open_db(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_primary_connection_explicitly_restores_full_synchronous(tmp_path: Path) -> None:
    """Raw durability must not depend on SQLite's compile-time default."""
    store = SqliteStore(str(tmp_path / "store.db"), device=TEST_DEVICE)
    store._conn.execute("PRAGMA synchronous = NORMAL")

    store._apply_connection_pragmas(store._conn, establish_wal=True)
    synchronous = store._conn.execute("PRAGMA synchronous").fetchone()
    store.close()

    assert synchronous == (2,)  # FULL


def test_maintenance_connection_does_not_renegotiate_persistent_wal_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The primary connection establishes WAL once at startup and SQLite keeps
    # that mode in the database. Reissuing PRAGMA journal_mode=WAL when every
    # maintenance connection opens is a lock-taking operation on the exact
    # sixty-second cadence being investigated, and the builder timings begin
    # after it, so it is both unnecessary and excluded from those measurements.
    store = SqliteStore(str(tmp_path / "store.db"), device=TEST_DEVICE)
    real_connect = sqlite3.connect
    statements: list[str] = []

    class RecordingConnection:
        def __init__(self, path: str, check_same_thread: bool = True) -> None:
            self._conn: sqlite3.Connection = real_connect(path, check_same_thread=check_same_thread)

        def execute(self, sql: str) -> sqlite3.Cursor:
            statements.append(sql)
            cursor: sqlite3.Cursor = self._conn.execute(sql)
            return cursor

        def close(self) -> None:
            self._conn.close()

    monkeypatch.setattr(sqlite3, "connect", RecordingConnection)
    maintenance = store.maintenance_connection()
    maintenance.close()
    store.close()

    assert not any("JOURNAL_MODE" in statement.upper() for statement in statements)


def test_maintenance_connection_inherits_wal_mode_established_at_startup(tmp_path: Path) -> None:
    # WAL is database state, not a per-connection option. Skipping the setter on
    # the once-a-minute connection must still leave that connection using WAL.
    store = SqliteStore(str(tmp_path / "store.db"), device=TEST_DEVICE)
    maintenance = store.maintenance_connection()
    mode = maintenance.execute("PRAGMA journal_mode").fetchone()
    maintenance.close()
    store.close()

    assert mode is not None and mode[0] == "wal"


def test_maintenance_connection_keeps_per_connection_safety_pragmas(tmp_path: Path) -> None:
    """Every maintenance connection keeps integrity and contention safeguards."""
    store = SqliteStore(str(tmp_path / "store.db"), device=TEST_DEVICE)
    maintenance = store.maintenance_connection()
    maintenance_pragmas = {
        "foreign_keys": maintenance.execute("PRAGMA foreign_keys").fetchone(),
        "busy_timeout": maintenance.execute("PRAGMA busy_timeout").fetchone(),
    }
    maintenance.close()
    store.close()

    assert maintenance_pragmas["foreign_keys"] == (1,)
    assert maintenance_pragmas["busy_timeout"] == (5000,)


def test_appended_reading_is_stored_scaled(tmp_path: Path) -> None:
    # battery_voltage_v has scale 10 — the resolution the register carries:
    # 51.9 V must land on disk as the integer 519, not as a float.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"battery_voltage_v": 51.9}))
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT battery_voltage_v FROM inverter_raw").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 519
    assert isinstance(row[0], int)


def test_absent_metric_is_null_and_distinct_from_zero(tmp_path: Path) -> None:
    # pv_total_power_w is a real zero (within bounds); battery_soc_pct is
    # simply absent from the sample. NULL must stay NULL, never become 0.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 0.0}))
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT pv_total_power_w, battery_soc_pct FROM inverter_raw").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 0
    assert row[1] is None


def test_failed_poll_stores_reason_and_no_readings(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT * FROM inverter_raw").fetchone()
    assert row is not None
    # Every metric column is NULL — a failed poll has no readings, not zeroed
    # readings — and the reason rides in the trailing error column.
    # Columns run timestamp, device, then the registry, then error.
    for i, _ in enumerate(_metric_columns(conn)):
        assert row[2 + i] is None
    assert row[-1] == "inverter unreachable"
    conn.close()


def test_out_of_bounds_reading_is_stored_and_flagged(tmp_path: Path) -> None:
    # 25,583 W of battery power is about double what an 18kPV can deliver. It
    # must be stored (evidence of a decode bug) and flagged in invalid_readings.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"battery_power_w": 25583.0}))
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT battery_power_w FROM inverter_raw").fetchone()
    invalid = conn.execute(
        "SELECT timestamp, metric, value, serial FROM invalid_readings"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 25583
    assert invalid is not None
    assert invalid[0] == int(_ts().timestamp())
    assert invalid[1] == "battery_power_w"
    assert invalid[2] == 25583.0
    assert invalid[3] is None  # inverter reading carries no serial


def test_same_timestamp_twice_leaves_one_row_later_values(tmp_path: Path) -> None:
    # A collector retry after a partial failure rewrites the same timestamp;
    # the later write wins and the row is never duplicated.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    ts = _ts()
    store.append(Sample(timestamp=ts, readings={"pv_total_power_w": 100.0}))
    store.append(Sample(timestamp=ts, readings={"pv_total_power_w": 200.0}))
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT pv_total_power_w FROM inverter_raw").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == 200


def test_unknown_metric_name_raises(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    with pytest.raises(KeyError):
        store.append(Sample(timestamp=_ts(), readings={"no_such_metric": 5.0}))
    store.close()
    # The programming error must fail before anything is written.
    conn = _open_db(path)
    count = conn.execute("SELECT COUNT(*) FROM inverter_raw").fetchone()[0]
    conn.close()
    assert count == 0


def test_repeated_append_does_not_duplicate_failure_flags(tmp_path: Path) -> None:
    # The row upsert is idempotent; the flags must be too, or a collector retry
    # would inflate the failure count for a fault that happened once.
    store = SqliteStore(str(tmp_path / "t.db"), device=TEST_DEVICE)
    ts = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    sample = Sample(timestamp=ts, readings={"battery_power_w": 25583.0})
    store.append(sample)
    store.append(sample)
    store.close()
    conn = _open_db(tmp_path / "t.db")
    rows = conn.execute(
        "SELECT COUNT(*) FROM invalid_readings WHERE timestamp = ?",
        (int(ts.timestamp()),),
    ).fetchone()[0]
    conn.close()
    assert rows == 1


def test_a_reading_becoming_valid_clears_its_stale_flag(tmp_path: Path) -> None:
    # If a retry reports a plausible value, the earlier flag must not linger and
    # imply the reading is still suspect.
    store = SqliteStore(str(tmp_path / "t.db"), device=TEST_DEVICE)
    ts = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    store.append(Sample(timestamp=ts, readings={"battery_power_w": 25583.0}))
    store.append(Sample(timestamp=ts, readings={"battery_power_w": 5000.0}))
    store.close()
    conn = _open_db(tmp_path / "t.db")
    rows = conn.execute(
        "SELECT COUNT(*) FROM invalid_readings WHERE timestamp = ?",
        (int(ts.timestamp()),),
    ).fetchone()[0]
    conn.close()
    assert rows == 0


def test_module_reading_is_stored_scaled(tmp_path: Path) -> None:
    # Module temperature_c has scale 10: 23.5 C must land on disk as 235.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE12345678", slot=1, temperature_c=23.5),),
        )
    )
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT temperature_c FROM module_raw").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 235


def test_serial_is_registered_once(tmp_path: Path) -> None:
    # The same serial appearing in every poll must not create a new id each
    # time; registering must be safe to repeat.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    module = BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=80.0)
    store.append(Sample(timestamp=_ts(), readings={}, battery_modules=(module,)))
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
            readings={},
            battery_modules=(module,),
        )
    )
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT id, serial FROM serials").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][1] == "CE12345678"


def test_two_serials_in_same_slot_make_distinct_series(tmp_path: Path) -> None:
    # A slot is positional, not identity: two physical batteries rotated through
    # the same slot must land on two module_ids, never merge into one series.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE11111111", slot=1, soc_pct=80.0),),
        )
    )
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE22222222", slot=1, soc_pct=70.0),),
        )
    )
    store.close()
    conn = _open_db(path)
    # Join back to the serial rather than counting ids: two distinct ids would
    # also pass if the serial-to-id association were swapped, which is exactly
    # the failure that attaches a reading to the wrong battery.
    rows = conn.execute(
        "SELECT s.serial, m.soc_pct FROM module_raw m "
        "JOIN serials s ON s.id = m.module_id ORDER BY m.timestamp"
    ).fetchall()
    conn.close()
    assert rows == [("CE11111111", 80), ("CE22222222", 70)]


def test_one_serial_across_slots_stays_a_single_series(tmp_path: Path) -> None:
    # The inverse of the rotation case: a module moved to another slot is the
    # same battery and must keep one identity, not fork into a second series.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    for minute, slot, soc in ((0, 1, 80.0), (1, 4, 79.0)):
        store.append(
            Sample(
                timestamp=datetime(2026, 8, 6, 12, minute, tzinfo=UTC),
                readings={},
                battery_modules=(BatteryModuleSample(serial="CE11111111", slot=slot, soc_pct=soc),),
            )
        )
    store.close()
    conn = _open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM serials").fetchone()[0] == 1
    rows = conn.execute(
        "SELECT s.serial, m.soc_pct FROM module_raw m "
        "JOIN serials s ON s.id = m.module_id ORDER BY m.timestamp"
    ).fetchall()
    conn.close()
    assert rows == [("CE11111111", 80), ("CE11111111", 79)]


def test_absent_module_field_is_null_distinct_from_zero(tmp_path: Path) -> None:
    # soc_pct=0.0 is a real zero (within bounds); soh_pct is simply absent.
    # NULL must stay NULL, never become 0.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=0.0),),
        )
    )
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT soc_pct, soh_pct FROM module_raw").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 0
    assert row[1] is None


def test_same_timestamp_and_module_leaves_one_row_later_values(tmp_path: Path) -> None:
    # A collector retry rewrites the same timestamp and module; the later write
    # wins and the row is never duplicated.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    ts = _ts()
    store.append(
        Sample(
            timestamp=ts,
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=80.0),),
        )
    )
    store.append(
        Sample(
            timestamp=ts,
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=90.0),),
        )
    )
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT soc_pct FROM module_raw").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == 90


def test_four_modules_at_one_timestamp_produce_four_rows(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    modules = tuple(
        BatteryModuleSample(serial=f"CE0000000{i}", slot=i, soc_pct=float(i)) for i in range(1, 5)
    )
    store.append(Sample(timestamp=_ts(), readings={}, battery_modules=modules))
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT COUNT(*) FROM module_raw").fetchone()[0]
    conn.close()
    assert rows == 4


def test_out_of_bounds_module_reading_is_stored_and_flagged(tmp_path: Path) -> None:
    # 120% SOC is impossible; it must be stored (evidence of a decode bug) and
    # flagged against the module's serial, and repeating the write must not
    # duplicate the flag.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    module = BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=120.0)
    sample = Sample(timestamp=_ts(), readings={}, battery_modules=(module,))
    store.append(sample)
    store.append(sample)
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT soc_pct FROM module_raw").fetchone()
    invalid = conn.execute(
        "SELECT timestamp, metric, value, serial FROM invalid_readings"
    ).fetchall()
    conn.close()
    assert row is not None
    assert row[0] == 120
    assert len(invalid) == 1
    assert invalid[0][1] == "battery_module1_soc_pct"
    assert invalid[0][2] == 120.0
    assert invalid[0][3] == "CE12345678"


def test_an_out_of_bounds_reading_names_the_pack_that_produced_it(tmp_path: Path) -> None:
    # The bounds come from the shared per-module template, because the registry
    # names columns for four slots and a fifth pack has none. The flag still has
    # to say which pack it was: labelling every pack battery_module1_* put one
    # condition in the database under two names and disagreed with the name
    # validate.py reports for the same reading. Both now name the slot.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    module = BatteryModuleSample(serial="CE12345675", slot=5, soc_pct=137.0)
    store.append(Sample(timestamp=_ts(), readings={}, battery_modules=(module,)))
    store.close()
    conn = _open_db(path)
    invalid = conn.execute("SELECT metric, value, serial FROM invalid_readings").fetchall()
    conn.close()
    assert len(invalid) == 1
    assert invalid[0][0] == "battery_module5_soc_pct"
    assert invalid[0][2] == "CE12345675"


def test_module_reading_becoming_valid_clears_its_stale_flag(tmp_path: Path) -> None:
    # If a retry reports a plausible value, the earlier module flag must not
    # linger and imply the reading is still suspect.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    ts = _ts()
    store.append(
        Sample(
            timestamp=ts,
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=120.0),),
        )
    )
    store.append(
        Sample(
            timestamp=ts,
            readings={},
            battery_modules=(BatteryModuleSample(serial="CE12345678", slot=1, soc_pct=80.0),),
        )
    )
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT COUNT(*) FROM invalid_readings").fetchone()[0]
    conn.close()
    assert rows == 0


def test_failed_poll_writes_no_module_rows(tmp_path: Path) -> None:
    # A failed poll has no modules (Sample enforces it); it must write no
    # module rows and register no serials.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    store.close()
    conn = _open_db(path)
    module_rows = conn.execute("SELECT COUNT(*) FROM module_raw").fetchone()[0]
    serial_rows = conn.execute("SELECT COUNT(*) FROM serials").fetchone()[0]
    conn.close()
    assert module_rows == 0
    assert serial_rows == 0


def test_a_failing_module_write_rolls_back_the_whole_sample(tmp_path: Path) -> None:
    # Both halves of a sample share one transaction, so a crash partway must
    # leave nothing behind rather than an inverter row with no modules.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store._conn.execute(
        "CREATE TRIGGER reject_modules BEFORE INSERT ON module_raw "
        "BEGIN SELECT RAISE(ABORT, 'simulated failure'); END"
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.append(
            Sample(
                timestamp=_ts(),
                readings={"pv_total_power_w": 500.0},
                battery_modules=(BatteryModuleSample(serial="CE11111111", slot=1, soc_pct=80.0),),
            )
        )
    store._conn.execute("DROP TRIGGER reject_modules")
    store.close()
    conn = _open_db(path)
    assert conn.execute("SELECT COUNT(*) FROM inverter_raw").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM module_raw").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM serials").fetchone()[0] == 0
    conn.close()


def test_query_returns_real_world_values(tmp_path: Path) -> None:
    # battery_voltage_v stores scaled by 1000; the caller gets volts back.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"battery_voltage_v": 51.9}))
    rows = store.query(["battery_voltage_v"], _ts(), _ts())
    store.close()
    assert len(rows) == 1
    assert rows[0]["battery_voltage_v"] == pytest.approx(51.9)
    assert rows[0]["timestamp"] == _ts()


def test_query_keeps_absent_distinct_from_zero(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 0.0}))
    rows = store.query(["pv_total_power_w", "battery_soc_pct"], _ts(), _ts())
    store.close()
    assert rows[0]["pv_total_power_w"] == 0.0
    assert rows[0]["battery_soc_pct"] is None


def test_query_respects_the_range_and_orders_by_time(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    for minute, value in ((0, 1.0), (1, 2.0), (5, 3.0)):
        store.append(
            Sample(
                timestamp=datetime(2026, 8, 6, 12, minute, tzinfo=UTC),
                readings={"pv_total_power_w": value},
            )
        )
    rows = store.query(
        ["pv_total_power_w"],
        datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
    )
    store.close()
    assert [r["pv_total_power_w"] for r in rows] == [1.0, 2.0]


def test_query_identifies_a_failed_poll(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    rows = store.query(["pv_total_power_w"], _ts(), _ts())
    store.close()
    assert rows[0]["error"] == "inverter unreachable"
    assert rows[0]["pv_total_power_w"] is None


def test_unknown_metric_in_query_raises(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    with pytest.raises(KeyError):
        store.query(["no_such_metric"], _ts(), _ts())
    store.close()


def test_unknown_tier_raises(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    with pytest.raises(KeyError):
        store.query(["pv_total_power_w"], _ts(), _ts(), tier="nonexistent")
    # Module data has no minute tier.
    with pytest.raises(KeyError):
        store.query_modules(["soc_pct"], _ts(), _ts(), tier="minute")
    store.close()


def test_module_query_is_keyed_by_serial(tmp_path: Path) -> None:
    # Two batteries that occupied slot 1 at different times must come back as
    # two series, identified by serial rather than position.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    for minute, serial, soc in ((0, "AAA", 90.0), (1, "BBB", 20.0)):
        store.append(
            Sample(
                timestamp=datetime(2026, 8, 6, 12, minute, tzinfo=UTC),
                readings={},
                battery_modules=(BatteryModuleSample(serial=serial, slot=1, soc_pct=soc),),
            )
        )
    rows = store.query_modules(
        ["soc_pct"],
        datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
    )
    store.close()
    assert [(r["serial"], r["soc_pct"]) for r in rows] == [("AAA", 90.0), ("BBB", 20.0)]


def test_module_query_can_filter_to_one_serial(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(
                BatteryModuleSample(serial="AAA", slot=1, soc_pct=90.0),
                BatteryModuleSample(serial="BBB", slot=2, soc_pct=20.0),
            ),
        )
    )
    rows = store.query_modules(["soc_pct"], _ts(), _ts(), serial="BBB")
    store.close()
    assert [(r["serial"], r["soc_pct"]) for r in rows] == [("BBB", 20.0)]


def test_latest_returns_the_most_recent_reading(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    for minute, value in ((0, 1.0), (5, 2.0), (3, 3.0)):
        store.append(
            Sample(
                timestamp=datetime(2026, 8, 6, 12, minute, tzinfo=UTC),
                readings={"pv_total_power_w": value},
            )
        )
    row = store.latest(["pv_total_power_w"])
    store.close()
    assert row is not None
    assert row["pv_total_power_w"] == 2.0  # 12:05, not the last written
    assert row["timestamp"] == datetime(2026, 8, 6, 12, 5, tzinfo=UTC)


def test_latest_is_none_on_an_empty_store(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    assert store.latest(["pv_total_power_w"]) is None
    assert store.latest_modules(["soc_pct"]) == []
    store.close()


def test_latest_modules_gives_each_module_once(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    for minute, soc_a, soc_b in ((0, 90.0, 20.0), (1, 88.0, 19.0)):
        store.append(
            Sample(
                timestamp=datetime(2026, 8, 6, 12, minute, tzinfo=UTC),
                readings={},
                battery_modules=(
                    BatteryModuleSample(serial="AAA", slot=1, soc_pct=soc_a),
                    BatteryModuleSample(serial="BBB", slot=2, soc_pct=soc_b),
                ),
            )
        )
    rows = store.latest_modules(["soc_pct"])
    store.close()
    assert [(r["serial"], r["soc_pct"]) for r in rows] == [("AAA", 88.0), ("BBB", 19.0)]


def test_latest_uses_an_index_rather_than_scanning(tmp_path: Path) -> None:
    # The live view asks for this on every refresh; it must not scan the table.
    # The tables are WITHOUT ROWID, so the primary key is the b-tree itself and
    # SQLite reports "SCAN" while walking it in reverse and stopping at the
    # first row. The property that matters is that it does not sort: a temp
    # b-tree would mean reading every row on every refresh.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    plan = store._conn.execute(
        "EXPLAIN QUERY PLAN SELECT timestamp, pv_total_power_w FROM inverter_raw "
        "ORDER BY timestamp DESC LIMIT 1"
    ).fetchall()
    store.close()
    text = " ".join(str(r[-1]) for r in plan).upper()
    assert "TEMP B-TREE" not in text, plan


def test_the_store_can_be_used_from_another_thread(tmp_path: Path) -> None:
    # The collector writes from the event loop while the web server answers on
    # a threadpool. A connection bound to its creating thread refuses the
    # second one, which surfaces only once both halves are running together.
    import threading

    store = SqliteStore(str(tmp_path / "threads.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 1234.0}))
    result: list[object] = []

    def read_from_elsewhere() -> None:
        result.append(store.latest(["pv_total_power_w"]))

    thread = threading.Thread(target=read_from_elsewhere)
    thread.start()
    thread.join()
    store.close()
    assert result and result[0] is not None
    assert result[0]["pv_total_power_w"] == 1234.0  # type: ignore[index]


OTHER_DEVICE = "CE00000001"


def test_two_inverters_at_the_same_instant_are_two_rows(tmp_path: Path) -> None:
    # Parallel units are polled independently and land on the same second often
    # enough. Before the device was in the key one simply overwrote the other,
    # and the loser's reading was gone with nothing to say it had existed.
    store = SqliteStore(str(tmp_path / "two.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 1000.0}))
    store.append(
        Sample(timestamp=_ts(), readings={"pv_total_power_w": 2000.0}), device=OTHER_DEVICE
    )
    conn = _open_db(tmp_path / "two.db")
    rows = conn.execute("SELECT device, pv_total_power_w FROM inverter_raw ORDER BY device")
    assert rows.fetchall() == [(TEST_DEVICE, 1000), (OTHER_DEVICE, 2000)]
    conn.close()
    store.close()


def test_a_query_returns_one_inverter_and_not_the_other(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "two.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 1000.0}))
    store.append(
        Sample(timestamp=_ts(), readings={"pv_total_power_w": 2000.0}), device=OTHER_DEVICE
    )

    mine = store.query(["pv_total_power_w"], _ts(), _ts())
    theirs = store.query(["pv_total_power_w"], _ts(), _ts(), device=OTHER_DEVICE)
    store.close()
    assert [r["pv_total_power_w"] for r in mine] == [1000.0]
    assert [r["pv_total_power_w"] for r in theirs] == [2000.0]


def test_latest_is_this_inverters_latest_not_the_newest_row(tmp_path: Path) -> None:
    # The second inverter reporting a second later must not become this one's
    # live reading, which is what an unfiltered "newest row" would do.
    store = SqliteStore(str(tmp_path / "two.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 1000.0}))
    store.append(
        Sample(timestamp=_ts() + timedelta(seconds=1), readings={"pv_total_power_w": 2000.0}),
        device=OTHER_DEVICE,
    )

    row = store.latest(["pv_total_power_w"])
    store.close()
    assert row is not None
    assert row["pv_total_power_w"] == 1000.0


def test_a_pack_serial_belongs_to_its_own_inverter(tmp_path: Path) -> None:
    # Two banks can carry the same serial — a replacement pack, or a vendor that
    # numbers from one. Each inverter's reading has to stay its own.
    store = SqliteStore(str(tmp_path / "two.db"), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(BatteryModuleSample(serial="P1", slot=1, soc_pct=40.0),),
        )
    )
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(BatteryModuleSample(serial="P1", slot=1, soc_pct=90.0),),
        ),
        device=OTHER_DEVICE,
    )

    mine = store.latest_modules(["soc_pct"])
    theirs = store.latest_modules(["soc_pct"], device=OTHER_DEVICE)
    store.close()
    assert [(r["serial"], r["soc_pct"]) for r in mine] == [("P1", 40.0)]
    assert [(r["serial"], r["soc_pct"]) for r in theirs] == [("P1", 90.0)]


def test_a_flagged_reading_is_attributed_to_the_inverter_that_produced_it(
    tmp_path: Path,
) -> None:
    # A decode fault is evidence about one unit. Recording it without saying
    # which makes it evidence about nothing, and a retry on either inverter
    # would clear the other's flag.
    store = SqliteStore(str(tmp_path / "two.db"), device=TEST_DEVICE)
    store.append(Sample(timestamp=_ts(), readings={"battery_power_w": 25583.0}))
    store.append(
        Sample(timestamp=_ts(), readings={"battery_power_w": 25583.0}), device=OTHER_DEVICE
    )
    store.append(Sample(timestamp=_ts(), readings={"battery_power_w": 5000.0}))

    conn = _open_db(tmp_path / "two.db")
    flags = conn.execute("SELECT device FROM invalid_readings").fetchall()
    conn.close()
    store.close()
    # This inverter's retry cleared its own flag and left the other's standing.
    assert flags == [(OTHER_DEVICE,)]


def test_a_store_refuses_to_open_without_a_device(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="device"):
        SqliteStore(str(tmp_path / "s.db"), device="   ")


def test_a_blank_device_is_refused_rather_than_silently_empty(tmp_path: Path) -> None:
    # __init__ already rejects a blank device, and the resolver did not — so
    # ?device= on a query string, which a browser sends readily, resolved to a
    # device nothing has ever recorded. No rows, no error, and a page that
    # reads as an inverter which stopped reporting.
    store = SqliteStore(str(tmp_path / "blank.db"), device=TEST_DEVICE)
    with pytest.raises(ValueError):
        store.latest(["pv_total_power_w"], device="")
    with pytest.raises(ValueError):
        store.latest(["pv_total_power_w"], device="   ")
    store.close()


# --- a store opened for what its driver declares ------------------------------
#
# A driver declares the metrics its device produces, and the store lays its
# schema for exactly those. A fresh database then has no column that can never
# be filled — while a database made before drivers declared subsets keeps every
# column it has, because narrowing what will be written must never touch what
# was.

# One PV reading and one per-module template. Declaring one slot's expansion
# declares the template for every slot, since the module tables carry one bare
# column per template.
_DECLARED = frozenset({"pv_total_power_w", "battery_module1_soc_pct"})


def test_a_narrowed_store_creates_only_declared_columns(tmp_path: Path) -> None:
    path = tmp_path / "narrow.db"
    store = SqliteStore(str(path), device=TEST_DEVICE, metrics=_DECLARED)
    store.close()
    conn = _open_db(path)
    inverter = _metric_columns(conn)
    modules = [
        r[1]
        for r in conn.execute("PRAGMA table_info(module_raw)")
        if r[1] not in ("timestamp", "device", "module_id")
    ]
    conn.close()
    assert inverter == ("pv_total_power_w",)
    assert modules == ["soc_pct"]


def test_an_undeclared_registry_metric_reads_back_as_none(tmp_path: Path) -> None:
    # The live page asks for every registry metric whatever the device is. A
    # metric the driver never declared has no column, and the honest answer is
    # the same None an unreported reading gives — never an SQL error.
    store = SqliteStore(str(tmp_path / "narrow.db"), device=TEST_DEVICE, metrics=_DECLARED)
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 500.0}))
    rows = store.query(["pv_total_power_w", "load_power_w"], _ts() - timedelta(minutes=1), _ts())
    latest = store.latest(["pv_total_power_w", "load_power_w"])
    store.close()
    assert rows[0]["pv_total_power_w"] == 500.0
    assert rows[0]["load_power_w"] is None
    assert latest is not None
    assert latest["load_power_w"] is None


def test_an_undeclared_module_metric_reads_back_as_none(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "narrow.db"), device=TEST_DEVICE, metrics=_DECLARED)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=(BatteryModuleSample(serial="BA1", slot=1, soc_pct=64.0),),
        )
    )
    rows = store.query_modules(["soc_pct", "voltage_v"], _ts() - timedelta(minutes=1), _ts())
    modules = store.latest_modules(["soc_pct", "voltage_v"])
    store.close()
    assert rows[0]["soc_pct"] == 64.0
    assert rows[0]["voltage_v"] is None
    assert modules[0]["voltage_v"] is None


def test_append_refuses_a_reading_the_driver_never_declared(tmp_path: Path) -> None:
    # A reading the schema has no column for would otherwise vanish without a
    # trace on its way to the store — the one failure this project exists to
    # prevent. It means the driver's declaration and its output have drifted,
    # which is a bug to surface, not a value to drop.
    store = SqliteStore(str(tmp_path / "narrow.db"), device=TEST_DEVICE, metrics=_DECLARED)
    with pytest.raises(KeyError, match="load_power_w"):
        store.append(Sample(timestamp=_ts(), readings={"load_power_w": 100.0}))
    store.close()


def test_append_refuses_an_undeclared_module_reading(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "narrow.db"), device=TEST_DEVICE, metrics=_DECLARED)
    with pytest.raises(KeyError, match="voltage_v"):
        store.append(
            Sample(
                timestamp=_ts(),
                readings={},
                battery_modules=(
                    BatteryModuleSample(serial="BA1", slot=1, soc_pct=64.0, voltage_v=53.7),
                ),
            )
        )
    store.close()


def test_a_narrowed_store_opens_a_full_database_and_leaves_it_whole(tmp_path: Path) -> None:
    # The compatible behaviour, exercised end to end: the declared set governs
    # tables that do not exist yet, and an installation whose tables already
    # carry the full registry keeps every column — and every reading — it has.
    path = tmp_path / "grown.db"
    full = SqliteStore(str(path), device=TEST_DEVICE)
    full.append(Sample(timestamp=_ts(), readings={"load_power_w": 2810.0}))
    full.close()

    store = SqliteStore(str(path), device=TEST_DEVICE, metrics=_DECLARED)
    # The undeclared column survives, and its history stays readable.
    conn = _open_db(path)
    cols = _metric_columns(conn)
    conn.close()
    assert "load_power_w" in cols
    rows = store.query(["load_power_w"], _ts() - timedelta(minutes=1), _ts())
    assert rows[0]["load_power_w"] == 2810.0
    # Writing still follows the declaration.
    store.append(Sample(timestamp=_ts() + timedelta(minutes=1), readings={"pv_total_power_w": 1.0}))
    store.close()


def test_a_newly_declared_metric_gains_its_column_on_open(tmp_path: Path) -> None:
    # Adding a metric to the registry and a driver's declaration stays a
    # no-migration change: the column arrives the next time the store opens.
    path = tmp_path / "grow.db"
    SqliteStore(str(path), device=TEST_DEVICE, metrics=frozenset({"pv_total_power_w"})).close()
    store = SqliteStore(str(path), device=TEST_DEVICE, metrics=_DECLARED | {"load_power_w"})
    store.append(Sample(timestamp=_ts(), readings={"load_power_w": 100.0}))
    latest = store.latest(["load_power_w"])
    store.close()
    assert latest is not None
    assert latest["load_power_w"] == 100.0


def test_an_empty_declaration_still_records_a_gap(tmp_path: Path) -> None:
    # A declaration with no inverter metric at all is legal, and a gap row
    # carries no readings anyway — only a timestamp, a device and its reason.
    # The upsert must survive an empty column list; it once generated
    # "DO UPDATE SET , error=..." and broke gap recording outright for the
    # whole device class this narrowing exists for. Appended twice, because
    # the retry path is the DO UPDATE branch.
    store = SqliteStore(str(tmp_path / "empty.db"), device=TEST_DEVICE, metrics=frozenset())
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    rows = store.query([], _ts() - timedelta(minutes=1), _ts())
    store.close()
    assert len(rows) == 1
    assert rows[0]["error"] == "inverter unreachable"


def test_a_module_only_declaration_records_gaps_and_module_readings(tmp_path: Path) -> None:
    # The bank-summary-inverted device: everything it reports is per-module.
    # Its inverter tiers hold nothing but timestamps and gap reasons, and both
    # halves must keep working.
    store = SqliteStore(
        str(tmp_path / "mod.db"),
        device=TEST_DEVICE,
        metrics=frozenset({"battery_module1_soc_pct"}),
    )
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    store.append(
        Sample(
            timestamp=_ts() + timedelta(minutes=1),
            readings={},
            battery_modules=(BatteryModuleSample(serial="BA1", slot=1, soc_pct=64.0),),
        )
    )
    gap = store.query([], _ts() - timedelta(minutes=1), _ts())
    modules = store.latest_modules(["soc_pct"])
    store.close()
    assert gap[0]["error"] == "inverter unreachable"
    assert modules[0]["soc_pct"] == 64.0


def test_a_module_carrying_identity_alone_survives_an_empty_declaration(tmp_path: Path) -> None:
    # With no template declared, a module row is nothing but identity and a
    # timestamp — and a collector retry of that row has nothing to update.
    # Both writes must succeed; the second is the ON CONFLICT branch.
    store = SqliteStore(str(tmp_path / "ident.db"), device=TEST_DEVICE, metrics=frozenset())
    sample = Sample(
        timestamp=_ts(),
        readings={},
        battery_modules=(BatteryModuleSample(serial="BA1", slot=1),),
    )
    store.append(sample)
    store.append(sample)
    modules = store.latest_modules([])
    store.close()
    assert [m["serial"] for m in modules] == ["BA1"]


# --- banks larger than four modules (#29) --------------------------------------
#
# Nothing in storage requires four. module_raw is keyed on (timestamp, module_id)
# with the serial resolving to a stable id, so a fifth pack is another row and not
# another column. What refuses it is the registry lookup: the store finds a
# reading's scale through ``battery_module{slot}_{name}``, the registry expands
# those names over four slots, and every slot's spec is generated from the same
# template — so the slot number only ever locates a spec identical to its
# neighbours. It contributes nothing but a bound to trip over.


def test_a_bank_of_five_modules_stores_every_one(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    modules = tuple(
        BatteryModuleSample(serial=f"CE0000000{i}", slot=i, soc_pct=float(50 + i))
        for i in range(1, 6)
    )
    store.append(Sample(timestamp=_ts(), readings={}, battery_modules=modules))
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT COUNT(*) FROM module_raw").fetchone()[0]
    conn.close()
    assert rows == 5


def test_the_fifth_module_reads_back_with_the_same_scale_as_the_first(tmp_path: Path) -> None:
    # The slot decides which registry spec is consulted for the scale, and every
    # slot's spec comes from one template, so a fifth pack must encode exactly as
    # the first does. A fifth that stored raw while the first stored scaled would
    # put two units in one column.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    modules = (
        BatteryModuleSample(serial="CE00000001", slot=1, voltage_v=53.25),
        BatteryModuleSample(serial="CE00000005", slot=5, voltage_v=53.25),
    )
    store.append(Sample(timestamp=_ts(), readings={}, battery_modules=modules))
    got = {m["serial"]: m["voltage_v"] for m in store.latest_modules(["voltage_v"])}
    store.close()
    assert got["CE00000001"] == 53.25
    assert got["CE00000005"] == 53.25


def test_a_database_written_with_four_packs_accepts_a_fifth(tmp_path: Path) -> None:
    # The upgrade path: an installation that has been running four packs gains one.
    # No migration should be needed, because the fifth is a row and not a column.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=tuple(
                BatteryModuleSample(serial=f"CE0000000{i}", slot=i, soc_pct=60.0)
                for i in range(1, 5)
            ),
        )
    )
    store.close()

    reopened = SqliteStore(str(path), device=TEST_DEVICE)
    reopened.append(
        Sample(
            timestamp=_ts(),
            readings={},
            battery_modules=tuple(
                BatteryModuleSample(serial=f"CE0000000{i}", slot=i, soc_pct=61.0)
                for i in range(1, 6)
            ),
        )
    )
    serials = {m["serial"] for m in reopened.latest_modules(["soc_pct"])}
    reopened.close()
    assert serials == {f"CE0000000{i}" for i in range(1, 6)}


def test_the_primary_connection_honours_the_configured_durability(tmp_path: Path) -> None:
    # The setting is worth nothing if it does not reach the connection. Read the
    # pragma back rather than trusting that it was passed: 2 is FULL, 1 NORMAL.
    full = SqliteStore(str(tmp_path / "f.db"), device=TEST_DEVICE)
    assert full._conn.execute("PRAGMA synchronous").fetchone() == (2,)
    full.close()

    relaxed = SqliteStore(str(tmp_path / "n.db"), device=TEST_DEVICE, synchronous="normal")
    assert relaxed._conn.execute("PRAGMA synchronous").fetchone() == (1,)
    relaxed.close()


def test_relaxed_durability_still_stores_and_reads_back(tmp_path: Path) -> None:
    # NORMAL trades a bounded amount of recent data on an abrupt power loss. It
    # does not trade correctness, and a reading written under it must come back
    # exactly as one written under FULL.
    store = SqliteStore(str(tmp_path / "n.db"), device=TEST_DEVICE, synchronous="normal")
    now = datetime.now(tz=UTC)
    store.append(Sample(timestamp=now, readings={"battery_voltage_v": 55.9}))
    rows = store.query(["battery_voltage_v"], now, now)
    store.close()
    assert rows[0]["battery_voltage_v"] == 55.9


def test_latest_skips_a_newer_row_that_carries_none_of_the_asked_metrics(
    tmp_path: Path,
) -> None:
    # Two writers share this tier: the weather poller lands a row whose
    # inverter columns are all null. Row-based recency returned that row and
    # blanked the live dashboard for a poll cycle after every weather tick —
    # an absence drawn where data exists. The most recent READING of the asked
    # metrics is the answer, in both directions.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            readings={"pv_total_power_w": 5000.0, "battery_soc_pct": 64.0},
        )
    )
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 0, 30, tzinfo=UTC),
            readings={"outside_temperature_c": 37.4, "cloud_cover_pct": 0.0},
        )
    )
    live = store.latest(["pv_total_power_w", "battery_soc_pct"])
    assert live is not None
    assert live["pv_total_power_w"] == 5000.0, "a weather row must not blank the live view"
    assert live["timestamp"] == datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    # The mirror: the sky is readable under newer inverter rows.
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
            readings={"pv_total_power_w": 5100.0},
        )
    )
    sky = store.latest(["outside_temperature_c"])
    store.close()
    assert sky is not None
    assert sky["outside_temperature_c"] == 37.4


def test_latest_still_surfaces_a_gap_row(tmp_path: Path) -> None:
    # Recency is not health: a recorded gap answers every request, whatever
    # metrics it was asked for, because a gap is information and hiding it
    # would dress an outage as the last good reading with no timestamp shift.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            readings={"pv_total_power_w": 5000.0},
        )
    )
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
            readings={},
            error="ConnectionError: nobody answered",
        )
    )
    row = store.latest(["pv_total_power_w"])
    store.close()
    assert row is not None
    assert row["error"] == "ConnectionError: nobody answered"
    assert row["timestamp"] == datetime(2026, 8, 6, 12, 1, tzinfo=UTC)


def test_latest_without_gaps_walks_past_a_newer_gap_row(tmp_path: Path) -> None:
    # The sky readout asks for the newest actual weather reading. A recorded
    # inverter gap lands newer than the last weather tick many times an hour on
    # a lossy link, and surfacing it there would blank the readout while a
    # five-minute-old reading exists. Opting out of gaps walks past them; the
    # default still surfaces them, because for the dashboard recency is not
    # health.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
            readings={"outside_temperature_c": 37.4},
        )
    )
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 6, 12, 1, tzinfo=UTC),
            readings={},
            error="ConnectionError: nobody answered",
        )
    )
    with_gaps = store.latest(["outside_temperature_c"])
    assert with_gaps is not None
    assert with_gaps["error"] is not None, "the default keeps surfacing gaps"
    reading = store.latest(["outside_temperature_c"], include_gaps=False)
    store.close()
    assert reading is not None
    assert reading["outside_temperature_c"] == 37.4
    assert reading["error"] is None


def test_forecast_keeps_every_revision_and_serves_dawn_and_latest(tmp_path: Path) -> None:
    # Two truths from one table: the dawn plan the day is tracked against, and
    # the latest expectation for the hours still to come. A revision must never
    # erase the plan it revises — and a forecast made yesterday is not today's
    # dawn plan.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    day = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    noon = day.replace(hour=12)
    one_pm = day.replace(hour=13)
    # yesterday's forecast for today: excluded from "first"
    store.append_forecast(day - timedelta(hours=10), [(noon, 5000.0)])
    # dawn plan
    store.append_forecast(day.replace(hour=6), [(noon, 6000.0), (one_pm, 6500.0)])
    # midday revision
    store.append_forecast(day.replace(hour=11), [(noon, 4200.0), (one_pm, 4400.0)])
    curves = store.forecast_day(day, day + timedelta(days=1))
    store.close()
    first = {c["hour"]: c["expected_w"] for c in curves["first"]}
    latest = {c["hour"]: c["expected_w"] for c in curves["latest"]}
    assert first[noon] == 6000.0, "the dawn plan survives its revision"
    assert first[one_pm] == 6500.0
    assert latest[noon] == 4200.0, "the latest expectation is the revision"
    assert latest[one_pm] == 4400.0


def test_forecast_prune_drops_old_hours(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    old = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    new = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)
    store.append_forecast(old, [(old, 4000.0)])
    store.append_forecast(new, [(new, 5000.0)])
    removed = store.prune_forecast(datetime(2026, 6, 1, tzinfo=UTC))
    curves = store.forecast_day(new.replace(hour=0), new.replace(hour=0) + timedelta(days=1))
    store.close()
    assert removed == 1
    assert len(curves["latest"]) == 1


def test_efficiency_days_are_written_and_read_back(tmp_path: Path) -> None:
    from arraysense.efficiency import EfficiencyRow

    store = SqliteStore(str(tmp_path / "eff.db"), device=TEST_DEVICE)
    day = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    rows = [
        EfficiencyRow(
            day=day,
            string_name="East",
            expected_kwh=32.0,
            actual_kwh=28.5,
            curtailed_kwh=1.5,
            unexplained_kwh=2.0,
            modelled_hours=10,
            partial=False,
            pr=0.89,
            config_version=1,
        ),
        EfficiencyRow(
            day=day,
            string_name="",
            expected_kwh=32.0,
            actual_kwh=28.5,
            curtailed_kwh=1.5,
            unexplained_kwh=2.0,
            modelled_hours=10,
            partial=False,
            pr=0.89,
            config_version=1,
        ),
    ]
    store.write_efficiency_day(rows)

    got = store.read_efficiency_days(day, day + timedelta(days=1))
    store.close()
    assert len(got) == 2
    by_name = {r.string_name: r for r in got}
    assert by_name["East"].expected_kwh == 32.0
    assert by_name["East"].actual_kwh == 28.5
    assert by_name["East"].curtailed_kwh == 1.5
    assert by_name["East"].unexplained_kwh == 2.0
    assert by_name["East"].modelled_hours == 10
    assert not by_name["East"].partial
    assert by_name["East"].pr == 0.89
    assert by_name["East"].config_version == 1
    assert by_name[""].string_name == ""


def test_scored_days_reports_only_total_rows_at_the_asked_version(tmp_path: Path) -> None:
    from arraysense.efficiency import EfficiencyRow

    store = SqliteStore(str(tmp_path / "scored.db"), device=TEST_DEVICE)
    day1 = datetime(2026, 8, 8, 0, 0, tzinfo=UTC)
    day2 = datetime(2026, 8, 9, 0, 0, tzinfo=UTC)

    def rows(day: datetime, version: int) -> list[EfficiencyRow]:
        return [
            EfficiencyRow(
                day=day,
                string_name=name,
                expected_kwh=10.0,
                actual_kwh=9.0,
                curtailed_kwh=0.0,
                unexplained_kwh=1.0,
                modelled_hours=8,
                partial=False,
                pr=0.9,
                config_version=version,
            )
            for name in ("East", "")
        ]

    # day1 at version 1; day2 has only a string row at version 1 — no total —
    # plus a full tally at version 2. Only a complete day counts, and only at
    # the version asked for.
    store.write_efficiency_day(rows(day1, 1))
    store.write_efficiency_day(rows(day2, 1)[:1])
    store.write_efficiency_day(rows(day2, 2))

    assert store.scored_days(1) == {int(day1.timestamp())}
    assert store.scored_days(2) == {int(day2.timestamp())}
    store.close()


def test_efficiency_day_writes_overwrite_by_primary_key(tmp_path: Path) -> None:
    from arraysense.efficiency import EfficiencyRow

    store = SqliteStore(str(tmp_path / "eff2.db"), device=TEST_DEVICE)
    day = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    v1 = [
        EfficiencyRow(
            day=day,
            string_name="East",
            expected_kwh=30.0,
            actual_kwh=25.0,
            curtailed_kwh=0.0,
            unexplained_kwh=5.0,
            modelled_hours=8,
            partial=False,
            pr=0.83,
            config_version=1,
        )
    ]
    store.write_efficiency_day(v1)
    v2 = [
        EfficiencyRow(
            day=day,
            string_name="East",
            expected_kwh=31.0,
            actual_kwh=25.0,
            curtailed_kwh=0.0,
            unexplained_kwh=6.0,
            modelled_hours=8,
            partial=False,
            pr=0.81,
            config_version=2,
        )
    ]
    store.write_efficiency_day(v2)

    got = store.read_efficiency_days(day, day + timedelta(days=1))
    store.close()
    assert len(got) == 1
    assert got[0].expected_kwh == 31.0
    assert got[0].config_version == 2


def test_peak_reads_the_raw_maximum(tmp_path: Path) -> None:
    # The forecast calibrates on the system's own observed peak — the raw tier,
    # because a coarser tier's mean flattens exactly the peak this is for.
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    for minute, watts in ((0, 9000.0), (1, 13000.0), (2, 11000.0)):
        store.append(
            Sample(
                timestamp=datetime(2026, 8, 6, 12, minute, tzinfo=UTC),
                readings={"pv_total_power_w": watts},
            )
        )
    peak = store.peak(
        "pv_total_power_w",
        datetime(2026, 8, 1, tzinfo=UTC),
        datetime(2026, 8, 7, tzinfo=UTC),
    )
    empty = store.peak(
        "pv_total_power_w",
        datetime(2020, 1, 1, tzinfo=UTC),
        datetime(2020, 1, 2, tzinfo=UTC),
    )
    store.close()
    assert peak == 13000.0
    assert empty is None


def test_an_efficiency_day_keeps_its_calendar_date_east_of_utc(tmp_path: Path) -> None:
    """A day is the instant of local midnight, and must read back as that day.

    Returned as UTC, local midnight in any zone east of Greenwich lands on the
    previous calendar date, so every day would be labelled as the one before.
    Sydney rather than Chicago on purpose: the reference installation is west
    of UTC and cannot show this at all.
    """
    from zoneinfo import ZoneInfo

    from arraysense.efficiency import EfficiencyRow

    sydney = ZoneInfo("Australia/Sydney")
    day = datetime(2026, 8, 10, 0, 0, tzinfo=sydney)
    store = SqliteStore(str(tmp_path / "tz.db"), device=TEST_DEVICE)
    store.write_efficiency_day(
        [
            EfficiencyRow(
                day=day,
                string_name="",
                expected_kwh=10.0,
                actual_kwh=9.0,
                curtailed_kwh=0.0,
                unexplained_kwh=1.0,
                modelled_hours=8,
                partial=False,
                pr=0.9,
                config_version=1,
            )
        ]
    )
    got = store.read_efficiency_days(day, day + timedelta(days=1))
    store.close()
    assert len(got) == 1
    assert got[0].day.date() == day.date(), "the day slipped to the one before"
