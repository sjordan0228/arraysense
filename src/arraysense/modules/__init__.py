"""modules — the optional capabilities this build can be extended with, by name.

Registration is explicit and lives at the bottom of this file, for the reason
``drivers/__init__.py`` gives about its own registry: a file that lands in the
tree — half-finished, copied in to read, left behind by a merge — must not
become live code because it happens to be sitting there. The cost is one line
per module, paid by the person adding it, in the file that lists what this build
supports.

A module is off until somebody turns it on, and off must cost nothing: no
poller, no HTTP, no rows. ``is_enabled`` is therefore the only question the rest
of the service asks, and it answers False for a name nobody registered rather
than raising — an installation carrying a setting for a module this build does
not have is a downgrade, not a fault.
"""

from __future__ import annotations

from arraysense.modules.base import ModuleEntry
from arraysense.settings import EMPORIA_ENABLED_KEY, SettingsStore

__all__ = ["ModuleEntry", "available", "find", "is_enabled"]

_REGISTRY: tuple[ModuleEntry, ...] = (
    ModuleEntry(
        name="emporia",
        description=(
            "Read Emporia Vue circuit monitors and an Emporia EV charger. "
            "Requires an Emporia account and internet access; solar collection "
            "is unaffected when it is off or unreachable."
        ),
        enable_key=EMPORIA_ENABLED_KEY,
    ),
)


def available() -> tuple[ModuleEntry, ...]:
    """Every module this build knows about, enabled or not."""
    return _REGISTRY


def find(name: str) -> ModuleEntry | None:
    """The module registered under ``name``, or None when nothing is."""
    for entry in _REGISTRY:
        if entry.name == name:
            return entry
    return None


def is_enabled(name: str, settings: SettingsStore) -> bool:
    """Whether the owner has switched this module on.

    False for an unknown name rather than an exception: a database written by a
    newer build can carry settings for modules this one has never heard of, and
    that is a downgrade rather than a fault.
    """
    entry = find(name)
    if entry is None:
        return False
    return bool(settings.get(entry.enable_key))
