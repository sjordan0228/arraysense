"""Tests for the SQLite store: arraysense.store.sqlite_store.

The store opens a database, lays down the schema, and appends inverter samples
to the full-cadence tier. Each test inspects the on-disk rows through a fresh
connection so the assertions target what was actually written, not what the
store's own handle would report. Databases come from ``tmp_path``, never a
fixed path.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.sqlite_store import SqliteStore


def _ts() -> datetime:
    return datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _open_db(path: Path) -> sqlite3.Connection:
    """Open a raw connection to ``path`` for inspecting what the store wrote."""
    return sqlite3.connect(path)


def _metric_columns(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Return the inverter metric column names in table declaration order."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(inverter_raw)")]
    return tuple(c for c in cols if c not in ("timestamp", "error"))


def test_opening_creates_schema(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path))
    store.close()
    conn = _open_db(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "inverter_raw" in tables
    assert "invalid_readings" in tables
    assert "serials" in tables


def test_opening_existing_database_succeeds_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path))
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 500.0}))
    store.close()
    # Reopening runs the idempotent DDL again; the existing row must survive.
    store = SqliteStore(str(path))
    store.close()
    conn = _open_db(path)
    rows = conn.execute("SELECT pv_total_power_w FROM inverter_raw").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0][0] == 500


def test_wal_journaling_is_enabled(tmp_path: Path) -> None:
    path = tmp_path / "store.db"
    store = SqliteStore(str(path))
    store.close()
    conn = _open_db(path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_appended_reading_is_stored_scaled(tmp_path: Path) -> None:
    # battery_voltage_v has scale 10 — the resolution the register carries:
    # 51.9 V must land on disk as the integer 519, not as a float.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    store.close()
    conn = _open_db(path)
    row = conn.execute("SELECT * FROM inverter_raw").fetchone()
    assert row is not None
    # Every metric column is NULL — a failed poll has no readings, not zeroed
    # readings — and the reason rides in the trailing error column.
    for i, _ in enumerate(_metric_columns(conn)):
        assert row[1 + i] is None
    assert row[-1] == "inverter unreachable"
    conn.close()


def test_out_of_bounds_reading_is_stored_and_flagged(tmp_path: Path) -> None:
    # 25,583 W of battery power is about double what an 18kPV can deliver. It
    # must be stored (evidence of a decode bug) and flagged in invalid_readings.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(tmp_path / "t.db"))
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
    store = SqliteStore(str(tmp_path / "t.db"))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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


def test_module_reading_becoming_valid_clears_its_stale_flag(tmp_path: Path) -> None:
    # If a retry reports a plausible value, the earlier module flag must not
    # linger and imply the reading is still suspect.
    path = tmp_path / "store.db"
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(path))
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
    store = SqliteStore(str(tmp_path / "s.db"))
    store.append(Sample(timestamp=_ts(), readings={"battery_voltage_v": 51.9}))
    rows = store.query(["battery_voltage_v"], _ts(), _ts())
    store.close()
    assert len(rows) == 1
    assert rows[0]["battery_voltage_v"] == pytest.approx(51.9)
    assert rows[0]["timestamp"] == _ts()


def test_query_keeps_absent_distinct_from_zero(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
    store.append(Sample(timestamp=_ts(), readings={"pv_total_power_w": 0.0}))
    rows = store.query(["pv_total_power_w", "battery_soc_pct"], _ts(), _ts())
    store.close()
    assert rows[0]["pv_total_power_w"] == 0.0
    assert rows[0]["battery_soc_pct"] is None


def test_query_respects_the_range_and_orders_by_time(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
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
    store = SqliteStore(str(tmp_path / "s.db"))
    store.append(Sample.failed(_ts(), "inverter unreachable"))
    rows = store.query(["pv_total_power_w"], _ts(), _ts())
    store.close()
    assert rows[0]["error"] == "inverter unreachable"
    assert rows[0]["pv_total_power_w"] is None


def test_unknown_metric_in_query_raises(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
    with pytest.raises(KeyError):
        store.query(["no_such_metric"], _ts(), _ts())
    store.close()


def test_unknown_tier_raises(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
    with pytest.raises(KeyError):
        store.query(["pv_total_power_w"], _ts(), _ts(), tier="nonexistent")
    # Module data has no minute tier.
    with pytest.raises(KeyError):
        store.query_modules(["soc_pct"], _ts(), _ts(), tier="minute")
    store.close()


def test_module_query_is_keyed_by_serial(tmp_path: Path) -> None:
    # Two batteries that occupied slot 1 at different times must come back as
    # two series, identified by serial rather than position.
    store = SqliteStore(str(tmp_path / "s.db"))
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
    store = SqliteStore(str(tmp_path / "s.db"))
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
    store = SqliteStore(str(tmp_path / "s.db"))
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
    store = SqliteStore(str(tmp_path / "s.db"))
    assert store.latest(["pv_total_power_w"]) is None
    assert store.latest_modules(["soc_pct"]) == []
    store.close()


def test_latest_modules_gives_each_module_once(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
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
    store = SqliteStore(str(tmp_path / "s.db"))
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

    store = SqliteStore(str(tmp_path / "threads.db"))
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
