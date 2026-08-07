"""Tests for rollup maintenance: the coarse tiers must actually get built."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.models import Sample
from arraysense.store.sqlite_store import SqliteStore


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "roll.db"))


async def test_the_coarse_tiers_are_built_without_anyone_asking(tmp_path: Path) -> None:
    # Nothing scheduled the rollups, so on a live install the minute and hourly
    # tiers stayed empty forever. /api/history serves the minute tier for
    # anything over six hours and calibration reads it exclusively, so history
    # charts were blank and the drift warning was permanently stuck at
    # "no full charge found".
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    for i in range(400):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"battery_voltage_v": 55.9, "pv_total_power_w": 4000.0},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)

    minute = store.query(["battery_voltage_v"], now - timedelta(days=1), now, tier="minute")
    hourly = store.query(["battery_voltage_v"], now - timedelta(days=1), now, tier="hourly")
    assert minute, "the minute tier is what history and calibration read"
    assert hourly
    store.close()


async def test_maintenance_is_idempotent(tmp_path: Path) -> None:
    # It runs on a timer forever, so running it twice over the same range must
    # not duplicate a bucket or change a value.
    store = _store(tmp_path)
    now = datetime.now(tz=UTC)
    for i in range(200):
        store.append(
            Sample(
                timestamp=now - timedelta(seconds=11 * i),
                readings={"pv_total_power_w": 4000.0},
            )
        )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=now)
    first = store.query(["pv_total_power_w"], now - timedelta(days=1), now, tier="minute")
    await service.maintain_rollups(now=now)
    second = store.query(["pv_total_power_w"], now - timedelta(days=1), now, tier="minute")
    assert [r["timestamp"] for r in first] == [r["timestamp"] for r in second]
    assert [r["pv_total_power_w"] for r in first] == [r["pv_total_power_w"] for r in second]
    store.close()


async def test_maintenance_does_not_fail_the_poll_loop(tmp_path: Path) -> None:
    # Rollup maintenance is housekeeping. A failure in it must never stop the
    # collector, which is the thing that cannot be caught up on later.
    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    store.close()  # every rollup query will now raise
    await service.maintain_rollups(now=datetime.now(tz=UTC))


async def test_an_empty_database_is_not_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    await service.maintain_rollups(now=datetime.now(tz=UTC))
    store.close()


async def test_the_running_collector_builds_the_tiers_on_its_own(tmp_path: Path) -> None:
    # The point of the whole exercise: a service that is merely started, with
    # nobody calling anything, must end up with populated coarse tiers.
    import asyncio

    store = _store(tmp_path)
    service = CollectorService(source=FakeSource(), store=store, interval=0.01)
    await service.start()
    try:
        for _ in range(100):
            await asyncio.sleep(0.01)
            rows = store.query(
                ["pv_total_power_w"],
                datetime.now(tz=UTC) - timedelta(hours=1),
                datetime.now(tz=UTC) + timedelta(minutes=1),
                tier="minute",
            )
            if rows:
                break
        assert rows, "the collector ran but never built the minute tier"
    finally:
        await service.stop()
        store.close()
