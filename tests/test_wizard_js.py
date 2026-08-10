"""test_wizard_js.py — the wizard's decisions are the server's, read back.

The setup wizard renders from /api/setup and must decide nothing the server has
not already said: which models a driver has, which fields a transport needs,
which battery sources it supports, what to send on apply. Those choices live as
pure functions in common.js, and this runs the exact slice under node — the same
way tests/test_settings_band_editor.py checks the tariff editor — so a field
shown that a transport does not need, or a value sent that apply would refuse, is
caught here rather than in Chrome. Skipped where node is not installed, and
loud if the extraction markers move, so the slice cannot drift out from under it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"

_START = "// >>> setup-logic"
_END = "// <<< setup-logic"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "setup-logic markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


PAYLOAD = """
const P = {
  manufacturers: [
    { name: "EG4", families: [{ driver: "eg4_luxpower", description: "", models: [
      { name: "18kPV", citation: "measured", cited_fields: ["pv_strings"],
        pv_strings: 3, battery_module_slots: 4 },
      { name: "6000XP", citation: "", cited_fields: [],
        pv_strings: 2, battery_module_slots: 4 } ] }], models: [] },
    { name: "Simulated", families: [{ driver: "fake", description: "", models: [
      { name: "Simulated", citation: "", cited_fields: [],
        pv_strings: 2, battery_module_slots: 4 } ] }], models: [] } ],
  transports: { dongle: ["dongle_host","dongle_serial"], modbus_serial: ["serial_device"] },
  battery_sources: { eg4_luxpower: ["relayed","none"], fake: ["none"] },
  ports: [{ stable: "/dev/serial/by-id/x", target: "/dev/ttyUSB0" }],
  current: {}
};
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_models_for_a_driver() -> None:
    out = _run(PAYLOAD + 'console.log(setupModelsFor(P,"eg4_luxpower").map(m=>m.name).join(","));')
    assert out == "18kPV,6000XP"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_maker_of_a_driver() -> None:
    out = _run(PAYLOAD + 'console.log(setupMakerOf(P,"fake"));')
    assert out == "Simulated"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_fields_for_each_transport() -> None:
    out = _run(
        PAYLOAD
        + 'console.log(setupFieldsFor(P,"modbus_serial").join(",")'
        + '+"|"+setupFieldsFor(P,"dongle").join(","));'
    )
    assert out == "serial_device|dongle_host,dongle_serial"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unknown_transport_asks_for_nothing() -> None:
    out = _run(PAYLOAD + 'console.log(JSON.stringify(setupFieldsFor(P,"telepathy")));')
    assert out == "[]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_battery_sources_for_a_driver() -> None:
    out = _run(
        PAYLOAD
        + 'console.log(setupBatterySourcesFor(P,"fake").join(",")'
        + '+"|"+setupBatterySourcesFor(P,"eg4_luxpower").join(","));'
    )
    assert out == "none|relayed,none"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unknown_driver_falls_back_to_none() -> None:
    # A driver the payload never mentioned must not offer a battery it cannot
    # read; "none" is the honest floor, never an empty grid.
    out = _run(PAYLOAD + 'console.log(setupBatterySourcesFor(P,"mystery").join(","));')
    assert out == "none"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_body_sends_only_the_transport_it_uses() -> None:
    # A serial install: dongle fields are absent even if the form still holds
    # stale values, and empty/redacted fields are dropped rather than blanking a
    # real one.
    state = (
        'const s = {driver:"eg4_luxpower", model:"18kPV", transport:"modbus_serial", '
        'serial_device:"/dev/ttyUSB0", serial_baud:19200, serial_unit_id:1, '
        'battery_source:"relayed", inverter_serial:"", dongle_host:"leftover", dongle_serial:""};'
    )
    out = _run(PAYLOAD + state + "console.log(Object.keys(buildSetupBody(s)).sort().join(','));")
    assert out == "battery_source,driver,model,serial_baud,serial_device,serial_unit_id,transport"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_body_sends_the_dongle_fields_on_a_dongle() -> None:
    # The dongle port is not a form field, so it never appears in the body even
    # when the state carries a stray one — 8000 is the server's default.
    state = (
        'const s = {driver:"eg4_luxpower", model:"18kPV", transport:"dongle", '
        'dongle_host:"192.0.2.1", dongle_serial:"BA00000000", dongle_port:8000, '
        'serial_device:"leftover", inverter_serial:"CE00000000", battery_source:"relayed"};'
    )
    out = _run(PAYLOAD + state + "console.log(Object.keys(buildSetupBody(s)).sort().join(','));")
    assert out == (
        "battery_source,dongle_host,dongle_serial,driver,inverter_serial,model,transport"
    )
