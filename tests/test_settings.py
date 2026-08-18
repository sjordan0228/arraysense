"""Tests for stored settings: arraysense.settings."""

from __future__ import annotations

import errno
import itertools
import os
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from arraysense.efficiency import EfficiencyRow
from arraysense.energy import resolve_zone
from arraysense.panels import parse_strings
from arraysense.settings import (
    BACKUP_DIRECTORY_KEY,
    BACKUP_ENABLED_KEY,
    BACKUP_HOUR_KEY,
    BACKUP_KEEP_KEY,
    BACKUP_MINUTE_KEY,
    CONFIG_VALID_FROM_KEY,
    CONFIG_VERSION_KEY,
    EMPORIA_INTERVAL_KEY,
    PANELS_STRINGS_KEY,
    SETTING_CONTACT_EMAIL,
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    SETTING_TIMEZONE,
    SETTINGS,
    SettingsStore,
    _schedule_reach,
    check_backup_directory,
    describe,
    emporia_interval_seconds,
    lookup_setting,
)
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE


@pytest.fixture
def settings(tmp_path: Path) -> SettingsStore:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    return SettingsStore(store)


def test_an_unset_setting_reads_its_default(settings: SettingsStore) -> None:
    # A fresh install has an empty table and must still work. Defaults live in
    # the registry, not in the database, so adding a setting needs no migration
    # and an untouched install behaves the same as a configured one.
    assert settings.get("display.temperature_unit") == "F"


def test_constructing_a_settings_reader_executes_no_schema_ddl(tmp_path: Path) -> None:
    # Request handlers construct this object to read current settings. Schema
    # creation belongs to store startup: even CREATE TABLE IF NOT EXISTS is a
    # schema operation and can wait behind another connection's transaction.
    # A read path must never issue it on the event-loop thread.
    store = SqliteStore(str(tmp_path / "read-only.db"), device=TEST_DEVICE)
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)

    SettingsStore(store).get("display.temperature_unit")

    store._conn.set_trace_callback(None)
    store.close()
    assert not any(
        statement.lstrip().upper().startswith("CREATE TABLE") for statement in statements
    )


def test_a_stored_value_wins_over_the_default(settings: SettingsStore) -> None:
    settings.set("display.temperature_unit", "C")
    assert settings.get("display.temperature_unit") == "C"


def test_a_setting_survives_reopening_the_database(tmp_path: Path) -> None:
    store = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
    SettingsStore(store).set("display.temperature_unit", "C")
    store.close()
    reopened = SqliteStore(str(tmp_path / "s.db"), device=TEST_DEVICE)
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


def test_a_table_of_monthly_factors_saves_as_typed(settings: SettingsStore) -> None:
    # Written a line at a time, the way the supplier publishes them, and read
    # back unchanged: the registry stores the text and the tariff parses it.
    typed = "2026-07 | -0.001230 | 0.004560\n2026-08 | 0.002100 | 0.004560"
    settings.set("tariff.adjustments", typed)
    assert settings.get("tariff.adjustments") == typed


def test_a_malformed_month_of_factors_is_refused_at_the_box_it_was_typed_in(
    settings: SettingsStore,
) -> None:
    # Storing it and letting the Costs page discover it as an absence is how
    # the tariff field went wrong; the same check runs here for the same reason.
    with pytest.raises(ValueError, match="2026-07"):
        settings.set("tariff.adjustments", "2026-07 | -0.001 | 0.004; 2026-07 | -0.002 | 0.004")


def test_an_empty_table_of_factors_is_allowed(settings: SettingsStore) -> None:
    # A supplier that charges no rider is the ordinary case, and it must not
    # have to type something to say so.
    settings.set("tariff.adjustments", "")
    assert settings.get("tariff.adjustments") == ""


# --- units and suggestions ----------------------------------------------------


def test_every_described_field_carries_a_unit_and_suggestions() -> None:
    # The page renders itself from describe(). A field that omits either key
    # makes the page branch on whether the key exists, which is exactly the
    # hard-coding the registry is meant to remove.
    for field in describe():
        assert "unit" in field, field["key"]
        assert "suggestions" in field, field["key"]
        assert isinstance(field["unit"], str)
        assert isinstance(field["suggestions"], list)


def test_a_setting_with_nothing_to_say_about_units_says_nothing() -> None:
    # Empty rather than a placeholder: a page appending "units" beside a
    # temperature unit control would be writing nonsense of its own.
    assert lookup_setting("display.temperature_unit").unit == ""


def test_the_intervals_are_labelled_in_seconds() -> None:
    assert lookup_setting("collector.poll_interval").unit == "seconds"
    assert lookup_setting("display.refresh_seconds").unit == "seconds"


def test_the_export_credit_is_labelled_as_a_rate_and_not_as_energy() -> None:
    # The value is money per kWh. A unit reading "kWh" would present a rate as
    # a quantity of energy, which is the one reading it must not have.
    unit = lookup_setting("tariff.export_per_kwh").unit
    assert "kWh" in unit
    assert unit.startswith("currency per")
    assert lookup_setting("tariff.fixed_monthly").unit == "currency per month"


def test_the_currency_suggests_without_restricting(settings: SettingsStore) -> None:
    # #6 is explicit that a closed list makes an unusual currency
    # unrepresentable. Suggestions are a datalist, not a choice list.
    spec = lookup_setting("tariff.currency")
    assert spec.kind == "str"
    assert spec.choices == ()
    assert "$" in spec.suggestions
    assert "USD" in spec.suggestions
    # Both forms have to keep working: money() spaces a symbol and a code
    # differently, so neither may be normalised into the other.
    for typed in ("$", "USD", "kr", "₹", "R$"):
        settings.set("tariff.currency", typed)
        assert settings.get("tariff.currency") == typed


def test_an_existing_typed_currency_is_never_replaced(settings: SettingsStore) -> None:
    # The issue warns about this directly: someone who has already typed their
    # own currency must not find it swapped for a suggested one.
    settings.set("tariff.currency", "Kč")
    assert settings.public()["tariff.currency"] == "Kč"


# --- site: where the installation is ------------------------------------------


def test_a_fresh_install_follows_the_host_zone(settings: SettingsStore) -> None:
    # Empty is the default and means "whatever the machine keeps". Anything
    # else would move an existing install's midnight on upgrade, and every
    # money figure is cut at a midnight.
    assert settings.get(SETTING_TIMEZONE) == ""


def test_a_real_zone_is_accepted(settings: SettingsStore) -> None:
    settings.set(SETTING_TIMEZONE, "America/New_York")
    assert settings.get(SETTING_TIMEZONE) == "America/New_York"


def test_an_unparseable_zone_is_refused_where_it_is_typed(settings: SettingsStore) -> None:
    # Not discovered later by an endpoint that then has to decide what to do
    # about it. The check resolves the name against the tz database.
    with pytest.raises(ValueError, match="Mars/Olympus_Mons"):
        settings.set(SETTING_TIMEZONE, "Mars/Olympus_Mons")
    with pytest.raises(ValueError, match=SETTING_TIMEZONE):
        settings.set(SETTING_TIMEZONE, "EST5EDT-nope")


def test_clearing_the_zone_back_to_empty_is_allowed(settings: SettingsStore) -> None:
    settings.set(SETTING_TIMEZONE, "Asia/Tokyo")
    settings.set(SETTING_TIMEZONE, "")
    assert settings.get(SETTING_TIMEZONE) == ""


def test_an_unset_coordinate_reads_as_none_not_as_zero(settings: SettingsStore) -> None:
    # 0.0 is a real place in the Gulf of Guinea. An unset latitude that read as
    # zero would put the installation there, and this project exists because
    # absent data rendered as a number.
    assert settings.get(SETTING_LATITUDE) is None
    assert settings.get(SETTING_LONGITUDE) is None
    assert lookup_setting(SETTING_LATITUDE).optional


def test_the_equator_is_storable_and_distinguishable_from_unset(
    settings: SettingsStore,
) -> None:
    settings.set(SETTING_LATITUDE, 0.0)
    assert settings.get(SETTING_LATITUDE) == 0.0
    assert settings.get(SETTING_LATITUDE) is not None
    # And on the wire, where a page has to tell the two apart as well.
    assert settings.public()[SETTING_LATITUDE] == 0.0
    assert settings.public()[SETTING_LONGITUDE] is None


def test_a_coordinate_survives_reopening_as_zero_rather_than_as_unset(
    tmp_path: Path,
) -> None:
    # The distinction has to survive the round trip through TEXT storage: an
    # empty cell is unset and "0.0" is the equator, and decoding must not
    # collapse them.
    store = SqliteStore(str(tmp_path / "c.db"), device=TEST_DEVICE)
    SettingsStore(store).set(SETTING_LATITUDE, 0.0)
    store.close()
    reopened = SqliteStore(str(tmp_path / "c.db"), device=TEST_DEVICE)
    assert SettingsStore(reopened).get(SETTING_LATITUDE) == 0.0
    reopened.close()


def test_a_coordinate_can_be_emptied_back_to_unset(settings: SettingsStore) -> None:
    # The page posts an empty box as empty text. That is "I have not said",
    # and it must not be read as the equator.
    settings.set(SETTING_LATITUDE, 51.5)
    settings.set(SETTING_LATITUDE, "")
    assert settings.get(SETTING_LATITUDE) is None
    settings.set(SETTING_LONGITUDE, -0.12)
    settings.set(SETTING_LONGITUDE, None)
    assert settings.get(SETTING_LONGITUDE) is None


def test_an_impossible_coordinate_is_refused(settings: SettingsStore) -> None:
    with pytest.raises(ValueError, match="latitude"):
        settings.set(SETTING_LATITUDE, 91.0)
    with pytest.raises(ValueError, match="latitude"):
        settings.set(SETTING_LATITUDE, -90.5)
    with pytest.raises(ValueError, match="longitude"):
        settings.set(SETTING_LONGITUDE, 180.5)
    with pytest.raises(ValueError, match="longitude"):
        settings.set(SETTING_LONGITUDE, "north")


def test_the_poles_and_the_antimeridian_are_still_valid(settings: SettingsStore) -> None:
    for lat in (-90.0, 90.0):
        settings.set(SETTING_LATITUDE, lat)
        assert settings.get(SETTING_LATITUDE) == lat
    for lon in (-180.0, 180.0):
        settings.set(SETTING_LONGITUDE, lon)
        assert settings.get(SETTING_LONGITUDE) == lon


def test_an_optional_setting_that_was_never_set_is_not_an_override(
    settings: SettingsStore,
) -> None:
    # A default is not a decision, and neither is an unset coordinate.
    assert SETTING_LATITUDE not in settings.overrides()
    settings.set(SETTING_LATITUDE, 0.0)
    assert settings.overrides()[SETTING_LATITUDE] == 0.0


def test_the_contact_email_is_masked_like_the_serials(settings: SettingsStore) -> None:
    # The settings page has no authentication in front of it, so an address
    # typed here would otherwise be readable by anything on the network.
    assert lookup_setting(SETTING_CONTACT_EMAIL).secret
    settings.set(SETTING_CONTACT_EMAIL, "owner@example.com")
    masked = settings.public()[SETTING_CONTACT_EMAIL]
    assert isinstance(masked, str)
    assert "owner@example.com" not in masked
    assert masked.startswith("ow") and masked.endswith("om")
    assert settings.get(SETTING_CONTACT_EMAIL) == "owner@example.com"


def test_something_that_is_plainly_not_an_address_is_refused(settings: SettingsStore) -> None:
    for bad in ("owner", "owner@", "@example.com", "owner@example", "a b@example.com"):
        with pytest.raises(ValueError, match="contact_email"):
            settings.set(SETTING_CONTACT_EMAIL, bad)


def test_an_ordinary_address_with_a_plus_or_a_long_tld_is_accepted(
    settings: SettingsStore,
) -> None:
    # The check refuses what is obviously wrong rather than enforcing RFC 5322.
    # An over-strict pattern rejects real addresses, and this is a note to the
    # owner rather than a credential.
    for good in ("owner+solar@example.com", "o@example.photography", "a.b-c@sub.example.co.uk"):
        settings.set(SETTING_CONTACT_EMAIL, good)
        assert settings.get(SETTING_CONTACT_EMAIL) == good


def test_an_empty_contact_email_is_allowed(settings: SettingsStore) -> None:
    settings.set(SETTING_CONTACT_EMAIL, "")
    assert settings.get(SETTING_CONTACT_EMAIL) == ""
    assert settings.public()[SETTING_CONTACT_EMAIL] == ""


def test_the_setup_connection_keys_are_registered_with_bounds() -> None:
    # effective() only overlays registered keys, and the apply endpoint can
    # only write registered keys — an unregistered key silently vanishes,
    # which is how the first attempt at these settings failed review.
    assert lookup_setting("connection.transport").choices == ("dongle", "modbus_serial")
    assert lookup_setting("connection.serial_device").kind == "str"
    baud = lookup_setting("connection.serial_baud")
    assert baud.kind == "int"
    unit = lookup_setting("connection.serial_unit_id")
    assert (unit.lower, unit.upper) == (1, 247)
    assert lookup_setting("connection.model").kind == "str"
    assert lookup_setting("connection.battery_source").choices == ("", "relayed", "none")


def test_the_weather_interval_is_registered_with_bounds() -> None:
    # The weather poller reads this every tick, so an install tunes cadence
    # without a code change. 900 s is Open-Meteo's own refresh rate for
    # current conditions; polling faster stores near-identical points.
    spec = lookup_setting("collector.weather_interval")
    assert spec.kind == "float"
    assert spec.default == 900.0
    assert (spec.lower, spec.upper) == (300.0, 86400.0)
    assert spec.unit == "seconds"


def test_a_serial_device_that_is_a_url_is_refused() -> None:
    # pyserial reads any device string with "://" as a URL and dispatches to a
    # handler that raises an undeclared exception at connect. The registry
    # refuses it so it never persists — the same rule the request models and the
    # first-run wizard enforce, so no write path can store a bricking device.
    spec = lookup_setting("connection.serial_device")
    with pytest.raises(ValueError, match="filesystem path, not a URL"):
        spec.validate("loop://?foo=bar")
    # A real device path is still accepted.
    assert spec.validate("/dev/serial/by-id/usb-1a86") == "/dev/serial/by-id/usb-1a86"


def test_the_panel_strings_setting_parses_at_the_door() -> None:
    # One grammar, one parser: the registry refuses exactly what panels.py
    # cannot read, so a stored config is always a readable one.
    spec = lookup_setting("panels.strings")
    assert spec.kind == "str"
    assert spec.multiline is True
    assert spec.default == ""
    spec.validate("East | 1 | 9 | 410 | 25 | 90")
    with pytest.raises(ValueError, match="tilt"):
        spec.validate("East | 1 | 9 | 410 | 95 | 90")


def test_the_battery_group_is_registered_with_the_measured_default() -> None:
    rt = lookup_setting("battery.round_trip_pct")
    assert rt.default == 91.4  # the owner's measured round trip, not a datasheet
    assert lookup_setting("battery.chemistry").choices == ("lifepo4", "other")
    heater = lookup_setting("battery.heater_w")
    assert heater.default == 0.0
    assert lookup_setting("battery.min_soc_pct").default == 10.0


class TestEfficiencyConfigVersion:
    """Describing the array differently invalidates what was scored under the old one."""

    @staticmethod
    def _version(settings: SettingsStore) -> int:
        value = settings.get(CONFIG_VERSION_KEY)
        assert isinstance(value, int)
        return value

    def test_changing_the_array_bumps_the_version(self, tmp_path: Path) -> None:
        """Without this the invalidation is decorative.

        Days carry the version they were scored under, and the summary pass
        rescores a day whose version has moved on. If nothing ever moves it,
        a day scored against ten panels keeps that score after the owner
        describes twenty, and the record silently describes an array that no
        longer exists.
        """
        store = SqliteStore(str(tmp_path / "v.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        before = self._version(settings)
        settings.set("panels.strings", "East | 1 | 10 | 400 | 25 | 90")
        after = self._version(settings)
        store.close()
        assert after > before

    def test_a_battery_change_counts_as_an_array_change(self, tmp_path: Path) -> None:
        store = SqliteStore(str(tmp_path / "b.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        before = self._version(settings)
        settings.set("battery.installed", "2024-08")
        after = self._version(settings)
        store.close()
        assert after > before

    def test_an_unrelated_setting_leaves_it_alone(self, tmp_path: Path) -> None:
        # Every bump costs a rescore of every stored day, so a setting that
        # does not change what the array should produce must not trigger one.
        # A contact address is the clearest case of that.
        store = SqliteStore(str(tmp_path / "u.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        before = self._version(settings)
        settings.set(SETTING_CONTACT_EMAIL, "someone@example.com")
        after = self._version(settings)
        store.close()
        assert after == before

    def test_moving_the_site_invalidates_what_was_scored_there(self, tmp_path: Path) -> None:
        """Where the array is decides what its sun should have been.

        Latitude and longitude place the sun in the sky; the zone decides where
        one day stops and the next begins. A day scored before any of the three
        was corrected was scored against a different sky than the array was
        actually under, and must not be left standing as though it were current.
        """
        for key, value in (
            (SETTING_LATITUDE, 45.0),
            (SETTING_LONGITUDE, -93.0),
            (SETTING_TIMEZONE, "America/Denver"),
        ):
            store = SqliteStore(str(tmp_path / f"{key}.db"), device=TEST_DEVICE)
            settings = SettingsStore(store)
            before = self._version(settings)
            settings.set(key, value)
            after = self._version(settings)
            store.close()
            assert after > before, f"{key} left the efficiency version untouched"

    def test_a_batch_write_bumps_once_not_per_key(self, tmp_path: Path) -> None:
        store = SqliteStore(str(tmp_path / "m.db"), device=TEST_DEVICE)
        settings = SettingsStore(store)
        before = self._version(settings)
        settings.set_many(
            {"panels.strings": "East | 1 | 10 | 400 | 25 | 90", "battery.installed": "2024-08"}
        )
        after = self._version(settings)
        store.close()
        assert after == before + 1


def test_the_array_help_names_every_key_the_parser_accepts() -> None:
    """A help text that drifts from the grammar is a config nobody can write.

    The page renders this string verbatim, so a key missing from it is a key
    the owner has no way to discover -- and one listed but unparsed is worse.
    Checked against the parser rather than a copy of the list, so adding a key
    to panels.py and forgetting the help turns this red.
    """
    from arraysense.panels import KNOWN_STRING_KEYS

    help_text = lookup_setting("panels.strings").help
    missing = sorted(k for k in KNOWN_STRING_KEYS if k not in help_text)
    assert not missing, f"the array help never mentions {missing}"


# --- The daily backup -------------------------------------------------------


BACKUP_KEYS = (
    BACKUP_ENABLED_KEY,
    BACKUP_DIRECTORY_KEY,
    BACKUP_KEEP_KEY,
    BACKUP_HOUR_KEY,
    BACKUP_MINUTE_KEY,
)


def test_the_backup_settings_read_their_registered_defaults(settings: SettingsStore) -> None:
    """A fresh install must back up on the schedule the units shipped with.

    These were compiled-in constants, so an installation that has stored
    nothing has to behave exactly as it did before they became settings.
    """
    assert settings.get(BACKUP_ENABLED_KEY) is True
    assert settings.get(BACKUP_DIRECTORY_KEY) == "/var/backups/arraysense"
    assert settings.get(BACKUP_KEEP_KEY) == 14
    assert settings.get(BACKUP_HOUR_KEY) == 3
    assert settings.get(BACKUP_MINUTE_KEY) == 15


def test_the_backup_settings_render_from_the_registry_alone() -> None:
    """settings.html builds every control from describe() and nothing else.

    A setting absent from this payload is a setting with no control, and the
    fix is always the registry entry -- a page taught about one key by hand is
    the drift the registry exists to prevent.
    """
    described = {field["key"]: field for field in describe()}
    for key in BACKUP_KEYS:
        assert key in described, f"{key} would not appear on the settings page"
        assert described[key]["label"], f"{key} would render with no label"
        assert described[key]["help"], f"{key} would render with no explanation"


def test_the_backup_schedule_is_bounded_to_a_real_clock_time() -> None:
    """An hour of 24 or a minute of 60 is a time that never arrives.

    The scheduled run compares the wall clock against these two numbers, so a
    value outside the clock is a backup that silently never becomes due.
    """
    hour = lookup_setting(BACKUP_HOUR_KEY)
    assert (hour.lower, hour.upper) == (0, 23)
    minute = lookup_setting(BACKUP_MINUTE_KEY)
    assert (minute.lower, minute.upper) == (0, 59)


def test_keeping_zero_backups_is_refused(settings: SettingsStore) -> None:
    """Rotation keeps the newest N; at zero it deletes every copy there is."""
    with pytest.raises(ValueError):
        settings.set(BACKUP_KEEP_KEY, 0)


def test_a_writable_directory_is_accepted(tmp_path: Path) -> None:
    """The happy path has to be confirmed: a false refusal here is an
    installation that cannot configure a backup at all."""
    assert check_backup_directory(str(tmp_path)) == str(tmp_path)


def test_a_destination_that_does_not_exist_is_refused_by_name(tmp_path: Path) -> None:
    """Naming the path and the command that creates it, because the directory
    is deliberately not created here -- whoever runs first would own it."""
    missing = tmp_path / "nowhere"
    with pytest.raises(ValueError) as caught:
        check_backup_directory(str(missing))
    assert str(missing) in str(caught.value)
    assert "install -d" in str(caught.value)


def test_a_relative_destination_is_refused(tmp_path: Path) -> None:
    """systemd runs the backup with no working directory of its own, so a
    relative path resolves somewhere nobody chose."""
    with pytest.raises(ValueError) as caught:
        check_backup_directory("backups")
    assert "absolute" in str(caught.value)


def test_an_empty_destination_is_refused() -> None:
    """The empty string is not "leave it alone"; it is a path the backup would
    join a filename onto and write into the current directory."""
    with pytest.raises(ValueError):
        check_backup_directory("   ")


def test_a_file_where_the_directory_should_be_is_refused(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("")
    with pytest.raises(ValueError) as caught:
        check_backup_directory(str(not_a_dir))
    assert "not a directory" in str(caught.value)


def test_a_directory_the_service_cannot_write_is_refused(tmp_path: Path) -> None:
    """root creating the directory first is how this has already failed: the
    service runs as arraysense and can never write there."""
    if os.geteuid() == 0:
        pytest.skip("root can write to a 0500 directory")
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(mode=0o500)
    try:
        with pytest.raises(ValueError) as caught:
            check_backup_directory(str(unwritable))
        assert "chown" in str(caught.value)
    finally:
        unwritable.chmod(0o700)


def test_a_read_only_sandbox_is_named_as_the_sandbox(tmp_path: Path, monkeypatch: Any) -> None:
    """The failure found on a real machine, reported as "another backup is
    running", which was false.

    Under ProtectSystem=strict a path outside the unit's writable set fails
    with "Read-only file system" however good the permissions are, and the
    only remedy is a drop-in naming the directory. A rejection that blamed
    ownership would send the owner chasing a chown that changes nothing.
    """

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(tempfile, "mkstemp", refuse)
    with pytest.raises(ValueError) as caught:
        check_backup_directory(str(tmp_path))
    message = str(caught.value)
    assert "ReadWritePaths" in message
    assert "ProtectSystem=strict" in message


def test_the_probe_leaves_nothing_behind(tmp_path: Path) -> None:
    """A check that littered the destination would leave the rotation counting
    files that are not backups."""
    check_backup_directory(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def _local_epoch(year: int, month: int, day: int) -> int:
    """Midnight on the site's own clock, which is how efficiency days are keyed.

    Written out here rather than assumed to be UTC: the two differ by the site's
    offset, and a test that assumed UTC would pass in London and fail in Sydney.
    """
    zone = resolve_zone(None, None)
    return int(datetime(year, month, day, tzinfo=zone).timestamp())


def test_writing_a_first_array_description_bumps_the_version(settings: SettingsStore) -> None:
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before


def test_changing_the_panel_count_reaches_the_whole_history(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 10 | 400 | 25 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == 0


def test_adding_a_string_reaches_the_whole_history(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0\nWest | 2 | 8 | 350 | 25 | 180")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == 0


def test_renaming_a_string_reaches_the_whole_history(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "West | 1 | 9 | 400 | 25 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == 0


def test_altering_a_tilt_already_in_force_reaches_the_whole_history(
    settings: SettingsStore,
) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 30 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == 0


def test_appending_a_future_tilt_leaves_the_past_alone(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2027, 10, 1)


def test_correcting_a_dated_entry_reaches_back_only_to_that_date(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,38@2027-10-01 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2027, 10, 1)


def test_correcting_the_last_entry_leaves_the_earlier_adjustment_alone(
    settings: SettingsStore,
) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01,25@2028-03-15 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01,28@2028-03-15 | 0")
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2028, 3, 15)


def test_rewriting_the_identical_description_changes_nothing(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    assert settings.get(CONFIG_VERSION_KEY) == before


def test_moving_the_site_reaches_the_whole_history(settings: SettingsStore) -> None:
    before = settings.get(CONFIG_VERSION_KEY)
    settings.set(SETTING_LATITUDE, 34.0522)
    assert settings.get(CONFIG_VERSION_KEY) != before
    assert settings.get(CONFIG_VALID_FROM_KEY) == 0


def test_two_successive_future_appends_each_leave_the_past_alone(settings: SettingsStore) -> None:
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01 | 0")
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2027, 10, 1)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01,30@2028-03-15 | 0")
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2028, 3, 15)


def test_the_floor_is_the_sites_own_midnight_not_utcs(settings: SettingsStore) -> None:
    # East of Greenwich local midnight lands BEFORE UTC midnight. A floor built
    # in UTC therefore sorts the first day of the new tilt below itself, blesses
    # the stale score, and leaves the one day the owner changed the array
    # scored against the geometry it no longer had.
    settings.set(SETTING_TIMEZONE, "Australia/Sydney")
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01 | 0")
    sydney = ZoneInfo("Australia/Sydney")
    first_day = int(datetime(2027, 10, 1, tzinfo=sydney).timestamp())
    assert settings.get(CONFIG_VALID_FROM_KEY) == first_day
    # The day itself is not below the floor, so it is rescored rather than kept.
    assert not first_day < first_day
    assert first_day < int(datetime(2027, 10, 1, tzinfo=UTC).timestamp())


def test_a_second_seasonal_append_does_not_reopen_the_first(tmp_path: Path) -> None:
    # With history already scored below the standing floor, a further future
    # adjustment must raise the floor rather than be pinned at the earlier one.
    # Counting those blessed rows as outstanding froze the floor forever, so the
    # second adjustment an owner ever made rescored everything back to the
    # first — the exact punishment a tilt schedule exists to remove.
    store = SqliteStore(str(tmp_path / "r.db"), device=TEST_DEVICE)
    settings = SettingsStore(store)
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25 | 0")
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01 | 0")
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2027, 10, 1)

    # A day scored long ago, under a version that has since moved on. It sits
    # below the floor, so it is legitimately blessed rather than outstanding.
    zone = resolve_zone(None, None)
    old_day = datetime(2026, 6, 1, tzinfo=zone)
    store.write_efficiency_day(
        [
            EfficiencyRow(
                day=old_day,
                string_name="",
                expected_kwh=10.0,
                actual_kwh=9.0,
                curtailed_kwh=0.0,
                unexplained_kwh=1.0,
                modelled_hours=12,
                partial=False,
                pr=0.9,
                config_version=1,
            )
        ]
    )
    settings.set(PANELS_STRINGS_KEY, "East | 1 | 9 | 400 | 25,40@2027-10-01,30@2028-03-15 | 0")
    assert settings.get(CONFIG_VALID_FROM_KEY) == _local_epoch(2028, 3, 15)


@pytest.mark.parametrize(
    ("zone_name", "year", "month", "day"),
    [
        ("Asia/Beirut", 2027, 3, 28),  # local midnight is skipped entirely
        ("America/Havana", 2027, 3, 14),  # same, other hemisphere's neighbour
        ("America/Santiago", 2027, 9, 5),  # the shift lands at 24:00
        ("Asia/Beirut", 2027, 10, 31),  # local midnight happens twice
    ],
)
def test_the_floor_matches_the_row_key_across_a_midnight_dst_shift(
    zone_name: str, year: int, month: int, day: int
) -> None:
    # The floor and the day key must be the same instant, and on these dates
    # local midnight either never happens or happens twice. Both sides build it
    # the same way and so resolve the fold identically; a floor built any other
    # way would sort the day either side of itself twice a year.
    zone = ZoneInfo(zone_name)
    floor = int(datetime(year, month, day, tzinfo=zone).timestamp())
    row_key = int(
        datetime.combine(date(year, month, day), datetime.min.time(), tzinfo=zone).timestamp()
    )
    assert floor == row_key


def test_the_reach_is_the_first_day_the_two_descriptions_actually_disagree() -> None:
    # A property, checked exhaustively over a small domain rather than argued.
    # The reach is what decides which stored days keep their scores, and the
    # dangerous direction is returning a date LATER than the real disagreement:
    # that leaves a day believed which was scored against geometry it no longer
    # had. Both schedules are step functions, so agreeing at every step and
    # before the first implies agreeing everywhere — this is what proves it.
    def line(tilt: str) -> str:
        return f"East | 1 | 9 | 410 | {tilt} | 90"

    stamps = ["2027-03-01", "2027-10-01", "2028-03-15"]
    angles = [20, 25, 30]
    schedules: list[str] = [str(a) for a in angles]
    for opening in angles:
        for count in (1, 2):
            for chosen in itertools.combinations(stamps, count):
                for degrees in itertools.product(angles, repeat=count):
                    tail = ",".join(f"{g}@{d}" for g, d in zip(degrees, chosen, strict=True))
                    schedules.append(f"{opening},{tail}")

    first = date(2026, 1, 1)
    days = [first + timedelta(days=i) for i in range(1096)]
    for before, after in itertools.product(schedules, repeat=2):
        (old,) = parse_strings(line(before))
        (new,) = parse_strings(line(after))
        truth = next((d for d in days if old.tilt_at(d) != new.tilt_at(d)), None)
        reach = _schedule_reach(line(before), line(after))
        if truth is None:
            assert reach is None, f"{before!r} -> {after!r} claimed a reach of {reach}"
        elif truth == first:
            assert reach == date.min, f"{before!r} -> {after!r} should reach everything"
        else:
            assert reach == truth, f"{before!r} -> {after!r}: want {truth}, got {reach}"


def test_the_emporia_interval_refuses_a_stored_value_outside_its_registered_bounds(
    settings: SettingsStore,
) -> None:
    # The registry permits 10 to 3600 and a reader that checks only "is it a
    # positive whole number" accepts 1 and 3601. This figure is both a divisor
    # and a multiplier for an energy figure — one second would credit a reading
    # with a sixtieth of what it covers — and it is what the poller spaces its
    # calls to Emporia's cloud by. Written straight into the table because
    # ``set`` already refuses these; the case is a database another build wrote
    # or somebody edited by hand.
    spec = lookup_setting(EMPORIA_INTERVAL_KEY)
    for stored in ("1", "3601", "0", "soon", ""):
        settings._conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (EMPORIA_INTERVAL_KEY, stored),
        )
        settings._conn.commit()
        assert emporia_interval_seconds(settings) == spec.default, stored


def test_the_emporia_interval_reads_a_value_the_registry_accepts(
    settings: SettingsStore,
) -> None:
    settings.set(EMPORIA_INTERVAL_KEY, 10)
    assert emporia_interval_seconds(settings) == 10
