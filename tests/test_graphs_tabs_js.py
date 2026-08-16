"""test_graphs_tabs_js.py — every section of the Graphs page has a tab, and the
weather pin round-trips.

The Graphs page's tab bar and weather pin are pure decisions — which tab is
open, whether a section is on screen, what the pin is stored as — sliced
between the graph-tabs markers and run under node, the same way the settings
page's tab-defs slice is (tests/test_settings_tabs_js.py). Two invariants a
band table can drift from sit here:

* every `sec:` the band table names has a tab, and every tab other than All
  names a section — so a new section cannot be added without a tab, and a tab
  cannot outlive its section;
* the default tab is All, so the tabbed page is today's page exactly.

Skipped where node is not installed; loud if the extraction markers move.

What this cannot catch: it does not render a chart, so the deferred-paint rule
— a chart is only built the first time its section becomes visible, because
constructing a uPlot into a hidden container lays out an empty grid — is not
checked here. That needs a browser. The rule lives in paintBand() and is
verified by measurement on the LXC.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "graphs.html"

_START = "// >>> graph-tabs"
_END = "// <<< graph-tabs"


def _slice() -> str:
    text = PAGE.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, f"graph-tabs markers are out of order in graphs.html: {_START}"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_band_section_has_a_tab_and_every_tab_a_section() -> None:
    # Every section the page can draw — the BANDS table's `sec:` values and the
    # SECTIONS table's keys — must have a tab, and every tab other than All must
    # name a section. A new section added to either table without a tab fails
    # here, and a tab that outlives its section fails too. The packs section has
    # no entry in BANDS (its bands are built per serial), so it is caught by the
    # SECTIONS side.
    html = PAGE.read_text()
    band_secs = set(re.findall(r"sec: '(\w+)'", html))
    section_keys = set(re.findall(r"key: '(\w+)', host:", html))
    all_sections = band_secs | section_keys
    assert all_sections, "no band sections found — has the band table moved?"
    out = _run(
        "console.log('tabs:' + GRAPH_TABS.map(t => t.key).join(','));"
        "console.log('default:' + DEFAULT_GRAPH_TAB);"
    )
    lines = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    tabs = set(lines["tabs"].split(","))
    assert lines["default"] == "all"
    missing = sorted(all_sections - tabs)
    assert not missing, f"sections with no tab: {missing}"
    dead = sorted(tabs - all_sections - {"all"})
    assert not dead, f"tabs with no section: {dead}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_default_tab_is_all() -> None:
    # A bare `/graphs` opens on All — the page exactly as it always was. A
    # remembered tab only decides when no hash is present; the hash wins over
    # everything, so `/graphs#battery` is a link somebody can send.
    out = _run(
        "console.log(wantedGraphTab('', null));"
        "console.log(wantedGraphTab('', 'battery'));"
        "console.log(wantedGraphTab('battery', 'solar'));"
        "console.log(wantedGraphTab('gone', 'ac'));"
    )
    results = out.split("\n")
    assert results[0] == "all", "a bare page should open on All"
    assert results[1] == "battery", "a remembered tab should win over the default"
    assert results[2] == "battery", "the hash should win over the remembered tab"
    assert results[3] == "ac", "an unknown hash should fall back to the remembered tab"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pin_state_round_trips() -> None:
    # write-then-read through the same helpers the page uses, so a stored pin
    # survives a reload, and a key never written reads back as unpinned.
    out = _run(
        "const held = graphPinTo(true);"
        "const back = graphPinOf(held);"
        "const off = graphPinOf('0');"
        "const never = graphPinOf(null);"
        "console.log('pinned:' + back);"
        "console.log('unpinned:' + off);"
        "console.log('never:' + never);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["pinned"] == "true"
    assert results["unpinned"] == "false"
    assert results["never"] == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pin_keeps_weather_visible_under_another_tab() -> None:
    # The point of the pin: studying Solar with the sky beside it. The pin and
    # the Weather tab both resolve to the one weather section, so it is shown
    # once whether the tab or the pin (or both) asked for it. A weather section
    # with nothing recorded stays hidden whatever the pin says.
    out = _run(
        "console.log('solar_no_pin:' "
        "+ graphSectionVisible('solar', 'solar', false, true));\n"
        "console.log('weather_no_pin_solar:' "
        "+ graphSectionVisible('weather', 'solar', false, true));\n"
        "console.log('weather_pinned_solar:' "
        "+ graphSectionVisible('weather', 'solar', true, true));\n"
        "console.log('weather_pinned_weather_tab:' "
        "+ graphSectionVisible('weather', 'weather', true, true));\n"
        "console.log('weather_tab_no_pin:' "
        "+ graphSectionVisible('weather', 'weather', false, true));\n"
        "console.log('battery_under_solar:' "
        "+ graphSectionVisible('battery', 'solar', false, true));\n"
        "console.log('all_everything:' "
        "+ graphSectionVisible('ac', 'all', false, true));\n"
        "console.log('weather_empty:' "
        "+ graphSectionVisible('weather', 'all', true, false));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["solar_no_pin"] == "true"
    assert results["weather_no_pin_solar"] == "false", (
        "weather should hide under Solar unless pinned"
    )
    assert results["weather_pinned_solar"] == "true", "the pin is the whole point: sky beside solar"
    assert results["weather_pinned_weather_tab"] == "true", (
        "the tab still shows weather when pinned"
    )
    assert results["weather_tab_no_pin"] == "true"
    assert results["battery_under_solar"] == "false", "a section tab hides the other sections"
    assert results["all_everything"] == "true", "All is today's page, every section"
    assert results["weather_empty"] == "false", "no weather data hides the section even when pinned"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_packs_section_hides_on_a_device_that_declares_no_packs() -> None:
    # Per-pack bands are hardware, not readings. A machine whose driver
    # declares no per-module battery has no packs to draw, and an empty Packs
    # section reads as a fault. Unknown keeps it, as everywhere else.
    out = _run(
        "console.log('declared_none:' + graphSectionVisible('packs', 'all', false, true, false));\n"
        "console.log('declared_some:' + graphSectionVisible('packs', 'all', false, true, true));\n"
        "console.log('undeclared:' + graphSectionVisible('packs', 'all', false, true));\n"
        "console.log('other_section:' + graphSectionVisible('solar', 'all', false, true, false));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["declared_none"] == "false", "no per-module battery means no Packs section"
    assert results["declared_some"] == "true"
    assert results["undeclared"] == "true", "unknown must not suppress"
    assert results["other_section"] == "true", "the gate is the packs section alone"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_packs_tab_leaves_with_its_section() -> None:
    # A tab that opens a hidden section is a blank page, and a link somebody
    # sent to /graphs#packs must land somewhere real on a machine without them.
    out = _run(
        "console.log('none:' + graphTabsFor(false).map(t => t.key).join(','));\n"
        "console.log('some:' + graphTabsFor(true).map(t => t.key).join(','));\n"
        "console.log('hash:' + wantedGraphTab('packs', null, graphTabsFor(false)));\n"
        "console.log('stored:' + wantedGraphTab('', 'packs', graphTabsFor(false)));\n"
        "console.log('kept:' + wantedGraphTab('packs', null, graphTabsFor(true)));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert "packs" not in results["none"].split(","), "the tab goes with the section"
    assert "packs" in results["some"].split(",")
    assert results["hash"] == "all", "a link to a section this device lacks lands on All"
    assert results["stored"] == "all", "a remembered tab this device lacks falls back to All"
    assert results["kept"] == "packs"
