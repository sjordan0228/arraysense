"""test_api.py — the HTTP surface, over a temporary store and a fake inverter.

No hardware and no real config: the app is assembled around a database in
tmp_path and a collector driving FakeSource, which is the whole reason
create_app takes its dependencies rather than building them. The collector is
real, so the yield endpoints exercise the actual code rather than a stub that
always agrees.
"""

from __future__ import annotations

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
    # /api/energy refuses an unknown zone with a 400 and the page's answer is
    # to ask again without it. Refusing here too would take down the banner
    # over a stale browser zone, and the fallback is the same zone that retry
    # lands on — so the calendar the page builds still matches the reply.
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
    assert device["metrics"] is None
    assert device["battery_module_metrics"] is None
