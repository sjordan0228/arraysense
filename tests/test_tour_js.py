"""test_tour_js.py — the tour's steps are gates over the payloads the pages read.

The guided tour is state-aware: each step decides from /api/capabilities,
/api/status and /api/settings whether it runs at all, and a step's copy never
states a number or a capability value — it anchors and points, so it cannot
drift from the card it describes. Those decisions live as pure functions in
common.js between the tour-logic markers, and this runs the exact slice under
node — the same way tests/test_dashboard_caps_js.py checks caps-logic — so a
step that would describe a string on a machine that has none, or a module grid
on a bank that relays only aggregate, is caught here rather than in Chrome.
Skipped where node is not installed, and loud if the extraction markers move,
so the slice cannot drift out from under it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"

_TOUR_START = "// >>> tour-logic"
_TOUR_END = "// <<< tour-logic"
_CAPS_START = "// >>> caps-logic"
_CAPS_END = "// <<< caps-logic"

# The step list and its gates reuse capStrings, so the test runs the caps-logic
# slice ahead of the tour-logic slice — exactly as the browser does, where both
# live in one file.


def _slice(start: str, end: str) -> str:
    text = COMMON.read_text()
    a = text.index(start)
    b = text.index(end)
    assert a < b, f"markers out of order in common.js: {start}"
    return text[a:b]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice(_CAPS_START, _CAPS_END) + "\n" + _slice(_TOUR_START, _TOUR_END) + "\n" + body
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

# The status shapes that decide whether the tour runs at all, and the settings
# shapes that decide whether the Costs step exists.
STATUS = """
const FRESH = { running: true, staleness: { any_rows: false, verdict: "fresh" } };
const STOPPED = { running: false, staleness: { verdict: "not_running" } };
"""

SETTINGS = """
const TARIFF = { values: { "tariff.bands": "Peak | 0.34 | 16:00-21:00" } };
const NO_TARIFF = { values: { "tariff.bands": "" } };
const BLANK_TARIFF = { values: { "tariff.bands": "   " } };
const EMPTY_VALUES = { values: {} };
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_step_ids_are_unique_and_pages_are_known() -> None:
    out = _run(
        CAPS
        + "console.log(JSON.stringify([new Set(TOUR_STEPS.map(s=>s.id)).size, "
        + "TOUR_STEPS.map(s=>s.page).every(p=>"
        + '["now","flow","inverter","graphs","history","costs","efficiency","settings"]'
        + ".includes(p))]));"
    )
    assert out == "[7,true]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_step_restates_a_number() -> None:
    # A tour step never states a number or a threshold: it anchors and points,
    # so it cannot drift from the card it describes. A digit in any copy is a
    # claim that goes stale the moment the reading beside it changes.
    out = _run(
        CAPS + "console.log(String(TOUR_STEPS.filter(s => /[0-9]/.test(s.title + s.body)).length));"
    )
    assert out == "0"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_module_grid_step_needs_declared_per_module_battery() -> None:
    out = _run(
        CAPS
        + STATUS
        + "console.log(JSON.stringify(["
        + "tourHasModules(FULL, FRESH, null), tourHasModules(SMALL, FRESH, null), "
        + "tourHasModules(BARE, FRESH, null)]));"
    )
    assert out == "[true,false,false]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_backup_step_mirrors_the_pages_own_gate() -> None:
    # The dashboard renders the Legs panel when backup_output !== false; the
    # tour's rule is the rendering rule, read from the same place. null (a bare
    # source) keeps the step because the page still draws the panel.
    out = _run(
        CAPS
        + STATUS
        + "console.log(JSON.stringify(["
        + "tourHasBackup(FULL, FRESH, null), tourHasBackup(SMALL, FRESH, null), "
        + "tourHasBackup(BARE, FRESH, null), tourHasBackup(null, FRESH, null)]));"
    )
    assert out == "[true,false,true,true]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_strings_step_fails_closed_on_unknown() -> None:
    # null pv_strings is unknown, never zero; a step that would describe string
    # bands must skip rather than invent them, and zero must skip too.
    out = _run(
        CAPS
        + STATUS
        + "console.log(JSON.stringify(["
        + "tourHasStrings(FULL, FRESH, null), tourHasStrings(SMALL, FRESH, null), "
        + "tourHasStrings(BARE, FRESH, null), tourHasStrings({pv_strings:0}, FRESH, null)]));"
    )
    assert out == "[true,true,false,false]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_tariff_step_reads_settings_not_costs() -> None:
    out = _run(
        STATUS
        + SETTINGS
        + "console.log(JSON.stringify(["
        + "tourHasTariff(null, FRESH, TARIFF), tourHasTariff(null, FRESH, NO_TARIFF), "
        + "tourHasTariff(null, FRESH, BLANK_TARIFF), tourHasTariff(null, FRESH, EMPTY_VALUES), "
        + "tourHasTariff(null, FRESH, null)]));"
    )
    assert out == "[true,false,false,false,false]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_collector_not_running_suppresses_the_tour() -> None:
    out = _run(
        STATUS
        + "console.log(JSON.stringify(["
        + "tourSuppressed(STOPPED), tourSuppressed(FRESH), "
        + 'tourSuppressed({running:true,staleness:{verdict:"stopped"}}), tourSuppressed(null)]));'
    )
    assert out == "[true,false,false,false]"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_fresh_install_keeps_every_step_and_omits_the_no_tariff_costs() -> None:
    # any_rows false is the normal state seconds after setup; it must not
    # suppress anything. With no tariff, the Costs step is the only one skipped.
    out = _run(
        CAPS + STATUS + "console.log(tourPassingSteps(FULL, FRESH, null).map(s=>s.id).join(','));"
    )
    assert out == "now-live,now-modules,inverter-legs,flow-sankey,graphs-bands,graphs-strings"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_declared_tariff_adds_the_costs_step() -> None:
    out = _run(
        CAPS
        + STATUS
        + SETTINGS
        + "console.log(tourPassingSteps(FULL, FRESH, TARIFF).map(s=>s.id).join(','));"
    )
    assert out == (
        "now-live,now-modules,inverter-legs,flow-sankey,graphs-bands,graphs-strings,costs-priced"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_reduced_device_skips_what_it_does_not_have() -> None:
    out = _run(
        CAPS + STATUS + "console.log(tourPassingSteps(SMALL, FRESH, null).map(s=>s.id).join(','));"
    )
    assert out == "now-live,flow-sankey,graphs-bands,graphs-strings"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_bare_source_keeps_only_steps_an_unknown_may_keep() -> None:
    # Unknown must not suppress what the page still renders (the Legs step, by
    # the rendering rule), and must not invent what it cannot describe (modules
    # and strings fail closed).
    out = _run(
        CAPS + STATUS + "console.log(tourPassingSteps(BARE, FRESH, null).map(s=>s.id).join(','));"
    )
    assert out == "now-live,inverter-legs,flow-sankey,graphs-bands"
