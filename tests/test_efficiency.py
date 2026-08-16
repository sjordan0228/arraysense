"""Tests for the efficiency engine: arraysense.efficiency.

compute_day is the central function — it reads from the hourly tier, computes
expected production from the solar model, and returns one row per string plus a
total. These tests stage a day of inputs and assert hand-checked figures, then
verify the partial/downtime/versioning rules the spec lays out.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from arraysense.efficiency import (
    CONFIG_VERSION_KEY,
    EfficiencyRow,
    _never_adjusted,
    compute_day,
    compute_hours,
    tilt_benefit,
)
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


def test_an_hour_with_no_wind_reading_is_unmodelled_not_modelled_as_still_air(
    tmp_path: Path,
) -> None:
    """Still air is a measurement, not a default.

    Faiman puts the cells about 13 C hotter at zero wind than at 2 m/s, which
    is five percent of expected output on this array -- and it understates
    what the array should have made, so the ratio it is judged by comes out
    flattering. An hour nobody measured the wind for is an hour the model
    cannot run, and the coverage figure is where that gets said.
    """
    store = _store(str(tmp_path / "nowind.db"))
    day_start = _summer_day(0)
    for h in range(8, 16):
        _insert_hourly(
            store._conn,
            _utc(h),
            pv_power=3000.0,
            ghi=800.0,
            dni=850.0,
            dhi=120.0,
            wind=None if h == 12 else 2.0,
            air_c=30.0,
        )
    rows = compute_day(
        store, SettingsStore(store), day_start, day_start + timedelta(days=1), _ONE_STRING, 1
    )
    store.close()
    total = next(r for r in rows if r.string_name == "")
    assert total.modelled_hours == 7, "the windless hour was modelled from air nobody measured"


def test_a_range_longer_than_a_day_scores_every_day_in_it(tmp_path: Path) -> None:
    """The worst hour of a week has to be searched for in the whole week.

    ``_hourly_rows`` indexed each row by its wall-clock offset from the range's
    opening edge and then kept only offsets under twenty-four, so a week's
    scoring stopped at the end of the first day. The summary and the trend were
    unaffected — they come from ``compute_day``, one day at a time — but the
    Efficiency page's "Worst hour" panel reads this, and on the reference
    installation it named an hour that lost 1.7 kWh while the real worst hour of
    that week lost 4.4. The same truncation made the baseline fit structurally
    dead: it is documented as spanning the range and never saw past day one.
    """
    store = _store(str(tmp_path / "week.db"))
    day_start = _summer_day(0)
    for day in range(7):
        for h in range(8, 16):
            _insert_hourly(
                store._conn,
                _utc(h) + timedelta(days=day),
                # One day loses most of its output; it must be findable.
                pv_power=300.0 if day == 5 else 3200.0,
                ghi=800.0,
                dni=850.0,
                dhi=120.0,
                wind=2.0,
                air_c=30.0,
            )
    hours = compute_hours(
        store, SettingsStore(store), day_start, day_start + timedelta(days=7), _ONE_STRING
    )
    store.close()

    days_seen = {h.hour.date() for h in hours}
    assert len(days_seen) == 7, f"only {sorted(days_seen)} was scored of a seven-day range"
    worst = max(hours, key=lambda h: h.unexplained_kwh)
    assert worst.hour.date() == (day_start + timedelta(days=5)).date()


def test_a_string_that_never_reported_gets_no_row_at_all(tmp_path: Path) -> None:
    """A string the inverter was silent about is absent, never a row of zeros.

    compute_day already refuses to emit anything for a day it could not model,
    on the grounds that "expected nothing and made nothing" is a claim rather
    than a silence. It seeded every described string's accumulator with 0.0 all
    the same, so a string whose power reading was NULL for the whole day came
    back as expected 0.0, actual 0.0, specific yield 0.0 and partial false —
    which is what the reference installation served for PV3 on 10 July 2025, a
    day PV3 was never read at all and GHI peaked at 845 W/m2.
    """
    two = parse_strings("East | 1 | 10 | 400 | 25 | 90\nWest | 2 | 10 | 400 | 25 | 270")
    store = _store(str(tmp_path / "silent.db"))
    SettingsStore(store).set(
        "panels.strings", "East | 1 | 10 | 400 | 25 | 90\nWest | 2 | 10 | 400 | 25 | 270"
    )
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
        store, SettingsStore(store), day_start, day_start + timedelta(days=1), two, 1
    )
    store.close()

    names = {r.string_name for r in rows}
    assert "West" not in names, "a string nobody read was reported as making nothing"
    assert names == {"East", ""}


class TestSharedMpptGroups:
    """An MPPT meter is one reading even when more than one string shares it."""

    _shared_text = "East | 1 | 10 | 400 | 25 | 90\nWest | 1 | 8 | 410 | 25 | 270"

    @staticmethod
    def _stage(
        store: SqliteStore,
        *,
        pv1_w: float | None,
        pv2_w: float | None = None,
        pv3_w: float | None = None,
        throttled: bool = False,
    ) -> None:
        """Stage enough sunlit hours for an MPPT group to be scored."""
        from arraysense.metrics import lookup

        for h in range(8, 16):
            values: dict[str, float] = {
                "ghi_wm2": 800.0,
                "dni_wm2": 850.0,
                "dhi_wm2": 120.0,
                "wind_speed_ms": 2.0,
                "outside_temperature_c": 30.0,
                "battery_soc_pct": 100.0 if throttled and h == 10 else 60.0,
                "bms_charge_current_limit_a": 40.0 if throttled and h == 10 else 800.0,
            }
            if pv1_w is not None:
                values["pv1_power_w"] = 300.0 if throttled and h == 10 else pv1_w
                values["pv1_voltage_v"] = 373.0 if throttled and h == 10 else 310.0
                values["pv1_current_a"] = 1.0 if throttled and h == 10 else pv1_w / 310.0
            if pv2_w is not None:
                values["pv2_power_w"] = pv2_w
                values["pv2_voltage_v"] = 310.0
                values["pv2_current_a"] = pv2_w / 310.0
            if pv3_w is not None:
                values["pv3_power_w"] = pv3_w
                values["pv3_voltage_v"] = 310.0
                values["pv3_current_a"] = pv3_w / 310.0
            columns = ["timestamp", "device", *values, "sample_count"]
            encoded = [
                int(_utc(h).timestamp()),
                TEST_DEVICE,
                *(round(value * lookup(name).scale) for name, value in values.items()),
                1,
            ]
            store._conn.execute(
                f"INSERT OR REPLACE INTO inverter_hourly ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                encoded,
            )

    @staticmethod
    def _rows(store: SqliteStore, strings: str) -> list[EfficiencyRow]:
        """Score the fixed summer day against this array description."""
        SettingsStore(store).set("panels.strings", strings)
        return compute_day(
            store,
            SettingsStore(store),
            _summer_day(0),
            _summer_day(0) + timedelta(days=1),
            parse_strings(strings),
            1,
        )

    def test_shared_mppt_counts_its_actual_once_and_not_at_the_legacy_pr(
        self, tmp_path: Path
    ) -> None:
        """One 3 kW MPPT reading is not two 3 kW strings' output."""
        store = _store(str(tmp_path / "shared-once.db"))
        self._stage(store, pv1_w=3000.0)

        rows = self._rows(store, self._shared_text)
        total = next(row for row in rows if row.string_name == "")

        assert [row.string_name for row in rows] == ["[MPPT 1] East + West", ""]
        assert total.actual_kwh == pytest.approx(24.0)
        assert total.pr is not None
        legacy_pr = 2.0 * total.actual_kwh / (total.expected_kwh - total.curtailed_kwh)
        assert total.pr == pytest.approx(legacy_pr / 2.0)
        strings = "East | 1 | 10 | 400 | 25 | 90\nWest | 2 | 8 | 410 | 25 | 270"
        one_per_store = _store(str(tmp_path / "one-per-mppt.db"))
        self._stage(one_per_store, pv1_w=3000.0, pv2_w=2500.0)
        one_per_rows = self._rows(one_per_store, strings)
        one_per_store.write_efficiency_day(one_per_rows)
        stored = one_per_store.read_efficiency_days(
            _summer_day(0), _summer_day(0) + timedelta(days=1)
        )
        expected_bytes = (
            '[{"actual_kwh": 24.0, "config_version": 1, "curtailed_kwh": 0.0, '
            '"day": "2026-08-10 00:00:00-05:00", "expected_kwh": 21.263182740044265, '
            '"modelled_hours": 8, "partial": false, "pr": 1.1287115524244438, '
            '"string_name": "East", "unexplained_kwh": 0.0}, '
            '{"actual_kwh": 20.0, "config_version": 1, "curtailed_kwh": 0.0, '
            '"day": "2026-08-10 00:00:00-05:00", "expected_kwh": 10.786957522510683, '
            '"modelled_hours": 8, "partial": false, "pr": 1.854090920286202, '
            '"string_name": "West", "unexplained_kwh": 0.0}, '
            '{"actual_kwh": 44.0, "config_version": 1, "curtailed_kwh": 0.0, '
            '"day": "2026-08-10 00:00:00-05:00", "expected_kwh": 32.050140262554955, '
            '"modelled_hours": 8, "partial": false, "pr": 1.3728489061062359, '
            '"string_name": "", "unexplained_kwh": 0.0}]'
        )
        assert (
            json.dumps([asdict(row) for row in one_per_rows], default=str, sort_keys=True)
            == expected_bytes
        )
        assert (
            json.dumps(
                [asdict(row) for row in [stored[1], stored[2], stored[0]]],
                default=str,
                sort_keys=True,
            )
            == expected_bytes
        )

    def test_a_shared_pair_and_lone_string_keep_the_lone_name(self, tmp_path: Path) -> None:
        """Grouping one MPPT does not change another MPPT's independently measured row."""
        strings = self._shared_text + "\nSouth | 2 | 9 | 400 | 30 | 180"
        store = _store(str(tmp_path / "mixed-groups.db"))
        self._stage(store, pv1_w=3000.0, pv2_w=2500.0)

        rows = self._rows(store, strings)
        names = [row.string_name for row in rows]

        assert names == ["[MPPT 1] East + West", "South", ""]
        assert next(row for row in rows if row.string_name == "South").actual_kwh == pytest.approx(
            20.0
        )

    def test_a_string_named_like_a_group_does_not_merge_two_mppts(self, tmp_path: Path) -> None:
        """MPPT identity cannot be the label an owner chose for another string."""
        collision = "[MPPT 1] East + West"
        strings = self._shared_text + f"\n{collision} | 3 | 9 | 400 | 30 | 180"
        store = _store(str(tmp_path / "colliding-group-name.db"))
        self._stage(store, pv1_w=3000.0, pv3_w=1600.0)

        rows = self._rows(store, strings)
        groups = [row for row in rows if row.string_name]
        store.write_efficiency_day(rows)
        stored = store.read_efficiency_days(_summer_day(0), _summer_day(0) + timedelta(days=1))

        assert len(groups) == 2
        assert len({row.string_name for row in groups}) == 2
        assert collision in {row.string_name for row in groups}
        assert f"{collision} (2)" in {row.string_name for row in groups}
        assert sorted(row.actual_kwh for row in groups) == pytest.approx([12.8, 24.0])
        assert len(stored) == 3

    def test_a_group_recompute_replaces_its_old_string_rows(self, tmp_path: Path) -> None:
        """A version-two group must not leave version-one strings beside it."""
        store = _store(str(tmp_path / "replace-shared-rows.db"))
        self._stage(store, pv1_w=3000.0)
        day = _summer_day(0)
        old = [
            EfficiencyRow(day, name, 10.0, 5.0, 0.0, 5.0, 8, False, 0.5, 1)
            for name in ("East", "West", "")
        ]
        store.write_efficiency_day(old)

        fresh = [replace(row, config_version=2) for row in self._rows(store, self._shared_text)]
        store.write_efficiency_day(fresh)
        rows = store.read_efficiency_days(day, day + timedelta(days=1))

        assert [row.string_name for row in rows] == ["", "[MPPT 1] East + West"]
        assert all(row.config_version == 2 for row in rows)

    def test_group_expected_keeps_each_members_geometry(self, tmp_path: Path) -> None:
        """Grouping adds separately modelled strings instead of copying one model twice."""
        separate = "East | 1 | 10 | 400 | 25 | 90\nWest | 2 | 8 | 410 | 25 | 270"
        shared_store = _store(str(tmp_path / "shared-geometry.db"))
        separate_store = _store(str(tmp_path / "separate-geometry.db"))
        self._stage(shared_store, pv1_w=3000.0)
        self._stage(separate_store, pv1_w=1500.0, pv2_w=1500.0)

        shared = self._rows(shared_store, self._shared_text)
        separate_rows = self._rows(separate_store, separate)

        shared_group = next(row for row in shared if row.string_name.startswith("[MPPT 1]"))
        separate_expected = sum(row.expected_kwh for row in separate_rows if row.string_name)
        assert shared_group.expected_kwh == pytest.approx(separate_expected)

    def test_shared_mppt_curtailment_is_booked_once_at_group_level(self, tmp_path: Path) -> None:
        """The MPPT's one throttled operating point cannot create two refused-energy rows."""
        store = _store(str(tmp_path / "shared-curtailment.db"))
        self._stage(store, pv1_w=2830.0, throttled=True)

        rows = self._rows(store, self._shared_text)
        group = next(row for row in rows if row.string_name.startswith("[MPPT 1]"))
        total = next(row for row in rows if row.string_name == "")

        assert group.curtailed_kwh > 0.0
        assert total.curtailed_kwh == pytest.approx(group.curtailed_kwh)
        assert total.actual_kwh == pytest.approx(20.11)

    def test_group_names_are_stable_when_configuration_order_changes(self, tmp_path: Path) -> None:
        """A reordered setting must overwrite the same history row, not fragment it."""
        reordered = "West | 1 | 8 | 410 | 25 | 270\nEast | 1 | 10 | 400 | 25 | 90"
        store = _store(str(tmp_path / "stable-group-name.db"))
        self._stage(store, pv1_w=3000.0)

        forward = self._rows(store, self._shared_text)
        backward = self._rows(store, reordered)

        assert forward[0].string_name == backward[0].string_name == "[MPPT 1] East + West"

    def test_shared_mppt_without_its_reading_has_no_group_row(self, tmp_path: Path) -> None:
        """An absent MPPT measurement remains absent rather than becoming zero output."""
        strings = self._shared_text + "\nSouth | 2 | 9 | 400 | 30 | 180"
        present_store = _store(str(tmp_path / "shared-present.db"))
        self._stage(present_store, pv1_w=3000.0, pv2_w=2500.0)
        present = self._rows(present_store, strings)
        store = _store(str(tmp_path / "shared-silent.db"))
        self._stage(store, pv1_w=None, pv2_w=2500.0)

        rows = self._rows(store, strings)

        assert [row.string_name for row in present] == ["[MPPT 1] East + West", "South", ""]
        assert [row.string_name for row in rows] == ["South", ""]


class TestTiltSchedule:
    """The array's geometry is time-dependent, and scoring has to follow it."""

    def _clear_day(self, path: Path) -> tuple[SqliteStore, datetime, datetime]:
        store = _store(str(path))
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
        return store, day_start, day_start + timedelta(days=1)

    def test_a_day_after_an_adjustment_scores_as_if_the_mount_were_fixed_there(
        self, tmp_path: Path
    ) -> None:
        # The whole claim of the feature: on 10 August, an array adjusted to 40°
        # on 5 August must be modelled at 40° — indistinguishable from one that
        # had always been at 40°.
        store, start, end = self._clear_day(tmp_path / "after.db")
        settings = SettingsStore(store)
        scheduled = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-05 | 90")
        always_40 = parse_strings("East | 1 | 10 | 400 | 40 | 90")
        moved = compute_day(store, settings, start, end, scheduled, 1)
        fixed = compute_day(store, settings, start, end, always_40, 1)
        assert moved[0].expected_kwh == pytest.approx(fixed[0].expected_kwh)

    def test_a_day_before_an_adjustment_keeps_the_angle_it_was_under(self, tmp_path: Path) -> None:
        # The other half, and the one the issue exists for: adding a future
        # adjustment must not retrospectively re-model the past at the new angle.
        store, start, end = self._clear_day(tmp_path / "before.db")
        settings = SettingsStore(store)
        scheduled = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-15 | 90")
        always_25 = parse_strings("East | 1 | 10 | 400 | 25 | 90")
        pending = compute_day(store, settings, start, end, scheduled, 1)
        fixed = compute_day(store, settings, start, end, always_25, 1)
        assert pending[0].expected_kwh == pytest.approx(fixed[0].expected_kwh)

    def test_the_two_angles_actually_produce_different_expectations(self, tmp_path: Path) -> None:
        # Guards the pair above against passing vacuously. If 25° and 40° scored
        # the same, both assertions would hold while the schedule did nothing.
        store, start, end = self._clear_day(tmp_path / "differ.db")
        settings = SettingsStore(store)
        low = compute_day(
            store, settings, start, end, parse_strings("E | 1 | 10 | 400 | 25 | 90"), 1
        )
        high = compute_day(
            store, settings, start, end, parse_strings("E | 1 | 10 | 400 | 40 | 90"), 1
        )
        assert low[0].expected_kwh != pytest.approx(high[0].expected_kwh)

    def test_the_adjustment_takes_effect_on_the_owners_calendar_day(self, tmp_path: Path) -> None:
        # The site is US Central; the scored hours span two UTC days. An
        # adjustment dated 10 August must apply to all of the owner's 10 August,
        # not to whichever hours happen to fall in the UTC day of that name.
        store, start, end = self._clear_day(tmp_path / "boundary.db")
        settings = SettingsStore(store)
        today = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-10 | 90")
        always_40 = parse_strings("East | 1 | 10 | 400 | 40 | 90")
        rows = compute_day(store, settings, start, end, today, 1)
        fixed = compute_day(store, settings, start, end, always_40, 1)
        assert rows[0].expected_kwh == pytest.approx(fixed[0].expected_kwh)

    def test_a_fixed_mount_has_nothing_to_compare(self, tmp_path: Path) -> None:
        store, day_start, day_end = self._clear_day(tmp_path / "fixed.db")
        strings = parse_strings("East | 1 | 10 | 400 | 25 | 90")
        settings = SettingsStore(store)
        assert tilt_benefit(store, settings, day_start, day_end, strings) is None

    def test_an_adjusted_mount_reports_hours_it_was_drawn_from(self, tmp_path: Path) -> None:
        store, day_start, day_end = self._clear_day(tmp_path / "adjusted.db")
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-05 | 90")
        settings = SettingsStore(store)
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.hours > 0

    def test_the_two_geometries_produce_different_expectations(self, tmp_path: Path) -> None:
        store, day_start, day_end = self._clear_day(tmp_path / "diff.db")
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-05 | 90")
        settings = SettingsStore(store)
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.scheduled_kwh != pytest.approx(found.unadjusted_kwh)

    def test_the_gain_is_the_difference_between_the_two_sides(self, tmp_path: Path) -> None:
        store, day_start, day_end = self._clear_day(tmp_path / "gain.db")
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-05 | 90")
        settings = SettingsStore(store)
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.gain_kwh == pytest.approx(found.scheduled_kwh - found.unadjusted_kwh)

    def test_an_adjustment_still_in_the_future_has_changed_nothing_yet(
        self, tmp_path: Path
    ) -> None:
        # The schedule is real but the day sits wholly inside the first period,
        # so there is no difference to attribute to an adjustment.
        store, day_start, day_end = self._clear_day(tmp_path / "future.db")
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-09-01 | 90")
        settings = SettingsStore(store)
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.scheduled_kwh == pytest.approx(found.unadjusted_kwh)
        assert found.adjustments == 0

    def test_an_adjustment_inside_the_range_is_counted(self, tmp_path: Path) -> None:
        store, day_start, day_end = self._clear_day(tmp_path / "inside.db")
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-10 | 90")
        settings = SettingsStore(store)
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.adjustments == 1

    def test_one_adjustable_string_among_fixed_ones_still_answers(self, tmp_path: Path) -> None:
        store, day_start, day_end = self._clear_day(tmp_path / "mixed.db")
        strings = parse_strings(
            "East | 1 | 10 | 400 | 25,40@2026-08-05 | 90\nWest | 2 | 10 | 400 | 30 | 270"
        )
        settings = SettingsStore(store)
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.hours > 0

    def test_both_sides_are_drawn_from_the_same_hours(self, tmp_path: Path) -> None:
        # The subtraction only means something if the two runs saw the same
        # hours. Tilt takes no part in which hours are skippable, and this is
        # what holds that true rather than assuming it.
        store, day_start, day_end = self._clear_day(tmp_path / "sameh.db")
        settings = SettingsStore(store)
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-05 | 90")
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        scheduled = compute_hours(store, settings, day_start, day_end, strings)
        never = compute_hours(store, settings, day_start, day_end, _never_adjusted(strings))
        assert found.hours == len(scheduled) == len(never)

    def test_a_gap_removes_the_hour_from_both_sides(self, tmp_path: Path) -> None:
        # An outage must not flatter either geometry. The hour is absent from
        # both runs, so it cannot land on one side of the comparison only.
        store, day_start, day_end = self._clear_day(tmp_path / "gap.db")
        store._conn.execute(
            "UPDATE inverter_hourly SET pv1_power_w = NULL WHERE timestamp = ?",
            (int(_utc(12).timestamp()),),
        )
        settings = SettingsStore(store)
        strings = parse_strings("East | 1 | 10 | 400 | 25,40@2026-08-05 | 90")
        found = tilt_benefit(store, settings, day_start, day_end, strings)
        assert found is not None
        assert found.hours == 7  # eight staged, one silenced
