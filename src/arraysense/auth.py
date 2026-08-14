"""auth.py — the optional credential: the hash, the sessions, and the login throttle.

The owner's answer to #34 is that authentication is off until somebody sets a
password. A hash under AUTH_PASSWORD_KEY is the switch: with none stored, every
write endpoint behaves exactly as it did before this module existed; with one,
they ask for a session cookie. The key lives in the existing settings table and
is deliberately absent from the settings registry, so the settings API can
neither read it nor write it.

What this protects against, and what it does not, is part of the contract. The
password and the session cookie cross a home LAN in plain HTTP, so the
protection is against casual and accidental writes from anything that can reach
the port — a guest phone, an IoT gadget, a device with the page open — and not
against anyone who can observe the traffic. Describing it as more than that
would be the same mistake as presenting a partial figure as a complete one,
which is the rule #23 settles.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arraysense.settings import SettingsStore

logger = logging.getLogger(__name__)

# The settings-table key the password hash is stored under. Deliberately NOT in
# the SETTINGS registry: a registered key would be readable and writable through
# the settings API, which is the one thing a credential must not be.
AUTH_PASSWORD_KEY = "auth.password_hash"

# scrypt parameters, chosen for the hardware this runs on. n=2**14 costs about
# 16 MB and a fraction of a second on a Pi 4, which is the point: the parameters
# travel with the hash, so a later release can raise the cost without stranding
# existing passwords.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

# The shortest password worth asking for. The owner is the only person who ever
# types it, so eight characters is not a burden; below that, the hash is cheap
# to guess off a stolen settings table.
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    """Return a self-describing scrypt hash for ``password``, salted fresh.

    The stored form is ``scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>``, so the
    parameters travel with the hash. That is what lets a later release raise
    the cost without stranding existing passwords: verify reads the parameters
    out of the stored value rather than assuming the current ones.
    """
    salt = os.urandom(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${derived.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check ``password`` against the stored form, never raising on a bad one.

    A malformed stored value is a mismatch rather than a fault: the value lives
    in the settings table, which a hand edit or a previous buggy release could
    leave in any shape, and the correct response to an unreadable credential is
    to refuse it, not to fail the login with a traceback nobody can use.
    """
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(derived, expected)


def set_password(store: SettingsStore, password: str) -> None:
    """Store a fresh hash for ``password``, replacing whatever was there.

    The write goes straight to the settings table rather than through
    ``SettingsStore.set``, because that method validates against the registry
    and this key is deliberately not in it. The connection and its transaction
    are the store's own, so the hash lands with the same durability every
    setting does.
    """
    stored = hash_password(password)
    with store._conn:
        store._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (AUTH_PASSWORD_KEY, stored),
        )
    logger.info("authentication password set")


def clear_password(store: SettingsStore) -> None:
    """Remove the stored hash, switching authentication off.

    The session table is separate and in memory; the endpoint that calls this
    revokes it, because only it knows that the switch has been thrown.
    """
    with store._conn:
        store._conn.execute("DELETE FROM settings WHERE key = ?", (AUTH_PASSWORD_KEY,))
    logger.info("authentication password cleared")


def password_is_set(store: SettingsStore) -> bool:
    """Whether a hash is currently stored.

    This is the switch the write guard reads, and it has to be a fact about
    the database rather than about the process: a restart must not silently
    lock the owner out of a service that has a password, nor unlock one that
    does not.
    """
    row = store._conn.execute(
        "SELECT value FROM settings WHERE key = ?", (AUTH_PASSWORD_KEY,)
    ).fetchone()
    return row is not None and bool(row[0])


def password_hash(store: SettingsStore) -> str | None:
    """The stored hash, or None when no password has been set.

    Login needs the actual hash to verify against, not merely the fact that one
    exists, so this is the read half of ``set_password``.
    """
    row = store._conn.execute(
        "SELECT value FROM settings WHERE key = ?", (AUTH_PASSWORD_KEY,)
    ).fetchone()
    if row is None or not row[0]:
        return None
    return str(row[0])


class Sessions:
    """The in-memory session table, keyed by the sha256 of each token.

    A restart ends every session; that is accepted, and it is the better trade
    here for two reasons the plan records: the wall display holds no session to
    lose, and the database is copied by the backup feature, so a persistent
    session token would ride into every backup archive as a live credential.
    Nothing about authentication is written to disk except the password hash
    itself.

    Only the digest of a token is kept, never the token, so a memory dump or a
    stray log line does not hand over a live session.
    """

    SESSION_LIFETIME = timedelta(days=30)

    def __init__(self) -> None:
        """Start with no sessions — every session begins after the process does."""
        self._expiry: dict[str, float] = {}

    def issue(self) -> str:
        """Start a session and return its raw token.

        The caller hands the token to the browser in a cookie; only its digest
        is kept here.
        """
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self._expiry[digest] = time.time() + self.SESSION_LIFETIME.total_seconds()
        return token

    def valid(self, token: str) -> bool:
        """Whether ``token`` names a session that has not expired.

        Expiry is lazy: an expired token is dropped when it is looked up, and
        nothing sweeps the table. At this scale — one owner, one session a
        month — the map is small, and a sweep would be machinery for its own
        sake.
        """
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        expires = self._expiry.get(digest)
        if expires is None:
            return False
        if time.time() >= expires:
            del self._expiry[digest]
            return False
        return True

    def revoke(self, token: str) -> None:
        """End the one session named by ``token``."""
        self._expiry.pop(hashlib.sha256(token.encode("utf-8")).hexdigest(), None)

    def revoke_all(self) -> None:
        """End every session. Clearing the password calls this."""
        self._expiry.clear()


class LoginThrottle:
    """Backstop against guessing the password, in memory and deliberately small.

    scrypt is already slow enough to make online brute force impractical, so
    this is not the defence; the plan calls it a backstop. After five failed
    attempts from one client, the login endpoint refuses that client for sixty
    seconds. Keyed by the client address because it is the only thing a request
    reliably carries; a home network's NAT means a device shares its block,
    which is the price of having a key at all and acceptable for a minute at a
    time.
    """

    MAX_FAILURES = 5
    BLOCK_SECONDS = 60.0

    def __init__(self) -> None:
        """Start with no failures and no blocks recorded."""
        self._failures: dict[str, int] = {}
        self._blocked_until: dict[str, float] = {}

    def blocked(self, key: str, now: float) -> bool:
        """Whether ``key`` is currently refused, clearing an expired block."""
        until = self._blocked_until.get(key)
        if until is None:
            return False
        if now >= until:
            del self._blocked_until[key]
            self._failures.pop(key, None)
            return False
        return True

    def record_failure(self, key: str, now: float) -> None:
        """Note one wrong password, blocking ``key`` once the limit is reached.

        The block replaces the failure count rather than sitting beside it, so
        a client that stays blocked does not pile fresh failures on top of the
        ones that earned the block.
        """
        count = self._failures.get(key, 0) + 1
        if count >= self.MAX_FAILURES:
            self._blocked_until[key] = now + self.BLOCK_SECONDS
            self._failures.pop(key, None)
        else:
            self._failures[key] = count

    def record_success(self, key: str) -> None:
        """Clear a client's history after a correct password."""
        self._failures.pop(key, None)
        self._blocked_until.pop(key, None)
