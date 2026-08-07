"""test_api.py — the HTTP surface, over a temporary store and a fake inverter.

No hardware and no real config: the app is assembled around a database in
tmp_path and a collector driving FakeSource, which is the whole reason
create_app takes its dependencies rather than building them. The collector is
real, so the yield endpoints exercise the actual code rather than a stub that
always agrees.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arraysense.api.app import create_app
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.sqlite_store import SqliteStore

T0 = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def client(tmp_path: Path) -> Any:
    store = SqliteStore(str(tmp_path / "api.db"))
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


def test_status_reports_the_collector(client: Any) -> None:
    body = client.get("/api/status").json()
    assert body["running"] is True
    assert body["yielding"] is False
    assert body["total_samples"] == 7
    assert body["version"]


def test_live_returns_the_latest_inverter_and_every_module(client: Any) -> None:
    body = client.get("/api/live").json()
    assert body["inverter"]["pv_total_power_w"] == 3000.0
    assert {m["serial"] for m in body["modules"]} == {"AAA", "BBB"}
    assert [m["soc_pct"] for m in body["modules"]] == [92.0, 22.0]


def test_live_keeps_absent_values_null(client: Any) -> None:
    # A battery block empty because CAN is down must not arrive as 0.
    body = client.get("/api/live").json()
    assert body["inverter"]["grid_power_w"] is None


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


def _bank(store: SqliteStore, when: datetime, volts: float, socs: dict[str, float]) -> None:
    """Record one poll of the bank at a given voltage with the given pack states."""
    store.append(
        Sample(
            timestamp=when,
            readings={"battery_voltage_v": volts, "bms_charge_voltage_ref_v": 56.0},
            battery_modules=tuple(
                BatteryModuleSample(serial=s, slot=i + 1, soc_pct=soc, voltage_v=volts)
                for i, (s, soc) in enumerate(socs.items())
            ),
        )
    )


def _calibration_client(tmp_path: Path, build: Any) -> Any:
    store = SqliteStore(str(tmp_path / "cal.db"))
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
    # A wiring fault must not also tell the owner to go and charge the bank.
    assert "charge" not in body["detail"].lower()


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
