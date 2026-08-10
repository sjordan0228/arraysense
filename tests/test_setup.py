"""Tests for arraysense.setup — the payload the wizard renders from."""

from __future__ import annotations

from pathlib import Path

from arraysense.config import Config, load
from arraysense.setup import describe_setup, list_serial_ports, render_config


def _config(**overrides: object) -> Config:
    base: dict[str, object] = {
        "dongle_host": "192.0.2.1",
        "dongle_serial": "BA12345678",
        "inverter_serial": "CE12345678",
        "database_path": "/tmp/test.db",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def test_ports_come_from_by_id_with_resolved_targets(tmp_path: Path) -> None:
    # Stable names first: /dev/serial/by-id survives replugging, /dev/ttyUSB0
    # does not, and a monitoring box silently attached to the wrong device is
    # worse than one that fails loudly.
    by_id = tmp_path / "serial" / "by-id"
    by_id.mkdir(parents=True)
    target = tmp_path / "ttyUSB0"
    target.touch()
    (by_id / "usb-1a86_USB_Serial-if00-port0").symlink_to(target)
    ports = list_serial_ports(dev_root=tmp_path)
    assert len(ports) == 1
    assert ports[0]["stable"].endswith("usb-1a86_USB_Serial-if00-port0")
    assert ports[0]["target"].endswith("ttyUSB0")


def test_no_serial_directory_is_an_empty_list_not_an_error(tmp_path: Path) -> None:
    assert list_serial_ports(dev_root=tmp_path) == []


def test_the_payload_carries_the_tree_requirements_and_choices() -> None:
    payload = describe_setup(_config(model="18kPV"))
    eg4 = next(m for m in payload["manufacturers"] if m["name"] == "EG4")
    names = [model["name"] for model in eg4["models"]]
    assert names == ["18kPV", "6000XP", "12kPV"]
    assert payload["transports"]["modbus_serial"] == ["serial_device"]
    assert payload["transports"]["dongle"] == ["dongle_host", "dongle_serial"]
    assert payload["current"]["model"] == "18kPV"
    assert payload["current"]["dongle_serial"] != "BA12345678", "secrets must be redacted"


def test_model_deltas_declare_their_citation_status() -> None:
    payload = describe_setup(_config())
    eg4 = next(m for m in payload["manufacturers"] if m["name"] == "EG4")
    cited = next(m for m in eg4["models"] if m["name"] == "18kPV")
    inherited = next(m for m in eg4["models"] if m["name"] == "6000XP")
    assert cited["citation"]
    assert inherited["citation"] == ""


def test_render_config_writes_a_file_load_accepts(tmp_path: Path) -> None:
    # First run writes the one config file the installation will ever get
    # from software. The only correctness that matters: load() accepts what
    # render_config wrote, with every value intact.
    text = render_config(
        {
            "driver": "eg4_luxpower",
            "transport": "modbus_serial",
            "serial_device": "/dev/rs485",
            "inverter_serial": "3352000000",
            "model": "18kPV",
            "battery_source": "relayed",
            "database_path": str(tmp_path / "arraysense.db"),
        }
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    config = load(path)
    assert config.serial_device == "/dev/rs485"
    assert config.model == "18kPV"
    assert config.dongle_host == ""
