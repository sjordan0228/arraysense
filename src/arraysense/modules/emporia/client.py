"""client.py — talk to Emporia's cloud, with the standard library and nothing else.

There is no official Emporia API and no local access: every reading crosses
their cloud. The endpoints here were established by measurement against a real
account, because the community documentation is wrong in two places —
``/customers/evchargers`` returns 404, and charger control is
``PUT /devices/evcharger`` rather than ``/devices/outlet``.

Authentication is AWS Cognito. ``USER_PASSWORD_AUTH`` is enabled on Emporia's
app client, verified by probing with a deliberately fake username: it answered
``NotAuthorizedException`` rather than "flow not enabled". That is why this needs
no dependency at all — login is one JSON POST — where the reference library pulls
in pycognito and boto3 to do the same job.

The distinction this module exists to get right is between a **rejected
credential** and an **unreachable service**. They are different states with
different remedies, and the reference implementation conflates them by letting
botocore's exception escape uncaught — which is the likeliest explanation for the
unresolved "cannot log in" reports against the Home Assistant integration. A
flaky connection must never read as "you have been logged out".
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime

from arraysense.modules.emporia.tokens import TokenSet

logger = logging.getLogger(__name__)

COGNITO_URL = "https://cognito-idp.us-east-2.amazonaws.com/"
# Emporia's own app client, hardcoded in their mobile application and published
# in the community library. Not a secret and not the owner's: it identifies the
# application, not the account.
CLIENT_ID = "4qte47jbstod8apnfic0bunmrq"
API_BASE = "https://api.emporiaenergy.com"
TIMEOUT_SECONDS = 20.0

# (method, url, headers, body) -> (status, body). Injected so tests never touch
# the network and never need an account.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class EmporiaError(Exception):
    """Anything that stopped a call to Emporia completing."""


class EmporiaUnreachableError(EmporiaError):
    """The service could not be reached, or answered with a server error.

    Transient by assumption: back off and try again. This must never be treated
    as a credential problem.
    """


class EmporiaAuthExpiredError(EmporiaError):
    """The credential was rejected. Only a fresh login fixes this.

    Distinct from unreachable because the remedy is different and involves the
    owner. Cognito never rotates a refresh token, so its clock runs from the
    original login and no amount of polling extends it.
    """


class EmporiaChallengeError(EmporiaError):
    """Cognito wants more than a password — MFA, or a forced password change."""


def _urllib_transport(
    method: str, url: str, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """The production transport. Blocking, so callers move it off the loop."""
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return int(response.status), bytes(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), bytes(exc.read())


class EmporiaClient:
    """Every call this module makes to Emporia, and the failures they can have."""

    def __init__(self, transport: Transport | None = None) -> None:
        """Wire the client to a transport; production passes none and gets urllib."""
        self._transport = transport if transport is not None else _urllib_transport

    def _cognito(self, payload: dict[str, object]) -> dict[str, object]:
        headers = {
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            "Content-Type": "application/x-amz-json-1.1",
        }
        try:
            status, raw = self._transport(
                "POST", COGNITO_URL, headers, json.dumps(payload).encode()
            )
        # URLError is an OSError subclass, so this catches a dead socket, a
        # refused connection and a name that will not resolve alike. All three
        # mean "try again later", which is exactly what unreachable says.
        except OSError as exc:
            raise EmporiaUnreachableError(f"could not reach Cognito: {exc}") from exc
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmporiaUnreachableError("Cognito returned something that was not JSON") from exc
        if not isinstance(body, dict):
            raise EmporiaUnreachableError("Cognito returned an unexpected shape")
        if status == 200:
            return body
        kind = str(body.get("__type", ""))
        if "NotAuthorizedException" in kind or "UserNotFoundException" in kind:
            raise EmporiaAuthExpiredError(str(body.get("message", "credential rejected")))
        raise EmporiaUnreachableError(f"Cognito returned HTTP {status}")

    @staticmethod
    def _tokens_from(body: dict[str, object], previous: TokenSet | None) -> TokenSet:
        if "ChallengeName" in body:
            raise EmporiaChallengeError(str(body["ChallengeName"]))
        result = body.get("AuthenticationResult")
        if not isinstance(result, dict):
            raise EmporiaUnreachableError("Cognito answered without an AuthenticationResult")
        id_token = str(result.get("IdToken", ""))
        # A refresh response carries no RefreshToken, so the stored one is kept.
        # Overwriting it with an absent value would log the owner out hourly.
        refresh = result.get("RefreshToken")
        if isinstance(refresh, str) and refresh:
            return TokenSet(id_token, refresh, datetime.now(tz=UTC).isoformat(timespec="seconds"))
        if previous is None:
            raise EmporiaUnreachableError("Cognito answered a login without a refresh token")
        return TokenSet(id_token, previous.refresh_token, previous.refresh_issued)

    def login(self, email: str, password: str) -> TokenSet:
        """Exchange a password for tokens. The password is not retained."""
        body = self._cognito(
            {
                "AuthFlow": "USER_PASSWORD_AUTH",
                "ClientId": CLIENT_ID,
                "AuthParameters": {"USERNAME": email, "PASSWORD": password},
            }
        )
        return self._tokens_from(body, previous=None)

    def refresh(self, token_set: TokenSet) -> TokenSet:
        """Renew the hour-long id token. Needs no password."""
        body = self._cognito(
            {
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": CLIENT_ID,
                "AuthParameters": {"REFRESH_TOKEN": token_set.refresh_token},
            }
        )
        return self._tokens_from(body, previous=token_set)

    def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
        """Command a charge rate. A thin name over ``write_charger``."""
        return self.write_charger(record, {"chargingRate": amps}, id_token)

    def write_charger(
        self, record: dict[str, object], changes: dict[str, object], id_token: str
    ) -> object:
        """Change fields on the charger, echoing its whole record back.

        The one call in this module that changes something physical, and the
        value it writes **persists for ever**: nothing at Emporia's end will
        return it. Every guard around that lives in ``control.py``; what happens
        here is only the sending.

        The whole record goes back with the named fields changed, because
        ``PUT /devices/evcharger`` takes the entire object — including
        ``breakerPIN``, which the charger requires to accept a write at all.
        That is also why the record is copied rather than edited: the caller
        holds the original to compare against once the write has landed, and
        the prototype's lesson was to re-read rather than assume the commanded
        rate took.

        The endpoint is ``/devices/evcharger``, not the ``/customers/evchargers``
        the community documentation names — that one answers 404.
        """
        payload = dict(record)
        payload.update(changes)
        headers = {"authtoken": id_token, "Content-Type": "application/json"}
        try:
            status, raw = self._transport(
                "PUT", API_BASE + "/devices/evcharger", headers, json.dumps(payload).encode()
            )
        except OSError as exc:
            raise EmporiaUnreachableError(f"could not reach Emporia: {exc}") from exc
        if status == 401:
            raise EmporiaAuthExpiredError("Emporia rejected the token")
        if status >= 400:
            raise EmporiaUnreachableError(f"Emporia returned HTTP {status} for the charger write")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A write that landed but answered with something unreadable is not
            # a failed write. The caller re-reads to find out either way.
            return None

    def get(self, path: str, id_token: str) -> object:
        """One authenticated GET. The token goes in ``authtoken``, not a bearer."""
        try:
            status, raw = self._transport("GET", API_BASE + path, {"authtoken": id_token}, None)
        except OSError as exc:
            raise EmporiaUnreachableError(f"could not reach Emporia: {exc}") from exc
        if status == 401:
            raise EmporiaAuthExpiredError("Emporia rejected the token")
        if status >= 400:
            raise EmporiaUnreachableError(f"Emporia returned HTTP {status} for {path}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EmporiaUnreachableError(f"Emporia returned non-JSON for {path}") from exc
