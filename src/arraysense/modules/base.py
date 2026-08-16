"""base.py — what a module is, independent of what any particular module does.

A module is an optional capability an owner switches on: a name, a sentence
explaining it, and the setting that turns it on. Keeping that shape here rather
than in the registry means the registry is a list rather than a definition, and
a second module adds a line instead of a concept.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModuleEntry:
    """One optional capability: what it is called, what it does, and its enable.

    ``enable_key`` is a settings-registry key rather than a boolean held here,
    because the registry is what renders the settings page and what validates a
    write. A module holding its own enabled flag would be a second source of
    truth for the same question.
    """

    name: str
    description: str
    enable_key: str
