"""test_emporia_routes.py — what the Emporia routes report, and what they refuse.

Three properties, and the third is the one worth the file. A build that never
started the module answers "off" rather than raising, so an installation that
has not enabled it still serves a page. Writes sit behind the password while
reads stay open, because the wall display is not logged in. And no route ever
puts the credential in a response — a token that reaches a page reaches a
screenshot.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arraysense.api.app import create_app
from arraysense.auth import set_password
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.models import Sample
from arraysense.modules.emporia import tokens
from arraysense.modules.emporia.client import EmporiaUnreachableError
from arraysense.modules.emporia.parse import ChargerState, Circuit, Reading
from arraysense.modules.emporia.poller import EmporiaPoller
from arraysense.settings import (
    CHARGE_CEILING_KEY,
    CHARGE_OVERRIDE_UNTIL_KEY,
    CHARGER_AUTHORITY_KEY,
    EMPORIA_ENABLED_KEY,
    HIGH_USAGE_WATTS_KEY,
    SettingsStore,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

PASSWORD = "correct horse"

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


class _StubClientBase:
    """The reads every stub answers the same way."""

    def login(self, email: str, password: str) -> tokens.TokenSet:
        return tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00")

    def refresh(self, token_set: tokens.TokenSet) -> tokens.TokenSet:
        return tokens.TokenSet("fresh", token_set.refresh_token, token_set.refresh_issued)

    def get(self, path: str, id_token: str) -> object:
        return DEVICES if path.startswith("/customers/devices") else USAGE

    def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
        raise AssertionError("this stub was not expected to write")

    def write_charger(
        self, record: dict[str, object], changes: dict[str, object], id_token: str
    ) -> object:
        raise AssertionError("this stub was not expected to write")


class _StubCharger(_StubClientBase):
    """A charger that accepts writes, or refuses them on demand."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.writes: list[int] = []
        self.rate = 6
        self.on = True

    def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
        return self.write_charger(record, {"chargingRate": amps}, id_token)

    def write_charger(
        self, record: dict[str, object], changes: dict[str, object], id_token: str
    ) -> object:
        if self.fail is not None:
            raise self.fail
        rate = changes.get("chargingRate")
        if isinstance(rate, int):
            self.writes.append(rate)
            self.rate = rate
        on = changes.get("chargerOn")
        if isinstance(on, bool):
            self.on = on
        return dict(changes)

    def get(self, path: str, id_token: str) -> object:
        # The read-back after a write: the charger now reports what it took.
        if path.startswith("/customers/devices/status"):
            return {
                "evChargers": [
                    {
                        "deviceGid": 900001,
                        "chargingRate": self.rate,
                        "maxChargingRate": 48,
                        "chargerOn": self.on,
                        "status": "Charging",
                        "message": "Charging",
                    }
                ],
                "loads": [],
            }
        return super().get(path, id_token)


class _StubClient(_StubClientBase):
    """An Emporia that always answers, so the routes can be exercised offline."""


def _app(tmp_path: Path, with_poller: bool = True) -> tuple[Any, SqliteStore, Path]:
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
    token_path = tmp_path / "tok.json"
    if with_poller:
        app.state.emporia = EmporiaPoller(store, token_path)
    return app, store, token_path


@pytest.fixture
def client(tmp_path: Path) -> Any:
    app, store, _ = _app(tmp_path)
    with TestClient(app) as c:
        yield c
    store.close()


@pytest.fixture
def client_with_password(tmp_path: Path) -> Any:
    app, store, _ = _app(tmp_path)
    set_password(SettingsStore(store), PASSWORD)
    with TestClient(app) as c:
        yield c
    store.close()


def test_status_says_off_before_anybody_enables_it(client: TestClient) -> None:
    body = client.get("/api/emporia/status").json()
    assert body["status"] == "off"
    assert body["enabled"] is False


def test_status_answers_off_rather_than_failing_without_the_module(tmp_path: Path) -> None:
    # A build that never started the poller must still serve the page rather
    # than 500 at it. This is what "an installation that never enables it is
    # indistinguishable from one built before it existed" means at the API.
    app, store, _ = _app(tmp_path, with_poller=False)
    with TestClient(app) as c:
        assert c.get("/api/emporia/status").json()["status"] == "off"
        assert c.get("/api/emporia/circuits").json()["circuits"] == []
    store.close()


def test_circuits_are_empty_rather_than_absent_when_nothing_is_stored(
    client: TestClient,
) -> None:
    body = client.get("/api/emporia/circuits").json()
    assert body["circuits"] == []


def test_login_requires_write_permission(client_with_password: TestClient) -> None:
    # It stores a credential and changes what the service does. Reads stay open
    # for the wall display; this is not a read.
    response = client_with_password.post(
        "/api/emporia/login", json={"email": "a@example.invalid", "password": "pw"}
    )
    assert response.status_code in (401, 403)


def test_disconnect_requires_write_permission(client_with_password: TestClient) -> None:
    assert client_with_password.post("/api/emporia/disconnect").status_code in (401, 403)


def test_reading_the_circuits_stays_open_to_the_wall_display(
    client_with_password: TestClient,
) -> None:
    assert client_with_password.get("/api/emporia/status").status_code == 200
    assert client_with_password.get("/api/emporia/circuits").status_code == 200


def test_disconnect_forgets_the_token_and_says_it_did_not_revoke(tmp_path: Path) -> None:
    # Saying "revoked" while only forgetting would be worse than the truth: a
    # leaked copy would keep working for thirty days while the page claimed it
    # was dead. Revocation is Stage 3, after somebody tests it against the pool.
    app, store, token_path = _app(tmp_path)
    tokens.save(token_path, tokens.TokenSet("id", "refresh", "2026-08-15T00:00:00+00:00"))
    with TestClient(app) as c:
        body = c.post("/api/emporia/disconnect").json()
    assert body == {"ok": True, "revoked": False}
    assert tokens.load(token_path) is None
    store.close()


def test_no_route_ever_returns_the_token(client: TestClient) -> None:
    # A credential that reaches a page reaches a screenshot.
    for path in ("/api/emporia/status", "/api/emporia/circuits"):
        text = client.get(path).text
        assert "refresh_token" not in text
        assert "authtoken" not in text


def test_the_page_is_served(client: TestClient) -> None:
    response = client.get("/emporia")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_page_declares_no_external_resources() -> None:
    # The service runs on home networks that may have no route to the internet,
    # which is why uPlot is vendored. A page reaching a CDN would be blank there.
    import arraysense

    html = (Path(arraysense.__file__).parent / "web" / "emporia.html").read_text()
    assert "https://" not in html.split("</style>")[0], "no external stylesheet or font"
    assert "cdn." not in html


def test_a_successful_login_updates_the_state_before_it_answers(tmp_path: Path) -> None:
    # What made a working login look like a rejected one: the page reads the
    # poller's state, the poller ticks once a minute, so for up to a minute
    # after a good login the page still said "your login has expired" with the
    # form standing open. Somebody watching that types their password again —
    # which is exactly what happened on the bench.
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)
    poller = app.state.emporia
    poller.client = _StubClient()

    # Put the poller in the state a first-time owner actually meets: enabled,
    # with no credential saved, so it is asking to be logged in.
    asyncio.run(poller.tick(datetime(2026, 8, 15, 12, 0, tzinfo=UTC)))
    assert poller.state.status == "reconnect_required"

    with TestClient(app) as c:
        response = c.post(
            "/api/emporia/login", json={"email": "a@example.invalid", "password": "pw"}
        )
        assert response.status_code == 200
        body = c.get("/api/emporia/status").json()

    assert body["status"] != "reconnect_required", (
        "a login that worked must not still be asking for one"
    )
    assert body["status"] == "ok"
    store.close()


def test_the_login_form_can_actually_be_hidden() -> None:
    # A display rule beats the browser's own [hidden], so a form styled with
    # flex and toggled with `hidden` never goes away — it sat under
    # "Connected." on a real account, inviting a password nobody needed.
    # common.js guards .nowstrip, .view, .p and .stale the same way. Asserted
    # on the text because the gates cannot open a browser, and the alternative
    # is finding out on the hardware again.
    import arraysense

    html = (Path(arraysense.__file__).parent / "web" / "emporia.html").read_text()
    assert "form.login[hidden]{display:none}" in html


def test_the_circuits_endpoint_passes_the_category_through(tmp_path: Path) -> None:
    # The page chooses an icon from this. Left out of the payload it could not.
    app, store, _ = _app(tmp_path)
    app.state.emporia.repository.sync_circuits(
        [Circuit(100000, "8", "air conditioner main", 2.0, "circuit", type_gid=1)],
        datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )
    with TestClient(app) as c:
        rows = c.get("/api/emporia/circuits").json()["circuits"]
    assert rows[0]["type_gid"] == 1
    store.close()


def test_no_alert_until_a_threshold_is_set(tmp_path: Path) -> None:
    # Off is the default, and off must mean the payload carries nothing rather
    # than an object saying "not alerting" that a page could misread.
    app, store, _ = _app(tmp_path)
    with TestClient(app) as c:
        assert c.get("/api/live").json()["alert"] is None
    store.close()


def test_the_alert_fires_from_the_inverter_and_names_the_circuits(tmp_path: Path) -> None:
    # Both halves at once: the verdict comes from load_power_w, the names come
    # from Emporia, and the two arrive in one response so a wall display polls
    # once.
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(HIGH_USAGE_WATTS_KEY, 5000)
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            readings={"load_power_w": 9000.0},
        )
    )
    app.state.emporia.repository.sync_circuits(
        [
            Circuit(100000, "8", "air conditioner main", 1.0, "circuit", type_gid=1),
            Circuit(100000, "1", "Main panel", 1.0, "mains"),
        ],
        datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )
    app.state.emporia.repository.append_readings(
        [Reading(100000, "8", 4000), Reading(100000, "1", 9000)],
        datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )

    with TestClient(app) as c:
        alert = c.get("/api/live").json()["alert"]

    assert alert is not None
    assert alert["load_w"] == 9000
    assert [c["name"] for c in alert["contributors"]] == ["air conditioner main"]
    assert alert["accounted_w"] == 4000
    assert alert["complete"] is False, "4 kW of a 9 kW house is not the explanation"
    store.close()


def test_the_alert_fires_without_the_module_and_names_nobody(tmp_path: Path) -> None:
    # The promise that the module is optional even for the feature it was asked
    # for. No Emporia, no attribution, but the house still gets its warning.
    app, store, _ = _app(tmp_path, with_poller=False)
    SettingsStore(store).set(HIGH_USAGE_WATTS_KEY, 5000)
    store.append(
        Sample(
            timestamp=datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
            readings={"load_power_w": 9000.0},
        )
    )
    with TestClient(app) as c:
        alert = c.get("/api/live").json()["alert"]
    assert alert is not None
    assert alert["contributors"] == []
    assert alert["accounted_w"] is None
    store.close()


# --- the charger ----------------------------------------------------------


def test_the_charger_reads_as_absent_when_none_has_been_seen(client: TestClient) -> None:
    body = client.get("/api/emporia/charger").json()
    assert body["charger"] is None
    assert body["changes"] == []


def test_setting_a_rate_needs_write_permission(client_with_password: TestClient) -> None:
    # It changes a physical thing that keeps its value for ever. If anything on
    # this service sits behind the password, this does.
    response = client_with_password.post("/api/emporia/charger/rate", json={"amps": 16})
    assert response.status_code in (401, 403)


def test_setting_a_rate_clamps_it_audits_it_and_starts_the_override(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(CHARGE_CEILING_KEY, 32)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    SettingsStore(store).set(CHARGER_AUTHORITY_KEY, "full")
    poller = app.state.emporia
    poller.client = _StubCharger()
    poller.charger = ChargerState(
        device_gid=900001,
        rate_a=6,
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
    poller._charger_record = {"deviceGid": 900001, "chargingRate": 6}
    poller._id_token = "id-1"

    with TestClient(app) as c:
        body = c.post("/api/emporia/charger/rate", json={"amps": 40}).json()

    assert body["rate_a"] == 32, "held at the ceiling"
    assert "ceiling" in body["refused"]
    assert poller.client.writes == [32]
    change = poller.audit.recent_changes()[0]
    assert change.applied is True
    assert change.from_a == 6 and change.to_a == 32
    held_until = settings.get(CHARGE_OVERRIDE_UNTIL_KEY)
    assert isinstance(held_until, int) and held_until > 0, "the owner's hand holds for a while"
    store.close()


def test_a_write_that_emporia_refuses_is_audited_as_not_applied(tmp_path: Path) -> None:
    # The worst thing this could do is record a change that never happened: the
    # restore then believes the charger is at a rate it is not, and leaves it.
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(CHARGER_AUTHORITY_KEY, "full")
    poller = app.state.emporia
    poller.client = _StubCharger(fail=EmporiaUnreachableError("no route"))
    poller.charger = ChargerState(
        device_gid=900001,
        rate_a=6,
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
    poller._charger_record = {"deviceGid": 900001, "chargingRate": 6}
    poller._id_token = "id-1"

    with TestClient(app) as c:
        assert c.post("/api/emporia/charger/rate", json={"amps": 16}).status_code == 503

    change = poller.audit.recent_changes()[0]
    assert change.applied is False
    assert poller.audit.last_applied_rate(900001) is None
    store.close()


def test_stopping_and_starting_the_charger_is_audited(tmp_path: Path) -> None:
    # Heavier than setting a rate: a low rate charges a car slowly, a charger
    # switched off charges it not at all. "Why is the car not charged" has to
    # have an answer, and this is where it comes from.
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(CHARGER_AUTHORITY_KEY, "full")
    poller = app.state.emporia
    poller.client = _StubCharger()
    poller.charger = ChargerState(
        device_gid=900001,
        rate_a=16,
        max_rate_a=48,
        on=True,
        status="Charging",
        message="Charging",
        conflicts=(),
        plugged_in=True,
        connected=True,
        offline_since=None,
        fault=None,
    )
    poller._charger_record = {"deviceGid": 900001, "chargingRate": 16, "chargerOn": True}
    poller._id_token = "id-1"

    with TestClient(app) as c:
        stopped = c.post("/api/emporia/charger/power", json={"on": False}).json()
        started = c.post("/api/emporia/charger/power", json={"on": True}).json()

    assert stopped["confirmed"] is True
    assert started["confirmed"] is True
    reasons = [change.reason for change in poller.audit.recent_changes()]
    assert "started charging" in reasons
    assert "stopped charging" in reasons
    store.close()


def test_a_rate_the_charger_does_not_take_is_not_audited_as_applied(tmp_path: Path) -> None:
    # The defect that made a working write look like a failed one, in reverse:
    # a 200 from Emporia is not the charger agreeing. If the read-back disagrees
    # the change is recorded as not applied, so restore never trusts a rate the
    # charger is not actually at.
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(CHARGER_AUTHORITY_KEY, "full")
    poller = app.state.emporia
    stub = _StubCharger()
    stub.rate = 6  # whatever is written, it keeps reporting 6 A
    poller.client = stub
    poller.charger = ChargerState(
        device_gid=900001,
        rate_a=6,
        max_rate_a=48,
        on=True,
        status="Charging",
        message="Charging",
        conflicts=(),
        plugged_in=True,
        connected=True,
        offline_since=None,
        fault=None,
    )
    poller._charger_record = {"deviceGid": 900001, "chargingRate": 6}
    poller._id_token = "id-1"

    def stubborn(record: dict[str, object], changes: dict[str, object], id_token: str) -> object:
        return dict(changes)  # accepted, but the charger never moves

    stub.write_charger = stubborn  # type: ignore[method-assign]

    with TestClient(app) as c:
        body = c.post("/api/emporia/charger/rate", json={"amps": 24}).json()

    assert body["confirmed"] is False
    change = poller.audit.recent_changes()[0]
    assert change.applied is False
    assert poller.audit.last_applied_rate(900001) is None
    store.close()


def test_the_charger_page_is_served(client: TestClient) -> None:
    response = client.get("/charger")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_charger_page_declares_no_external_resources() -> None:
    import arraysense

    html = (Path(arraysense.__file__).parent / "web" / "charger.html").read_text()
    assert "https://" not in html.split("</style>")[0]
    assert "cdn." not in html


def test_the_charger_tab_appears_only_where_there_is_a_charger() -> None:
    # Most people who switch this module on have no EV charger at all. A tab
    # leading to a page about hardware they do not own is the empty-card fault
    # one level up, which is what #12 was about.
    import re

    import arraysense

    common = (Path(arraysense.__file__).parent / "web" / "common.js").read_text()
    entry = re.search(r"key: 'charger'.*?\}", common, re.S)
    assert entry is not None, "the charger nav entry is missing"
    assert "/api/emporia/charger" in entry.group(0)
    assert "body.charger" in entry.group(0), "it must gate on a charger existing, not on a setting"


def test_a_charger_the_app_manages_refuses_a_rate_rather_than_ignoring_it(
    tmp_path: Path,
) -> None:
    # The default. A control that takes a number and silently does nothing with
    # it is worse than one that says why it will not — and the page hides the
    # control entirely, so this is the belt behind the braces.
    app, store, _ = _app(tmp_path)
    poller = app.state.emporia
    poller.client = _StubCharger()
    poller.charger = ChargerState(
        device_gid=900001,
        rate_a=6,
        max_rate_a=48,
        on=True,
        status="Standby",
        message="Ready",
        conflicts=(),
        plugged_in=False,
        connected=True,
        offline_since=None,
        fault=None,
    )
    poller._charger_record = {"deviceGid": 900001, "chargingRate": 6}
    poller._id_token = "id-1"

    with TestClient(app) as c:
        rate = c.post("/api/emporia/charger/rate", json={"amps": 16})
        power = c.post("/api/emporia/charger/power", json={"on": False})

    assert rate.status_code == 409
    assert "Emporia app" in rate.json()["detail"]
    assert power.status_code == 409
    assert poller.client.writes == [], "nothing reached the charger"
    store.close()


def test_the_page_decides_from_the_one_setting_that_exists(tmp_path: Path) -> None:
    # It briefly read a second setting that had been folded away, so a charger
    # the owner had just handed over still said the Emporia app had it. The page
    # and the API have to be reading the same field.
    import arraysense

    html = (Path(arraysense.__file__).parent / "web" / "charger.html").read_text()
    assert "c.managed_by" not in html, "that setting no longer exists"
    assert "c.authority !== 'app'" in html
