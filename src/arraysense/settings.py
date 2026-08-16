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

import contextlib
import errno
import logging
import math
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from datetime import date, datetime
from typing import TYPE_CHECKING, Literal
from zoneinfo import available_timezones

from arraysense.auth import AUTH_PASSWORD_KEY
from arraysense.energy import resolve_zone
from arraysense.panels import EXAMPLE_STRINGS, StringSpec, parse_strings
from arraysense.store.schema import INVERTER_TIERS, MODULE_TIERS, Tier
from arraysense.tariff import EXAMPLE_ADJUSTMENTS, parse_adjustments, parse_bands

if TYPE_CHECKING:
    from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

Kind = Literal["str", "int", "float", "bool", "choice"]

# Where the installation is. Named here rather than spelled as literals at each
# call site, for the same reason the tariff keys are named in tariff.py: a key
# typed by hand in one place and mistyped in another reads as an unset setting
# rather than as an error.
CONFIG_VERSION_KEY = "efficiency.config_version"
CONFIG_VALID_FROM_KEY = "efficiency.config_valid_from"


# Everything about a string except when it was pointed where. Named from the
# dataclass rather than listed by hand so a field added to the grammar joins the
# comparison automatically — one forgotten here is a change that silently fails
# to invalidate the history it invalidates. Deliberately not built by replacing
# the schedule with an empty tuple: that produces a StringSpec whose ``tilt_at``
# raises IndexError, and a throwaway object that cannot answer the question the
# type promises to answer is a trap left lying about for the next reader.
_GEOMETRY_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(StringSpec) if f.name != "tilt_schedule"
)


def _schedule_reach(before: str, after: str) -> date | None:
    """How far back a change to the array description reaches.

    ``None`` when nothing a stored day was scored against moved. ``date.min``
    when the change reaches the whole history — a panel count, an azimuth, a
    string added or removed, or a tilt that was already in force being altered.
    Any other date is the first day the two descriptions disagree, and every day
    before it keeps the score it already has.

    That last case is the one the seasonal mount needs. Appending "40@2027-10-01"
    to a string says nothing whatever about 2026, so 2026 must not be rescored —
    and rescoring it is what threw away the yearly comparison that would have
    told the owner whether adjusting the mount was worth doing.

    Anything unparseable reaches everywhere. A description that cannot be read
    cannot be shown to have left the past alone, and guessing in the generous
    direction here would keep stale scores on the page.
    """
    try:
        old = {s.name: s for s in parse_strings(before)}
        new = {s.name: s for s in parse_strings(after)}
    except ValueError:
        return date.min
    if set(old) != set(new):
        return date.min

    earliest: date | None = None
    for name, new_spec in new.items():
        old_spec = old[name]
        if any(getattr(old_spec, f) != getattr(new_spec, f) for f in _GEOMETRY_FIELDS):
            return date.min
        if old_spec.tilt_schedule == new_spec.tilt_schedule:
            continue
        # Both are step functions, so they can only part company at a step. If
        # they already differ before the first one, the change is retrospective
        # and reaches everything.
        if old_spec.tilt_schedule[0].degrees != new_spec.tilt_schedule[0].degrees:
            return date.min
        steps = sorted(
            {
                entry.effective_from
                for schedule in (old_spec.tilt_schedule, new_spec.tilt_schedule)
                for entry in schedule
                if entry.effective_from is not None
            }
        )
        for step in steps:
            if old_spec.tilt_at(step) != new_spec.tilt_at(step):
                earliest = step if earliest is None else min(earliest, step)
                break
    return earliest


_EFFICIENCY_SCORER_REVISION_KEY = "efficiency.scorer_revision"
PANELS_STRINGS_KEY = "panels.strings"
SETTING_TIMEZONE = "site.timezone"
SETTING_LATITUDE = "site.latitude"
SETTING_LONGITUDE = "site.longitude"
SETTING_CONTACT_EMAIL = "site.contact_email"
WEATHER_INTERVAL_KEY = "collector.weather_interval"
# The optional Emporia module's enable. Named here beside the other feature keys
# rather than inside the module, because the settings registry is what renders
# the page and validates a write; a module holding its own flag would be a
# second answer to the same question.
EMPORIA_ENABLED_KEY = "emporia.enabled"
EMPORIA_INTERVAL_KEY = "emporia.interval"
# Charger control. How much autonomy the module has is the owner's choice;
# the floor, the ceiling and the audit are not, because a charge rate
# persists for ever once set and nothing at Emporia's end will put it back.
CHARGER_AUTHORITY_KEY = "emporia.charger_authority"
CHARGE_FLOOR_KEY = "emporia.charge_floor_a"
CHARGE_CEILING_KEY = "emporia.charge_ceiling_a"
CHARGE_DEFAULT_KEY = "emporia.charge_default_a"
CHARGE_OVERRIDE_MINUTES_KEY = "emporia.charge_override_minutes"
# When the current manual override lapses, as a unix epoch. Written by the
# service rather than chosen, like the efficiency rescore floor: it has to
# survive a restart, because an owner who set a rate by hand ten minutes ago
# should not have the module take the wheel back because the process bounced.
CHARGE_OVERRIDE_UNTIL_KEY = "emporia.charge_override_until"
# The house-draw warning. Not an Emporia setting: the threshold is compared
# against the inverter's own load figure, so it works on an installation that
# has never heard of Emporia — which only supplies the names of the culprits.
HIGH_USAGE_WATTS_KEY = "alerts.high_usage_watts"
# The daily backup, which used to be compiled into manage.py and overridden only
# by flags nobody types twice. manage.py cannot import this module — it runs on
# the distribution's Python 3.8 while the package needs 3.12 — so it reads these
# over HTTP and keeps its own copy of the defaults; ``tests/test_manage.py``
# fails if the two ever disagree.
BACKUP_ENABLED_KEY = "backup.enabled"
BACKUP_DIRECTORY_KEY = "backup.directory"
BACKUP_KEEP_KEY = "backup.keep"
BACKUP_HOUR_KEY = "backup.hour"
BACKUP_MINUTE_KEY = "backup.minute"
RETENTION_ENABLED_KEY = "retention.enabled"
RETENTION_RAW_DAYS_KEY = "retention.raw_days"
RETENTION_MINUTE_DAYS_KEY = "retention.minute_days"


def _tier_keep_days(tiers: tuple[Tier, ...], name: str) -> int:
    """Read one finite retention default from the schema's tier declaration.

    The schema is where the intended lifetime belongs. Settings only decide
    whether it is enforced, so repeating its numbers here would make an edit to
    the schema silently leave new installations on the old retention period.
    """
    for tier in tiers:
        if tier.name == name:
            if not isinstance(tier.keep_days, int):
                raise ValueError(f"tier {name!r} has no finite retention period")
            return tier.keep_days
    raise ValueError(f"tier {name!r} is not declared")


_INVERTER_RAW_KEEP_DAYS = _tier_keep_days(INVERTER_TIERS, "full")
_MODULE_RAW_KEEP_DAYS = _tier_keep_days(MODULE_TIERS, "full")
if _INVERTER_RAW_KEEP_DAYS != _MODULE_RAW_KEEP_DAYS:
    raise RuntimeError("the raw inverter and module tiers must share one retention setting")
RETENTION_RAW_DAYS_DEFAULT = _INVERTER_RAW_KEEP_DAYS
RETENTION_MINUTE_DAYS_DEFAULT = _tier_keep_days(INVERTER_TIERS, "minute")


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


# The service account the packaged units run as. Named once so the two remedies
# below cannot drift from packaging/arraysense-backup.service.
_SERVICE_USER = "arraysense"


def check_backup_directory(value: str) -> str:
    """Refuse a backup destination the backup would later fail to write.

    A path that only fails at 03:15 fails unattended, and the failure this
    catches has already happened on a real machine: the destination was
    outside the unit's writable set, every run died with "Read-only file
    system" on the lock it could not create, and the CLI reported "another
    backup is running" — which was false, and sent somebody looking for a
    process that did not exist. Refusing at the moment the path is typed is
    the only point where there is a person present to read the remedy.

    The probe is a real file, created and removed, rather than ``os.access``.
    Permission bits are not the only thing between this service and a write:
    ``ProtectSystem=strict`` mounts everything outside the unit's declared
    writable paths read-only, which no bit on the directory records, and the
    kernel answers that with EROFS whoever asks. Each cause gets its own
    remedy, because the wrong one is worse than none — a chown will not fix a
    read-only bind mount, and no amount of ReadWritePaths will fix an owner.

    This is deliberately not the registry's ``check=``. Those are pure
    functions of the text, the same answer on every machine; this one asks the
    filesystem, and a form posting an unchanged value back must not be refused
    for a fault it did not introduce. The API calls it when the value changes.
    """
    path = value.strip()
    if not path:
        raise ValueError(
            "a backup destination is needed; leaving it empty would write the archive "
            "into whatever directory the backup happened to start in"
        )
    if not os.path.isabs(path):
        raise ValueError(
            f"{path!r} is not an absolute path, and systemd runs the backup with no "
            "working directory of its own to resolve it against"
        )
    if not os.path.isdir(path):
        if os.path.exists(path):
            raise ValueError(f"{path} is not a directory")
        raise ValueError(
            f"{path} does not exist. Create it with the right owner: "
            f"sudo install -d -o {_SERVICE_USER} -g {_SERVICE_USER} -m 0750 {path}"
        )
    try:
        handle, probe = tempfile.mkstemp(dir=path, prefix=".arraysense-write-test-")
    except OSError as exc:
        raise ValueError(_why_unwritable(path, exc)) from exc
    os.close(handle)
    # Leaving it would make the rotation count a file that is not a backup.
    with contextlib.suppress(OSError):
        os.remove(probe)
    return path


def _why_unwritable(path: str, exc: OSError) -> str:
    """Turn a failed write into the one remedy that addresses its actual cause."""
    if exc.errno == errno.EROFS:
        return (
            f"{path} is read-only for this service. The backup runs under "
            "ProtectSystem=strict, so a directory outside the unit's writable set "
            "fails with 'Read-only file system' however good the permissions are. "
            "Give it a drop-in — [Service] then ReadWritePaths=" + path + " — for "
            "arraysense-backup.service and arraysense.service, then systemctl "
            "daemon-reload"
        )
    if exc.errno in (errno.EACCES, errno.EPERM):
        return (
            f"{path} exists but this service cannot write there. Hand it to the "
            f"service account: sudo chown {_SERVICE_USER}:{_SERVICE_USER} {path}"
        )
    return f"{path} could not be written to: {exc.strerror or exc}"


@dataclass(frozen=True)
class SettingSpec:
    """One settable value: its type, its bounds, and how to describe it to a person.

    ``secret`` marks a value the API masks rather than echoes. The dongle and
    inverter serials are not passwords, but they identify specific hardware and
    reads stay open even when a password is set, so they go out with their
    middle replaced — enough for the owner to recognise which serial is
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
        help="How often the page asks for new readings.",
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
        # What to ask for is explained once, for both transports, in the
        # Collection introduction on the settings page. Saying it again beside
        # the box is the same sentence twice.
        help="",
    ),
    SettingSpec(
        key="collector.weather_interval",
        kind="float",
        default=900.0,
        lower=300.0,
        upper=86400.0,
        unit="seconds",
        label="Weather interval",
        help=(
            "Seconds between weather fetches. Open-Meteo refreshes its current "
            "conditions about every fifteen minutes, so asking faster stores "
            "near-identical points. Weather is fetched only while a location "
            "is set under This installation."
        ),
    ),
    # --- Backup --------------------------------------------------------------
    # The daily compressed copy of the database. All five of these were
    # compiled into manage.py, where changing one meant editing a systemd unit
    # over SSH — which is the same argument that put every other setting here.
    # The timer now fires every fifteen minutes and asks these whether there is
    # anything to do, so the schedule is answered by the installation rather
    # than by an OnCalendar line only root can edit.
    SettingSpec(
        key=BACKUP_ENABLED_KEY,
        kind="bool",
        default=True,
        label="Daily backup",
        help=(
            "Whether the timer writes a compressed copy of the database each "
            "day. Turning it off stops the scheduled run only — "
            "'arraysense backup' by hand still works, so a backup is always "
            "one command away."
        ),
    ),
    SettingSpec(
        key=BACKUP_DIRECTORY_KEY,
        kind="str",
        default="/var/backups/arraysense",
        label="Backup directory",
        help=(
            "Where the compressed copies are written. It should be on a "
            "different disk from the database — a backup on the same disk is "
            "protection against nothing. The directory has to exist and be "
            "writable by the service before it can be saved here."
        ),
        # Checked when it changes, against the real filesystem, by the API. Not
        # a registry check=: those answer the same on every machine, and this
        # one asks the disk in front of it.
    ),
    SettingSpec(
        key=BACKUP_KEEP_KEY,
        kind="int",
        default=14,
        # Never zero: rotation keeps the newest N, so zero deletes every copy
        # there is immediately after writing one.
        lower=1,
        upper=365,
        unit="copies",
        label="Backups kept",
        help=(
            "How many daily copies to keep. The oldest are removed after a new "
            "one has been written and verified, never before. Fourteen at "
            "roughly 23 MB each is about a third of a gigabyte."
        ),
    ),
    SettingSpec(
        key=BACKUP_HOUR_KEY,
        kind="int",
        default=3,
        lower=0,
        upper=23,
        label="Backup hour",
        help=(
            "The hour, on the installation's own clock, after which the day's "
            "backup may run. The timer checks every fifteen minutes, so the "
            "run starts at the first quarter hour at or after this time. "
            "A time in the last quarter-hour block of the day (23:46-23:59)"
            "runs at 23:45, the last firing before midnight, rather than "
            "being skipped."
        ),
    ),
    SettingSpec(
        key=BACKUP_MINUTE_KEY,
        kind="int",
        default=15,
        lower=0,
        upper=59,
        label="Backup minute",
        help=(
            "Minutes past the hour. A machine that was asleep at that time "
            "backs up when it wakes, rather than skipping the day."
        ),
    ),
    # --- Retention ----------------------------------------------------------
    # Raw history is useful only until the coarser copies behind it hold every
    # bucket, and deleting it is irreversible. It therefore stays opt-in until
    # an owner has chosen to let the scheduled backup satisfy that gate.
    SettingSpec(
        key=RETENTION_ENABLED_KEY,
        kind="bool",
        default=False,
        label="Enforce data retention",
        help=(
            "Delete old raw readings only after a current backup exists and "
            "the minute and hourly tiers already hold every bucket. It is off "
            "by default because deletion cannot be undone."
        ),
    ),
    SettingSpec(
        key=RETENTION_RAW_DAYS_KEY,
        kind="int",
        default=RETENTION_RAW_DAYS_DEFAULT,
        lower=2,
        upper=3650,
        unit="days",
        label="Raw data kept",
        help=(
            "How long full-cadence inverter and battery-module readings are "
            "kept before their covered coarse copies can replace them."
        ),
    ),
    SettingSpec(
        key=RETENTION_MINUTE_DAYS_KEY,
        kind="int",
        default=RETENTION_MINUTE_DAYS_DEFAULT,
        lower=7,
        upper=3650,
        unit="days",
        label="Minute data kept",
        help=(
            "How long minute-resolution inverter readings are kept before "
            "their covered hourly copies can replace them."
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
    # --- Solar panels --------------------------------------------------------
    # The array, one line per string, in the grammar panels.py owns. Stored as
    # text for the same reason the tariff is: a repeating structure in a flat
    # registry, composed by the page and parsed only in Python. Empty means no
    # array configured, which is a state and not an error.
    SettingSpec(
        key="panels.strings",
        kind="str",
        default="",
        multiline=True,
        max_length=4000,
        label="Array strings",
        help=(
            "One line per string: name | MPPT | panels | watts each | tilt° | "
            "azimuth° — then optional key=value pairs (temp_coeff, noct, "
            "mounting, bifacial, installed, degradation, vmp, voc, wire_awg, "
            "wire_run_ft, note). "
            "An adjustable mount can give tilt as a schedule instead of one "
            "angle: 25,40@2027-10-01 means 25° until 1 October 2027 and 40° "
            "from it. Dates must run forwards, only the first angle may omit "
            "one, and every hour is scored against the angle the array really "
            "stood at — so adjusting the mount no longer discards the "
            "performance history. "
            "For example: " + EXAMPLE_STRINGS.replace("\n", "  •  ")
        ),
        check=parse_strings,
    ),
    # --- Battery bank --------------------------------------------------------
    # Static specs the efficiency accounting reads; live BMS values win
    # wherever they exist. Everything optional with a named default, because
    # every value has a sane fallback and a wall of required fields would be
    # the setup burden the panels grammar just avoided.
    SettingSpec(
        key="battery.chemistry",
        kind="str",
        default="lifepo4",
        choices=("lifepo4", "other"),
        label="Battery chemistry",
        help="LiFePO4 is every current EG4 pack; 'other' only changes labels today.",
    ),
    SettingSpec(
        key="battery.count",
        kind="int",
        default=0,
        lower=0,
        upper=64,
        label="Batteries in the bank",
        help="0 means unstated — bank arithmetic then stays off rather than guessing.",
    ),
    SettingSpec(
        key="battery.capacity_kwh_each",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=100.0,
        unit="kWh",
        label="Capacity per battery",
        help="Nameplate, not derived from live voltage — that figure drifts 7% over a cycle.",
    ),
    SettingSpec(
        key="battery.round_trip_pct",
        kind="float",
        default=91.4,
        lower=50.0,
        upper=100.0,
        unit="%",
        label="Round-trip efficiency",
        help="Defaults to the reference installation's measured figure, not a datasheet.",
    ),
    SettingSpec(
        key="battery.min_soc_pct",
        kind="float",
        default=10.0,
        lower=0.0,
        upper=90.0,
        unit="%",
        label="Minimum state of charge",
        help="The floor the inverter is configured to hold; usable capacity ends here.",
    ),
    SettingSpec(
        key="battery.max_charge_a",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=2000.0,
        unit="A",
        label="Max charge current",
        help="0 means unstated. Context for charge-limit detection, not a command.",
    ),
    SettingSpec(
        key="battery.max_discharge_a",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=2000.0,
        unit="A",
        label="Max discharge current",
        help="0 means unstated.",
    ),
    SettingSpec(
        key="battery.heater_w",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=2000.0,
        unit="W",
        label="Heater draw",
        help="Per battery, while heating. 0 for packs without heaters.",
    ),
    SettingSpec(
        key="battery.heater_on_c",
        kind="float",
        default=5.0,
        lower=-30.0,
        upper=20.0,
        unit="°C",
        label="Heater on below",
        help="",
    ),
    SettingSpec(
        key="battery.heater_off_c",
        kind="float",
        default=10.0,
        lower=-20.0,
        upper=30.0,
        unit="°C",
        label="Heater off above",
        help="",
    ),
    SettingSpec(
        key="battery.idle_draw_w",
        kind="float",
        default=0.0,
        lower=0.0,
        upper=500.0,
        unit="W",
        label="BMS idle draw",
        help="Per battery. 0 means unstated.",
    ),
    SettingSpec(
        key="efficiency.config_version",
        kind="int",
        default=0,
        lower=0,
        upper=2147483647,
        label="Efficiency config version",
        help=(
            "Bumped automatically when the array or battery config changes, so "
            "stored efficiency days are recomputed against the new settings. "
            "Leave it alone — it is not a setting to choose."
        ),
    ),
    SettingSpec(
        key="efficiency.config_valid_from",
        kind="int",
        default=0,
        lower=0,
        upper=253402300799,
        label="Efficiency rescore floor (day epoch)",
        help=(
            "The earliest day still bound to the current config version. Days "
            "before it keep the score they already have, because the change "
            "that moved the version did not reach back that far. Leave it "
            "alone — it is not a setting to choose."
        ),
    ),
    SettingSpec(
        key="battery.installed",
        kind="str",
        default="",
        max_length=7,
        label="Bank installed (YYYY-MM)",
        help="For future capacity-fade context; empty is fine.",
    ),
    # --- Alerts -------------------------------------------------------------
    SettingSpec(
        key=HIGH_USAGE_WATTS_KEY,
        kind="int",
        default=0,
        lower=0,
        upper=100000,
        unit="W",
        label="Warn when the house draws more than",
        help=(
            "Show a warning when the house is drawing more than this. Zero is "
            "off, which is the default — a threshold nobody chose would warn "
            "about a kettle. The figure compared against it is the inverter's "
            "own load reading, which arrives every eleven seconds, so the "
            "warning does not wait on anything else. If the Emporia module is "
            "on, the warning also names the circuits responsible; without it "
            "the warning still appears and simply cannot say what caused it."
        ),
    ),
    # --- Modules ------------------------------------------------------------
    SettingSpec(
        key=CHARGER_AUTHORITY_KEY,
        kind="choice",
        choices=("app", "advisory", "limited", "full"),
        default="app",
        label="Who controls the EV charger",
        help=(
            "app leaves it to the Emporia app and this service never writes to "
            "the charger — the default, because installing this is not the same "
            "as asking it to take over your car charger. advisory lets it "
            "propose a rate and change nothing. limited lets it set a rate "
            "between your floor and ceiling. full also lets it stop and start "
            "charging. Whichever you pick, the floor, the ceiling, the audit "
            "trail and the restore on startup all still apply — those are not "
            "settings, because a charge rate persists for ever once set and "
            "nothing at Emporia's end will ever put it back. Only one "
            "controller should have the charger: Emporia ships four of its own, "
            "and the Charger page says which are switched on."
        ),
    ),
    SettingSpec(
        key=CHARGE_FLOOR_KEY,
        kind="int",
        default=6,
        lower=1,
        upper=80,
        unit="A",
        label="Never charge below",
        help=(
            "The least current the module will ever command. Six amps is the "
            "usual minimum a car will accept at all; below it some simply stop "
            "charging rather than charge slowly."
        ),
    ),
    SettingSpec(
        key=CHARGE_CEILING_KEY,
        kind="int",
        default=32,
        lower=1,
        upper=80,
        unit="A",
        label="Never charge above",
        help=(
            "The most current the module will ever command. Your charger's own "
            "maximum still wins if it is lower — a command it cannot honour is "
            "a command whose effect nobody can predict."
        ),
    ),
    SettingSpec(
        key=CHARGE_DEFAULT_KEY,
        kind="int",
        default=32,
        lower=1,
        upper=80,
        unit="A",
        label="Put the charger back to",
        help=(
            "Where the rate is returned to when the module has no reason to "
            "hold it anywhere else — including after a restart. This is what "
            "stops a service that died mid-throttle leaving a car at the floor "
            "all night. It only ever restores a rate it set itself; one you "
            "moved by hand is left alone."
        ),
    ),
    SettingSpec(
        key=CHARGE_OVERRIDE_UNTIL_KEY,
        kind="int",
        default=0,
        lower=0,
        upper=253402300799,
        label="Manual override lapses at (epoch)",
        help=(
            "Set by the service when you change the charge rate yourself, so "
            "the override survives a restart. Leave it alone — it is not a "
            "setting to choose."
        ),
    ),
    SettingSpec(
        key=CHARGE_OVERRIDE_MINUTES_KEY,
        kind="int",
        default=120,
        lower=1,
        upper=1440,
        unit="minutes",
        label="A manual change holds for",
        help=(
            "How long the module keeps its hands off after you set a rate "
            "yourself. Somebody standing at the car knows something this "
            "service does not."
        ),
    ),
    SettingSpec(
        key=EMPORIA_INTERVAL_KEY,
        kind="int",
        default=60,
        lower=10,
        upper=3600,
        unit="seconds",
        label="Emporia poll interval",
        help=(
            "How often to read circuit power from Emporia. Sixty seconds by "
            "default, which is what Emporia's own Home Assistant integration "
            "uses. The round trip was measured at 133-206 ms, so this is not "
            "limited by speed: Emporia publishes no rate limit, and a shorter "
            "interval means more calls against a quota nobody can see. Lower it "
            "by measurement rather than by hope."
        ),
    ),
    SettingSpec(
        key=EMPORIA_ENABLED_KEY,
        kind="bool",
        default=False,
        label="Emporia circuit monitoring",
        help=(
            "Read circuit-level power from Emporia Vue monitors and an Emporia "
            "EV charger. Off by default. This is the one part of the service "
            "that needs the internet: Emporia offers no local access, so every "
            "circuit reading crosses their cloud. Solar collection is never "
            "affected — an unreachable Emporia costs the inverter nothing, and "
            "turning this off stops all of it within one poll interval."
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

    # A change to either of these changes what the array is expected to produce,
    # which makes every efficiency day scored under the old description stale.
    # The version is what marks them so: the summary pass rescores a day whose
    # stored version is not the current one. Bumping is the writer's job because
    # only the writer knows a change happened — a reader comparing settings to
    # scored days would have to keep a copy of the settings to compare against,
    # which is the same problem again one level down.
    _VERSIONED_PREFIXES = ("panels.", "battery.")
    # Not a prefix: "site." also holds a contact address and other things that
    # change nothing about the sun. These three do. Latitude and longitude place
    # the sun in the sky, and the zone decides where one day stops and the next
    # begins -- a day scored before a correction to any of them was scored
    # against a different sky than the one the array was under.
    _VERSIONED_KEYS = (SETTING_LATITUDE, SETTING_LONGITUDE, SETTING_TIMEZONE)

    def _bump_config_version(self, keys: Iterable[str], previous: str | None = None) -> None:
        """Advance the efficiency config version if any of ``keys`` describes the array.

        Called inside the caller's transaction, so a write that fails validation
        leaves the version alone and days scored under it stay valid.

        ``previous`` is the text ``panels.strings`` held before this write, and
        it is what lets a tilt schedule be appended to without discarding the
        history. Without it every edit is a change of unknown reach and the only
        safe answer is to rescore everything — which is what punished an owner
        for adjusting a mount they were sold the ability to adjust.
        """
        touched = list(keys)
        if not any(
            k.startswith(self._VERSIONED_PREFIXES) or k in self._VERSIONED_KEYS for k in touched
        ):
            return

        # How far back this change reaches. date.min means "all of it"; a later
        # date means the days before it were scored against a description that
        # still describes them, and must be left exactly as they are.
        reach = date.min
        if touched == [PANELS_STRINGS_KEY] and previous is not None:
            current_text = self.get(PANELS_STRINGS_KEY)
            reach_or_none = _schedule_reach(
                previous, current_text if isinstance(current_text, str) else ""
            )
            if reach_or_none is None:
                # Nothing that any stored day was scored against actually moved.
                return
            reach = reach_or_none
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (CONFIG_VERSION_KEY,)
        ).fetchone()
        try:
            current = int(row[0]) if row and row[0] else 0
        except ValueError:
            # A version we cannot read is one we cannot trust to be older than
            # what comes next; starting again from zero would make every stored
            # day agree with it and freeze exactly the staleness it exists to
            # catch, so step somewhere no stored row can already be sitting.
            logger.warning("efficiency config version unreadable; restarting it")
            current = 0
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CONFIG_VERSION_KEY, str(current + 1)),
        )
        self._move_valid_from(reach, current)
        logger.info(
            "array configuration changed; efficiency version now %d, rescoring from %s",
            current + 1,
            "the beginning" if reach == date.min else reach.isoformat(),
        )

    def _move_valid_from(self, reach: date, settled_version: int) -> None:
        """Record how far back the version just written has to be believed.

        The floor may be *raised* only when every stored day already agrees with
        the version being superseded — that is, when nothing is queued for a
        rescore. Then no score is at risk and a change confined to next October
        can leave the whole history alone, which is the entire point of a tilt
        schedule.

        When days are still queued the floor is lowered to whichever reaches
        back further, never raised. A change confined to October cannot
        un-invalidate what last week's correction already queued, and stepping
        over it would bless a score computed against a description somebody went
        to the trouble of correcting.
        """
        floor = 0 if reach == date.min else self._local_midnight(reach)
        standing = self._standing_floor()
        if not self._efficiency_settled(settled_version, standing):
            floor = min(floor, standing)
        self._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (CONFIG_VALID_FROM_KEY, str(floor)),
        )

    def _local_midnight(self, day: date) -> int:
        """The epoch an efficiency row for ``day`` is keyed by.

        Rows are stamped at midnight on the owner's own clock, so the floor they
        are compared against has to be built the same way. Reading it as UTC
        midnight instead puts the two up to fourteen hours apart, and the sign
        of that gap follows the site's offset: east of Greenwich the very first
        day of a new tilt sorts below the floor and quietly keeps the score it
        was given under the old geometry — the one day of the year the change
        was made to fix.
        """
        configured = self.get(SETTING_TIMEZONE)
        zone = resolve_zone(None, configured if isinstance(configured, str) else None)
        return int(datetime(day.year, day.month, day.day, tzinfo=zone).timestamp())

    def _standing_floor(self) -> int:
        """The floor as it stands, or zero when none has been recorded or it is unreadable.

        Zero is the conservative answer in both cases: it claims the whole
        history for the current version, which costs a rescore and never blesses
        a score that should have been recomputed.
        """
        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (CONFIG_VALID_FROM_KEY,)
        ).fetchone()
        if not row or not row[0]:
            return 0
        try:
            return int(row[0])
        except ValueError:
            logger.warning("efficiency rescore floor unreadable; claiming the whole history")
            return 0

    def _efficiency_settled(self, version: int, floor: int) -> bool:
        """True when no stored efficiency day is still waiting to be rescored.

        ``floor`` is what the current version already claims, and rows below it
        are excluded from the question. They carry an older version legitimately
        — that is the whole point of the floor — so counting them as outstanding
        would make the answer permanently no. The floor could then never rise
        again, and the second seasonal adjustment an owner ever made would
        rescore everything back to the first one, which is the behaviour this
        was built to remove.

        Reaches into the efficiency table from the settings writer, which bends
        the usual one-way flow and is worth naming rather than hiding. The
        alternative is worse: without knowing whether a rescore is outstanding
        the floor can only ever be lowered, and a fresh installation — whose
        floor starts at zero — would rescore its whole history the first time an
        owner scheduled a future adjustment, which is the bug this exists to
        remove. The two live in one SQLite file and one transaction, so the
        question costs a single indexed read.

        A database with no efficiency table at all is settled by definition:
        there are no scores to protect.
        """
        try:
            row = self._conn.execute(
                "SELECT 1 FROM efficiency_day WHERE string_name = '' AND config_version <> ? "
                "AND day >= ? LIMIT 1",
                (version, floor),
            ).fetchone()
        except sqlite3.Error:
            return True
        return row is None

    def ensure_efficiency_scorer_revision(self, revision: int) -> bool:
        """Apply a scorer migration once, then invalidate every stored score.

        The configuration version belongs to owner edits and has no upper bound:
        using it as a code-migration marker meant an installation that had edited
        its array often looked newer than the scorer that produced its rows.  A
        separate persisted revision records the scorer instead, while advancing
        the configuration version keeps the existing stale-row and backfill
        machinery responsible for recomputing history.
        """
        if revision < 1:
            raise ValueError("efficiency scorer revision must be positive")
        revision_row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (_EFFICIENCY_SCORER_REVISION_KEY,)
        ).fetchone()
        try:
            applied = int(revision_row[0]) if revision_row and revision_row[0] else 0
        except ValueError:
            applied = 0
        if applied >= revision:
            return False

        row = self._conn.execute(
            "SELECT value FROM settings WHERE key = ?", (CONFIG_VERSION_KEY,)
        ).fetchone()
        try:
            current = int(row[0]) if row and row[0] else 0
        except ValueError:
            current = 0
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (CONFIG_VERSION_KEY, str(current + 1)),
            )
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_EFFICIENCY_SCORER_REVISION_KEY, str(revision)),
            )
        logger.info(
            "efficiency scorer revision %d advanced config version from %d to %d",
            revision,
            current,
            current + 1,
        )
        return True

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
        previous = self._array_text() if PANELS_STRINGS_KEY in values else None
        with self._conn:
            for key, stored in checked:
                self._conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, stored),
                )
            self._bump_config_version((k for k, _ in checked), previous)

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
        previous = self._array_text() if key == PANELS_STRINGS_KEY else None
        with self._conn:
            self._conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, stored),
            )
            self._bump_config_version((key,), previous)
        logger.info("setting %s changed", key)

    def _array_text(self) -> str:
        """The array description as it stands before this write overwrites it.

        Captured by the writer rather than read back afterwards, because the
        reach of a change is a question about the difference between two
        descriptions and one of them stops existing the moment the row is
        updated.
        """
        stored = self.get(PANELS_STRINGS_KEY)
        return stored if isinstance(stored, str) else ""

    def clear(self, key: str) -> None:
        """Forget any stored value for ``key`` so it reads its default again."""
        lookup_setting(key)
        with self._conn:
            self._conn.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._bump_config_version((key,))

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
            if key in (CONFIG_VERSION_KEY, _EFFICIENCY_SCORER_REVISION_KEY, AUTH_PASSWORD_KEY):
                continue
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
        With no password set, the same unauthenticated endpoint accepts writes,
        so a client on the network can point ``connection.dongle_host`` at a
        listener it controls and read both serials off the wire at the next
        poll — the protocol carries them in clear ASCII. Masking stops a serial
        being read off the page; only authentication stops it being taken, and
        since #34 that is a password the owner can now actually set, which
        closes this particular route. Nothing here should be
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
