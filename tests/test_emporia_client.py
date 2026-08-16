"""test_emporia_client.py — logging in, staying in, and failing usefully.

The distinction under test is the one the reference implementation gets wrong: a
rejected credential and an unreachable service are different states with
different remedies, and conflating them makes a flaky link read as "you have
been logged out". The rest guards details that are silently wrong rather than
loudly wrong — Cognito returning no refresh token on a refresh, and Emporia
wanting its own header name rather than a bearer.
"""

from __future__ import annotations

import json

import pytest

from arraysense.modules.emporia.client import (
    EmporiaAuthExpiredError,
    EmporiaChallengeError,
    EmporiaClient,
    EmporiaUnreachableError,
)
from arraysense.modules.emporia.tokens import TokenSet


def _auth_body(refresh: str | None = "refresh-1") -> bytes:
    result: dict[str, object] = {
        "IdToken": "id-1",
        "AccessToken": "access-1",
        "TokenType": "Bearer",
    }
    if refresh is not None:
        result["RefreshToken"] = refresh
    return json.dumps({"AuthenticationResult": result}).encode()


def test_login_returns_tokens_and_stamps_when_the_refresh_began() -> None:
    calls: list[tuple[str, str]] = []

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        calls.append((method, url))
        assert body is not None
        assert json.loads(body)["AuthFlow"] == "USER_PASSWORD_AUTH"
        return 200, _auth_body()

    got = EmporiaClient(transport=transport).login("someone@example.invalid", "pw")
    assert got.refresh_token == "refresh-1"
    assert got.id_token == "id-1"
    assert got.refresh_issued != "", "the refresh clock starts at login and must be recorded"
    assert calls[0][0] == "POST"


def test_a_refresh_keeps_the_original_refresh_token() -> None:
    # Cognito never returns a new refresh token on a refresh. Overwriting the
    # stored one with an absent value would log the owner out every hour.
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, _auth_body(refresh=None)

    before = TokenSet("old-id", "refresh-1", "2026-08-15T00:00:00+00:00")
    after = EmporiaClient(transport=transport).refresh(before)
    assert after.refresh_token == "refresh-1"
    assert after.refresh_issued == "2026-08-15T00:00:00+00:00", "the clock must not restart"
    assert after.id_token == "id-1"


def test_a_dead_refresh_token_is_its_own_state_not_a_network_error() -> None:
    # The distinction this module exists to get right: a rejected credential
    # means "log in again", a broken connection means "try later". Conflating
    # them makes a flaky link look like being logged out.
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 400, json.dumps(
            {"__type": "NotAuthorizedException", "message": "Refresh Token has expired"}
        ).encode()

    with pytest.raises(EmporiaAuthExpiredError):
        EmporiaClient(transport=transport).refresh(TokenSet("i", "r", "when"))


def test_a_transport_failure_is_unreachable_not_expired() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        raise OSError("name resolution failed")

    with pytest.raises(EmporiaUnreachableError):
        EmporiaClient(transport=transport).refresh(TokenSet("i", "r", "when"))


def test_a_login_challenge_is_reported_rather_than_swallowed() -> None:
    # MFA or a forced password change. Silently failing here would look like a
    # wrong password and send the owner round a loop they cannot win.
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, json.dumps({"ChallengeName": "SOFTWARE_TOKEN_MFA"}).encode()

    with pytest.raises(EmporiaChallengeError, match="SOFTWARE_TOKEN_MFA"):
        EmporiaClient(transport=transport).login("someone@example.invalid", "pw")


def test_get_sends_the_id_token_in_the_authtoken_header() -> None:
    # Not Authorization: Bearer. Emporia uses its own header name and a bearer
    # token is silently unauthenticated.
    seen: dict[str, str] = {}

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        seen.update(headers)
        return 200, json.dumps({"devices": []}).encode()

    EmporiaClient(transport=transport).get("/customers/devices", "id-1")
    assert seen.get("authtoken") == "id-1"
    assert "Authorization" not in seen


def test_a_401_from_the_api_is_an_expired_auth() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 401, b"{}"

    with pytest.raises(EmporiaAuthExpiredError):
        EmporiaClient(transport=transport).get("/customers/devices", "id-1")


def test_a_server_error_from_cognito_is_unreachable_not_a_bad_password() -> None:
    # A 500 is Emporia's problem, not the owner's. Reported as a credential
    # failure it would send somebody to reset a password that was never wrong.
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 500, json.dumps({"message": "Internal Server Error"}).encode()

    with pytest.raises(EmporiaUnreachableError, match="HTTP 500"):
        EmporiaClient(transport=transport).login("someone@example.invalid", "pw")


def test_a_cognito_body_that_is_not_an_object_is_unreachable() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, json.dumps([1, 2, 3]).encode()

    with pytest.raises(EmporiaUnreachableError, match="unexpected shape"):
        EmporiaClient(transport=transport).login("someone@example.invalid", "pw")


def test_an_authentication_result_that_is_not_an_object_is_unreachable() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, json.dumps({"AuthenticationResult": [1, 2, 3]}).encode()

    with pytest.raises(EmporiaUnreachableError, match="AuthenticationResult"):
        EmporiaClient(transport=transport).login("someone@example.invalid", "pw")


def test_a_reply_with_neither_a_challenge_nor_a_result_is_unreachable() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, json.dumps({"OtherKey": "value"}).encode()

    with pytest.raises(EmporiaUnreachableError, match="AuthenticationResult"):
        EmporiaClient(transport=transport).login("someone@example.invalid", "pw")


def test_a_server_error_and_a_missing_endpoint_are_both_unreachable() -> None:
    # 404 matters here specifically: /customers/evchargers is documented and
    # returns one, so the client meets this in normal use and must back off
    # rather than declare the credential dead.
    for status in (500, 404):

        def transport(
            method: str,
            url: str,
            headers: dict[str, str],
            body: bytes | None,
            code: int = status,
        ) -> tuple[int, bytes]:
            return code, b"{}"

        with pytest.raises(EmporiaUnreachableError, match=f"HTTP {status}"):
            EmporiaClient(transport=transport).get("/path", "id-1")


def test_a_json_list_is_returned_as_it_arrived() -> None:
    # Emporia answers some paths with an array. get() returns whatever parsed,
    # and the parsers decide what to make of it.
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, b'["a","b"]'

    assert EmporiaClient(transport=transport).get("/path", "id-1") == ["a", "b"]


# --- writing to the charger ------------------------------------------------
#
# The one path in this module that changes something in the physical world, and
# the value it writes persists for ever. Nothing here reaches a real charger:
# the transport is a function, and these assert on what would have been sent.


def _charger_record() -> dict[str, object]:
    """Shaped like the reference account's, with an obviously invented PIN.

    A write will not be accepted without one, so the field has to be here — but
    a value that could be mistaken for a real breaker PIN has no business in a
    public repository, and a plausible-looking four digits is exactly what a
    reader would mistake for one.
    """
    return {
        "deviceGid": 900001,
        "loadGid": 900002,
        "breakerPIN": "NOT-A-REAL-PIN",
        "chargerOn": True,
        "chargingRate": 6,
        "maxChargingRate": 48,
        "loadManagementEnabled": False,
        "status": "Standby",
    }


def test_a_write_echoes_the_whole_record_with_one_field_changed() -> None:
    # PUT /devices/evcharger takes the entire object. Sending only the field
    # being changed drops every other one, and what a charger does with a record
    # missing half its fields is not something to discover on somebody's car.
    sent: dict[str, object] = {}

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        assert body is not None
        sent.update({"method": method, "url": url, "body": json.loads(body)})
        return 200, json.dumps({"chargingRate": 16}).encode()

    EmporiaClient(transport=transport).set_charge_rate(_charger_record(), 16, "id-1")

    assert sent["method"] == "PUT"
    assert str(sent["url"]).endswith("/devices/evcharger")
    body = sent["body"]
    assert isinstance(body, dict)
    assert body["chargingRate"] == 16, "the one field that changed"
    assert body["maxChargingRate"] == 48, "and everything else came along"
    assert body["breakerPIN"] == "NOT-A-REAL-PIN", "which the charger requires to accept the write"


def test_a_write_does_not_mutate_the_record_it_was_given() -> None:
    # The caller holds that record to compare against afterwards. A write that
    # edited it in place would leave nothing to compare with.
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 200, b"{}"

    record = _charger_record()
    EmporiaClient(transport=transport).set_charge_rate(record, 16, "id-1")
    assert record["chargingRate"] == 6


def test_a_rejected_write_is_reported_rather_than_assumed_to_have_worked() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 500, b"{}"

    with pytest.raises(EmporiaUnreachableError):
        EmporiaClient(transport=transport).set_charge_rate(_charger_record(), 16, "id-1")


def test_a_write_refused_for_the_credential_is_its_own_state() -> None:
    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        return 401, b"{}"

    with pytest.raises(EmporiaAuthExpiredError):
        EmporiaClient(transport=transport).set_charge_rate(_charger_record(), 16, "id-1")


def test_stopping_the_charger_echoes_the_record_with_the_switch_flipped() -> None:
    sent: dict[str, object] = {}

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        assert body is not None
        sent.update(json.loads(body))
        return 200, json.dumps({"chargerOn": False}).encode()

    EmporiaClient(transport=transport).write_charger(
        _charger_record(), {"chargerOn": False}, "id-1"
    )

    assert sent["chargerOn"] is False
    assert sent["chargingRate"] == 6, "the rate it was at is not disturbed by switching it off"
    assert sent["breakerPIN"] == "NOT-A-REAL-PIN"


def test_a_write_can_change_two_fields_at_once() -> None:
    sent: dict[str, object] = {}

    def transport(
        method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        assert body is not None
        sent.update(json.loads(body))
        return 200, b"{}"

    EmporiaClient(transport=transport).write_charger(
        _charger_record(), {"chargerOn": True, "chargingRate": 24}, "id-1"
    )
    assert sent["chargerOn"] is True
    assert sent["chargingRate"] == 24
