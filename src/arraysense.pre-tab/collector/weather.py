"""weather.py — the poller that records the sky, independent of the inverter.

A separate loop rather than a branch of the inverter poll, because weather is a
site-level source: it must keep recording through an inverter outage, and an
unreachable weather service must cost the inverter nothing. Both writers share
one store on one event loop, so their appends interleave at await points and
never race — the same reasoning that lets the web server read while the
collector writes.

The fetch runs in a worker thread. urllib blocks, and a blocking call on the
event loop stalls every open page for the length of the timeout — the exact
stall class #63 tracks. asyncio.to_thread keeps the loop free for the seconds
the GET takes.

No location means no fetch: the location settings are the enable, read fresh
every tick so setting them takes effect within one interval, no restart needed.

As well as the current sky conditions, tick() records the day's production
forecast — the hourly shortwave radiation scaled to watts by the array's own
observed peak over the last thirty days. The model is deliberately simple: a
single ratio of observed peak to clear-sky peak radiation, with no nameplate
rating anywhere in the arithmetic. A fresh install with no PV history records
no forecast (an honest cold start), and a failed radiation fetch leaves the
current-conditions write undisturbed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from arraysense.models import Sample
from arraysense.settings import (
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    SettingsStore,
    lookup_setting,
)
from arraysense.store.sqlite_store import SqliteStore
from arraysense.weather import fetch_current, fetch_radiation_forecast

logger = logging.getLogger(__name__)

_INTERVAL_KEY = "collector.weather_interval"

# The same tuple the inverter collector catches — see service.py:93 for the
# rationale. Defined here rather than imported so the weather poller stays
# separable from the inverter collector; a busy database is the same condition
# whichever writer hit it.
STORE_ERRORS = (sqlite3.Error,)

# A typical clear-sky summer peak at this scale of installation. The honest
# calibration is this array's own observed peak against it, not a nameplate
# rating — the ratio K = observed_peak_pv / CLEAR_SKY_PEAK_RADIATION turns a
# radiation forecast in W/m² into an expected wattage for this specific array.
CLEAR_SKY_PEAK_RADIATION = 950.0  # W/m²


class WeatherPoller:
    """Fetch the weather on its own clock and append what arrives.

    ``fetch`` and ``fetch_forecast`` are injected for tests; production passes
    nothing and gets the Open-Meteo clients. A tick that has no location, or
    whose fetch returns None, writes nothing — absent is absent.
    """

    def __init__(
        self,
        store: SqliteStore,
        fetch: Callable[[float, float], Sample | None] = fetch_current,
        fetch_forecast: Callable[
            [float, float], list[tuple[datetime, float]] | None
        ] = fetch_radiation_forecast,
    ) -> None:
        """Wire the poller to the store it appends to and the fetch it asks."""
        self._store = store
        self._settings = SettingsStore(store)
        self._fetch = fetch
        self._fetch_forecast = fetch_forecast
        self._task: asyncio.Task[None] | None = None
        self._said_idle = False
        self._said_no_pv_history = False

    @property
    def running(self) -> bool:
        """Whether the loop task is alive — the lifespan test's whole question."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Begin the loop; a second start on a running poller does nothing."""
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="weather-poller")

    async def stop(self) -> None:
        """Cancel the loop and wait it out, so no orphan task survives shutdown."""
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def tick(self) -> bool:
        """One cycle: read the enable, fetch off-loop, append. True if written.

        The return is True when either the current-conditions write or the
        forecast write landed — the loop's only consumer is tests today, and
        "wrote anything" is the honest reading. A failed weather fetch does not
        prevent the forecast path from running, and vice versa.
        """
        latitude = self._settings.get(SETTING_LATITUDE)
        longitude = self._settings.get(SETTING_LONGITUDE)
        if not isinstance(latitude, float) or not isinstance(longitude, float):
            if not self._said_idle:
                logger.info("weather idle: no location set; set latitude and longitude to enable")
                self._said_idle = True
            return False
        self._said_idle = False

        wrote = False

        sample = await asyncio.to_thread(self._fetch, latitude, longitude)
        if sample is not None:
            try:
                self._store.append(sample)
            except STORE_ERRORS as exc:
                logger.warning("could not store weather reading: %s", exc)
            else:
                wrote = True

        # --- Production forecast ---
        # v1 model: scale the day's radiation forecast (W/m²) by K, where
        # K = observed_peak_pv / CLEAR_SKY_PEAK_RADIATION.  The constant is a
        # typical clear-sky summer peak at this scale of installation; the honest
        # calibration is this array's own observed peak against it, not a
        # nameplate rating.  expected_w(hour) = radiation(hour) * K, floored at 0.
        forecast = await asyncio.to_thread(self._fetch_forecast, latitude, longitude)
        if forecast is not None:
            now = datetime.now(UTC)
            observed_peak_pv = self._store.peak("pv_total_power_w", now - timedelta(days=30), now)
            if observed_peak_pv is None:
                if not self._said_no_pv_history:
                    logger.info(
                        "no PV history in the last 30 days; "
                        "recording no forecast until the array has been measured"
                    )
                    self._said_no_pv_history = True
            else:
                self._said_no_pv_history = False
                k = observed_peak_pv / CLEAR_SKY_PEAK_RADIATION
                points = [(hour, max(radiation * k, 0.0)) for hour, radiation in forecast]
                try:
                    self._store.append_forecast(now, points)
                    self._store.prune_forecast(now - timedelta(days=90))
                except STORE_ERRORS as exc:
                    logger.warning("could not store forecast: %s", exc)
                else:
                    wrote = True

        return wrote

    def _interval(self) -> float:
        """Seconds until the next tick, read fresh so a settings change applies.

        A corrupt stored row falls back to the registry's own default rather
        than a number written here, so the cadence has exactly one home. The
        registry declares this setting as a float; a non-numeric default is a
        programming error worth stopping on, not a condition to paper over.
        """
        value = self._settings.get(_INTERVAL_KEY)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        default = lookup_setting(_INTERVAL_KEY).default
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            return float(default)
        raise AssertionError(f"{_INTERVAL_KEY} is registered without a numeric default")

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            # A tick already contains every expected failure; anything else is
            # a bug worth a traceback, but the sky is not worth killing the
            # loop over — the next tick starts clean.
            except Exception:
                logger.exception("weather tick failed unexpectedly")
            await asyncio.sleep(self._interval())
