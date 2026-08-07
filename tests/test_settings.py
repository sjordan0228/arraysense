"""Tests for stored settings: arraysense.settings."""

from __future__ import annotations

from pathlib import Path

import pytest

from arraysense.settings import (
    SETTINGS,
    SettingsStore,
    lookup_setting,
)
from arraysense.store.sqlite_store import SqliteStore


@pytest.fixture
def settings(tmp_path: Path) -> SettingsStore:
    store = SqliteStore(str(tmp_path / "s.db"))
    return SettingsStore(store)


def test_an_unset_setting_reads_its_default(settings: SettingsStore) -> None:
    # A fresh install has an empty table and must still work. Defaults live in
    # the registry, not in the database, so adding a setting needs no migration
    # and an untouched install behaves the same as a configured one.
    assert settings.get("display.temperature_unit") == "F"


def test_a_stored_value_wins_over_the_default(settings: SettingsStore) -> None:
    settings.set("display.temperature_unit", "C")
    assert settings.get("display.temperature_unit") == "C"


def test_a_setting_survives_reopening_the_database(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"))
    SettingsStore(store).set("display.temperature_unit", "C")
    store.close()
    reopened = SqliteStore(str(tmp_path / "s.db"))
    assert SettingsStore(reopened).get("display.temperature_unit") == "C"
    reopened.close()


def test_types_survive_the_round_trip(settings: SettingsStore) -> None:
    # Everything is TEXT on disk. A float coming back as the string "11.0"
    # would silently break arithmetic wherever it is used.
    settings.set("collector.poll_interval", 13.5)
    settings.set("display.refresh_seconds", 20)
    assert settings.get("collector.poll_interval") == 13.5
    assert isinstance(settings.get("collector.poll_interval"), float)
    assert settings.get("display.refresh_seconds") == 20
    assert isinstance(settings.get("display.refresh_seconds"), int)


def test_a_value_outside_the_allowed_choices_is_refused(settings: SettingsStore) -> None:
    # Celsius, Fahrenheit, and nothing else. A typo stored here would render
    # every temperature on the page in a unit that does not exist.
    with pytest.raises(ValueError, match="temperature_unit"):
        settings.set("display.temperature_unit", "K")


def test_a_number_outside_its_bounds_is_refused(settings: SettingsStore) -> None:
    # The poll interval reaches the collector. A zero would spin the dongle's
    # single TCP slot as fast as the loop allows.
    with pytest.raises(ValueError, match="poll_interval"):
        settings.set("collector.poll_interval", 0)
    with pytest.raises(ValueError, match="poll_interval"):
        settings.set("collector.poll_interval", 4000)


def test_a_wrong_type_is_refused_rather_than_coerced(settings: SettingsStore) -> None:
    with pytest.raises(ValueError, match="refresh_seconds"):
        settings.set("display.refresh_seconds", "soon")


def test_an_unknown_key_is_refused(settings: SettingsStore) -> None:
    # A typo must not create a setting nothing reads. Silently accepting one
    # means the page shows a control that changes nothing.
    with pytest.raises(KeyError, match="nonsense"):
        settings.set("nonsense.key", 1)
    with pytest.raises(KeyError, match="nonsense"):
        settings.get("nonsense.key")


def test_all_returns_every_setting_including_the_untouched_ones(
    settings: SettingsStore,
) -> None:
    settings.set("display.temperature_unit", "C")
    everything = settings.all()
    assert len(everything) == len(SETTINGS)
    assert everything["display.temperature_unit"] == "C"
    assert (
        everything["display.refresh_seconds"] == lookup_setting("display.refresh_seconds").default
    )


def test_setting_the_same_key_twice_replaces_rather_than_duplicates(
    settings: SettingsStore,
) -> None:
    settings.set("display.temperature_unit", "C")
    settings.set("display.temperature_unit", "F")
    assert settings.get("display.temperature_unit") == "F"
    assert len(settings.all()) == len(SETTINGS)


def test_a_setting_can_be_cleared_back_to_its_default(settings: SettingsStore) -> None:
    settings.set("display.temperature_unit", "C")
    settings.clear("display.temperature_unit")
    assert settings.get("display.temperature_unit") == "F"


def test_every_registered_default_is_itself_valid() -> None:
    # A default that would be rejected on write is a setting nobody can save
    # without changing it first.
    for spec in SETTINGS:
        spec.validate(spec.default)


def test_secrets_are_marked_so_the_api_can_withhold_them() -> None:
    # The serials identify the hardware and reach an unauthenticated page. The
    # registry has to say which values must never be echoed back.
    assert lookup_setting("connection.dongle_serial").secret
    assert not lookup_setting("display.temperature_unit").secret


def test_identifying_values_are_masked_not_echoed(settings: SettingsStore) -> None:
    # There is no authentication in front of the settings page, so this
    # endpoint answers anything on the LAN. Enough of the serial to recognise
    # it, not enough to learn it.
    settings.set("connection.dongle_serial", "BA12345678")
    public = settings.public()
    assert public["connection.dongle_serial"] == "BA••••••78"
    assert "33400" not in str(public["connection.dongle_serial"])
    # The real value is still readable by the code that needs it.
    assert settings.get("connection.dongle_serial") == "BA12345678"


def test_an_unset_secret_reads_as_empty_not_as_dots(settings: SettingsStore) -> None:
    # A page has to tell "not configured yet" from "configured, and withheld".
    assert settings.public()["connection.dongle_serial"] == ""


def test_display_settings_are_returned_in_full(settings: SettingsStore) -> None:
    settings.set("display.temperature_unit", "C")
    assert settings.public()["display.temperature_unit"] == "C"


def test_update_writes_nothing_when_any_value_is_invalid(settings: SettingsStore) -> None:
    # A settings form posts every field together. Half a form landing would
    # leave the installation in a state the person never chose.
    with pytest.raises(ValueError):
        settings.update({"display.temperature_unit": "C", "display.refresh_seconds": -4})
    assert settings.get("display.temperature_unit") == "F"


def test_update_reports_only_what_actually_changed(settings: SettingsStore) -> None:
    settings.set("display.temperature_unit", "C")
    changed = settings.update({"display.temperature_unit": "C", "collector.poll_interval": 20.0})
    assert changed == ["collector.poll_interval"]


def test_overrides_returns_only_what_was_explicitly_stored(settings: SettingsStore) -> None:
    # A default is not a decision. The file configuration has to win where the
    # owner has expressed no preference, so "set to the default value" and
    # "never touched" must be distinguishable.
    assert settings.overrides() == {}
    settings.set("collector.poll_interval", 11.0)
    assert settings.overrides() == {"collector.poll_interval": 11.0}


def test_clearing_a_setting_removes_its_override(settings: SettingsStore) -> None:
    settings.set("display.temperature_unit", "C")
    settings.clear("display.temperature_unit")
    assert settings.overrides() == {}


# --- findings from an independent review -------------------------------------


def test_a_non_finite_number_is_refused(settings: SettingsStore) -> None:
    # NaN passes every bounds check, because each comparison against it is
    # false. It was accepted, stored, and then killed the collector when it
    # reached the event loop — with the HTTP API still up, so the service
    # looked healthy while collecting nothing.
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="finite"):
            settings.set("collector.poll_interval", bad)


def test_an_invisible_character_cannot_override_a_working_value(
    settings: SettingsStore,
) -> None:
    # A zero-width space is not whitespace to str.strip(), so a value made of
    # one counts as set and would replace a real serial with nothing visible.
    with pytest.raises(ValueError, match="control or invisible"):
        settings.set("connection.dongle_serial", "BA​12345678")


def test_a_control_character_is_refused(settings: SettingsStore) -> None:
    # These reach a wire protocol and a socket.
    with pytest.raises(ValueError, match="control or invisible"):
        settings.set("connection.dongle_host", "192.168.1.50\r\nEvil")


def test_an_absurdly_long_value_is_refused(settings: SettingsStore) -> None:
    with pytest.raises(ValueError, match="too long"):
        settings.set("connection.dongle_serial", "x" * 500)


def test_an_ordinary_serial_is_still_accepted(settings: SettingsStore) -> None:
    settings.set("connection.dongle_serial", "BA12345678")
    assert settings.get("connection.dongle_serial") == "BA12345678"
