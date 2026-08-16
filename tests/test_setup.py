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
    # Exact equality, not a subset: the failure worth catching is a gap
    # silently dropping OUT of the list, which means a metric silently
    # coming back. A subset check would catch additions and miss removals,
    # which is the wrong direction. Extend this list when a gap is added.
    assert names == ["18kPV", "12kPV", "FlexBOSS21", "FlexBOSS18", "6000XP", "12000XP"]
    assert payload["transports"]["modbus_serial"] == ["serial_device"]
    assert payload["transports"]["dongle"] == ["dongle_host", "dongle_serial"]
    assert payload["current"]["model"] == "18kPV"
    assert payload["current"]["dongle_serial"] != "BA12345678", "secrets must be redacted"


def test_model_deltas_declare_their_citation_status() -> None:
    payload = describe_setup(_config())
    eg4 = next(m for m in payload["manufacturers"] if m["name"] == "EG4")
    cited = next(m for m in eg4["models"] if m["name"] == "18kPV")
    offgrid = next(m for m in eg4["models"] if m["name"] == "6000XP")
    assert cited["citation"]
    assert cited["cited_fields"] == ["pv_strings"]
    # The 6000XP's string count used to be an open question upstream and it
    # inherited the family's 3. The EG4 spec sheet settled it — NUMBER OF
    # MPPTS 2, INPUTS PER MPPT 1 — so it now cites its own count. The fact
    # worth guarding is that the citation names the spec sheet, so a future
    # reader can tell a fact moved rather than a test bent to fit.
    assert offgrid["citation"]
    assert "6000XP spec sheet" in offgrid["citation"]
    assert offgrid["cited_fields"] == ["pv_strings"]


def test_describe_setup_carries_the_unreadable_gap_list() -> None:
    payload = describe_setup(_config())
    eg4 = next(m for m in payload["manufacturers"] if m["name"] == "EG4")
    six = next(m for m in eg4["models"] if m["name"] == "6000XP")
    names = [g["metric"] for g in six["unreadable"]]
    # Exact equality, not a subset: the failure worth catching is a gap
    # silently dropping OUT of the list, which means a metric silently
    # coming back. A subset check would catch additions and miss removals,
    # which is the wrong direction. Extend this list when a gap is added.
    assert names == [
        "generator_power_w",
        "generator_voltage_v",
        "generator_frequency_hz",
        "grid_export_energy_today_kwh",
        "grid_export_energy_total_kwh",
    ]
    assert all(g["reason"] and g["citation"] for g in six["unreadable"])
    assert all("cloud_available" in g for g in six["unreadable"])

    # Every model the payload offers has the key, so a page reads it
    # unconditionally rather than branching on whether it happens to be there.
    assert all("unreadable" in model for model in eg4["models"])

    # The hybrids read everything the family does.
    for name in ("18kPV", "12kPV", "FlexBOSS21", "FlexBOSS18"):
        model = next(m for m in eg4["models"] if m["name"] == name)
        assert model["unreadable"] == [], f"{name} must declare no gaps"


def test_describe_setup_offers_the_12000xp_with_citation_and_gaps() -> None:
    payload = describe_setup(_config())
    eg4 = next(m for m in payload["manufacturers"] if m["name"] == "EG4")
    model = next(m for m in eg4["models"] if m["name"] == "12000XP")
    assert model["citation"]
    assert "12000XP spec sheet" in model["citation"]
    assert model["cited_fields"] == ["pv_strings"]
    assert model["pv_strings"] == 2
    names = [g["metric"] for g in model["unreadable"]]
    # Exact equality, not a subset: the failure worth catching is a gap
    # silently dropping OUT of the list, which means a metric silently
    # coming back. A subset check would catch additions and miss removals,
    # which is the wrong direction. Extend this list when a gap is added.
    assert names == [
        "generator_power_w",
        "generator_voltage_v",
        "generator_frequency_hz",
        "grid_export_energy_today_kwh",
        "grid_export_energy_total_kwh",
    ]
    assert all(g["reason"] and g["citation"] for g in model["unreadable"])


def test_a_model_whose_readings_are_unproven_says_so() -> None:
    """A caveat travels to the page, or the page presents a guess as support.

    The EG4 off-grid machines answer at the same register addresses as the
    hybrids and disagree about what several of them hold, so offering one
    silently would put a wrong reading on a chart rather than a gap. Offering it
    labelled is a decision the owner gets to make.
    """
    payload = describe_setup(_config())
    eg4 = next(m for m in payload["manufacturers"] if m["name"] == "EG4")
    offgrid = next(m for m in eg4["models"] if m["name"] == "6000XP")
    assert offgrid["caveat"], "an unproven model must carry its caveat to the page"

    # Every model the payload offers has the key, so a page reads it
    # unconditionally rather than branching on whether it happens to be there.
    assert all("caveat" in model for model in eg4["models"])

    # And a model that has been confirmed carries no caveat, or the label means
    # nothing: FlexBOSS shares the hybrids' device type code and their
    # live-confirmed string count.
    for name in ("18kPV", "12kPV", "FlexBOSS21", "FlexBOSS18"):
        model = next(m for m in eg4["models"] if m["name"] == name)
        assert model["caveat"] == "", f"{name} is confirmed and must carry no caveat"
        assert model["pv_strings"] == 3


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
