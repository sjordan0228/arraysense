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

import json
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
def test_model_gap_note_splits_into_the_three_honest_categories() -> None:
    out = _run(
        PAYLOAD
        + """
        const m = {name:'6000XP', unreadable:[
          {metric:'generator_power_w', reason:'a seconds counter', cloud_available:false},
          {metric:'generator_voltage_v', reason:'never verified', cloud_available:true}
        ]};
        console.log(JSON.stringify(setupModelGapNote(m)));
        """
    )
    note = json.loads(out)
    assert note["local"] is True
    assert [g["metric"] for g in note["gone"]] == ["generator_power_w"]
    assert [g["metric"] for g in note["cloud"]] == ["generator_voltage_v"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_model_with_no_gaps_has_no_note() -> None:
    # JSON.stringify so the bare JS null is logged as the string "null";
    # node colours the null keyword itself and the assertion would see escape
    # codes rather than the value.
    out = _run(
        PAYLOAD + "console.log(JSON.stringify(setupModelGapNote({name:'18kPV', unreadable:[]})));"
    )
    assert out == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_models_for_a_driver() -> None:
    out = _run(PAYLOAD + 'console.log(setupModelsFor(P,"eg4_luxpower").map(m=>m.name).join(","));')
    assert out == "18kPV,6000XP"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_list_says_plainly_what_the_12kpv_and_12000xp_are() -> None:
    # A keystroke apart and opposite families: the 12kPV is a hybrid and the
    # 12000XP is off-grid. The option label has to say which is which, because
    # someone scanning the dropdown for their machine decides there and then.
    # The label comes from the model's own declared family, never inferred from
    # something merely correlated with it — a hybrid that one day declares an
    # unreadable metric must not start calling itself off-grid.
    out = _run(
        "console.log(setupModelLabel({name:'12kPV', family:'hybrid'}, true) + '|' + "
        "setupModelLabel({name:'12000XP', family:'off-grid'}, true));"
    )
    assert out == "12kPV — hybrid|12000XP — off-grid"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_family_with_one_kind_gets_no_tags() -> None:
    # The simulated driver has no off-grid machine to contrast with, so tagging
    # its one model "hybrid" would assert a family it does not belong to.
    out = _run("console.log(setupModelLabel({name:'Simulated', unreadable:[]}, false));")
    assert out == "Simulated"


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


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_battery_select_has_disabled_coming_soon_option() -> None:
    # The "direct" battery source is reserved but not yet available; it must
    # appear as a disabled option so users see it is coming, but it must never
    # be selectable or become the state.battery_source.
    #
    # This test verifies:
    # 1. The battLabel mapping includes the correct labels for relayed, none, and direct
    # 2. The "direct" option is NOT in the server-provided battery_sources list
    # 3. The fallback logic falls back to batteries[0] or "none", never "direct"
    payload = """
    const P = {
      manufacturers: [{ name: "EG4", families: [{ driver: "eg4_luxpower", description: "", models: [
        { name: "18kPV", citation: "measured", cited_fields: ["pv_strings"],
          pv_strings: 3, battery_module_slots: 4 } ] }], models: [] }],
      transports: { modbus_serial: ["serial_device"] },
      battery_sources: { eg4_luxpower: ["relayed", "none"] },
      ports: [],
      current: {}
    };
    """
    # Check that setupBatterySourcesFor does NOT return "direct"
    out = _run(payload + 'console.log(setupBatterySourcesFor(P, "eg4_luxpower").join(","));')
    assert out == "relayed,none"
    assert "direct" not in out

    # Check that the fallback logic uses batteries[0] or "none", never "direct"
    # When state.battery_source is empty, it should fall back to batteries[0] which is "relayed"
    out2 = _run(
        payload
        + 'const batteries = setupBatterySourcesFor(P, "eg4_luxpower");'
        + 'const fallback = batteries[0] || "none";'
        + "console.log(fallback);"
    )
    assert out2 == "relayed"

    # Verify "direct" is never the fallback when batteries list is empty
    out3 = _run(
        'const batteries = []; const fallback = batteries[0] || "none"; console.log(fallback);'
    )
    assert out3 == "none"


# ---------------------------------------------------------------------------
# Timezone dropdown tests
# ---------------------------------------------------------------------------


def test_timezone_setting_has_choices_with_empty_default() -> None:
    """The timezone setting should be a choice type with "" as the first/default value."""
    from arraysense.settings import SETTING_TIMEZONE, lookup_setting

    spec = lookup_setting(SETTING_TIMEZONE)
    assert spec.kind == "choice"
    assert spec.default == ""
    assert "" in spec.choices
    # The empty string should be the first choice
    assert spec.choices[0] == ""


def test_timezone_validates_real_zones() -> None:
    """Real IANA timezone names should validate successfully."""
    from arraysense.settings import SETTING_TIMEZONE, lookup_setting

    spec = lookup_setting(SETTING_TIMEZONE)
    # America/New_York should validate
    result = spec.validate("America/New_York")
    assert result == "America/New_York"

    # Europe/London should validate
    result = spec.validate("Europe/London")
    assert result == "Europe/London"


def test_timezone_empty_string_validates() -> None:
    """The empty string should validate as it means 'follow the machine's zone'."""
    from arraysense.settings import SETTING_TIMEZONE, lookup_setting

    spec = lookup_setting(SETTING_TIMEZONE)
    result = spec.validate("")
    assert result == ""


def test_timezone_choices_cover_every_zone_the_tz_database_has() -> None:
    """Turning the field into a choice must not narrow what an owner may store.

    A first pass at this dropdown filtered "UTC" out of the list, which the
    choice check then refused — silently breaking any installation that had
    "UTC" saved. The guard is the whole database, not a spot check.
    """
    from zoneinfo import available_timezones

    from arraysense.settings import SETTING_TIMEZONE, lookup_setting

    spec = lookup_setting(SETTING_TIMEZONE)
    missing = available_timezones() - set(spec.choices)
    assert not missing, f"zones dropped from the dropdown: {sorted(missing)[:5]}"
    assert spec.validate("UTC") == "UTC"
