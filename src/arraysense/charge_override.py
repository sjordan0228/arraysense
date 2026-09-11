"""charge_override.py — the record that keeps a grid charge undoable after a restart.

A grid charge outlives the process that started it: the registers stay in the
inverter while the collector moves on, and a service that dies mid-charge
takes its only copy of what the inverter held before with it, leaving a rate
nobody chose running for ever. This module stores that copy — the raw
registers one read answered, the moment they were read, when the charge's
window closes, and what power was asked for — as one JSON value under the
"charge.override" setting key, in the same database as the readings, so a
record written before a restart is readable after one.

One value rather than several, because the parts are only useful together:
registers without their read time cannot rebuild the configuration a restore
writes back, and half a record is worse than none.

A record that cannot be read raises rather than answering "no override is
running". A caller that believed absence would start a second charge and
overwrite the only description of how to put the inverter back; a raised
fault leaves that description on the disk, unread but recoverable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from arraysense.charge import ChargeConfig, decode_charge_config
from arraysense.settings import CHARGE_OVERRIDE_KEY, SettingsStore

# A record missing any one of these describes no charge, so it is not stored
# and must not be read: the registers and their read time rebuild the undo,
# the window and the power say what was started and on whose authority.
_REQUIRED_FIELDS = ("registers", "read_at", "until", "requested_w")


@dataclass(frozen=True)
class ChargeOverride:
    """A charge that can still be undone, and when its window closes."""

    saved: ChargeConfig
    until: datetime
    requested_w: int


def encode_override(override: ChargeOverride) -> str:
    """The stored text for one override.

    Register keys are strings here and integers everywhere else: a JSON
    object has no integer keys, and this is the one place the two
    representations meet. The quick-charge countdown is not stored because
    an undo restores what the inverter held, not how much of a countdown
    was left when the read happened.
    """
    payload = {
        "registers": {str(address): value for address, value in override.saved.registers.items()},
        "read_at": override.saved.read_at.isoformat(),
        "until": override.until.isoformat(),
        "requested_w": override.requested_w,
    }
    return json.dumps(payload)


def _moment(payload: dict[str, object], name: str) -> datetime:
    """One timestamp out of the payload, aware or ValueError.

    A window with no zone is a window nobody can compare against a clock:
    the point of the record is deciding whether a charge is still running,
    and a naive answer cannot be placed on any clock.
    """
    raw = payload[name]
    if not isinstance(raw, str):
        raise ValueError(f"the stored charge override has no readable {name}")
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"the stored charge override has an unparseable {name}: {raw!r}") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"the stored charge override's {name} carries no timezone offset")
    return moment


def decode_override(text: str) -> ChargeOverride | None:
    """The override a stored value holds, or None when nothing is stored.

    An empty cell, or one padded to emptiness with spaces, means no charge
    is running: that is the registry's default and not a fault. Anything
    else either reads or raises ValueError naming what is wrong, because a
    caller that read damage as absence would start a second charge over the
    top of the only record of how to undo the first.
    """
    if not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"the stored charge override is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("the stored charge override is not a JSON object")
    missing = [name for name in _REQUIRED_FIELDS if name not in parsed]
    if missing:
        raise ValueError(f"the stored charge override is missing {', '.join(missing)}")
    registers = parsed["registers"]
    if not isinstance(registers, dict):
        raise ValueError("the stored charge override's registers are not an object")
    values: dict[int, int] = {}
    for key, value in registers.items():
        try:
            address = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"the stored charge override names register {key!r}, which is not an address"
            ) from exc
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError(
                f"the stored charge override holds a non-numeric value in register {key!r}"
            )
        values[address] = value
    requested = parsed["requested_w"]
    if not isinstance(requested, int) or isinstance(requested, bool):
        raise ValueError("the stored charge override's requested_w is not an integer")
    read_at = _moment(parsed, "read_at")
    until = _moment(parsed, "until")
    # The countdown is left None rather than guessed at: it decodes to the
    # same absence every stored record means, and a restored inverter is
    # written back, not restarted mid-count.
    saved = decode_charge_config(values, quick_charge_remaining_s=None, read_at=read_at)
    return ChargeOverride(saved=saved, until=until, requested_w=requested)


def save_override(settings: SettingsStore, override: ChargeOverride) -> None:
    """Write the record, replacing any earlier one."""
    settings.set(CHARGE_OVERRIDE_KEY, encode_override(override))


def load_override(settings: SettingsStore) -> ChargeOverride | None:
    """Read the record, or None when none is stored.

    A stored value that cannot be used raises rather than reading as "no
    charge is running". A caller that trusted that answer would start a
    second charge and overwrite the record, and the inverter would keep the
    first charge's settings for ever with nothing left to restore.
    """
    stored = settings.get(CHARGE_OVERRIDE_KEY)
    if not isinstance(stored, str):
        return None
    return decode_override(stored)


def clear_override(settings: SettingsStore) -> None:
    """Forget the record."""
    settings.clear(CHARGE_OVERRIDE_KEY)


def override_is_active(override: ChargeOverride | None, now: datetime) -> bool:
    """Whether the charge's window is still open at ``now``.

    The module reads no clock: ``until`` and ``now`` are always arguments,
    so deciding whether a charge has lapsed asks no device and no system
    time. Nothing is active when no override was loaded.
    """
    return override is not None and now < override.until
