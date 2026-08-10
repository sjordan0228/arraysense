"""Tests for the weather poller: fetches on its clock, writes or stays silent."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.collector.weather import CLEAR_SKY_PEAK_RADIATION, WeatherPoller
from arraysense.models import Sample
from arraysense.settings import SETTING_LATITUDE, SETTING_LONGITUDE, SettingsStore
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


async def test_forecast_from_radiation_and_pv_history(store: SqliteStore) -> None:
    """Forecast rows land with expected_w = radiation * K for the staged peak."""
    _set_location(store)

    # Stage a known PV peak: 8000 W a day ago.
    peak_pv = 8000.0
    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": peak_pv},
        )
    )

    # Staged radiation forecast: two hours at 500 and 800 W/m².
    hour1 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)
    hour2 = datetime(2026, 8, 11, 13, 0, tzinfo=UTC)
    rad_h1 = 500.0
    rad_h2 = 800.0

    def fetch_forecast(
        lat: float,
        lon: float,
    ) -> list[tuple[datetime, float]] | None:
        return [(hour1, rad_h1), (hour2, rad_h2)]

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample(), fetch_forecast=fetch_forecast)
    wrote = await poller.tick()

    assert wrote is True

    # K = observed_peak_pv / CLEAR_SKY_PEAK_RADIATION
    k = peak_pv / CLEAR_SKY_PEAK_RADIATION
    # Hand-compute: 500 * 8000/950 ≈ 4210.5 → round to 4211
    expected_w1 = round(rad_h1 * k)
    # Hand-compute: 800 * 8000/950 ≈ 6736.8 → round to 6737
    expected_w2 = round(rad_h2 * k)

    day = store.forecast_day(hour1, hour2 + timedelta(hours=1))
    latest = day["latest"]
    assert len(latest) == 2
    assert latest[0]["expected_w"] == float(expected_w1)
    assert latest[1]["expected_w"] == float(expected_w2)


async def test_no_pv_history_records_no_forecast_but_weather_still_lands(
    store: SqliteStore,
) -> None:
    """A fresh install with no PV peak gets no forecast; weather is unaffected."""
    _set_location(store)

    # No PV history staged — the store is empty.

    forecast_calls: list[tuple[float, float]] = []

    def fetch_forecast(
        lat: float,
        lon: float,
    ) -> list[tuple[datetime, float]] | None:
        forecast_calls.append((lat, lon))
        return [(datetime(2026, 8, 11, 12, 0, tzinfo=UTC), 600.0)]

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample(), fetch_forecast=fetch_forecast)
    wrote = await poller.tick()

    assert wrote is True  # weather written
    assert len(forecast_calls) == 1  # forecast was fetched
    # But no forecast rows because there is no PV peak to calibrate against.
    day = store.forecast_day(
        datetime(2026, 8, 11, 0, tzinfo=UTC),
        datetime(2026, 8, 12, 0, tzinfo=UTC),
    )
    assert day["latest"] == []

    # Weather still landed.
    latest = store.latest(["outside_temperature_c", "cloud_cover_pct"])
    assert latest is not None
    assert latest["outside_temperature_c"] == 21.5


async def test_radiation_fetch_none_writes_no_forecast_weather_unaffected(
    store: SqliteStore,
) -> None:
    """A failed radiation fetch records no forecast; the weather write stays."""
    _set_location(store)

    # Stage PV history so the forecast path would run if it had data.
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
    # No forecast rows.
    day = store.forecast_day(
        datetime(2026, 8, 11, 0, tzinfo=UTC),
        datetime(2026, 8, 12, 0, tzinfo=UTC),
    )
    assert day["latest"] == []

    # Weather still landed.
    latest = store.latest(["outside_temperature_c", "cloud_cover_pct"])
    assert latest is not None
    assert latest["outside_temperature_c"] == 21.5


async def test_prune_removes_old_forecast_rows(store: SqliteStore) -> None:
    """After a tick, forecast rows older than 90 days are gone."""
    _set_location(store)

    # Stage PV history.
    store.append(
        Sample(
            timestamp=datetime.now(UTC) - timedelta(days=1),
            readings={"pv_total_power_w": 8000.0},
        )
    )

    # Stage an old forecast row: target_hour 100 days ago.
    old_hour = datetime.now(UTC) - timedelta(days=100)
    store.append_forecast(
        made_at=old_hour,
        points=[(old_hour, 5000.0)],
    )

    # A fresh forecast stub so the tick writes new rows and prunes.
    new_hour = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

    def fetch_forecast(
        lat: float,
        lon: float,
    ) -> list[tuple[datetime, float]] | None:
        return [(new_hour, 600.0)]

    poller = WeatherPoller(store, fetch=lambda lat, lon: _sample(), fetch_forecast=fetch_forecast)
    wrote = await poller.tick()
    assert wrote is True

    # The old row is gone.
    old_day = store.forecast_day(
        old_hour - timedelta(hours=1),
        old_hour + timedelta(hours=1),
    )
    assert old_day["latest"] == []

    # The new row is present.
    new_day = store.forecast_day(
        new_hour - timedelta(hours=1),
        new_hour + timedelta(hours=1),
    )
    assert len(new_day["latest"]) == 1
