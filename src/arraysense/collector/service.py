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

# How long the loop may produce neither a success nor a failure before it is
# judged stuck rather than slow. Well above the maximum backoff, because a
# backing-off loop is working and still marks each attempt; this catches only a
# read that never returns or a task that died. Twenty minutes costs at most
# twenty minutes of data and cannot be reached by any healthy cadence.
STALL_AFTER = timedelta(minutes=20)

# Backoff doubles from the poll interval up to this. Five minutes is long
# enough to stop pestering a dongle that has been unplugged, short enough that
# recovery after a blip is not noticeable.
MAX_BACKOFF_SECONDS = 300.0

# Errors that mean "the inverter is not answering right now". Anything else is
# a bug in our own code and should surface rather than be recorded as a gap.
TRANSPORT_ERRORS = (ConnectionError, OSError, TimeoutError, asyncio.TimeoutError)

# Writing can fail too, and for reasons that pass. SQLite raises
# OperationalError on a database held by another writer past the busy timeout,
# which the scrub tool in tools/ can do because it clears in one transaction.
# Nothing above catches that — sqlite3.Error is not an OSError — so a write that
# failed took the whole poll loop with it, and the service went on serving pages
# over a collector that had stopped. A busy database is a temporary condition
# and belongs with the other temporary conditions.
STORE_ERRORS = (sqlite3.Error,)

# How many times the backoff may double before the cap is certain to have been
# reached. 2**40 seconds is longer than the age of the universe in any interval
# anyone would configure, so no reachable setting is capped early by this.
_MAX_DOUBLINGS = 40


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
        stall_after: timedelta = STALL_AFTER,
    ) -> None:
        """Wire a source to a store, polling every *interval* seconds."""
        if interval <= 0:
            raise ValueError(f"interval must be positive, got {interval}")
        self._source = source
        self._store = store
        self._interval = interval
        self._max_backoff = max_backoff
        self._stall_after = stall_after
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
        chart that quietly skips an outage is worse than one that shows it. A
        *storage* failure is not raised either, for the same reason a read
        failure is not: a database busy for a few seconds is a passing
        condition, and the loop that backs off and tries again outlives it.
        Returns None while yielding, when no read was attempted at all, and
        when a reading was taken but could not be stored.
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
            # No second reason if the gap itself cannot be written. The read
            # already failed and that is what gets recorded; a database that is
            # also busy just means this outage goes unmarked.
            self._store_failure(gap)
            self.status.connected = False
            self._count_failure(timestamp, reason)
            return gap

        # Set before the write is attempted, because it is the read that
        # establishes it and the read has already happened. Leaving it to the
        # end meant a service whose first poll hit a busy database reported the
        # inverter as unreachable, sending whoever read that after the dongle,
        # the WiFi and the breaker while the actual problem was the disk.
        self.status.connected = True

        failed = self._store_failure(sample)
        if failed is not None:
            # The inverter answered, so the connection is fine. What is lost is
            # this reading. Counted as a failure all the same, so the loop backs
            # off instead of hammering a database that is busy — and so the
            # watchdog sees the silence if it never clears.
            self._count_failure(timestamp, failed)
            return None

        self.status.last_success = timestamp
        self.status.last_error = None
        self.status.consecutive_failures = 0
        self.status.total_samples += 1
        return sample

    def _store_failure(self, sample: Sample) -> str | None:
        """Write a sample, returning the reason it could not be written.

        Exists so the two write sites in ``poll_once`` cannot differ, and so a
        storage error reaches the same counters and the same status line as a
        transport error rather than escaping into ``_loop``.
        """
        try:
            self._store.append(sample)
        except STORE_ERRORS as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("could not store reading: %s", reason)
            return reason
        return None

    def _count_failure(self, timestamp: datetime, reason: str) -> None:
        """Record a failed poll against the status, whatever it failed at."""
        self.status.last_failure = timestamp
        self.status.last_error = reason
        self.status.consecutive_failures += 1
        self.status.total_failures += 1
        logger.warning("poll failed (%d consecutive): %s", self.status.consecutive_failures, reason)

    def _backoff(self) -> float:
        """How long to wait before the next poll, given recent failures.

        Doubles per consecutive failure from the poll interval, capped. A
        healthy service always returns the plain interval.
        """
        if self.status.consecutive_failures == 0:
            return self._interval
        # Capped before the exponent, not after. Doubling first builds an
        # integer that only grows: at 1024 consecutive failures 2**n is too
        # large to be a float at all and raises OverflowError, which kills the
        # loop. At the five-minute cap that is about eighty-five hours of an
        # unreachable inverter — a long holiday, a tripped breaker — and the
        # loop would die at exactly the moment it had been working correctly
        # the whole time. Once doubling has passed the cap the answer is the
        # cap, so there is nothing to learn from computing the rest of it.
        doublings = min(self.status.consecutive_failures, _MAX_DOUBLINGS)
        grown = self._interval * float(2**doublings)
        return float(min(grown, self._max_backoff))

    def stalled_for(self, now: datetime | None = None) -> timedelta | None:
        """How long since the poll loop last did anything at all, or None if it is fine.

        The question is deliberately not "is the inverter answering". A dongle
        that has been unplugged makes every poll fail, and that is the loop
        working correctly — it records each gap, backs off, and keeps trying. A
        service that restarted itself over that would restart every few minutes
        for as long as the inverter was away, and would lose the backoff each
        time.

        What this detects is the loop not *running*: a read that never returns,
        or a task that died and left the web server serving stale pages over a
        collector that stopped. Either shows up the same way — neither the
        success nor the failure timestamp has moved for far longer than a poll
        should ever take.

        Returns None while yielding, when the dongle has been handed over
        deliberately and no polling is expected, and when nothing was ever
        started. Those are the only two ways to be quiet on purpose.

        What it deliberately does *not* consult is ``status.running``. That flag
        is the first thing the dying loop clears — ``_loop`` sets it False and
        then re-raises — so a check that stood down when it was False stood down
        exactly when the loop had died, which is the case this exists for. It
        was written that way and shipped that way, and a test that killed the
        loop and asked three hours later got None.
        """
        if self.status.yielding:
            return None
        now = now or datetime.now(tz=UTC)
        # Asked before the running flag, deliberately. A dead loop clears that
        # flag on its way out, so consulting it first is what made this return
        # None in the one case it was written for.
        if self._task is not None and self._task.done():
            # Nothing will move the timestamps again, so there is no waiting to
            # be done and no threshold to clear.
            since = self.status.last_success or self.status.last_failure or self.status.started_at
            return now - since if since else self._stall_after
        # Never started, or stopped on purpose. Both leave the flag False and
        # both are silence somebody asked for.
        if not self.status.running:
            return None
        marks = [t for t in (self.status.last_success, self.status.last_failure) if t is not None]
        since = max(marks) if marks else self.status.started_at
        if since is None:
            return None
        idle = now - since
        return idle if idle > self._stall_after else None

    def _wait_from(self, started: float) -> float:
        """How long to sleep so that polls land one interval apart.

        The interval is a *cadence*, not a pause between polls, and the
        difference is most of the sampling rate. Sleeping the full interval
        after each read makes the real spacing read time plus interval: on the
        reference dongle a read takes twelve to seventeen seconds, so an eleven
        second interval produced samples twenty-five seconds apart — less than
        half the rate the setting asks for, and well behind what the tool this
        replaced managed on the same hardware.

        A read that overruns its own interval gets no sleep at all rather than a
        negative one, which makes the cadence ``max(interval, read time)``. That
        is the honest floor: the dongle cannot be asked faster than it answers.

        Backoff still wins while the inverter is unreachable. A failing read
        returns quickly, so scheduling by cadence would otherwise retry a dead
        connection at full speed, and the point of backing off is to stop that.
        """
        wait = self._backoff()
        if self.status.consecutive_failures:
            return wait
        elapsed = asyncio.get_running_loop().time() - started
        return max(0.0, wait - elapsed)

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
                started = asyncio.get_running_loop().time()
                await self.poll_once()
                now = asyncio.get_running_loop().time()
                if now - last_rollup >= ROLLUP_INTERVAL:
                    last_rollup = now
                    await self.maintain_rollups()
                await asyncio.sleep(self._wait_from(started))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bug in our own code would otherwise kill the loop silently and
            # leave the service running but collecting nothing.
            logger.exception("collector loop died unexpectedly")
            self.status.running = False
            raise
