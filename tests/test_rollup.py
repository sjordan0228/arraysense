"""Tests for rollup maintenance: arraysense.store.rollup.

Each test builds source rows directly into an in-memory database with the
generated schema, calls the rollup, and asserts what the destination table
actually holds. Rollups are the one place a mistake becomes permanent — raw
rows are pruned after 30 days — so the assertions target computed values and
counts, not just presence.
"""

from __future__ import annotations

import sqlite3

from arraysense.metrics import INVERTER_METRICS, lookup
from arraysense.store.rollup import (
    LATE_APPEND_SECONDS,
    collapse_policy,
    is_energy_counter,
    merge_site_hours,
    pack_scale,
    promote_pending_hours,
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


def test_a_counter_row_holds_the_value_its_timestamp_claims() -> None:
    # A row stamped 3600 says it describes the instant 3600. Collapsing the
    # counter with max put the value from the *end* of the hour there instead,
    # so every counter reading sat one bucket later than its label — the same
    # off-by-one hour the importer was written to avoid, in the same column.
    conn = _open()
    # pv_energy_total_kwh is scaled by 10: 36000 is 3600.0 kWh.
    for sec, stored in ((3601, 36000), (3602, 36005), (3659, 36011)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT timestamp, pv_energy_total_kwh FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (3600, 36000)


def test_a_daily_counter_reads_zero_on_the_hour_its_day_begins() -> None:
    # The daily counters reset at midnight. The 23:00 row must hold the total
    # the day had reached by 23:00, and the 00:00 row of the next day must read
    # zero, because at midnight nothing has been generated yet. Under max the
    # 00:00 row held a whole hour of the new day's production.
    conn = _open()
    rows = ((82_800, 9500), (86_390, 9800), (86_401, 0), (89_990, 300))
    for sec, stored in rows:
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_today_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_hourly(conn, 82_800, 90_000)
    got = conn.execute(
        "SELECT timestamp, pv_energy_today_kwh FROM inverter_hourly ORDER BY timestamp"
    ).fetchall()
    conn.close()
    assert got == [(82_800, 9500), (86_400, 0)]


def test_a_fault_code_still_keeps_the_maximum_in_its_bucket() -> None:
    # The counter convention must not sweep every "max" metric along with it. A
    # fault raised for ten seconds and cleared has to survive into the coarse
    # tiers, which is the whole reason bitfields collapse with max.
    conn = _open()
    for sec, code in ((3601, 0), (3602, 12), (3603, 0)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, inverter_fault_code) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, code),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT inverter_fault_code FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (12,)


def test_an_energy_counter_collapses_to_its_opening_value() -> None:
    # collapse_policy is the one place the decision is made, so the assertion
    # is on it rather than on a list of names repeated here.
    counters = [spec for spec in INVERTER_METRICS if spec.unit == "kWh"]
    assert len(counters) == 24, "every kWh metric in the registry is a counter"
    for spec in counters:
        assert collapse_policy(spec) == "min", spec.name


def test_nothing_but_a_counter_has_its_registry_policy_overridden() -> None:
    for spec in INVERTER_METRICS:
        if spec.unit != "kWh":
            assert collapse_policy(spec) == spec.aggregation, spec.name


def test_a_counter_that_dips_mid_bucket_keeps_its_opening_value() -> None:
    # A counter that opens at 1000.0 kWh, misdecodes to 900.0 mid-hour and
    # recovers to 1000.5 has to roll up as 1000.0. Taking the smallest reading
    # stores the glitch, and because the hourly tier is kept for ever the
    # phantom 100 kWh would show up as a hole in that hour and a spike in the
    # next, permanently.
    conn = _open()
    for sec, stored in ((3601, 10_000), (3602, 9_000), (3659, 10_005)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT pv_energy_total_kwh FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (10_000,)


def test_a_counter_that_resets_mid_bucket_keeps_the_value_before_the_reset() -> None:
    # The daily counters restart at the owner's midnight, which lands inside an
    # hourly bucket in any zone offset by a half hour. That is a real reset, not
    # a glitch, and it is handled by the same rule rather than by a special
    # case: the row is stamped at the start of its hour, so it has to hold what
    # the counter read then — the day's production so far, not the zero it
    # dropped to later. Reading it as the smallest value claims the day had
    # generated nothing an hour before midnight, and moves the reset a whole
    # bucket earlier than it happened.
    conn = _open()
    for sec, stored in ((3601, 9_500), (5_000, 0), (5_100, 300)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_today_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT pv_energy_today_kwh FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (9_500,)


def test_a_dipping_counter_keeps_its_opening_value_in_the_minute_tier_too() -> None:
    # The opening reading is found from each row's offset into its own bucket,
    # so a bucket length the expression was not written against would silently
    # pick the wrong row. Sixty seconds rather than thirty-six hundred.
    conn = _open()
    for sec, stored in ((60, 10_000), (75, 9_000), (119, 10_002)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_minute(conn, 60, 120)
    row = conn.execute("SELECT timestamp, pv_energy_total_kwh FROM inverter_minute").fetchone()
    conn.close()
    assert row == (60, 10_000)


def test_a_counters_opening_value_is_its_earliest_reported_one() -> None:
    # An inverter can answer a poll without answering the energy registers, and
    # the row it wrote carries NULL for the counter. The bucket must open at the
    # earliest reading that exists rather than at the earliest row, which has
    # nothing on it — the same distinction the "last" policy rests on, at the
    # other end of the hour.
    conn = _open()
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (3601, '{TEST_DEVICE}', 4000)"
    )
    for sec, stored in ((3602, 10_000), (3603, 9_000)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT pv_energy_total_kwh FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (10_000,)


def test_a_counter_before_the_epoch_keeps_its_opening_value() -> None:
    # A row's offset into its bucket is derived from the same floored division
    # the bucket itself is, so it stays inside [0, bucket) below zero as well.
    # An offset that went negative there would rank the readings backwards and
    # hand the bucket its closing value.
    conn = _open()
    for sec, stored in ((-59, 10_000), (-30, 9_000), (-1, 10_002)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_minute(conn, -60, 0)
    row = conn.execute("SELECT timestamp, pv_energy_total_kwh FROM inverter_minute").fetchone()
    conn.close()
    assert row == (-60, 10_000)


def test_one_inverters_glitch_cannot_open_the_others_bucket() -> None:
    # The opening reading is picked per group, and the group is the bucket and
    # the device. Two machines reporting at the same instants must each keep
    # their own opening value however far apart the readings are.
    conn = _open()
    for device, stored in ((TEST_DEVICE, 10_000), (OTHER_DEVICE, 20_000)):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) VALUES (3601, ?, ?)",
            (device, stored),
        )
    for device, stored in ((TEST_DEVICE, 9_000), (OTHER_DEVICE, 19_000)):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) VALUES (3602, ?, ?)",
            (device, stored),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    rows = conn.execute(
        "SELECT device, pv_energy_total_kwh FROM inverter_hourly ORDER BY device"
    ).fetchall()
    conn.close()
    assert rows == [(TEST_DEVICE, 10_000), (OTHER_DEVICE, 20_000)]


def test_every_plausible_counter_reading_fits_the_packing() -> None:
    # The earliest reading is found by ranking one packed integer per row, with
    # the reading in the low bits and its offset into the bucket above them, and
    # only rows the packing can carry are eligible. That guard has to exclude
    # nothing a working inverter reports, so the whole of every counter's
    # registry range must sit inside it — a metric added with a wider bound than
    # the multiplier would have its highest readings quietly skipped.
    for spec in INVERTER_METRICS:
        if not is_energy_counter(spec):
            continue
        assert spec.lower >= 0.0, spec.name
        assert spec.encode(spec.upper) < pack_scale(spec), spec.name


def test_a_misdecoded_counter_cannot_become_a_buckets_opening_value() -> None:
    # An implausible reading is stored and flagged in invalid_readings rather
    # than rejected, so a misdecoded register really does reach the raw tier.
    # Both directions break the packing if allowed into it — a negative unpacks
    # with the wrong sign, an enormous one overflows into the offset — and
    # either would leave a fabricated number in the tier kept for ever. The
    # bucket must open on the earliest reading the packing can carry.
    conn = _open()
    huge = pack_scale(lookup("pv_energy_total_kwh")) + 5
    for sec, stored in ((3601, -700), (3602, huge), (3603, 36_000), (3604, 36_004)):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, stored),
        )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT pv_energy_total_kwh FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (36_000,)


def test_a_bucket_holding_nothing_but_a_misdecode_reports_no_counter() -> None:
    # Nothing usable was read, so nothing is claimed. Storing the misdecode
    # would hand every consumer of the hourly tier a counter that jumps by
    # millions of kWh and back, and energy.py would bill the jump to a day.
    conn = _open()
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, device, pv_energy_total_kwh) "
        f"VALUES (3601, '{TEST_DEVICE}', -700)"
    )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT pv_energy_total_kwh, sample_count FROM inverter_hourly").fetchone()
    conn.close()
    # The row still exists and still counts the poll: the poll happened.
    assert row == (None, 1)


def test_an_absent_counter_reading_stays_absent() -> None:
    # A bucket where the inverter reported no counter at all must roll up NULL,
    # not zero. Collapsing with min is one careless COALESCE away from turning
    # a missing lifetime total into a system that has never generated anything.
    conn = _open()
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (3601, '{TEST_DEVICE}', 4000)"
    )
    rebuild_inverter_hourly(conn, 3600, 7200)
    row = conn.execute("SELECT pv_energy_total_kwh FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (None,)


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


def test_rollups_cover_a_schema_narrowed_to_a_declaration() -> None:
    # A database created for a driver that declares a subset has columns for
    # that subset alone. The rebuilds must roll up what the tables hold rather
    # than what the registry could hold, or the first maintenance pass on such
    # an installation names columns that do not exist and dies.
    conn = sqlite3.connect(":memory:")
    conn.execute(FOREIGN_KEYS_PRAGMA)
    conn.executescript(schema_ddl(frozenset({"pv_total_power_w", "battery_module1_soc_pct"})))
    for sec, power in ((61, 10), (62, 20), (63, 30)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (1, '{TEST_DEVICE}', 'BA00000001')"
    )
    for sec, soc in ((3660, 60), (3720, 62)):
        conn.execute(
            f"INSERT INTO module_raw (timestamp, device, module_id, soc_pct) "
            f"VALUES (?, '{TEST_DEVICE}', 1, ?)",
            (sec, soc),
        )
    rebuild_inverter_minute(conn, 60, 120)
    rebuild_inverter_hourly(conn, 0, 3600)
    rebuild_module_hourly(conn, 3600, 7200)
    minute = conn.execute("SELECT timestamp, pv_total_power_w FROM inverter_minute").fetchone()
    hourly = conn.execute("SELECT timestamp, pv_total_power_w FROM inverter_hourly").fetchone()
    module = conn.execute("SELECT timestamp, soc_pct FROM module_hourly").fetchone()
    conn.close()
    assert minute == (60, 20)
    assert hourly == (0, 20)
    assert module == (3600, 61)


def test_a_declaration_with_no_module_templates_rolls_up_without_error() -> None:
    # A device that reports only a bank-level summary declares no per-module
    # template at all — drivers/base.py allows that on purpose. Its module
    # tables then hold no metric columns, and a rebuild that still assembled
    # SQL for them produced "COUNT(*) AS sample_count,  FROM ..." — a syntax
    # error raised by every sixty-second maintenance pass for the life of the
    # service. Nothing to roll up must mean no rebuild, not a broken one.
    conn = sqlite3.connect(":memory:")
    conn.execute(FOREIGN_KEYS_PRAGMA)
    conn.executescript(schema_ddl(frozenset({"pv_total_power_w", "battery_soc_pct"})))
    for sec, power in ((61, 10), (62, 20), (63, 30)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
            f"VALUES (?, '{TEST_DEVICE}', ?)",
            (sec, power),
        )
    rebuild_module_hourly(conn, 0, 7200)  # must not raise
    rebuild_inverter_minute(conn, 60, 120)  # the inverter side still rolls up
    minute = conn.execute("SELECT timestamp, pv_total_power_w FROM inverter_minute").fetchone()
    modules = conn.execute("SELECT COUNT(*) FROM module_hourly").fetchone()[0]
    conn.close()
    assert minute == (60, 20)
    assert modules == 0


def test_a_declaration_with_no_inverter_metrics_rolls_up_without_error() -> None:
    # The same failure from the other side: a declaration holding only module
    # templates leaves the inverter tiers without a single metric column.
    conn = sqlite3.connect(":memory:")
    conn.execute(FOREIGN_KEYS_PRAGMA)
    conn.executescript(schema_ddl(frozenset({"battery_module1_soc_pct"})))
    conn.execute(
        f"INSERT INTO serials (id, device, serial) VALUES (1, '{TEST_DEVICE}', 'BA00000001')"
    )
    conn.execute(
        f"INSERT INTO module_raw (timestamp, device, module_id, soc_pct) "
        f"VALUES (3660, '{TEST_DEVICE}', 1, 60)"
    )
    rebuild_inverter_minute(conn, 60, 120)  # must not raise
    rebuild_inverter_hourly(conn, 0, 3600)  # must not raise
    rebuild_module_hourly(conn, 3600, 7200)  # the module side still rolls up
    module = conn.execute("SELECT timestamp, soc_pct FROM module_hourly").fetchone()
    inverter = conn.execute("SELECT COUNT(*) FROM inverter_minute").fetchone()[0]
    conn.close()
    assert module == (3600, 60)
    assert inverter == 0


# --- folding a backfilled hour into a tier that outlives its source -----------


def test_merging_site_hours_writes_only_the_sky_columns() -> None:
    # An hourly row from before the raw tier's retention window holds readings
    # raw can no longer supply. Writing an hour's weather into it must leave
    # every one of them exactly as it stands, or a backfill would trade a year
    # of production history for one temperature.
    conn = _open()
    hour = 1_700_000_000 // 3600 * 3600
    conn.execute(
        f"INSERT INTO inverter_hourly (timestamp, device, sample_count, pv_total_power_w) "
        f"VALUES (?, '{TEST_DEVICE}', 300, 8000)",
        (hour,),
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, ghi_wm2) VALUES (?, '{TEST_DEVICE}', 500)",
        (hour,),
    )
    merge_site_hours(conn, [hour])
    row = conn.execute(
        "SELECT pv_total_power_w, ghi_wm2, sample_count FROM inverter_hourly"
    ).fetchone()
    conn.close()
    assert row == (8000, 500, 300)


def test_merging_does_not_erase_a_sky_column_raw_cannot_answer() -> None:
    # The archive answers one hour in two pieces — the means over the hour and
    # the readings at its label — and they arrive as separate requests. The
    # second must not blank what the first recorded, so a metric no raw row in
    # the hour carries leaves the stored value alone. Raw is a thirty-day
    # window; absence in it says nothing about an hour outside it.
    conn = _open()
    hour = 1_700_000_000 // 3600 * 3600
    conn.execute(
        f"INSERT INTO inverter_hourly (timestamp, device, sample_count, ghi_wm2) "
        f"VALUES (?, '{TEST_DEVICE}', 1, 500)",
        (hour,),
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, outside_temperature_c) "
        f"VALUES (?, '{TEST_DEVICE}', 250)",
        (hour,),
    )
    merge_site_hours(conn, [hour])
    row = conn.execute("SELECT ghi_wm2, outside_temperature_c FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (500, 250)


def test_merging_leaves_an_hour_with_no_sky_reading_untouched() -> None:
    # An hour holding nothing but inverter polls must not be rewritten at all —
    # not even to the same values — because the row may be older than every raw
    # row that built it.
    conn = _open()
    hour = 1_700_000_000 // 3600 * 3600
    conn.execute(
        f"INSERT INTO inverter_hourly (timestamp, device, sample_count, ghi_wm2) "
        f"VALUES (?, '{TEST_DEVICE}', 300, 700)",
        (hour,),
    )
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, pv_total_power_w) "
        f"VALUES (?, '{TEST_DEVICE}', 9000)",
        (hour,),
    )
    merge_site_hours(conn, [hour])
    row = conn.execute("SELECT ghi_wm2, sample_count FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (700, 300)


def test_merging_averages_the_hour_and_stamps_the_bucket_start() -> None:
    conn = _open()
    hour = 1_700_000_000 // 3600 * 3600
    for offset, ghi in ((0, 400), (1800, 600)):
        conn.execute(
            f"INSERT INTO inverter_raw (timestamp, device, ghi_wm2) VALUES (?, '{TEST_DEVICE}', ?)",
            (hour + offset, ghi),
        )
    merge_site_hours(conn, [hour + 1800])
    row = conn.execute("SELECT timestamp, ghi_wm2, sample_count FROM inverter_hourly").fetchone()
    conn.close()
    assert row == (hour, 500, 2)


def test_a_queued_hour_is_promoted_once_and_then_forgotten() -> None:
    conn = _open()
    hour = 1_700_000_000 // 3600 * 3600
    conn.execute(
        f"INSERT INTO inverter_raw (timestamp, device, ghi_wm2) VALUES (?, '{TEST_DEVICE}', 300)",
        (hour,),
    )
    conn.execute("INSERT INTO rollup_pending (hour) VALUES (?)", (hour,))
    conn.commit()
    assert promote_pending_hours(conn) == 1
    assert conn.execute("SELECT COUNT(*) FROM rollup_pending").fetchone()[0] == 0
    assert promote_pending_hours(conn) == 0
    conn.close()


def test_the_late_write_threshold_sits_inside_the_rebuild_window() -> None:
    # The queue covers exactly what the maintenance rebuild does not reach. If
    # the threshold ever grew past the window, hours between the two would be
    # promoted by neither and the archive backfill would go quiet again.
    from arraysense.collector.service import HOURLY_REBUILD_WINDOW

    assert LATE_APPEND_SECONDS < HOURLY_REBUILD_WINDOW
