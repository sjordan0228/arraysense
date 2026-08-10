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
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import Callable

from arraysense.models import Sample
from arraysense.settings import (
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    SettingsStore,
    lookup_setting,
)
from arraysense.store.sqlite_store import SqliteStore
from arraysense.weather import fetch_current

logger = logging.getLogger(__name__)

_INTERVAL_KEY = "collector.weather_interval"

# The same tuple the inverter collector catches — see service.py:93 for the
# rationale. Defined here rather than imported so the weather poller stays
# separable from the inverter collector; a busy database is the same condition
# whichever writer hit it.
STORE_ERRORS = (sqlite3.Error,)


class WeatherPoller:
    """Fetch the weather on its own clock and append what arrives.

    ``fetch`` is injected for tests; production passes nothing and gets the
    Open-Meteo client. A tick that has no location, or whose fetch returns
    None, writes nothing — absent is absent.
    """

    def __init__(
        self,
        store: SqliteStore,
        fetch: Callable[[float, float], Sample | None] = fetch_current,
    ) -> None:
        """Wire the poller to the store it appends to and the fetch it asks."""
        self._store = store
        self._settings = SettingsStore(store)
        self._fetch = fetch
        self._task: asyncio.Task[None] | None = None
        self._said_idle = False

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
        """One cycle: read the enable, fetch off-loop, append. True if written."""
        latitude = self._settings.get(SETTING_LATITUDE)
        longitude = self._settings.get(SETTING_LONGITUDE)
        if not isinstance(latitude, float) or not isinstance(longitude, float):
            if not self._said_idle:
                logger.info("weather idle: no location set; set latitude and longitude to enable")
                self._said_idle = True
            return False
        self._said_idle = False
        sample = await asyncio.to_thread(self._fetch, latitude, longitude)
        if sample is None:
            return False
        try:
            self._store.append(sample)
        except STORE_ERRORS as exc:
            logger.warning("could not store weather reading: %s", exc)
            return False
        return True

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
