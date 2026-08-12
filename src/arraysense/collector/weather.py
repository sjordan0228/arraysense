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
forecast. The arithmetic lives in forecast.py; what happens here is the choice
between its two bases — the array's demonstrated performance ratio when enough
days have been scored, and the old observed-peak curve when they have not — and
the fetch that feeds either. A fresh install with neither scored days nor PV
history records no forecast at all, an honest cold start, and a failed forecast
fetch leaves the current-conditions write undisturbed exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from arraysense.forecast import (
    MIN_SCORED_DAYS,
    PR_WINDOW_DAYS,
    SkyHour,
    expected_points,
    trailing_pr,
)
from arraysense.models import Sample
from arraysense.panels import StringSpec, parse_strings
from arraysense.settings import (
    PANELS_STRINGS_KEY,
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    WEATHER_INTERVAL_KEY,
    SettingsStore,
    lookup_setting,
)
from arraysense.store.sqlite_store import SqliteStore
from arraysense.weather import fetch_conditions_forecast, fetch_current

logger = logging.getLogger(__name__)

# The same tuple the inverter collector catches — see service.py:93 for the
# rationale. Defined here rather than imported so the weather poller stays
# separable from the inverter collector; a busy database is the same condition
# whichever writer hit it.
STORE_ERRORS = (sqlite3.Error,)


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
        fetch_forecast: Callable[[float, float], list[SkyHour] | None] = fetch_conditions_forecast,
    ) -> None:
        """Wire the poller to the store it appends to and the fetch it asks."""
        self._store = store
        self._settings = SettingsStore(store)
        self._fetch = fetch
        self._fetch_forecast = fetch_forecast
        self._task: asyncio.Task[None] | None = None
        self._said_idle = False
        # Which way the forecast is currently being scaled, so a change of basis
        # is logged once instead of every quarter of an hour forever.
        self._basis = ""

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
        # The same chain the efficiency engine runs, over predicted conditions
        # instead of recorded ones, scaled by what this array has actually been
        # delivering. See forecast.py for why a trailing median of scored days
        # beats the observed peak this used to scale by.
        sky = await asyncio.to_thread(self._fetch_forecast, latitude, longitude)
        if sky is not None:
            now = datetime.now(UTC)
            points = self._forecast_points(sky, latitude, longitude, now)
            try:
                # Pruning is not conditional on there being something to write.
                # An installation that has stopped forecasting — an array
                # description withdrawn, a run of days nothing could score —
                # would otherwise keep its last predictions forever, which is
                # the one state where old rows have nothing arriving to push
                # them out.
                self._store.prune_forecast(now - timedelta(days=90))
                if points:
                    self._store.append_forecast(now, points)
            except STORE_ERRORS as exc:
                logger.warning("could not store forecast: %s", exc)
            else:
                wrote = wrote or bool(points)

        return wrote

    def _array(self) -> tuple[StringSpec, ...]:
        """The strings as described today, or none when the array is undescribed.

        Read fresh on every tick, like the location, so correcting a panel count
        changes the next forecast rather than waiting for a restart. An
        unparseable description is an unconfigured installation and not a
        failure worth killing the poll over — it is already reported loudly by
        the settings page that accepted it.
        """
        text = self._settings.get(PANELS_STRINGS_KEY)
        if not isinstance(text, str) or not text.strip():
            return ()
        try:
            return parse_strings(text)
        except ValueError as exc:
            logger.debug("forecast: array description unusable: %s", exc)
            return ()

    def _forecast_points(
        self,
        sky: Sequence[SkyHour],
        latitude: float,
        longitude: float,
        now: datetime,
    ) -> list[tuple[datetime, float]]:
        """Model the coming hours, or record nothing and say why.

        There is one basis and no second-best. A described array with
        ``MIN_SCORED_DAYS`` complete days behind it gets the modelled curve
        scaled by what it has demonstrably been delivering; anything less gets
        no forecast at all, and the dashboard draws the absence it already draws
        when the poller has never run.

        This used to fall back to the peak-scaled curve the demonstrated ratio
        replaced, on the grounds that it was the behaviour a fresh install had
        always had. Measured against the reference installation it over-called
        by 43-63 % on every one of the last twelve complete days, and no reader
        of the stored forecast or of ``/api/forecast`` could tell which curve
        they had — see forecast.py.

        Which basis is in force is logged when it changes rather than on every
        tick, because this runs every fifteen minutes forever and a line per
        tick is a log nobody reads.
        """
        strings = self._array()
        if strings:
            rows = self._store.read_efficiency_days(now - timedelta(days=PR_WINDOW_DAYS), now)
            ratio = trailing_pr(rows)
            if ratio is not None:
                if self._basis != "pr":
                    logger.info(
                        "forecast scaled by demonstrated performance ratio %.3f, "
                        "from the scored days of the last %d",
                        ratio,
                        PR_WINDOW_DAYS,
                    )
                    self._basis = "pr"
                return expected_points(strings, latitude, longitude, sky, ratio)

        if self._basis != "none":
            logger.info(
                "fewer than %d complete scored days for a described array; recording no "
                "forecast until this one has been measured for longer",
                MIN_SCORED_DAYS,
            )
            self._basis = "none"
        return []

    def _interval(self) -> float:
        """Seconds until the next tick, read fresh so a settings change applies.

        A corrupt stored row falls back to the registry's own default rather
        than a number written here, so the cadence has exactly one home. The
        registry declares this setting as a float; a non-numeric default is a
        programming error worth stopping on, not a condition to paper over.
        """
        value = self._settings.get(WEATHER_INTERVAL_KEY)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        default = lookup_setting(WEATHER_INTERVAL_KEY).default
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            return float(default)
        raise AssertionError(f"{WEATHER_INTERVAL_KEY} is registered without a numeric default")

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
