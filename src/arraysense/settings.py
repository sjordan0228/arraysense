"""settings.py — the settings a person changes, kept in the database rather than a file.

Editing a TOML file over SSH is a reasonable thing to ask of someone who already
has a terminal open, and an unreasonable thing to ask of someone whose solar
monitor is a tablet on a wall. Everything here is settable from the running
service and is one value for the whole installation rather than per browser.
Most settings take effect on the next request. The two exceptions are read
once at startup and so wait for a restart: the connection group, merged over
the config file, which is why the setup flow's apply ends in a restart rather
than a promise; and the poll interval, which the collector reads when it
begins its loop — a temperature unit stored in local
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
import math
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from arraysense.tariff import EXAMPLE_ADJUSTMENTS, parse_adjustments, parse_bands

if TYPE_CHECKING:
    from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

Kind = Literal["str", "int", "float", "bool", "choice"]

# Where the installation is. Named here rather than spelled as literals at each
# call site, for the same reason the tariff keys are named in tariff.py: a key
# typed by hand in one place and mistyped in another reads as an unset setting
# rather than as an error.
SETTING_TIMEZONE = "site.timezone"
SETTING_LATITUDE = "site.latitude"
SETTING_LONGITUDE = "site.longitude"
SETTING_CONTACT_EMAIL = "site.contact_email"


def check_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA zone name, raising ValueError if the tz database has no such zone.

    Refused where it is typed rather than discovered later by an endpoint that
    then has to decide what to do about it. Every daily and monthly total in
    this project is cut at a midnight, and which midnight is exactly what this
    value chooses; an unusable one stored here would surface as a page that
    cannot answer at all, far from the box it was typed in.
    """
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"{name.strip()!r} is not a zone in the tz database — "
            "use a name like America/New_York, or leave it empty to follow the machine"
        ) from exc


# Deliberately loose. The full grammar of an address admits quoted local parts
# and bracketed literals that no owner is going to type, and every stricter
# pattern than this one refuses real addresses — plus-addressing and the long
# TLDs are the usual casualties. What it catches is the mistake worth catching:
# something with no @ at all, no domain, or whitespace through the middle.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


def check_email(value: str) -> str:
    """Refuse something that is plainly not an email address.

    This is a note to the owner rather than a credential, so the check exists
    to catch a typo at the box rather than to enforce RFC 5322 — a pattern
    strict enough to do the latter refuses addresses people actually have.
    """
    address = value.strip()
    if not _EMAIL.match(address):
        raise ValueError(f"{address!r} does not look like an email address")
    return address


def check_serial_device(value: str) -> None:
    """Refuse a serial device that pyserial would treat as a URL.

    pyserial dispatches any port string containing ``://`` to a URL handler —
    ``loop://``, ``socket://``, ``rfc2217://``, ``hwgrep://`` and the rest —
    each of which parses its own query string and raises undeclared exception
    types (a bare ``KeyError``, an ``re.error``) that the connection layer
    cannot catch by type. Such a value is accepted as a string, stored by both
    write paths, and then kills the collector on the next boot or turns a detect
    into a 500. A real RS485 adapter is a filesystem device path and never a
    URL, so this is a crisp thing to refuse at the door rather than an exception
    class to chase downstream. Enforced identically at the settings registry and
    the request models, so no entry point disagrees.
    """
    if "://" in value:
        raise ValueError("a serial device is a filesystem path, not a URL")


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
    # What the number means, rendered beside the control. A bare box asking for
    # a poll interval is a box that gets minutes typed into it. Empty where
    # there is nothing to say — a page appending "units" to a temperature unit
    # would be writing nonsense of its own.
    #
    # The money settings say "currency per month" and "currency per kWh" rather
    # than "kWh": they hold a rate, and a unit reading as a quantity of energy
    # invites the value to be typed as one.
    unit: str = ""
    # Offered, never enforced — a datalist rather than a choice list. The
    # difference is the whole point for the currency (#6): an owner whose
    # supplier bills in something not on any list must still be able to type
    # it, and one who has already typed their own must not find it replaced.
    # Use ``choices`` for a value the server genuinely refuses anything else
    # for, and this for one where a list is a convenience.
    suggestions: tuple[str, ...] = ()
    # Whether "not set" is a value this setting can hold, distinct from any
    # number it could hold. Latitude is the reason: 0.0 is the Gulf of Guinea,
    # a real place, so a zero standing in for "nobody has said" would put the
    # installation there — the same shape of mistake as a battery block with no
    # answer rendering as 0% state of charge. An optional setting defaults to
    # None, stores as an empty cell, decodes back to None, and travels the wire
    # as JSON null, so the two are distinguishable at every step.
    optional: bool = False
    lower: float | None = None
    upper: float | None = None
    secret: bool = False
    # Length cap for text. The default suits a hostname or a serial; anything
    # holding a structured value needs its own, and getting this wrong is not
    # theoretical — a flat 128 rejected the reference installation's own tariff
    # at 130 characters, which is three bands with their seasons.
    max_length: int = 128
    # Whether a newline is content rather than corruption. False for everything
    # that reaches a wire protocol; true for a value a person writes a line at
    # a time. The tariff is the only one so far, and ``parse_bands`` has always
    # accepted newlines between bands — it was this validator that refused
    # them, so a tariff typed the way it reads could not be saved at all.
    multiline: bool = False
    # An extra check for a value whose grammar the scalar types cannot express.
    # Raises ValueError with a message meant for the person typing. Without it
    # a malformed tariff saved, reported itself as saved, and only failed at
    # the far end — where the Costs page could tell it had no usable tariff but
    # not that one had been entered, so it said "no tariff entered" to somebody
    # looking straight at the one they had just typed.
    check: Callable[[str], object] | None = None

    def validate(self, value: object) -> object:
        """Return ``value`` coerced to this setting's type, or raise ValueError.

        Refuses rather than coerces across types. A refresh interval given as
        "soon" is a mistake, and quietly turning it into a number would hide the
        mistake somewhere far from where it was made.

        An ``optional`` setting accepts nothing at all — None, or the empty
        string a page posts from a box someone cleared — and returns None for
        it. That is what keeps an unset latitude out of the Gulf of Guinea: the
        emptiness travels as emptiness instead of being coerced to a number
        that means somewhere.

        Raises:
            ValueError: the value is the wrong type, outside the bounds, or not
                one of the allowed choices.
        """
        if self.optional and _unset(value):
            return None
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
            # These reach a wire protocol and a socket. A control character or
            # a zero-width space is not a hostname or a serial, and a value
            # that only looks blank would override a working one.
            allowed = {" ", "\n"} if self.multiline else {" "}
            if any(ch.isspace() or not ch.isprintable() for ch in value if ch not in allowed):
                raise ValueError(f"{self.key} must not contain control or invisible characters")
            if len(value) > self.max_length:
                raise ValueError(
                    f"{self.key} is too long at {len(value)} characters, "
                    f"the limit is {self.max_length}"
                )
            if self.check is not None and value.strip():
                try:
                    self.check(value)
                except ValueError as exc:
                    raise ValueError(f"{self.key}: {exc}") from exc
            return value
        # Booleans are integers in Python, so they have to be excluded before
        # the numeric check or `true` would quietly become 1.
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{self.key} must be a number, got {value!r}")
        # NaN and the infinities pass every bounds check, because each
        # comparison against NaN is false and infinity is only caught on one
        # side. A NaN poll interval was accepted here, stored, and then killed
        # the collector when it reached the event loop — with the HTTP API
        # still up, so the service looked healthy while collecting nothing.
        if not math.isfinite(value):
            raise ValueError(f"{self.key} must be a finite number, got {value!r}")
        # A whole-number setting given 5.5 was quietly stored as 5 — the same
        # coercion this docstring disclaims two paragraphs up, and the reason
        # the settings page and this validator disagreed about one field.
        if self.kind == "int" and value != int(value):
            raise ValueError(f"{self.key} must be a whole number, got {value!r}")
        number = int(value) if self.kind == "int" else float(value)
        if self.lower is not None and number < self.lower:
            raise ValueError(f"{self.key} must be at least {self.lower}, got {number}")
        if self.upper is not None and number > self.upper:
            raise ValueError(f"{self.key} must be at most {self.upper}, got {number}")
        return number

    def decode(self, stored: str) -> object:
        """Turn the stored text back into the type this setting is declared as.

        An optional setting stores "not set" as an empty cell, and that is the
        one reading which must not become a number: ``float("")`` raises, and
        the caller's fallback to the default would be right by accident today
        and wrong the moment an optional setting has a non-None default.
        """
        if self.optional and not stored.strip():
            return None
        if self.kind == "int":
            return int(float(stored))
        if self.kind == "float":
            return float(stored)
        if self.kind == "bool":
            return stored == "1"
        return stored


def _unset(value: object) -> bool:
    """Whether a posted value is somebody saying nothing rather than saying a number.

    A cleared box arrives as the empty string over JSON and as None from a
    caller in Python, and both mean the same thing. Whitespace counts as empty
    for the same reason it does everywhere else here: a value made of spaces is
    not a decision.
    """
    return value is None or (isinstance(value, str) and not value.strip())


# Common currencies, offered rather than imposed. Symbols first because that is
# what most bills show, then the ISO codes for the ones whose symbol is
# ambiguous or absent from a keyboard. The list is deliberately short — it is a
# convenience for the usual case, and every unusual one is still typable.
CURRENCY_SUGGESTIONS: tuple[str, ...] = (
    "$",
    "£",
    "€",
    "¥",
    "₹",
    "R$",
    "USD",
    "EUR",
    "GBP",
    "CAD",
    "AUD",
    "NZD",
    "JPY",
    "INR",
    "ZAR",
)

# Timezone choices for the dropdown. The empty string is first and means
# "follow the machine's own zone" — this is the default and must remain
# valid for existing installations. The rest are sorted IANA zone names.
_TIMEZONE_CHOICES: tuple[str, ...] = ("", *sorted(available_timezones()))


SETTINGS: tuple[SettingSpec, ...] = (
    # --- Site ---------------------------------------------------------------
    # Where the installation is, as against who is looking at it. The inverter
    # is in one place; a phone that has travelled is not, and every one of
    # these was being answered by the browser or by the machine's own settings
    # before they existed here.
    SettingSpec(
        key=SETTING_TIMEZONE,
        kind="choice",
        default="",
        label="Timezone",
        help=(
            "The zone the installation lives in, as an IANA name like "
            "America/New_York. It decides where midnight falls and which hours "
            "a rate band covers, so it belongs to the site rather than to "
            "whoever is looking. Leave it empty to follow the machine's own "
            "zone, which is what happens today."
        ),
        # The choices are the tz database itself, so membership is the check —
        # a `check=` callback would never fire, because validate() returns
        # inside the choice branch before reaching it.
        choices=_TIMEZONE_CHOICES,
        max_length=64,
    ),
    SettingSpec(
        key=SETTING_LATITUDE,
        kind="float",
        # Not 0.0. That is a real latitude, and an install that has said
        # nothing has not said it is on the equator.
        default=None,
        optional=True,
        lower=-90.0,
        upper=90.0,
        unit="decimal degrees",
        label="Latitude",
        help=(
            "Decimal degrees, negative south of the equator. Leave it empty if "
            "you would rather not record it — empty means not recorded, and is "
            "kept distinct from zero, which is a place in the Gulf of Guinea."
        ),
    ),
    SettingSpec(
        key=SETTING_LONGITUDE,
        kind="float",
        default=None,
        optional=True,
        lower=-180.0,
        upper=180.0,
        unit="decimal degrees",
        label="Longitude",
        help=(
            "Decimal degrees, negative west of Greenwich. Leave it empty if "
            "you would rather not record it."
        ),
    ),
    SettingSpec(
        key=SETTING_CONTACT_EMAIL,
        kind="str",
        default="",
        label="Contact email",
        help=(
            "Who to reach about this installation. Nothing is sent to it yet; "
            "it is recorded so a future alert has somewhere to go."
        ),
        # Masked on read like the serials, and for the same reason: the page in
        # front of it has no authentication, so an address typed here would
        # otherwise be readable by anything that can reach the port.
        secret=True,
        check=check_email,
    ),
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
        unit="seconds",
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
        unit="seconds",
        label="Poll interval",
        help=(
            "Seconds between reads of the inverter. The dongle answers at its "
            "own pace, so asking faster than about ten seconds mostly produces "
            "reads that overlap the previous one."
        ),
    ),
    # --- Tariff -------------------------------------------------------------
    # What the owner pays. Nothing here has a default that prices anything: an
    # install that has entered no tariff shows energy and no money at all,
    # because a guessed rate produces a savings figure that reads as measured.
    SettingSpec(
        key="tariff.bands",
        # Several bands, each with a name, a price, its hours and its season.
        max_length=2000,
        # The help text below says "one band per line", and it has to be true:
        # parse_bands splits on newlines and always has.
        multiline=True,
        # Checked with the real parser, so the page reports a malformed band
        # beside the box it was typed in rather than storing it and letting
        # the Costs page discover it as an absence.
        check=parse_bands,
        kind="str",
        default="",
        label="Rate bands",
        help=(
            "One band per line, or separated by semicolons: name, price per "
            "kWh, and the hours it applies to, separated by pipes. A band may "
            "list several ranges separated by commas, and a range may run "
            "through midnight. For example: "
            "Peak | 0.34 | 16:00-21:00; Off-peak | 0.11 | 21:00-16:00 — "
            "leave it empty and no money is shown anywhere."
        ),
    ),
    SettingSpec(
        key="tariff.fixed_monthly",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=100000.0,
        # Money for a month, not a count of months. Named as the rate it is so
        # the box cannot be read as asking how many.
        unit="currency per month",
        label="Fixed monthly charge",
        help=(
            "The connection or supply charge, payable whatever the usage. It "
            "is shared across whatever period is being shown and added once to "
            "an estimated bill. It never appears in the savings figure, since "
            "no amount of solar avoids it."
        ),
    ),
    SettingSpec(
        key="tariff.export_per_kwh",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=1000.0,
        # The value is money for each kWh exported, not an amount of energy.
        # "kWh" alone beside the box reads as the second, which is how a rate
        # ends up typed as a quantity.
        unit="currency per kWh",
        label="Export credit",
        help=(
            "What the supplier pays for a kWh sent back. Leave it at zero if "
            "yours pays nothing, and no credit is shown rather than a zero one."
        ),
    ),
    SettingSpec(
        key="tariff.adjustments",
        # One line per billing month, at roughly thirty characters a line. A
        # supplier publishes twelve a year and the old lines are what let an
        # old month be re-priced, so this has to hold years of them.
        max_length=4000,
        multiline=True,
        # Checked with the real parser, for the same reason the bands are: a
        # month that stored but would not read is a bill silently unadjusted.
        check=parse_adjustments,
        kind="str",
        default="",
        label="Monthly adjustment factors",
        help=(
            "PCRF and SCRF, the per-kWh factors the supplier re-sets every "
            "month and charges on top of the band rate. One line per billing "
            "month: the month as YYYY-MM, then PCRF, then SCRF, separated by "
            "pipes. PCRF is often negative. For example: "
            f"{EXAMPLE_ADJUSTMENTS} — leave a factor empty if it has not been "
            "published, and leave the whole box empty if your supplier charges "
            "neither. A month with nothing recorded is priced at the base rate "
            "and shown as unadjusted rather than as adjusted by nothing."
        ),
    ),
    SettingSpec(
        key="tariff.currency",
        kind="str",
        default="$",
        # Suggested, never restricted. A closed list would make an unusual
        # currency unrepresentable, and the page renders these as a datalist so
        # a value already typed is offered alongside rather than replaced.
        # Both a symbol and a code have to keep working: the page spaces them
        # differently, "$12.30" against "USD 12.30", so neither may be
        # normalised into the other.
        suggestions=CURRENCY_SUGGESTIONS,
        label="Currency",
        help=(
            "Written in front of every money figure. A symbol or a code — whatever your bill uses."
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
    SettingSpec(
        key="connection.driver",
        kind="str",
        default="",
        label="Inverter family",
        help=(
            "Which driver family reads this inverter. Empty keeps the config "
            "file's choice. Applies at the next collector restart."
        ),
    ),
    SettingSpec(
        key="connection.transport",
        kind="str",
        default="",
        choices=("dongle", "modbus_serial"),
        label="Connection type",
        help=(
            "How the inverter is reached. Empty keeps the config file's "
            "choice. Applies at the next collector restart."
        ),
    ),
    SettingSpec(
        key="connection.serial_device",
        kind="str",
        default="",
        label="Serial device",
        help=(
            "Device path for the RS485 adapter — a udev symlink or a "
            "/dev/serial/by-id path survives replugging; /dev/ttyUSB0 may not."
        ),
        check=check_serial_device,
    ),
    SettingSpec(
        key="connection.serial_baud",
        kind="int",
        default=19200,
        lower=1,
        upper=1000000,
        label="Serial baud rate",
        help="19200 is the LuxPower convention; change it only if yours differs.",
    ),
    SettingSpec(
        key="connection.serial_unit_id",
        kind="int",
        default=1,
        lower=1,
        upper=247,
        label="Modbus unit id",
        help="Which unit answers on the bus. 0 is broadcast and never answers reads.",
    ),
    SettingSpec(
        key="connection.model",
        kind="str",
        default="",
        label="Inverter model",
        help="Which model of the chosen family this installation is.",
    ),
    SettingSpec(
        key="connection.battery_source",
        kind="str",
        default="",
        choices=("", "relayed", "none"),
        label="Battery source",
        help=(
            "Where battery data comes from: relayed through the inverter, or "
            "no communicating battery. Empty derives it from the driver."
        ),
    ),
)

_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SETTINGS}


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
        """Attach to an open store.

        ``SqliteStore`` creates the settings table with the rest of the schema
        before request handling begins. This constructor must stay read-only:
        API routes construct it per request — cheap ones on the event-loop
        thread, tier-scanning ones on threadpool workers over a read view — and
        even idempotent schema DDL is a lock-taking operation that does not
        belong in a read on any of those paths.

        Takes the store rather than a raw connection so settings share its
        transaction and its lifetime: a settings write and a reading write in
        the same moment commit together, and nothing outlives the database it
        was reading from.
        """
        self._conn: sqlite3.Connection = store._conn

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

    def set_many(self, values: dict[str, object]) -> None:
        """Validate every value, then store all of them in one transaction.

        The apply endpoint writes several connection settings as one act, and
        one act is what it has to be: a batch that validated four keys, wrote
        two and refused the third leaves an overlay the next boot assembles
        from halves — a stored transport with no device path is a page-made
        crash loop. Nothing is written until every value has passed its spec.
        """
        checked: list[tuple[str, str]] = []
        for key, value in values.items():
            spec = lookup_setting(key)
            valid = spec.validate(value)
            if valid is None:
                stored = ""
            else:
                stored = "1" if valid is True else "0" if valid is False else str(valid)
            checked.append((key, stored))
        with self._conn:
            for key, stored in checked:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, stored),
                )

    def set(self, key: str, value: object) -> None:
        """Validate ``value`` against its spec and store it.

        Raises:
            KeyError: no setting is registered under that key.
            ValueError: the value is the wrong type or outside its bounds.
        """
        spec = lookup_setting(key)
        checked = spec.validate(value)
        # An optional setting that holds nothing stores an empty cell, which is
        # what ``decode`` reads back as None. str(None) would write the four
        # characters "None", and a latitude of "None" decodes to nothing
        # useful — or worse, to a number, if a later reader is generous.
        if checked is None:
            stored = ""
        else:
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

        **This is form safety, not confidentiality, and the difference matters.**
        The same unauthenticated endpoint accepts writes, so a client on the
        network can point ``connection.dongle_host`` at a listener it controls
        and read both serials off the wire at the next poll — the protocol
        carries them in clear ASCII. Masking stops a serial being read off the
        page; only authentication stops it being taken. Nothing here should be
        described to an owner as protecting the value.
        """
        out: dict[str, object] = {}
        for spec in SETTINGS:
            value = self.get(spec.key)
            # Masked only when there is text to mask. ``str(None)`` would send
            # the word "None" through the masker and back out as "N••e", which
            # a page would render as a configured value.
            out[spec.key] = _mask(value) if spec.secret and isinstance(value, str) else value
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
            # Both are needed by a page that wants to refuse exactly what the
            # server refuses. Without max_length a text box happily submits a
            # value the server then rejects; without multiline it cannot tell
            # which field is a line and which is a paragraph.
            "max_length": spec.max_length,
            "multiline": spec.multiline,
            # What the number means, and what a value might reasonably be.
            # Emitted on every field, empty where there is nothing to say, so
            # a page reads them unconditionally rather than branching on
            # whether the server happened to mention them.
            "unit": spec.unit,
            # Not ``choices``. A page must offer these without refusing
            # anything else, or the currency becomes a closed list and an
            # unusual one becomes unrepresentable.
            "suggestions": list(spec.suggestions),
            # Whether an empty control means "not set" rather than zero. A page
            # that renders an optional number has to send the empty string back
            # for an emptied box and show a blank for null, instead of drawing
            # a 0 that would claim the site is on the equator.
            "optional": spec.optional,
        }
        for spec in SETTINGS
    ]
