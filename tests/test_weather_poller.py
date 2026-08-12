"""Tests for the weather poller: fetches on its clock, writes or stays silent."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.collector import weather as weather_module
from arraysense.collector.weather import WeatherPoller
from arraysense.efficiency import EfficiencyRow
from arraysense.forecast import MIN_SCORED_DAYS, SkyHour
from arraysense.models import Sample
from arraysense.settings import (
    PANELS_STRINGS_KEY,
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    WEATHER_INTERVAL_KEY,
    SettingsStore,
    lookup_setting,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "w.db"), device=TEST_DEVICE)
    yield s
    s.close()


def _sample() -> Sample:
    return Sample(
        timestamp=datetime.now(UTC),
        readings={"outside_temperature_c": 21.5, "cloud_cover_pct": 40.0},
    )


def _set_location(store: SqliteStore, lat: float = 35.2, lon: float = -97.4) -> None:
    settings = SettingsStore(store)
    settings.set(SETTING_LATITUDE, lat)
    settings.set(SETTING_LONGITUDE, lon)


async def test_a_configured_location_fetches_and_writes(store: SqliteStore) -> None:
    settings = SettingsStore(store)
    settings.set(SETTING_LATITUDE, 35.2)
    settings.set(SETTING_LONGITUDE, -97.4)
    calls: list[tuple[float, float]] = []

    def fetch(lat: float, lon: float) -> Sample | None:
        calls.append((lat, lon))
        return _sample()

    poller = WeatherPoller(store, fetch=fetch)
    wrote = await poller.tick()
    assert wrote is True
    assert calls == [(35.2, -97.4)]
    latest = store.latest(["outside_temperature_c", "cloud_cover_pct"])
    assert latest is not None
    assert latest["outside_temperature_c"] == 21.5
    assert latest["cloud_cover_pct"] == 40.0


async def test_no_location_means_no_fetch_and_no_write(store: SqliteStore) -> None:
    calls: list[object] = []

    def fetch(lat: float, lon: float) -> Sample | None:
        calls.append((lat, lon))
        return _sample()

    poller = WeatherPoller(store, fetch=fetch)
    wrote = await poller.tick()
    assert wrote is False
    assert calls == []
    assert store.latest(["outside_temperature_c", "cloud_cover_pct"]) is None


async def test_a_failed_fetch_writes_nothing(store: SqliteStore) -> None:
    settings = SettingsStore(store)
    settings.set(SETTING_LATITUDE, 35.2)
    settings.set(SETTING_LONGITUDE, -97.4)

    poller = WeatherPoller(store, fetch=lambda lat, lon: None)
    wrote = await poller.tick()
    assert wrote is False
    assert store.latest(["outside_temperature_c", "cloud_cover_pct"]) is None


async def test_the_loop_starts_ticks_and_stops(store: SqliteStore) -> None:
    # The loop itself: one tick per interval, and stop() actually stops it.
    settings = SettingsStore(store)
    settings.set(SETTING_LATITUDE, 35.2)
    settings.set(SETTING_LONGITUDE, -97.4)
    ticks = asyncio.Event()

    def fetch(lat: float, lon: float) -> Sample | None:
        ticks.set()
        return _sample()

    poller = WeatherPoller(store, fetch=fetch)
    await poller.start()
    await asyncio.wait_for(ticks.wait(), timeout=2.0)
    await poller.stop()
    assert poller.running is False


# --- Forecast tests ---
#
# Two ways the curve can be scaled and the poller has to pick between them: the
# array's demonstrated performance ratio when enough days have been scored, and
# the old observed-peak fallback when they have not. Which one is in force is
# the thing worth testing, because both produce a plausible-looking curve and
# only one of them is calibrated.


def _sky(when: datetime, ghi: float, dni: float = 0.0, dhi: float | None = None) -> SkyHour:
    """One predicted hour. Diffuse defaults to the whole of GHI: with no beam
    that is the only self-consistent sky, and it keeps the fallback's arithmetic
    — which reads GHI alone — easy to check by hand."""
    return SkyHour(
        when=when,
        ghi=ghi,
        dni=dni,
        dhi=ghi if dhi is None else dhi,
        air_c=25.0,
        wind_ms=2.0,
    )


def _describe_array(store: SqliteStore) -> None:
    """The reference array, so the model has something to run over."""
    SettingsStore(store).set(
        PANELS_STRINGS_KEY,
        "PV1 | 1 | 12 | 370 | 27 | 180 | vmp=34.5 temp_coeff=-0.36",
    )


def _score_days(store: SqliteStore, count: int, pr: float, *, partial: bool = False) -> None:
    """Write ``count`` scored days ending yesterday, each delivering ``pr``."""
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    rows = [
        EfficiencyRow(
            day=midnight - timedelta(days=n + 1),
            string_name="",
            expected_kwh=80.0,
            actual_kwh=80.0 * pr,
            curtailed_kwh=0.0,
            unexplained_kwh=0.0,
            modelled_hours=14,
            partial=partial,
            pr=pr,
            config_version=1,
        )
        for n in range(count)
    ]
    store.write_efficiency_day(rows)


async def test_no_scored_days_records_no_forecast_at_all(store: SqliteStore) -> None:
    """PV history alone is not a forecast, however much of it there is.

    The peak-scaled curve this replaces called 43-63% more than the reference
    array made on every one of the last twelve complete days, and nothing a
    viewer could reach told it apart from the demonstrated one: the forecast
    table holds a target hour, a time it was made and a number of watts, and
    /api/forecast returns no basis. A figure that wrong, unlabelled and
    indistinguishable from a measured one, is worse than the "no forecast data
    for today" the dashboard already draws when there is nothing to say.
    """
    _set_location(store)

    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": 8000.0},
        )
    )

    hour1 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    hour2 = datetime(2026, 8, 11, 13, 0, tzinfo=UTC)
    sky = [_sky(hour1, 500.0), _sky(hour2, 800.0)]

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample(), fetch_forecast=lambda a, b: sky)
    assert await poller.tick() is True, "the weather reading itself must still land"

    assert store.forecast_day(hour1, hour2 + timedelta(hours=1)) == []


async def test_enough_scored_days_switch_the_forecast_to_demonstrated_performance(
    store: SqliteStore,
) -> None:
    """With five scored days the curve is modelled and scaled by what was shown.

    The staged PV peak is deliberately far above what this one twelve-panel
    string can make, so a curve scaled by it rather than by the demonstrated
    ratio could not be mistaken for this one.
    """
    _set_location(store)
    _describe_array(store)
    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": 15000.0},
        )
    )
    _score_days(store, MIN_SCORED_DAYS, pr=0.9)

    hour = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)
    sky = [_sky(hour, 900.0, dni=850.0, dhi=110.0)]

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample(), fetch_forecast=lambda a, b: sky)
    assert await poller.tick() is True

    rows = store.forecast_day(hour, hour + timedelta(hours=1))
    assert len(rows) == 1
    watts = rows[0]["expected_w"]
    assert isinstance(watts, float)

    # One string of twelve 370 W panels cannot exceed its own nameplate.
    assert 0.0 < watts < 12 * 370


async def test_four_scored_days_are_not_enough(store: SqliteStore) -> None:
    """One short of the floor records nothing rather than fitting a ratio to it."""
    _set_location(store)
    _describe_array(store)
    peak_pv = 8000.0
    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": peak_pv},
        )
    )
    _score_days(store, MIN_SCORED_DAYS - 1, pr=0.9)

    hour = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)
    poller = WeatherPoller(
        store,
        fetch=lambda lat, lon: _sample(),
        fetch_forecast=lambda a, b: [_sky(hour, 500.0)],
    )
    assert await poller.tick() is True

    assert store.forecast_day(hour, hour + timedelta(hours=1)) == []


async def test_partial_days_do_not_count_toward_the_floor(store: SqliteStore) -> None:
    """A day watched in part has a ratio fitted to whichever part, so it cannot
    be one of the five that unlock a forecast."""
    _set_location(store)
    _describe_array(store)
    peak_pv = 8000.0
    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": peak_pv},
        )
    )
    _score_days(store, MIN_SCORED_DAYS + 3, pr=0.9, partial=True)

    hour = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)
    poller = WeatherPoller(
        store,
        fetch=lambda lat, lon: _sample(),
        fetch_forecast=lambda a, b: [_sky(hour, 500.0)],
    )
    assert await poller.tick() is True

    assert store.forecast_day(hour, hour + timedelta(hours=1)) == []


async def test_an_undescribed_array_records_no_forecast_however_many_days_are_scored(
    store: SqliteStore,
) -> None:
    """Without a described array there is nothing to run the model over.

    This is the case that never resolved itself: an installation with no array
    description accrues no scored days, so the old fallback was not a first-week
    measure for it but the permanent answer, wrong by half, forever.
    """
    _set_location(store)
    peak_pv = 8000.0
    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": peak_pv},
        )
    )
    _score_days(store, 20, pr=0.9)

    hour = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)
    poller = WeatherPoller(
        store,
        fetch=lambda lat, lon: _sample(),
        fetch_forecast=lambda a, b: [_sky(hour, 500.0)],
    )
    assert await poller.tick() is True

    assert store.forecast_day(hour, hour + timedelta(hours=1)) == []


async def test_no_pv_history_records_no_forecast_but_weather_still_lands(
    store: SqliteStore,
) -> None:
    """A fresh install with no PV peak gets no forecast; weather is unaffected."""
    _set_location(store)

    forecast_calls: list[tuple[float, float]] = []

    def fetch_forecast(lat: float, lon: float) -> list[SkyHour] | None:
        forecast_calls.append((lat, lon))
        return [_sky(datetime(2026, 8, 11, 12, 0, tzinfo=UTC), 600.0)]

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample(), fetch_forecast=fetch_forecast)
    wrote = await poller.tick()

    assert wrote is True  # weather written
    assert len(forecast_calls) == 1  # forecast was fetched
    # But no forecast rows because there is no PV peak to calibrate against.
    day = store.forecast_day(
        datetime(2026, 8, 11, 0, tzinfo=UTC),
        datetime(2026, 8, 12, 0, tzinfo=UTC),
    )
    assert day == []

    # Weather still landed.
    latest = store.latest(["outside_temperature_c", "cloud_cover_pct"])
    assert latest is not None
    assert latest["outside_temperature_c"] == 21.5


async def test_forecast_fetch_none_writes_no_forecast_weather_unaffected(
    store: SqliteStore,
) -> None:
    """A failed forecast fetch records no forecast; the weather write stays."""
    _set_location(store)

    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": 8000.0},
        )
    )

    poller = WeatherPoller(
        store,
        fetch=lambda lat, lon: _sample(),
        fetch_forecast=lambda lat, lon: None,
    )
    wrote = await poller.tick()

    assert wrote is True  # weather written
    day = store.forecast_day(
        datetime(2026, 8, 11, 0, tzinfo=UTC),
        datetime(2026, 8, 12, 0, tzinfo=UTC),
    )
    assert day == []

    latest = store.latest(["outside_temperature_c", "cloud_cover_pct"])
    assert latest is not None
    assert latest["outside_temperature_c"] == 21.5


async def test_prune_removes_old_forecast_rows(store: SqliteStore) -> None:
    """After a tick, forecast rows older than 90 days are gone."""
    _set_location(store)
    _describe_array(store)
    _score_days(store, MIN_SCORED_DAYS, pr=0.9)

    old_hour = datetime.now(UTC) - timedelta(days=100)
    store.append_forecast(made_at=old_hour, points=[(old_hour, 5000.0)])

    new_hour = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    poller = WeatherPoller(
        store,
        fetch=lambda lat, lon: _sample(),
        fetch_forecast=lambda lat, lon: [_sky(new_hour, 600.0)],
    )
    assert await poller.tick() is True

    old_day = store.forecast_day(
        old_hour - timedelta(hours=1),
        old_hour + timedelta(hours=1),
    )
    assert old_day == []

    new_day = store.forecast_day(
        new_hour - timedelta(hours=1),
        new_hour + timedelta(hours=1),
    )
    assert len(new_day) == 1


async def test_old_rows_are_pruned_even_when_there_is_no_forecast_to_write(
    store: SqliteStore,
) -> None:
    """The one state where nothing arrives to push the old rows out.

    An installation whose array description is withdrawn, or whose days stop
    being scorable, records no new prediction — and while the prune sat behind
    the write, its last predictions would have stayed in the table for as long
    as it ran.
    """
    _set_location(store)

    old_hour = datetime.now(UTC) - timedelta(days=100)
    store.append_forecast(made_at=old_hour, points=[(old_hour, 5000.0)])

    new_hour = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    poller = WeatherPoller(
        store,
        fetch=lambda lat, lon: _sample(),
        fetch_forecast=lambda lat, lon: [_sky(new_hour, 600.0)],
    )
    assert await poller.tick() is True

    assert store.forecast_day(old_hour - timedelta(hours=1), old_hour + timedelta(hours=1)) == []


# --- Staying alive ---
#
# The loop that records the sky has nothing above it: no watchdog watches it,
# no page reports it, and metrics.SITE_METRICS deliberately keeps weather rows
# out of the staleness witness so the dashboard banner stays quiet through a
# sky outage. Whatever kills it therefore kills it until somebody restarts the
# process by hand, and the symptom is only that recorded conditions stop.


async def test_a_locked_database_on_the_interval_read_does_not_end_the_loop(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The interval is read from the settings table on every cycle, which is a
    # SELECT another writer can hold past the busy timeout. The read sits
    # outside the loop's guard — a failed tick must still wait out the cadence
    # — so _interval has to handle the busy database itself, and it does, by
    # taking the registered default. A locked database once ended the sky
    # poller for the life of the process.
    _set_location(store)
    ticks = asyncio.Event()

    def fetch(lat: float, lon: float) -> Sample | None:
        ticks.set()
        return _sample()

    poller = WeatherPoller(store, fetch=fetch)
    real_get = poller._settings.get

    def get(key: str) -> object:
        if key == WEATHER_INTERVAL_KEY:
            raise sqlite3.OperationalError("database is locked")
        return real_get(key)

    monkeypatch.setattr(poller._settings, "get", get)

    await poller.start()
    try:
        await asyncio.wait_for(ticks.wait(), timeout=2.0)
        # Give the cycle after the tick time to reach the interval read — and,
        # as it used to, to die there.
        for _ in range(100):
            if not poller.running:
                break
            await asyncio.sleep(0.01)
        assert poller.running is True
    finally:
        await poller.stop()


def test_an_unreadable_interval_falls_back_to_the_registered_default(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cadence still has one home: a read that fails takes the registry's
    # own default rather than a number written into the loop.
    poller = WeatherPoller(store, fetch=lambda lat, lon: None)

    def get(key: str) -> object:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(poller._settings, "get", get)
    registered = lookup_setting(WEATHER_INTERVAL_KEY).default
    assert poller._interval() == registered


async def test_a_loop_that_dies_is_brought_back(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Belt and braces for the case the guards inside the loop cannot cover: if
    # the loop ends for any reason at all, the poller starts it again instead
    # of going quiet for the life of the process.
    monkeypatch.setattr(weather_module, "LOOP_RESTART_SECONDS", 0.01)
    _set_location(store)
    starts = 0
    running_again = asyncio.Event()

    real_loop = WeatherPoller._loop

    async def flaky_loop(self: WeatherPoller) -> None:
        nonlocal starts
        starts += 1
        if starts == 1:
            raise RuntimeError("something nobody predicted")
        running_again.set()
        await real_loop(self)

    monkeypatch.setattr(WeatherPoller, "_loop", flaky_loop)

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample())
    await poller.start()
    try:
        await asyncio.wait_for(running_again.wait(), timeout=2.0)
        assert poller.running is True
    finally:
        await poller.stop()


async def test_an_unexpected_tick_failure_keeps_the_configured_cadence(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fault in the model chain must not turn the loop into a five-second
    # retry against Open-Meteo. tick() has already made its two HTTPS GETs by
    # the time _forecast_points runs ordinary Python, so a bug there would
    # otherwise refetch the weather service ~17,000 times a day instead of
    # ~96 — the supervisor's restart delay is a different question from the
    # poll interval.
    _set_location(store)
    monkeypatch.setattr(weather_module, "LOOP_RESTART_SECONDS", 0.01)
    attempts = 0

    def fetch(lat: float, lon: float) -> Sample | None:
        nonlocal attempts
        attempts += 1
        raise TypeError("a bug downstream of the fetch")

    poller = WeatherPoller(store, fetch=fetch)
    # A short cadence so the test finishes quickly; long enough that a retry
    # loop at LOOP_RESTART_SECONDS lands an order of magnitude more attempts.
    monkeypatch.setattr(poller, "_interval", lambda: 0.2)

    await poller.start()
    try:
        await asyncio.sleep(0.7)
    finally:
        await poller.stop()
    # ~0.7 s at the 0.2 s cadence is about four attempts; the 0.01 s retry loop
    # would be roughly seventy.
    assert attempts <= 12


async def test_a_poller_that_has_stopped_ticking_can_be_seen_to_have_stopped(
    store: SqliteStore,
) -> None:
    # Nothing can report an outage it cannot measure. A completed cycle is
    # stamped, and the gap since it is what an endpoint or a watchdog reads.
    _set_location(store)
    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample())
    assert poller.stalled_for() is None

    await poller.tick()
    marked = poller.last_tick_at
    assert marked is not None
    assert poller.stalled_for(marked + timedelta(minutes=1)) is None
    stalled = poller.stalled_for(marked + timedelta(hours=6))
    assert stalled is not None
    assert stalled > timedelta(hours=5)
