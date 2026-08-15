"""test_auth.py — the optional write guard, its endpoints, and the CLI recovery.

Authentication is off until somebody sets a password. The single most
important behaviour in this change is that a fresh install — no password — has
every write endpoint answering exactly as it did before this feature existed,
and the regression bar below asserts the real status codes, not merely
"not 401". Everything else in this file guards the credential, the sessions,
the isolation of the hash key from the settings API, and the CLI recovery.
"""

from __future__ import annotations

import hashlib
import logging
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arraysense import __main__ as main_module
from arraysense.api import routes
from arraysense.api.app import create_app
from arraysense.auth import (
    AUTH_PASSWORD_KEY,
    Sessions,
    hash_password,
    password_hash,
    password_is_set,
    set_password,
    verify_password,
)
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.settings import SettingsStore, describe
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

PASSWORD = "correct horse"
OTHER = "battery staple"


@pytest.fixture
def client(tmp_path: Path) -> Any:
    store = SqliteStore(str(tmp_path / "auth.db"), device=TEST_DEVICE)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "auth.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    service.status.running = True
    service.status.connected = True
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as c:
        yield c
    store.close()


def _settings(client: Any) -> SettingsStore:
    return SettingsStore(client.app.state.store)


def _toml(tmp_path: Path, db: Path) -> Path:
    """A minimal valid config pointing at ``db``, for the CLI tests."""
    path = tmp_path / "config.toml"
    path.write_text(
        'dongle_host = "192.0.2.10"\n'
        'dongle_serial = "BA12345678"\n'
        'inverter_serial = "CE12345678"\n'
        f'database_path = "{db}"\n'
    )
    return path


# --- the credential -----------------------------------------------------------


def test_hash_password_produces_a_fresh_salt_each_time() -> None:
    # A fixed salt would make every stored hash identical, so one stolen
    # settings table would hand over every installation's password at once.
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second
    assert verify_password(PASSWORD, first)
    assert verify_password(PASSWORD, second)


def test_verify_password_rejects_a_wrong_password() -> None:
    stored = hash_password(PASSWORD)
    assert not verify_password("not the password", stored)


def test_verify_password_returns_false_for_an_empty_stored_value() -> None:
    assert verify_password(PASSWORD, "") is False


def test_verify_password_returns_false_for_a_malformed_stored_value() -> None:
    # A hand edit or a previous buggy release can leave the value in any shape;
    # the answer is a refusal, not a traceback nobody can use.
    assert verify_password(PASSWORD, "not-a-valid-stored-hash") is False


def test_the_stored_form_round_trips_through_the_settings_store(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    stored = password_hash(_settings(client))
    assert stored is not None
    assert stored.startswith("scrypt$")
    assert verify_password(PASSWORD, stored)


# --- the guard ---------------------------------------------------------------

# The six write endpoints that carry the guard, named for the parametrised
# tests below. A seventh route added to the protected set later has to be
# added here too, or nothing will stop it being guarded.
_PROTECTED = (
    "settings",
    "setup/apply",
    "setup/detect",
    "yield",
    "resume",
    "efficiency/backfill",
)


def _hit(client: Any, endpoint: str, monkeypatch: Any) -> Any:
    """Send one valid request to a protected endpoint, stubbing its real work.

    Each request would otherwise reach hardware, schedule a SIGTERM, or fetch
    from the internet; the stubs stand in for exactly that. The request must be
    valid for the endpoint, because the regression bar asserts the real status
    a successful request gets, and a 400 from a malformed body would read as a
    pass.
    """
    if endpoint == "settings":
        return client.put("/api/settings", json={"display.temperature_unit": "C"})
    if endpoint == "setup/apply":
        monkeypatch.setattr(routes, "_schedule_restart", lambda: None)
        return client.post("/api/setup/apply", json={"model": "18kPV"})
    if endpoint == "setup/detect":

        async def _probe(body: Any) -> str:
            return "3352000000"

        monkeypatch.setattr(routes, "_probe_serial", _probe)
        return client.post(
            "/api/setup/detect",
            json={"transport": "modbus_serial", "serial_device": "/dev/rs485"},
        )
    if endpoint == "yield":
        return client.post("/api/yield", json={"seconds": 120})
    if endpoint == "resume":
        return client.post("/api/resume")
    if endpoint == "efficiency/backfill":
        client.put("/api/settings", json={"site.latitude": 33.0, "site.longitude": -97.0})
        monkeypatch.setattr(routes, "fetch_archive_hours", lambda *a, **k: [])
        return client.post(
            "/api/efficiency/backfill",
            json={"start": "2026-08-01", "end": "2026-08-01"},
        )
    raise AssertionError(f"no stub for endpoint {endpoint!r}")


@pytest.mark.parametrize("endpoint", _PROTECTED)
def test_the_six_protected_endpoints_are_untouched_with_no_password(
    client: Any, endpoint: str, monkeypatch: Any
) -> None:
    """The regression bar: with no password set, every protected endpoint
    answers exactly as it did before authentication existed.

    This is what "optional" means and it is the thing that must never break.
    The status asserted is the endpoint's real one — a fresh install must not
    so much as see a 401, let alone a wrong one.
    """
    assert not password_is_set(_settings(client))
    r = _hit(client, endpoint, monkeypatch)
    assert r.status_code == 200, f"{endpoint}: {r.status_code} {r.text}"


@pytest.mark.parametrize("endpoint", _PROTECTED)
def test_each_protected_endpoint_is_a_401_with_a_password_and_no_cookie(
    client: Any, endpoint: str, monkeypatch: Any
) -> None:
    set_password(_settings(client), PASSWORD)
    r = _hit(client, endpoint, monkeypatch)
    assert r.status_code == 401, f"{endpoint}: {r.status_code} {r.text}"


@pytest.mark.parametrize("endpoint", _PROTECTED)
def test_each_protected_endpoint_answers_normally_with_a_valid_session(
    client: Any, endpoint: str, monkeypatch: Any
) -> None:
    set_password(_settings(client), PASSWORD)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    r = _hit(client, endpoint, monkeypatch)
    assert r.status_code == 200, f"{endpoint}: {r.status_code} {r.text}"


def test_an_expired_session_is_refused(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    sessions = client.app.state.sessions
    token = sessions.issue()
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    sessions._expiry[digest] = time.time() - 1
    client.cookies.set("arraysense_session", token)
    assert client.put("/api/settings", json={"display.temperature_unit": "C"}).status_code == 401


def test_a_revoked_session_is_refused(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    token = client.app.state.sessions.issue()
    client.app.state.sessions.revoke(token)
    client.cookies.set("arraysense_session", token)
    assert client.put("/api/settings", json={"display.temperature_unit": "C"}).status_code == 401


def test_a_token_that_was_never_issued_is_refused(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    client.cookies.set("arraysense_session", "not-a-real-token")
    assert client.put("/api/settings", json={"display.temperature_unit": "C"}).status_code == 401


def test_reads_are_never_refused(client: Any) -> None:
    # The wall display only ever reads, so protecting reads would log it out on
    # every restart. Password or not, the reads stay open.
    set_password(_settings(client), PASSWORD)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/live").status_code == 200
    assert client.get("/").status_code == 200


# --- the read surface (#34) --------------------------------------------------


def test_the_display_defaults_survive_without_a_session(client: Any) -> None:
    """The pin for the whole design: the wall display seed data stays open.

    ``syncDisplayDefaults`` reads ``/api/settings`` to seed the browser
    temperature unit and refresh interval, and a flat 401 would leave a fresh
    tablet showing the built-in defaults — degrading exactly the screen this
    issue forbids degrading. So the endpoint keeps answering and withholds
    only the identifying values, absent rather than masked.
    """
    client.put("/api/settings", json={"site.contact_email": "owner@example.com"})
    set_password(_settings(client), PASSWORD)
    values = client.get("/api/settings").json()["values"]
    assert values["display.temperature_unit"] == "F"
    assert values["display.refresh_seconds"] == 5
    assert values["collector.poll_interval"] == 11.0
    # The four secret-flagged values are withheld; the public settings are not.
    for withheld in (
        "site.contact_email",
        "connection.dongle_host",
        "connection.dongle_serial",
        "connection.inverter_serial",
    ):
        assert withheld not in values


def test_the_read_surface_is_untouched_with_no_password(client: Any) -> None:
    # The regression bar for the read half: a fresh install sees every read
    # exactly as it did before authentication existed.
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/setup").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/live").status_code == 200
    assert client.get("/api/capabilities").status_code == 200


def test_settings_answers_normally_with_a_valid_session(client: Any) -> None:
    from arraysense.settings import _mask

    client.put("/api/settings", json={"site.contact_email": "owner@example.com"})
    set_password(_settings(client), PASSWORD)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    values = client.get("/api/settings").json()["values"]
    assert values["site.contact_email"] == _mask("owner@example.com")
    assert values["display.temperature_unit"] == "F"


def test_setup_is_a_401_with_a_password_and_no_session(client: Any) -> None:
    # /api/setup carries the connection editor values and the wall display
    # never requests it, so it takes the 401 that gives the settings page a
    # reason to prompt. The reads the dashboard actually polls stay open.
    set_password(_settings(client), PASSWORD)
    assert client.get("/api/setup").status_code == 401
    assert client.get("/api/settings").status_code == 200
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/live").status_code == 200
    assert client.get("/api/capabilities").status_code == 200
    assert client.get("/").status_code == 200


def test_setup_answers_normally_with_a_valid_session(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    assert client.get("/api/setup").status_code == 200


def test_capabilities_masks_the_serial_for_everyone(client: Any) -> None:
    # The serial is an installation secret by this project's own rules, and
    # nothing renders it — the pages consume the transport, the string count
    # and the metric list, never the serial — so it is masked for a caller
    # with no password and for one with a session alike. The mask is the
    # settings module's own, so one format means one thing everywhere.
    from arraysense.settings import _mask

    device = client.get("/api/capabilities").json()["devices"][0]
    assert device["device"] == _mask("CE00000000")
    set_password(_settings(client), PASSWORD)
    device = client.get("/api/capabilities").json()["devices"][0]
    assert device["device"] == _mask("CE00000000")


# --- the endpoints -----------------------------------------------------------


def test_login_sets_a_cookie_with_the_right_password(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 200
    header = r.headers.get("set-cookie", "")
    assert "arraysense_session" in header
    assert f"Max-Age={int(Sessions.SESSION_LIFETIME.total_seconds())}" in header
    assert "HttpOnly" in header
    assert "SameSite=strict" in header


def test_login_with_the_wrong_password_returns_401_and_sets_nothing(
    client: Any,
) -> None:
    set_password(_settings(client), PASSWORD)
    r = client.post("/api/auth/login", json={"password": "wrong"})
    assert r.status_code == 401
    assert "arraysense_session" not in r.headers.get("set-cookie", "")


def test_changing_the_password_without_the_current_one_is_refused(
    client: Any,
) -> None:
    # A session proves the browser once logged in, not that the person at it
    # knows the password; changing the credential must still require it.
    set_password(_settings(client), PASSWORD)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    r = client.post("/api/auth/password", json={"new_password": OTHER})
    assert r.status_code == 401
    assert verify_password(PASSWORD, password_hash(_settings(client)) or "")


def test_clearing_the_password_turns_authentication_off_and_revokes_sessions(
    client: Any,
) -> None:
    set_password(_settings(client), PASSWORD)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    token = client.cookies.get("arraysense_session")
    assert token is not None
    assert client.app.state.sessions.valid(token)
    r = client.post(
        "/api/auth/password",
        json={"new_password": "", "current_password": PASSWORD},
    )
    assert r.status_code == 200
    assert not client.app.state.sessions.valid(token), "clearing must revoke every session"
    assert not password_is_set(_settings(client))
    client.cookies.clear()
    assert client.put("/api/settings", json={"display.temperature_unit": "F"}).status_code == 200


def test_a_new_password_shorter_than_8_characters_is_refused(client: Any) -> None:
    r = client.post("/api/auth/password", json={"new_password": "short"})
    assert r.status_code == 400
    assert not password_is_set(_settings(client))


def test_login_is_refused_after_five_failures(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    # The sixth attempt, even with the right password, is refused for a minute.
    r = client.post("/api/auth/login", json={"password": PASSWORD})
    assert r.status_code == 429


def test_changing_the_password_is_throttled_like_logging_in(client: Any) -> None:
    """The throttle must not have a second door.

    ``/auth/password`` verifies the same secret as ``/auth/login`` to authorise
    a change. Guarding only the login endpoint left the current password open
    to unlimited guessing here — measured at fifteen attempts without a single
    refusal while login stopped at five.
    """
    set_password(_settings(client), PASSWORD)
    for _ in range(5):
        r = client.post(
            "/api/auth/password",
            json={"new_password": "somethinglong", "current_password": "wrong"},
        )
        assert r.status_code == 401
    r = client.post(
        "/api/auth/password",
        json={"new_password": "somethinglong", "current_password": "wrong"},
    )
    assert r.status_code == 429


def test_guessing_the_password_change_also_blocks_logging_in(client: Any) -> None:
    """One secret, one throttle. Failures here must count against login too."""
    set_password(_settings(client), PASSWORD)
    for _ in range(5):
        client.post(
            "/api/auth/password",
            json={"new_password": "somethinglong", "current_password": "wrong"},
        )
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_failed_guesses_before_a_password_is_set_do_not_fill_the_throttle(
    client: Any,
) -> None:
    """Nothing to guess yet, so nothing may be counted.

    Otherwise a stranger exhausts the throttle before the owner has set a
    password, and the owner's own first login meets a block somebody else
    earned — renewable every minute, so waiting never clears it.

    It is ``/auth/login`` that must be exercised here, and only it. An earlier
    version of this test posted short passwords to ``/auth/password``, which
    returns 400 on the length check before any throttle code runs when no
    password is set — so it passed without ever reaching the behaviour it
    names, and the real hole in ``login`` survived a review underneath it.
    ``/auth/password`` cannot be probed this way at all: with no password set
    it takes the first one it is given, so guessing a current password there
    sets one instead, and the wrong guesses that follow fill the throttle
    legitimately.
    """
    for _ in range(6):
        assert client.post("/api/auth/login", json={"password": "junk"}).status_code == 401
    set_password(_settings(client), PASSWORD)
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


# --- the isolation -----------------------------------------------------------

# The hash must be invisible to the settings API in both directions: never
# read out, never written in. A key that appears on the page or accepts a PUT
# is a credential stored where every device on the LAN can see it.


def test_the_hash_key_never_appears_in_get_settings(client: Any) -> None:
    set_password(_settings(client), PASSWORD)
    body = client.get("/api/settings").json()
    assert AUTH_PASSWORD_KEY not in body["values"]
    field_keys = [f["key"] for f in body["fields"]]
    assert AUTH_PASSWORD_KEY not in field_keys


def test_put_settings_with_the_hash_key_is_a_400(client: Any) -> None:
    payload = dict()
    payload[AUTH_PASSWORD_KEY] = "scrypt$16384$8$1$0000$0000"
    r = client.put("/api/settings", json=payload)
    assert r.status_code == 400
    assert not password_is_set(_settings(client))


def test_describe_does_not_list_the_hash_key() -> None:
    field_keys = [f["key"] for f in describe()]
    assert AUTH_PASSWORD_KEY not in field_keys


def test_overrides_does_not_warn_about_the_hash_key(client: Any, caplog: Any) -> None:
    # A warning on every startup is how this is noticed six months later: the
    # key is deliberately absent from the registry, and overrides() must skip
    # it by name rather than log "ignoring unusable stored setting" each boot.
    set_password(_settings(client), PASSWORD)
    with caplog.at_level(logging.WARNING, logger="arraysense.settings"):
        _settings(client).overrides()
    assert not any("auth.password_hash" in r.getMessage() for r in caplog.records)


# --- the CLI recovery --------------------------------------------------------


def test_clear_password_with_a_password_set_removes_it_and_reports_so(
    tmp_path: Path, capsys: Any
) -> None:
    db = tmp_path / "as.db"
    store = SqliteStore(str(db), device=TEST_DEVICE)
    set_password(SettingsStore(store), PASSWORD)
    store.close()
    assert main_module.main(["--config", str(_toml(tmp_path, db)), "--clear-password"]) == 0
    out = capsys.readouterr().out
    assert "cleared" in out.lower()
    reopened = SqliteStore(str(db), device=TEST_DEVICE)
    assert not password_is_set(SettingsStore(reopened))
    reopened.close()


def test_clear_password_with_none_set_exits_cleanly_and_says_so(
    tmp_path: Path, capsys: Any
) -> None:
    db = tmp_path / "as.db"
    store = SqliteStore(str(db), device=TEST_DEVICE)
    store.close()
    assert main_module.main(["--config", str(_toml(tmp_path, db)), "--clear-password"]) == 0
    out = capsys.readouterr().out
    assert "no password" in out.lower()
