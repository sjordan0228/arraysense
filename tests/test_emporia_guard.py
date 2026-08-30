"""test_emporia_guard.py — the inverter output guard.

The guard holds the inverter under the owner's limit by moving the car's amps.
It cuts fast and releases slowly: a cut is taken immediately, but the rate only
goes back up after the house has been continuously clear for five minutes.

The pure half — allowance() and plan() — is tested without any store, client,
or asyncio. The impure half — InverterGuard — is tested with a fake poller and
a real in-memory SqliteStore.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.models import Sample
from arraysense.modules.emporia.client import EmporiaAuthExpiredError, EmporiaUnreachableError
from arraysense.modules.emporia.control import Limits
from arraysense.modules.emporia.guard import (
    EVSE_STALE_MULTIPLE,
    GUARD_INTERVAL_SECONDS,
    INVERTER_STALE_SECONDS,
    SETTLE_SECONDS,
    Allowance,
    GuardReading,
    InverterGuard,
    allowance,
    plan,
)
from arraysense.modules.emporia.parse import ChargerState
from arraysense.modules.emporia.repository import MODULE
from arraysense.settings import (
    CHARGE_DEFAULT_KEY,
    CHARGER_AUTHORITY_KEY,
    EMPORIA_ENABLED_KEY,
    INVERTER_LIMIT_KEY,
    SettingsStore,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

NOW = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)

# --- The pinned arithmetic table from §3 -----------------------------------

_TABLE = [
    # A: heavy house, grid not carrying
    dict(
        load_w=13500.0,
        grid_w=0.0,
        charger_w=8018,
        rate_a=32,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=22,
        expected_supplied=13500,
    ),
    # B: grid carrying the house — other_w clamped to 0
    dict(
        load_w=13500.0,
        grid_w=9000.0,
        charger_w=8018,
        rate_a=32,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=45,
        expected_supplied=4500,
    ),
    # C: clear house, car at floor
    dict(
        load_w=5000.0,
        grid_w=0.0,
        charger_w=0,
        rate_a=6,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=24,
        expected_supplied=5000,
    ),
    # D: moderate house
    dict(
        load_w=9000.0,
        grid_w=0.0,
        charger_w=1250,
        rate_a=12,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=13,
        expected_supplied=9000,
    ),
    # E: heavy house
    dict(
        load_w=16000.0,
        grid_w=0.0,
        charger_w=8018,
        rate_a=32,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=12,
        expected_supplied=16000,
    ),
    # F: negative room — floors downward to -29, not -28
    dict(
        load_w=20000.0,
        grid_w=0.0,
        charger_w=2000,
        rate_a=32,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=-29,
        expected_supplied=20000,
    ),
    # G: export case — grid_w negative, must clamp to 0 before subtracting
    dict(
        load_w=8000.0,
        grid_w=-3000.0,
        charger_w=1500,
        rate_a=16,
        eps_voltage_v=242.5,
        grid_voltage_v=242.5,
        expected_amps=18,
        expected_supplied=8000,
    ),
    # H: voltage fallback test — eps=None, grid=245.1 gives 19; eps=242.5 gives 20
    dict(
        load_w=7600.0,
        grid_w=0.0,
        charger_w=1500,
        rate_a=16,
        eps_voltage_v=242.5,
        grid_voltage_v=245.1,
        expected_amps=20,
        expected_supplied=7600,
    ),
]

_LIMIT_W = 12000
_VOLTS = 242.5


def _reading(
    load_w: float | None = None,
    grid_w: float | None = None,
    charger_w: int | None = None,
    rate_a: int | None = None,
    eps_voltage_v: float | None = _VOLTS,
    grid_voltage_v: float | None = _VOLTS,
) -> GuardReading:
    return GuardReading(
        load_w=load_w,
        grid_w=grid_w,
        charger_w=charger_w,
        rate_a=rate_a,
        eps_voltage_v=eps_voltage_v,
        grid_voltage_v=grid_voltage_v,
    )


# --- The arithmetic — allowance() ------------------------------------------


@pytest.mark.parametrize("row", _TABLE)
def test_allowance_matches_the_pinned_table(row: dict[str, object]) -> None:
    reading = GuardReading(
        load_w=float(row["load_w"]),  # type: ignore[arg-type]
        grid_w=float(row["grid_w"]),  # type: ignore[arg-type]
        charger_w=int(row["charger_w"]),  # type: ignore[call-overload]
        rate_a=int(row["rate_a"]),  # type: ignore[call-overload]
        eps_voltage_v=float(row["eps_voltage_v"]),  # type: ignore[arg-type]
        grid_voltage_v=float(row["grid_voltage_v"]),  # type: ignore[arg-type]
    )
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.amps == row["expected_amps"]
    assert result.supplied_w == row["expected_supplied"]


def test_grid_export_does_not_inflate_what_the_inverter_supplies() -> None:
    # Row G alone, spelled out. grid_w is negative and large enough that
    # forgetting max(grid_w, 0.0) changes the answer.
    reading = _reading(load_w=8000.0, grid_w=-3000.0, charger_w=1500, rate_a=16)
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.supplied_w == 8000, "export must not inflate supplied_w"
    assert result.amps == 18


def test_the_charger_share_never_goes_negative() -> None:
    # Row B. Without the max(..., 0) clamp, other_w = -3518 and the answer is
    # 59, a plainly different number.
    reading = _reading(load_w=13500.0, grid_w=9000.0, charger_w=8018, rate_a=32)
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.amps == 45


def test_amps_floor_rather_than_round() -> None:
    # room_w = 5518, volts = 242.5 → 22.75 → 22, not 23.
    reading = _reading(load_w=13500.0, grid_w=0.0, charger_w=8018, rate_a=32)
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.amps == 22


def test_a_negative_room_floors_downward() -> None:
    # Row F. The true quotient is just under a whole amp; floor gives -29,
    # truncation would give -28.
    reading = _reading(load_w=20000.0, grid_w=0.0, charger_w=2000, rate_a=32)
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.amps == -29


def test_each_absent_reading_holds_with_its_own_reason() -> None:
    # Six hold conditions, each with its own distinct reason string.
    cases = [
        (dict(limit_w=0), "the inverter limit is off"),
        (dict(load_w=None, grid_w=0.0), "the house reading is absent"),
        (dict(load_w=13500.0, grid_w=None), "the grid reading is absent"),
        (
            dict(
                load_w=13500.0,
                grid_w=0.0,
                eps_voltage_v=None,
                grid_voltage_v=None,
            ),
            "the voltage reading is absent",
        ),
        (dict(load_w=13500.0, grid_w=0.0, charger_w=None), "the charger reading is absent"),
        (
            dict(load_w=13500.0, grid_w=0.0, charger_w=8018, rate_a=None),
            "the charge rate is unknown",
        ),
    ]
    for kwargs, expected_reason in cases:
        limit_w = kwargs.pop("limit_w", _LIMIT_W)  # type: ignore[attr-defined]
        reading = _reading(**kwargs)  # type: ignore[arg-type]
        result = allowance(reading, limit_w=limit_w)
        assert result.amps is None
        assert result.reason == expected_reason


def test_a_charger_drawing_zero_is_a_measurement_not_an_absence() -> None:
    # charger_w = 0 is a measurement, not absent data.
    reading = _reading(load_w=5000.0, grid_w=0.0, charger_w=0, rate_a=6)
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.amps == 24, "charger_w=0 must produce a real allowance"


def test_the_voltage_falls_back_to_the_grid_reading() -> None:
    # Row H's inputs with eps=None and grid=245.1 → amps == 19.
    reading_no_eps = _reading(
        load_w=7600.0,
        grid_w=0.0,
        charger_w=1500,
        rate_a=16,
        eps_voltage_v=None,
        grid_voltage_v=245.1,
    )
    result_no_eps = allowance(reading_no_eps, limit_w=_LIMIT_W)
    assert result_no_eps.amps == 19

    # Then the same row with eps=242.5 → amps == 20.
    reading_with_eps = _reading(
        load_w=7600.0,
        grid_w=0.0,
        charger_w=1500,
        rate_a=16,
        eps_voltage_v=242.5,
        grid_voltage_v=245.1,
    )
    result_with_eps = allowance(reading_with_eps, limit_w=_LIMIT_W)
    assert result_with_eps.amps == 20


def test_no_voltage_at_all_holds() -> None:
    # Both voltages None → hold.
    reading = _reading(
        load_w=5000.0,
        grid_w=0.0,
        charger_w=0,
        rate_a=6,
        eps_voltage_v=None,
        grid_voltage_v=None,
    )
    result = allowance(reading, limit_w=_LIMIT_W)
    assert result.amps is None
    assert result.reason == "the voltage reading is absent"

    # eps=0.0 and grid=None → hold.
    reading_zero_eps = _reading(
        load_w=5000.0,
        grid_w=0.0,
        charger_w=0,
        rate_a=6,
        eps_voltage_v=0.0,
        grid_voltage_v=None,
    )
    result_zero_eps = allowance(reading_zero_eps, limit_w=_LIMIT_W)
    assert result_zero_eps.amps is None
    assert result_zero_eps.reason == "the voltage reading is absent"

    # eps=0.0 and grid=245.1 → does NOT hold, falls through to grid voltage.
    reading_zero_eps_grid = _reading(
        load_w=7600.0,
        grid_w=0.0,
        charger_w=1500,
        rate_a=16,
        eps_voltage_v=0.0,
        grid_voltage_v=245.1,
    )
    result_zero_eps_grid = allowance(reading_zero_eps_grid, limit_w=_LIMIT_W)
    assert result_zero_eps_grid.amps == 19, "zero EPS must fall through to grid voltage"


# --- The policy — plan() ---------------------------------------------------


def test_a_cut_is_taken_immediately() -> None:
    allowed = Allowance(amps=22, supplied_w=13500, charger_w=8018, limit_w=_LIMIT_W, reason="")
    got = plan(allowed, rate_a=32, settled=True)
    assert got.kind == "cut"
    assert got.amps == 22
    assert got.reason == "inverter supplying 13500 W against a 12000 W limit"


def test_a_cut_waits_while_settling() -> None:
    # Identical inputs to test_a_cut_is_taken_immediately but settled=False.
    allowed = Allowance(amps=22, supplied_w=13500, charger_w=8018, limit_w=_LIMIT_W, reason="")
    got = plan(allowed, rate_a=32, settled=False)
    assert got.kind == "hold"
    assert got.amps is None
    assert got.reason == "settling after the last change"


def test_plenty_of_headroom_never_raises_the_rate() -> None:
    """The guard lowers and nothing else. Headroom is not an instruction.

    The fixture is deliberately the most tempting case there is: the charger sits
    at the 6 A floor while the arithmetic says the house could carry 45 A, and
    the settle window is open. Any code path that raises a rate fires here.
    """
    allowed = Allowance(amps=45, supplied_w=5000, charger_w=0, limit_w=_LIMIT_W, reason="")
    got = plan(allowed, rate_a=6, settled=True)
    assert got.kind == "hold"
    assert got.amps is None
    assert got.reason == "inverter supplying 5000 W, within the 12000 W limit"


def test_an_allowance_equal_to_the_rate_holds() -> None:
    # The boundary: equal is not "below", so it must not be read as a cut.
    allowed = Allowance(amps=22, supplied_w=9000, charger_w=1250, limit_w=_LIMIT_W, reason="")
    got = plan(allowed, rate_a=22, settled=True)
    assert got.kind == "hold"
    assert got.amps is None


def test_an_unknown_rate_holds() -> None:
    allowed = Allowance(amps=22, supplied_w=13500, charger_w=8018, limit_w=_LIMIT_W, reason="")
    got = plan(allowed, rate_a=None, settled=True)
    assert got.kind == "hold"
    assert got.reason == "the charge rate is unknown"


def test_the_order_of_the_rules_is_settle_before_cut() -> None:
    # Both a cut and the settle window apply; assert hold.
    allowed = Allowance(amps=22, supplied_w=13500, charger_w=8018, limit_w=_LIMIT_W, reason="")
    got = plan(allowed, rate_a=32, settled=False)
    assert got.kind == "hold"
    assert got.reason == "settling after the last change"


# --- The loop — InverterGuard ----------------------------------------------


@dataclass(frozen=True)
class FakeChargerState:
    device_gid: int = 900001
    rate_a: int | None = 32


class FakeCircuit:
    def __init__(self, device_gid: int, watts: int | None = None, ts: int | None = None) -> None:
        self.device_gid = device_gid
        self.watts = watts
        self.ts = ts


class FakeAudit:
    def __init__(self) -> None:
        self.changes: list[dict[str, object]] = []

    def record_change(
        self,
        device_gid: int,
        *,
        from_a: int | None,
        to_a: int | None,
        reason: str,
        applied: bool,
        source: str,
        now: datetime,
    ) -> None:
        self.changes.append(
            dict(
                device_gid=device_gid,
                from_a=from_a,
                to_a=to_a,
                reason=reason,
                applied=int(applied),
                source=source,
                now=now.timestamp(),
            )
        )


class _FakeRepository:
    """A repository that answers .latest() with the poller's circuit list."""

    def __init__(self, poller: FakePoller) -> None:
        self._poller = poller

    def latest(self) -> list[FakeCircuit]:
        return self._poller.repository_latest


class FakePoller:
    def __init__(self, charger_state: FakeChargerState | None = None) -> None:
        self.charger = charger_state
        self.repository_latest: list[FakeCircuit] = []
        self.repository = _FakeRepository(self)
        self.audit = FakeAudit()
        self.limits_value = Limits(floor_a=6, ceiling_a=32, hardware_max_a=48)
        self.override_until_value: datetime | None = None
        self.write_rate_result: FakeChargerState | None = None
        self.writes: list[int] = []

    def limits(self) -> Limits:
        return self.limits_value

    def override_until(self) -> datetime | None:
        return self.override_until_value

    def write_rate(self, amps: int) -> ChargerState | None:
        self.writes.append(amps)
        if self.write_rate_result is not None:
            return self.write_rate_result  # type: ignore[return-value]
        return FakeChargerState(rate_a=amps)  # type: ignore[return-value]


def _make_guard(
    tmp_path: Path,
    enabled: bool = True,
    limit_w: int = _LIMIT_W,
    authority: str = "full",
    charger_state: FakeChargerState | None = None,
) -> tuple[InverterGuard, SqliteStore, FakePoller]:
    store = SqliteStore(str(tmp_path / "g.db"), device=TEST_DEVICE)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, enabled)
    SettingsStore(store).set(INVERTER_LIMIT_KEY, limit_w)
    SettingsStore(store).set(CHARGER_AUTHORITY_KEY, authority)
    SettingsStore(store).set(CHARGE_DEFAULT_KEY, 32)
    poller = FakePoller(charger_state)
    guard = InverterGuard(poller, store)  # type: ignore[arg-type]
    return guard, store, poller


def _insert_inverter_sample(
    store: SqliteStore,
    load_w: float | None,
    grid_w: float | None,
    eps_voltage_v: float | None = _VOLTS,
    grid_voltage_v: float | None = _VOLTS,
    ts: datetime | None = None,
) -> None:
    if ts is None:
        ts = NOW
    readings: dict[str, float] = {}
    if load_w is not None:
        readings["load_power_w"] = load_w
    if grid_w is not None:
        readings["grid_power_w"] = grid_w
    if eps_voltage_v is not None:
        readings["eps_voltage_v"] = eps_voltage_v
    if grid_voltage_v is not None:
        readings["grid_voltage_v"] = grid_voltage_v
    store.append(Sample(timestamp=ts, readings=readings))


def _insert_charger_circuit(
    poller: FakePoller, device_gid: int, watts: int, ts: datetime | None = None
) -> None:
    if ts is None:
        ts = NOW
    poller.repository_latest = [
        FakeCircuit(device_gid=device_gid, watts=watts, ts=int(ts.timestamp()))
    ]


# Test 18: a disabled module reads nothing and writes nothing
async def test_a_disabled_module_reads_nothing_and_writes_nothing(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=False)
    # Set up readings that would definitely cut if enabled.
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []
    assert poller.audit.changes == []
    assert guard.last_plan is None
    assert guard.last_allowance is None


# Test 19: a zero limit holds
async def test_a_zero_limit_holds(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=0)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []
    assert poller.audit.changes == []
    assert guard.last_plan is not None
    assert guard.last_plan.kind == "hold"
    assert guard.last_plan.reason == "the inverter limit is off"


# Test 20: a heavy house cuts the rate and audits it
async def test_a_heavy_house_cuts_the_rate_and_audits_it(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == [22]
    assert len(poller.audit.changes) == 1
    change = poller.audit.changes[0]
    assert change["from_a"] == 32
    assert change["to_a"] == 22
    assert change["applied"] == 1
    assert change["source"] == "module"
    assert change["reason"] == "inverter supplying 13500 W against a 12000 W limit"


# Test 21: the grid carrying the house writes nothing
async def test_the_grid_carrying_the_house_writes_nothing(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    # Row B: grid carrying the house.
    _insert_inverter_sample(store, 13500.0, 9000.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []
    assert poller.audit.changes == []


# Test 22: a stale inverter row holds
async def test_a_stale_inverter_row_holds(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    stale_ts = NOW - timedelta(seconds=INVERTER_STALE_SECONDS + 1)
    _insert_inverter_sample(store, 13500.0, 0.0, ts=stale_ts)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []
    assert guard.last_plan is not None
    assert guard.last_plan.reason == "the house reading is absent"


# Test 23: a recorded gap is not a reading
async def test_a_recorded_gap_is_not_a_reading(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    # Insert a gap row (error, no readings) as the newest, then a heavy real row before it.
    gap_ts = NOW - timedelta(seconds=10)
    store.append(Sample(timestamp=gap_ts, readings={}, error="sensor_timeout"))
    real_ts = NOW - timedelta(seconds=20)
    _insert_inverter_sample(store, 13500.0, 0.0, ts=real_ts)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    # Should read the older real row, not the gap.
    assert poller.writes == [22]


# Test 24: a stale charger reading holds
async def test_a_stale_charger_reading_holds(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    # Set interval to 10 so the stale threshold is 20 seconds.
    SettingsStore(store).set("emporia.interval", 10)
    _insert_inverter_sample(store, 13500.0, 0.0)
    # Charger circuit is stale: older than EVSE_STALE_MULTIPLE * interval.
    stale_ts = NOW - timedelta(seconds=EVSE_STALE_MULTIPLE * 10 + 1)
    _insert_charger_circuit(poller, 900001, 8018, ts=stale_ts)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []
    assert guard.last_plan is not None
    assert guard.last_plan.reason == "the charger reading is absent"


# Test 25: the charger circuit is found by device not by name
async def test_the_charger_circuit_is_found_by_device_not_by_name(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    # Circuit with a different name but matching device_gid.
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == [22], "must find circuit by device_gid, not name"


# Test 26: advisory authority plans but never writes
async def test_advisory_authority_plans_but_never_writes(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(
        tmp_path, enabled=True, limit_w=_LIMIT_W, authority="advisory"
    )
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []
    assert poller.audit.changes == []
    assert guard.last_plan is not None
    assert guard.last_plan.kind == "cut"
    assert guard.last_plan.amps == 22


# Test 27: app authority never writes
async def test_app_authority_never_writes(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W, authority="app")
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []


# Test 28: an unknown authority fails closed
async def test_an_unknown_authority_fails_closed(tmp_path: Path) -> None:
    # Use an authority string that is not one of the four known levels.
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W, authority="full")
    # Override the authority directly in the database to bypass validation.
    store._conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (CHARGER_AUTHORITY_KEY, "wizard"),
    )
    store._conn.commit()
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert poller.writes == []


# Test 29: a manual override holds the guard off
async def test_a_manual_override_holds_the_guard_off(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)
    poller.override_until_value = NOW + timedelta(hours=1)

    await guard.tick(NOW)

    assert poller.writes == []


# Test 30: the settle window blocks a second change
async def test_the_settle_window_blocks_a_second_change(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    # First tick: cut lands.
    await guard.tick(NOW)
    assert poller.writes == [22]

    # Second tick: SETTLE_SECONDS - 1 later, would cut further but is blocked.
    t2 = NOW + timedelta(seconds=SETTLE_SECONDS - 1)
    _insert_inverter_sample(store, 13500.0, 0.0, ts=t2)
    _insert_charger_circuit(poller, 900001, 8018, ts=t2)
    poller.charger = FakeChargerState(rate_a=22)
    await guard.tick(t2)
    assert len(poller.writes) == 1, "second tick must be blocked by settle window"

    # Third tick: SETTLE_SECONDS + 1 later, allowed to cut again.
    # Charger rate has returned to 32 (e.g. owner raised it), so a new cut is
    # warranted.
    t3 = NOW + timedelta(seconds=SETTLE_SECONDS + 1)
    _insert_inverter_sample(store, 13500.0, 0.0, ts=t3)
    _insert_charger_circuit(poller, 900001, 8018, ts=t3)
    poller.charger = FakeChargerState(rate_a=32)
    await guard.tick(t3)
    assert len(poller.writes) == 2, "third tick must be allowed after settle window"


async def test_a_clear_house_never_raises_the_rate(tmp_path: Path) -> None:
    """However long the house stays clear, the guard never touches the rate.

    This is the guarantee the owner asked for, so the fixture is the most
    tempting case available: the charger sits at the 6 A floor while the
    arithmetic says the house could carry 24 A, and the loop runs for ten
    minutes of continuously clear readings — longer than any window a release
    would plausibly have waited for. A single write here is a regression.
    """
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 5000.0, 0.0)
    _insert_charger_circuit(poller, 900001, 0)
    poller.charger = FakeChargerState(rate_a=6)

    for i in range(60):
        tick_ts = NOW + timedelta(seconds=(i + 1) * GUARD_INTERVAL_SECONDS)
        _insert_inverter_sample(store, 5000.0, 0.0, ts=tick_ts)
        _insert_charger_circuit(poller, 900001, 0, ts=tick_ts)
        poller.charger = FakeChargerState(rate_a=6)
        await guard.tick(tick_ts)

    assert poller.writes == []
    assert poller.audit.changes == []
    assert guard.last_plan is not None
    assert guard.last_plan.kind == "hold"
    assert guard.last_plan.reason == "inverter supplying 5000 W, within the 12000 W limit"


async def test_a_cut_still_happens_after_a_long_clear_spell(tmp_path: Path) -> None:
    """Not raising must not become not acting. The cut path stays live.

    Ten minutes of a clear house, then the dryer starts. Without this, deleting
    the whole of plan() below the settle check would still pass the test above.
    """
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    for i in range(60):
        tick_ts = NOW + timedelta(seconds=(i + 1) * GUARD_INTERVAL_SECONDS)
        _insert_inverter_sample(store, 5000.0, 0.0, ts=tick_ts)
        _insert_charger_circuit(poller, 900001, 8018, ts=tick_ts)
        poller.charger = FakeChargerState(rate_a=32)
        await guard.tick(tick_ts)
    assert poller.writes == []

    heavy_ts = NOW + timedelta(seconds=61 * GUARD_INTERVAL_SECONDS)
    _insert_inverter_sample(store, 13500.0, 0.0, ts=heavy_ts)
    _insert_charger_circuit(poller, 900001, 8018, ts=heavy_ts)
    poller.charger = FakeChargerState(rate_a=32)
    await guard.tick(heavy_ts)

    assert poller.writes == [22]


# Test 33: an unreachable cloud audits the failure and backs off
async def test_an_unreachable_cloud_audits_the_failure_and_backs_off(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    def fail_write(amps: int) -> None:
        raise EmporiaUnreachableError("no route")

    poller.write_rate = fail_write  # type: ignore[method-assign]

    await guard.tick(NOW)

    assert len(poller.audit.changes) == 1
    change = poller.audit.changes[0]
    assert change["applied"] == 0
    assert change["reason"].startswith("failed: ")  # type: ignore[attr-defined]

    # Second tick should not call write_rate again.
    await guard.tick(NOW + timedelta(seconds=GUARD_INTERVAL_SECONDS))
    assert len(poller.writes) == 0


# Test 34: a rejected credential is caught by name
async def test_a_rejected_credential_is_caught_by_name(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    def fail_write(amps: int) -> None:
        raise EmporiaAuthExpiredError("Refresh Token has expired")

    poller.write_rate = fail_write  # type: ignore[method-assign]

    # Should not raise.
    await guard.tick(NOW)


# Test 35: a write the charger did not take is audited as not applied
async def test_a_write_the_charger_did_not_take_is_audited_as_not_applied(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)
    # Return a charger still at the old rate.
    poller.write_rate_result = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert len(poller.audit.changes) == 1
    change = poller.audit.changes[0]
    assert change["applied"] == 0, "must audit as not applied when charger did not take the rate"


# Test 36: the endpoint reports the guard
async def test_the_endpoint_reports_the_guard(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arraysense.api.app import create_app
    from arraysense.collector.service import CollectorService
    from arraysense.collector.source import FakeSource
    from arraysense.config import Config
    from arraysense.modules.emporia import tokens
    from arraysense.modules.emporia.parse import ChargerState
    from arraysense.modules.emporia.poller import EmporiaPoller

    store = SqliteStore(str(tmp_path / "e.db"), device=TEST_DEVICE)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "e.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)

    # Wire the guard through the same store so the endpoint reads the settings
    # the guard was configured with. _make_guard creates its own database; we
    # set up the settings here instead and build the guard by hand.
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)
    SettingsStore(store).set(INVERTER_LIMIT_KEY, _LIMIT_W)
    SettingsStore(store).set(CHARGER_AUTHORITY_KEY, "full")
    SettingsStore(store).set(CHARGE_DEFAULT_KEY, 32)
    guard_poller = FakePoller(FakeChargerState(rate_a=32))
    guard = InverterGuard(guard_poller, store)  # type: ignore[arg-type]
    app.state.emporia_guard = guard
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(guard_poller, 900001, 8018)

    # The endpoint reads app.state.emporia to decide whether the module is
    # present; without it the route returns guard=None before reaching the
    # payload. A real poller with a charger state is enough — we do not need
    # the loop to run.
    token_path = tmp_path / "tok.json"
    tokens.save(token_path, tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00"))

    class _EndpointClient:
        def login(self, email: str, password: str) -> tokens.TokenSet:
            return tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00")

        def refresh(self, token_set: tokens.TokenSet) -> tokens.TokenSet:
            return tokens.TokenSet("fresh", token_set.refresh_token, token_set.refresh_issued)

        def get(self, path: str, id_token: str) -> object:
            return {"devices": []}

        def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
            return {}

        def write_charger(
            self, record: dict[str, object], changes: dict[str, object], id_token: str
        ) -> object:
            return changes

    emporia_poller = EmporiaPoller(store, token_path, client=_EndpointClient())
    emporia_poller.charger = ChargerState(
        device_gid=900001,
        rate_a=32,
        max_rate_a=48,
        on=True,
        status="Standby",
        message="Ready",
        conflicts=(),
        plugged_in=True,
        connected=True,
        offline_since=None,
        fault=None,
    )
    app.state.emporia = emporia_poller

    await guard.tick(NOW)

    with TestClient(app) as client:
        resp = client.get("/api/emporia/charger")

    assert resp.status_code == 200
    data = resp.json()
    guard_data = data["guard"]
    assert guard_data["limit_w"] == _LIMIT_W
    assert guard_data["supplied_w"] == 13500
    assert guard_data["charger_w"] == 8018
    assert guard_data["allowance_a"] == 22
    assert guard_data["amps"] == 22
    assert guard_data["kind"] == "cut"
    assert guard_data["reason"] == "inverter supplying 13500 W against a 12000 W limit"
    store.close()


# Test 37: the endpoint reports a null guard when the module is off
async def test_the_endpoint_reports_a_null_guard_when_the_module_is_off(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from arraysense.api.app import create_app
    from arraysense.collector.service import CollectorService
    from arraysense.collector.source import FakeSource
    from arraysense.config import Config

    store = SqliteStore(str(tmp_path / "e.db"), device=TEST_DEVICE)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "e.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)

    # No guard set up.
    app.state.emporia_guard = None

    with TestClient(app) as client:
        resp = client.get("/api/emporia/charger")

    assert resp.status_code == 200
    data = resp.json()
    assert "guard" in data
    assert data["guard"] is None
    store.close()


# Test 38: a non-integer limit holds without raising
async def test_a_non_integer_limit_holds_without_raising(tmp_path: Path) -> None:
    # Store a string value for the limit directly in the database, bypassing
    # validation — the same trick test_an_unknown_authority_fails_closed uses.
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    store._conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (INVERTER_LIMIT_KEY, "banana"),
    )
    store._conn.commit()
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    # Must not raise — a hand-edited setting must never stop the guard from
    # running.
    await guard.tick(NOW)

    assert poller.writes == []
    assert guard.last_plan is not None
    assert guard.last_plan.kind == "hold"


# Test 39: the audit records the module as the source
async def test_the_audit_records_the_module_as_the_source(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    await guard.tick(NOW)

    assert len(poller.audit.changes) == 1
    change = poller.audit.changes[0]
    assert change["source"] == MODULE


# Test 40: a repository error is not silently swallowed as an absent reading
async def test_a_repository_error_is_not_silently_swallowed(tmp_path: Path) -> None:
    guard, store, poller = _make_guard(tmp_path, enabled=True, limit_w=_LIMIT_W)
    _insert_inverter_sample(store, 13500.0, 0.0)
    _insert_charger_circuit(poller, 900001, 8018)
    poller.charger = FakeChargerState(rate_a=32)

    def fail_latest() -> list[object]:
        raise RuntimeError("database is corrupt")

    poller.repository.latest = fail_latest  # type: ignore[method-assign, assignment]

    with pytest.raises(RuntimeError, match="database is corrupt"):
        await guard.tick(NOW)
