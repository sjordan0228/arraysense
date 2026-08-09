"""test_service.py — the polling loop, its backoff, and yield mode.

Everything here runs against FakeSource and a temporary database. Intervals are
tiny so the suite stays fast; nothing sleeps for a real second.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from pathlib import Path

import pytest

from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.models import Sample
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE


def _service(tmp_path: Path, **kwargs: object) -> tuple[CollectorService, SqliteStore]:
    store = SqliteStore(str(tmp_path / "svc.db"), device=TEST_DEVICE)
    source = kwargs.pop("source", None) or FakeSource()
    svc = CollectorService(source=source, store=store, interval=0.01, **kwargs)  # type: ignore[arg-type]
    return svc, store


async def test_a_successful_poll_is_stored(tmp_path: Path) -> None:
    svc, store = _service(tmp_path)
    sample = await svc.poll_once()
    assert sample is not None and not sample.is_failed
    rows = store.query(["pv_total_power_w"], sample.timestamp, sample.timestamp)
    store.close()
    assert rows[0]["pv_total_power_w"] == 7614.0
    assert svc.status.total_samples == 1
    assert svc.status.consecutive_failures == 0


async def test_a_slow_store_commit_does_not_block_the_event_loop(tmp_path: Path) -> None:
    """A filesystem stall while committing a poll must not freeze HTTP work.

    The production symptom is process-wide, so this test measures the property
    that matters without pretending a Mac filesystem reproduces the LXC's ZFS
    latency. A timer thread releases the fake slow commit; while it is held, an
    asyncio task must continue to run.
    """
    svc, store = _service(tmp_path)
    real_append = store.append
    entered = threading.Event()
    release = threading.Event()

    def slow_append(sample: Sample, device: str | None = None) -> None:
        entered.set()
        assert release.wait(timeout=2.0), "test did not release the fake commit"
        real_append(sample, device=device)

    store.append = slow_append  # type: ignore[method-assign]
    ticks = 0

    async def count_event_loop_turns() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(count_event_loop_turns())
    release_timer = threading.Timer(0.25, release.set)
    release_timer.start()
    try:
        await svc.poll_once()
    finally:
        release.set()
        release_timer.join()
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        store.close()

    assert entered.is_set(), "the test never reached the store write"
    assert ticks >= 5, f"the event loop made only {ticks} turns during the slow commit"


async def test_a_failed_read_is_stored_as_a_gap(tmp_path: Path) -> None:
    # A chart that quietly skips an outage is worse than one that shows it.
    source = FakeSource(fail_on_read=ConnectionError("stream closed"))
    svc, store = _service(tmp_path, source=source)
    sample = await svc.poll_once()
    assert sample is not None and sample.is_failed
    assert "stream closed" in (sample.error or "")
    rows = store.query(["pv_total_power_w"], sample.timestamp, sample.timestamp)
    store.close()
    assert rows[0]["error"] is not None
    assert rows[0]["pv_total_power_w"] is None


async def test_a_failure_does_not_stop_the_loop(tmp_path: Path) -> None:
    source = FakeSource(fail_on_connect=ConnectionError("dongle busy"))
    svc, store = _service(tmp_path, source=source)
    for _ in range(3):
        await svc.poll_once()
    store.close()
    assert svc.status.consecutive_failures == 3
    assert svc.status.total_failures == 3


async def test_backoff_grows_and_is_capped(tmp_path: Path) -> None:
    svc, store = _service(tmp_path, max_backoff=0.5)
    assert svc._backoff() == pytest.approx(0.01)
    svc.status.consecutive_failures = 1
    assert svc._backoff() == pytest.approx(0.02)
    svc.status.consecutive_failures = 3
    assert svc._backoff() == pytest.approx(0.08)
    svc.status.consecutive_failures = 40
    assert svc._backoff() == pytest.approx(0.5)
    store.close()


async def test_recovery_resets_the_backoff(tmp_path: Path) -> None:
    source = FakeSource(fail_on_read=ConnectionError("blip"))
    svc, store = _service(tmp_path, source=source)
    await svc.poll_once()
    assert svc.status.consecutive_failures == 1
    source.fail_on_read = None
    await svc.poll_once()
    store.close()
    assert svc.status.consecutive_failures == 0
    assert svc._backoff() == pytest.approx(0.01)


async def test_yield_releases_the_dongle_and_skips_polling(tmp_path: Path) -> None:
    # The vendor's app needs the single TCP slot to push a firmware update.
    source = FakeSource()
    svc, store = _service(tmp_path, source=source)
    await svc.poll_once()
    assert source.connected
    until = await svc.yield_for(0.05)
    assert svc.is_yielding
    assert not source.connected
    assert svc.status.last_success is not None
    assert until > svc.status.last_success
    before = source.reads
    assert await svc.poll_once() is None
    assert source.reads == before
    store.close()


async def test_yield_expires_on_its_own(tmp_path: Path) -> None:
    svc, store = _service(tmp_path)
    await svc.yield_for(0.05)
    await asyncio.sleep(0.12)
    assert not svc.is_yielding
    assert svc.status.yield_until is None
    assert await svc.poll_once() is not None
    await svc.stop()
    store.close()


async def test_resume_ends_a_yield_early(tmp_path: Path) -> None:
    svc, store = _service(tmp_path)
    await svc.yield_for(60)
    assert svc.is_yielding
    await svc.resume()
    assert not svc.is_yielding
    assert await svc.poll_once() is not None
    await svc.stop()
    store.close()


async def test_yield_duration_must_be_positive(tmp_path: Path) -> None:
    svc, store = _service(tmp_path)
    with pytest.raises(ValueError, match="positive"):
        await svc.yield_for(0)
    store.close()


async def test_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    # A shutdown path that depends on knowing whether startup got that far is a
    # shutdown path that fails during a crash.
    svc, store = _service(tmp_path)
    await svc.stop()  # never started
    await svc.start()
    await svc.start()  # already running
    await asyncio.sleep(0.05)
    await svc.stop()
    await svc.stop()
    store.close()
    assert not svc.status.running


async def test_close_waits_for_an_off_loop_write_before_closing_its_store(
    tmp_path: Path,
) -> None:
    """Cancellation cannot stop a worker thread already inside append()."""
    store = SqliteStore(str(tmp_path / "owned.db"), device=TEST_DEVICE)
    svc = CollectorService(
        source=FakeSource(),
        store=store,
        interval=3600,
        owns_store=True,
    )
    real_append = store.append
    entered = threading.Event()
    release = threading.Event()

    def held_append(sample: Sample, device: str | None = None) -> None:
        entered.set()
        assert release.wait(timeout=2.0), "test did not release the held append"
        real_append(sample, device=device)

    store.append = held_append  # type: ignore[method-assign]
    await svc.start()
    assert await asyncio.to_thread(entered.wait, 2.0), "the poll never entered append"

    closing = asyncio.create_task(svc.close())
    await asyncio.sleep(0.05)
    assert not closing.done(), "close raced a write still running in its worker"
    assert store._conn.execute("SELECT 1").fetchone() == (1,)

    release.set()
    await closing
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        store._conn.execute("SELECT 1")


async def test_a_write_failure_finishing_during_close_does_not_swallow_cancellation(
    tmp_path: Path,
) -> None:
    """A worker's SQLite error must not replace the poll task's cancellation."""
    store = SqliteStore(str(tmp_path / "owned-failure.db"), device=TEST_DEVICE)
    svc = CollectorService(
        source=FakeSource(),
        store=store,
        interval=3600,
        owns_store=True,
    )
    entered = threading.Event()
    release = threading.Event()

    def held_failure(sample: Sample, device: str | None = None) -> None:
        entered.set()
        assert release.wait(timeout=2.0), "test did not release the held append"
        raise sqlite3.OperationalError("database is locked")

    store.append = held_failure  # type: ignore[method-assign]
    await svc.start()
    assert await asyncio.to_thread(entered.wait, 2.0), "the poll never entered append"

    closing = asyncio.create_task(svc.close())
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(closing, timeout=1.0)

    assert svc.status.running is False
    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        store._conn.execute("SELECT 1")


async def test_the_loop_collects_repeatedly(tmp_path: Path) -> None:
    source = FakeSource()
    svc, store = _service(tmp_path, source=source)
    await svc.start()
    await asyncio.sleep(0.1)
    await svc.stop()
    store.close()
    assert source.reads >= 2, source.reads
    assert svc.status.total_samples >= 2


async def test_interval_must_be_positive(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "x.db"), device=TEST_DEVICE)
    with pytest.raises(ValueError, match="interval"):
        CollectorService(source=FakeSource(), store=store, interval=0)
    store.close()


async def test_status_reports_what_happened(tmp_path: Path) -> None:
    source = FakeSource(fail_on_read=ConnectionError("nope"))
    svc, store = _service(tmp_path, source=source)
    await svc.poll_once()
    assert svc.status.last_failure is not None
    assert svc.status.last_error is not None and "nope" in svc.status.last_error
    assert svc.status.last_success is None
    source.fail_on_read = None
    await svc.poll_once()
    store.close()
    assert svc.status.last_success is not None
    assert svc.status.last_error is None


async def test_the_interval_is_a_cadence_not_a_pause_between_polls(tmp_path: Path) -> None:
    # Sleeping the whole interval after each read makes the real spacing read
    # time plus interval. On the reference dongle a read takes twelve to
    # seventeen seconds, so an eleven second interval produced samples
    # twenty-five seconds apart — under half the rate the setting asks for.
    svc, store = _service(tmp_path)
    loop = asyncio.get_running_loop()
    interval = svc._backoff()

    # A read that took two thirds of the interval leaves a third to wait.
    started = loop.time() - interval * (2 / 3)
    assert svc._wait_from(started) == pytest.approx(interval / 3, abs=interval / 20)

    # One that took no time at all waits the whole interval.
    assert svc._wait_from(loop.time()) == pytest.approx(interval, abs=interval / 20)
    store.close()


async def test_a_read_slower_than_its_interval_gets_no_sleep_rather_than_a_negative_one(
    tmp_path: Path,
) -> None:
    # The cadence floor is what the dongle can answer at. Asking for eleven
    # seconds when a read takes fourteen means fourteen, not a negative sleep.
    svc, store = _service(tmp_path)
    loop = asyncio.get_running_loop()
    assert svc._wait_from(loop.time() - 10.0) == 0.0
    store.close()


async def test_backoff_still_wins_while_the_inverter_is_unreachable(tmp_path: Path) -> None:
    # A failing read returns fast, so scheduling by cadence would retry a dead
    # connection at full speed — which is the one thing backing off exists to
    # prevent.
    svc, store = _service(tmp_path)
    svc.status.consecutive_failures = 3
    loop = asyncio.get_running_loop()
    assert svc._wait_from(loop.time() - 5.0) == pytest.approx(svc._backoff())
    store.close()


async def test_a_loop_that_stops_running_is_reported_as_stalled(tmp_path: Path) -> None:
    # Restart=always covers a process that exits. It cannot see this process
    # serving pages perfectly while collecting nothing, which is what a dead
    # poll task or a read that never returns both look like from outside.
    from datetime import UTC, datetime, timedelta

    svc, store = _service(tmp_path)
    svc._stall_after = timedelta(minutes=20)
    svc.status.running = True
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    svc.status.started_at = now - timedelta(minutes=90)

    # Nothing at all since startup, well past the threshold.
    assert svc.stalled_for(now) is not None

    # A poll that succeeded a moment ago is fine.
    svc.status.last_success = now - timedelta(seconds=30)
    assert svc.stalled_for(now) is None
    store.close()


async def test_an_inverter_that_is_simply_absent_is_not_a_stall(tmp_path: Path) -> None:
    # Every poll failing is the loop working: it records the gap and backs off.
    # Restarting over that loses the backoff and thrashes for as long as the
    # inverter is away, which is the opposite of what a watchdog is for.
    from datetime import UTC, datetime, timedelta

    svc, store = _service(tmp_path)
    svc._stall_after = timedelta(minutes=20)
    svc.status.running = True
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    svc.status.started_at = now - timedelta(hours=6)
    svc.status.last_success = now - timedelta(hours=5)
    svc.status.last_failure = now - timedelta(seconds=20)
    assert svc.stalled_for(now) is None
    store.close()


async def test_a_yielded_dongle_is_not_a_stall(tmp_path: Path) -> None:
    # Polling has been handed over deliberately so the vendor's app can push a
    # firmware update. Restarting mid-update would take the dongle back.
    from datetime import UTC, datetime, timedelta

    svc, store = _service(tmp_path)
    svc._stall_after = timedelta(minutes=20)
    svc.status.running = True
    svc.status.yielding = True
    now = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    svc.status.started_at = now - timedelta(hours=2)
    assert svc.stalled_for(now) is None
    store.close()


async def test_a_dead_loop_is_a_stall_even_though_running_went_false(tmp_path: Path) -> None:
    # The case the watchdog exists for, and the one it missed. `_loop` clears
    # `running` before re-raising, so a check that stood down when running was
    # False stood down precisely when the loop had died — leaving the web server
    # serving stale pages over a collector that had stopped, forever.
    import sqlite3

    svc, store = _service(tmp_path)

    async def boom() -> None:
        # A locked database, which is what a long scrub does to a live poll and
        # is not among the transport errors poll_once recovers from.
        raise sqlite3.OperationalError("database is locked")

    await svc.start()
    await asyncio.sleep(0.05)
    svc._source.read = boom  # type: ignore[method-assign, assignment]
    await asyncio.sleep(0.15)

    assert svc._task is not None and svc._task.done(), "the loop should have died"
    assert svc.status.running is False
    assert svc.stalled_for() is not None, "a dead loop must read as stalled"
    store.close()


async def test_a_service_that_was_never_started_is_not_a_stall(tmp_path: Path) -> None:
    # Quiet on purpose. There is no loop to be dead.
    svc, store = _service(tmp_path)
    assert svc.stalled_for() is None
    store.close()


async def test_a_stopped_service_is_not_a_stall(tmp_path: Path) -> None:
    # Also quiet on purpose: somebody asked it to stop.
    svc, store = _service(tmp_path)
    await svc.start()
    await asyncio.sleep(0.02)
    await svc.stop()
    assert svc.stalled_for() is None
    store.close()


async def test_backoff_survives_an_inverter_that_is_gone_for_days(tmp_path: Path) -> None:
    # 2**consecutive_failures is evaluated before the cap is applied, so it
    # grows an integer nobody needs: at 1024 failures it is too large to be a
    # float and raises. With a five-minute cap that is about eighty-five hours
    # of an unreachable inverter — a long holiday, a tripped breaker — after
    # which the loop dies rather than carrying on retrying every five minutes.
    svc, store = _service(tmp_path, max_backoff=300.0)
    for failures in (1, 10, 100, 1024, 5000, 100_000):
        svc.status.consecutive_failures = failures
        assert svc._backoff() == pytest.approx(300.0) or failures < 20
    store.close()


async def test_a_write_that_fails_is_recorded_and_survived(tmp_path: Path) -> None:
    # store.append sits outside the handler that recovers from transport
    # errors, and sqlite3.OperationalError is not among them — so a database
    # held by something else long enough to exhaust the busy timeout takes the
    # whole collector with it. The scrub tool clears in a single transaction
    # and can do exactly that.
    import sqlite3

    svc, store = _service(tmp_path)

    def wedged(sample: object, device: str | None = None) -> None:
        raise sqlite3.OperationalError("database is locked")

    svc._store.append = wedged  # type: ignore[method-assign]
    # It must come back rather than raise, and count as a failure so the loop
    # backs off instead of hammering a database that is busy.
    await svc.poll_once()
    assert svc.status.consecutive_failures == 1
    assert svc.status.last_error is not None
    assert "locked" in svc.status.last_error
    store.close()


async def test_the_loop_keeps_running_through_a_wedged_database(tmp_path: Path) -> None:
    # The point of the above: the loop is still alive afterwards, so when the
    # database frees up the next poll simply works.
    import sqlite3

    svc, store = _service(tmp_path)
    real = svc._store.append
    calls = {"n": 0}

    def sometimes(sample: object, device: str | None = None) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise sqlite3.OperationalError("database is locked")
        real(sample, device=device)  # type: ignore[arg-type]

    svc._store.append = sometimes  # type: ignore[method-assign]
    await svc.start()
    await asyncio.sleep(0.2)
    assert svc._task is not None and not svc._task.done(), "the loop must survive"
    await svc.stop()
    assert calls["n"] > 2, "it should have gone on trying"
    store.close()


async def test_a_wedged_database_does_not_report_the_inverter_as_gone(tmp_path: Path) -> None:
    # The read succeeded and only the write failed, so the connection is fine.
    # Saying otherwise sends whoever is reading the status page after the
    # dongle, the WiFi and the breaker while the actual problem is the disk.
    import sqlite3

    svc, store = _service(tmp_path)

    def wedged(sample: object, device: str | None = None) -> None:
        raise sqlite3.OperationalError("database is locked")

    svc._store.append = wedged  # type: ignore[method-assign]
    await svc.poll_once()
    assert svc.status.connected is True
    store.close()


async def test_a_reading_is_filed_under_the_source_not_the_stores_default(
    tmp_path: Path,
) -> None:
    # The store's default is for readers. What a poll records has to be the
    # inverter that answered, or a second collector writing through the same
    # store would file its readings under the first one's serial.
    store = SqliteStore(str(tmp_path / "svc.db"), device=TEST_DEVICE)
    svc = CollectorService(source=FakeSource(device="CE00000001"), store=store, interval=0.01)
    sample = await svc.poll_once()
    assert sample is not None
    rows = store._conn.execute("SELECT DISTINCT device FROM inverter_raw").fetchall()
    store.close()
    assert rows == [("CE00000001",)]


async def test_a_recorded_gap_names_the_inverter_that_went_quiet(tmp_path: Path) -> None:
    # An outage stamped with the wrong serial reports the fault on a machine
    # that was working.
    store = SqliteStore(str(tmp_path / "svc.db"), device=TEST_DEVICE)
    svc = CollectorService(
        source=FakeSource(fail_on_read=ConnectionError("gone"), device="CE00000001"),
        store=store,
        interval=0.01,
    )
    await svc.poll_once()
    rows = store._conn.execute("SELECT device, error FROM inverter_raw").fetchall()
    store.close()
    assert rows == [("CE00000001", "ConnectionError: gone")]


# --- a sample that cannot be built (#29) ---------------------------------------
#
# A malformed sample is deterministic: it will fail identically on every poll, so
# retrying is pointless — but stopping the loop is worse. The ValueError a sample
# raises when it refuses what the driver assembled escapes poll_once today, kills
# the asyncio task while uvicorn keeps serving, and nothing restarts it: the
# watchdog only fires on a stalled loop and systemd sees no process exit. Not one
# gap row is written either, so the outage leaves no trace at all.


class _MalformedSource(FakeSource):
    """A source whose read raises the way a driver does on a sample it cannot build.

    The example is an empty serial rather than an out-of-range slot: a slot past
    four is legal now, so a refused slot would be a failure the model no longer
    has, and a fixture that simulates an impossible error tests nothing real.
    """

    async def read(self) -> Sample:
        raise ValueError("serial must not be empty; it is the module identity")


class _CrashingSource(FakeSource):
    """A source whose read raises what nothing catches, so the loop really dies.

    ``_MalformedSource`` cannot stand in for this: its ValueError is caught and
    recorded as a gap, so the loop survives and ``stop()`` only ever cancels a
    live task — which is how the release-the-dongle test below passed while
    proving nothing. Demonstrating that a *dead* loop still releases the socket
    needs a failure that escapes ``poll_once`` altogether, which is what a bug in
    our own code is: neither a transport fault nor a sample that would not build.
    """

    async def read(self) -> Sample:
        raise RuntimeError("a bug in our own code, not a transport fault")


async def test_a_sample_that_cannot_be_built_is_recorded_as_a_gap(tmp_path: Path) -> None:
    svc, store = _service(tmp_path, source=_MalformedSource())
    sample = await svc.poll_once()
    assert sample is not None and sample.is_failed
    assert "serial" in (sample.error or "")
    rows = store.query(["pv_total_power_w"], sample.timestamp, sample.timestamp)
    store.close()
    # The outage has to be visible in the history, exactly as an unreachable
    # inverter is. A silent hole is the one thing worse than a recorded one.
    assert rows[0]["error"] is not None
    assert rows[0]["pv_total_power_w"] is None


async def test_a_sample_that_cannot_be_built_leaves_the_loop_running(tmp_path: Path) -> None:
    svc, store = _service(tmp_path, source=_MalformedSource())
    for _ in range(3):
        await svc.poll_once()
    store.close()
    assert svc.status.total_failures == 3
    assert svc.status.consecutive_failures == 3


async def test_a_sample_that_cannot_be_built_does_not_blame_the_inverter(tmp_path: Path) -> None:
    svc, store = _service(tmp_path, source=_MalformedSource())
    await svc.poll_once()
    store.close()
    # The inverter answered: connect() returned and the registers arrived, and
    # only turning the reply into a sample failed. Reporting the connection as
    # down sends whoever reads it after the dongle, the WiFi and the breaker over
    # a fault that is in this build's own decoding.
    assert svc.status.connected is True
    assert svc.status.last_failure_kind == "build"


async def test_a_transport_failure_still_marks_the_connection_down(tmp_path: Path) -> None:
    # The other side of the same distinction: an unreachable inverter must still
    # report the connection down, or the page calls a real outage a disk fault.
    svc, store = _service(tmp_path, source=FakeSource(fail_on_read=ConnectionError("no route")))
    await svc.poll_once()
    store.close()
    assert svc.status.connected is False
    assert svc.status.last_failure_kind == "transport"


async def test_the_dongle_is_released_even_when_the_loop_died(tmp_path: Path) -> None:
    # The dongle accepts exactly one TCP client, so a loop that dies without
    # disconnecting holds the slot until the dongle times it out — blocking both
    # the restart and the owner's own vendor app. stop() re-raises the dead
    # task's exception before it reaches disconnect, so the socket leaks.
    source = _CrashingSource()
    svc, store = _service(tmp_path, source=source)
    await svc.start()
    # Wait for the loop to actually die, and prove that it did. Without this the
    # test is vacuous: with a source whose failure poll_once *catches*, the loop
    # survives, stop() only ever cancels a live task, and the whole release path
    # this test exists for can be deleted with the suite still green.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if not svc.status.running:
            break
    assert not svc.status.running, "the loop never died, so this test proves nothing"
    await svc.stop()
    store.close()
    assert source.connected is False
