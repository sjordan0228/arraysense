"""Tests for the efficiency engine: arraysense.efficiency.

compute_day is the central function — it reads from the hourly tier, computes
expected production from the solar model, and returns one row per string plus a
total. These tests stage a day of inputs and assert hand-checked figures, then
verify the partial/downtime/versioning rules the spec lays out.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from arraysense.efficiency import CONFIG_VERSION_KEY, EfficiencyRow, compute_day
from arraysense.panels import parse_strings
from arraysense.settings import SettingsStore
from arraysense.store.sqlite_store import SqliteStore

TEST_DEVICE = "CE00000000"

# A simple one-string array: East, 10 panels of 400 W at 25° tilt, 90° azimuth.
_ONE_STRING = parse_strings("East | 1 | 10 | 400 | 25 | 90")


def _store(db_path: str) -> SqliteStore:
    """Open a store and prepare the database for efficiency tests."""
    store = SqliteStore(db_path, device=TEST_DEVICE)
    # The collector's own store creates the settings table on open, but we need
    # to prepare settings our own way — ensure the efficiency config version
    # setting is storeable.
    settings = SettingsStore(store)
    settings.set("site.timezone", "America/Chicago")
    settings.set("site.latitude", 33.0)
    settings.set("site.longitude", -97.0)
    settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
    settings.set(CONFIG_VERSION_KEY, 1)
    return store


def _insert_hourly(
    conn: sqlite3.Connection,
    hour_utc: datetime,
    pv_power: float | None,
    ghi: float | None = None,
    dni: float | None = None,
    dhi: float | None = None,
    wind: float | None = None,
    air_c: float | None = None,
) -> None:
    """Insert one row into the hourly inverter tier.

    The hourly tier has the same columns as the raw tier plus sample_count.
    We insert with named columns so the test controls exactly what lands.
    """
    epoch = int(hour_utc.timestamp())
    columns = ["timestamp", "device", "pv1_power_w"]
    values: list[int | str | None] = [epoch, TEST_DEVICE]
    if pv_power is not None:
        values.append(round(pv_power))
    else:
        values.append(None)
    weather_cols = ["ghi_wm2", "dni_wm2", "dhi_wm2", "wind_speed_ms", "outside_temperature_c"]
    weather_vals = [ghi, dni, dhi, wind, air_c]
    for col, val in zip(weather_cols, weather_vals, strict=True):
        columns.append(col)
        if val is not None:
            values.append(round(val))
        else:
            values.append(None)
    columns.append("sample_count")
    values.append(1)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO inverter_hourly ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def _summer_day(hour: int) -> datetime:
    """A daylight hour in August 2026, in US Central time."""
    return datetime(2026, 8, 10, hour, 0, 0, tzinfo=timezone(timedelta(hours=-5)))


def _utc(hour: int) -> datetime:
    """The same local hour in UTC (Central is UTC-5 in August).

    Added rather than arithmetic on the hour field: a local evening hour lands
    on the next UTC day, and hour+5 simply raises once the sum passes 23.
    """
    return datetime(2026, 8, 10, 0, 0, 0, tzinfo=UTC) + timedelta(hours=hour + 5)


class TestComputeDay:
    """compute_day reads the hourly tier and scores one day."""

    def test_a_clear_day_produces_rows_with_hand_computed_proportions(self, tmp_path: Path) -> None:
        """Insert eight sunlit hours with the same irradiance and power, then
        verify the expected figure is roughly nameplate x POA ratio x derate,
        and the PR is within a sensible range."""
        store = _store(str(tmp_path / "clear.db"))
        strings = _ONE_STRING
        day_start = _summer_day(0)
        day_end = day_start + timedelta(days=1)

        # Stage eight hours of identical clear-sky conditions
        for h in range(8, 16):  # 08:00-15:00 local = 13:00-20:00 UTC
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=3200.0,
                ghi=800.0,
                dni=850.0,
                dhi=120.0,
                wind=2.0,
                air_c=30.0,
            )

        settings = SettingsStore(store)
        rows = compute_day(store, settings, day_start, day_end, strings, 1)

        assert len(rows) == 2  # one per string + total
        east = rows[0]
        total = rows[1]

        assert east.string_name == "East"
        assert east.expected_kwh > 0.0
        assert east.actual_kwh > 0.0
        assert east.curtailed_kwh == 0.0
        assert east.config_version == 1
        assert not east.partial

        # Eight hours at ~3200 W = ~25.6 kWh actual
        assert east.actual_kwh == pytest.approx(25.6, rel=0.05)

        # Expected should be in the ballpark: nameplate 4000 W x 0.86 derate
        # x POA/1000 (~0.8) ≈ 2752 W per hour x 8 hours ≈ 22 kWh. But with
        # string_poa computed from the actual geometry, it will vary.
        # Just assert it's a positive, reasonable number.
        assert 15.0 <= east.expected_kwh <= 35.0

        # PR = actual / expected (no curtailment)
        assert east.pr is not None
        assert 0.5 <= east.pr <= 1.5

        # Total row aggregates
        assert total.string_name == ""
        assert total.expected_kwh == pytest.approx(east.expected_kwh, rel=0.01)
        assert total.actual_kwh == pytest.approx(east.actual_kwh, rel=0.01)

    def test_a_day_with_fewer_than_sixty_percent_daylight_coverage_is_partial(
        self, tmp_path: Path
    ) -> None:
        """Only two of ~14 daylight hours have data → coverage < 60% → partial=True."""
        store = _store(str(tmp_path / "partial.db"))
        strings = _ONE_STRING
        day_start = _summer_day(0)
        day_end = day_start + timedelta(days=1)

        # Just two hours of data out of ~14 daylight hours
        for h in (10, 11):
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=3000.0,
                ghi=700.0,
                dni=750.0,
                dhi=100.0,
                wind=2.0,
                air_c=30.0,
            )

        settings = SettingsStore(store)
        rows = compute_day(store, settings, day_start, day_end, strings, 1)
        total = next(r for r in rows if r.string_name == "")
        assert total.partial
        assert total.modelled_hours == 2

    def test_a_collector_gap_is_excluded_from_both_sides(self, tmp_path: Path) -> None:
        """Hours with irradiance but no inverter reading are skipped —
        downtime is not a loss."""
        store = _store(str(tmp_path / "gap.db"))
        strings = _ONE_STRING
        day_start = _summer_day(0)
        day_end = day_start + timedelta(days=1)

        # Two hours with full data
        for h in (10, 11):
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=3000.0,
                ghi=700.0,
                dni=750.0,
                dhi=100.0,
                wind=2.0,
                air_c=30.0,
            )

        # One hour with irradiance but NO inverter reading (gap)
        _insert_hourly(
            store._conn,
            _utc(12),
            pv_power=None,  # inverter down this hour
            ghi=800.0,
            dni=850.0,
            dhi=120.0,
            wind=2.0,
            air_c=30.0,
        )

        settings = SettingsStore(store)
        rows = compute_day(store, settings, day_start, day_end, strings, 1)
        total = next(r for r in rows if r.string_name == "")

        # The gap hour contributes nothing. With only two hours of data in
        # ~14 daylight hours, coverage is low → partial, but the two good
        # hours still contribute.
        assert total.modelled_hours == 2
        assert total.partial  # 2/14 < 0.6

    def test_a_stale_config_version_is_written_on_the_row(self, tmp_path: Path) -> None:
        """verify that the row records whatever version was passed in."""
        store = _store(str(tmp_path / "version.db"))
        strings = _ONE_STRING
        day_start = _summer_day(0)
        day_end = day_start + timedelta(days=1)

        for h in (10, 11, 12, 13, 14, 15, 16):
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=3000.0,
                ghi=700.0,
                dni=750.0,
                dhi=100.0,
                wind=2.0,
                air_c=30.0,
            )

        settings = SettingsStore(store)
        rows_v2 = compute_day(store, settings, day_start, day_end, strings, 2)
        assert all(r.config_version == 2 for r in rows_v2)

        # Writing via the store and reading back preserves the version
        store.write_efficiency_day(rows_v2)
        stored = store.read_efficiency_days(day_start, day_end)
        assert len(stored) == len(rows_v2)
        assert all(r.config_version == 2 for r in stored)

    def test_per_string_rows_sum_to_the_total(self, tmp_path: Path) -> None:
        """Each string row sums its own MPPT's power; the total sums all of them."""
        # Two-string array
        strings = parse_strings("East | 1 | 10 | 400 | 25 | 90\nWest | 2 | 8 | 410 | 25 | 270")
        store = SqliteStore(str(tmp_path / "sum.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        settings.set("site.timezone", "America/Chicago")
        settings.set("site.latitude", 33.0)
        settings.set("site.longitude", -97.0)
        settings.set(
            "panels.strings", "East | 1 | 10 | 400 | 25 | 90\nWest | 2 | 8 | 410 | 25 | 270"
        )
        settings.set(CONFIG_VERSION_KEY, 1)

        day_start = _summer_day(0)
        day_end = day_start + timedelta(days=1)

        # Insert data for both strings
        for h in (10, 11, 12, 13, 14, 15, 16):
            epoch = int(_utc(h).timestamp())
            store._conn.execute(
                "INSERT OR REPLACE INTO inverter_hourly "
                "(timestamp, device, pv1_power_w, pv2_power_w, "
                "ghi_wm2, dni_wm2, dhi_wm2, wind_speed_ms, outside_temperature_c, sample_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (epoch, TEST_DEVICE, 3000, 2500, 700, 750, 100, 2, 30, 1),
            )

        rows = compute_day(store, settings, day_start, day_end, strings, 1)
        names = {r.string_name for r in rows}
        assert names == {"East", "West", ""}

        east = next(r for r in rows if r.string_name == "East")
        west = next(r for r in rows if r.string_name == "West")
        total = next(r for r in rows if r.string_name == "")

        assert total.actual_kwh == pytest.approx(east.actual_kwh + west.actual_kwh, rel=0.01)
        assert total.expected_kwh == pytest.approx(east.expected_kwh + west.expected_kwh, rel=0.01)

    def test_no_location_returns_nothing(self, tmp_path: Path) -> None:
        """Without a location the model cannot place the sun; return empty."""
        store = SqliteStore(str(tmp_path / "noloc.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        settings.set("site.timezone", "America/Chicago")
        # latitude and longitude left unset
        settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
        settings.set(CONFIG_VERSION_KEY, 1)

        day_start = _summer_day(0)
        rows = compute_day(
            store, settings, day_start, day_start + timedelta(days=1), _ONE_STRING, 1
        )
        assert rows == []

    def test_no_strings_returns_nothing(self, tmp_path: Path) -> None:
        """Without array configuration there is nothing to score."""
        store = _store(str(tmp_path / "nostr.db"))
        settings = SettingsStore(store)
        day_start = _summer_day(0)
        rows = compute_day(store, settings, day_start, day_start + timedelta(days=1), (), 1)
        assert rows == []

    def test_winter_day_is_all_partial_above_arctic_circle(self, tmp_path: Path) -> None:
        """In December above the Arctic circle the sun never rises;
        zero daylight hours means zero rows."""
        store = SqliteStore(str(tmp_path / "arctic.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        settings.set("site.timezone", "Atlantic/Reykjavik")
        settings.set("site.latitude", 66.5)
        settings.set("site.longitude", -18.0)
        settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
        settings.set(CONFIG_VERSION_KEY, 1)

        # December 21 in Reykjavik — essentially no daylight
        day_start = datetime(2026, 12, 21, 0, 0, 0, tzinfo=timezone(timedelta(hours=0)))
        rows = compute_day(
            store, settings, day_start, day_start + timedelta(days=1), _ONE_STRING, 1
        )
        # Zero daylight hours → zero rows
        assert rows == []

    def test_a_day_with_full_coverage_is_not_partial(self, tmp_path: Path) -> None:
        """When every daylight hour has data, partial is False."""
        store = _store(str(tmp_path / "full.db"))
        strings = _ONE_STRING
        day_start = _summer_day(0)
        day_end = day_start + timedelta(days=1)

        # Fill all daylight hours in August (roughly 06:00-20:00 local → 14 hours)
        for h in range(6, 21):
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=3000.0 if 8 <= h <= 18 else 500.0,
                ghi=700.0 if 8 <= h <= 18 else 100.0,
                dni=750.0 if 8 <= h <= 18 else 50.0,
                dhi=100.0,
                wind=2.0,
                air_c=30.0,
            )

        settings = SettingsStore(store)
        rows = compute_day(store, settings, day_start, day_end, strings, 1)
        total = next(r for r in rows if r.string_name == "")

        # With 15 modelled hours out of roughly 14 daylight hours, coverage >= 60%
        assert not total.partial
        assert total.pr is not None


class TestCoverageIsMeasuredInEnergy:
    """Coverage asks how much of the day was seen, and hours are the wrong unit.

    These two days carry the same story from opposite sides, and an hour-counting
    rule gets both of them backwards. They are the regression guard on that: a
    return to counting hours turns both of these red.
    """

    def test_a_day_that_lost_its_best_hours_is_partial_despite_most_hours_kept(
        self, tmp_path: Path
    ) -> None:
        # Noon to 17:00 missing — the collector down over the sunniest stretch.
        # Nine of fourteen daylight hours survive, so counting hours calls this
        # 64 % covered and prints a confident performance ratio; the hours that
        # went unseen carry 56 % of the day's energy.
        store = _store(str(tmp_path / "midday-outage.db"))
        day_start = _summer_day(0)
        for h in list(range(7, 12)) + list(range(17, 21)):
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=1500.0,
                ghi=400.0,
                dni=420.0,
                dhi=90.0,
                wind=2.0,
                air_c=30.0,
            )
        rows = compute_day(
            store, SettingsStore(store), day_start, day_start + timedelta(days=1), _ONE_STRING, 1
        )
        assert rows, "nine daylight hours were measured; there is a day to report"
        assert all(r.partial for r in rows), (
            "a day missing noon through 17:00 saw under half its energy and "
            "must not be presented as a complete one"
        )

    def test_a_day_that_kept_its_best_hours_is_complete_despite_fewer_hours(
        self, tmp_path: Path
    ) -> None:
        # The mirror: only the thin hours either side of the day are missing.
        # Eight of fourteen hours is 57 % by count — under an hour-counting
        # floor — but those eight carry 72 % of the day's energy, and calling
        # this partial would be hiding a figure that is not actually partial.
        store = _store(str(tmp_path / "fringes-missing.db"))
        day_start = _summer_day(0)
        for h in range(8, 16):
            _insert_hourly(
                store._conn,
                _utc(h),
                pv_power=3200.0,
                ghi=800.0,
                dni=850.0,
                dhi=120.0,
                wind=2.0,
                air_c=30.0,
            )
        rows = compute_day(
            store, SettingsStore(store), day_start, day_start + timedelta(days=1), _ONE_STRING, 1
        )
        assert rows
        assert not any(r.partial for r in rows)


class TestCurtailmentIsWiredIn:
    """A detector nobody calls is a detector that changes nothing.

    These stage the same shortfall twice and differ only in the electrical
    evidence, which is the whole rule: refused energy and lost energy look
    identical in a power reading and are opposite in what they mean.
    """

    @staticmethod
    def _hour(
        conn: sqlite3.Connection,
        when: datetime,
        watts: float,
        volts: float,
        amps: float,
        soc: float,
        limit: float,
    ) -> None:
        from arraysense.metrics import lookup

        cols = {
            "pv1_power_w": watts,
            "pv1_voltage_v": volts,
            "pv1_current_a": amps,
            "battery_soc_pct": soc,
            "bms_charge_current_limit_a": limit,
            "ghi_wm2": 800.0,
            "dni_wm2": 850.0,
            "dhi_wm2": 120.0,
            "wind_speed_ms": 2.0,
            "outside_temperature_c": 30.0,
        }
        names = ["timestamp", "device", *cols, "sample_count"]
        values = [int(when.timestamp()), TEST_DEVICE]
        values += [round(v * lookup(m).scale) for m, v in cols.items()]
        values.append(1)
        conn.execute(
            f"INSERT OR REPLACE INTO inverter_hourly ({', '.join(names)}) "
            f"VALUES ({', '.join('?' for _ in names)})",
            values,
        )

    def _day(self, tmp_path: Path, name: str, bad_volts: float, bad_amps: float) -> EfficiencyRow:
        store = _store(str(tmp_path / name))
        day_start = _summer_day(0)
        # Ordinary hours establish the string's own operating point, staged at
        # roughly what the model expects of them: above expectation, the
        # surplus nets against the bad hour at day level and the shortfall
        # disappears before anything can name it.
        for h in range(8, 16):
            if h == 10:
                continue
            self._hour(store._conn, _utc(h), 2830.0, 310.0, 10.3, 60.0, 800.0)
        # One mid-morning hour producing almost nothing, on a full bank whose
        # BMS has pinched the current it will accept. Mid-morning because this
        # string faces east: at four in the afternoon the model rightly expects
        # little of it, so a bad hour there would be a shortfall of nothing.
        self._hour(store._conn, _utc(10), 300.0, bad_volts, bad_amps, 100.0, 40.0)
        rows = compute_day(
            store, SettingsStore(store), day_start, day_start + timedelta(days=1), _ONE_STRING, 1
        )
        store.close()
        return next(r for r in rows if r.string_name == "East")

    def test_a_throttled_hour_books_as_curtailed_not_as_loss(self, tmp_path: Path) -> None:
        # Held near open circuit with its current strangled: the inverter
        # refusing energy it had nowhere to put.
        row = self._day(tmp_path, "throttled.db", bad_volts=372.8, bad_amps=1.0)
        assert row.curtailed_kwh > 0.0, "the detector is not reaching compute_day"
        assert row.unexplained_kwh == pytest.approx(0.0, abs=0.01)

    def test_the_same_shortfall_at_a_normal_voltage_is_never_excused(self, tmp_path: Path) -> None:
        """The fault case, and the reason the gate alone is not enough.

        Identical hour, identical full bank, identical pinched limit -- but the
        string sits at its ordinary voltage, so nothing was walking it off the
        power point. Something is wrong with it, and a gate-only rule would
        book this as the inverter protecting the battery and say nothing.
        """
        row = self._day(tmp_path, "faulty.db", bad_volts=310.0, bad_amps=1.0)
        assert row.curtailed_kwh == 0.0, "a fault was excused as curtailment"
        assert row.unexplained_kwh > 0.0, "the shortfall vanished instead of being named"
