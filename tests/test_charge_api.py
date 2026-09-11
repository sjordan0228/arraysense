"""test_charge_api.py — the charge endpoints over a register map that can start.

No hardware: the app stands on a temporary store and a fake source holding a
register map, so every read-back is what the "device" reports after the write
rather than what the API assumed going in. The fake carries the failure modes
the endpoints have to survive — a refused write, a refused restore — because
the refusal paths are the safety story here, not an afterthought.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from arraysense.api import app as app_module
from arraysense.api.app import create_app
from arraysense.api.routes import expire_recorded_charge
from arraysense.charge import (
    ChargeConfig,
    ChargeWriteRefusedError,
    GridChargeChange,
    decode_charge_config,
    pack_time,
)
from arraysense.charge_override import ChargeOverride, load_override, save_override
from arraysense.collector.service import CollectorService
from arraysense.config import Config
from arraysense.models import Sample
from arraysense.settings import (
    CHARGE_OVERRIDE_KEY,
    INVERTER_LIMIT_KEY,
    SETTING_TIMEZONE,
    SettingsStore,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

T0 = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)

# What the "inverter" holds before any charge: the enable bit of register 21
# is clear, power sits at 1 kW, and one window is set. All nine write
# addresses are present so a start reaches the wire instead of being refused
# for want of an undo.
ORIGINAL_REGISTERS = {
    21: 0x00,
    66: 10,
    67: 0,
    68: pack_time(4, 0),
    69: pack_time(6, 0),
    70: 0,
    71: 0,
    72: 0,
    73: 0,
    120: 0x02,
    158: 460,
    159: 540,
    160: 0,
    161: 100,
}


class ChargeSource:
    """A register map that starts and restores a charge the way the driver does.

    It records every start's arguments so a test can check the power that
    reached it, and it takes the two faults the endpoints must survive. The
    probe runs at the top of a start, while the caller can still be checked:
    that is how a test sees what was already stored before the inverter was
    touched.
    """

    def __init__(
        self,
        *,
        fail_start: Exception | None = None,
        fail_restore: Exception | None = None,
        probe: Callable[[], None] | None = None,
        read_gate: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.registers = dict(ORIGINAL_REGISTERS)
        self.start_calls: list[dict[str, Any]] = []
        self.restore_calls: list[ChargeConfig] = []
        self.power_calls: list[int] = []
        self.fail_power: Exception | None = None
        self.fail_start = fail_start
        self.fail_restore = fail_restore
        self.probe = probe
        # An awaitable run in place of the first read of the configuration, which
        # is the window a second charge request has to be kept out of. The gate is
        # how a test holds one request inside the transaction while another
        # arrives.
        self.read_gate = read_gate
        self.reads = 0

    async def read_charge_config(self) -> ChargeConfig:
        self.reads += 1
        if self.read_gate is not None and self.reads == 1:
            await self.read_gate()
        return decode_charge_config(
            self.registers,
            quick_charge_remaining_s=None,
            read_at=datetime.now(tz=UTC),
        )

    async def start_grid_charge(
        self,
        *,
        power_w: int,
        duration_min: int,
        target_soc_pct: int = 100,
        now: datetime | None = None,
    ) -> GridChargeChange:
        if self.probe is not None:
            self.probe()
        self.start_calls.append({"power_w": power_w, "duration_min": duration_min, "now": now})
        saved = await self.read_charge_config()
        start = now if now is not None else datetime.now(tz=UTC)
        until = start + timedelta(minutes=duration_min)
        if self.fail_start is not None:
            # A refused write is not a clean one: some registers may already
            # be changed by the time the transport answers, which is why the
            # record has to exist before the write rather than after it.
            self.registers[21] |= 1 << 7
            self.registers[66] = power_w // 100
            raise self.fail_start
        self.registers[21] |= 1 << 7
        self.registers[66] = power_w // 100
        self.registers[67] = target_soc_pct
        self.registers[68] = pack_time(start.hour, start.minute)
        self.registers[69] = pack_time(until.hour, until.minute)
        applied = await self.read_charge_config()
        return GridChargeChange(saved=saved, applied=applied, until=until)

    async def set_grid_charge_power(self, *, power_w: int) -> ChargeConfig:
        self.power_calls.append(power_w)
        if self.fail_power is not None:
            raise self.fail_power
        self.registers[66] = power_w // 100
        return await self.read_charge_config()

    async def restore_grid_charge(self, saved: ChargeConfig) -> ChargeConfig:
        self.restore_calls.append(saved)
        if self.fail_restore is not None:
            raise self.fail_restore
        self.registers = dict(saved.registers)
        return await self.read_charge_config()


class PlainSource:
    """An installation whose driver has none of the charge methods."""


@contextmanager
def _rig(
    tmp_path: Path,
    source: Any,
    *,
    load_w: float | None = 5000.0,
    limit: int | None = 12000,
) -> Iterator[Any]:
    """The whole app over a temporary store, one seeded load row and a limit.

    The seeded house draws 5000 W against a 12000 W site limit, the shape the
    planning layer was written against. Tests that need a different house or
    no limit at all say so here rather than editing the store afterwards.
    """
    app, store, settings = _assembled(tmp_path, source, load_w=load_w, limit=limit)
    try:
        with TestClient(app) as client:
            yield client, source, store, settings
    finally:
        store.close()


def _assembled(
    tmp_path: Path,
    source: Any,
    *,
    load_w: float | None = 5000.0,
    limit: int | None = 12000,
) -> tuple[Any, SqliteStore, SettingsStore]:
    """The app itself, for a test that has to drive it two requests at a time.

    TestClient serializes its calls, so a test about what happens when two
    charge requests overlap cannot use it: it needs the ASGI app and an async
    client, which is what this returns.
    """
    store = SqliteStore(str(tmp_path / "charge.db"), device=TEST_DEVICE)
    if load_w is not None:
        # Stamped now, not at T0: a charge is sized against what the house is
        # drawing at the moment of the press, and the endpoint refuses a reading
        # old enough that nobody is watching the house. A fixed timestamp would
        # make every charge test a test of that refusal instead.
        store.append(Sample(timestamp=datetime.now(tz=UTC), readings={"load_power_w": load_w}))
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "charge.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=source, store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    settings = SettingsStore(store)
    if limit is not None:
        settings.set(INVERTER_LIMIT_KEY, limit)
    return app, store, settings


# --- the preview the power slider reads -------------------------------------------


def test_the_plan_says_what_a_request_would_actually_run_at(tmp_path: Path) -> None:
    """A charge power is a request. The page shows a handle that reaches the
    inverter's maximum, and the site limit, the house and the reserve come off
    whatever is chosen — so the preview says both numbers, with the numbers
    behind the decision, before anything is written."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, _settings):
        body = client.get("/api/charge/plan", params={"power_w": 10000}).json()
        assert body["requested_w"] == 10000
        # A 12000 W site limit, 5000 W of house, 1000 W held back.
        assert body["effective_w"] == 6000
        assert body["site_limit_w"] == 12000
        assert body["house_load_w"] == 5000
        assert body["margin_w"] == 1000
        assert "6000" in body["reason"]
        # The handle's own ends come from here, so the range on screen is the
        # range the server enforces: the driver's floor and the inverter's AC
        # charge maximum.
        assert body["min_w"] == 1000
        assert body["max_w"] == 12000
        # Read-only, and it asks the inverter nothing, which is what lets it
        # answer every time the handle moves.
        assert src.reads == 0
        assert src.start_calls == []


def test_the_plan_and_the_start_decide_the_same_power(tmp_path: Path) -> None:
    """One rule, not two. A preview that promised a power the start then refused
    would be worse than no preview, so the plan is compared with what the start
    actually sent."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, _settings):
        plan = client.get("/api/charge/plan", params={"power_w": 10000}).json()
        started = client.post("/api/charge/start", json={"power_w": 10000})
        assert started.status_code == 200
        assert src.start_calls[0]["power_w"] == plan["effective_w"]


def test_the_plan_refuses_to_size_a_charge_without_a_live_reading(tmp_path: Path) -> None:
    """The preview meets the same refusals the start does, in the same words: an
    installation with no site limit, or a house nobody is measuring, has no power
    to offer. The page shows that rather than a button."""
    source = ChargeSource()
    with _rig(tmp_path, source, limit=None) as (client, _src, _store, _settings):
        unbounded = client.get("/api/charge/plan", params={"power_w": 3000}).json()
        assert unbounded["effective_w"] is None
        assert "no site limit" in unbounded["reason"]
        assert unbounded["site_limit_w"] is None
    unmeasured = ChargeSource()
    # Its own directory: the settings live in the database file, and a second
    # rig on the same path would inherit the first one's site limit.
    alone = tmp_path / "unmeasured"
    alone.mkdir()
    with _rig(alone, unmeasured, load_w=None) as (client, src, _store, _settings):
        body = client.get("/api/charge/plan", params={"power_w": 3000}).json()
        assert body["effective_w"] is None
        assert "not being measured" in body["reason"]
        assert body["house_load_w"] is None
        assert src.reads == 0


# --- refusals before anything reaches the inverter -------------------------------


def test_start_refuses_when_the_driver_cannot_charge(tmp_path: Path) -> None:
    with _rig(tmp_path, PlainSource()) as (client, _src, _store, settings):
        start = client.post("/api/charge/start", json={})
        assert start.status_code == 404
        assert "cannot start a grid charge" in start.json()["detail"]
        stop = client.post("/api/charge/stop")
        assert stop.status_code == 404
        assert "cannot restore a grid charge" in stop.json()["detail"]
        assert settings.get(CHARGE_OVERRIDE_KEY) == ""


def test_start_refuses_when_no_site_limit_is_configured(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source, limit=None) as (client, _src, _store, settings):
        response = client.post("/api/charge/start", json={})
        assert response.status_code == 409
        assert "emporia.inverter_limit_w" in response.json()["detail"]
        assert settings.get(CHARGE_OVERRIDE_KEY) == ""
        assert source.start_calls == []


def test_start_refuses_when_the_house_leaves_no_headroom(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source, load_w=11500.0) as (client, _src, _store, _settings):
        response = client.post("/api/charge/start", json={})
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "11500" in detail
        assert "12000" in detail
        assert source.start_calls == []


def test_start_bounds_the_power_to_what_the_site_has_left(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, _settings):
        response = client.post("/api/charge/start", json={"power_w": 10000})
        assert response.status_code == 200
        asked = [(call["power_w"], call["duration_min"]) for call in src.start_calls]
        assert asked == [(6000, 600)]
        body = response.json()
        assert body["power_w"] == 6000
        assert body["requested_w"] == 10000


def test_the_window_is_cut_on_the_installation_clock(tmp_path: Path) -> None:
    """The window registers hold clock times, and the inverter reads them in
    the installation's own zone. Cut from UTC instead, a press at 22:40 local
    on the reference installation packed 03:39-13:39 into registers 68 and 69,
    which is a window at the wrong hour of the day rather than a long one.
    """
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        settings.set(SETTING_TIMEZONE, "America/Chicago")
        response = client.post("/api/charge/start", json={})
        assert response.status_code == 200
        assert len(src.start_calls) == 1
        sent = src.start_calls[0]["now"]
        assert sent is not None
        site = ZoneInfo("America/Chicago")
        assert sent.utcoffset() == site.utcoffset(sent)
        assert sent.utcoffset() != timedelta(0)


async def test_two_starts_at_once_leave_one_charge_and_one_usable_record(tmp_path: Path) -> None:
    """A start is a transaction over the device and the record, and it awaits
    throughout. Two of them overlapping can both read "no charge recorded", and
    the second would then save a configuration the first had already changed —
    an undo that restores the charge instead of the inverter's own settings. The
    page's disabled button covers one tab; this covers the API."""
    source = ChargeSource()
    app, store, settings = _assembled(tmp_path, source)
    # The first start is held inside its read of the inverter, which is the
    # window two overlapping starts have to be kept out of.
    reading = asyncio.Event()

    async def hold_the_first_read() -> None:
        reading.set()
        await asyncio.sleep(0.2)

    source.read_gate = hold_the_first_read
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/api/charge/start", json={}))
        await reading.wait()
        second = await client.post("/api/charge/start", json={})
        first_response = await first

    assert first_response.status_code == 200
    assert second.status_code == 409
    assert "already running" in second.json()["detail"]
    # One charge ran, and the record that stands is the configuration read
    # before it — not the charge's own settings, which is what a second start
    # racing the first would have saved.
    assert len(source.start_calls) == 1
    record = load_override(settings)
    assert record is not None
    assert record.saved.registers == dict(ORIGINAL_REGISTERS)
    store.close()


async def test_a_restore_the_inverter_did_not_take_keeps_the_record(tmp_path: Path) -> None:
    """The record is cleared only by a restore that landed. The driver compares
    the read-back, so a stop that left the inverter charged answers 502 and the
    record stays — which is the only thing that can try again."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        assert client.post("/api/charge/start", json={}).status_code == 200
        src.fail_restore = ChargeWriteRefusedError(
            "the inverter read back a different restore than it was written: {21: (2, 130)}"
        )
        stopped = client.post("/api/charge/stop")
        assert stopped.status_code == 502
        assert "kept" in stopped.json()["detail"]
        assert settings.get(CHARGE_OVERRIDE_KEY) != ""


def test_start_refuses_when_the_house_load_is_not_being_measured(tmp_path: Path) -> None:
    """The site limit caps the charge, not the charge plus the house, so a
    charge sized with no idea what the house is drawing can put more on the site
    than the site was limited to. No reading at all is refused rather than
    guessed at, and the reason says which reading is missing."""
    source = ChargeSource()
    with _rig(tmp_path, source, load_w=None) as (client, src, _store, settings):
        response = client.post("/api/charge/start", json={})
        assert response.status_code == 409
        assert "not being measured" in response.json()["detail"]
        assert "missing" in response.json()["detail"]
        assert src.start_calls == []
        assert settings.get(CHARGE_OVERRIDE_KEY) == ""


def test_start_refuses_a_load_reading_old_enough_that_nobody_is_watching(
    tmp_path: Path,
) -> None:
    """Recency is not health either way round: a reading from half an hour ago
    describes a house at that moment, and the collector polls every few seconds,
    so a number this old means the collector is not running."""
    source = ChargeSource()
    app, store, settings = _assembled(tmp_path, source, load_w=None)
    store.append(
        Sample(
            timestamp=datetime.now(tz=UTC) - timedelta(minutes=30),
            readings={"load_power_w": 5000.0},
        )
    )
    with TestClient(app) as client:
        response = client.post("/api/charge/start", json={})
    assert response.status_code == 409
    assert "not being measured" in response.json()["detail"]
    assert source.start_calls == []
    assert settings.get(CHARGE_OVERRIDE_KEY) == ""
    store.close()


# --- the record around the write --------------------------------------------------


def test_the_record_is_written_before_the_inverter_is_touched(tmp_path: Path) -> None:
    seen: list[Any] = []
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, store, _settings):
        src.probe = lambda: seen.append(load_override(SettingsStore(store)))
        response = client.post("/api/charge/start", json={})
        assert response.status_code == 200
        assert len(seen) == 1
        assert seen[0] is not None
        assert seen[0].requested_w == 3000
        assert seen[0].saved.registers == dict(ORIGINAL_REGISTERS)


def test_a_refused_write_leaves_a_record_a_stop_can_use(tmp_path: Path) -> None:
    source = ChargeSource(
        fail_start=ChargeWriteRefusedError("the inverter did not acknowledge the charge write")
    )
    with _rig(tmp_path, source) as (client, src, _store, settings):
        start = client.post("/api/charge/start", json={})
        assert start.status_code == 502
        assert "kept" in start.json()["detail"]
        assert settings.get(CHARGE_OVERRIDE_KEY) != ""
        assert src.registers[21] & (1 << 7)
        stop = client.post("/api/charge/stop")
        assert stop.status_code == 200
        assert src.registers == dict(ORIGINAL_REGISTERS)
        assert settings.get(CHARGE_OVERRIDE_KEY) == ""


def test_a_second_start_is_refused_and_the_first_record_survives(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        first = client.post("/api/charge/start", json={"power_w": 3000})
        assert first.status_code == 200
        record = load_override(settings)
        assert record is not None
        second = client.post("/api/charge/start", json={"power_w": 10000})
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]
        kept = load_override(settings)
        assert kept is not None
        assert kept.requested_w == 3000
        assert kept.saved.registers == dict(ORIGINAL_REGISTERS)
        assert len(src.start_calls) == 1


async def test_a_charge_whose_window_has_closed_is_put_back_without_a_press(
    tmp_path: Path,
) -> None:
    """A device schedule has no date in it, so the window written for this
    charge opens again the next night at the same hour. The record is what knows
    the charge is over, and the expiry is the stop the owner would otherwise
    have to press — taken at the window's edge, and doing nothing at all while
    the window is still open."""
    source = ChargeSource()
    app, store, settings = _assembled(tmp_path, source)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/charge/start", json={})).status_code == 200
    record = load_override(settings)
    assert record is not None
    lock = app.state.charge_lock
    # Inside the window: nothing is written, nothing is cleared. The window
    # closes *at* its end, which is the rule the record itself reports on.
    assert (
        await expire_recorded_charge(store, source, lock, now=record.until - timedelta(seconds=1))
        is False
    )
    assert source.restore_calls == []
    assert load_override(settings) is not None
    # Past the window: the record's own saved configuration goes back, and the
    # record is cleared only because the restore worked.
    assert (
        await expire_recorded_charge(store, source, lock, now=record.until + timedelta(minutes=1))
        is True
    )
    assert source.restore_calls == [record.saved]
    assert source.registers == dict(ORIGINAL_REGISTERS)
    assert load_override(settings) is None
    store.close()


async def test_the_expiry_keeps_the_record_when_the_inverter_cannot_be_put_back(
    tmp_path: Path,
) -> None:
    """The record is cleared only by a restore that landed. An expiry that
    cleared it on a failed write would leave a charge running with nothing left
    that describes how to end it, and no button that could try again."""
    source = ChargeSource()
    app, store, settings = _assembled(tmp_path, source)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/api/charge/start", json={})).status_code == 200
    record = load_override(settings)
    assert record is not None
    source.fail_restore = ChargeWriteRefusedError(
        "the inverter read back a different restore than it was written"
    )
    expired = await expire_recorded_charge(
        store, source, app.state.charge_lock, now=record.until + timedelta(minutes=1)
    )
    assert expired is False
    assert load_override(settings) is not None
    store.close()


def test_the_running_service_ends_a_charge_whose_window_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wiring, not the rule: the expiry has to run inside the app the
    service actually serves, on its own timer, with no request asking for it. A
    rule nothing calls would pass every other test in this file."""
    source = ChargeSource()
    monkeypatch.setattr(app_module, "CHARGE_EXPIRY_INTERVAL", 0.05)
    app, store, settings = _assembled(tmp_path, source)
    with TestClient(app) as client:
        assert client.post("/api/charge/start", json={}).status_code == 200
        record = load_override(settings)
        assert record is not None
        # The window is over: the device's own schedule would open again at the
        # same hour tomorrow night, which is the charge nobody asked for.
        save_override(
            settings,
            ChargeOverride(
                saved=record.saved,
                until=datetime.now(tz=UTC) - timedelta(minutes=1),
                requested_w=record.requested_w,
            ),
        )
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and load_override(settings) is not None:
            time.sleep(0.05)
    assert load_override(settings) is None
    assert source.registers == dict(ORIGINAL_REGISTERS)
    store.close()


async def test_the_expiry_leaves_an_unreadable_record_alone(tmp_path: Path) -> None:
    """A record nobody can read is not a record that is absent: something may be
    charging on it, and the damaged text is the only description of how to stop
    that. The expiry reports it and writes nothing."""
    source = ChargeSource()
    app, store, settings = _assembled(tmp_path, source)
    settings.set(CHARGE_OVERRIDE_KEY, "{")
    assert await expire_recorded_charge(store, source, app.state.charge_lock) is False
    assert source.restore_calls == []
    assert settings.get(CHARGE_OVERRIDE_KEY) == "{"
    store.close()


# --- changing the power of a charge that is running ---------------------------------


def test_a_running_charge_can_have_its_power_changed(tmp_path: Path) -> None:
    """The house changes under a charge — an oven, a car — and the power the site
    could spare when it began is not the power it can spare now. The change
    re-decides against the site as it is now, and it leaves the record's window
    and saved configuration exactly where they were, because the charge still has
    to end where the window said it would."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        assert client.post("/api/charge/start", json={"power_w": 3000}).status_code == 200
        before = load_override(settings)
        assert before is not None
        # 5000 W fits the headroom (12000 - 5000 of house - 1000 held back), so
        # what is written is what was asked for; the clamping case is its own
        # test below.
        changed = client.post("/api/charge/power", json={"power_w": 5000})
        assert changed.status_code == 200
        assert src.power_calls == [5000]
        assert changed.json()["power_w"] == 5000
        assert changed.json()["requested_w"] == 5000
        assert changed.json()["until"] == before.until.isoformat()
        # The record's request moved; its window and its saved configuration did
        # not, so the undo is the configuration the inverter held before the
        # charge rather than anything the change touched.
        after = load_override(settings)
        assert after is not None
        assert (after.requested_w, after.until, after.saved.registers) == (
            5000,
            before.until,
            before.saved.registers,
        )


def test_a_power_change_is_held_to_what_the_site_has_left_now(tmp_path: Path) -> None:
    """Raising a charge mid-flight crosses the same limit a start would: the
    request is clamped to the headroom of the moment, and the answer carries the
    number that was actually written."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, _settings):
        assert client.post("/api/charge/start", json={"power_w": 3000}).status_code == 200
        changed = client.post("/api/charge/power", json={"power_w": 12000})
        assert changed.status_code == 200
        # 12000 W site limit, 5000 W of house, 1000 W held back.
        assert changed.json()["power_w"] == 6000
        assert src.power_calls == [6000]


def test_a_power_change_without_a_record_is_refused(tmp_path: Path) -> None:
    """With no charge of ours running, register 66 is the owner's own setting and
    not ours to change. The refusal says which of the two the page is looking at."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, _settings):
        refused = client.post("/api/charge/power", json={"power_w": 5000})
        assert refused.status_code == 409
        assert "no grid charge is recorded" in refused.json()["detail"]
        assert src.power_calls == []


def test_a_power_change_with_no_room_leaves_the_charge_running(tmp_path: Path) -> None:
    """A refusal here is not a stopped charge. The power stays where it is, the
    record stays, and the reason carries the numbers."""
    source = ChargeSource()
    with _rig(tmp_path, source, load_w=11500.0) as (client, src, _store, settings):
        assert client.post("/api/charge/start", json={"power_w": 1000}).status_code == 409
        # No room to start either, so a record is made by hand: what is being
        # tested is the change, not the start.
        saved = decode_charge_config(src.registers, quick_charge_remaining_s=None, read_at=T0)
        save_override(
            settings,
            ChargeOverride(saved=saved, until=T0 + timedelta(minutes=600), requested_w=1000),
        )
        refused = client.post("/api/charge/power", json={"power_w": 5000})
        assert refused.status_code == 409
        assert "11500" in refused.json()["detail"]
        assert src.power_calls == []
        assert load_override(settings) is not None


def test_a_refused_power_write_keeps_the_record_and_the_charge(tmp_path: Path) -> None:
    """The charge is still running and still needs its undo, so a write that did
    not land keeps the record: the failure only says the new number did not
    arrive."""
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        assert client.post("/api/charge/start", json={"power_w": 3000}).status_code == 200
        src.fail_power = ChargeWriteRefusedError(
            "the inverter did not acknowledge the charge write to register 66"
        )
        failed = client.post("/api/charge/power", json={"power_w": 7500})
        assert failed.status_code == 502
        assert "still running at the power it had" in failed.json()["detail"]
        record = load_override(settings)
        assert record is not None
        assert record.requested_w == 3000  # the change did not happen
        assert src.registers[66] == 30  # the charge is still at 3 kW


# --- stop -------------------------------------------------------------------------


def test_stop_restores_what_the_inverter_held_before_the_charge(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        started = client.post("/api/charge/start", json={})
        assert started.status_code == 200
        assert src.registers != dict(ORIGINAL_REGISTERS)
        stop = client.post("/api/charge/stop")
        assert stop.status_code == 200
        assert stop.json()["restored"] is True
        assert src.registers == dict(ORIGINAL_REGISTERS)
        assert settings.get(CHARGE_OVERRIDE_KEY) == ""
        assert len(src.restore_calls) == 1
        assert src.restore_calls[0].registers == dict(ORIGINAL_REGISTERS)


def test_stop_without_a_record_is_refused(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, _settings):
        stop = client.post("/api/charge/stop")
        assert stop.status_code == 409
        assert "no grid charge is recorded" in stop.json()["detail"]
        assert src.restore_calls == []


def test_a_failed_restore_keeps_the_record_for_another_attempt(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        assert client.post("/api/charge/start", json={}).status_code == 200
        src.fail_restore = ChargeWriteRefusedError(
            "the inverter did not acknowledge the charge restore"
        )
        first = client.post("/api/charge/stop")
        assert first.status_code == 502
        assert settings.get(CHARGE_OVERRIDE_KEY) != ""
        src.fail_restore = None
        second = client.post("/api/charge/stop")
        assert second.status_code == 200
        assert settings.get(CHARGE_OVERRIDE_KEY) == ""


def test_an_unreadable_record_is_reported_rather_than_treated_as_no_charge(
    tmp_path: Path,
) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, src, _store, settings):
        settings.set(CHARGE_OVERRIDE_KEY, "{")
        stop = client.post("/api/charge/stop")
        assert stop.status_code == 409
        assert "not valid JSON" in stop.json()["detail"]
        assert src.restore_calls == []
        assert settings.get(CHARGE_OVERRIDE_KEY) == "{"


# --- the read endpoint ------------------------------------------------------------


def test_the_read_endpoint_reports_the_override_state(tmp_path: Path) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, _src, _store, _settings):
        before = client.get("/api/charge").json()["override"]
        assert before["recorded"] is False
        assert before["readable"] is False
        assert before["active"] is False
        assert before["until"] is None
        assert before["requested_w"] is None
        assert client.post("/api/charge/start", json={}).status_code == 200
        during = client.get("/api/charge").json()["override"]
        assert during["recorded"] is True
        assert during["readable"] is True
        assert during["active"] is True
        datetime.fromisoformat(during["until"])
        assert during["requested_w"] == 3000
        assert client.post("/api/charge/stop").status_code == 200
        after = client.get("/api/charge").json()["override"]
        assert after["recorded"] is False
        assert after["active"] is False


def test_the_start_response_carries_the_configuration_the_device_reports(
    tmp_path: Path,
) -> None:
    source = ChargeSource()
    with _rig(tmp_path, source) as (client, _src, _store, _settings):
        response = client.post("/api/charge/start", json={})
        assert response.status_code == 200
        body = response.json()
        applied = body["applied"]
        saved = body["saved"]
        assert all(isinstance(key, str) for key in applied["registers"])
        assert saved["registers"]["21"] == 0x00
        assert applied["registers"]["21"] == 128
        assert saved["registers"]["66"] == 10
        assert applied["registers"]["66"] == 30
        assert datetime.fromisoformat(body["until"])
        assert "reason" in body
