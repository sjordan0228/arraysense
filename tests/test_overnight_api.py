"""test_overnight_api.py — the overnight plan endpoint over a temp store.

Same shape as test_api.py: a file-backed SqliteStore seeded with real
samples, the minute tier rebuilt from them, and the app assembled with a
fake source. The endpoint is read-only, so no settings writes and no
hardware stand-ins beyond the module rows the fixtures choose to seed.

A plan is only "ok" when calibration reads trustworthy, and a bank whose
history holds no completed full charge reads elevated: the default fixture
therefore replays the same recent-full-charge pattern test_api.py's
calibration tests use, dated three days back, so the OK-path contracts are
pinned against a genuinely believable state of charge.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from arraysense.api.app import create_app
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.models import BatteryModuleSample, Sample
from arraysense.store.rollup import rebuild_inverter_minute
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

HISTORY_DAYS = 10


def _bank(
    store: SqliteStore,
    when: datetime,
    volts: float,
    socs: dict[str, float],
    extra: dict[str, float] | None = None,
) -> None:
    """Record one poll of the bank at a given voltage with the given pack states."""
    readings: dict[str, float] = {
        "battery_voltage_v": volts,
        "bms_charge_voltage_ref_v": 56.0,
    }
    if extra:
        readings.update(extra)
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


def _build_client(
    tmp_path: Path,
    *,
    days: int = HISTORY_DAYS,
    load: float = 2000.0,
    soc: float | None = 50.0,
    power: float = -1200.0,
    capacity: float | None = 280.0,
    modules: tuple[BatteryModuleSample, ...] = (),
    charge: bool = True,
    step_seconds: int = 300,
) -> Any:
    """A client whose store holds `days` days of dense history to now."""
    store = SqliteStore(str(tmp_path / "oh.db"), device=TEST_DEVICE)
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=days)
    readings: dict[str, float] = {"load_power_w": load}
    if soc is not None:
        readings["battery_soc_pct"] = soc
    if power is not None:
        readings["battery_power_w"] = power
    if capacity is not None:
        readings["battery_full_capacity_ah"] = capacity
    when = start
    while when < now:
        store.append(Sample(timestamp=when, readings=dict(readings), battery_modules=modules))
        when += timedelta(seconds=step_seconds)
    if charge and not modules:
        # The pattern test_api.py's calibration tests prove a recent full
        # charge with: three days ago the bank sat at its charge reference
        # for half an hour and every pack reached full; now it rests at 53 V
        # with the packs a point apart.
        charged = now - timedelta(days=3)
        for minute in range(0, 31, 2):
            _bank(store, charged + timedelta(minutes=minute), 55.9, {"A": 100.0, "B": 100.0})
        _bank(
            store,
            now - timedelta(seconds=step_seconds),
            53.0,
            {"A": 61.0, "B": 62.0},
            extra={"load_power_w": load},
        )
    rebuild_inverter_minute(store._conn, int(start.timestamp()), int(now.timestamp()))
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "oh.db"),
        poll_interval=11.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    return TestClient(app)


def test_overnight_answers_with_the_contract_shape(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    body = client.get("/api/overnight")
    assert body.status_code == 200
    data = body.json()
    assert set(data) == {"scenarios", "replay", "inputs", "guidance", "assumptions"}
    assert set(data["scenarios"]) == {"typical", "essential", "scheduled"}
    typical = data["scenarios"]["typical"]
    assert typical["status"] == "ok", typical
    assert data["scenarios"]["essential"] is not None
    assert data["scenarios"]["scheduled"] is None
    assert data["inputs"]["calibration_severity"] == "none", data["inputs"]
    assert set(data["inputs"]) == {
        "soc_now_pct",
        "usable_capacity_ah",
        "min_soc_pct",
        "efficiency_pct",
        "discharge_limit_w",
        "calibration_severity",
        "drift_band_pct",
        "stale",
        "emporia_enabled",
    }
    assert data["inputs"]["emporia_enabled"] is False
    assert isinstance(data["guidance"], list)
    # The outage caveat rides the assumptions even on a healthy answer: the
    # reported plan is the grid-available one and says so.
    assert any("outage" in text.lower() for text in data["assumptions"])
    # The grid-available semantics: the reply is a projection, and wherever
    # the reserve floor is reached the story is an import, not an outage.
    assert "The grid is assumed available" in " ".join(data["assumptions"])


def test_settings_and_store_values_flow_into_the_inputs(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    client.put(
        "/api/settings",
        json={
            "battery.min_soc_pct": 25.0,
            "battery.round_trip_pct": 80.0,
            "battery.max_charge_a": 60.0,
        },
    )
    data = client.get("/api/overnight").json()
    inputs = data["inputs"]
    assert inputs["min_soc_pct"] == 25.0
    assert inputs["efficiency_pct"] == 80.0
    assert inputs["soc_now_pct"] == 50.0
    assert inputs["usable_capacity_ah"] == 210.0
    # The discharge limit is the p95 of the recorded discharge power: every
    # seeded step read -1200 W, so the limit is that magnitude, not a zero
    # and not an invented default.
    assert inputs["discharge_limit_w"] == 1200.0
    assert inputs["stale"] is False


def test_replay_nights_appear_with_their_errors(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    data = client.get("/api/overnight").json()
    replay = data["replay"]
    assert set(replay) == {"nights", "crossing_errors_minutes", "wh_errors"}
    assert replay["nights"], "ten days of history must yield replayed nights"
    for night in replay["nights"]:
        assert set(night) == {
            "night",
            "projected_crossing",
            "actual_crossing",
            "wh_error",
        }
    assert isinstance(replay["crossing_errors_minutes"], list)
    assert all(isinstance(value, float) for value in replay["crossing_errors_minutes"])
    assert isinstance(replay["wh_errors"], list)


def test_scheduled_parameters_add_the_scheduled_scenario(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    start = (datetime.now(tz=UTC) + timedelta(hours=2)).isoformat()
    data = client.get(
        "/api/overnight",
        params={
            "sched_start": start,
            "sched_duration_s": 3600,
            "sched_watts": 3000.0,
        },
    ).json()
    scheduled = data["scenarios"]["scheduled"]
    assert scheduled is not None
    assert any("scheduled load" in text.lower() for text in scheduled["assumptions"])


def test_essential_scenario_follows_the_allowance(tmp_path: Path) -> None:
    client = _build_client(tmp_path)
    zero = client.get("/api/overnight", params={"essential_allowance_w": 0.0}).json()
    zero_curve = zero["scenarios"]["essential"]
    assert zero_curve is not None
    assert all(point[1] == zero_curve["trajectory"][0][1] for point in zero_curve["trajectory"]), (
        "a zero allowance draws nothing, so the SoC stays flat"
    )
    big = client.get("/api/overnight", params={"essential_allowance_w": 800.0}).json()
    assert big["scenarios"]["essential"] is not None


def test_empty_store_refuses_and_names_the_gaps(tmp_path: Path) -> None:
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
    with TestClient(app) as client:
        data = client.get("/api/overnight").json()
    store.close()
    assert data["scenarios"]["typical"]["status"] == "estimate_unavailable"
    assert data["scenarios"]["typical"]["reason"]
    assert data["scenarios"]["essential"] is None or (
        data["scenarios"]["essential"]["status"] == "ok"
        and len(data["scenarios"]["essential"]["trajectory"]) <= 2
    )
    assert data["replay"]["nights"] == []
    assert data["inputs"]["soc_now_pct"] == 10.0
    assert data["inputs"]["usable_capacity_ah"] is None
    assert data["inputs"]["stale"] is True
    joined = " ".join(data["guidance"]).lower()
    assert "capacity" in joined
    assert "history" in joined
    assert "state of charge" in joined


def test_missing_capacity_is_named_even_with_full_history(tmp_path: Path) -> None:
    client = _build_client(tmp_path, capacity=None)
    data = client.get("/api/overnight").json()
    assert data["scenarios"]["typical"]["status"] == "estimate_unavailable"
    joined = " ".join(data["guidance"]).lower()
    assert "capacity" in joined
    assert "history" not in joined


def test_drift_state_reaches_the_guidance(tmp_path: Path) -> None:
    # Two packs agreeing on voltage and disagreeing 50 points on state of
    # charge, with no completed charge on record: the plan must carry the
    # drift up as guidance and as the drift band input, and must not answer
    # a projection on top of a state of charge it does not believe.
    modules = (
        BatteryModuleSample(serial="AAA", slot=1, soc_pct=75.0, voltage_v=52.0),
        BatteryModuleSample(serial="BBB", slot=2, soc_pct=25.0, voltage_v=52.0),
    )
    client = _build_client(tmp_path, days=2, modules=modules)
    data = client.get("/api/overnight").json()
    joined = " ".join(data["guidance"]).lower()
    assert "drift" in joined, data["guidance"]
    assert data["scenarios"]["typical"]["status"] == "estimate_unavailable"
    if data["inputs"]["drift_band_pct"] is not None:
        assert data["inputs"]["drift_band_pct"] == 50.0
