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

# How much of the recent past each maintenance pass rebuilds. Three hours is a
# conservative bound for both tiers, not a backfill mechanism:
#
# - the collector's gap path and the production driver stamp rows at poll time,
#   so none can arrive in a materially older bucket;
# - a pass every ROLLUP_INTERVAL only has the current hour open, and after a
#   restart the first poll attempt runs a pass before sleeping;
# - import_solar_assistant writes raw, minute and hourly tiers together for
#   arbitrary historical timestamps;
# - scrub_out_of_bounds corrects every affected tier directly rather than
#   relying on a later rebuild; and
# - a raw row is rewritten only by the store's own upsert, whose conflict target
#   is the timestamp itself, so a retry can replace a row from the instant it was
#   stamped and never one from an older bucket.
#
# The builders delete and reinsert the buckets they cover, so the extra hours
# also repair a recently closed bucket after a retry or restart. Older buckets
# are final: scanning 48 hours here made every pass proportional to poll cadence
# even though no steady-state writer could change those rows.
#
# What the narrowing does give up, stated rather than glossed: a pass reads raw
# at the instant it runs, so rows written between one pass and a crash — under a
# minute of them — are not in the bucket yet. A restart inside the window repairs
# that; one after it leaves that single hour averaged over slightly fewer rows
# than it holds, recorded in the bucket's SAMPLE_COUNT rather than hidden. The
# minute tier has carried exactly this bound since it was written.
MINUTE_REBUILD_WINDOW = 3 * 3600
HOURLY_REBUILD_WINDOW = 3 * 3600

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

# Errors that mean "the driver could not turn what the inverter returned into a
# sample". The driver builds Sample and BatteryModuleSample objects, and their
# validation raises ValueError on a malformed one — a slot the model refuses, a
# naive timestamp, an empty serial, a failed poll that also carries readings.
# Such a failure is deterministic: it will repeat identically on every poll, so
# retrying cannot fix it, but stopping the loop is worse — the web server keeps
# serving over a collector that has died, with no gap row written and no process
# exit for the watchdog to see. It is therefore recorded as a gap and backed off
# from, exactly as an unreachable inverter is. ValueError is deliberately narrow
# and separate from TRANSPORT_ERRORS: a genuine programming error in our own
# code — an AttributeError or TypeError — still surfaces rather than being
# mislabelled as an outage.
BUILD_ERRORS = (ValueError,)

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
    # Which layer the most recent failure happened at: "transport" when the
    # inverter could not be reached, "store" when it answered but the write
    # failed, "build" when it answered and the driver could not turn the reply
    # into a sample. ``connected`` alone used to carry this, which worked while
    # there were only two answers; a third one had to land on one of them and
    # named the inverter as the fault while it was answering every poll.
    last_failure_kind: str | None = None
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
        *,
        owns_store: bool = False,
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
        self._store_write_lock = asyncio.Lock()
        self._owns_store = owns_store
        self._store_closed = False
        self.status = ServiceStatus()

    @property
    def is_yielding(self) -> bool:
        """Whether the dongle has been released for someone else to use."""
        return self.status.yielding

    @property
    def source(self) -> InverterSource:
        """The inverter this is polling.

        Exposed so the status endpoint can report diagnostics the source keeps
        and the service does not — the count of crossed replies, for one, which
        belongs to whichever transport had to recover from them. Read-only: the
        service owns the connection's lifecycle and a caller that reconnected
        it out from under the poll loop would lose the dongle's single slot.
        """
        return self._source

    async def start(self) -> None:
        """Begin polling in the background.

        Starting an already-running service is a no-op rather than an error, so
        a supervisor that retries does not accumulate loops.
        """
        if self._task is not None and not self._task.done():
            logger.debug("start() called while already running; ignoring")
            return
        if self._store_closed:
            raise RuntimeError("cannot restart a collector after its owned store was closed")
        self.status.running = True
        self.status.started_at = datetime.now(tz=UTC)
        self._task = asyncio.create_task(self._loop())
        logger.info("collector started, polling every %.1fs", self._interval)

    async def stop(self) -> None:
        """Stop polling and release the connection.

        Safe to call twice, and safe to call on a service that never started.

        Awaiting a task that died of its own exception re-raises it, and letting
        that escape used to skip the disconnect below — leaving the dongle's
        single TCP slot held until it timed out, which blocks both the restart
        and the owner's vendor app. ``gather`` collects those exceptions instead
        of raising them, so every task is still awaited and the release is
        reached whatever any of them did. It is also why there is no broad
        ``except`` here: ``return_exceptions`` does that job without one, and the
        alternative of wrapping only the disconnect in ``finally`` would abandon
        the second task the moment the first one raised.

        A death is logged rather than re-raised. ``_loop`` has already recorded
        it with a traceback and cleared ``running``, so the fault is on the
        record before this is reached; raising again from a shutdown path would
        only break the caller that asked to shut down.
        """
        tasks = [task for task in (self._yield_task, self._task) if task is not None]
        for task in tasks:
            task.cancel()
        try:
            outcomes = await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._task = None
            self._yield_task = None
            with contextlib.suppress(*TRANSPORT_ERRORS):
                await self._source.disconnect()
            self.status.running = False
            self.status.connected = False
            logger.info("collector stopped")
        for outcome in outcomes:
            if isinstance(outcome, BaseException) and not isinstance(
                outcome, asyncio.CancelledError
            ):
                logger.error("poll task had already died before stop(): %s", outcome)

    async def close(self) -> None:
        """Stop the collector and close a store it was explicitly given ownership of.

        Most tests and library callers manage the store themselves, so ownership
        is opt-in. Application assembly opens a dedicated collector connection
        and transfers it here; keeping that lifecycle beside the poll task means
        shutdown cannot close SQLite while an off-loop write is still finishing.
        """
        try:
            await self.stop()
        finally:
            if self._owns_store and not self._store_closed:
                self._store.close()
                self._store_closed = True

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
        than the window is already final. Three hours covers the open bucket,
        the maintenance interval and any restart that lands inside it; historical
        import and correction tools update every affected tier explicitly.

        A SQLite failure is logged rather than raised. This is housekeeping
        running beside the poll loop, and the poll is the thing that cannot be
        caught up on later — a rollup can always be rebuilt from raw on the next
        pass. Cancellation is *not* swallowed, because that is how shutdown
        arrives and a pass that refused to stop would hang it.

        The pass runs off the event loop in a thread, so it no longer blocks
        HTTP responses — which was the whole of #30, measured at a 4 ms median
        rising to 1141 ms twice a minute on the reference box. It does still
        delay this collector's *own* next poll, because the loop awaits it, and
        that is the intended trade: a slow rebuild costs the collector its lock
        rather than costing every open page its response.

        The connection is opened and closed inside the worker. Handing a thread
        a connection owned by the event loop means a cancellation can close it
        from underneath a statement still running — which blocks the loop on
        SQLite's own mutex, exactly the stall this change exists to remove.
        """
        moment = now or datetime.now(tz=UTC)
        end = int(moment.timestamp()) + 60

        def _rebuild_all(conn: sqlite3.Connection) -> None:
            rebuild_inverter_minute(conn, end - MINUTE_REBUILD_WINDOW, end)
            rebuild_inverter_hourly(conn, end - HOURLY_REBUILD_WINDOW, end)
            rebuild_module_hourly(conn, end - HOURLY_REBUILD_WINDOW, end)

        def _rebuild_on_own_connection() -> None:
            conn = self._store.maintenance_connection()
            try:
                _rebuild_all(conn)
            finally:
                conn.close()

        try:
            if self._store.is_memory_backed:
                # A second connection to ":memory:" is a *different*, empty
                # database, so a threaded pass would rebuild nothing and report
                # success — the tiers would sit empty with no error anywhere.
                # An in-memory store is a test fixture and costs microseconds,
                # so it runs inline rather than silently doing nothing.
                _rebuild_all(self._store._conn)
            else:
                await asyncio.to_thread(_rebuild_on_own_connection)
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
        except TRANSPORT_ERRORS as exc:
            return await self._record_gap(
                timestamp, f"{type(exc).__name__}: {exc}", kind="transport"
            )

        try:
            sample = await self._source.read()
        except TRANSPORT_ERRORS as exc:
            return await self._record_gap(
                timestamp, f"{type(exc).__name__}: {exc}", kind="transport"
            )
        except BUILD_ERRORS as exc:
            # A sample the driver could not build is deterministic — it will
            # fail identically on every poll, so retrying cannot fix it — but
            # the loop must survive it and the history must show the hole,
            # exactly as for an unreachable inverter. BUILD_ERRORS is scoped to
            # read() rather than covering connect(), so a genuine bug in our own
            # code still surfaces instead of being recorded as an outage.
            return await self._record_gap(timestamp, f"{type(exc).__name__}: {exc}", kind="build")

        # Set before the write is attempted, because it is the read that
        # establishes it and the read has already happened. Leaving it to the
        # end meant a service whose first poll hit a busy database reported the
        # inverter as unreachable, sending whoever read that after the dongle,
        # the WiFi and the breaker while the actual problem was the disk.
        self.status.connected = True

        failed = await self._store_failure(sample)
        if failed is not None:
            # The inverter answered, so the connection is fine. What is lost is
            # this reading. Counted as a failure all the same, so the loop backs
            # off instead of hammering a database that is busy — and so the
            # watchdog sees the silence if it never clears.
            self._count_failure(timestamp, failed, kind="store")
            return None

        self.status.last_success = timestamp
        self.status.last_error = None
        self.status.last_failure_kind = None
        self.status.consecutive_failures = 0
        self.status.total_samples += 1
        return sample

    async def _record_gap(self, timestamp: datetime, reason: str, *, kind: str) -> Sample:
        """Record a failed poll as a gap and count it, returning the sample.

        Shared by the transport- and build-error paths in ``poll_once`` so the
        two cannot diverge: each files the gap against the source's device and
        counts the failure so the loop backs off. A gap is data even when nothing
        could be read — a chart that quietly skips an outage is worse than one
        that shows it.

        Only a transport failure marks the connection down. On the build path the
        inverter answered — ``connect()`` returned and the registers arrived — and
        only turning the reply into a sample failed, so reporting the connection
        as lost would send whoever read it after the dongle, the WiFi and the
        breaker over a fault that is in our own decoding.
        """
        gap = Sample.failed(timestamp, reason)
        # No second reason if the gap itself cannot be written. The read
        # already failed and that is what gets recorded; a database that is
        # also busy just means this outage goes unmarked.
        await self._store_failure(gap)
        if kind == "transport":
            self.status.connected = False
        elif kind == "build":
            # ``connect()`` returned before this, so the dongle answered. Leaving
            # the flag at its old value would report a connection that is up as
            # down on the very first poll, which is the misdiagnosis this whole
            # distinction exists to avoid.
            self.status.connected = True
        self._count_failure(timestamp, reason, kind=kind)
        return gap

    async def _store_failure(self, sample: Sample) -> str | None:
        """Write a sample against this source's inverter, returning why it could not be.

        Exists so the two write sites in ``poll_once`` cannot differ, and so a
        storage error reaches the same counters and the same status line as a
        transport error rather than escaping into ``_loop``.

        The device comes from the source rather than from the store's default.
        The source is the only thing here that knows which inverter answered,
        and a recorded gap has to be filed under the unit that went quiet — a
        gap stamped with whatever the store was opened for would report an
        outage on the wrong machine.
        """
        try:
            # ``append`` includes SQLite's commit and therefore the filesystem's
            # durability wait. On the reference ZFS volume that wait can take
            # about half a second; running it directly in this coroutine froze
            # every HTTP response on the process's one event loop for the same
            # interval. A dedicated collector connection (wired in build_app)
            # lets the write move to a worker without taking the API connection's
            # SQLite mutex with it.
            #
            # Shielding is about shutdown correctness, not latency. Cancelling
            # an asyncio task cannot stop code already running in a thread. If
            # stop() closed the connection while that worker was committing, the
            # shutdown path would race the one sample it is meant to preserve.
            # Wait for the worker to finish, then propagate cancellation.
            async with self._store_write_lock:
                write = asyncio.create_task(
                    asyncio.to_thread(
                        self._store.append,
                        sample,
                        device=self._source.device,
                    )
                )
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    # Retrieve an ordinary write failure without letting it
                    # replace cancellation. Otherwise a locked database that
                    # finishes failing during shutdown is caught by the outer
                    # STORE_ERRORS handler, the cancellation disappears, and
                    # the supposedly stopped poll loop carries on running.
                    with contextlib.suppress(*STORE_ERRORS):
                        await write
                    raise
        except STORE_ERRORS as exc:
            reason = f"{type(exc).__name__}: {exc}"
            logger.warning("could not store reading: %s", reason)
            return reason
        return None

    def _count_failure(self, timestamp: datetime, reason: str, *, kind: str) -> None:
        """Record a failed poll against the status, whatever it failed at.

        ``kind`` is required rather than defaulted so that a new failure path
        cannot be added without saying which layer it belongs to. A stale kind
        would name the wrong fault on the status page, which is the mistake this
        field exists to stop.
        """
        self.status.last_failure = timestamp
        self.status.last_error = reason
        self.status.last_failure_kind = kind
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
