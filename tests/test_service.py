"""test_service.py — the polling loop, its backoff, and yield mode.

Everything here runs against FakeSource and a temporary database. Intervals are
tiny so the suite stays fast; nothing sleeps for a real second.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.store.sqlite_store import SqliteStore


def _service(tmp_path: Path, **kwargs: object) -> tuple[CollectorService, SqliteStore]:
    store = SqliteStore(str(tmp_path / "svc.db"))
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
    store = SqliteStore(str(tmp_path / "x.db"))
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
