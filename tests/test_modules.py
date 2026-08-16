"""test_modules.py — the module registry: what this build can be extended with.

An optional module is off until somebody turns it on, and off has to cost
nothing at all. These check the two halves of that promise: a registered module
is findable by name, and a fresh installation reports it disabled without
anything having been written. The unknown-name cases matter as much — a
database written by a newer build carries settings for modules this one has
never heard of, and reading one back must be a shrug rather than an exception.
"""

from __future__ import annotations

from pathlib import Path

from arraysense import modules
from arraysense.settings import EMPORIA_ENABLED_KEY, SettingsStore
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE


def test_a_registered_module_is_findable_by_name() -> None:
    entry = modules.find("emporia")
    assert entry is not None
    assert entry.enable_key == EMPORIA_ENABLED_KEY


def test_an_unregistered_name_is_absent_not_an_error() -> None:
    assert modules.find("not-a-module") is None


def test_a_module_is_disabled_until_somebody_enables_it(tmp_path: Path) -> None:
    # The whole promise of an optional module: an installation that has never
    # heard of it behaves as though it does not exist.
    store = SqliteStore(str(tmp_path / "m.db"), device=TEST_DEVICE)
    settings = SettingsStore(store)
    assert modules.is_enabled("emporia", settings) is False
    settings.set(EMPORIA_ENABLED_KEY, True)
    assert modules.is_enabled("emporia", settings) is True
    store.close()


def test_an_unknown_module_is_never_enabled(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "m.db"), device=TEST_DEVICE)
    assert modules.is_enabled("not-a-module", SettingsStore(store)) is False
    store.close()
