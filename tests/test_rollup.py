"""Tests for rollup maintenance: arraysense.store.rollup.

Each test builds source rows directly into an in-memory database with the
generated schema, calls the rollup, and asserts what the destination table
actually holds. Rollups are the one place a mistake becomes permanent — raw
rows are pruned after 30 days — so the assertions target computed values and
counts, not just presence.
"""

from __future__ import annotations

import sqlite3

from arraysense.store.rollup import (
    rebuild_inverter_hourly,
    rebuild_inverter_minute,
    rebuild_module_hourly,
)
from arraysense.store.schema import FOREIGN_KEYS_PRAGMA, schema_ddl
from conftest import TEST_DEVICE


def _open() -> sqlite3.Connection:
    """Open an in-memory connection with the storage schema laid down."""
    conn = sqlite3.connect(":memory:")
    conn.execute(FOREIGN_KEYS_PRAGMA)
    conn.executescript(schema_ddl())
    return conn


def test_minute_bucket_averages_and_timestamps_at_minute_start() -> None:
    conn = _open()
    # Three readings inside the minute covering [60, 120), at epochs 61-63.
    for sec, power in ((61, 10), (62, 20), (63, 30)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    rebuild_inverter_minute(conn, 60, 120)
    row = conn.execute(
        "SELECT timestamp, pv_total_power_w, sample_count FROM inverter_minute"
    ).fetchone()
    conn.close()
    assert row is not None
    # The bucket lands at 60 (60 // 60 * 60), the start of the minute, never a
    # rounded local time, and the average is the mean of the three readings.
    assert row == (60, 20, 3)


def test_max_and_min_metrics_keep_extremes_not_means() -> None:
    conn = _open()
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (1, '{TEST_DEVICE}', 'CE12345678')"
    )
    # One module, three readings in one hour. cycle_count has the "max" policy,
    # cell_min_voltage_v the "min" policy; soc_pct is a plain mean.
    for sec, cyc, vmin, soc in (
        (3601, 100, 3300, 80),
        (3602, 103, 3250, 82),
        (3603, 101, 3280, 81),
    ):
        conn.execute(
            "INSERT INTO module_raw (timestamp, device, module_id, cycle_count, "
            f"cell_min_voltage_v, soc_pct) VALUES (?, '{TEST_DEVICE}', 1, ?, ?, ?)",
            (sec, cyc, vmin, soc),
        )
    rebuild_module_hourly(conn, 3600, 7200)
    row = conn.execute(
        "SELECT timestamp, sample_count, cycle_count, cell_min_voltage_v, soc_pct "
        "FROM module_hourly"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 3600  # start of the hour [3600, 7200)
    assert row[1] == 3
    assert row[2] == 103  # max, not the mean of 101.33...
    assert row[3] == 3250  # min, not the mean
    assert row[4] == 81  # (80 + 82 + 81) / 3


def test_last_metric_takes_the_latest_value_not_a_mean() -> None:
    conn = _open()
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (1, '{TEST_DEVICE}', 'CE12345678')"
    )
    # cell_max_voltage_num has the "last" policy: the value from the latest
    # source row in the bucket, not the average of several cell numbers.
    for sec, num in ((3601, 5), (3602, 7), (3603, 6)):
        conn.execute(
            f"INSERT INTO module_raw (timestamp, device, module_id, cell_max_voltage_num) "
            f"VALUES (?, '{TEST_DEVICE}', 1, ?)",
            (sec, num),
        )
    rebuild_module_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT cell_max_voltage_num FROM module_hourly").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 6  # the value at the latest timestamp (3603)


def test_sample_count_reflects_source_rows_covered() -> None:
    conn = _open()
    # Two minutes with different numbers of readings: bucket 0 covers two rows,
    # bucket 60 covers one.
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (0, '{TEST_DEVICE}', 5)"
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (1, '{TEST_DEVICE}', 7)"
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (60, '{TEST_DEVICE}', 9)"
    )
    rebuild_inverter_minute(conn, 0, 120)
    rows = conn.execute(
        "SELECT timestamp, sample_count FROM inverter_minute ORDER BY timestamp"
    ).fetchall()
    conn.close()
    assert rows == [(0, 2), (60, 1)]


def test_rebuilding_twice_leaves_identical_rows() -> None:
    conn = _open()
    for sec, power in ((61, 10), (62, 20), (63, 30)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    rebuild_inverter_minute(conn, 60, 120)
    first = conn.execute(
        "SELECT timestamp, pv_total_power_w, sample_count FROM inverter_minute"
    ).fetchall()
    rebuild_inverter_minute(conn, 60, 120)
    second = conn.execute(
        "SELECT timestamp, pv_total_power_w, sample_count FROM inverter_minute"
    ).fetchall()
    conn.close()
    # Same rows, no duplicates, no drift: an upsert alone would not guarantee
    # this, which is why the rebuild deletes and reinserts.
    assert second == first
    assert len(second) == 1


def test_a_bucket_whose_source_rows_are_gone_is_removed() -> None:
    conn = _open()
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (61, '{TEST_DEVICE}', 10)"
    )
    rebuild_inverter_minute(conn, 60, 120)
    assert conn.execute("SELECT COUNT(*) FROM inverter_minute").fetchone()[0] == 1
    # The source row is pruned; a rebuild must drop the now-empty bucket rather
    # than leave it stale.
    conn.execute("DELETE FROM inverter_raw WHERE timestamp = 61")
    rebuild_inverter_minute(conn, 60, 120)
    count = conn.execute("SELECT COUNT(*) FROM inverter_minute").fetchone()[0]
    conn.close()
    assert count == 0


def test_failed_polls_are_excluded_from_the_aggregate() -> None:
    conn = _open()
    # Two successful readings and one failed poll in the same minute. The
    # failed row carries an error reason and no readings; it must not be
    # counted as a reading of zero.
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w, error) "
        f"VALUES (61, '{TEST_DEVICE}', 10, NULL)"
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w, error) "
        f"VALUES (62, '{TEST_DEVICE}', 20, NULL)"
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w, error) "
        f"VALUES (63, '{TEST_DEVICE}', NULL, 'inverter unreachable')"
    )
    rebuild_inverter_minute(conn, 60, 120)
    row = conn.execute("SELECT pv_total_power_w, sample_count FROM inverter_minute").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 15  # (10 + 20) / 2, the failed row is not a zero
    assert row[1] == 2  # only the two successful rows count


def test_module_rollups_group_by_module_not_by_slot() -> None:
    conn = _open()
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (1, '{TEST_DEVICE}', 'CE11111111')"
    )
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (2, '{TEST_DEVICE}', 'CE22222222')"
    )
    # Two modules in one hour. Their SOC readings must never average together;
    # each module gets its own row and its own mean.
    for sec, soc in ((3601, 80), (3602, 82), (3603, 81)):
        conn.execute(
            f"INSERT INTO module_raw (timestamp, device, module_id, soc_pct) "
            f"VALUES (?, '{TEST_DEVICE}', 1, ?)",
            (sec, soc),
        )
    for sec, soc in ((3601, 50), (3602, 52)):
        conn.execute(
            f"INSERT INTO module_raw (timestamp, device, module_id, soc_pct) "
            f"VALUES (?, '{TEST_DEVICE}', 2, ?)",
            (sec, soc),
        )
    rebuild_module_hourly(conn, 3600, 7200)
    rows = conn.execute(
        "SELECT module_id, timestamp, soc_pct, sample_count FROM module_hourly ORDER BY module_id"
    ).fetchall()
    conn.close()
    assert rows == [
        (1, 3600, 81, 3),  # (80 + 82 + 81) / 3
        (2, 3600, 51, 2),  # (50 + 52) / 2
    ]


def test_hourly_tier_averages_all_raw_rows_in_the_hour() -> None:
    conn = _open()
    # Five full-cadence rows across two minutes of the hour. The hourly mean is
    # the direct average of the raw readings — (10+20+30+40+50)/5 = 30 — never
    # a recombination of minute buckets, and the count is the number of raw
    # rows the hour covers.
    for sec, power in ((61, 10), (62, 20), (63, 30)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    for sec, power in ((121, 40), (122, 50)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    rebuild_inverter_hourly(conn, 0, 3600)
    row = conn.execute(
        "SELECT timestamp, pv_total_power_w, sample_count FROM inverter_hourly"
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 0  # start of the hour [0, 3600)
    assert row[1] == 30  # (10+20+30+40+50)/5
    assert row[2] == 5  # five raw rows in the hour


def test_hourly_mean_ignores_rows_that_did_not_report_a_metric() -> None:
    conn = _open()
    # Two rows report battery_soc_pct; three others omitted it, so they carry
    # NULL (absent, never zero). The hourly mean must be the average over the
    # reported rows alone — the NULLs must not drag it toward zero — while the
    # hourly count still reflects every row the hour covers.
    for sec, soc in ((61, 80), (62, 82)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, battery_soc_pct) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, soc),
        )
    for sec in (121, 122, 123):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device) VALUES (?, '{TEST_DEVICE}')", (sec,)
        )
    rebuild_inverter_hourly(conn, 0, 3600)
    row = conn.execute("SELECT battery_soc_pct, sample_count FROM inverter_hourly").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 81  # (80 + 82) / 2, a NULL is not a zero
    assert row[1] == 5  # 2 + 3 rows, the count reflects coverage


def test_hourly_mean_averages_only_the_rows_that_report_a_metric() -> None:
    conn = _open()
    # Two readings of 0 and 10 exist, plus one row that omits the metric. The
    # hourly mean must be the average of the reported readings — (0+10)/2 = 5 —
    # never the old weighted result of 3, and the count reflects all three rows.
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (61, '{TEST_DEVICE}', 0)"
    )
    conn.execute(f"INSERT INTO inverter_raw (timestamp, device) VALUES (62, '{TEST_DEVICE}')")
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (121, '{TEST_DEVICE}', 10)"
    )
    rebuild_inverter_hourly(conn, 0, 3600)
    row = conn.execute("SELECT pv_total_power_w, sample_count FROM inverter_hourly").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 5  # (0 + 10) / 2, the absent row is not a zero
    assert row[1] == 3


def test_hourly_mean_rounds_instead_of_truncating() -> None:
    conn = _open()
    # Two readings of 10 and 11 average to 10.5. The hourly value must be the
    # rounded 11, not the truncated 10: AVG returns a real in SQLite, so the
    # mean is rounded, never integer-divided before rounding.
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (61, '{TEST_DEVICE}', 10)"
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (62, '{TEST_DEVICE}', 11)"
    )
    rebuild_inverter_hourly(conn, 0, 3600)
    row = conn.execute("SELECT pv_total_power_w FROM inverter_hourly").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 11


def test_negative_timestamp_bucket_floors_and_rebuild_is_idempotent() -> None:
    conn = _open()
    # SQLite's / truncates toward zero, so a naive bucket would land epoch -1
    # in bucket 0 — outside the delete range — and a second rebuild would then
    # violate the primary key. Floor division lands it in bucket -60, inside
    # the range, so the rebuild is idempotent.
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (-1, '{TEST_DEVICE}', 5)"
    )
    rebuild_inverter_minute(conn, -120, 0)
    rows = conn.execute("SELECT timestamp, pv_total_power_w FROM inverter_minute").fetchall()
    assert rows == [(-60, 5)]
    rebuild_inverter_minute(conn, -120, 0)  # second run must not raise
    rows = conn.execute("SELECT timestamp, pv_total_power_w FROM inverter_minute").fetchall()
    conn.close()
    assert rows == [(-60, 5)]


def test_last_metric_keeps_a_value_reported_earlier() -> None:
    conn = _open()
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (1, '{TEST_DEVICE}', 'CE12345678')"
    )
    # cell_max_voltage_num is reported at 3601 and 3602 but omitted by the
    # final row (3603). "last" must keep the latest *reported* value — 7 — not
    # the NULL of the last row.
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id, cell_max_voltage_num) "
        f"VALUES (3601, '{TEST_DEVICE}', 1, 5)"
    )
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id, cell_max_voltage_num) "
        f"VALUES (3602, '{TEST_DEVICE}', 1, 7)"
    )
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id) VALUES (3603, '{TEST_DEVICE}', 1)"
    )
    rebuild_module_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT cell_max_voltage_num FROM module_hourly").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 7


OTHER_DEVICE = "CE00000001"


def test_two_inverters_never_average_into_one_row() -> None:
    # The coarse tiers outlive the raw rows behind them, so a bucket holding
    # the mean of two machines could never be told apart from a real reading
    # and no later pass would undo it. Two devices, two rows, each its own mean.
    conn = _open()
    for sec, power in ((61, 1000), (62, 2000)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    for sec, power in ((61, 6000), (62, 8000)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{OTHER_DEVICE}', ?)",
            (sec, power),
        )
    rebuild_inverter_minute(conn, 60, 120)
    rows = conn.execute(
        "SELECT device, pv_total_power_w, sample_count FROM inverter_minute ORDER BY device"
    ).fetchall()
    conn.close()
    assert rows == [(TEST_DEVICE, 1500, 2), (OTHER_DEVICE, 7000, 2)]


def test_a_last_metric_does_not_borrow_the_other_inverters_value() -> None:
    # One unit stops reporting the metric partway through the bucket. The
    # "last" policy must fall back to that unit's own earlier reading, never to
    # whichever unit happened to report most recently.
    conn = _open()
    for device in (TEST_DEVICE, OTHER_DEVICE):
        conn.execute(
            "INSERT INTO serials (device, serial) VALUES (?, 'BM01')",
            (device,),
        )
    ids = dict(conn.execute("SELECT device, id FROM serials").fetchall())
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id, cell_max_voltage_num) "
        f"VALUES (3601, '{TEST_DEVICE}', ?, 5)",
        (ids[TEST_DEVICE],),
    )
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id) VALUES (3602, '{TEST_DEVICE}', ?)",
        (ids[TEST_DEVICE],),
    )
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id, cell_max_voltage_num) "
        f"VALUES (3602, '{OTHER_DEVICE}', ?, 9)",
        (ids[OTHER_DEVICE],),
    )
    rebuild_module_hourly(conn, 3600, 7200)
    rows = conn.execute(
        "SELECT device, cell_max_voltage_num FROM module_hourly ORDER BY device"
    ).fetchall()
    conn.close()
    assert rows == [(TEST_DEVICE, 5), (OTHER_DEVICE, 9)]


def test_one_inverters_failed_polls_do_not_erase_the_others_bucket() -> None:
    # A failed poll is stored with a reason and no readings. Excluding it must
    # be per row, not per instant: the unit that answered still has an hour.
    conn = _open()
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, error) "
        f"VALUES (61, '{TEST_DEVICE}', 'inverter unreachable')"
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (61, '{OTHER_DEVICE}', 4000)"
    )
    rebuild_inverter_minute(conn, 60, 120)
    rows = conn.execute("SELECT device, pv_total_power_w FROM inverter_minute").fetchall()
    conn.close()
    assert rows == [(OTHER_DEVICE, 4000)]
