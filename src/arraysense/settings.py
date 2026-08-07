"""settings.py — the settings a person changes, kept in the database rather than a file.

Editing a TOML file over SSH is a reasonable thing to ask of someone who already
has a terminal open, and an unreasonable thing to ask of someone whose solar
monitor is a tablet on a wall. Everything here is settable from the running
service, takes effect without a restart, and is one value for the whole
installation rather than per browser — a temperature unit stored in local
storage means the tablet and the phone disagree about what 39 means.

Defaults live in this registry, never in the database. A fresh install with an
empty table behaves exactly like a configured one, and adding a setting is a
line here rather than a migration.

What deliberately stays outside: the database path and the address the server
binds to. Those are needed before there is a database to read them from, so
they remain command-line arguments. Nothing sensitive lives in that set.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

Kind = Literal["str", "int", "float", "bool", "choice"]


@dataclass(frozen=True)
class SettingSpec:
    """One settable value: its type, its bounds, and how to describe it to a person.

    ``secret`` marks a value the API masks rather than echoes. The dongle and
    inverter serials are not passwords, but they identify specific hardware and
    the settings page has no authentication in front of it, so they go out with
    their middle replaced — enough for the owner to recognise which serial is
    configured, not enough for a stranger on the network to learn it.

    ``label`` and ``help`` live here rather than in the page because the page is
    not the only thing that renders them, and because a setting whose meaning is
    only explained in HTML is one nobody can document.
    """

    key: str
    kind: Kind
    default: object
    label: str
    help: str
    choices: tuple[str, ...] = ()
    lower: float | None = None
    upper: float | None = None
    secret: bool = False

    def validate(self, value: object) -> object:
        """Return ``value`` coerced to this setting's type, or raise ValueError.

        Refuses rather than coerces across types. A refresh interval given as
        "soon" is a mistake, and quietly turning it into a number would hide the
        mistake somewhere far from where it was made.

        Raises:
            ValueError: the value is the wrong type, outside the bounds, or not
                one of the allowed choices.
        """
        if self.kind == "choice":
            if value not in self.choices:
                raise ValueError(f"{self.key} must be one of {list(self.choices)}, got {value!r}")
            return value
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"{self.key} must be true or false, got {value!r}")
            return value
        if self.kind == "str":
            if not isinstance(value, str):
                raise ValueError(f"{self.key} must be text, got {value!r}")
            return value
        # Booleans are integers in Python, so they have to be excluded before
        # the numeric check or `true` would quietly become 1.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{self.key} must be a number, got {value!r}")
        number = int(value) if self.kind == "int" else float(value)
        if self.lower is not None and number < self.lower:
            raise ValueError(f"{self.key} must be at least {self.lower}, got {number}")
        if self.upper is not None and number > self.upper:
            raise ValueError(f"{self.key} must be at most {self.upper}, got {number}")
        return number

    def decode(self, stored: str) -> object:
        """Turn the stored text back into the type this setting is declared as."""
        if self.kind == "int":
            return int(float(stored))
        if self.kind == "float":
            return float(stored)
        if self.kind == "bool":
            return stored == "1"
        return stored


SETTINGS: tuple[SettingSpec, ...] = (
    # --- Display ------------------------------------------------------------
    SettingSpec(
        key="display.temperature_unit",
        kind="choice",
        choices=("F", "C"),
        default="F",
        label="Temperature unit",
        help="Applies to every temperature on the page, on every device.",
    ),
    SettingSpec(
        key="display.refresh_seconds",
        kind="int",
        default=5,
        lower=1,
        upper=300,
        label="Dashboard refresh",
        help=(
            "How often the page asks for new readings. It does not affect how "
            "often the inverter is polled, so setting it faster than the poll "
            "interval only redraws the same numbers."
        ),
    ),
    # --- Collector ----------------------------------------------------------
    SettingSpec(
        key="collector.poll_interval",
        kind="float",
        default=11.0,
        lower=1.0,
        upper=3600.0,
        label="Poll interval",
        help=(
            "Seconds between reads of the inverter. The dongle answers at its "
            "own pace, so asking faster than about ten seconds mostly produces "
            "reads that overlap the previous one."
        ),
    ),
    # --- Connection ---------------------------------------------------------
    # These identify one particular installation. They are written blind and
    # read back redacted, because the page in front of them is unauthenticated.
    SettingSpec(
        key="connection.dongle_host",
        kind="str",
        default="",
        label="Dongle address",
        help="The WiFi dongle's IP address. Give it a static DHCP lease.",
        secret=True,
    ),
    SettingSpec(
        key="connection.dongle_serial",
        kind="str",
        default="",
        label="Dongle serial",
        help=(
            "Ten characters, on the dongle's label or broadcast as its WiFi "
            "network name. The protocol authenticates with it."
        ),
        secret=True,
    ),
    SettingSpec(
        key="connection.inverter_serial",
        kind="str",
        default="",
        label="Inverter serial",
        help=(
            "Ten characters. Read it off the inverter itself — other tools have "
            "been seen reporting a different value, and a mismatch makes every "
            "read fail."
        ),
        secret=True,
    ),
)

_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}

SETTINGS_DDL = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL
) STRICT, WITHOUT ROWID
"""


def lookup_setting(key: str) -> SettingSpec:
    """Return the spec for ``key``, raising KeyError if nothing is registered under it.

    Raises rather than returning None so a typo in a caller surfaces where it
    was made. A settings page that silently accepts an unknown key renders a
    control that changes nothing.
    """
    try:
        return _BY_KEY[key]
    except KeyError as exc:
        raise KeyError(f"unknown setting: {key!r}") from exc


class SettingsStore:
    """Reads and writes settings against the same database the readings live in.

    Holds no cache. Settings change rarely and are read from a single-row
    primary-key lookup, so caching would buy nothing measurable and would mean
    a value changed in one worker going unseen by another.
    """

    def __init__(self, store: SqliteStore) -> None:
        """Attach to an open store and make sure the settings table exists.

        Takes the store rather than a raw connection so settings share its
        transaction and its lifetime: a settings write and a reading write in
        the same moment commit together, and nothing outlives the database it
        was reading from.
        """
        self._conn: sqlite3.Connection = store._conn
        self._conn.execute(SETTINGS_DDL)
        self._conn.commit()

    def get(self, key: str) -> object:
        """Return the stored value for ``key``, or its registered default.

        Raises:
            KeyError: no setting is registered under that key.
        """
        spec = lookup_setting(key)
        row = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return spec.default
        try:
            return spec.decode(row[0])
        except ValueError:
            # A value that no longer decodes means the setting's type changed
            # under a database that still holds the old shape. The default is a
            # working answer; refusing to start is not.
            logger.warning("setting %s holds undecodable %r; using the default", key, row[0])
            return spec.default

    def set(self, key: str, value: object) -> None:
        """Validate ``value`` against its spec and store it.

        Raises:
            KeyError: no setting is registered under that key.
            ValueError: the value is the wrong type or outside its bounds.
        """
        spec = lookup_setting(key)
        checked = spec.validate(value)
        stored = "1" if checked is True else "0" if checked is False else str(checked)
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stored),
            )
        logger.info("setting %s changed", key)

    def clear(self, key: str) -> None:
        """Forget any stored value for ``key`` so it reads its default again."""
        lookup_setting(key)
        with self._conn:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))

    def overrides(self) -> dict[str, object]:
        """Return only the settings someone has actually stored a value for.

        A default is not a decision. Callers merging these over a configuration
        file need to tell "set to the default" from "never touched", because a
        default silently overwriting a file setting would mean the file could
        never win anything.
        """
        rows = self._conn.execute("SELECT key, value FROM settings").fetchall()
        out: dict[str, object] = {}
        for key, raw in rows:
            try:
                out[key] = lookup_setting(key).decode(raw)
            except (KeyError, ValueError):
                # A key retired from the registry, or a value that no longer
                # decodes. Neither should stop the service from starting.
                logger.warning("ignoring unusable stored setting %r", key)
        return out

    def all(self) -> dict[str, object]:
        """Return every registered setting, stored value or default.

        Every key always appears. A caller rendering a settings page needs the
        untouched ones too, and an absent key would be indistinguishable from
        one deliberately left empty.
        """
        return {spec.key: self.get(spec.key) for spec in SETTINGS}

    def update(self, values: dict[str, object]) -> list[str]:
        """Apply several settings at once, validating all of them before writing any.

        All-or-nothing on purpose. A settings form posts every field together,
        and half a form landing would leave the installation in a state the
        person never chose.

        Returns:
            The keys that actually changed, so a caller can decide whether
            anything needs restarting.

        Raises:
            KeyError: an unknown key was supplied.
            ValueError: a value failed validation. Nothing is written.
        """
        checked: dict[str, object] = {}
        for key, value in values.items():
            checked[key] = lookup_setting(key).validate(value)

        changed: list[str] = []
        for key, value in checked.items():
            if self.get(key) != value:
                changed.append(key)
        for key, value in checked.items():
            self.set(key, value)
        return changed

    def public(self) -> dict[str, object]:
        """Return every setting, with the identifying ones masked rather than echoed.

        There is no authentication in front of the settings page, so this
        endpoint answers anything that can reach the port. The serials are not
        passwords — the dongle broadcasts its own as a WiFi network name — but
        they identify one specific piece of hardware on one specific network,
        and handing the full set to any device on the LAN is a decision nobody
        made deliberately.

        Masking rather than blanking keeps the page usable: the owner can see
        which serial is configured and confirm it is the right one, without the
        value being readable by someone who did not already know it.
        """
        out: dict[str, object] = {}
        for spec in SETTINGS:
            value = self.get(spec.key)
            out[spec.key] = _mask(str(value)) if spec.secret else value
        return out


def _mask(value: str) -> str:
    """Show enough of a value for its owner to recognise it and no more.

    An empty value stays empty, so a page can tell "not configured yet" from
    "configured, and withheld".
    """
    if not value:
        return ""
    if len(value) <= 4:
        return "•" * len(value)
    return f"{value[:2]}{'•' * (len(value) - 4)}{value[-2:]}"


def describe() -> list[dict[str, object]]:
    """Return the registry as plain data, for a page that renders its own controls.

    Keeps the labels, help text, bounds and choices in one place. A page that
    hard-codes them drifts from the validation the moment either changes, and
    the drift shows up as a control that offers a value the server refuses.
    """
    return [
        {
            "key": spec.key,
            "kind": spec.kind,
            "label": spec.label,
            "help": spec.help,
            "choices": list(spec.choices),
            "lower": spec.lower,
            "upper": spec.upper,
            "secret": spec.secret,
            "default": spec.default,
        }
        for spec in SETTINGS
    ]
