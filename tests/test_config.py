"""Tests for runtime configuration: arraysense.config."""

from __future__ import annotations

from pathlib import Path

import pytest

from arraysense.config import Config, example_toml, load

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

    store = SqliteStore(str(tmp_path / "e.db"))
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

    store = SqliteStore(str(tmp_path / "e2.db"))
    settings = SettingsStore(store)
    settings.set("connection.dongle_serial", "")

    p = tmp_path / "c.toml"
    p.write_text(GOOD)
    merged = effective(load(p), settings)
    assert merged.dongle_serial == load(p).dongle_serial
    store.close()
