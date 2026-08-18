"""tokens.py — the Emporia credential, kept out of the database on purpose.

Emporia issues no API key. The refresh token is account access: whoever holds it
can read every circuit and set the charge rate until it expires. So it lives in
a 0600 file owned by the service account rather than in the database, because
the realistic threat here is a database or backup being shared while diagnosing
a problem — and separation defeats that completely.

Encryption was considered and rejected. The service must use this token
unattended after a reboot, so any key would sit on the same machine readable by
the same account, and an attacker who can read the database can read the key
beside it. A TPM or a boot passphrase would genuinely beat separation; the
reference hardware has neither, and a passphrase at boot stops solar monitoring
until somebody is home.

The password is never stored, not even briefly: it buys tokens and is dropped.

Every failure to read is absence rather than an exception. Nothing here is
irreplaceable — the owner can always log in again — and a truncated write must
not be a service that refuses to start.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenSet:
    """What is kept between runs, and when the long-lived half began.

    ``refresh_issued`` is recorded because Cognito never rotates a refresh token
    — a refresh response carries no new one — so its clock runs from the
    original login and cannot be extended by polling. Emporia's actual lifetime
    is not readable from outside, so the installation learns it by noticing when
    this one first stops working.
    """

    id_token: str
    refresh_token: str
    refresh_issued: str


def load(path: Path) -> TokenSet | None:
    """The stored tokens, or None when there are none worth having."""
    try:
        raw = path.read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("emporia token file at %s is not readable JSON; treating as absent", path)
        return None
    if not isinstance(data, dict):
        return None
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        return None
    id_token = data.get("id_token")
    issued = data.get("refresh_issued")
    return TokenSet(
        id_token=id_token if isinstance(id_token, str) else "",
        refresh_token=refresh,
        refresh_issued=issued if isinstance(issued, str) else "",
    )


def save(path: Path, token_set: TokenSet) -> None:
    """Write the tokens, readable by nobody but the account that owns them.

    The mode is set on the descriptor rather than after writing, so the file is
    never briefly world-readable between creation and chmod.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "id_token": token_set.id_token,
            "refresh_token": token_set.refresh_token,
            "refresh_issued": token_set.refresh_issued,
        }
    )
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(payload)
    os.chmod(path, 0o600)


def clear(path: Path) -> None:
    """Forget the tokens. Repeating it is not an error."""
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
