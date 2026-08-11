"""test_api.py — the HTTP surface, over a temporary store and a fake inverter.

No hardware and no real config: the app is assembled around a database in
tmp_path and a collector driving FakeSource, which is the whole reason
create_app takes its dependencies rather than building them. The collector is
real, so the yield endpoints exercise the actual code rather than a stub that
always agrees.
"""

from __future__ import annotations

import itertools
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from arraysense.api.app import PAGES, SHARED_SCRIPT, _file_route, create_app
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.rollup import rebuild_inverter_hourly
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path) -> Any:
    store = SqliteStore(str(tmp_path / "api.db"), device=TEST_DEVICE)
    for minute, pv in ((0, 1000.0), (1, 2000.0), (2, 3000.0)):
        store.append(
            Sample(
                timestamp=T0 + timedelta(minutes=minute),
                readings={"pv_total_power_w": pv, "battery_soc_pct": 50.0 + minute},
                battery_modules=(
                    BatteryModuleSample(serial="AAA", slot=1, soc_pct=90.0 + minute),
                    BatteryModuleSample(serial="BBB", slot=2, soc_pct=20.0 + minute),
                ),
            )
        )
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "api.db"),
        poll_interval=10.0,
    )
    # A real collector over a fake source: the yield endpoints then exercise
    # the actual yield path rather than a stand-in that always agrees.
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    service.status.running = True
    service.status.connected = True
    service.status.last_success = T0
    service.status.total_samples = 7
    service.status.started_at = T0
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        yield c
    store.close()


@pytest.fixture
def empty_client(tmp_path: Path) -> Any:
    """A store with no readings at all — a service that has never polled."""
    store = SqliteStore(str(tmp_path / "empty.db"), device=TEST_DEVICE)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "empty.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        yield c
    store.close()


def test_status_reports_the_collector(client: Any) -> None:
    body = client.get("/api/status").json()
    assert body["running"] is True
    assert body["yielding"] is False
    assert body["total_samples"] == 7
    assert body["version"]


def test_status_names_the_calendar_it_would_answer_on(client: Any) -> None:
    # A page cannot write a request for "this month" until it knows which zone
    # the reply will be cut in, and working that out in the browser would be a
    # second copy of the precedence rule — the copy that drifts. This is the
    # one copy: the setting, then the caller's tz, then the machine's.
    assert client.get("/api/status", params={"tz": "Asia/Tokyo"}).json()["timezone"] == "Asia/Tokyo"
    client.put("/api/settings", json={"site.timezone": "Pacific/Honolulu"})
    assert (
        client.get("/api/status", params={"tz": "Asia/Tokyo"}).json()["timezone"]
        == "Pacific/Honolulu"
    )


def test_status_answers_even_when_the_caller_names_a_zone_it_does_not_know(client: Any) -> None:
    # The data endpoints refuse an unknown zone with a 400 — /api/energy,
    # /api/bands, and /api/costs since #49 — because each of their answers is
    # cut at a midnight and one cut in the wrong place looks entirely normal.
    #
    # This one falls back instead, and the difference is what the answer is for:
    # the banner says whether the screen is current, which is worth answering in
    # some nearby zone and not worth withholding over a browser's stale name. No
    # page retries without ``tz`` — a page that takes a 400 says so and leaves
    # what it already drew — so refusing here would simply lose the banner.
    r = client.get("/api/status", params={"tz": "Mars/Olympus_Mons"})
    assert r.status_code == 200
    assert r.json()["timezone"] == client.get("/api/status").json()["timezone"]


# --- the staleness verdict -------------------------------------------------
#
# The banner used to reach these conclusions in the browser, from a copy of the
# poll loop's stall threshold and a field no success ever clears. Each of these
# asserts on the verdict the endpoint now hands it, because a page that decides
# is a page that can decide differently from the service.


def _polling(client: Any, now: datetime) -> Any:
    """Put the client's collector into the state of one that has just polled well."""
    service = client.app.state.service
    service.status.running = True
    service.status.connected = True
    service.status.last_success = now
    return service


def _staleness(client: Any) -> Any:
    return client.get("/api/status").json()["staleness"]


def test_status_ages_the_reading_and_not_the_process(empty_client: Any) -> None:
    # A restart clears last_success, so a collector crash-looping faster than
    # the threshold never looked stale at all — the one case the warning is
    # for. The rows it already wrote do not move when the process does.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=40), readings={"pv_total_power_w": 1000.0})
    )
    service = empty_client.app.state.service
    service.status.running = True
    service.status.started_at = now
    service.status.last_success = None

    body = _staleness(empty_client)
    assert body["stale"] is True
    assert body["age_seconds"] == pytest.approx(2400, abs=30)
    assert body["reading_at"] == (now - timedelta(minutes=40)).replace(microsecond=0).isoformat()


def test_status_is_not_stale_while_the_readings_are_current(empty_client: Any) -> None:
    # last_failure is never cleared by a later success, so a banner reading it
    # fires after a poll that worked. consecutive_failures is cleared.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now, readings={"pv_total_power_w": 1000.0})
    )
    service = _polling(empty_client, now)
    service.status.last_failure = now - timedelta(hours=2)
    service.status.last_error = "ConnectionRefusedError: [Errno 111] refused"

    body = _staleness(empty_client)
    assert body["stale"] is False
    assert body["verdict"] == "fresh"
    assert body["reason"] is None


def test_status_blames_the_database_when_the_write_is_what_failed(empty_client: Any) -> None:
    # A busy database is recorded in the same field as an unreachable inverter.
    # Reported as the inverter, it sends the reader after the dongle, the WiFi
    # and the breaker while the fault is the disk. The read having succeeded is
    # what tells them apart: connected is set from the read, before the write.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=20), readings={"pv_total_power_w": 1000.0})
    )
    service = _polling(empty_client, now - timedelta(minutes=20))
    service.status.last_failure = now
    service.status.last_error = "OperationalError: database is locked"
    service.status.consecutive_failures = 3

    body = _staleness(empty_client)
    assert body["stale"] is True
    assert body["verdict"] == "storage"
    assert body["reason"] == "OperationalError: database is locked"


def test_status_names_the_driver_when_the_reply_could_not_be_decoded(empty_client: Any) -> None:
    # The third fault, and the one with nowhere honest to go until it was named.
    # The inverter answered and the driver refused the reply: called an outage it
    # sends the reader after the dongle, called a storage fault it sends them to
    # a disk that is fine. Deriving it from `connected` alone could only ever
    # produce one of those two wrong answers.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=20), readings={"pv_total_power_w": 1000.0})
    )
    service = _polling(empty_client, now - timedelta(minutes=20))
    service.status.last_failure = now
    service.status.last_error = "ValueError: serial must not be empty; it is the module identity"
    service.status.last_failure_kind = "build"
    service.status.consecutive_failures = 3

    body = _staleness(empty_client)
    assert body["stale"] is True
    assert body["verdict"] == "driver"
    assert body["reason"] == "ValueError: serial must not be empty; it is the module identity"


def test_status_blames_the_inverter_when_the_read_is_what_failed(empty_client: Any) -> None:
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=20), readings={"pv_total_power_w": 1000.0})
    )
    service = _polling(empty_client, now - timedelta(minutes=20))
    service.status.connected = False
    service.status.last_failure = now
    service.status.last_error = "TimeoutError: read timed out"
    service.status.consecutive_failures = 3

    body = _staleness(empty_client)
    assert body["verdict"] == "inverter"
    assert body["reason"] == "TimeoutError: read timed out"


def test_status_reports_the_stall_the_service_itself_detects(empty_client: Any) -> None:
    # One opinion, not two: whatever stalled_for() says is what goes over the
    # wire, so the page cannot disagree with the watchdog about a dead loop.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=30), readings={"pv_total_power_w": 1000.0})
    )
    service = _polling(empty_client, now - timedelta(minutes=30))

    stalled = service.stalled_for()
    assert stalled is not None, "a loop silent for half an hour is stalled"
    body = _staleness(empty_client)
    assert body["verdict"] == "stopped"
    assert body["stalled_seconds"] == pytest.approx(stalled.total_seconds(), abs=5)


def test_status_does_not_count_a_recorded_gap_as_a_reading(empty_client: Any) -> None:
    # A gap carries a reason and no values, so a page drawing it shows dashes.
    # Counted as data it would report a screen full of nothing as current.
    now = datetime.now(tz=UTC)
    store = empty_client.app.state.store
    store.append(Sample(timestamp=now - timedelta(minutes=40), readings={"pv_total_power_w": 1.0}))
    store.append(Sample.failed(now - timedelta(seconds=10), "TimeoutError: read timed out"))
    service = _polling(empty_client, now - timedelta(minutes=40))
    service.status.connected = False
    service.status.last_failure = now
    service.status.last_error = "TimeoutError: read timed out"
    service.status.consecutive_failures = 200

    body = _staleness(empty_client)
    assert body["stale"] is True
    assert body["age_seconds"] == pytest.approx(2400, abs=30)
    assert body["verdict"] == "inverter"


def test_status_says_no_reading_rather_than_guessing_at_an_age(empty_client: Any) -> None:
    # The search behind a gap is bounded — a status poll every thirty seconds
    # may not scan a month of raw rows — so an outage longer than the window
    # has no age to report. None, never a number and never zero.
    now = datetime.now(tz=UTC)
    store = empty_client.app.state.store
    store.append(Sample(timestamp=now - timedelta(hours=9), readings={"pv_total_power_w": 1.0}))
    store.append(Sample.failed(now - timedelta(seconds=10), "TimeoutError: read timed out"))
    service = _polling(empty_client, now - timedelta(hours=9))
    service.status.connected = False
    service.status.last_failure = now
    service.status.last_error = "TimeoutError: read timed out"
    service.status.consecutive_failures = 200

    body = _staleness(empty_client)
    assert body["reading_at"] is None
    assert body["age_seconds"] is None
    assert body["any_rows"] is True
    assert body["stale"] is True
    assert body["searched_seconds"] > 0


def test_status_does_not_call_a_fresh_install_stale(empty_client: Any) -> None:
    # Nothing recorded and nothing wrong: a service that started a moment ago
    # has an empty store, and a banner over it would be the first thing a new
    # owner ever saw.
    service = _polling(empty_client, datetime.now(tz=UTC))
    service.status.started_at = datetime.now(tz=UTC)

    body = _staleness(empty_client)
    assert body["any_rows"] is False
    assert body["reading_at"] is None
    assert body["stale"] is False
    assert body["verdict"] == "fresh"


def test_status_reports_an_empty_store_behind_a_stalled_loop(empty_client: Any) -> None:
    # An install that never worked: the loop is running and has marked nothing,
    # which stalled_for() calls stalled whether or not a row was ever written.
    now = datetime.now(tz=UTC)
    service = empty_client.app.state.service
    service.status.running = True
    service.status.started_at = now - timedelta(minutes=30)

    body = _staleness(empty_client)
    assert body["stale"] is True
    assert body["verdict"] == "stopped"
    assert body["any_rows"] is False
    assert body["reading_at"] is None


def test_status_treats_a_deliberate_yield_as_its_own_case(empty_client: Any) -> None:
    # The dongle takes one client at a time, so handing it over is a deliberate
    # act with an end time on it and not a fault to be warned about.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=40), readings={"pv_total_power_w": 1.0})
    )
    service = _polling(empty_client, now - timedelta(minutes=40))
    service.status.yielding = True
    service.status.yield_until = now + timedelta(minutes=5)

    body = _staleness(empty_client)
    assert body["stale"] is True
    assert body["verdict"] == "yielding"
    assert body["stalled_seconds"] is None


def test_live_returns_the_latest_inverter_and_every_module(client: Any) -> None:
    body = client.get("/api/live").json()
    assert body["inverter"]["pv_total_power_w"] == 3000.0
    assert {m["serial"] for m in body["modules"]} == {"AAA", "BBB"}
    assert [m["soc_pct"] for m in body["modules"]] == [92.0, 22.0]


def test_live_keeps_absent_values_null(client: Any) -> None:
    # A battery block empty because CAN is down must not arrive as 0.
    body = client.get("/api/live").json()
    assert body["inverter"]["grid_power_w"] is None


def test_live_names_the_operating_mode(client: Any) -> None:
    # Judged in arraysense.mode and shipped with the reading it was judged
    # from, so the page prints a verdict rather than reaching its own. The
    # Costs page already showed what happens when a browser recomputes what
    # Python has decided.
    body = client.get("/api/live").json()
    assert set(body["mode"]) == {"mode", "battery", "why", "known"}
    assert body["mode"]["why"], "a mode with no stated reason cannot be checked"


def test_live_names_no_mode_when_nothing_was_measured(empty_client: Any) -> None:
    # An empty store has no reading to interpret. Saying "on grid" from that
    # is the same error as drawing a missing value as zero.
    body = empty_client.get("/api/live").json()
    assert body["mode"]["known"] is False


def test_history_returns_points_in_range(client: Any) -> None:
    body = client.get(
        "/api/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(minutes=1)).isoformat(),
            "metrics": "pv_total_power_w",
            "width": 1000,
        },
    ).json()
    assert body["count"] == 2
    assert [p["pv_total_power_w"] for p in body["points"]] == [1000.0, 2000.0]


def test_history_picks_a_coarser_tier_for_a_longer_span(client: Any) -> None:
    short = client.get(
        "/api/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(hours=1)).isoformat(),
            "metrics": "pv_total_power_w",
            "width": 1000,
        },
    ).json()
    long = client.get(
        "/api/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(days=30)).isoformat(),
            "metrics": "pv_total_power_w",
            "width": 1000,
        },
    ).json()
    assert short["tier"] == "full"
    assert long["tier"] == "hourly"


def test_history_rejects_an_unknown_metric(client: Any) -> None:
    r = client.get(
        "/api/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(hours=1)).isoformat(),
            "metrics": "not_a_metric",
            "width": 1000,
        },
    )
    assert r.status_code == 400
    assert "not_a_metric" in r.json()["detail"]


def test_history_rejects_an_inverted_range(client: Any) -> None:
    r = client.get(
        "/api/history",
        params={
            "start": (T0 + timedelta(hours=1)).isoformat(),
            "end": T0.isoformat(),
            "metrics": "pv_total_power_w",
            "width": 1000,
        },
    )
    assert r.status_code == 400


def test_history_rejects_a_silly_width(client: Any) -> None:
    for width in (0, 100000):
        r = client.get(
            "/api/history",
            params={
                "start": T0.isoformat(),
                "end": (T0 + timedelta(hours=1)).isoformat(),
                "metrics": "pv_total_power_w",
                "width": width,
            },
        )
        assert r.status_code == 422, width


def test_battery_history_is_keyed_by_serial(client: Any) -> None:
    body = client.get(
        "/api/battery/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(minutes=2)).isoformat(),
            "metrics": "soc_pct",
            "width": 1000,
        },
    ).json()
    serials = {p["serial"] for p in body["points"]}
    assert serials == {"AAA", "BBB"}


def test_battery_history_can_filter_to_one_module(client: Any) -> None:
    body = client.get(
        "/api/battery/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(minutes=2)).isoformat(),
            "metrics": "soc_pct",
            "width": 1000,
            "serial": "BBB",
        },
    ).json()
    assert {p["serial"] for p in body["points"]} == {"BBB"}


def test_battery_history_rejects_an_inverter_metric(client: Any) -> None:
    # pv_total_power_w is not a per-module reading.
    r = client.get(
        "/api/battery/history",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(minutes=2)).isoformat(),
            "metrics": "pv_total_power_w",
            "width": 1000,
        },
    )
    assert r.status_code == 400


def test_yield_hands_the_dongle_over(client: Any) -> None:
    r = client.post("/api/yield", json={"seconds": 120})
    assert r.status_code == 200
    assert r.json()["yielding"] is True
    assert client.get("/api/status").json()["yielding"] is True


def test_yield_rejects_a_nonsense_duration(client: Any) -> None:
    for seconds in (0, -1, 99999):
        assert client.post("/api/yield", json={"seconds": seconds}).status_code == 422


def test_resume_takes_the_dongle_back(client: Any) -> None:
    client.post("/api/yield", json={"seconds": 600})
    assert client.post("/api/resume").json()["yielding"] is False
    assert client.get("/api/status").json()["yielding"] is False


# --- state-of-charge calibration --------------------------------------------


def _bank(
    store: SqliteStore,
    when: datetime,
    volts: float,
    socs: dict[str, float],
    amps: float | None = None,
) -> None:
    """Record one poll of the bank at a given voltage with the given pack states.

    ``amps`` is the bank current, and omitting it leaves the column NULL — which
    is a real state the inverter produces and one the taper check deliberately
    does not judge. A test about the taper has to say a number.
    """
    readings: dict[str, float] = {"battery_voltage_v": volts, "bms_charge_voltage_ref_v": 56.0}
    if amps is not None:
        readings["battery_current_a"] = amps
    store.append(
        Sample(
            timestamp=when,
            readings=readings,
            battery_modules=tuple(
                BatteryModuleSample(serial=s, slot=i + 1, soc_pct=soc, voltage_v=volts)
                for i, (s, soc) in enumerate(socs.items())
            ),
        )
    )


def _calibration_client(tmp_path: Path, build: Any) -> Any:
    store = SqliteStore(str(tmp_path / "cal.db"), device=TEST_DEVICE)
    build(store)
    from arraysense.store.rollup import rebuild_inverter_minute

    lo = int((datetime.now(tz=UTC) - timedelta(days=61)).timestamp())
    hi = int((datetime.now(tz=UTC) + timedelta(days=1)).timestamp())
    rebuild_inverter_minute(store._conn, lo, hi)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "cal.db"),
        poll_interval=11.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    return TestClient(app)


def test_calibration_finds_a_recent_full_charge(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)

    def build(store: SqliteStore) -> None:
        # Three days ago the bank sat at its charge reference for half an hour
        # and every pack reached full.
        charged = now - timedelta(days=3)
        for minute in range(0, 31, 2):
            _bank(store, charged + timedelta(minutes=minute), 55.9, {"A": 100.0, "B": 100.0})
        _bank(store, now, 53.0, {"A": 61.0, "B": 62.0})

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["severity"] == "none"
    assert body["soc_is_estimate"] is False
    assert 2.9 < body["days_since"] < 3.2


def test_calibration_reports_drift_when_the_charge_never_completed(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)

    def build(store: SqliteStore) -> None:
        # The bank reached absorb voltage but one pack never got to full, so
        # its counter never reset and the clock must not restart.
        charged = now - timedelta(days=3)
        for minute in range(0, 31, 2):
            _bank(store, charged + timedelta(minutes=minute), 55.9, {"A": 100.0, "B": 88.0})
        _bank(store, now, 53.0, {"A": 61.0, "B": 62.0})

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["severity"] == "elevated"
    assert body["days_since"] is None
    assert body["last_full_charge"] is None
    assert body["soc_is_estimate"] is True


def test_calibration_separates_a_wiring_fault_from_a_drifting_counter(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=now,
                readings={"battery_voltage_v": 53.0},
                battery_modules=(
                    BatteryModuleSample(serial="A", slot=1, soc_pct=60.0, voltage_v=53.80),
                    BatteryModuleSample(serial="B", slot=2, soc_pct=60.0, voltage_v=53.50),
                ),
            )
        )

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["severity"] == "alert"
    assert body["wiring_suspect"] is True
    assert body["voltage_spread_mv"] == 300.0
    # A wiring fault must not offer charging as the remedy — that is the whole
    # point of separating the two. It may still mention a missed full charge,
    # because this bank has one as well and both are true at once.
    assert "Charging will not fix it" in body["detail"]
    assert "Charge to 100%" not in body["detail"]
    # And the drift verdict survives alongside the alert rather than being
    # replaced by it.
    assert body["drift_severity"] == "elevated"


def test_calibration_never_turns_a_silent_pack_into_zero_percent(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=now,
                readings={"battery_voltage_v": 53.0},
                battery_modules=(
                    BatteryModuleSample(serial="A", slot=1, soc_pct=61.0, voltage_v=53.78),
                    BatteryModuleSample(serial="B", slot=2),
                ),
            )
        )

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["soc_spread_pct"] is None
    assert body["voltage_spread_mv"] is None
    assert body["wiring_suspect"] is False


def test_calibration_answers_on_an_empty_database(tmp_path: Path) -> None:
    # A fresh install has no history at all. It must say so rather than crash.
    with _calibration_client(tmp_path, lambda store: None) as c:
        response = c.get("/api/calibration")
    assert response.status_code == 200
    body = response.json()
    assert body["last_full_charge"] is None
    assert body["soc_spread_pct"] is None


# The reference installation's charge of 8 August 2026, as the minute tier
# recorded it: 55.6, 55.7, 55.7 and 55.5 V at 111.2, 63.9, 12.2 and -1.6 A —
# three minutes of absorb against a twenty-minute rule. Three packs snapped to
# 100% two minutes in, the fourth five minutes later, three minutes after the
# bank's own voltage had fallen back below the reference. The climb before the
# absorb is shortened; everything else is as recorded.
#
# _DRIFTED is what the four read through the quarter hour before the absorb:
# 70-75, 77-80, 72-76 and 96-99. The fourth pack's dip below 99 is what proves
# its counter reset too, and its last such reading was 2 min 19 s before the
# absorb opened — so the lookback has to be minutes rather than seconds.
_ABSORB = ((55.6, 111.2), (55.7, 63.9), (55.7, 12.2), (55.5, -1.6))
_DRIFTED = {"A": 75.0, "B": 80.0, "C": 76.0, "D": 96.0}
_THREE_OF_FOUR = {"A": 100.0, "B": 100.0, "C": 77.0, "D": 100.0}
_RESET = {"A": 100.0, "B": 100.0, "C": 100.0, "D": 100.0}


def _short_charge(
    store: SqliteStore,
    charged: datetime,
    *,
    before: dict[str, float],
    during: dict[str, float],
    after: dict[str, float],
) -> None:
    """Replay that charge through the store, with the pack states each stage reports."""
    for minute in range(15):
        _bank(store, charged + timedelta(minutes=minute), 54.0, before, amps=175.0)
    for minute, (volts, amps) in enumerate(_ABSORB):
        socs = during if minute >= 2 else before
        _bank(store, charged + timedelta(minutes=15 + minute), volts, socs, amps=amps)
    for minute in range(19, 31):
        socs = after if minute >= 22 else during
        _bank(store, charged + timedelta(minutes=minute), 54.9, socs, amps=0.0)


def test_calibration_credits_a_charge_that_only_absorbed_for_three_minutes(
    tmp_path: Path,
) -> None:
    # The defect. This hardware crosses absorb, finishes and tapers to zero in
    # about three minutes, so the twenty-minute hold never happens and sixty
    # days of nightly full charges came back as "no full charge found" — telling
    # the owner to do the thing they had just finished doing.
    now = datetime.now(tz=UTC)
    charged = now - timedelta(hours=6)

    def build(store: SqliteStore) -> None:
        _short_charge(store, charged, before=_DRIFTED, during=_THREE_OF_FOUR, after=_RESET)
        _bank(store, now, 53.0, {"A": 92.0, "B": 93.0, "C": 92.0, "D": 94.0}, amps=-80.0)

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["severity"] == "none"
    assert body["drift_severity"] == "none"
    assert body["soc_is_estimate"] is False
    assert body["headline"] == "State of charge is calibrated"
    # The instant the last counter reset, three minutes after the bank left
    # absorb — not the end of the voltage window, which was earlier.
    reset_at = (charged + timedelta(minutes=22)).replace(microsecond=0)
    assert body["last_full_charge"] == reset_at.isoformat()
    assert 0.2 < body["days_since"] < 0.3


def test_calibration_still_ignores_one_pack_drifting_to_full(tmp_path: Path) -> None:
    # The hole that must stay shut. One counter at 100% while the rest sit at 70
    # is drift, which is the condition being detected — and the inverter side of
    # this database is the real charge, byte for byte, so only the packs differ.
    now = datetime.now(tz=UTC)
    lagging = {"A": 70.0, "B": 70.0, "C": 70.0, "D": 70.0}
    drifted_high = {**lagging, "D": 100.0}

    def build(store: SqliteStore) -> None:
        _short_charge(
            store,
            now - timedelta(hours=6),
            before=lagging,
            during=drifted_high,
            after=drifted_high,
        )
        _bank(store, now, 53.0, drifted_high, amps=-80.0)

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["last_full_charge"] is None
    assert body["days_since"] is None
    assert body["severity"] == "elevated"
    assert body["soc_is_estimate"] is True


def test_calibration_ignores_a_bank_whose_counters_are_all_pegged(tmp_path: Path) -> None:
    # The other shape of the same hole. Uncounted standby draw makes every
    # counter read high, so a bank left alone long enough has all four pegged at
    # 100% while it sits at half charge. A standing unanimity is not a reset, and
    # crediting it would silence the warning on the bank that most needs it.
    now = datetime.now(tz=UTC)

    def build(store: SqliteStore) -> None:
        _short_charge(store, now - timedelta(hours=6), before=_RESET, during=_RESET, after=_RESET)
        _bank(store, now, 53.0, _RESET, amps=-80.0)

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["last_full_charge"] is None
    assert body["severity"] == "elevated"


def test_calibration_ignores_three_pegged_counters_beside_one_that_charged(
    tmp_path: Path,
) -> None:
    # Found by review and reproduced against the reference database through this
    # endpoint. Three counters drifted high and pegged at 100%, the fourth
    # genuinely charges across 99: every pack reads full at the end and one of
    # them was previously below, which was enough for an earlier version of the
    # rule. Three quarters of these percentages are stale, and the endpoint was
    # reporting the bank calibrated with soc_is_estimate false — so the
    # dashboard would have drawn all four as measurements.
    now = datetime.now(tz=UTC)
    pegged = {"A": 100.0, "B": 100.0, "C": 100.0}

    def build(store: SqliteStore) -> None:
        _short_charge(
            store,
            now - timedelta(hours=6),
            before={**pegged, "D": 96.0},
            during={**pegged, "D": 100.0},
            after={**pegged, "D": 100.0},
        )
        _bank(store, now, 53.0, {**pegged, "D": 100.0}, amps=-80.0)

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    assert body["last_full_charge"] is None
    assert body["severity"] == "elevated"
    assert body["soc_is_estimate"] is True


def test_calibration_does_not_let_a_second_absorb_borrow_the_first_transition(
    tmp_path: Path,
) -> None:
    # Two absorb touches ten minutes apart, packs at 100 throughout the second.
    # The second's lookback reaches back past the first and finds the below-full
    # rows belonging to that charge, so the later window was being credited on
    # evidence that was never about it. The answer is the first charge's reset.
    now = datetime.now(tz=UTC)
    charged = now - timedelta(hours=6)

    def build(store: SqliteStore) -> None:
        _short_charge(store, charged, before=_DRIFTED, during=_RESET, after=_RESET)
        for minute, (volts, amps) in enumerate(_ABSORB):
            _bank(store, charged + timedelta(minutes=28 + minute), volts, _RESET, amps=amps)
        for minute in range(32, 40):
            _bank(store, charged + timedelta(minutes=minute), 54.9, _RESET, amps=0.0)
        _bank(store, now, 53.0, {"A": 92.0, "B": 93.0, "C": 92.0, "D": 94.0}, amps=-80.0)

    with _calibration_client(tmp_path, build) as c:
        body = c.get("/api/calibration").json()
    reset_at = (charged + timedelta(minutes=17)).replace(microsecond=0)
    assert body["last_full_charge"] == reset_at.isoformat()
    assert body["severity"] == "none"


# --- settings ----------------------------------------------------------------


def test_settings_ship_their_own_descriptions(client: Any) -> None:
    # The page renders controls from this rather than hard-coding labels and
    # bounds, which would drift from the validation the moment either changed.
    body = client.get("/api/settings").json()
    keys = {f["key"] for f in body["fields"]}
    assert "display.temperature_unit" in keys
    unit = next(f for f in body["fields"] if f["key"] == "display.temperature_unit")
    assert unit["choices"] == ["F", "C"]
    assert body["values"]["display.temperature_unit"] == "F"


def test_a_display_setting_round_trips(client: Any) -> None:
    r = client.put("/api/settings", json={"display.temperature_unit": "C"})
    assert r.status_code == 200
    assert r.json()["changed"] == ["display.temperature_unit"]
    assert r.json()["restart_required"] is False
    assert client.get("/api/settings").json()["values"]["display.temperature_unit"] == "C"


def test_a_bad_value_is_a_400_naming_the_setting(client: Any) -> None:
    r = client.put("/api/settings", json={"display.temperature_unit": "K"})
    assert r.status_code == 400
    assert "temperature_unit" in r.json()["detail"]
    # Nothing was written.
    assert client.get("/api/settings").json()["values"]["display.temperature_unit"] == "F"


def test_an_unknown_setting_is_a_400(client: Any) -> None:
    assert client.put("/api/settings", json={"made.up": 1}).status_code == 400


def test_a_serial_comes_back_masked(client: Any) -> None:
    client.put("/api/settings", json={"connection.dongle_serial": "BA12345678"})
    shown = client.get("/api/settings").json()["values"]["connection.dongle_serial"]
    assert shown == "BA••••••78"
    assert "33400" not in shown


def test_posting_a_mask_back_does_not_overwrite_the_real_serial(client: Any) -> None:
    # The page renders the mask. A form saved without touching that field would
    # otherwise write the bullets over the serial and break the next poll.
    client.put("/api/settings", json={"connection.dongle_serial": "BA12345678"})
    masked = client.get("/api/settings").json()["values"]["connection.dongle_serial"]
    r = client.put("/api/settings", json={"connection.dongle_serial": masked})
    assert r.json()["changed"] == []
    assert client.get("/api/settings").json()["values"]["connection.dongle_serial"] == masked


def test_changing_a_connection_setting_asks_for_a_restart(client: Any) -> None:
    r = client.put("/api/settings", json={"collector.poll_interval": 20.0})
    assert r.json()["restart_required"] is True


def test_posting_a_masked_email_back_does_not_overwrite_the_real_one(client: Any) -> None:
    # Same trap as the serials: the page renders the mask, and a form saved
    # without touching the field would write the bullets over the address.
    client.put("/api/settings", json={"site.contact_email": "owner@example.com"})
    masked = client.get("/api/settings").json()["values"]["site.contact_email"]
    assert "owner@example.com" not in masked
    r = client.put("/api/settings", json={"site.contact_email": masked})
    assert r.status_code == 200
    assert r.json()["changed"] == []
    assert client.get("/api/settings").json()["values"]["site.contact_email"] == masked


def test_the_described_fields_carry_a_unit_and_suggestions(client: Any) -> None:
    # What agent B's page renders from. Both keys are present on every field,
    # so the page never has to branch on whether the server mentioned them.
    fields = client.get("/api/settings").json()["fields"]
    assert all("unit" in f and "suggestions" in f for f in fields)
    poll = next(f for f in fields if f["key"] == "collector.poll_interval")
    assert poll["unit"] == "seconds"
    currency = next(f for f in fields if f["key"] == "tariff.currency")
    assert "$" in currency["suggestions"]
    assert currency["choices"] == []


def test_an_unset_coordinate_arrives_as_null_and_the_equator_as_zero(client: Any) -> None:
    # 0.0 is a real place. The wire has to keep "not set" and "on the equator"
    # apart, or the page cannot tell them apart either.
    body = client.get("/api/settings").json()
    assert body["values"]["site.latitude"] is None
    latitude = next(f for f in body["fields"] if f["key"] == "site.latitude")
    assert latitude["optional"] is True
    assert latitude["default"] is None

    assert client.put("/api/settings", json={"site.latitude": 0.0}).status_code == 200
    assert client.get("/api/settings").json()["values"]["site.latitude"] == 0.0
    # And an emptied box clears it rather than moving the site to the equator.
    assert client.put("/api/settings", json={"site.latitude": ""}).status_code == 200
    assert client.get("/api/settings").json()["values"]["site.latitude"] is None


def test_an_unparseable_timezone_is_a_400_at_the_settings_endpoint(client: Any) -> None:
    r = client.put("/api/settings", json={"site.timezone": "Mars/Olympus_Mons"})
    assert r.status_code == 400
    assert "site.timezone" in r.json()["detail"]
    assert client.get("/api/settings").json()["values"]["site.timezone"] == ""


# --- the pages ----------------------------------------------------------------


def test_the_dashboard_is_served(client: Any) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Solar ArraySense" in r.text


def test_the_shared_front_end_is_served(client: Any) -> None:
    # One copy of the palette, the formatters and the chart factory. A page that
    # carried its own would drift, and the drift arrives as two pages disagreeing
    # about what the same reading means.
    r = client.get("/common.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert "numOrNull" in r.text


def test_every_page_in_the_allow_list_has_a_route(client: Any) -> None:
    routed = {getattr(route, "path", None) for route in client.app.routes}
    assert set(PAGES) <= routed
    assert f"/{SHARED_SCRIPT}" in routed


@pytest.mark.parametrize("path", ["/graphs", "/history", "/costs"])
def test_a_page_route_answers_rather_than_raising(client: Any, path: str) -> None:
    # These pages are written separately, so this asserts what holds either way:
    # before the file exists the route is a 404, afterwards it is HTML, and at no
    # point is it a traceback out of the response.
    r = client.get(path)
    assert r.status_code in (200, 404), r.status_code
    if r.status_code == 200:
        assert r.headers["content-type"].startswith("text/html")


async def test_a_page_whose_file_is_missing_is_a_404(tmp_path: Path) -> None:
    # Reached through the route builder rather than the client because the three
    # new pages will exist soon and this contract has to keep being tested after
    # they do. Starlette raises from inside the response for an absent file,
    # which the browser sees as a 500; a page nobody has written yet is missing,
    # not broken.
    serve = _file_route(tmp_path / "not_written_yet.html", "text/html")
    with pytest.raises(HTTPException) as raised:
        await serve()
    assert raised.value.status_code == 404


def test_a_file_in_web_is_not_served_just_because_it_is_there(client: Any) -> None:
    # The allow-list is the whole point: the dashboard lives at "/" and nothing
    # answers to the name of a file on disk.
    for attempt in ("/index.html", "/graphs.html", "/costs.html", "/uPlot.LICENSE", "/nope"):
        assert client.get(attempt).status_code == 404, attempt


@pytest.mark.parametrize(
    "attempt",
    [
        "../config/config.toml",
        "../../config/config.toml",
        "..%2Fconfig%2Fconfig.toml",
        "%2e%2e%2fconfig%2fconfig.toml",
    ],
)
def test_a_traversal_against_the_pages_cannot_reach_the_configuration(
    client: Any, attempt: str
) -> None:
    # None of the page routes takes a path parameter at all, which is what makes
    # this unroutable rather than a filesystem read that has to be sanitised.
    for prefix in ("/", "/graphs/", "/common.js/"):
        r = client.get(prefix + attempt)
        assert r.status_code == 404, (prefix, attempt)
        assert "dongle_serial" not in r.text


# --- vendored front-end files ------------------------------------------------


def test_the_vendored_chart_library_is_served(client: Any) -> None:
    r = client.get("/vendor/uPlot.iife.min.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert b"uPlot" in r.content


def test_the_vendored_licence_ships_with_it(client: Any) -> None:
    # uPlot is MIT inside an AGPL project. Shipping the code without the
    # licence is the one thing that turns vendoring into a problem.
    r = client.get("/vendor/uPlot.LICENSE")
    assert r.status_code == 200
    assert "MIT" in r.text


def test_an_unknown_vendored_name_is_a_404(client: Any) -> None:
    assert client.get("/vendor/anything.js").status_code == 404


def test_a_path_traversal_cannot_reach_the_configuration(client: Any) -> None:
    # The route takes a name, not a path. An allow-list rather than a directory
    # mount is what makes this a 404 instead of somebody's serial numbers.
    for attempt in (
        "../config/config.toml",
        "../../config/config.toml",
        "..%2Fconfig%2Fconfig.toml",
        "%2e%2e%2fconfig%2fconfig.toml",
    ):
        r = client.get(f"/vendor/{attempt}")
        assert r.status_code == 404, attempt
        assert "dongle_serial" not in r.text


def test_the_calibration_endpoint_asks_for_the_current_it_needs(client: Any) -> None:
    # full_charge_windows rejects a window still pushing charge current. The
    # endpoint was not requesting the column, so that safeguard was inert in
    # production while passing every direct test of the function.
    import inspect

    from arraysense.api import routes

    src = inspect.getsource(routes.calibration)
    assert "battery_current_a" in src


def test_a_non_finite_setting_is_a_400_not_a_500(client: Any) -> None:
    # Posted as a raw JSON NaN, which json.loads accepts by default.
    r = client.put(
        "/api/settings",
        content='{"collector.poll_interval": NaN}',
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text


# --- costs -------------------------------------------------------------------


def test_costs_shows_no_money_at_all_without_a_tariff(client: Any) -> None:
    # Not zero, and not a guessed rate. An install that has never entered a
    # tariff shows its energy and says so.
    body = client.get(
        "/api/costs",
        params={"start": "2026-07-15T00:00:00Z", "end": "2026-07-16T00:00:00Z"},
    ).json()
    assert body["configured"] is False
    assert body["cost"] is None
    assert body["bill"] is None
    assert body["currency"] is None


def test_costs_names_its_calendar_even_with_no_tariff(client: Any) -> None:
    # A page has to be able to say which calendar it is showing whether or not
    # there is money on it, and a field that appears only sometimes is one every
    # caller has to branch on. The docs promise it on both endpoints.
    body = client.get(
        "/api/costs",
        params={
            "start": "2026-07-15T00:00:00",
            "end": "2026-07-16T00:00:00",
            "tz": "Asia/Tokyo",
        },
    ).json()
    assert body["configured"] is False
    assert body["timezone"] == "Asia/Tokyo"


def test_costs_prices_the_seasonal_tariff_the_page_could_not_read(client: Any) -> None:
    # The blocking defect: the browser's own parser required exactly three
    # pipe-separated fields and rejected the season, so the reference
    # installation's own tariff priced nothing at all. Pricing server-side
    # means one grammar and one meaning.
    client.put(
        "/api/settings",
        json={
            "tariff.bands": (
                "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
                "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
                "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
            ),
            "tariff.fixed_monthly": 15.0,
        },
    )
    body = client.get(
        "/api/costs",
        params={
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-07-16T00:00:00Z",
            "tz": "America/Chicago",
        },
    ).json()
    assert body["configured"] is True
    assert body["currency"] == "$"


def test_costs_refuses_a_period_too_long_to_price(client: Any) -> None:
    r = client.get(
        "/api/costs",
        params={"start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
    )
    # Without a tariff there is nothing to scan, so configure one first.
    client.put("/api/settings", json={"tariff.bands": "Flat | 0.12 | 00:00-24:00"})
    r = client.get(
        "/api/costs",
        params={"start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
    )
    assert r.status_code == 400
    assert "days" in r.json()["detail"]


def test_costs_carries_everything_the_page_would_otherwise_derive_twice(client: Any) -> None:
    # The page draws; it does not compute. Every field here exists because the
    # alternative was the browser working it out again from a second endpoint,
    # and a second derivation of the same thing is how the tariff grammar came
    # to have two implementations that disagreed.
    client.put(
        "/api/settings",
        json={
            "tariff.bands": (
                "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
                "Off-peak | 0.086709 | 00:00-24:00 | May-Oct"
            ),
            "tariff.fixed_monthly": 15.0,
        },
    )
    body = client.get(
        "/api/costs",
        params={
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-07-16T00:00:00Z",
            "tz": "America/Chicago",
        },
    ).json()
    peak = next(r for r in body["rows"] if r["name"] == "On-peak")
    assert peak["hours"] == "15:00\N{EN DASH}20:00"
    # The season, so a peak window nobody is currently in is not shown as a
    # rate they are being charged today.
    assert peak["months"] == [5, 6, 7, 8, 9, 10]
    # Every figure the table draws, already in money. The page multiplying a
    # rate by a kilowatt-hour is the same mistake as the page parsing a tariff.
    for key in (
        "import_kwh",
        "cost",
        "house_kwh",
        "house_cost",
        "battery_kwh",
        "battery_value",
        "saved",
        "price_per_kwh",
    ):
        assert key in peak, key
    for key in ("tier", "unpriced_minutes", "measured_minutes"):
        assert key in body, key
    assert body["elapsed_minutes"] == pytest.approx(1440.0)
    # The shortfall block, per counter, is what the labels are drawn from —
    # deriving "is this figure whole" from the minutes above is exactly the
    # mistake the second attempt at #23 shipped.
    for counter in ("grid_import", "load", "grid_export"):
        entry = body["shortfall"][counter]
        for key in ("attributed_kwh", "unattributed_kwh", "unknowable"):
            assert key in entry, key
    assert body["cost"]["cost_is_short"] is True
    assert body["bill"]["is_short"] is True


def test_costs_says_whether_a_stored_tariff_is_merely_absent_or_unreadable(client: Any) -> None:
    # "Nothing entered" and "something entered that cannot be read" call for
    # opposite actions, and conflating them tells somebody staring at the
    # tariff they just typed that they have not entered one.
    body = client.get(
        "/api/costs",
        params={"start": "2026-07-15T00:00:00Z", "end": "2026-07-16T00:00:00Z"},
    ).json()
    assert body["configured"] is False
    assert body["unreadable"] is False


def test_costs_refuses_a_zone_the_tz_database_does_not_know(client: Any) -> None:
    # A 400 rather than the 500 a bare KeyError out of _request_zone became. The
    # caller sent a bad zone; telling them the service is broken sends them to
    # look at the wrong thing. /api/energy and /api/bands already answer this way
    # and this endpoint is the one whose answer is money, so it belongs with them
    # rather than with /api/status, which falls back on purpose (#49).
    r = client.get(
        "/api/costs",
        params={
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-07-16T00:00:00Z",
            "tz": "Mars/Olympus_Mons",
        },
    )
    assert r.status_code == 400
    assert "Mars/Olympus_Mons" in r.json()["detail"]


def test_costs_ignores_a_bad_browser_zone_when_the_installation_has_its_own(
    client: Any,
) -> None:
    # The refusal above must not reach an install that has stated its own zone.
    # resolve_zone does not consult the caller's name at all in that case — not
    # even to reject it — because the answer is already fully determined, and a
    # phone carrying a stale zone must not be able to refuse a request the
    # service can answer perfectly well.
    client.put("/api/settings", json={"site.timezone": "America/Chicago"})
    params = {"start": "2026-07-15T00:00:00Z", "end": "2026-07-16T00:00:00Z"}
    r = client.get("/api/costs", params={**params, "tz": "Mars/Olympus_Mons"})
    assert r.status_code == 200
    assert r.json()["timezone"] == "America/Chicago"
    # And identical to the answer with no zone named at all.
    assert r.json()["timezone"] == client.get("/api/costs", params=params).json()["timezone"]


def test_a_malformed_tariff_is_refused_when_it_is_saved(client: Any) -> None:
    # Not on the way out. Storing it and letting the Costs page discover the
    # absence puts the error a page away from the box that caused it.
    r = client.put("/api/settings", json={"tariff.bands": "Peak | xx:00-20:00 | 0.21"})
    assert r.status_code == 400
    assert "tariff.bands" in r.json()["detail"]


def test_a_tariff_written_one_band_per_line_can_be_saved(client: Any) -> None:
    # The help text says "one band per line" and parse_bands has always
    # accepted newlines; it was the settings validator that refused them, so
    # a tariff typed the way it reads could not be stored at all.
    r = client.put(
        "/api/settings",
        json={"tariff.bands": "On-peak | 0.21 | 15:00-20:00\nOff-peak | 0.09 | 00:00-24:00"},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["changed"] == ["tariff.bands"]
    # The tariff is read afresh on every costs request, so nothing restarts.
    assert r.json()["restart_required"] is False


def test_changing_how_the_collector_connects_does_need_a_restart(client: Any) -> None:
    r = client.put("/api/settings", json={"connection.dongle_host": "192.168.1.77"})
    assert r.status_code == 200
    assert r.json()["restart_required"] is True


def test_costs_reports_hours_no_band_covers(client: Any) -> None:
    # A tariff with a hole in it prices less than the month used. Saying how
    # much of the day falls outside every band is the difference between a
    # small bill and a wrong one.
    client.put("/api/settings", json={"tariff.bands": "Daytime | 0.15 | 08:00-20:00"})
    body = client.get(
        "/api/costs",
        params={
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-07-16T00:00:00Z",
            "tz": "America/Chicago",
        },
    ).json()
    assert body["unpriced_minutes"] == 12 * 60


def test_the_settings_page_is_served(client: Any) -> None:
    # The tariff, the connection and the poll interval are all database
    # settings with a PUT endpoint, and until this route existed there was no
    # page anywhere that wrote to it — so the Costs page's own empty state
    # pointed the owner at somewhere they could not enter a tariff.
    r = client.get("/settings")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "/api/settings" in r.text


# --- energy with money on it ---------------------------------------------------

# The reference installation's own tariff: time-of-use May to October, one flat
# rate the rest of the year, and a connection charge every month regardless.
COSERV_BANDS = (
    "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
    "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
    "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
)
CHICAGO = ZoneInfo("America/Chicago")
JULY = datetime(2026, 7, 1, tzinfo=CHICAGO)
AUGUST = datetime(2026, 8, 1, tzinfo=CHICAGO)


def _counters(store: SqliteStore, first: datetime, last: datetime, skip: Any = None) -> None:
    """Hourly lifetime counters climbing between two instants.

    Import climbs faster during the afternoon so the peak band carries real
    money rather than the same rate everywhere, which would make a
    misattributed hour invisible in the total.
    """
    when = first
    imported, load = 1000.0, 4000.0
    while when < last:
        peak = 15 <= when.astimezone(CHICAGO).hour < 20
        imported += 2.5 if peak else 0.8
        load += 4.0 if peak else 1.5
        if skip is None or not skip(when):
            store.append(
                Sample(
                    timestamp=when,
                    readings={
                        "grid_import_energy_total_kwh": round(imported, 1),
                        "load_energy_total_kwh": round(load, 1),
                        "grid_export_energy_total_kwh": 5.0,
                    },
                )
            )
        when += timedelta(hours=1)


def _energy_client(tmp_path: Path, build: Any, bands: str | None = COSERV_BANDS) -> Any:
    store = SqliteStore(str(tmp_path / "energy.db"), device=TEST_DEVICE)
    build(store)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "energy.db"),
        poll_interval=11.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    client = TestClient(create_app(store=store, service=service, config=config))
    if bands is not None:
        client.put("/api/settings", json={"tariff.bands": bands, "tariff.fixed_monthly": 15.0})
    return client


def test_a_date_in_words_buckets_on_the_installations_calendar(tmp_path: Path) -> None:
    # What the History page depends on. It builds one row per day on the site's
    # calendar and keys the reply's buckets by their own date, so the two have
    # to be cut at the same midnights. Asking with the reader's midnight begins
    # the run a day early where the inverter is — that first bucket then has no
    # row to land in and is dropped from the table, the chart and the row count,
    # while its energy stays inside the footer's total.
    with _energy_client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        c.put("/api/settings", json={"site.timezone": "Pacific/Honolulu"})
        asked = {"period": "day", "tz": "America/Chicago"}
        in_words = c.get(
            "/api/energy",
            params={"start": "2026-07-10T00:00:00", "end": "2026-07-13T00:00:00", **asked},
        ).json()
        readers = c.get(
            "/api/energy",
            params={"start": "2026-07-10T05:00:00Z", "end": "2026-07-13T05:00:00Z", **asked},
        ).json()
    assert in_words["timezone"] == readers["timezone"] == "Pacific/Honolulu"
    assert [b["start"][:10] for b in in_words["buckets"]] == [
        "2026-07-10",
        "2026-07-11",
        "2026-07-12",
    ]
    assert readers["buckets"][0]["start"][:10] == "2026-07-09"


def _july(client: Any, **extra: Any) -> Any:
    params = {
        "start": JULY.isoformat(),
        "end": AUGUST.isoformat(),
        "tz": "America/Chicago",
        **extra,
    }
    return client.get("/api/energy", params=params).json()


def test_a_priced_month_matches_what_the_costs_page_reports_for_it(tmp_path: Path) -> None:
    # The one number that has to agree. The Costs page and the History page
    # answer the same question about the same July, and if they answer it
    # differently the owner has two bills and no way to tell which is theirs.
    with _energy_client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        history = _july(c, period="month", priced=True)
        costs = c.get(
            "/api/costs",
            params={
                "start": JULY.isoformat(),
                "end": AUGUST.isoformat(),
                "tz": "America/Chicago",
            },
        ).json()
    assert len(history["buckets"]) == 1
    assert history["currency"] == costs["currency"] == "$"
    assert history["buckets"][0]["cost"] == costs["cost"]["cost"]
    assert history["buckets"][0]["energy_cost"] == costs["cost"]["energy_cost"]
    assert history["buckets"][0]["fixed_charge"] == costs["cost"]["fixed_charge"]
    assert history["buckets"][0]["cost"] > 0


def test_the_days_of_a_month_add_up_to_the_month(tmp_path: Path) -> None:
    # Thirty-one daily rows and one monthly row are two views of one bill. They
    # are rounded separately, so they may differ by pennies and must not differ
    # by more: a systematic gap would mean a band's energy is landing in a
    # different place depending on how the question is asked.
    with _energy_client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        daily = _july(c, period="day", priced=True)
        monthly = _july(c, period="month", priced=True)
    total = sum(b["cost"] for b in daily["buckets"])
    assert len(daily["buckets"]) == 31
    assert total == pytest.approx(monthly["buckets"][0]["cost"], abs=0.25)
    # The connection charge is in there once, not thirty-one times. It comes
    # back a few cents under fifteen because a thirty-first of it is 0.4838 and
    # every row is rounded to the cent it is displayed at; the alternative is a
    # column whose figures do not add up to themselves.
    assert sum(b["fixed_charge"] for b in daily["buckets"]) == pytest.approx(15.0, abs=0.2)


def test_energy_carries_no_money_unless_money_is_asked_for(tmp_path: Path) -> None:
    # The Costs page reads this endpoint too, for its energy grid. Pricing
    # every bucket for a caller that wanted kilowatt-hours would make it do the
    # band scan for nothing on every load.
    with _energy_client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        body = _july(c, period="month")
    assert "currency" not in body
    assert "cost" not in body["buckets"][0]


def test_an_install_with_no_tariff_gets_energy_and_no_money_at_all(tmp_path: Path) -> None:
    # Not zero, and not a column of dashes either. There is nothing to say
    # about money here, so the buckets carry no money key at all and the page
    # has nothing to draw a column from.
    build = lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)  # noqa: E731
    with _energy_client(tmp_path, build, bands=None) as c:
        body = _july(c, period="month", priced=True)
    assert body["configured"] is False
    assert body["unreadable"] is False
    assert body["currency"] is None
    assert body["buckets"][0]["load_kwh"] is not None
    for key in ("cost", "energy_cost", "fixed_charge"):
        assert key not in body["buckets"][0], key


def test_a_day_whose_peak_hours_were_never_recorded_is_priced_partial_and_flagged(
    tmp_path: Path,
) -> None:
    # The collector was down across the peak window on the 15th. The old rule
    # made that day a dash; #23's decision is the measured part with its
    # qualification riding beside it, so the page can label instead of
    # withhold. The days either side stay unflagged — a flag that fires
    # everywhere says nothing.
    def build(store: SqliteStore) -> None:
        _counters(
            store,
            JULY - timedelta(hours=2),
            AUGUST,
            skip=lambda w: (
                w.astimezone(CHICAGO).day == 15 and 14 <= w.astimezone(CHICAGO).hour < 21
            ),
        )

    with _energy_client(tmp_path, build) as c:
        body = _july(c, period="day", priced=True)
    by_day = {b["start"][:10]: b for b in body["buckets"]}
    assert by_day["2026-07-15"]["cost"] is not None
    assert by_day["2026-07-15"]["cost_short"] is True
    assert by_day["2026-07-15"]["saved_short"] is True
    assert by_day["2026-07-15"]["shortfall"]["grid_import"]["unattributed_kwh"] > 0
    assert by_day["2026-07-14"]["cost"] is not None
    assert by_day["2026-07-14"]["cost_short"] is False
    # The energy columns are untouched by any of this.
    assert by_day["2026-07-15"]["grid_imported_kwh"] is not None


def test_the_month_in_progress_is_priced_over_the_part_that_has_happened(tmp_path: Path) -> None:
    # A calendar month runs to the first of the next one, so most of the month
    # the owner is living through is hours nobody has readings for. Pricing the
    # whole bucket leaves the peak band unmeasured and the row shows a dash
    # beside a month that plainly used electricity. The bucket is priced to the
    # moment asked about instead, which is what the Costs page does with the
    # same bound — so the two agree on the month to date as well as on a whole
    # one.
    part = datetime(2026, 7, 18, 9, 0, tzinfo=CHICAGO)
    build = lambda s: _counters(s, JULY - timedelta(hours=2), part)  # noqa: E731
    with _energy_client(tmp_path, build) as c:
        history = c.get(
            "/api/energy",
            params={
                "start": JULY.isoformat(),
                "end": part.isoformat(),
                "period": "month",
                "tz": "America/Chicago",
                "priced": True,
            },
        ).json()
        costs = c.get(
            "/api/costs",
            params={
                "start": JULY.isoformat(),
                "end": part.isoformat(),
                "tz": "America/Chicago",
            },
        ).json()
    bucket = history["buckets"][0]
    assert bucket["complete"] is False
    assert bucket["cost"] == costs["cost"]["cost"]
    assert bucket["cost"] > 0
    # The standing charge is the whole month's, not the part that has run. It
    # falls due once for the month however early in it you ask, so showing a
    # slice described an instalment nobody is billed and understated the month.
    # Both paths charge it the same way, which is the assertion above: a page
    # showing one figure while the other page shows another is the failure this
    # pair of assertions exists to catch.
    assert bucket["fixed_charge"] == pytest.approx(15.0, abs=0.01)


def test_a_whole_month_asked_about_beyond_its_end_is_unchanged(tmp_path: Path) -> None:
    # Capping the last bucket must not move a bucket that has already closed.
    # A range that runs past a completed month still prices that month over the
    # whole of it.
    with _energy_client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        exact = _july(c, period="month", priced=True)["buckets"][0]
        beyond = c.get(
            "/api/energy",
            params={
                "start": JULY.isoformat(),
                "end": (AUGUST + timedelta(days=3)).isoformat(),
                "period": "month",
                "tz": "America/Chicago",
                "priced": True,
            },
        ).json()["buckets"][0]
    assert beyond["cost"] == exact["cost"]
    assert beyond["fixed_charge"] == 15.0


def test_the_day_in_progress_is_priced_and_both_endpoints_say_the_same(tmp_path: Path) -> None:
    # Nine in the morning on a summer day. The peak window is still six hours
    # off, so it has no reading and never had one to miss — and treating that
    # as an unmeasured band made the top row of the History table, the row the
    # owner actually looks at, a dash for most of every day. Both endpoints go
    # through one pricing path, so whatever they say they say together.
    morning = datetime(2026, 7, 15, 9, 0, tzinfo=CHICAGO)
    build = lambda s: _counters(s, JULY - timedelta(hours=2), morning)  # noqa: E731
    with _energy_client(tmp_path, build) as c:
        day = c.get(
            "/api/energy",
            params={
                "start": datetime(2026, 7, 15, tzinfo=CHICAGO).isoformat(),
                "end": morning.isoformat(),
                "period": "day",
                "tz": "America/Chicago",
                "priced": True,
            },
        ).json()["buckets"][-1]
        costs = c.get(
            "/api/costs",
            params={
                "start": datetime(2026, 7, 15, tzinfo=CHICAGO).isoformat(),
                "end": morning.isoformat(),
                "tz": "America/Chicago",
            },
        ).json()
    assert day["cost"] is not None
    assert day["cost"] == costs["cost"]["cost"]


def test_a_day_complete_in_energy_can_still_be_short_in_money(tmp_path: Path) -> None:
    # Reverted finding 4, at the endpoint. The gap sits wholly inside the
    # 20th, so the day's *energy* is exact — the counters span the hole — and
    # ``complete`` is rightly true. Its *cost* is short by every peak hour
    # nobody can place. The two flags answer different questions, and the
    # second attempt at #23 died of reading the first as if it answered both.
    def build(store: SqliteStore) -> None:
        _counters(
            store,
            JULY - timedelta(hours=2),
            AUGUST,
            skip=lambda w: (
                w.astimezone(CHICAGO).day == 20 and 14 <= w.astimezone(CHICAGO).hour < 21
            ),
        )

    with _energy_client(tmp_path, build) as c:
        body = _july(c, period="day", priced=True)
    by_day = {b["start"][:10]: b for b in body["buckets"]}
    assert by_day["2026-07-20"]["complete"] is True
    assert by_day["2026-07-20"]["cost"] is not None
    assert by_day["2026-07-20"]["cost_short"] is True


def test_costs_reads_a_month_older_than_the_minute_tier_keeps(client: Any) -> None:
    # The minute tier is retained for a year and the raw tier for thirty days,
    # so a month older than either exists only in the hourly tier. Falling from
    # minute straight to raw found nothing and reported the month as unpriceable
    # — while the History page, which does consult hourly, showed a figure for
    # the very same month.
    client.put("/api/settings", json={"tariff.bands": "Flat | 0.12 | 00:00-24:00"})
    store = client.app.state.store
    old = datetime(2024, 6, 1, tzinfo=UTC)
    for hour in range(0, 49):
        store.append(
            Sample(
                timestamp=old + timedelta(hours=hour),
                readings={
                    "grid_import_energy_total_kwh": 100.0 + hour,
                    "load_energy_total_kwh": 200.0 + hour * 2,
                },
                battery_modules=(),
            )
        )
    conn = sqlite3.connect(client.app.state.config.database_path)
    rebuild_inverter_hourly(
        conn, int(old.timestamp()) - 3600, int((old + timedelta(days=3)).timestamp())
    )
    conn.commit()
    # Nothing may remain in the tiers the endpoint used to depend on.
    conn.execute("DELETE FROM inverter_raw")
    conn.execute("DELETE FROM inverter_minute")
    conn.commit()
    conn.close()

    body = client.get(
        "/api/costs",
        params={
            "start": "2024-06-01T00:00:00Z",
            "end": "2024-06-02T00:00:00Z",
            "tz": "UTC",
        },
    ).json()
    assert body["tier"] == "hourly"
    assert body["cost"] is not None
    assert body["cost"]["energy_cost"] is not None


def test_a_priced_bucket_carries_what_the_system_saved(client: Any) -> None:
    # The History page shows saving beside cost. It is the counterfactual — the
    # same house load bought entirely from the grid, less what the grid actually
    # cost — and it must come from the same place the Costs page gets it, or the
    # two pages disagree about the same day.
    client.put("/api/settings", json={"tariff.bands": "Flat | 0.12 | 00:00-24:00"})
    body = client.get(
        "/api/energy",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(days=1)).isoformat(),
            "period": "day",
            "priced": 1,
            "tz": "UTC",
        },
    ).json()
    assert body["configured"] is True
    for bucket in body["buckets"]:
        assert "saved" in bucket
        assert "no_solar_cost" in bucket


def test_without_a_tariff_a_bucket_carries_no_saving_key_at_all(client: Any) -> None:
    # Absent, not null. A column of dashes over an install that simply has no
    # rates entered invites the reader to wonder what went wrong, when nothing
    # did — so the page draws no column rather than an empty one.
    body = client.get(
        "/api/energy",
        params={
            "start": T0.isoformat(),
            "end": (T0 + timedelta(days=1)).isoformat(),
            "period": "day",
            "priced": 1,
            "tz": "UTC",
        },
    ).json()
    assert body["configured"] is False
    for bucket in body["buckets"]:
        assert "saved" not in bucket
        assert "cost" not in bucket


def test_pages_and_the_shared_script_are_revalidated(client: Any) -> None:
    # The pages and common.js change together and are cached separately, so a
    # browser left to its own heuristics will happily pair a fresh page with
    # yesterday's script. When that happened the page called a helper that did
    # not exist yet, the chart threw, and the failure surfaced as "history
    # unavailable" — pointing at the network for a fault in the cache.
    for path in ("/", "/graphs", "/history", "/costs", "/settings", "/common.js"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "no-cache" in r.headers.get("cache-control", ""), path


def test_the_vendored_library_is_still_cacheable(client: Any) -> None:
    # It is versioned by its filename and never changes under the same name, so
    # revalidating it every load would buy nothing and cost a request.
    r = client.get("/vendor/uPlot.min.css")
    assert r.status_code == 200
    assert "no-cache" not in r.headers.get("cache-control", "")


def test_a_single_day_is_charged_a_days_share_by_both_endpoints(client: Any) -> None:
    # The whole connection charge belongs to a billing month, not to any period
    # somebody happens to ask about. Charging it per request made /api/costs
    # answer 15.55 for a day the History page priced at 0.74 — the same day,
    # two figures, which is the thing this project most has to avoid.
    client.put(
        "/api/settings",
        json={"tariff.bands": "Flat | 0.12 | 00:00-24:00", "tariff.fixed_monthly": 15.0},
    )
    mid = datetime(2026, 7, 15, 5, 0, tzinfo=UTC)
    body = client.get(
        "/api/costs",
        params={
            "start": mid.isoformat(),
            "end": (mid + timedelta(days=1)).isoformat(),
            "tz": "America/Chicago",
        },
    ).json()
    charged = (body.get("cost") or {}).get("fixed_charge")
    assert charged is not None
    # A day of a 31-day month, not the whole month.
    assert charged == pytest.approx(15.0 / 31, abs=0.02)


def test_the_month_charge_is_judged_in_the_owners_zone(client: Any) -> None:
    # The page asks for 05:00 UTC because that is local midnight in Chicago.
    # Comparing that against UTC midnight says "not a month start" and quietly
    # apportions — which is exactly the with_zone trap that has now cost this
    # project three separate bugs.
    client.put(
        "/api/settings",
        json={"tariff.bands": "Flat | 0.12 | 00:00-24:00", "tariff.fixed_monthly": 15.0},
    )
    body = client.get(
        "/api/costs",
        params={
            "start": "2026-07-01T05:00:00Z",
            "end": "2026-07-20T05:00:00Z",
            "tz": "America/Chicago",
        },
    ).json()
    assert (body.get("cost") or {}).get("fixed_charge") == pytest.approx(15.0, abs=0.01)


def test_the_month_charge_survives_a_reader_in_another_zone(client: Any) -> None:
    # The defect the pages carried once the setting could outrank the browser.
    # A page that computes midnight on the first where the *reader* is sends an
    # instant that, five hours west, is the previous month's evening — so the
    # connection charge that falls due whole was apportioned to a fifth of
    # itself, and the month-to-date cost was short by the hours either side.
    #
    # The fix is to send the date in words and let the service read it in the
    # zone it resolved, which is the whole point of a naive bound.
    client.put(
        "/api/settings",
        json={
            "tariff.bands": "Flat | 0.12 | 00:00-24:00",
            "tariff.fixed_monthly": 15.0,
            "site.timezone": "Pacific/Honolulu",
        },
    )
    rest = {"end": "2026-07-20T00:00:00", "tz": "America/Chicago"}
    readers = client.get("/api/costs", params={"start": "2026-07-01T05:00:00Z", **rest}).json()
    assert (readers.get("cost") or {}).get("fixed_charge") != pytest.approx(15.0, abs=0.01)

    in_words = client.get("/api/costs", params={"start": "2026-07-01T00:00:00", **rest}).json()
    assert in_words["timezone"] == "Pacific/Honolulu"
    assert (in_words.get("cost") or {}).get("fixed_charge") == pytest.approx(15.0, abs=0.01)


def test_status_reports_recovered_crossed_replies(client: Any) -> None:
    # The dongle serves its vendor's cloud on the same socket and the replies
    # cross. One retried and recovered is the dongle being itself; a rate that
    # climbs is a fault. Neither is visible today: a successful retry logs at
    # DEBUG and the service runs at INFO, so the healthy case and the failing
    # case look identical from outside — which is the whole reason the adapter
    # counts them.
    body = client.get("/api/status").json()
    assert "misroutes" in body


def test_status_says_nothing_about_misroutes_a_source_cannot_count(
    client: Any,
) -> None:
    # FakeSource has no counter, and a source that cannot count crossed replies
    # must report null rather than zero. Zero is a measurement meaning "none
    # happened", and claiming it from a source that never looked is the same
    # error as rendering a missing reading as 0.
    body = client.get("/api/status").json()
    assert body["misroutes"] is None


SECOND_DEVICE = "CE00000001"


def _two_device_client(tmp_path: Path) -> Any:
    """A store holding two inverters, served by a page that knows about one."""
    store = SqliteStore(str(tmp_path / "stack.db"), device=TEST_DEVICE)
    store.append(
        Sample(
            timestamp=T0,
            readings={"pv_total_power_w": 1000.0},
            battery_modules=(BatteryModuleSample(serial="AAA", slot=1, soc_pct=90.0),),
        )
    )
    store.append(
        Sample(
            timestamp=T0,
            readings={"pv_total_power_w": 9000.0},
            battery_modules=(BatteryModuleSample(serial="ZZZ", slot=1, soc_pct=10.0),),
        ),
        device=SECOND_DEVICE,
    )
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "stack.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    return TestClient(create_app(store=store, service=service, config=config))


def test_a_second_inverter_is_invisible_to_a_page_that_asks_for_nothing(
    tmp_path: Path,
) -> None:
    # The point of the whole change landing quietly: an existing page sends no
    # device and sees exactly the inverter it always saw.
    with _two_device_client(tmp_path) as c:
        live = c.get("/api/live").json()
        history = c.get(
            "/api/history",
            params={
                "start": (T0 - timedelta(minutes=1)).isoformat(),
                "end": (T0 + timedelta(minutes=1)).isoformat(),
                "metrics": "pv_total_power_w",
            },
        ).json()
    assert live["inverter"]["pv_total_power_w"] == 1000.0
    assert [m["serial"] for m in live["modules"]] == ["AAA"]
    assert [p["pv_total_power_w"] for p in history["points"]] == [1000.0]


def test_naming_the_second_inverter_returns_its_readings(tmp_path: Path) -> None:
    with _two_device_client(tmp_path) as c:
        live = c.get("/api/live", params={"device": SECOND_DEVICE}).json()
        battery = c.get(
            "/api/battery/history",
            params={
                "start": (T0 - timedelta(minutes=1)).isoformat(),
                "end": (T0 + timedelta(minutes=1)).isoformat(),
                "device": SECOND_DEVICE,
            },
        ).json()
    assert live["inverter"]["pv_total_power_w"] == 9000.0
    assert [m["serial"] for m in live["modules"]] == ["ZZZ"]
    assert [p["serial"] for p in battery["points"]] == ["ZZZ"]


def test_an_unknown_device_returns_nothing_rather_than_somebody_elses_rows(
    tmp_path: Path,
) -> None:
    with _two_device_client(tmp_path) as c:
        live = c.get("/api/live", params={"device": "CE99999999"}).json()
    assert live["inverter"] is None
    assert live["modules"] == []


def test_a_blank_device_is_a_client_error_not_a_server_one(client: Any) -> None:
    # A browser sends ?device= readily — a cleared input, a hand-built URL.
    # It used to reach the store as a device nothing had ever recorded, which
    # answered with no rows and no error and read as an inverter that had
    # stopped reporting. The store refuses it now, so the same request would
    # otherwise be a 500. It is the caller's mistake either way.
    for query in ("?device=", "?device=%20"):
        assert client.get("/api/live" + query).status_code == 400


def test_naming_the_configured_device_answers_as_usual(client: Any) -> None:
    # The parameter has to be usable, not merely validated. A page that does
    # send it must get exactly what a page that does not send it gets.
    plain = client.get("/api/live").json()
    named = client.get("/api/live", params={"device": client.app.state.store.device}).json()
    assert named["inverter"] == plain["inverter"]


# --- capabilities: what the device produces ----------------------------------
#
# A page that enumerates metrics by hand shows a one-string machine two
# permanently empty charts. This endpoint is where a page learns what the
# device it is looking at actually produces, straight from the driver's own
# declaration — no inverter round trip, no store query.


def test_capabilities_names_what_the_device_produces(client: Any) -> None:
    body = client.get("/api/capabilities").json()
    assert len(body["devices"]) == 1
    device = body["devices"][0]
    assert device["driver"] == "fake"
    assert device["device"] == "CE00000000"
    assert device["model"] == "simulated"
    assert device["pv_strings"] == 3
    assert device["energy"] == "estimated"
    assert device["per_module_battery"] is True
    # Inverter-level names come from the driver's declaration, in registry
    # order — the fake reports a total but nothing per string.
    assert "pv_total_power_w" in device["metrics"]
    assert "pv1_power_w" not in device["metrics"]
    assert device["metrics"][0] == "pv_total_power_w"
    # Module names are the bare per-module ones the battery endpoints accept.
    assert "soc_pct" in device["battery_module_metrics"]
    assert "status_code" not in device["battery_module_metrics"]


def test_capabilities_reports_the_transport(client: Any) -> None:
    # The dashboard labels its connection controls from this field — with it
    # missing the page hard-coded "Dongle", which was wrong on the RS485
    # installation the reference system now runs (#72). The value is the
    # source's own declaration: the fake declares no override, so this is the
    # Capabilities default. A built EG4 source substitutes its configured
    # transport, which test_eg4_luxpower_source covers.
    device = client.get("/api/capabilities").json()["devices"][0]
    assert device["transport"] == "dongle"


def test_capabilities_reports_identity_without_a_declaration(tmp_path: Path) -> None:
    # The collector only requires an InverterSource, which names its device but
    # carries no identity and no declaration. Absent capability is not absent
    # data, and it is not an absent device either: the serial is known, so the
    # device is reported — with null where the declaration would be, meaning
    # "not established". An empty metrics list would claim the opposite: a
    # device known to produce nothing.
    class _Mute:
        device = TEST_DEVICE

        async def connect(self) -> None:
            return None

        async def disconnect(self) -> None:
            return None

        async def read(self) -> Sample:
            return Sample(timestamp=datetime.now(tz=UTC), readings={})

    store = SqliteStore(str(tmp_path / "mute.db"), device=TEST_DEVICE)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "mute.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=_Mute(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        body = c.get("/api/capabilities").json()
    store.close()
    assert len(body["devices"]) == 1
    device = body["devices"][0]
    assert device["device"] == TEST_DEVICE
    assert device["driver"] is None
    assert device["model"] is None
    assert device["pv_strings"] is None
    assert device["energy"] is None
    assert device["per_module_battery"] is None
    assert device["transport"] is None
    assert device["metrics"] is None
    assert device["battery_module_metrics"] is None


# --- which tariff band a moment fell in (#46) -----------------------------------
#
# The Power flow chart shades its background by band so grid import can be read
# against what it cost. The windows are resolved here rather than in the page for
# the same reason the pricing is: the browser once had its own tariff parser and
# the two disagreed within a day, charging a January evening at the summer peak
# rate. A page that draws bands it worked out itself is that bug with a chart
# instead of a number.

_SEASONAL = (
    "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
    "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
    "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
)


def _bands(client: Any, **params: Any) -> Any:
    return client.get("/api/bands", params=params).json()


def test_bands_returns_one_window_for_a_range_inside_a_single_band(client: Any) -> None:
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    body = _bands(
        client,
        start="2026-07-15T21:00:00Z",  # 16:00 Chicago, inside the 15:00-20:00 peak
        end="2026-07-15T22:00:00Z",
        tz="America/Chicago",
    )
    assert [w["band"] for w in body["windows"]] == ["On-peak"]
    assert body["windows"][0]["price_per_kwh"] == pytest.approx(0.210321)


def test_bands_splits_exactly_at_a_boundary_with_no_gap_and_no_overlap(client: Any) -> None:
    # A gap would leave a stripe of chart unshaded that really was in a band; an
    # overlap would paint one instant twice. The boundary is the whole point.
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    body = _bands(
        client,
        start="2026-07-15T23:00:00Z",  # 18:00 Chicago, peak
        end="2026-07-16T02:00:00Z",  # 21:00 Chicago, off-peak
        tz="America/Chicago",
    )
    windows = body["windows"]
    assert [w["band"] for w in windows] == ["On-peak", "Off-peak"]
    assert windows[0]["end"] == windows[1]["start"]


def test_bands_changes_season_partway_through_a_range(client: Any) -> None:
    # A thirty-day view can cross a seasonal boundary, so returning one daily
    # pattern for the whole range prices half of it wrong.
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    body = _bands(
        client,
        start="2026-10-30T12:00:00Z",
        end="2026-11-02T12:00:00Z",
        tz="America/Chicago",
    )
    names = {w["band"] for w in body["windows"]}
    assert "Winter" in names, "the November side of the range is still priced as summer"
    assert names & {"On-peak", "Off-peak"}, "the October side lost its summer bands"


def test_bands_are_wall_clock_in_the_owners_zone_not_utc(client: Any) -> None:
    # The trap that mispriced every hour of every day: an aware bound must be
    # converted to the owner's zone, not merely have one attached. Read against
    # the UTC clock a 15:00-20:00 peak lands at 10:00-15:00 local.
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    body = _bands(
        client,
        start="2026-07-15T17:00:00Z",  # 12:00 Chicago — off-peak
        end="2026-07-15T18:00:00Z",  # 13:00 Chicago — still off-peak
        tz="America/Chicago",
    )
    assert [w["band"] for w in body["windows"]] == ["Off-peak"], (
        "midday local was priced as peak, so the bands matched the UTC clock"
    )


def test_bands_survive_a_daylight_saving_change(client: Any) -> None:
    # A day is not always 24 hours. The window crossing the change is the one
    # that goes wrong, and it goes wrong silently.
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    body = _bands(
        client,
        start="2026-11-01T04:00:00Z",  # spans 2026-11-01, when US DST ends
        end="2026-11-01T12:00:00Z",
        tz="America/Chicago",
    )
    windows = body["windows"]
    assert windows, "the DST day produced no windows at all"
    for earlier, later in itertools.pairwise(windows):
        assert earlier["end"] == later["start"], "a DST-day window left a gap"


def test_bands_refuses_a_zone_the_tz_database_does_not_know(client: Any) -> None:
    # Refused rather than quietly fallen back on, the way /api/energy refuses it.
    # A band window is a claim about which hours were expensive; one cut on a zone
    # the caller did not ask for is wrong in a way nothing on the page reveals.
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    r = client.get(
        "/api/bands",
        params={
            "start": "2026-07-15T00:00:00Z",
            "end": "2026-07-16T00:00:00Z",
            "tz": "Mars/Olympus_Mons",
        },
    )
    assert r.status_code == 400
    assert "Mars/Olympus_Mons" in r.json()["detail"]


def test_bands_refuses_a_range_longer_than_it_can_walk(client: Any) -> None:
    # A caller asking for five years gets told so, not a 500 implying the service
    # is broken. band_intervals raises ValueError; the endpoint converts it.
    client.put("/api/settings", json={"tariff.bands": _SEASONAL})
    r = client.get(
        "/api/bands",
        params={"start": "2021-01-01T00:00:00Z", "end": "2026-01-01T00:00:00Z"},
    )
    assert r.status_code == 400
    assert "may not exceed" in r.json()["detail"]


def test_bands_are_absent_rather_than_everything_when_no_tariff_is_set(client: Any) -> None:
    # Absent data is not zero. With no tariff the chart must draw no shading at
    # all, rather than one window implying the whole day was cheap.
    body = _bands(
        client,
        start="2026-07-15T00:00:00Z",
        end="2026-07-16T00:00:00Z",
        tz="America/Chicago",
    )
    assert body["configured"] is False
    assert body["windows"] == []


# --- how fast the bank is filling or emptying (#44) -----------------------------
#
# A power figure says how hard the bank is working; it does not say what that
# means for the bank. +7 kW into 57 kWh is a different afternoon from +7 kW into
# 14 kWh, and the owner reads the card to know how long they have. The rate is
# derived here rather than in the page, because the capacity it divides by is a
# reading, and a browser doing its own arithmetic on readings is how the Costs
# page came to disagree with the service about money.


def _with_battery(client: Any, **readings: float | None) -> Any:
    """Store one inverter reading carrying the given battery fields."""
    store = client.app.state.store
    store.append(
        Sample(
            timestamp=T0 + timedelta(minutes=30),
            readings={k: v for k, v in readings.items() if v is not None},
        )
    )
    return client.get("/api/live").json()


def test_the_rate_follows_the_power_and_the_banks_own_capacity(client: Any) -> None:
    # 1120 Ah at 51.1 V is 57.2 kWh, which is the reference bank. Charging at
    # 7.41 kW fills 12.95% of it in an hour.
    body = _with_battery(
        client,
        battery_power_w=7410.0,
        battery_full_capacity_ah=1120.0,
        battery_voltage_v=51.1,
    )
    assert body["battery"]["capacity_kwh"] == pytest.approx(57.2, abs=0.1)
    assert body["battery"]["rate_pct_per_hour"] == pytest.approx(12.95, abs=0.05)


def test_a_discharging_bank_reports_a_negative_rate(client: Any) -> None:
    body = _with_battery(
        client,
        battery_power_w=-5720.0,
        battery_full_capacity_ah=1120.0,
        battery_voltage_v=51.1,
    )
    assert body["battery"]["rate_pct_per_hour"] == pytest.approx(-10.0, abs=0.05)


def test_an_idle_bank_reports_zero_and_not_nothing(client: Any) -> None:
    # Zero is a real reading here and a different state from unknown: the bank
    # is neither filling nor emptying, which is worth saying.
    body = _with_battery(
        client,
        battery_power_w=0.0,
        battery_full_capacity_ah=1120.0,
        battery_voltage_v=51.1,
    )
    assert body["battery"]["rate_pct_per_hour"] == 0.0


def test_the_rate_is_absent_when_the_capacity_is_unknown(client: Any) -> None:
    # Absent, never zero. A bank whose capacity nobody reported is not a bank
    # filling at 0% an hour, and a card showing that would be inventing a fact.
    body = _with_battery(client, battery_power_w=7410.0, battery_voltage_v=51.1)
    assert body["battery"]["capacity_kwh"] is None
    assert body["battery"]["rate_pct_per_hour"] is None


def test_the_rate_is_absent_when_the_power_is_unknown(client: Any) -> None:
    body = _with_battery(client, battery_full_capacity_ah=1120.0, battery_voltage_v=51.1)
    assert body["battery"]["rate_pct_per_hour"] is None


def test_a_bank_that_holds_nothing_reports_no_rate_rather_than_crashing(client: Any) -> None:
    # Zero is inside battery_full_capacity_ah's own plausible range, so it
    # arrives as an ordinary reading. Dividing by it took down /api/live, which
    # is the endpoint the whole dashboard polls — every card on the page goes
    # blank over one implausible-but-legal number. A bank holding nothing also
    # has no rate to report, so the answer is absent rather than any figure.
    store = client.app.state.store
    store.append(
        Sample(
            timestamp=T0 + timedelta(minutes=45),
            readings={
                "battery_power_w": 7410.0,
                "battery_full_capacity_ah": 0.0,
                "battery_voltage_v": 51.1,
            },
        )
    )
    r = client.get("/api/live")
    assert r.status_code == 200
    assert r.json()["battery"]["rate_pct_per_hour"] is None


def test_the_rate_is_derived_from_the_unrounded_capacity(client: Any) -> None:
    # capacity_kwh is rounded for display. Dividing by the rounded figure would
    # compute the rate from what the card shows rather than from what the bank
    # reported, which is a small error today and a habit that gets larger.
    body = _with_battery(
        client,
        battery_power_w=7410.0,
        battery_full_capacity_ah=1120.0,
        battery_voltage_v=51.1,
    )
    assert body["battery"]["capacity_kwh"] == pytest.approx(57.2, abs=0.05)
    # 7410 / 57.232 / 10 = 12.95; from the rounded 57.2 it would be 12.96.
    assert body["battery"]["rate_pct_per_hour"] == pytest.approx(12.95, abs=0.005)


def test_the_battery_block_is_present_even_with_nothing_to_report(empty_client: Any) -> None:
    # A field that appears only sometimes is one every caller has to branch on —
    # the same reason the settings and staleness payloads carry every key.
    body = empty_client.get("/api/live").json()
    assert "battery" in body
    assert body["battery"]["rate_pct_per_hour"] is None


# --- tier-scanning reads run off the event loop, on their own connections (#63)


def test_tier_scanning_endpoints_are_not_coroutines() -> None:
    # The whole fix. As async def, these ran synchronous SQLite on the event
    # loop: /api/calibration measured 1.6 to 3.2 seconds on the production Pi,
    # and every concurrent response — status, static pages, everything — waited
    # for it, on the dashboard's own sixty-second refresh timer. A plain def is
    # what makes FastAPI run the handler in its threadpool, so this pins the
    # property the stall fix depends on. If one of these needs to become async
    # again, its queries must move off the loop some other way first.
    import asyncio

    from arraysense.api import routes

    for handler in (
        routes.live,
        routes.calibration,
        routes.costs,
        routes.history,
        routes.battery_history,
        routes.energy,
        routes.bands,
        routes.forecast,
    ):
        assert not asyncio.iscoroutinefunction(handler), (
            f"{handler.__name__} is async again: its synchronous queries would "
            "run on the event loop and block every concurrent response"
        )


def test_a_read_view_gets_its_own_connection_and_closing_it_spares_the_store(
    tmp_path: Path,
) -> None:
    # A reader on its own connection sees zero interference from writers under
    # WAL — measured on both reference filesystems — and that only holds if the
    # view really is its own connection, and its cleanup really does leave the
    # store's primary alone.
    store = SqliteStore(str(tmp_path / "view.db"), device=TEST_DEVICE)
    now = datetime.now(tz=UTC)
    store.append(Sample(timestamp=now, readings={"battery_voltage_v": 55.9}))
    with store.read_view() as view:
        assert view._conn is not store._conn
        rows = view.query(["battery_voltage_v"], now, now)
        assert rows[0]["battery_voltage_v"] == 55.9
    # the view is closed; the store must still work
    store.append(Sample(timestamp=now + timedelta(seconds=11), readings={}))
    store.close()


def test_a_memory_backed_store_serves_reads_from_itself(tmp_path: Path) -> None:
    # ":memory:" cannot be reopened — every connect() makes a new empty
    # database — so the view is the store, and leaving the block must not
    # close the one connection everything shares.
    store = SqliteStore(":memory:", device=TEST_DEVICE)
    with store.read_view() as view:
        assert view is store
    store.append(Sample(timestamp=datetime.now(tz=UTC), readings={}))
    store.close()


def test_every_connection_carries_the_configured_durability(tmp_path: Path) -> None:
    # 0.6.10 made durability a deployment choice, and the choice used to reach
    # only the primary connection: a store configured "normal" ran its
    # maintenance passes at the distro default of FULL, fsyncing flash once a
    # minute for a guarantee the owner had traded away. 1 is NORMAL, 2 is FULL.
    store = SqliteStore(str(tmp_path / "sync.db"), device=TEST_DEVICE, synchronous="normal")
    maintenance = store.maintenance_connection()
    with store.read_view() as view:
        assert view._conn.execute("PRAGMA synchronous").fetchone() == (1,)
    assert store._conn.execute("PRAGMA synchronous").fetchone() == (1,)
    assert maintenance.execute("PRAGMA synchronous").fetchone() == (1,)
    maintenance.close()
    store.close()

    unchanged = SqliteStore(str(tmp_path / "full.db"), device=TEST_DEVICE)
    kept = unchanged.maintenance_connection()
    assert kept.execute("PRAGMA synchronous").fetchone() == (2,)
    kept.close()
    unchanged.close()


# --- the setup trio: describe, detect, apply (#setup slice A)


def test_setup_describes_the_machine_and_redacts_secrets(client: Any) -> None:
    r = client.get("/api/setup")
    assert r.status_code == 200
    body = r.json()
    makers = [m["name"] for m in body["manufacturers"]]
    assert "EG4" in makers and "Simulated" in makers
    assert "dongle" in body["transports"]
    # Masked with the settings module's own masker, so an echo of this value
    # is recognisable to the apply endpoint's discard rule.
    assert "•" in body["current"]["inverter_serial"]


def test_detect_returns_the_serial_the_hardware_answered(client: Any, monkeypatch: Any) -> None:
    from arraysense.api import routes

    async def fake_probe(body: Any) -> str:
        return "3352000000"

    monkeypatch.setattr(routes, "_probe_serial", fake_probe)
    r = client.post(
        "/api/setup/detect",
        json={"transport": "modbus_serial", "serial_device": "/dev/rs485"},
    )
    assert r.status_code == 200
    assert r.json() == {"serial": "3352000000"}


def test_detect_failure_is_a_message_with_a_cause_not_a_500(client: Any, monkeypatch: Any) -> None:
    from arraysense.api import routes

    async def fake_probe(body: Any) -> str:
        raise ConnectionError("could not open /dev/rs485: permission denied")

    monkeypatch.setattr(routes, "_probe_serial", fake_probe)
    r = client.post(
        "/api/setup/detect",
        json={"transport": "modbus_serial", "serial_device": "/dev/rs485"},
    )
    assert r.status_code == 502
    assert "permission denied" in r.json()["detail"]


def test_detect_refuses_an_unknown_transport(client: Any) -> None:
    r = client.post("/api/setup/detect", json={"transport": "carrier_pigeon"})
    assert r.status_code == 400


def test_detect_gives_the_wire_back_after_borrowing_it(client: Any, monkeypatch: Any) -> None:
    # The probe borrows the single client slot through yield mode. Whatever
    # happens on the wire, the collector must get it back — a detect that left
    # the service yielded would silently stop collection.
    from arraysense.api import routes

    async def fake_probe(body: Any) -> str:
        raise ConnectionError("nothing answered")

    monkeypatch.setattr(routes, "_probe_serial", fake_probe)
    r = client.post(
        "/api/setup/detect",
        json={
            "transport": "dongle",
            "dongle_host": "192.0.2.9",
            "dongle_serial": "BA00000000",
            "inverter_serial": "CE00000000",
        },
    )
    assert r.status_code == 502
    status = client.get("/api/status").json()
    assert status["running"] is True, "the borrow must hand the collector back"
    assert status["yielding"] is False


def test_detect_fills_a_masked_connection_from_the_current_config(
    client: Any, monkeypatch: Any
) -> None:
    # The settings page prefills the connection redacted. A Detect that did not
    # retype the secrets must probe the configured connection, not literally dial
    # a bullet-filled host — so the server substitutes the real values from the
    # config the collector runs on before the probe.
    from arraysense.api import routes

    seen: dict[str, str] = {}

    async def fake_probe(body: Any) -> str:
        seen["host"] = body.dongle_host
        seen["serial"] = body.dongle_serial
        seen["inverter"] = body.inverter_serial
        return "CE12345678"

    monkeypatch.setattr(routes, "_probe_serial", fake_probe)
    cfg = client.app.state.config
    r = client.post(
        "/api/setup/detect",
        json={
            "transport": "dongle",
            "dongle_host": "1•••9",
            "dongle_serial": "B•••s",
            "inverter_serial": "C•••i",
        },
    )
    assert r.status_code == 200
    assert seen["host"] == cfg.dongle_host
    assert seen["serial"] == cfg.dongle_serial
    assert seen["inverter"] == cfg.inverter_serial
    assert "•" not in seen["host"]


def test_detect_uses_retyped_connection_values_as_given(client: Any, monkeypatch: Any) -> None:
    # A value the person actually retyped carries no mask and is used verbatim,
    # so a Detect against a new connection probes what was typed, not the old one.
    from arraysense.api import routes

    seen: dict[str, str] = {}

    async def fake_probe(body: Any) -> str:
        seen["host"] = body.dongle_host
        return "CE12345678"

    monkeypatch.setattr(routes, "_probe_serial", fake_probe)
    r = client.post(
        "/api/setup/detect",
        json={
            "transport": "dongle",
            "dongle_host": "192.0.2.50",
            "dongle_serial": "BA99999999",
            "inverter_serial": "CE99999999",
        },
    )
    assert r.status_code == 200
    assert seen["host"] == "192.0.2.50"


def test_apply_refuses_an_invalid_combination_without_writing(
    client: Any, monkeypatch: Any
) -> None:
    # Validation is by constructing a real Config from the merged result, so
    # every rule the service enforces at boot refuses here first — one rule
    # set, never two. The scheduler is stubbed so the exact regression this
    # guards — a refusal that schedules anyway — fails as an assertion rather
    # than SIGTERMing the test runner.
    from arraysense.api import routes

    fired: list[str] = []
    monkeypatch.setattr(routes, "_schedule_restart", lambda: fired.append("restart"))
    r = client.post(
        "/api/setup/apply",
        json={"transport": "modbus_serial", "serial_device": ""},
    )
    assert r.status_code == 400
    assert "serial_device" in r.json()["detail"]
    values = client.get("/api/settings").json()["values"]
    assert values["connection.transport"] == "", "a refused apply must write nothing"
    assert fired == [], "a refused apply must not schedule a restart"


def test_apply_writes_the_overlay_and_schedules_a_restart(client: Any, monkeypatch: Any) -> None:
    from arraysense.api import routes

    fired: list[str] = []
    monkeypatch.setattr(routes, "_schedule_restart", lambda: fired.append("restart"))
    r = client.post(
        "/api/setup/apply",
        json={"model": "18kPV", "battery_source": "relayed"},
    )
    assert r.status_code == 200
    assert fired == ["restart"]
    values = client.get("/api/settings").json()["values"]
    assert values["connection.model"] == "18kPV"


def test_apply_refuses_what_boot_would_refuse(client: Any, monkeypatch: Any) -> None:
    # The review's sharpest finding: an overlay that apply accepts but boot
    # rejects is a service systemd crash-loops with no page left to repair it.
    # battery_source "direct" passes Config validation and fails the registry's
    # rules — exactly the gap.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    r = client.post("/api/setup/apply", json={"battery_source": "direct"})
    assert r.status_code == 400
    assert "not yet" in r.json()["detail"]
    values = client.get("/api/settings").json()["values"]
    assert values["connection.battery_source"] == ""


def test_apply_is_all_or_nothing(client: Any, monkeypatch: Any) -> None:
    # One act: a batch that stored its first key and refused its second would
    # leave an overlay the next boot assembles from halves.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    r = client.post(
        "/api/setup/apply",
        json={"transport": "dongle", "serial_device": "x" * 500},
    )
    assert r.status_code == 400
    values = client.get("/api/settings").json()["values"]
    assert values["connection.transport"] == "", "the batch partner must not have landed"


def test_apply_discards_masked_echoes_rather_than_storing_dots(
    client: Any, monkeypatch: Any
) -> None:
    # A full form submitted unchanged echoes the masks /api/setup showed it.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    masked = client.get("/api/setup").json()["current"]["inverter_serial"]
    r = client.post("/api/setup/apply", json={"inverter_serial": masked, "model": "18kPV"})
    assert r.status_code == 200
    values = client.get("/api/settings").json()["values"]
    assert "•" not in str(values["connection.inverter_serial"])


def test_dongle_detect_without_the_serial_is_a_named_refusal(client: Any) -> None:
    r = client.post(
        "/api/setup/detect",
        json={"transport": "dongle", "dongle_host": "192.0.2.9", "dongle_serial": "BA0"},
    )
    assert r.status_code == 400
    assert "authenticates" in r.json()["detail"]


def test_apply_can_switch_the_driver_family(client: Any, monkeypatch: Any) -> None:
    # The spec names driver switching for an existing installation; the first
    # cut of apply forgot it.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    r = client.post("/api/setup/apply", json={"driver": "fake", "model": "Simulated"})
    assert r.status_code == 200
    values = client.get("/api/settings").json()["values"]
    assert values["connection.driver"] == "fake"


def test_the_settings_page_cannot_persist_an_unbootable_connection(
    client: Any, monkeypatch: Any
) -> None:
    # The settings PUT is a second write path to the connection group. An
    # invalid combination through it would crash-loop the next boot exactly as
    # one through /setup/apply would, so it runs the same registry validation.
    r = client.put("/api/settings", json={"connection.transport": "carrier_pigeon"})
    assert r.status_code == 400
    r2 = client.put("/api/settings", json={"connection.battery_source": "direct"})
    assert r2.status_code == 400
    values = client.get("/api/settings").json()["values"]
    assert values["connection.transport"] == "", "nothing unbootable should have landed"


def test_detect_hands_back_an_already_yielded_collector(client: Any, monkeypatch: Any) -> None:
    # The borrow stops the collector and starts it again. If stop() left the
    # yield flag set, the restarted loop would return early forever — a detect
    # that silently stopped collection. Yield first, then detect, then assert
    # the collector is polling.
    from arraysense.api import routes

    async def fake_probe(body: Any) -> str:
        return "3352000000"

    monkeypatch.setattr(routes, "_probe_serial", fake_probe)
    client.post("/api/yield", json={"seconds": 60})
    r = client.post(
        "/api/setup/detect",
        json={
            "transport": "dongle",
            "dongle_host": "192.0.2.9",
            "dongle_serial": "BA00000000",
            "inverter_serial": "CE00000000",
        },
    )
    assert r.status_code == 200
    status = client.get("/api/status").json()
    assert status["yielding"] is False, "a stopped-then-started borrow must clear yield"
    assert status["running"] is True


def test_clearing_an_overlay_field_is_validated_against_the_file_not_the_overlay(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # The subtle boot-safety hole: an install whose file names eg4/18kPV but
    # whose stored overlay says fake/Simulated. Clearing connection.driver
    # reverts the driver to the file's eg4 at next boot while the stored model
    # override Simulated stays — eg4 has no model "Simulated", so the next boot
    # would crash. The write path must model that revert and refuse the clear.
    from arraysense.__main__ import build_app
    from arraysense.config import load
    from arraysense.settings import SettingsStore
    from arraysense.store.sqlite_store import SqliteStore

    db = tmp_path / "clear.db"
    seed = SqliteStore(str(db), device="CE00000000")
    s = SettingsStore(seed)
    s.set("connection.driver", "fake")
    s.set("connection.model", "Simulated")
    seed.close()

    path = tmp_path / "config.toml"
    path.write_text(
        'driver = "eg4_luxpower"\n'
        'model = "18kPV"\n'
        'dongle_host = "192.0.2.1"\n'
        'dongle_serial = "BA00000000"\n'
        'inverter_serial = "CE00000000"\n'
        f'database_path = "{db}"\n'
    )
    app_obj, store, _service = build_app(load(path))
    from fastapi.testclient import TestClient

    with TestClient(app_obj) as client:
        # Clearing the driver would revert to eg4 while model stays Simulated.
        r = client.put("/api/settings", json={"connection.driver": ""})
        assert r.status_code == 400, (
            "the clear reverts to an eg4/Simulated boot and must be refused"
        )
    store.close()


def test_a_malformed_connection_value_is_a_bad_request_not_a_500(
    client: Any, monkeypatch: Any
) -> None:
    # Coercing a pending value can raise TypeError (a list where a number is
    # wanted) or OverflowError (a number too large to become a float). Both are
    # bad input, not a server fault, on either write path.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    r = client.put("/api/settings", json={"connection.serial_baud": []})
    assert r.status_code == 400
    # Out of the field's bounds now, so pydantic refuses it at the door as a
    # 422 — a cleaner refusal than the 400 the coercion path gave. Either is a
    # bad request, never a 500.
    r2 = client.post("/api/setup/apply", json={"serial_baud": 10**400})
    assert r2.status_code in (400, 422)
    values = client.get("/api/settings").json()["values"]
    assert values["connection.serial_baud"] == 19200, "nothing malformed should have landed"


def test_detect_rejects_a_device_path_with_a_null_byte_as_422(client: Any) -> None:
    # A null byte in a device path makes pyserial raise on open — a 500 from a
    # sink deep in the transport stack. The field validator refuses it at the
    # door instead. No real device path holds a control character.
    r = client.post(
        "/api/setup/detect",
        json={"transport": "modbus_serial", "serial_device": "/dev/" + chr(0) + "rs485"},
    )
    assert r.status_code == 422


def test_no_write_path_persists_an_unbootable_connection_value(
    client: Any, monkeypatch: Any
) -> None:
    # The class two review rounds kept turning up: a connection value some write
    # path accepts and stores, which then crashes the collector on the next boot
    # or 500s a probe. This pins the invariant across every field at once —
    # every hostile-but-typed value is a 4xx on both persist paths and nothing
    # lands — so a new field that forgets its validation fails here loudly rather
    # than in production. Hosts are the one field that cannot be validated at the
    # door (any string is a plausible name); they are caught at connect instead
    # and covered by their own gap tests.
    from arraysense.api import routes
    from arraysense.settings import SettingsStore

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    # The overlay-settable connection fields, minus the two file-only ones:
    # dongle_port and database_path are carried by neither ApplyRequest nor the
    # settings registry, so the API cannot persist them (apply silently drops
    # the unknown field). database_path IS written at first-run apply and is
    # validated there — see test_main.py. Hosts are excluded per the docstring:
    # any string is a plausible name, so they are caught at connect, not here.
    cases: dict[str, object] = {
        "model": "99kPV",
        "driver": "no_such_driver",
        "transport": "carrier_pigeon",
        "battery_source": "quantum",
        "serial_device": "loop://x",
        "serial_baud": 10**9,
        "serial_unit_id": 999,
        "inverter_serial": "x" * 5000,
        "dongle_serial": "x" * 5000,
    }
    for field, value in cases.items():
        apply = client.post("/api/setup/apply", json={field: value})
        assert apply.status_code >= 400, f"apply accepted a bad {field}"
        put = client.put("/api/settings", json={f"connection.{field}": value})
        assert put.status_code >= 400, f"settings PUT accepted a bad {field}"
    # The invariant that matters, read from the raw overlay: nothing hostile
    # landed. A 4xx that still wrote would be the actual bug.
    overrides = SettingsStore(client.app.state.store).overrides()
    for field in cases:
        assert f"connection.{field}" not in overrides, f"a bad {field} was persisted"


def test_a_url_serial_device_is_refused_at_the_door_not_stored(
    client: Any, monkeypatch: Any
) -> None:
    # pyserial treats a device string with "://" as a URL and its handler raises
    # an undeclared KeyError/re.error at connect — not (TransportError, OSError).
    # Accepted and stored, it would kill the collector on the next boot and 500
    # the detect probe. Every write path refuses it, and detect never reaches
    # pyserial's URL dispatch: it is a 422 at the model, a device path is a
    # filesystem path.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    bad = "loop://?foo=bar"

    r = client.post("/api/setup/detect", json={"transport": "modbus_serial", "serial_device": bad})
    assert r.status_code == 422

    r = client.post("/api/setup/apply", json={"transport": "modbus_serial", "serial_device": bad})
    assert r.status_code == 422

    r = client.put("/api/settings", json={"connection.serial_device": bad})
    assert r.status_code == 400

    values = client.get("/api/settings").json()["values"]
    assert values["connection.serial_device"] == "", "no URL device should have landed"


def test_detect_survives_an_unresolvable_host_as_502(client: Any) -> None:
    # A dongle host that resolves through IDNA to an over-long DNS label raises
    # UnicodeError from connect, not OSError — an unhandled 500 on the
    # unauthenticated setup surface unless the probe catches it. It is an
    # unreachable endpoint like any other: a 502 with a cause, and the collector
    # is handed back.
    r = client.post(
        "/api/setup/detect",
        json={
            "transport": "dongle",
            "dongle_host": "a" * 64 + ".invalid",
            "dongle_serial": "BA00000000",
            "inverter_serial": "CE00000000",
        },
    )
    assert r.status_code == 502
    assert client.get("/api/status").json()["running"] is True


def test_apply_rejects_control_text_in_a_serial_as_422(client: Any, monkeypatch: Any) -> None:
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    # A lone surrogate cannot be sent as a Python object — the client fails to
    # encode it, just as a real HTTP client would. The attack is the JSON
    # escape in the raw body, which the server decodes to a surrogate string.
    r = client.post(
        "/api/setup/apply",
        content=b'{"inverter_serial": "\\ud800"}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


def test_a_fresh_weather_row_does_not_mask_a_quiet_inverter(empty_client: Any) -> None:
    # The weather poller writes every fifteen minutes whatever the inverter is
    # doing. If those rows aged the dashboard, the stale banner would stay
    # quiet through a real outage — the sky is not the inverter answering.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=40), readings={"pv_total_power_w": 1000.0})
    )
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(minutes=1), readings={"outside_temperature_c": 37.4})
    )
    service = empty_client.app.state.service
    service.status.running = True
    service.status.started_at = now
    service.status.last_success = None

    body = _staleness(empty_client)
    assert body["stale"] is True, "a sky reading must not count as the inverter reporting"
    assert body["reading_at"] == (now - timedelta(minutes=40)).replace(microsecond=0).isoformat()


def test_live_survives_a_weather_row_landing_between_polls(empty_client: Any) -> None:
    # The caller-level proof, not just the store mechanism: /api/live asks with
    # _LIVE_INVERTER, and if that list carried the site metrics a weather row
    # would match on its own two columns and hand the dashboard a row of nulls
    # for the seconds between a weather tick and the next poll. The store test
    # proves latest() skips foreign rows for the metrics it is asked; this
    # proves the live endpoint asks for the right ones.
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now - timedelta(seconds=30), readings={"pv_total_power_w": 5000.0})
    )
    empty_client.app.state.store.append(
        Sample(timestamp=now, readings={"outside_temperature_c": 37.4, "cloud_cover_pct": 13.0})
    )
    body = empty_client.get("/api/live").json()
    assert body["inverter"]["pv_total_power_w"] == 5000.0, (
        "a weather row must not blank the live view"
    )


# --- the sky block in /api/live ------------------------------------------------


def test_live_sky_returns_weather_when_stored(empty_client: Any) -> None:
    """A weather row stored -> sky carries the values, inverter unaffected."""
    now = datetime.now(tz=UTC)
    store = empty_client.app.state.store
    store.append(
        Sample(timestamp=now - timedelta(minutes=1), readings={"pv_total_power_w": 5000.0})
    )
    store.append(
        Sample(
            timestamp=now,
            readings={"outside_temperature_c": 22.5, "cloud_cover_pct": 65.0},
        )
    )
    body = empty_client.get("/api/live").json()
    assert body["inverter"]["pv_total_power_w"] == 5000.0
    assert body["sky"] is not None
    assert body["sky"]["outside_temperature_c"] == 22.5
    assert body["sky"]["cloud_cover_pct"] == 65.0
    assert "timestamp" in body["sky"]


def test_live_sky_is_none_with_no_weather(empty_client: Any) -> None:
    """No weather ever stored -> sky is None."""
    now = datetime.now(tz=UTC)
    empty_client.app.state.store.append(
        Sample(timestamp=now, readings={"pv_total_power_w": 5000.0})
    )
    body = empty_client.get("/api/live").json()
    assert body["inverter"]["pv_total_power_w"] == 5000.0
    assert body["sky"] is None


def test_live_sky_survives_a_newer_gap(empty_client: Any) -> None:
    """Weather row followed by a newer gap -> sky still carries the weather values.

    include_gaps=False makes the store walk past the gap row to the last real
    reading, so an inverter gap that lands newer than the last weather tick
    does not blank the sky.
    """
    now = datetime.now(tz=UTC)
    store = empty_client.app.state.store
    store.append(
        Sample(
            timestamp=now - timedelta(minutes=30),
            readings={"outside_temperature_c": 18.2, "cloud_cover_pct": 90.0},
        )
    )
    store.append(Sample.failed(now - timedelta(seconds=5), "TimeoutError: read timed out"))
    body = empty_client.get("/api/live").json()
    assert body["sky"] is not None
    assert body["sky"]["outside_temperature_c"] == 18.2
    assert body["sky"]["cloud_cover_pct"] == 90.0


# --- forecast -------------------------------------------------------------------


def test_forecast_empty_table_returns_configured_false(empty_client: Any) -> None:
    """With no forecast rows the page shows nothing — not zeros, not a guessed curve."""
    body = empty_client.get("/api/forecast").json()
    assert body["configured"] is False
    assert body["dawn"] == []
    assert body["latest"] == []
    assert body["actual"] == []
    assert body["expected_today_kwh"] is None
    assert body["actual_so_far_kwh"] is None
    assert body["tracking_pct"] is None
    assert "start" in body["day"]
    assert "now" in body


def test_forecast_delivers_curves_and_tracking_for_matched_hours(tmp_path: Path) -> None:
    """Dawn + revision staged for three hours, actuals for two — tracking covers only
    the hours that carry both a dawn expectation and a measurement.

    Staged in the real current UTC day and asked for with tz=UTC, so the
    endpoint's own "today" needs no pinning: datetime.now is immutable and the
    day bounds are deterministic the moment the zone is UTC.
    """
    import sqlite3

    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    store = SqliteStore(str(tmp_path / "fc.db"), device=TEST_DEVICE)

    # --- stage forecast: dawn plan at 00:05 local, revision at 06:00 local ---
    made_dawn = day_start + timedelta(minutes=5)
    made_rev = day_start + timedelta(hours=6)

    h1 = day_start
    h2 = day_start + timedelta(hours=1)
    h3 = day_start + timedelta(hours=2)

    store.append_forecast(made_dawn, [(h1, 100.0), (h2, 500.0), (h3, 800.0)])
    store.append_forecast(made_rev, [(h1, 120.0), (h2, 550.0), (h3, 900.0)])

    # --- stage actuals for the first two hours (h1 and h2) ---
    store.append(Sample(timestamp=h1 + timedelta(minutes=30), readings={"pv_total_power_w": 150.0}))
    store.append(Sample(timestamp=h2 + timedelta(minutes=10), readings={"pv_total_power_w": 550.0}))
    store.append(Sample(timestamp=h2 + timedelta(minutes=40), readings={"pv_total_power_w": 650.0}))

    # --- rebuild the hourly tier so the endpoint can read it ---
    conn = sqlite3.connect(str(tmp_path / "fc.db"))
    rebuild_inverter_hourly(
        conn, int(day_start.timestamp()) - 3600, int((day_start + timedelta(days=1)).timestamp())
    )
    conn.commit()
    conn.close()

    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "fc.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        body = c.get("/api/forecast", params={"tz": "UTC"}).json()
    store.close()

    assert body["configured"] is True

    # Dawn curve carries the early plan — the revision must not change it.
    assert len(body["dawn"]) == 3
    dawn_w = [e["expected_w"] for e in body["dawn"]]
    assert dawn_w == [100.0, 500.0, 800.0]

    # Latest curve carries the revision.
    assert len(body["latest"]) == 3
    latest_w = [e["expected_w"] for e in body["latest"]]
    assert latest_w == [120.0, 550.0, 900.0]

    # Only hours 1 and 2 have actual readings.
    assert len(body["actual"]) == 2
    actual_w = [e["mean_w"] for e in body["actual"]]
    assert actual_w == [150.0, 600.0]  # hour 2 mean: (550+650)/2

    # expected_today_kwh: sum latest / 1000
    assert body["expected_today_kwh"] == pytest.approx(1.57, abs=0.01)

    # actual_so_far_kwh: (150 + 600) / 1000
    assert body["actual_so_far_kwh"] == pytest.approx(0.75, abs=0.01)

    # tracking_pct: only hours 1 and 2 (both have dawn and actual)
    # dawn sum = 100 + 500 = 600, actual sum = 150 + 600 = 750
    # (750/600 - 1) * 100 = 25.0%
    assert body["tracking_pct"] == 25.0


def test_forecast_tracking_null_when_no_dawn_covers_the_actual_hours(tmp_path: Path) -> None:
    """Actual readings exist but no dawn plan covers the same hours — tracking
    must be null, never a figure from mismatched hours. Staged in the real
    current UTC day, asked with tz=UTC, no pinned clock."""
    import sqlite3

    day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    store = SqliteStore(str(tmp_path / "fc2.db"), device=TEST_DEVICE)

    # Forecast only covers hours 4-5 (later in the day).
    made_at = day_start + timedelta(minutes=5)
    h4 = day_start + timedelta(hours=4)
    h5 = day_start + timedelta(hours=5)
    store.append_forecast(made_at, [(h4, 400.0), (h5, 500.0)])

    # Actuals land in hours 1-2 (earlier, no forecast overlap).
    h1 = day_start
    h2 = day_start + timedelta(hours=1)
    store.append(Sample(timestamp=h1 + timedelta(minutes=30), readings={"pv_total_power_w": 200.0}))
    store.append(Sample(timestamp=h2 + timedelta(minutes=30), readings={"pv_total_power_w": 300.0}))

    conn = sqlite3.connect(str(tmp_path / "fc2.db"))
    rebuild_inverter_hourly(
        conn, int(day_start.timestamp()) - 3600, int((day_start + timedelta(days=1)).timestamp())
    )
    conn.commit()
    conn.close()

    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "fc2.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        body = c.get("/api/forecast", params={"tz": "UTC"}).json()
    store.close()

    assert body["configured"] is True
    assert len(body["actual"]) == 2
    assert len(body["dawn"]) == 2
    # The hours carry no overlap: tracking must be absent.
    assert body["tracking_pct"] is None
    # actual_so_far counts all actuals regardless of forecast coverage.
    assert body["actual_so_far_kwh"] is not None
    assert body["actual_so_far_kwh"] > 0


# --- /api/panels: the parsed array, bank, and MPPT guard -----------------------


def test_panels_serves_the_parsed_array_with_defaults_named(client: Any, monkeypatch: Any) -> None:
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    r = client.put(
        "/api/settings",
        json={"panels.strings": "East | 1 | 9 | 410 | 25 | 90 | bifacial=9"},
    )
    assert r.status_code == 200
    body = client.get("/api/panels").json()
    (s,) = body["strings"]
    assert s["name"] == "East"
    assert s["watts"] == 410.0
    assert s["bifacial_pct"] == 9.0
    assert "temp_coeff" in s["defaulted"] and "bifacial" not in s["defaulted"]
    assert body["battery"]["round_trip_pct"] == 91.4
    assert body["declared_mppts"] == 3  # the fake declares pv_strings=3


def test_an_unconfigured_array_serves_empty_not_error(client: Any) -> None:
    body = client.get("/api/panels").json()
    assert body["strings"] == []


def test_a_string_on_an_undeclared_mppt_is_refused_at_the_write(
    client: Any, monkeypatch: Any
) -> None:
    # The parser cannot know the driver (no context in check=), so the write
    # path enforces it where drivers are already in scope — the same layering
    # as _reject_unbootable_connection. The fake declares three strings.
    from arraysense.api import routes

    monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
    r = client.put(
        "/api/settings",
        json={"panels.strings": "Ghost | 7 | 9 | 410 | 25 | 90"},
    )
    assert r.status_code == 400
    assert "mppt" in r.json()["detail"].lower()
    values = client.get("/api/settings").json()["values"]
    assert values["panels.strings"] == "", "a refused write must store nothing"


# --- efficiency backfill -------------------------------------------------------


def test_backfill_writes_archive_hours_and_reports_progress(client: Any, monkeypatch: Any) -> None:
    # An owner-triggered range, never an implicit one: nobody's page load
    # should fire three hundred archive requests.
    from arraysense.api import routes

    def fake_archive(
        lat: float, lon: float, start: Any, end: Any, timeout: float = 30.0
    ) -> list[Sample]:
        return [
            Sample(
                timestamp=datetime(2026, 8, 1, 12, tzinfo=UTC),
                readings={"ghi_wm2": 900.0, "wind_speed_ms": 2.0},
            )
        ]

    monkeypatch.setattr(routes, "fetch_archive_hours", fake_archive)
    client.put("/api/settings", json={"site.latitude": 33.0, "site.longitude": -97.0})
    r = client.post("/api/efficiency/backfill", json={"start": "2026-08-01", "end": "2026-08-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 1
    assert body["hours_written"] == 1
    assert body["last_day"] == "2026-08-01"


def test_backfill_without_a_location_is_a_named_refusal(client: Any) -> None:
    r = client.post("/api/efficiency/backfill", json={"start": "2026-08-01", "end": "2026-08-01"})
    assert r.status_code == 400
    assert "location" in r.json()["detail"].lower()


def test_backfill_refuses_a_backwards_or_huge_range(client: Any, monkeypatch: Any) -> None:
    client.put("/api/settings", json={"site.latitude": 33.0, "site.longitude": -97.0})
    back = client.post(
        "/api/efficiency/backfill", json={"start": "2026-08-05", "end": "2026-08-01"}
    )
    assert back.status_code == 400
    huge = client.post(
        "/api/efficiency/backfill", json={"start": "2020-01-01", "end": "2026-08-01"}
    )
    assert huge.status_code == 400


# --- efficiency endpoint -------------------------------------------------------


def _efficiency_client(
    tmp_path: Path,
    build: Any,
    *,
    strings: str | None = "South | 1 | 20 | 400 | 25 | 180",
) -> Any:
    """A client with location, strings, stored samples, and a rebuilt hourly tier."""
    import sqlite3

    store = SqliteStore(str(tmp_path / "eff.db"), device=TEST_DEVICE)
    build(store)
    # Rebuild hourly tier so the efficiency endpoint can read it.
    conn = sqlite3.connect(str(tmp_path / "eff.db"))
    lo = int((datetime(2026, 8, 1, tzinfo=UTC) - timedelta(days=1)).timestamp())
    hi = int((datetime(2026, 8, 11, tzinfo=UTC)).timestamp())
    rebuild_inverter_hourly(conn, lo, hi)
    conn.commit()
    conn.close()
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "eff.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    client = TestClient(create_app(store=store, service=service, config=config))
    client.put("/api/settings", json={"site.latitude": 33.0, "site.longitude": -97.0})
    if strings is not None:
        client.put("/api/settings", json={"panels.strings": strings})
    return client


def test_efficiency_unconfigured_returns_configured_false(tmp_path: Path) -> None:
    """With no strings every figure is null or empty — never zero."""
    with _efficiency_client(tmp_path, lambda s: None, strings=None) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()
    assert body["configured"] is False
    assert body["summary"] is None
    assert body["waterfall"] == []
    assert body["strings"] == []
    assert body["hours"] is None
    assert body["worst_hour"] is None
    assert body["baseline"]["window_start"] is None


def test_efficiency_rejects_an_unknown_period(tmp_path: Path) -> None:
    with _efficiency_client(tmp_path, lambda s: None) as c:
        r = c.get(
            "/api/efficiency",
            params={"period": "year", "start": "2026-08-10"},
        )
    assert r.status_code == 400
    assert "year" in r.json()["detail"]


def test_efficiency_rejects_a_malformed_date(tmp_path: Path) -> None:
    with _efficiency_client(tmp_path, lambda s: None) as c:
        r = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "not-a-date"},
        )
    assert r.status_code == 400
    assert "YYYY-MM-DD" in r.json()["detail"]


def test_efficiency_rejects_an_unknown_timezone(tmp_path: Path) -> None:
    with _efficiency_client(tmp_path, lambda s: None) as c:
        r = c.get(
            "/api/efficiency",
            params={
                "period": "day",
                "start": "2026-08-10",
                "tz": "Mars/Olympus_Mons",
            },
        )
    assert r.status_code == 400


def test_efficiency_period_day_computes_daily_summary(tmp_path: Path) -> None:
    """A single day with modelled hours returns a summary and waterfall."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)  # 8am Chicago = 13:00 UTC

    def build(store: SqliteStore) -> None:
        # One hour at 8am local (13:00 UTC): sun at ~30°, clear sky.
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={
                "period": "day",
                "start": "2026-08-10",
                "tz": "America/Chicago",
            },
        ).json()

    assert body["configured"] is True
    assert body["period"] == "day"
    assert body["summary"] is not None
    s = body["summary"]
    # The day had at least some modelled hours.
    assert s["expected_kwh"] > 0
    assert s["actual_kwh"] > 0
    assert s["pr"] is not None
    assert s["specific_yield"] is not None
    assert s["tolerance_pct"] == 3.0
    # One hour is partial (< 60% of daylight covered).
    assert s["partial"] is True


def test_efficiency_waterfall_reconciles_to_actual(tmp_path: Path) -> None:
    """The walk from expected to actual must close, in either direction.

    An array can beat its model as easily as fall short of it, and a shortfall
    clamped at zero cannot express that. If the segments do not sum to what the
    inverter actually made, the page is drawing a decomposition of a number it
    does not have.
    """
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    wf = {seg["name"]: seg["kwh"] for seg in body["waterfall"]}
    reconciled = wf["expected"] - wf["unexplained"] - wf["curtailed"] + wf["unmodelled_gain"]
    assert reconciled == pytest.approx(wf["actual"], abs=0.01), (
        f"expected({wf['expected']}) - unexplained({wf['unexplained']}) "
        f"- curtailed({wf['curtailed']}) + gain({wf['unmodelled_gain']}) "
        f"!= actual({wf['actual']})"
    )
    # Only one of the two residual segments can be non-zero: the array either
    # fell short of the model or beat it, never both at once.
    assert wf["unexplained"] == 0.0 or wf["unmodelled_gain"] == 0.0

    # And the segment that means "we produced more than predicted" must never
    # be drawn as a loss.
    gain = next(s for s in body["waterfall"] if s["name"] == "unmodelled_gain")
    assert gain["penalised"] is False


def test_efficiency_period_day_has_hourly_breakdown(tmp_path: Path) -> None:
    """period=day must carry the hours array computed from stored inputs."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    assert body["hours"] is not None
    assert len(body["hours"]) > 0
    hour = body["hours"][0]
    for key in ("hour", "expected_kwh", "actual_kwh", "curtailed_kwh", "unexplained_kwh"):
        assert key in hour, key


def test_efficiency_period_week_has_no_hours_array(tmp_path: Path) -> None:
    """Only period=day carries the hours array."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "week", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    assert body["hours"] is None


def test_efficiency_pr_excludes_curtailed_energy(tmp_path: Path) -> None:
    """PR = actual / (expected - curtailed), null when denominator <= 0."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    s = body["summary"]
    if s["pr"] is not None:
        # PR denominator excludes curtailed.
        denom = s["expected_kwh"] - s["curtailed_kwh"]
        if denom > 0:
            assert s["pr"] == pytest.approx(s["actual_kwh"] / denom, abs=0.001)


def test_efficiency_curtailed_not_penalised_in_waterfall(tmp_path: Path) -> None:
    """Curtailed energy carries penalised: false so no page treats it as a loss."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    curtailed_seg = next(s for s in body["waterfall"] if s["name"] == "curtailed")
    assert curtailed_seg["penalised"] is False


def test_efficiency_period_month_spans_calendar_days(tmp_path: Path) -> None:
    """period=month covers the full calendar month."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "month", "start": "2026-08-01", "tz": "America/Chicago"},
        ).json()

    assert body["period"] == "month"
    # August has 31 days.
    assert body["start"].startswith("2026-08-01")
    assert body["end"].startswith("2026-09-01")


def test_efficiency_missing_hours_marks_partial(tmp_path: Path) -> None:
    """A day with fewer than 60% of daylight hours modelled is partial."""
    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    # One hour of a summer day is definitely partial.
    assert body["summary"]["partial"] is True


def test_efficiency_with_no_data_is_absent_not_zero(tmp_path: Path) -> None:
    """A day with no stored row has no summary, not a zeroed one."""
    with _efficiency_client(tmp_path, lambda s: None) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    # Configured but no data: summary null, not zero.
    assert body["configured"] is True
    assert body["summary"] is None
    assert body["waterfall"] == []


def test_efficiency_waterfall_reconciles_across_a_mixed_range(tmp_path: Path) -> None:
    """A week holding both a short day and a generous one must still add up.

    Each day's unexplained figure is clamped at zero, so summing them counts
    every day that fell short and ignores every day that ran ahead. A range
    with one of each then reports a shortfall beside an expected and an actual
    that are equal, and the walk from expected to actual visibly fails to close
    in front of the owner. The residual has to come from the totals.
    """

    def build(store: SqliteStore) -> None:
        # Two days of the same sun, one producing far less than the other.
        for day_offset, watts in ((0, 1000.0), (1, 8000.0)):
            store.append(
                Sample(
                    timestamp=datetime(2026, 8, 4 + day_offset, 13, 0, tzinfo=UTC),
                    readings={
                        "pv1_power_w": watts,
                        "pv1_voltage_v": 310.0,
                        "pv1_current_a": watts / 310.0,
                        "ghi_wm2": 600.0,
                        "dni_wm2": 500.0,
                        "dhi_wm2": 100.0,
                        "wind_speed_ms": 2.0,
                        "outside_temperature_c": 30.0,
                        "battery_soc_pct": 60.0,
                        "bms_charge_current_limit_a": 400.0,
                    },
                )
            )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "week", "start": "2026-08-03", "tz": "America/Chicago"},
        ).json()

    wf = {s["name"]: s["kwh"] for s in body["waterfall"]}
    closed = wf["expected"] - wf["unexplained"] - wf["curtailed"] + wf["unmodelled_gain"]
    assert closed == pytest.approx(wf["actual"], abs=0.01), (
        f"expected({wf['expected']}) - unexplained({wf['unexplained']}) "
        f"- curtailed({wf['curtailed']}) + gain({wf['unmodelled_gain']}) "
        f"!= actual({wf['actual']})"
    )
    assert wf["unexplained"] == 0.0 or wf["unmodelled_gain"] == 0.0


def test_efficiency_does_not_serve_a_day_scored_against_a_different_array(
    tmp_path: Path,
) -> None:
    """A stored score belongs to the array description it was taken under.

    The maintenance pass rescores today and yesterday, but nothing revisits
    last month. Without this check, correcting a panel count leaves every
    older day carrying a score computed for an array that no longer exists,
    served as though it were current.
    """
    from arraysense.efficiency import EfficiencyRow

    day = datetime(2026, 8, 10, 13, 0, tzinfo=UTC)

    def build(store: SqliteStore) -> None:
        store.append(
            Sample(
                timestamp=day,
                readings={
                    "pv1_power_w": 4000.0,
                    "pv1_voltage_v": 310.0,
                    "pv1_current_a": 12.9,
                    "ghi_wm2": 600.0,
                    "dni_wm2": 500.0,
                    "dhi_wm2": 100.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 60.0,
                    "bms_charge_current_limit_a": 400.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        # A stored row from an older description of the array, with an
        # unmistakable figure that must not reach the response.
        store = SqliteStore(str(tmp_path / "eff.db"), device=TEST_DEVICE)
        local_day = datetime(2026, 8, 10, tzinfo=ZoneInfo("America/Chicago"))
        store.write_efficiency_day(
            [
                EfficiencyRow(
                    day=local_day,
                    string_name="",
                    expected_kwh=999.0,
                    actual_kwh=999.0,
                    curtailed_kwh=0.0,
                    unexplained_kwh=0.0,
                    modelled_hours=24,
                    partial=False,
                    pr=1.0,
                    config_version=-1,
                )
            ]
        )
        store.close()
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    assert body["summary"] is not None
    assert body["summary"]["expected_kwh"] != 999.0, (
        "a day scored against a different array was served as current"
    )


def test_efficiency_books_real_curtailment_and_never_penalises_it(tmp_path: Path) -> None:
    """Staged so curtailment is actually produced, not merely asserted about.

    A test whose fixture yields zero curtailment passes whatever the code does
    with it -- the segment reads 0.0 and every claim about it is vacuously
    true. This stages a full bank, a pinched charge limit and a string held
    near open circuit, so a regression that penalised refused energy or filed
    it as an unexplained loss turns this red.
    """

    def build(store: SqliteStore) -> None:
        base = datetime(2026, 8, 10, tzinfo=UTC)
        # Ordinary hours give the string an operating point to be judged against.
        for hour in (13, 14, 15, 16, 17, 19, 20):
            store.append(
                Sample(
                    timestamp=base + timedelta(hours=hour),
                    readings={
                        "pv1_power_w": 6000.0,
                        "pv1_voltage_v": 310.0,
                        "pv1_current_a": 19.4,
                        "ghi_wm2": 800.0,
                        "dni_wm2": 850.0,
                        "dhi_wm2": 120.0,
                        "wind_speed_ms": 2.0,
                        "outside_temperature_c": 30.0,
                        "battery_soc_pct": 60.0,
                        "bms_charge_current_limit_a": 400.0,
                    },
                )
            )
        # Then one hour with the bank full, the limit pinched, and the string
        # walked up toward open circuit while producing almost nothing.
        store.append(
            Sample(
                timestamp=base + timedelta(hours=18),
                readings={
                    "pv1_power_w": 400.0,
                    "pv1_voltage_v": 372.0,
                    "pv1_current_a": 1.1,
                    "ghi_wm2": 800.0,
                    "dni_wm2": 850.0,
                    "dhi_wm2": 120.0,
                    "wind_speed_ms": 2.0,
                    "outside_temperature_c": 30.0,
                    "battery_soc_pct": 100.0,
                    "bms_charge_current_limit_a": 40.0,
                },
            )
        )

    with _efficiency_client(tmp_path, build) as c:
        body = c.get(
            "/api/efficiency",
            params={"period": "day", "start": "2026-08-10", "tz": "America/Chicago"},
        ).json()

    summary = body["summary"]
    assert summary["curtailed_kwh"] > 0.0, "the fixture produced no curtailment to test"

    segments = {s["name"]: s for s in body["waterfall"]}
    assert segments["curtailed"]["kwh"] > 0.0
    assert segments["curtailed"]["penalised"] is False, "refused energy was drawn as a loss"

    # And it must leave the ratio's denominator, which is the entire point:
    # a demand-limited hour must not read as a fault.
    naive = summary["actual_kwh"] / summary["expected_kwh"]
    assert summary["pr"] > naive, "curtailed energy is still counted against the score"

    wf = {k: v["kwh"] for k, v in segments.items()}
    closed = wf["expected"] - wf["unexplained"] - wf["curtailed"] + wf["unmodelled_gain"]
    assert closed == pytest.approx(wf["actual"], abs=0.01)


def test_the_efficiency_page_is_served(client: Any) -> None:
    """The Efficiency page is a nav destination, and a nav target that answers
    404 reads as the feature being down rather than a page that was never
    written. The allow-list test only proves the route exists; this is what
    proves the file behind it is actually served, which is how a PAGES entry
    whose page never lands gets noticed at all.
    """
    r = client.get("/efficiency")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
