"""test_dashboard_caps_js.py — the pages draw only what the device declares.

The dashboard and graphs pages gate what they render on /api/capabilities: how
many PV string rows the detail draws, whether the Legs and BMS panels exist,
which small-multiple bands are built and fetched. Those decisions live as pure
functions in common.js — capStrings and capHasMetric, between the caps-logic
markers — and this runs the exact slice under node, the same way
tests/test_wizard_js.py checks the wizard's. The rule under test cuts both
ways: a metric the declaration leaves out is hardware that does not exist and
must not be drawn, while no declaration at all is unknown and must suppress
nothing — absent capability is not absent data. Skipped where node is not
installed, and loud if the extraction markers move, so the slice cannot drift
out from under it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"

_START = "// >>> caps-logic"
_END = "// <<< caps-logic"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "caps-logic markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


# Three device entries in the shapes /api/capabilities actually serves: the
# reference three-string 18kPV; a reduced one-string machine with no backup
# panel and no battery (the EG4 3000 EHV's shape); and a bare source that
# named its device but declared nothing — every field null, which must read as
# "unknown", never as "produces nothing".
CAPS = """
const FULL = {
  device: "CE12345678", driver: "eg4_luxpower", model: "18kPV",
  pv_strings: 3, energy: "counted", backup_output: true, split_phase: true,
  per_module_battery: true, transport: "dongle",
  metrics: ["pv1_power_w", "pv2_power_w", "pv3_power_w", "battery_soc_pct",
            "eps_l1_power_w", "eps_power_w", "load_power_w"],
};
const SMALL = {
  device: "CE87654321", driver: "eg4_luxpower", model: "EG4 3000 EHV",
  pv_strings: 1, energy: "estimated", backup_output: false, split_phase: false,
  per_module_battery: false, transport: "modbus_serial",
  metrics: ["pv1_power_w", "load_power_w", "grid_power_w"],
};
const BARE = {
  device: "CE00000000", driver: null, model: null,
  pv_strings: null, energy: null, backup_output: null, split_phase: null,
  per_module_battery: null, transport: null,
  metrics: null, battery_module_metrics: null,
};
"""

# The same shape and predicate graphs.html filters its BANDS with: a band is
# kept only when every inverter metric it reads — its own, or the operands a
# computed band names in `needs` — is one the device declares. The derived
# spread bands carry neither and must pass untouched.
BANDS = """
const BANDS = [
  { id: "pv1p", metric: "pv1_power_w" },
  { id: "pv2p", metric: "pv2_power_w" },
  { id: "pv3p", metric: "pv3_power_w" },
  { id: "loadP", metric: "load_power_w" },
  { id: "loadNon", needs: ["load_power_w", "eps_power_w"] },
  { id: "socSpread", derived: "soc_pct" },
];
const neededBy = (b) => b.needs || (b.metric ? [b.metric] : []);
const kept = (caps) =>
  BANDS.filter((b) => neededBy(b).every((m) => capHasMetric(caps, m)))
    .map((b) => b.id).join(",");
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_string_counts_come_from_the_declaration() -> None:
    out = _run(CAPS + "console.log(JSON.stringify([capStrings(FULL), capStrings(SMALL)]));")
    assert out == "[3,1]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_unknown_strings_are_null_never_zero() -> None:
    # null means "fall back to what the page always drew"; zero would erase
    # every string row from an installation that merely has not declared.
    out = _run(
        CAPS
        + "console.log(JSON.stringify("
        + "[capStrings(BARE), capStrings(null), capStrings({}), capStrings({pv_strings:'3'})]));"
    )
    assert out == "[null,null,null,null]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_declared_metric_answers_yes_and_an_undeclared_one_no() -> None:
    out = _run(
        CAPS
        + "console.log(JSON.stringify(["
        + 'capHasMetric(FULL, "pv2_power_w"), capHasMetric(SMALL, "pv2_power_w"), '
        + 'capHasMetric(SMALL, "pv1_power_w")]));'
    )
    assert out == "[true,false,true]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_unknown_capabilities_suppress_nothing() -> None:
    # A bare source declares no metric list and a build without the endpoint
    # yields no caps at all; neither is a device declaring less, so both must
    # keep every chart and panel rather than blanking the page.
    out = _run(
        CAPS
        + "console.log(JSON.stringify(["
        + 'capHasMetric(BARE, "pv3_power_w"), capHasMetric(null, "pv3_power_w"), '
        + 'capHasMetric({}, "pv3_power_w")]));'
    )
    assert out == "[true,true,true]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_band_filter_drops_absent_strings_and_orphaned_operands() -> None:
    # The one-string machine keeps its own string and loses the other two; the
    # passthrough band goes with them because one of its operands
    # (eps_power_w) is undeclared, and a difference with a missing operand is
    # a guess. The derived spread band names no inverter metric and stays.
    out = _run(CAPS + BANDS + "console.log(kept(SMALL));")
    assert out == "pv1p,loadP,socSpread"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_band_filter_keeps_everything_for_the_full_device() -> None:
    out = _run(CAPS + BANDS + "console.log(kept(FULL));")
    assert out == "pv1p,pv2p,pv3p,loadP,loadNon,socSpread"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_band_filter_keeps_everything_when_nothing_has_declared() -> None:
    out = _run(CAPS + BANDS + 'console.log([kept(BARE), kept(null)].join("|"));')
    assert out == ("pv1p,pv2p,pv3p,loadP,loadNon,socSpread|pv1p,pv2p,pv3p,loadP,loadNon,socSpread")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_bms_witness_is_state_of_charge() -> None:
    # The dashboard's BMS panel renders when the device has a battery, and the
    # witness is battery_soc_pct: any battery reports its state of charge
    # whatever else it relays. The batteryless SMALL hides the panel; the bare
    # source keeps it, because unknown must not suppress.
    out = _run(
        CAPS
        + "console.log(JSON.stringify(["
        + 'capHasMetric(FULL, "battery_soc_pct"), capHasMetric(SMALL, "battery_soc_pct"), '
        + 'capHasMetric(BARE, "battery_soc_pct")]));'
    )
    assert out == "[true,false,true]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_only_an_explicit_no_hides_the_pack_views() -> None:
    # The Battery modules card and the Graphs Packs section draw hardware the
    # device may not have. A machine that declares none must not be offered a
    # card saying its CAN link is down, since there is no link to be down; a
    # machine that has not declared keeps both, because unknown is not absent.
    out = _run(
        CAPS
        + "console.log(JSON.stringify(["
        + "capHasModules(FULL), capHasModules(SMALL), "
        + "capHasModules(BARE), capHasModules(null), capHasModules({})]));"
    )
    assert out == "[true,false,true,true,true]"
