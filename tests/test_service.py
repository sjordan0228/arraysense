"""test_service.py — the polling loop, its backoff, and yield mode.

Everything here runs against FakeSource and a temporary database. Intervals are
tiny so the suite stays fast; nothing sleeps for a real second.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from arraysense.collector import service as service_module
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.drivers.base import SampleBuildError
from arraysense.drivers.eg4_luxpower import source as source_module
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


# --- a sample that cannot be built (#29, narrowed by #42) ----------------------
#
# A reply the driver cannot turn into a sample refuses identically for that
# reply, though a later reply may well be fine — so this is not a permanent
# condition, and retrying is not pointless. Stopping the loop is still worse:
# it kills the asyncio task while uvicorn keeps serving, nothing restarts it,
# the watchdog only fires on a stalled loop, systemd sees no process exit, and
# not one gap row is written, so the outage leaves no trace at all.
#
# Since #42 the driver raises SampleBuildError for that case and BUILD_ERRORS
# catches only it. A bare ValueError is a bug in our own code and is deliberately
# left to escape.


class _MalformedSource(FakeSource):
    """A source whose read raises the way a driver does on a sample it cannot build.

    The example is an empty serial rather than an out-of-range slot: a slot past
    four is legal now, so a refused slot would be a failure the model no longer
    has, and a fixture that simulates an impossible error tests nothing real.
    """

    async def read(self) -> Sample:
        raise SampleBuildError("serial must not be empty; it is the module identity")


class _CrashingSource(FakeSource):
    """A source whose read raises what nothing catches, so the loop really dies.

    ``_MalformedSource`` cannot stand in for this: its SampleBuildError is caught
    and recorded as a gap, so the loop survives and ``stop()`` only ever cancels
    a live task — which is how the release-the-dongle test below passed while
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


class _BuildErrorSource:
    """A source that raises SampleBuildError on read."""

    device: str
    connected: bool

    def __init__(self, device: str = "CE00000000") -> None:
        self.device = device
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def read(self) -> Sample:
        raise SampleBuildError("to_sample refused the reply: slot must be positive")


async def test_a_samplebuilderror_is_recorded_as_a_gap(tmp_path: Path) -> None:
    # A SampleBuildError means the driver could not turn what the inverter
    # returned into a sample. It is deterministic for that reply — it will
    # repeat identically if the inverter sends the same malformed data — so it
    # is recorded as a gap and backed off from, exactly as an unreachable
    # inverter is.
    source = _BuildErrorSource()
    svc, store = _service(tmp_path, source=source)

    sample = await svc.poll_once()
    assert sample is not None and sample.is_failed
    assert "SampleBuildError" in (sample.error or "")
    assert "to_sample refused the reply" in (sample.error or "")
    assert svc.status.consecutive_failures == 1
    assert svc.status.total_failures == 1
    assert svc.status.connected is True  # the inverter answered
    store.close()


async def test_a_bare_valueerror_is_not_absorbed(tmp_path: Path) -> None:
    # A bare ValueError from somewhere other than sample construction is a
    # programming error and must surface rather than being recorded as a gap.
    # The collector should let it reach _loop, which logs it and lets the task
    # die so the watchdog and systemd can see it.

    class _BareValueErrorSource:
        device: str
        connected: bool

        def __init__(self) -> None:
            self.device = "CE00000000"
            self.connected = False

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def read(self) -> Sample:
            # A ValueError from somewhere other than Sample construction
            raise ValueError("unpack of incorrect length")

    source = _BareValueErrorSource()
    svc, store = _service(tmp_path, source=source)

    # Start the collector and wait for the task to die from the uncaught ValueError
    await svc.start()
    try:
        # Give the loop time to hit the error and die
        for _ in range(20):
            if not svc.status.running:
                break
            await asyncio.sleep(0.1)
        assert not svc.status.running, "task should have died from uncaught ValueError"
    finally:
        await svc.stop()
    store.close()


def test_a_model_refusal_is_wrapped_as_a_build_error_with_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The wrap sits on the construction site itself, so what it converts is a
    # model refusing what the driver assembled from a reply. Nothing a real
    # reply contains can make Sample refuse today — to_sample validates the
    # timestamp before it builds anything — so the refusal is injected rather
    # than contrived from a record, which would test the mapper, not the wrap.
    #
    # Deliberately not driven through to_sample's naive-timestamp guard: that
    # fires before any construction and is a caller's programming error, not a
    # malformed reply, so it must keep escaping as a bare ValueError. Wrapping
    # it would put a bug of ours back inside the gap path this exists to empty.
    def refuse(*args: object, **kwargs: object) -> Sample:
        raise ValueError("timestamp must be timezone-aware")

    monkeypatch.setattr(source_module, "Sample", refuse)
    runtime = SimpleNamespace(serial_number="CE00000000")

    with pytest.raises(SampleBuildError, match="Sample refused the reply"):
        source_module.to_sample(runtime, None)

    try:
        source_module.to_sample(runtime, None)
    except SampleBuildError as exc:
        assert isinstance(exc.__cause__, ValueError), "the original refusal must be chained"
        assert "timezone-aware" in str(exc.__cause__), "the cause must survive intact"


class TestEfficiencyMaintenance:
    """The summary pass, and the day it must not freeze."""

    @staticmethod
    def _configured(tmp_path: Path) -> tuple[CollectorService, SqliteStore]:
        from arraysense.settings import SettingsStore

        store = SqliteStore(str(tmp_path / "eff.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        settings.set("site.timezone", "America/Chicago")
        settings.set("site.latitude", 33.0)
        settings.set("site.longitude", -97.0)
        settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
        return CollectorService(source=FakeSource(), store=store, interval=0.01), store

    @staticmethod
    def _stage(store: SqliteStore, hours: range | list[int]) -> None:
        import sys

        sys.path.insert(0, "tests")
        from datetime import UTC, datetime, timedelta

        from test_efficiency import _insert_hourly

        base = datetime(2026, 8, 10, tzinfo=UTC)
        for h in hours:
            _insert_hourly(
                store._conn,
                base + timedelta(hours=h + 5),
                pv_power=3000.0,
                ghi=750.0,
                dni=800.0,
                dhi=110.0,
                wind=2.0,
                air_c=30.0,
            )

    async def test_today_is_rescored_as_the_day_fills_in(self, tmp_path: Path) -> None:
        """A day still being written is never final.

        Scored once in the morning and then skipped as "already done", today
        would hold three hours of dawn until midnight — and carry that figure
        into history when it became yesterday. Whatever else the pass skips,
        it must not skip the day that is still happening.
        """
        from datetime import UTC, datetime, timedelta
        from zoneinfo import ZoneInfo

        svc, store = self._configured(tmp_path)
        base = datetime(2026, 8, 10, tzinfo=UTC)
        local = datetime(2026, 8, 10, tzinfo=ZoneInfo("America/Chicago"))

        self._stage(store, [8, 9, 10])
        await svc.maintain_efficiency(now=base + timedelta(hours=16))
        morning = store.read_efficiency_days(local, local + timedelta(days=1))
        assert morning and morning[0].modelled_hours == 3

        self._stage(store, range(11, 19))
        await svc.maintain_efficiency(now=base + timedelta(hours=24))
        evening = store.read_efficiency_days(local, local + timedelta(days=1))
        store.close()
        assert evening[0].modelled_hours == 11, "today's score went stale"
        assert evening[0].actual_kwh > morning[0].actual_kwh

    async def test_an_unconfigured_installation_is_skipped_not_scored(self, tmp_path: Path) -> None:
        # No array described: there is nothing to compare a reading against,
        # and a row of zeros would be a claim about an array nobody has stated.
        store = SqliteStore(str(tmp_path / "bare.db"), device=TEST_DEVICE)
        svc = CollectorService(source=FakeSource(), store=store, interval=0.01)
        await svc.maintain_efficiency()
        from datetime import UTC, datetime

        rows = store.read_efficiency_days(
            datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)
        )
        store.close()
        assert rows == []

    async def test_an_algorithm_change_reopens_stored_efficiency_days(self, tmp_path: Path) -> None:
        """The first scorer revision advances a low configuration version once."""
        from arraysense.settings import CONFIG_VERSION_KEY, SettingsStore

        svc, store = self._configured(tmp_path)
        settings = SettingsStore(store)
        settings.set(CONFIG_VERSION_KEY, 1)

        await svc.maintain_efficiency()

        assert settings.get(CONFIG_VERSION_KEY) == 2
        store.close()

    async def test_the_first_scorer_revision_handles_config_version_zero(
        self, tmp_path: Path
    ) -> None:
        """A fresh installation starts its version sequence without a special case."""
        from arraysense.settings import CONFIG_VERSION_KEY, SettingsStore

        svc, store = self._configured(tmp_path)
        settings = SettingsStore(store)
        settings.set(CONFIG_VERSION_KEY, 0)

        await svc.maintain_efficiency()

        assert settings.get(CONFIG_VERSION_KEY) == 1
        store.close()

    async def test_a_scorer_revision_advances_a_high_config_version_once(
        self, tmp_path: Path
    ) -> None:
        """A code migration cannot be skipped because settings already advanced its counter."""
        from datetime import UTC, datetime, timedelta
        from zoneinfo import ZoneInfo

        from arraysense.efficiency import EfficiencyRow
        from arraysense.settings import CONFIG_VERSION_KEY, SettingsStore

        svc, store = self._configured(tmp_path)
        settings = SettingsStore(store)
        day = datetime(2026, 8, 10, tzinfo=ZoneInfo("America/Chicago"))
        self._stage(store, range(8, 16))
        store.write_efficiency_day(
            [EfficiencyRow(day, "East", 1.0, 1.0, 0.0, 0.0, 1, False, 1.0, 13)]
        )
        settings.set(CONFIG_VERSION_KEY, 13)

        now = datetime(2026, 8, 11, 12, tzinfo=UTC)
        await svc.maintain_efficiency(now=now)
        rescored = store.read_efficiency_days(day, day + timedelta(days=1))

        assert settings.get(CONFIG_VERSION_KEY) == 14
        assert rescored and all(row.config_version == 14 for row in rescored)
        assert any(row.actual_kwh != 1.0 for row in rescored)

        await svc.maintain_efficiency(now=now)
        assert settings.get(CONFIG_VERSION_KEY) == 14

        settings.set("panels.strings", "East | 1 | 10 | 410 | 25 | 90")
        await svc.maintain_efficiency(now=now)
        assert settings.get(CONFIG_VERSION_KEY) == 15
        assert all(
            row.config_version == 15
            for row in store.read_efficiency_days(day, day + timedelta(days=1))
        )
        store.close()


class TestEfficiencyBackfill:
    """The bounded historical backfill, and the days it must not touch twice."""

    @staticmethod
    def _configured(tmp_path: Path) -> tuple[CollectorService, SqliteStore]:
        from arraysense.settings import CONFIG_VERSION_KEY, SettingsStore

        store = SqliteStore(str(tmp_path / "backfill.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        settings.set("site.timezone", "America/Chicago")
        settings.set("site.latitude", 33.0)
        settings.set("site.longitude", -97.0)
        settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
        # Pinned after the versioned writes above, which bump the config version
        # for every key they touch; the tests want to know it exactly.
        settings.set(CONFIG_VERSION_KEY, 1)
        return CollectorService(source=FakeSource(), store=store, interval=0.01), store

    @staticmethod
    def _stage(store: SqliteStore, day: int, hours: range | list[int] = range(8, 17)) -> None:
        """Stage a sunlit August day of hourly rows, ``day`` being the local date."""
        from datetime import UTC, datetime, timedelta, timezone

        from test_efficiency import _insert_hourly

        local = timezone(timedelta(hours=-5))  # Chicago in August is UTC-5
        for h in hours:
            when = datetime(2026, 8, day, h, tzinfo=local)
            _insert_hourly(
                store._conn,
                when.astimezone(UTC),
                pv_power=3000.0,
                ghi=750.0,
                dni=800.0,
                dhi=110.0,
                wind=2.0,
                air_c=30.0,
            )
        # A real collector commits each poll; the backfill computes on its own
        # read-view connection, which cannot see rows left in an open
        # transaction on the primary one.
        store._conn.commit()

    @staticmethod
    def _midnight(day: int) -> datetime:
        from zoneinfo import ZoneInfo

        return datetime(2026, 8, day, 0, 0, tzinfo=ZoneInfo("America/Chicago"))

    async def test_every_historical_day_is_scored_but_not_today_or_yesterday(
        self, tmp_path: Path
    ) -> None:
        """A store with several days of hourly data is caught up, minus the live two."""
        from datetime import UTC, datetime, timedelta

        svc, store = self._configured(tmp_path)
        for day in (6, 7, 8, 9, 10):
            self._stage(store, day)
        now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)  # 13:00 local on the 10th

        await svc.backfill_efficiency(now=now)

        for day in (6, 7, 8):
            rows = store.read_efficiency_days(
                self._midnight(day), self._midnight(day) + timedelta(days=1)
            )
            assert rows, f"the backfill never reached {day} August"
            assert any(r.string_name == "" for r in rows)
        for day in (9, 10):
            rows = store.read_efficiency_days(
                self._midnight(day), self._midnight(day) + timedelta(days=1)
            )
            assert rows == [], "today and yesterday are the summary pass's job"
        store.close()

    async def test_a_pass_writes_at_most_efficiency_backfill_days(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The bound holds: one call takes the newest slice, older days wait."""
        from datetime import UTC, datetime

        monkeypatch.setattr(service_module, "EFFICIENCY_BACKFILL_DAYS", 3)
        svc, store = self._configured(tmp_path)
        for day in (1, 2, 3, 4, 5):
            self._stage(store, day)
        now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)

        await svc.backfill_efficiency(now=now)

        # Newest first: the 5th, 4th and 3rd were taken, the 1st and 2nd wait.
        scored = store.scored_days(1)
        assert scored == {int(self._midnight(d).timestamp()) for d in (3, 4, 5)}
        store.close()

    async def test_a_scored_day_is_skipped_until_the_config_version_moves(
        self, tmp_path: Path
    ) -> None:
        """A day scored against the current array is done; a version bump reopens it."""
        from datetime import UTC, datetime

        from arraysense.settings import CONFIG_VERSION_KEY, SettingsStore

        svc, store = self._configured(tmp_path)
        self._stage(store, 6)
        now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)

        await svc.backfill_efficiency(now=now)

        # A recompute would overwrite this marker; a skip leaves it standing.
        with store._conn:
            store._conn.execute(
                "UPDATE efficiency_day SET expected_kwh = 12345.0 WHERE string_name = ''"
            )
        await svc.backfill_efficiency(now=now)
        marker = store._conn.execute(
            "SELECT expected_kwh FROM efficiency_day WHERE string_name = ''"
        ).fetchone()
        assert marker is not None and marker[0] == 12345.0, (
            "the day was recomputed when it should have been skipped"
        )

        SettingsStore(store).set(CONFIG_VERSION_KEY, 2)
        await svc.backfill_efficiency(now=now)
        recomputed = store._conn.execute(
            "SELECT expected_kwh FROM efficiency_day WHERE string_name = ''"
        ).fetchone()
        assert recomputed is not None and recomputed[0] != 12345.0, (
            "the version bump did not make the day eligible again"
        )
        store.close()

    async def test_today_and_yesterday_are_never_touched_by_the_backfill(
        self, tmp_path: Path
    ) -> None:
        """The live two days belong to the summary pass, whatever data they hold."""
        from datetime import UTC, datetime, timedelta

        svc, store = self._configured(tmp_path)
        self._stage(store, 9)
        self._stage(store, 10)
        now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)

        await svc.backfill_efficiency(now=now)

        for day in (9, 10):
            rows = store.read_efficiency_days(
                self._midnight(day), self._midnight(day) + timedelta(days=1)
            )
            assert rows == [], f"the backfill wrote a summary the summary pass owns ({day} August)"
        store.close()

    async def test_a_store_with_no_hourly_rows_is_a_noop(self, tmp_path: Path) -> None:
        """No hourly data means nothing to backfill, and that is not an error."""
        from datetime import UTC, datetime

        svc, store = self._configured(tmp_path)
        now = datetime(2026, 8, 10, 18, 0, tzinfo=UTC)

        await svc.backfill_efficiency(now=now)  # must not raise

        rows = store.read_efficiency_days(
            datetime(2020, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC)
        )
        store.close()
        assert rows == []

    async def test_a_memory_backed_store_is_scored_inline(self) -> None:
        # ":memory:" cannot be reopened — every connect() makes a new, empty
        # database — so a threaded pass would score a different database and
        # report success while the real one stayed blank. The pass runs inline
        # instead, exactly as maintain_rollups does.
        from datetime import UTC, timedelta

        from arraysense.settings import CONFIG_VERSION_KEY, SettingsStore

        store = SqliteStore(":memory:", device=TEST_DEVICE)
        settings = SettingsStore(store)
        settings.set("site.timezone", "America/Chicago")
        settings.set("site.latitude", 33.0)
        settings.set("site.longitude", -97.0)
        settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
        settings.set(CONFIG_VERSION_KEY, 1)
        svc = CollectorService(source=FakeSource(), store=store, interval=0.01)

        self._stage(store, 6)
        await svc.backfill_efficiency(now=datetime(2026, 8, 10, 18, 0, tzinfo=UTC))

        rows = store.read_efficiency_days(self._midnight(6), self._midnight(6) + timedelta(days=1))
        store.close()
        assert rows, "a memory-backed pass scored a different, empty database"


class _SlowFailing:
    """A source whose read takes its time and then fails.

    The reference dongle answers an eleven-second interval in twelve to
    seventeen seconds, so the attempt that fails is long over by the time it
    fails. Which moment the gap is stamped with decides whether it lands on a
    second of its own or on the one the previous poll's reading was stamped
    with.
    """

    device = TEST_DEVICE

    def __init__(self, delay: float) -> None:
        """Fail every read, after ``delay`` seconds of pretending to work."""
        self._delay = delay

    async def connect(self) -> None:
        """Connect, as the dongle does before it goes quiet mid-read."""
        return None

    async def disconnect(self) -> None:
        """Release the slot."""
        return None

    async def read(self) -> Sample:
        """Take ``delay`` seconds and then report the inverter gone."""
        await asyncio.sleep(self._delay)
        raise TimeoutError("no reply from inverter")


async def test_a_gap_is_stamped_when_the_failure_was_seen(tmp_path: Path) -> None:
    """The stamp belongs to the failure, not to the attempt that led to it.

    Taken before the read, the stamp is up to a whole read older than the
    failure it records — and since a successful sample is stamped by the driver
    at read completion, and the cadence is max(interval, read time), that older
    stamp is exactly the second the previous successful poll was filed under.
    """
    delay = 0.05
    svc, store = _service(tmp_path, source=_SlowFailing(delay))
    before = datetime.now(UTC)
    gap = await svc.poll_once()
    store.close()
    assert gap is not None and gap.is_failed
    assert gap.timestamp >= before + timedelta(seconds=delay), (
        "the gap was stamped before the read it records the failure of"
    )
