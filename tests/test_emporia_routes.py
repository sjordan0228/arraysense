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
from datetime import UTC, datetime, timedelta
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
from arraysense.modules.emporia.parse import (
    ChargerState,
    Circuit,
    Reading,
    connections_from_status,
)
from arraysense.modules.emporia.poller import EmporiaPoller
from arraysense.settings import (
    CHARGE_CEILING_KEY,
    CHARGE_OVERRIDE_UNTIL_KEY,
    CHARGER_AUTHORITY_KEY,
    EMPORIA_ENABLED_KEY,
    HIGH_USAGE_WATTS_KEY,
    SettingsStore,
)
from arraysense.store.rollup import rebuild_circuit_hourly
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


@pytest.fixture
def client_without_emporia(tmp_path: Path) -> Any:
    app, store, _ = _app(tmp_path, with_poller=False)
    with TestClient(app) as c:
        yield c
    store.close()


# --- circuit history ------------------------------------------------------

# The instant every history window below ends at. Fixed rather than read from
# the clock because the route reads no clock either: every bound it works from
# arrives in the request or comes out of the store, so a fixed END makes the
# arithmetic checkable instead of approximate.
END = datetime(2026, 8, 16, 18, 0, tzinfo=UTC)

# How far behind END the newest inverter counter reading sits on a live-shaped
# request. The dongle read takes 12-17 s and the range on screen ends at the
# wall clock, so the house is never known quite up to the end of the window.
INVERTER_LAG = timedelta(seconds=12)

LOAD_COUNTER = "load_energy_total_kwh"


def _seed_counters(store: SqliteStore, newest: datetime, hours: float = 7.0) -> None:
    """Write the house's lifetime counter back from ``newest``, 0.1 kWh a minute.

    A minute apart because that is the clock the driver reads the energy
    registers on, and 0.1 kWh is the finest step the metric's scale of ten can
    hold — anything smaller would not survive the round trip through storage.

    Seven hours because the longest window asked for below is six, and a
    counter read is only knowable where readings bracket both of its bounds:
    three hours of history answers a one-hour question and blanks a six-hour
    one, which looks exactly like the endpoint being broken.
    """
    steps = int(hours * 60)
    for step in range(steps + 1):
        store.append(
            Sample(
                timestamp=newest - timedelta(minutes=step),
                readings={LOAD_COUNTER: 1000.0 + 0.1 * (steps - step)},
            )
        )


def _seed_circuits(app: Any, newest: datetime, minutes: int = 60) -> None:
    """A dryer and a mains channel, both reporting every minute back from ``newest``.

    Counted in whole minutes rather than in hours because the span check turns
    on exactly how many readings landed, and a fraction of an hour multiplied
    back out lands one reading either side of what was asked for.
    """
    repo = app.state.emporia.repository
    repo.sync_circuits(
        [
            Circuit(100000, "5", "Dryer", 1.0, "circuit"),
            Circuit(100000, "1", "Main panel", 1.0, "mains"),
        ],
        newest,
    )
    for step in range(minutes):
        when = newest - timedelta(minutes=step)
        repo.append_readings([Reading(100000, "5", 1000), Reading(100000, "1", 4000)], when)


def _app_with_history(
    tmp_path: Path, *, counters_newest: datetime | None = END - INVERTER_LAG
) -> tuple[Any, SqliteStore]:
    """An app whose circuits report to just before END, with the house behind them.

    ``counters_newest`` is what each test varies: a poll behind END is the live
    shape, hours behind it is an inverter that went silent, and None is one that
    has never reported an energy counter at all.
    """
    app, store, _ = _app(tmp_path)
    _seed_circuits(app, END - timedelta(seconds=30))
    if counters_newest is not None:
        _seed_counters(store, counters_newest)
    return app, store


def _history(client: TestClient, hours: float, **params: Any) -> dict[str, Any]:
    """Ask for the circuit history of the ``hours`` before END, as the page does."""
    response = client.get(
        "/api/emporia/history",
        params={
            "start": (END - timedelta(hours=hours)).isoformat(),
            "end": END.isoformat(),
            **params,
        },
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_history_excludes_mains_from_the_ranking_and_the_coverage(tmp_path: Path) -> None:
    # A monitor's mains channel is the sum of the circuits beside it. Counting
    # it as a part doubles every house — alerts.NOT_A_CULPRIT already names it
    # and this reads that set rather than keeping a second copy.
    app, store = _app_with_history(tmp_path)
    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    assert "mains" not in {circuit["kind"] for circuit in body["circuits"]}
    # The dryer alone: 1 kW held across sixty one-minute readings is 1 kWh. The
    # mains channel was reading 4 kW at the same instants and contributes none
    # of it.
    assert body["coverage"]["circuits_kwh"] == pytest.approx(1.0)


def test_history_reports_the_tier_it_answered_from(tmp_path: Path) -> None:
    # An hourly average of a pump that runs four minutes an hour is a true
    # number and a misleading shape. The page names the tier so the reader is
    # not told a shape it did not draw.
    app, store = _app_with_history(tmp_path)
    with TestClient(app) as c:
        day = _history(c, hours=24)
        week = _history(c, hours=24 * 7)
    store.close()

    assert day["tier"] == "full"
    assert week["tier"] == "hourly"


def test_history_says_what_fraction_of_the_house_the_circuits_cover(tmp_path: Path) -> None:
    # Five bars read as the whole house without this. The fraction is computed
    # from energy, never from minutes watched — coverage in minutes is a
    # different question and money depends on the second (#23).
    #
    # Asserted as a real number, not "None or in range". A tolerant assertion
    # here is what would have let a permanently null coverage pass as though the
    # feature worked.
    app, store = _app_with_history(tmp_path)
    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    coverage = body["coverage"]
    # The counter climbs 0.1 kWh a minute, so the hour before END holds 6 kWh —
    # measured to the last reading the inverter actually sent, twelve seconds
    # before the end of the window.
    assert coverage["house_kwh"] == pytest.approx(6.0)
    assert coverage["circuits_kwh"] == pytest.approx(1.0)
    assert coverage["fraction"] == pytest.approx(1.0 / 6.0, abs=0.01)


def test_coverage_is_reported_for_a_window_ending_now(tmp_path: Path) -> None:
    # The regression this guards is silent and total: _covered cannot bracket a
    # bound with no reading after it, so an unclamped window ending at the wall
    # clock blanks this line on every live request, for ever.
    #
    # The circuits are seeded across the whole window on purpose. This is about
    # the *house* side of the clamp, and a window the module only recorded part
    # of withholds the share for its own reason — which would leave this passing
    # or failing for something other than what it is here to watch.
    app, store, _ = _app(tmp_path)
    _seed_circuits(app, END - timedelta(seconds=30), minutes=6 * 60)
    _seed_counters(store, END - INVERTER_LAG)
    with TestClient(app) as c:
        body = _history(c, hours=6)
    store.close()

    assert body["coverage"]["fraction"] is not None


def test_coverage_goes_unknown_when_the_inverter_has_been_silent(tmp_path: Path) -> None:
    # The last inverter reading is hours old while circuits kept reporting.
    # Clamping to it would divide a full window of circuits by a partial window
    # of house and read well over 100%. Unknown is the truthful answer.
    app, store = _app_with_history(tmp_path, counters_newest=END - timedelta(hours=3))
    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    assert body["coverage"]["house_kwh"] is None
    assert body["coverage"]["fraction"] is None


def test_history_reports_a_fraction_above_one_rather_than_capping_it(tmp_path: Path) -> None:
    # A part cannot exceed the whole, so a fraction above one is not a coverage
    # figure at all — it is a fault saying so: a mains channel that escaped the
    # exclusion, or a multiplier set for the wrong circuit. Capping it to 1.0
    # would render every one of those as perfect coverage, which is the single
    # reading guaranteed to be wrong, and would hide the fault for as long as it
    # lasted. The page decides how to draw a disagreement; the endpoint's job is
    # to report the arithmetic it actually did.
    app, store, _ = _app(tmp_path)
    newest = END - timedelta(seconds=30)
    repo = app.state.emporia.repository
    repo.sync_circuits([Circuit(100000, "5", "Dryer", 1.0, "circuit")], newest)
    # 30 kW held for an hour is 30 kWh, against a house counter climbing 6.0 over
    # the same hour — the arithmetic of a multiplier set an order of magnitude
    # too high, which is exactly what this figure should expose.
    for step in range(60):
        repo.append_readings([Reading(100000, "5", 30000)], newest - timedelta(minutes=step))
    _seed_counters(store, END - INVERTER_LAG)

    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    assert body["coverage"]["house_kwh"] == pytest.approx(6.0)
    assert body["coverage"]["fraction"] > 1.0


def test_history_withholds_the_share_when_the_circuits_recorded_part_of_the_window(
    tmp_path: Path,
) -> None:
    # The check docs/api.md has promised since this endpoint shipped and only
    # the browser was making. _coverage_end bounds the house side; nothing
    # bounded the circuits', so an hour of circuit readings inside a six-hour
    # window was divided by six hours of house counter and reported as a share.
    # Every figure in it correct, and the sentence it produced — "monitored
    # circuits cover 3% of the house" — invites the reader to conclude the house
    # is barely monitored when the truth is the module was not running.
    app, store = _app_with_history(tmp_path)
    with TestClient(app) as c:
        body = _history(c, hours=6)
    store.close()

    coverage = body["coverage"]
    assert coverage["circuits_kwh"] == pytest.approx(1.0), "an hour of dryer really was recorded"
    assert coverage["house_kwh"] is not None, "and the house counter really did report"
    assert coverage["fraction"] is None, "but the two do not describe the same span"
    assert coverage["spans_match"] is False
    assert coverage["recorded_seconds"] == 60 * 60
    assert coverage["window_seconds"] == 6 * 3600


@pytest.mark.parametrize(
    ("minutes", "matches"),
    [
        (60, True),
        # One poll lost at each edge of the shortest range the page offers.
        (58, True),
        # Exactly ninety per cent: the threshold is a floor, not an exclusive bound.
        (54, True),
        (53, False),
        (20, False),
    ],
)
def test_the_span_threshold_clears_ordinary_operation_and_catches_a_gappy_window(
    tmp_path: Path, minutes: int, matches: bool
) -> None:
    # Ninety per cent of the window, and the number has to clear ordinary
    # operation or the qualification fires on every healthy installation and so
    # means nothing. A healthy one loses at most one poll at each edge, which is
    # two minutes of the shortest range the page offers. Below the threshold the
    # share would be understated by more than a tenth of its own value, which is
    # more than a percentage rounded to whole numbers can absorb.
    app, store, _ = _app(tmp_path)
    _seed_circuits(app, END - timedelta(seconds=30), minutes=minutes)
    _seed_counters(store, END - INVERTER_LAG)
    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    assert body["coverage"]["spans_match"] is matches
    assert (body["coverage"]["fraction"] is not None) is matches


def test_history_reports_the_span_it_measured_the_check_against(tmp_path: Path) -> None:
    # The page says how long the circuits recorded for, and it has to be the
    # figure the endpoint decided on rather than one measured again in the
    # browser — the browser's own count credited every hourly bucket holding
    # anything with a full 3,600 seconds and so could never find a window short.
    app, store = _app_with_history(tmp_path)
    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    coverage = body["coverage"]
    assert coverage["window_seconds"] == 3600
    assert coverage["recorded_seconds"] == 3600
    assert coverage["spans_match"] is True
    assert coverage["fraction"] is not None


def test_history_leaves_the_fraction_null_when_the_house_is_unknown(tmp_path: Path) -> None:
    # A house figure the inverter did not report is not a house drawing nothing.
    # A fraction computed against zero would read as full coverage, which is the
    # exact inversion of the truth.
    app, store = _app_with_history(tmp_path, counters_newest=None)
    with TestClient(app) as c:
        body = _history(c, hours=1)
    store.close()

    assert body["coverage"]["circuits_kwh"] == pytest.approx(1.0)
    assert body["coverage"]["house_kwh"] is None
    assert body["coverage"]["fraction"] is None


def test_history_can_be_narrowed_to_named_circuits(tmp_path: Path) -> None:
    # The reference account has thirty-nine circuits and the page draws five.
    # Fetching all of them to discard thirty-four is what this argument avoids.
    app, store = _app_with_history(tmp_path)
    ids = app.state.emporia.repository._ids()
    with TestClient(app) as c:
        body = _history(c, hours=1, ids=str(ids[(100000, "5")]))
    store.close()

    assert [circuit["name"] for circuit in body["circuits"]] == ["Dryer"]


def test_history_refuses_an_unparseable_circuit_id(client: TestClient) -> None:
    # Dropping the bad entry would return four strips where five were asked for,
    # and the page has no way to notice it was given a narrower answer.
    response = client.get(
        "/api/emporia/history",
        params={"start": "2026-08-16T00:00:00Z", "end": "2026-08-16T06:00:00Z", "ids": "3,oops"},
    )
    assert response.status_code == 400


def test_history_with_the_module_off_answers_empty_rather_than_erroring(
    client_without_emporia: TestClient,
) -> None:
    # The tab is gated on the module, but a stale bookmark must not 500.
    response = client_without_emporia.get(
        "/api/emporia/history",
        params={"start": "2026-08-16T00:00:00Z", "end": "2026-08-16T06:00:00Z"},
    )
    assert response.status_code == 200
    assert response.json()["circuits"] == []


def test_history_rejects_a_backwards_range(client: TestClient) -> None:
    response = client.get(
        "/api/emporia/history",
        params={"start": "2026-08-16T06:00:00Z", "end": "2026-08-16T00:00:00Z"},
    )
    assert response.status_code == 400


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


def test_the_circuits_endpoint_says_which_devices_stopped_answering(tmp_path: Path) -> None:
    # The gap this closes. Two of the reference account's outlets have been
    # offline since April and August, and the page could only draw them as a
    # dash — indistinguishable from a circuit that happened to be idle. Emporia
    # knows the difference and says so; this carries it through.
    app, store, _ = _app(tmp_path)
    now = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)
    app.state.emporia.repository.sync_circuits(
        [
            Circuit(100000, "5", "Dryer", 2.0, "circuit"),
            Circuit(100001, "1,2,3", "Shed outlet", 1.0, "outlet"),
        ],
        now,
    )
    app.state.emporia.connections = connections_from_status(
        {
            "devicesConnected": [
                {"deviceGid": 100000, "connected": True, "offlineSince": None},
                {
                    "deviceGid": 100001,
                    "connected": False,
                    "offlineSince": "since Apr 2, 2026, 5:06 PM",
                },
            ]
        }
    )
    with TestClient(app) as c:
        rows = {r["name"]: r for r in c.get("/api/emporia/circuits").json()["circuits"]}

    assert rows["Dryer"]["connected"] is True
    assert rows["Dryer"]["offline_since"] is None
    assert rows["Shed outlet"]["connected"] is False
    assert rows["Shed outlet"]["offline_since"] == "since Apr 2, 2026, 5:06 PM"
    store.close()


def test_the_live_list_and_the_history_endpoint_agree_on_circuit_ids(tmp_path: Path) -> None:
    # The whole reason an id was added to the live list: a row on /emporia
    # links to /graphs#circuits=<id>, and the link is worthless unless the
    # circuits endpoint answers to the same id for the same circuit. Every row
    # carries an id, and it is the surrogate history() already keys its series
    # on — never a name, which sync_circuits updates in place on a rename.
    app, store = _app_with_history(tmp_path)
    with TestClient(app) as c:
        live = c.get("/api/emporia/circuits").json()["circuits"]
        history = _history(c, hours=1)
    store.close()

    assert live, "the seeded circuits should be listed"
    assert all(isinstance(row["id"], int) for row in live), "every circuit carries an id"
    live_ids = {row["name"]: row["id"] for row in live}
    history_ids = {row["name"]: row["id"] for row in history["circuits"]}
    assert history_ids, "the history endpoint should report the dryer circuit"
    for name, circuit_id in history_ids.items():
        assert live_ids[name] == circuit_id, f"the live list and history disagree about {name}'s id"


def test_a_circuit_emporia_said_nothing_about_is_not_reported_as_online(tmp_path: Path) -> None:
    # Silence is not health. A device missing from devicesConnected has not been
    # declared up, and the page must be able to say nothing rather than "fine".
    app, store, _ = _app(tmp_path)
    app.state.emporia.repository.sync_circuits(
        [Circuit(100000, "5", "Dryer", 2.0, "circuit")],
        datetime(2026, 8, 15, 12, 0, tzinfo=UTC),
    )
    with TestClient(app) as c:
        rows = c.get("/api/emporia/circuits").json()["circuits"]
    assert rows[0]["connected"] is None
    assert rows[0]["offline_since"] is None
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


# --- costs/circuits ---------------------------------------------------------

# These live here rather than in test_api.py because the Costs page's circuit
# ranking is only reachable with a running poller: the route answers empty for
# a build with no Emporia module before it ever touches a circuit row, so
# exercising the ranked path needs the poller test_api.py's own client fixture
# does not carry. The tariff-only paths (no tariff, no module) are covered
# there instead, against a fixture that already matches their shape.

SPEND_HOUR = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)

# Flat, so the arithmetic under every test here is the wattage times the rate
# and the band split is trivially one entry — the band-splitting arithmetic
# itself belongs to test_emporia_repository.py's own band_kwh tests, not to a
# route test whose job is wiring, not pricing.
FLAT_TARIFF = {"tariff.bands": "Flat | 0.20 | 00:00-24:00"}


def _seed_spend_circuits(app: Any, store: SqliteStore, hour: datetime) -> None:
    """Three circuits and a mains channel, an hour of one-minute readings each,
    then the real hourly rollup — so the route is tested against a
    circuit_hourly row built the way a production one is, not one typed into
    the table by hand.

    The wattages are chosen so cost order and arrival order disagree with each
    other in nothing — Dryer, Oven and Fridge rank the same way by energy as by
    money under a flat rate — which is enough to prove the route ranks by the
    figure ``top_spenders`` returns rather than by the order ``sync_circuits``
    was called in.
    """
    repo = app.state.emporia.repository
    repo.sync_circuits(
        [
            Circuit(100000, "1", "Dryer", 1.0, "circuit"),
            Circuit(100000, "2", "Oven", 1.0, "circuit"),
            Circuit(100000, "3", "Fridge", 1.0, "circuit"),
            Circuit(100000, "4", "Main panel", 1.0, "mains"),
        ],
        hour,
    )
    for minute in range(60):
        when = hour + timedelta(minutes=minute)
        repo.append_readings(
            [
                Reading(100000, "1", 3000),
                Reading(100000, "2", 2000),
                Reading(100000, "3", 500),
                # The mains total of the three circuits above it, so a test
                # that failed to exclude it would see it outrank every one of
                # them rather than merely appear among them.
                Reading(100000, "4", 5500),
            ],
            when,
        )
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=60,
    )


def _spend_app(tmp_path: Path) -> tuple[Any, SqliteStore]:
    app, store, _ = _app(tmp_path)
    _seed_spend_circuits(app, store, SPEND_HOUR)
    return app, store


def _spend_window() -> dict[str, str]:
    return {
        "start": SPEND_HOUR.isoformat(),
        "end": (SPEND_HOUR + timedelta(hours=1)).isoformat(),
    }


def test_costs_circuits_names_the_dearest_first(tmp_path: Path) -> None:
    """Ranked by money, both figures reported, and the band split beside them."""
    app, store = _spend_app(tmp_path)
    with TestClient(app) as c:
        c.put("/api/settings", json=FLAT_TARIFF)
        response = c.get("/api/costs/circuits", params=_spend_window())
    store.close()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["configured"] is True
    names = [entry["name"] for entry in body["circuits"]]
    assert names == ["Dryer", "Oven", "Fridge"]
    assert names == sorted(
        names, key=lambda n: -next(e["cost"] for e in body["circuits"] if e["name"] == n)
    )
    assert all("bands" in entry for entry in body["circuits"])


def test_costs_circuits_never_names_mains(tmp_path: Path) -> None:
    # A monitor's mains channel is the sum of the circuits beside it, and the
    # readings above set it higher than any one of them — the strongest check
    # that exclusion, not merely low rank, is what keeps it off the list.
    app, store = _spend_app(tmp_path)
    with TestClient(app) as c:
        c.put("/api/settings", json=FLAT_TARIFF)
        body = c.get("/api/costs/circuits", params=_spend_window()).json()
    store.close()

    assert all(entry["kind"] != "mains" for entry in body["circuits"])


def test_costs_circuits_says_how_much_of_the_month_it_accounts_for(tmp_path: Path) -> None:
    """Beside the list, not in a footnote, and measured in energy rather than in
    minutes watched — money depends on the second."""
    app, store = _spend_app(tmp_path)
    with TestClient(app) as c:
        c.put("/api/settings", json=FLAT_TARIFF)
        body = c.get("/api/costs/circuits", params=_spend_window()).json()
    store.close()

    assert set(body["coverage"]) == {
        "circuits_kwh",
        "house_kwh",
        "fraction",
        "recorded_seconds",
        "window_seconds",
        "spans_match",
    }
    # The three named circuits: 3 + 2 + 0.5 kWh over the hour, with the mains
    # channel that would double it excluded.
    assert body["coverage"]["circuits_kwh"] == pytest.approx(5.5)


def test_costs_circuits_converts_an_aware_bound_to_the_owners_zone(tmp_path: Path) -> None:
    """``with_zone`` attaches a zone to a naive bound and does nothing at all
    to an aware one -- the month picker sends aware instants, and
    ``bands_in_effect`` has to read the *converted* calendar or it disagrees
    with ``band_intervals``, which does convert (via ``costs._local``).

    Chicago runs behind UTC, so 9pm on 31 October local has already rolled
    over to 1 November in UTC. Read without converting, ``bands_in_effect``
    would price the hour against a winter-only band list while the energy is
    correctly keyed "summer" by ``band_kwh`` -- two names that do not match,
    which is a sharper failure than the mispriced-window trap CLAUDE.md
    describes: a mismatch severe enough that every circuit prices as unknown
    rather than merely at the wrong rate.
    """
    app, store, _ = _app(tmp_path)
    hour = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # 21:00 CDT on 31 October -- still autumn
    _seed_spend_circuits(app, store, hour)
    with TestClient(app) as c:
        c.put(
            "/api/settings",
            json={
                "tariff.bands": (
                    "Summer | 0.30 | 00:00-24:00 | May-Oct; Winter | 0.10 | 00:00-24:00 | Nov-Apr"
                )
            },
        )
        response = c.get(
            "/api/costs/circuits",
            params={
                "start": hour.isoformat(),
                "end": (hour + timedelta(hours=1)).isoformat(),
                "tz": "America/Chicago",
            },
        )
    store.close()

    assert response.status_code == 200, response.text
    body = response.json()
    dryer = next(entry for entry in body["circuits"] if entry["name"] == "Dryer")
    assert dryer["cost"] is not None, (
        "the hour priced as unknown -- bands_in_effect read the UTC calendar, "
        "which had already turned over to November, rather than the Chicago one"
    )
    assert dryer["cost"] == pytest.approx(3.0 * 0.30)


def test_costs_circuits_does_not_flag_partial_for_a_band_the_window_never_reached(
    tmp_path: Path,
) -> None:
    """Finding 6. ``bands_in_effect`` only excludes a band that is out of
    season -- it says nothing about whether the requested window's clock ever
    reached the band's hours -- so a current-month request ending at 11:00,
    asked against a tariff with a 15:00-20:00 peak, used to get peak back
    anyway. ``top_spenders`` then read "never reported in peak" as a band
    that went unmeasured rather than one that had not happened yet, and
    marked every circuit's total partial for it. Selecting from the band
    names ``band_intervals`` actually named for this window -- the same
    narrowing ``costs.price_period`` already does from a split period's own
    energy -- answers the season question band_intervals already applied
    internally and the not-yet-occurred one together.
    """
    app, store = _spend_app(tmp_path)
    with TestClient(app) as c:
        c.put(
            "/api/settings",
            json={
                "tariff.bands": (
                    "Peak | 0.40 | 15:00-20:00; Off-peak | 0.10 | 00:00-15:00,20:00-24:00"
                )
            },
        )
        # _spend_window is the one recorded hour, 10:00-11:00 -- entirely
        # before the peak band's 15:00 start.
        body = c.get("/api/costs/circuits", params=_spend_window()).json()
    store.close()

    dryer = next(entry for entry in body["circuits"] if entry["name"] == "Dryer")
    assert {b["band"] for b in dryer["bands"]} == {"Off-peak"}, (
        "peak was never in this window at all -- it should not appear as a band "
        "the circuit was expected to report in"
    )
    assert dryer["partial"] is False, (
        "a band that has not happened yet is not the same claim as one that "
        "went unmeasured, and only the second should mark the row partial"
    )


def test_a_rounding_noise_house_reading_is_treated_as_no_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``house_kwh`` is rounded to three places for display below; "greater
    than zero" alone lets a real but tiny positive figure — the same scale of
    rounding noise the hours either side of sunrise produce on the efficiency
    chart — divide a real circuits_kwh out to a percentage nobody could check
    against the 0.000 the same response prints.

    ``_coverage_end`` and ``counter_kwh`` are stubbed rather than seeded
    through the store: the real counter's storage scale of ten cannot hold a
    delta this small at all, so a sub-floor house_kwh can only ever arise from
    interpolation across a boundary, and asserting the guard should not also
    depend on reproducing that interpolation exactly.
    """
    from arraysense.api import routes

    end = datetime(2026, 8, 16, 18, 0, tzinfo=UTC)
    start = end - timedelta(hours=1)
    monkeypatch.setattr(routes, "_coverage_end", lambda *a, **k: end)
    monkeypatch.setattr(routes, "counter_kwh", lambda *a, **k: 0.0002)
    store = SqliteStore(str(tmp_path / "cov.db"), device=TEST_DEVICE)

    coverage = routes._circuit_coverage(
        store,
        start,
        end,
        circuits_kwh=0.05,
        recorded_seconds=3600,
        window_seconds=3600,
    )
    store.close()

    assert coverage["house_kwh"] == 0.0, "0.0002 rounds to 0.000 at the displayed precision"
    assert coverage["fraction"] is None, (
        "a percentage was printed against a denominator that reads as no reading at all"
    )


def test_costs_circuits_refuses_a_period_too_long_to_price(tmp_path: Path) -> None:
    """band_intervals raises past MAX_SCAN_DAYS; that has to be a 400, not a 500.
    A month never reaches it, which is exactly why nothing else would catch a
    regression here.

    No circuits are seeded for this one — the period is refused before the
    route ever reaches a circuit row, and a poller with nothing to report is
    the whole point: this is the calendar guard, not the ranking.
    """
    app, store, _ = _app(tmp_path)
    with TestClient(app) as c:
        c.put("/api/settings", json=FLAT_TARIFF)
        response = c.get(
            "/api/costs/circuits",
            params={"start": "2026-01-01T00:00:00Z", "end": "2026-06-01T00:00:00Z"},
        )
    store.close()

    assert response.status_code == 400
    assert "days" in response.json()["detail"]


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
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGE_CEILING_KEY, 32)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
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
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
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
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
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
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
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
    assert "body.enabled" in entry.group(0), "and on the module actually being switched on"


def _plugged_in_charger(app: Any) -> Any:
    """A poller holding a charger, ready for a route to be pointed at it."""
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
    return poller


def test_a_rate_set_by_hand_is_recorded_as_the_owners(tmp_path: Path) -> None:
    # The load-bearing half of #202. The owner's write is applied — it really
    # did reach the charger — so ``applied`` alone cannot separate it from this
    # module's own work, and restore-on-startup undid a hand-set rate on the
    # strength of that equality.
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    poller = _plugged_in_charger(app)

    with TestClient(app) as c:
        assert c.post("/api/emporia/charger/rate", json={"amps": 16}).status_code == 200

    change = poller.audit.recent_changes()[0]
    assert change.applied is True, "it reached the charger"
    assert change.source == "owner", "but it was not this module's decision"
    assert poller.audit.last_applied_rate(900001) is None, "so restore has nothing of its own"
    store.close()


def test_the_override_opens_when_the_owner_presses_not_when_emporia_answers(
    tmp_path: Path,
) -> None:
    # The write is a round trip to a cloud service and the restore runs on the
    # poller's own clock, so opening the window afterwards left a gap in which
    # an automatic decision could find no hold and write over a rate the owner
    # was in the middle of setting. A write that fails holds too: they reached
    # for the charger either way.
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    poller = _plugged_in_charger(app)
    poller.client = _StubCharger(fail=EmporiaUnreachableError("no route"))

    with TestClient(app) as c:
        assert c.post("/api/emporia/charger/rate", json={"amps": 16}).status_code == 503

    held_until = settings.get(CHARGE_OVERRIDE_UNTIL_KEY)
    assert isinstance(held_until, int) and held_until > 0
    store.close()


def test_stopping_the_charger_by_hand_is_recorded_as_the_owners(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    poller = _plugged_in_charger(app)

    with TestClient(app) as c:
        assert c.post("/api/emporia/charger/power", json={"on": False}).status_code == 200

    assert poller.audit.recent_changes()[0].source == "owner"
    store.close()


def test_the_history_says_who_made_each_change(tmp_path: Path) -> None:
    # The page shows this list to answer "what has this service done to my car".
    # Without the source it cannot separate its own work from the owner's, which
    # is the same question restore-on-startup gets wrong without it.
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, True)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    _plugged_in_charger(app)

    with TestClient(app) as c:
        c.post("/api/emporia/charger/rate", json={"amps": 16})
        body = c.get("/api/emporia/charger").json()

    assert body["changes"][0]["source"] == "owner"
    store.close()


# --- a module that is switched off ----------------------------------------
#
# "Off" has to mean off. The poller kept the last charger it read for the life
# of the process, so a disabled module went on reporting a charger at a rate and
# the page drew live controls over it — controls whose press would have reached
# a real charger, because neither write route consults the enable either.


def test_a_disabled_module_serves_no_charger(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, False)
    _plugged_in_charger(app)

    with TestClient(app) as c:
        body = c.get("/api/emporia/charger").json()

    assert body["charger"] is None
    assert body["changes"] == []
    assert body["enabled"] is False
    store.close()


def test_a_disabled_module_refuses_to_set_a_rate(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, False)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    poller = _plugged_in_charger(app)

    with TestClient(app) as c:
        response = c.post("/api/emporia/charger/rate", json={"amps": 16})

    assert response.status_code == 409
    assert "switched off" in response.json()["detail"]
    assert poller.client.writes == [], "nothing reached the charger"
    store.close()


def test_a_disabled_module_says_why_rather_than_that_it_has_no_charger(tmp_path: Path) -> None:
    # A tick clears the cached charger the moment the module goes off, so by
    # the time most presses land there is no charger to find and the honest
    # answer — "no Emporia charger is being read" — is the unhelpful one.
    # Somebody who has just switched the module off is owed the reason.
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, False)

    with TestClient(app) as c:
        response = c.post("/api/emporia/charger/rate", json={"amps": 16})

    assert response.status_code == 409
    assert "switched off" in response.json()["detail"]
    store.close()


def test_a_disabled_module_refuses_to_stop_the_charger(tmp_path: Path) -> None:
    app, store, _ = _app(tmp_path)
    settings = SettingsStore(store)
    settings.set(EMPORIA_ENABLED_KEY, False)
    settings.set(CHARGER_AUTHORITY_KEY, "full")
    poller = _plugged_in_charger(app)

    with TestClient(app) as c:
        response = c.post("/api/emporia/charger/power", json={"on": False})

    assert response.status_code == 409
    assert poller.audit.recent_changes() == [], "nothing reached the charger"
    store.close()


def test_a_charger_the_app_manages_refuses_a_rate_rather_than_ignoring_it(
    tmp_path: Path,
) -> None:
    # The default. A control that takes a number and silently does nothing with
    # it is worse than one that says why it will not — and the page hides the
    # control entirely, so this is the belt behind the braces.
    app, store, _ = _app(tmp_path)
    SettingsStore(store).set(EMPORIA_ENABLED_KEY, True)
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
