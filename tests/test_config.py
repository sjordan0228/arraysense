"""Tests for runtime configuration: arraysense.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from arraysense.config import Config, example_toml, load
from conftest import TEST_DEVICE

GOOD = (
    'dongle_host = "192.168.1.50"\n'
    'dongle_serial = "BA12345678"\n'
    'inverter_serial = "CE12345678"\n'
    'database_path = "/var/lib/arraysense/arraysense.db"\n'
    "poll_interval = 10.0\n"
)


def test_loads_a_complete_file(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(GOOD)
    cfg = load(p)
    assert cfg.dongle_host == "192.168.1.50"
    assert cfg.inverter_serial == "CE12345678"
    assert cfg.poll_interval == 10.0
    assert cfg.dongle_port == 8000


def test_missing_file_says_what_to_do(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"config\.example\.toml"):
        load(tmp_path / "absent.toml")


def test_missing_required_setting_names_it(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(GOOD.replace('inverter_serial = "CE12345678"\n', ""))
    with pytest.raises(ValueError, match="inverter_serial"):
        load(p)


def test_empty_required_setting_is_rejected(tmp_path: Path) -> None:
    # An empty string is as unusable as an absent one and easier to leave behind
    # when copying the example.
    p = tmp_path / "c.toml"
    p.write_text(GOOD.replace('"192.168.1.50"', '""'))
    with pytest.raises(ValueError, match="dongle_host"):
        load(p)


def test_malformed_toml_is_reported_as_such(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("this is not = = toml\n")
    with pytest.raises(ValueError, match="not valid TOML"):
        load(p)


def test_poll_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="poll_interval"):
        Config(
            dongle_host="h",
            dongle_serial="s",
            inverter_serial="i",
            database_path="/tmp/x.db",
            poll_interval=0,
        )


def test_port_must_be_valid() -> None:
    for bad in (0, 70000):
        with pytest.raises(ValueError, match="dongle_port"):
            Config(
                dongle_host="h",
                dongle_serial="s",
                inverter_serial="i",
                database_path="/tmp/x.db",
                dongle_port=bad,
            )


def test_defaults_apply_when_optional_settings_are_absent(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(GOOD.replace("poll_interval = 10.0\n", ""))
    cfg = load(p)
    assert cfg.poll_interval == 11.0
    assert cfg.dongle_port == 8000


def test_the_shipped_example_is_itself_loadable(tmp_path: Path) -> None:
    # An example that does not parse is worse than none at all.
    p = tmp_path / "example.toml"
    p.write_text(example_toml())
    cfg = load(p)
    assert cfg.dongle_host
    assert cfg.inverter_serial


def test_a_wrong_typed_number_is_a_value_error_not_a_type_error(tmp_path: Path) -> None:
    # The entry point catches FileNotFoundError and ValueError. A TOML list
    # where a number belongs used to escape as TypeError, reaching a first-time
    # installer as a traceback instead of the line naming the bad setting.
    p = tmp_path / "c.toml"
    p.write_text(GOOD.replace("poll_interval = 10.0", "poll_interval = [1]"))
    with pytest.raises(ValueError, match="poll_interval must be a number"):
        load(p)


def test_a_wrong_typed_port_is_also_a_value_error(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(GOOD + '\ndongle_port = "eight thousand"\n')
    with pytest.raises(ValueError, match="dongle_port must be a number"):
        load(p)


def test_transport_defaults_to_dongle(tmp_path: Path) -> None:
    # An existing installation needs no edit: the default preserves the
    # existing behaviour byte for byte.
    p = tmp_path / "c.toml"
    p.write_text(GOOD)
    cfg = load(p)
    assert cfg.transport == "dongle"
    assert cfg.serial_device == ""
    assert cfg.serial_baud == 19200
    assert cfg.serial_unit_id == 1


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(ValueError, match="transport") as exc_info:
        Config(
            dongle_host="h",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            transport="unknown_transport",
        )
    assert "dongle" in str(exc_info.value)
    assert "modbus_serial" in str(exc_info.value)


def test_modbus_serial_requires_serial_device() -> None:
    with pytest.raises(ValueError, match="serial_device") as exc_info:
        Config(
            dongle_host="h",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            transport="modbus_serial",
            serial_device="",
        )
    assert "modbus_serial" in str(exc_info.value)


def test_modbus_serial_with_device_is_accepted() -> None:
    cfg = Config(
        dongle_host="h",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path="/tmp/x.db",
        transport="modbus_serial",
        serial_device="/dev/ttyUSB0",
    )
    assert cfg.transport == "modbus_serial"
    assert cfg.serial_device == "/dev/ttyUSB0"
    assert cfg.serial_baud == 19200
    assert cfg.serial_unit_id == 1


def test_serial_settings_load_from_toml(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(
        GOOD + 'transport = "modbus_serial"\n'
        'serial_device = "/dev/ttyUSB0"\n'
        "serial_baud = 38400\n"
        "serial_unit_id = 2\n"
    )
    cfg = load(p)
    assert cfg.transport == "modbus_serial"
    assert cfg.serial_device == "/dev/ttyUSB0"
    assert cfg.serial_baud == 38400
    assert cfg.serial_unit_id == 2


def test_the_shipped_example_matches_its_generator() -> None:
    # config.example.toml is generated, and the two drifted apart once already
    # when the poll interval changed. A shipped example that disagrees with the
    # code is a slow way to mislead someone setting up their first install.
    assert Path("config.example.toml").read_text() == example_toml()


def test_stored_settings_win_over_the_file(tmp_path: Path) -> None:
    # The settings page is the newer authority: someone who changes a value
    # there has to see it take effect, not be silently overruled by a file
    # they cannot reach from a tablet.
    from arraysense.config import effective
    from arraysense.settings import SettingsStore
    from arraysense.store.sqlite_store import SqliteStore
    from conftest import TEST_DEVICE

    store = SqliteStore(str(tmp_path / "e.db"), device=TEST_DEVICE)
    settings = SettingsStore(store)
    settings.set("collector.poll_interval", 30.0)
    settings.set("connection.dongle_host", "10.0.0.9")

    p = tmp_path / "c.toml"
    p.write_text(GOOD)
    merged = effective(load(p), settings)
    assert merged.poll_interval == 30.0
    assert merged.dongle_host == "10.0.0.9"
    # Untouched settings leave the file's values alone.
    assert merged.inverter_serial == load(p).inverter_serial
    store.close()


def test_an_empty_stored_value_does_not_blank_the_file_setting(tmp_path: Path) -> None:
    # Every connection setting defaults to empty. An unset one must not
    # overwrite a working serial with nothing and break every poll.
    from arraysense.config import effective
    from arraysense.settings import SettingsStore
    from arraysense.store.sqlite_store import SqliteStore

    store = SqliteStore(str(tmp_path / "e2.db"), device=TEST_DEVICE)
    settings = SettingsStore(store)
    settings.set("connection.dongle_serial", "")

    p = tmp_path / "c.toml"
    p.write_text(GOOD)
    merged = effective(load(p), settings)
    assert merged.dongle_serial == load(p).dongle_serial
    store.close()


def test_a_serial_installation_needs_no_dongle_settings() -> None:
    # The point of a transport choice. Demanding a placeholder host and dongle
    # serial from a machine wired to RS485 would make the choice a fiction, and
    # a placeholder in a required field is a value nobody can tell from a real
    # one later.
    config = Config(
        dongle_host="",
        dongle_serial="",
        inverter_serial="CE12345678",
        database_path="/tmp/x.db",
        transport="modbus_serial",
        serial_device="/dev/rs485",
    )
    assert config.transport == "modbus_serial"


def test_a_dongle_installation_still_needs_its_dongle_settings() -> None:
    # The other half: relaxing the requirement per transport must not relax it
    # for the transport that has always needed them.
    with pytest.raises(ValueError, match="dongle_host must be set"):
        Config(
            dongle_host="",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
        )


def test_the_broadcast_address_is_refused() -> None:
    # Unit 0 is the Modbus broadcast address: write-only by specification, and
    # it never answers a read. A collector pointed at it would poll a silent
    # bus forever while reporting itself configured.
    with pytest.raises(ValueError, match="serial_unit_id must be between 1 and 247"):
        Config(
            dongle_host="",
            dongle_serial="",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            transport="modbus_serial",
            serial_device="/dev/rs485",
            serial_unit_id=0,
        )


def test_a_nonsense_baud_rate_is_refused() -> None:
    with pytest.raises(ValueError, match="serial_baud must be positive"):
        Config(
            dongle_host="",
            dongle_serial="",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            transport="modbus_serial",
            serial_device="/dev/rs485",
            serial_baud=-1,
        )


def test_the_example_config_names_a_path_that_can_actually_be_opened() -> None:
    # Nothing here expands a glob, so a starred example is a path that fails to
    # open with a message about a missing file rather than about a wildcard.
    assert "usbserial-*" not in example_toml()


def test_durability_defaults_to_what_every_installation_already_had() -> None:
    # The choice is new; the behaviour must not be. An installation that says
    # nothing keeps fsyncing every commit.
    assert (
        Config(
            dongle_host="192.0.2.1",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
        ).synchronous
        == "full"
    )


def test_an_unknown_durability_is_refused() -> None:
    # "off" is a real SQLite setting and would silently risk corruption rather
    # than loss, which is a different bargain entirely and not one on offer.
    with pytest.raises(ValueError, match="synchronous must be one of"):
        Config(
            dongle_host="192.0.2.1",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            synchronous="off",
        )


def test_model_and_battery_source_default_to_unset() -> None:
    config = Config(
        dongle_host="192.0.2.1",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path="/tmp/x.db",
    )
    assert config.model == ""
    assert config.battery_source == ""


def test_an_unknown_battery_source_is_refused_naming_the_choices() -> None:
    with pytest.raises(ValueError, match="battery_source must be one of"):
        Config(
            dongle_host="192.0.2.1",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            battery_source="telepathy",
        )


def test_model_and_battery_source_load_from_the_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'dongle_host = "192.0.2.1"\n'
        'dongle_serial = "BA12345678"\n'
        'inverter_serial = "CE12345678"\n'
        f'database_path = "{tmp_path}/x.db"\n'
        'model = "18kPV"\n'
        'battery_source = "relayed"\n'
    )
    config = load(path)
    assert config.model == "18kPV"
    assert config.battery_source == "relayed"


def test_effective_overlays_the_setup_settings(tmp_path: Path) -> None:
    # The wizard writes these; the next boot must read them. Same merge the
    # dongle fields already do, extended to everything setup lets a page change.
    store = SqliteStore(str(tmp_path / "e.db"), device=TEST_DEVICE)
    settings = SettingsStore(store)
    settings.set("connection.transport", "modbus_serial")
    settings.set("connection.serial_device", "/dev/rs485")
    settings.set("connection.serial_baud", 19200)
    settings.set("connection.serial_unit_id", 1)
    settings.set("connection.model", "18kPV")
    settings.set("connection.battery_source", "relayed")
    base = Config(
        dongle_host="",
        dongle_serial="",
        inverter_serial="CE12345678",
        database_path=str(tmp_path / "e.db"),
        transport="modbus_serial",
        serial_device="/dev/placeholder",
    )
    merged = effective(base, settings)
    store.close()
    assert merged.serial_device == "/dev/rs485"
    assert merged.model == "18kPV"
    assert merged.battery_source == "relayed"
