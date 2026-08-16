"""test_emporia_poller.py — the poller's clock, its enable, and how it fails.

The two that matter most are the first and the last pair. A disabled module
must make no call and write no row, because that is the whole promise of an
optional module. And an unreachable Emporia must not read as a rejected
credential: one clears itself, the other needs the owner, and conflating them
is the defect that makes a flaky link look like being logged out.

Nothing here touches the network or needs an account — the client is a fake
answering the two GETs a tick makes.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arraysense.modules.emporia import tokens
from arraysense.modules.emporia.client import EmporiaAuthExpiredError, EmporiaUnreachableError
from arraysense.modules.emporia.poller import EmporiaPoller
from arraysense.modules.emporia.repository import ChargerAudit
from arraysense.settings import (
    CHARGE_DEFAULT_KEY,
    CHARGER_AUTHORITY_KEY,
    EMPORIA_ENABLED_KEY,
    SettingsStore,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)

DEVICES = {
    "devices": [
        {
            "deviceGid": 100000,
            "model": "VUE002",
            "channels": [{"channelNum": "5", "name": "Dryer", "channelMultiplier": 2.0}],
            "devices": [],
        }
    ]
}
USAGE = {
    "deviceListUsages": {
        "devices": [
            {
                "deviceGid": 100000,
                "channelUsages": [
                    {"deviceGid": 100000, "channelNum": "5", "usage": 0.05, "nestedDevices": []}
                ],
            }
        ]
    }
}


class FakeClient:
    """Answers the two GETs the poller makes, and can be told to fail."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.gets: list[str] = []
        # Every rate this was asked to write. Empty is the expected state in
        # almost every test here: the module's default authority writes nothing.
        self.writes: list[int] = []
        self.changes: list[dict[str, object]] = []

    def login(self, email: str, password: str) -> tokens.TokenSet:
        if self.fail is not None:
            raise self.fail
        return tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00")

    def refresh(self, token_set: tokens.TokenSet) -> tokens.TokenSet:
        if self.fail is not None:
            raise self.fail
        return tokens.TokenSet("fresh-id", token_set.refresh_token, token_set.refresh_issued)

    def get(self, path: str, id_token: str) -> object:
        if self.fail is not None:
            raise self.fail
        self.gets.append(path)
        return DEVICES if path.startswith("/customers/devices") else USAGE

    def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
        return self.write_charger(record, {"chargingRate": amps}, id_token)

    def write_charger(
        self, record: dict[str, object], changes: dict[str, object], id_token: str
    ) -> object:
        rate = changes.get("chargingRate")
        if isinstance(rate, int):
            self.writes.append(rate)
        self.changes.append(dict(changes))
        return dict(changes)


def _poller(tmp_path: Path, client: FakeClient) -> tuple[EmporiaPoller, SqliteStore, Path]:
    store = SqliteStore(str(tmp_path / "p.db"), device=TEST_DEVICE)
    token_path = tmp_path / "tok.json"
    tokens.save(token_path, tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00"))
    return EmporiaPoller(store, token_path, client=client), store, token_path


async def test_a_disabled_module_makes_no_calls_and_writes_nothing(tmp_path: Path) -> None:
    # The promise of an optional module, tested rather than asserted.
    client = FakeClient()
    poller, store, _ = _poller(tmp_path, client)
    await poller.tick(NOW)
    assert client.gets == []
    assert poller.state.status == "off"
    assert poller.repository.latest() == []
    store.close()


async def test_an_enabled_tick_records_the_owners_circuit(tmp_path: Path) -> None:
    client = FakeClient()
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)

    latest = poller.repository.latest()
    assert [c.name for c in latest] == ["Dryer"]
    assert latest[0].watts == 6000, "0.05 kWh/min is 3 kW, doubled by the 240 V multiplier"
    assert poller.state.status == "ok"
    store.close()


async def test_an_unreachable_emporia_is_transient_and_keeps_the_token(tmp_path: Path) -> None:
    client = FakeClient(fail=EmporiaUnreachableError("no route"))
    poller, store, token_path = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)

    assert poller.state.status == "unreachable"
    assert tokens.load(token_path) is not None, (
        "a network failure must never discard the credential"
    )
    store.close()


async def test_a_rejected_credential_is_its_own_state(tmp_path: Path) -> None:
    # The failure the reference implementation gets wrong. It must not read as a
    # network problem, and it must not silently retry forever.
    client = FakeClient(fail=EmporiaAuthExpiredError("Refresh Token has expired"))
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)

    assert poller.state.status == "reconnect_required"
    store.close()


async def test_a_tick_without_a_token_asks_for_a_login_rather_than_failing(
    tmp_path: Path,
) -> None:
    store = SqliteStore(str(tmp_path / "p.db"), device=TEST_DEVICE)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)
    poller = EmporiaPoller(store, tmp_path / "absent.json", client=FakeClient())

    await poller.tick(NOW)

    assert poller.state.status == "reconnect_required"
    store.close()


async def test_the_loop_starts_and_stops_cleanly(tmp_path: Path) -> None:
    poller, store, _ = _poller(tmp_path, FakeClient())
    await poller.start()
    await asyncio.sleep(0)
    await poller.stop()
    store.close()


async def test_the_device_list_is_not_re_read_on_every_tick(tmp_path: Path) -> None:
    # Names change about never, and a device list costs a call against a quota
    # nobody can see. Re-reading it every minute would spend 1,440 calls a day
    # on a question whose answer changes twice a year.
    client = FakeClient()
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)
    await poller.tick(NOW)

    assert client.gets.count("/customers/devices") == 1
    assert len([p for p in client.gets if "getDeviceListUsages" in p]) == 2
    store.close()


async def test_a_reading_survives_being_disabled_and_re_enabled(tmp_path: Path) -> None:
    # Turning the module off stops the poller, and must not take the history
    # with it: the circuits and their readings are the owner's data.
    client = FakeClient()
    poller, store, _ = _poller(tmp_path, client)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    await poller.tick(NOW)

    settings.set(EMPORIA_ENABLED_KEY, False)
    await poller.tick(NOW)

    assert poller.state.status == "off"
    assert [c.name for c in poller.repository.latest()] == ["Dryer"]
    store.close()


async def test_the_usage_request_names_the_devices_it_is_asking_about(tmp_path: Path) -> None:
    # Measured against the real API: without deviceGids the usage endpoint
    # answers HTTP 400, "Could not get attribute 'deviceGids' from input". A
    # tick that omits them reads nothing at all, so this is the request being
    # well formed rather than a preference about query strings.
    client = FakeClient()
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)

    usage = [p for p in client.gets if "getDeviceListUsages" in p]
    assert usage, "no usage request was made"
    assert "deviceGids=100000" in usage[0]


async def test_later_ticks_keep_naming_the_devices_without_re_reading_the_list(
    tmp_path: Path,
) -> None:
    # The device list is read daily; the usage call happens every interval. The
    # identifiers have to outlive the call that discovered them, or every tick
    # after the first asks about nothing.
    client = FakeClient()
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)
    await poller.tick(NOW)

    usage = [p for p in client.gets if "getDeviceListUsages" in p]
    assert len(usage) == 2
    assert "deviceGids=100000" in usage[1]
    assert client.gets.count("/customers/devices") == 1


# --- the charger ----------------------------------------------------------

CHARGER_STATUS = {
    "evChargers": [
        {
            "deviceGid": 900001,
            "loadGid": 900002,
            "message": "Ready",
            "status": "Standby",
            "chargerOn": True,
            "chargingRate": 6,
            "maxChargingRate": 48,
            "loadManagementEnabled": False,
        }
    ],
    "loads": [{"loadGid": 900002, "schedulesEnabled": True}],
    "devicesConnected": [{"deviceGid": 900001, "connected": True, "offlineSince": None}],
}


class ChargerClient(FakeClient):
    """A FakeClient that also answers the status endpoint."""

    def get(self, path: str, id_token: str) -> object:
        if path.startswith("/customers/devices/status"):
            self.gets.append(path)
            return CHARGER_STATUS
        return super().get(path, id_token)

    def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
        self.writes.append(amps)
        return {"chargingRate": amps}


async def test_a_tick_reads_the_charger(tmp_path: Path) -> None:
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)

    assert poller.charger is not None
    assert poller.charger.rate_a == 6
    assert poller.charger.conflicts == ("schedules",)
    store.close()


async def test_a_tick_reads_which_devices_are_still_answering(tmp_path: Path) -> None:
    # Held on the poller rather than stored, exactly like the charger: it is
    # what Emporia says right now, and a stale copy of it would tell somebody a
    # device was offline hours after it came back.
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)

    await poller.tick(NOW)

    assert poller.connections[900001].connected is True
    store.close()


async def test_advisory_authority_never_writes_to_the_charger(tmp_path: Path) -> None:
    # The default, and the reason it is the default: nothing this module decides
    # reaches a car until somebody has watched it decide.
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    poller.audit.record_change(900001, from_a=32, to_a=6, reason="test", applied=True, now=NOW)

    await poller.tick(NOW)

    assert client.writes == []
    store.close()


async def test_a_rate_this_service_set_is_put_back_when_it_may_write(tmp_path: Path) -> None:
    # The behaviour the whole stage exists for. The service threw a car down to
    # 6 A, died, and came back with no reason to hold it there.
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "limited")
    poller.audit.record_change(900001, from_a=32, to_a=6, reason="test", applied=True, now=NOW)

    await poller.tick(NOW)

    assert client.writes == [32], "back to the configured default"
    assert poller.audit.recent_changes()[0].reason.startswith("restored")
    store.close()


async def test_a_rate_somebody_else_set_is_left_where_they_put_it(tmp_path: Path) -> None:
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "limited")
    # This service last set 20 A; the charger is sitting at 6 A, so somebody
    # moved it by hand.
    poller.audit.record_change(900001, from_a=32, to_a=20, reason="test", applied=True, now=NOW)

    await poller.tick(NOW)

    assert client.writes == []
    store.close()


async def test_the_restore_is_attempted_once_and_not_every_minute(tmp_path: Path) -> None:
    # It is a startup behaviour. Repeating it every tick would fight the owner
    # for the slider all afternoon.
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "limited")
    poller.audit.record_change(900001, from_a=32, to_a=6, reason="test", applied=True, now=NOW)

    await poller.tick(NOW)
    await poller.tick(NOW)

    assert client.writes == [32]
    store.close()


async def test_a_restart_does_not_write_the_same_proposal_down_again(tmp_path: Path) -> None:
    # The restore is considered once per process, so a service that restarts
    # nightly and may not write recorded the identical proposal every night for
    # ever. The audit is read to answer "what has this service done to my car",
    # and a hundred copies of one sentence is a page that answers nothing.
    client = ChargerClient()
    store = SqliteStore(str(tmp_path / "p.db"), device=TEST_DEVICE)
    token_path = tmp_path / "tok.json"
    tokens.save(token_path, tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00"))
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "advisory")
    ChargerAudit(store).record_change(
        900001, from_a=32, to_a=6, reason="test", applied=True, now=NOW
    )

    first = EmporiaPoller(store, token_path, client=client)
    await first.tick(NOW)
    second = EmporiaPoller(store, token_path, client=client)
    await second.tick(NOW + timedelta(days=1))

    assert client.writes == [], "advisory writes nothing, which is why it repeats"
    proposals = [c for c in second.audit.recent_changes() if c.reason.startswith("restored")]
    assert len(proposals) == 1
    store.close()


async def test_a_proposal_that_has_changed_is_written_down(tmp_path: Path) -> None:
    # The rule is "not the same one twice", not "only ever once". A different
    # proposal is news, and skipping it would be the module going quiet about a
    # decision it actually made.
    client = ChargerClient()
    store = SqliteStore(str(tmp_path / "p.db"), device=TEST_DEVICE)
    token_path = tmp_path / "tok.json"
    tokens.save(token_path, tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00"))
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "advisory")
    ChargerAudit(store).record_change(
        900001, from_a=32, to_a=6, reason="test", applied=True, now=NOW
    )

    first = EmporiaPoller(store, token_path, client=client)
    await first.tick(NOW)
    settings.set(CHARGE_DEFAULT_KEY, 24)
    second = EmporiaPoller(store, token_path, client=client)
    await second.tick(NOW + timedelta(days=1))

    proposals = [c for c in second.audit.recent_changes() if c.reason.startswith("restored")]
    assert [c.to_a for c in proposals] == [24, 32]
    store.close()


async def test_the_charger_is_the_apps_until_the_owner_says_otherwise(tmp_path: Path) -> None:
    # The default, and the point of it: installing this service is not the same
    # as asking it to take over somebody's car charger. Full authority is not
    # enough on its own.
    client = ChargerClient()
    poller, store, _ = _poller(tmp_path, client)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "app")
    poller.audit.record_change(900001, from_a=32, to_a=6, reason="test", applied=True, now=NOW)

    await poller.tick(NOW)

    assert client.writes == []
    assert "Emporia app" in poller.audit.recent_changes()[0].reason
    store.close()
