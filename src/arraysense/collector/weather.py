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

Nothing above this loop watches it. The service's watchdog follows the inverter
collector alone, and site metrics are deliberately outside the store's
staleness witness — a sky reading every fifteen minutes would keep the outage
banner quiet through a real inverter outage — so a poller that stops leaves no
symptom at all beyond conditions ceasing to be recorded. It therefore watches
itself: every cycle's work, the interval read included, sits inside the loop's
guard; the loop runs under a supervisor that starts it again if it ever ends;
and each completed cycle is stamped so ``stalled_for`` can say how long the sky
has been unrecorded to anything that asks.

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
    fallback_points,
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

# How long to wait before starting the loop again after it has ended. The loop
# is not supposed to be able to end at all — every expected failure is caught
# inside it — so this covers only what nobody predicted, and it exists because
# the alternative is a poller that stops for the life of the process with no
# symptom beyond the sky quietly ceasing to be recorded. A few seconds, so a
# repeating fault writes a log line every few seconds rather than a wall of them.
LOOP_RESTART_SECONDS = 5.0

# How many intervals of silence make a poller stalled rather than merely
# between ticks. Three, so a single slow fetch is never mistaken for a stopped
# loop: at the registered fifteen-minute cadence a stall is called after
# three quarters of an hour, which is far shorter than the sky readings this
# would otherwise lose and far longer than any healthy cycle.
STALL_INTERVALS = 3


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
        # When a cycle last completed, and what last went wrong. Nothing above
        # this poller can report an outage it cannot measure, and the sky is
        # kept out of the store's staleness witness on purpose — a reading
        # every fifteen minutes would keep the dashboard's banner quiet through
        # a real inverter outage — so the poller has to say for itself.
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def running(self) -> bool:
        """Whether the loop task is alive — the lifespan test's whole question."""
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        """Begin the loop; a second start on a running poller does nothing."""
        if self.running:
            return
        self._task = asyncio.create_task(self._supervise(), name="weather-poller")

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

        Completing stamps ``last_tick_at`` whether or not anything was written.
        The mark says the loop is running, which is a different question from
        whether the sky answered: an installation with no location set writes
        nothing forever and is working exactly as configured.
        """
        latitude = self._settings.get(SETTING_LATITUDE)
        longitude = self._settings.get(SETTING_LONGITUDE)
        if not isinstance(latitude, float) or not isinstance(longitude, float):
            if not self._said_idle:
                logger.info("weather idle: no location set; set latitude and longitude to enable")
                self._said_idle = True
            self.last_tick_at = datetime.now(UTC)
            return False
        self._said_idle = False

        wrote = False
        failure: str | None = None

        sample = await asyncio.to_thread(self._fetch, latitude, longitude)
        if sample is not None:
            try:
                self._store.append(sample)
            except STORE_ERRORS as exc:
                logger.warning("could not store weather reading: %s", exc)
                failure = f"could not store weather reading: {exc}"
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
            if points:
                try:
                    self._store.append_forecast(now, points)
                    self._store.prune_forecast(now - timedelta(days=90))
                except STORE_ERRORS as exc:
                    logger.warning("could not store forecast: %s", exc)
                    failure = f"could not store forecast: {exc}"
                else:
                    wrote = True

        self.last_tick_at = datetime.now(UTC)
        self.last_error = failure
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
        """Model the coming hours, scaled the best way the record supports.

        Demonstrated performance is preferred. The peak-scaled fallback is
        reached only by an installation with fewer than ``MIN_SCORED_DAYS``
        scored days behind it — days the maintenance clock writes, so a system
        collecting normally leaves the fallback within a week of being
        configured and never returns to it.

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

        observed_peak_pv = self._store.peak("pv_total_power_w", now - timedelta(days=30), now)
        if observed_peak_pv is None:
            if self._basis != "none":
                logger.info(
                    "no scored days and no PV history in the last 30 days; "
                    "recording no forecast until the array has been measured"
                )
                self._basis = "none"
            return []
        if self._basis != "peak":
            logger.info(
                "fewer than %d scored days; forecasting from the observed peak until "
                "the array has been measured for longer",
                MIN_SCORED_DAYS,
            )
            self._basis = "peak"
        return fallback_points(sky, observed_peak_pv)

    def _interval(self) -> float:
        """Seconds until the next tick, read fresh so a settings change applies.

        A corrupt stored row falls back to the registry's own default rather
        than a number written here, so the cadence has exactly one home. The
        registry declares this setting as a float; a non-numeric default is a
        programming error worth stopping on, not a condition to paper over.

        The read itself is a SELECT against a database another writer can hold
        past the busy timeout — the condition ``STORE_ERRORS`` exists for — and
        it used to run outside the loop's guard, where one busy moment ended
        the sky poller for the life of the process with nothing watching to
        start it again. A failed read is a moment of contention and not a
        change of cadence, so it takes the registered default and tries again
        next cycle.
        """
        try:
            value: object = self._settings.get(WEATHER_INTERVAL_KEY)
        except STORE_ERRORS as exc:
            logger.warning("could not read the weather interval (%s); using the default", exc)
            value = None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        default = lookup_setting(WEATHER_INTERVAL_KEY).default
        if isinstance(default, (int, float)) and not isinstance(default, bool):
            return float(default)
        raise AssertionError(f"{WEATHER_INTERVAL_KEY} is registered without a numeric default")

    def stalled_for(self, now: datetime | None = None) -> timedelta | None:
        """How long since a cycle last completed, or None while that is normal.

        The sky poller is the one collector nothing else can witness. Weather
        rows are kept out of the store's staleness check on purpose — a reading
        every fifteen minutes would hold the dashboard's outage banner quiet
        through a real inverter outage — so a poller that has stopped leaves no
        trace anywhere but here. This is what an endpoint or a supervisor reads
        to say so.

        Silence is only reported once it has lasted ``STALL_INTERVALS`` of the
        configured cadence, so a single slow fetch is never mistaken for a
        stopped loop. A poller that was never started returns None: nothing has
        gone quiet that anybody asked to be loud.
        """
        if self.last_tick_at is None:
            return None
        moment = now or datetime.now(UTC)
        idle = moment - self.last_tick_at
        threshold = timedelta(seconds=STALL_INTERVALS * self._interval())
        return idle if idle > threshold else None

    async def _supervise(self) -> None:
        """Run the loop, and start it again if it ever ends.

        Belt and braces over the guards inside ``_loop``, and the reason it is
        worth having is what the alternative costs: a task created with
        ``create_task`` that nobody awaits takes its exception to the asyncio
        handler and stops, the web server carries on serving pages, and the
        only symptom is that recorded conditions stop arriving. Days of sky can
        go missing before anybody notices, and every one of those days then
        fails to score in the efficiency engine.

        Cancellation is how shutdown arrives and passes straight through, so
        ``stop`` still stops.
        """
        while True:
            try:
                await self._loop()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "weather poller stopped unexpectedly; starting it again in %.0fs",
                    LOOP_RESTART_SECONDS,
                )
            else:
                logger.error(
                    "weather poller returned without being asked to; starting it again in %.0fs",
                    LOOP_RESTART_SECONDS,
                )
            await asyncio.sleep(LOOP_RESTART_SECONDS)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
                delay = self._interval()
            except asyncio.CancelledError:
                raise
            # A tick already contains every expected failure; anything else is
            # a bug worth a traceback, but the sky is not worth killing the
            # loop over — the next tick starts clean. The interval read is
            # inside the guard for the same reason: it touches the database,
            # and a cadence nobody could read is not a reason to stop polling.
            except Exception:
                logger.exception("weather cycle failed unexpectedly")
                delay = LOOP_RESTART_SECONDS
            await asyncio.sleep(delay)
