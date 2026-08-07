"""service.py — poll the inverter on a loop and write what comes back.

Connection loss is the normal case here, not the exceptional one. The WiFi
dongle accepts exactly one TCP client, so anything else that touches it — the
vendor's app, another monitoring tool, a second copy of this service — evicts
us mid-read. A failed poll is therefore recorded as a gap and the loop carries
on, backing off so a dongle that has gone away is not hammered.

Yield mode exists for the same reason. Firmware updates go through the vendor's
app, which cannot connect while we hold the single slot, so the service can be
told to let go for a while and reconnect on its own. Without it every firmware
update means someone SSHing in to stop a service.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from arraysense.collector.source import InverterSource
from arraysense.models import Sample
from arraysense.store.rollup import (
    rebuild_inverter_hourly,
    rebuild_inverter_minute,
    rebuild_module_hourly,
)
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# How much of the recent past each maintenance pass rebuilds. The builders
# delete and reinsert whatever they cover, so a window only has to be wide
# enough to include any bucket still open when the last pass ran — a few hours
# covers a restart, and everything older is already final.
MINUTE_REBUILD_WINDOW = 3 * 3600
HOURLY_REBUILD_WINDOW = 48 * 3600

# How often maintenance runs, in seconds. Not on every poll: at an eleven-second
# cadence that would rebuild the same buckets three hundred times an hour for a
# minute bucket that changes five times.
ROLLUP_INTERVAL = 60.0

# Backoff doubles from the poll interval up to this. Five minutes is long
# enough to stop pestering a dongle that has been unplugged, short enough that
# recovery after a blip is not noticeable.
MAX_BACKOFF_SECONDS = 300.0

# Errors that mean "the inverter is not answering right now". Anything else is
# a bug in our own code and should surface rather than be recorded as a gap.
TRANSPORT_ERRORS = (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError)


@dataclass
class ServiceStatus:
    """What the service is doing, for the status endpoint to report.

    Kept as plain data rather than reached for through the service's internals,
    so the API layer never has to know how the loop works.
    """

    running: bool = False
    connected: bool = False
    yielding: bool = False
    yield_until: datetime | None = None
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_samples: int = 0
    total_failures: int = 0
    started_at: datetime | None = field(default=None)


class CollectorService:
    """Polls an inverter source and appends each reading to the store.

    One instance owns one source and one store. Starting spawns a background
    task; stopping cancels it and releases the connection, and stopping a
    service that was never started is not an error — a shutdown path that
    depends on knowing whether startup got that far is a shutdown path that
    fails during a crash.
    """

    def __init__(
        self,
        source: InverterSource,
        store: SqliteStore,
        interval: float = 10.0,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        """Wire a source to a store, polling every *interval* seconds."""
        if interval <= 0:
            raise ValueError(f"interval must be positive, got {interval}")
        self._source = source
        self._store = store
        self._interval = interval
        self._max_backoff = max_backoff
        self._task: asyncio.Task[None] | None = None
        self._yield_task: asyncio.Task[None] | None = None
        self.status = ServiceStatus()

    @property
    def is_yielding(self) -> bool:
        """Whether the dongle has been released for someone else to use."""
        return self.status.yielding

    async def start(self) -> None:
        """Begin polling in the background.

        Starting an already-running service is a no-op rather than an error, so
        a supervisor that retries does not accumulate loops.
        """
        if self._task is not None and not self._task.done():
            logger.debug("start() called while already running; ignoring")
            return
        self.status.running = True
        self.status.started_at = datetime.now(tz=UTC)
        self._task = asyncio.create_task(self._loop())
        logger.info("collector started, polling every %.1fs", self._interval)

    async def stop(self) -> None:
        """Stop polling and release the connection.

        Safe to call twice, and safe to call on a service that never started.
        """
        for task in (self._yield_task, self._task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._task = None
        self._yield_task = None
        with contextlib.suppress(*TRANSPORT_ERRORS):
            await self._source.disconnect()
        self.status.running = False
        self.status.connected = False
        logger.info("collector stopped")

    async def yield_for(self, seconds: float) -> datetime:
        """Release the dongle for *seconds*, then resume polling by itself.

        The vendor's app needs the dongle's single TCP slot to push a firmware
        update, and it cannot have it while we are connected. Returns the time
        polling will resume so a caller can say so.
        """
        if seconds <= 0:
            raise ValueError(f"yield duration must be positive, got {seconds}")
        if self._yield_task is not None:
            self._yield_task.cancel()
        self.status.yielding = True
        self.status.yield_until = datetime.now(tz=UTC) + timedelta(seconds=seconds)
        with contextlib.suppress(*TRANSPORT_ERRORS):
            await self._source.disconnect()
        self.status.connected = False
        self._yield_task = asyncio.create_task(self._resume_after(seconds))
        logger.info("yielding the dongle for %.0fs, until %s", seconds, self.status.yield_until)
        return self.status.yield_until

    async def resume(self) -> None:
        """Stop yielding immediately, without waiting out the timer."""
        if self._yield_task is not None:
            self._yield_task.cancel()
            self._yield_task = None
        self.status.yielding = False
        self.status.yield_until = None
        logger.info("resumed early")

    async def maintain_rollups(self, now: datetime | None = None) -> None:
        """Rebuild the recent coarse tiers from raw.

        Without this the tiers exist and stay empty forever, which is not a
        cosmetic problem: ``/api/history`` serves the minute tier for anything
        over about six hours, and the calibration endpoint reads it
        exclusively. An unscheduled rollup means blank history charts and a
        drift warning permanently stuck at "no full charge found" — all of it
        looking like a data problem rather than a missing cron.

        Only a recent window is rebuilt. The builders delete and reinsert the
        buckets they cover, so this is idempotent and cheap, and anything older
        than the window is already final. The minute window is short because
        minute buckets close quickly; the hourly one reaches back far enough to
        pick up a bucket that was still open when the service last stopped.

        Never raises. This is housekeeping running beside the poll loop, and
        the poll is the thing that cannot be caught up on later — a rollup can
        always be rebuilt from raw on the next pass.
        """
        moment = now or datetime.now(tz=UTC)
        end = int(moment.timestamp()) + 60
        try:
            with self._store._conn:
                rebuild_inverter_minute(self._store._conn, end - MINUTE_REBUILD_WINDOW, end)
                rebuild_inverter_hourly(self._store._conn, end - HOURLY_REBUILD_WINDOW, end)
                rebuild_module_hourly(self._store._conn, end - HOURLY_REBUILD_WINDOW, end)
        except sqlite3.Error as exc:
            logger.warning("rollup maintenance failed, will retry: %s", exc)

    async def poll_once(self) -> Sample | None:
        """Read once and store the result, returning what was stored.

        A transport failure is not raised — it is written as a gap, because a
        chart that quietly skips an outage is worse than one that shows it.
        Returns None only while yielding, when no read was attempted at all.
        """
        if self.status.yielding:
            return None

        timestamp = datetime.now(tz=UTC)
        try:
            await self._source.connect()
            sample = await self._source.read()
        except TRANSPORT_ERRORS as exc:
            reason = f"{type(exc).__name__}: {exc}"
            gap = Sample.failed(timestamp, reason)
            self._store.append(gap)
            self.status.connected = False
            self.status.last_failure = timestamp
            self.status.last_error = reason
            self.status.consecutive_failures += 1
            self.status.total_failures += 1
            logger.warning(
                "poll failed (%d consecutive): %s", self.status.consecutive_failures, reason
            )
            return gap

        self._store.append(sample)
        self.status.connected = True
        self.status.last_success = timestamp
        self.status.last_error = None
        self.status.consecutive_failures = 0
        self.status.total_samples += 1
        return sample

    def _backoff(self) -> float:
        """How long to wait before the next poll, given recent failures.

        Doubles per consecutive failure from the poll interval, capped. A
        healthy service always returns the plain interval.
        """
        if self.status.consecutive_failures == 0:
            return self._interval
        grown = self._interval * float(2**self.status.consecutive_failures)
        return float(min(grown, self._max_backoff))

    async def _resume_after(self, seconds: float) -> None:
        """Sleep out a yield and then let polling continue."""
        await asyncio.sleep(seconds)
        self.status.yielding = False
        self.status.yield_until = None
        logger.info("yield elapsed, resuming polling")

    async def _loop(self) -> None:
        """Poll forever, backing off while the inverter is unreachable.

        Rollup maintenance rides along on its own timer rather than getting a
        task of its own: it shares the store with the poll, and interleaving
        the two here means they never write at the same moment.
        """
        last_rollup = 0.0
        try:
            while True:
                await self.poll_once()
                now = asyncio.get_running_loop().time()
                if now - last_rollup >= ROLLUP_INTERVAL:
                    last_rollup = now
                    await self.maintain_rollups()
                await asyncio.sleep(self._backoff())
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bug in our own code would otherwise kill the loop silently and
            # leave the service running but collecting nothing.
            logger.exception("collector loop died unexpectedly")
            self.status.running = False
            raise
