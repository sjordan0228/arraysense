"""test_graphs_tabs_js.py — every section of the Graphs page has a tab, the
weather pin round-trips, and the circuit strips and summary are chosen by rule.

The Graphs page's tab bar, weather pin and circuit-strip selection are pure
decisions — which tab is open, whether a section is on screen, what the pin is
stored as, which circuits are drawn and what the expander says — sliced between
markers and run under node, the same way the settings page's tab-defs slice is
(tests/test_settings_tabs_js.py). Two invariants a band table can drift from sit
here:

* every `sec:` the band table names has a tab, and every tab other than All
  names a section — so a new section cannot be added without a tab, and a tab
  cannot outlive its section;
* the default tab is All, so the tabbed page is today's page exactly.

Skipped where node is not installed; loud if the extraction markers move.

What this cannot catch: it does not render a chart, so the deferred-paint rule
— a chart is only built the first time its section becomes visible, because
constructing a uPlot into a hidden container lays out an empty grid — is not
checked here. Neither is anything about how a null draws: that a gap breaks the
line rather than being bridged is uPlot's `spanGaps`, and only a canvas can show
it. Both need a browser. The rules live in paintBand() and in common.js's
trace(), and are verified by measurement on the LXC.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "graphs.html"


def _slice(marker: str = "graph-tabs") -> str:
    text = PAGE.read_text()
    start = text.index(f"// >>> {marker}")
    end = text.index(f"// <<< {marker}")
    assert start < end, f"{marker} markers are out of order in graphs.html"
    return text[start:end]


def _run(body: str, marker: str = "graph-tabs") -> str:
    assert NODE is not None
    script = _slice(marker) + "\n" + body
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


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_circuits_tab_is_offered_only_where_emporia_is_running() -> None:
    # A tab that opens an empty section is the defect #12 named: a page must
    # render from what the installation declares, not from one owner's shape.
    # An installation with no Emporia account has no circuits to draw.
    out = _run(
        "console.log('with:' + graphTabsFor(true, true).map(t => t.key).join(','));"
        "console.log('without:' + graphTabsFor(true, false).map(t => t.key).join(','));"
    )
    with_circuits, without = (line.split(":", 1)[1] for line in out.splitlines())
    assert "circuits" in with_circuits.split(",")
    assert "circuits" not in without.split(",")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_link_to_the_circuits_tab_lands_on_all_where_there_are_none() -> None:
    # /graphs#circuits sent between two owners must land somewhere real for a
    # receiver with no Emporia account, exactly as /graphs#packs already does
    # for one with no packs.
    out = _run(
        "const tabs = graphTabsFor(true, false);"
        "console.log(wantedGraphTab('circuits', null, tabs));"
    )
    assert out == "all"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_circuits_section_hides_until_something_has_been_read() -> None:
    # Enabled is not the same fact as having data. The tab is offered on the
    # owner's switch, so a module turned on a minute ago — or one whose saved
    # login Emporia has rejected — reaches this page with an enabled module and
    # no readings, and a bare heading over nothing is what that used to draw.
    # Unknown hides it, which is the opposite of the packs default and matches
    # the tab rule: circuits have never been shown to be present.
    out = _run(
        "console.log('with:' + graphSectionVisible('circuits', 'all', false, true, true, true));\n"
        "console.log('without:' "
        "+ graphSectionVisible('circuits', 'all', false, true, true, false));\n"
        "console.log('undeclared:' + graphSectionVisible('circuits', 'all', false, true, true));\n"
        "console.log('own_tab_empty:' "
        "+ graphSectionVisible('circuits', 'circuits', false, true, true, false));\n"
        "console.log('other_section:' "
        "+ graphSectionVisible('solar', 'all', false, true, true, false));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["with"] == "true"
    assert results["without"] == "false", "an enabled module with no readings draws no section"
    assert results["undeclared"] == "false", "unknown hides it — the opposite of the packs rule"
    assert results["own_tab_empty"] == "false", "the tab says why instead of opening an empty box"
    assert results["other_section"] == "true", "the gate is the circuits section alone"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_five_strips_are_drawn_and_the_expander_counts_the_rest() -> None:
    # Five is alerts.DEFAULT_TOP's answer to "which loads matter", and the
    # reference account has thirty-nine circuits: drawing them all is thousands
    # of pixels of scrolling before the reader learns anything. The order is the
    # endpoint's ranking by energy, so the five drawn are the five that used the
    # most and the expander holds the tail of the same list.
    out = _run(
        "const list = Array.from({length: 39}, (_, i) => ({name: 'c' + i}));\n"
        "console.log('folded:' + circuitsDrawn(list, false).map(c => c.name).join(','));\n"
        "console.log('expanded:' + circuitsDrawn(list, true).length);\n"
        "console.log('short:' + circuitsDrawn(list.slice(0, 3), false).length);\n"
        "console.log('words:' + circuitsMoreWords(34, false));\n"
        "console.log('one:' + circuitsMoreWords(1, false));\n"
        "console.log('back:' + circuitsMoreWords(34, true));",
        marker="circuit-rules",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["folded"] == "c0,c1,c2,c3,c4", "the top five, in the endpoint's own order"
    assert results["expanded"] == "39", "expanding draws the rest of the same answer"
    assert results["short"] == "3", "fewer than five is not padded out"
    assert results["words"] == "show the other 34", "the count is the offer"
    assert results["one"] == "show the other one"
    assert results["back"] == "show fewer"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_circuit_that_reported_nothing_gets_a_row_and_no_chart() -> None:
    # The rule the whole page turns on. A circuit heard from nowhere in the
    # window did not draw 0 W, and an empty plot reads as a fault while a
    # dropped row reads as a circuit that no longer exists — so it gets a row
    # saying why and no chart at all. A circuit measured at zero is a reading
    # and keeps its strip.
    out = _run(
        "const since = '2026-04-02T13:45:00.000Z';\n"
        "const dead = {watts: [null, null], kwh: null, offline_since: since};\n"
        "const quiet = {watts: [null, null], kwh: null, offline_since: null};\n"
        "const zero = {watts: [0, 0], kwh: 0, offline_since: null};\n"
        "const live = {watts: [null, 40], kwh: 0.01, offline_since: null};\n"
        "console.log('dead:' + circuitSilent(dead));\n"
        "console.log('quiet:' + circuitSilent(quiet));\n"
        "console.log('zero:' + circuitSilent(zero));\n"
        "console.log('live:' + circuitSilent(live));\n"
        "console.log('why_dead:' + circuitWhy(dead, '2 Apr 2026'));\n"
        "console.log('why_quiet:' + circuitWhy(quiet, ''));",
        marker="circuit-rules",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["dead"] == "true"
    assert results["quiet"] == "true"
    assert results["zero"] == "false", "a reading of no power is a reading"
    assert results["live"] == "false", "one reading among nulls is still a series"
    assert results["why_dead"] == "offline since 2 Apr 2026"
    assert results["why_quiet"] == "nothing recorded in this range", (
        "silence Emporia has not explained must not be reported as a fault"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_window_nobody_reported_in_is_not_worth_a_section() -> None:
    # What decides whether the section appears at all. One circuit that reported
    # is enough, because the offline rows are worth reading beside a strip. A
    # window in which nothing reported is a sentence, not thirty-nine rows all
    # saying the same thing.
    out = _run(
        "const dead = {watts: [null, null], kwh: null};\n"
        "const live = {watts: [null, 40], kwh: 0.01};\n"
        "console.log('some:' + circuitsWorthDrawing({circuits: [dead, live]}));\n"
        "console.log('none:' + circuitsWorthDrawing({circuits: [dead, dead]}));\n"
        "console.log('empty:' + circuitsWorthDrawing({circuits: []}));\n"
        "console.log('nothing:' + circuitsWorthDrawing(null));",
        marker="circuit-rules",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["some"] == "true"
    assert results["none"] == "false"
    assert results["empty"] == "false", "an enabled module with no circuits draws no section"
    assert results["nothing"] == "false", "nothing fetched is not a section either"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_summary_opens_on_energy_and_remembers_the_other() -> None:
    # Energy is the default because it needs no hue at all: length carries the
    # whole meaning of a ranked bar, and the palette this project validated
    # holds five colours against a stack that wants one per band. A stored
    # value that names no view falls back rather than blanking the panel.
    out = _run(
        "console.log('fresh:' + circuitViewOf(null));\n"
        "console.log('held:' + circuitViewOf('split'));\n"
        "console.log('back:' + circuitViewOf('energy'));\n"
        "console.log('junk:' + circuitViewOf('sankey'));\n"
        "console.log('default:' + DEFAULT_CIRCUIT_VIEW);",
        marker="circuit-summary",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["fresh"] == "energy", "a browser that has never chosen opens on Energy"
    assert results["held"] == "split", "the other view survives a reload"
    assert results["back"] == "energy"
    assert results["junk"] == "energy", "a key naming no view must not blank the panel"
    assert results["default"] == "energy"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_circuit_with_no_energy_figure_is_listed_rather_than_ranked() -> None:
    # The rule this panel turns on. A null kWh is not a zero-length bar: "used
    # no energy" and "nobody heard from it" are different claims, and drawing
    # the second as the first puts a dead outlet at the foot of a ranking as
    # though it were merely quiet. A measured zero is a reading and keeps its
    # bar. The order is the endpoint's own ranking and is not sorted again.
    out = _run(
        "const dryer = {id: 1, name: 'Dryer', kwh: 4.2, watts: [4000]};\n"
        "const idle = {id: 2, name: 'Porch', kwh: 0, watts: [0]};\n"
        "const dead = {id: 3, name: 'Shed', kwh: null, watts: [null]};\n"
        "const rows = circuitEnergyRows([dryer, idle, dead]);\n"
        "console.log('ranked:' + rows.ranked.map(c => c.name).join(','));\n"
        "console.log('unknown:' + rows.unknown.map(c => c.name).join(','));\n"
        "console.log('empty:' + circuitEnergyRows(null).ranked.length);",
        marker="circuit-summary",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["ranked"] == "Dryer,Porch", "a reading of no energy is still a reading"
    assert results["unknown"] == "Shed", "a circuit nobody heard from is not ranked at zero"
    assert results["empty"] == "0"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_recorded_seconds_count_what_was_heard_and_not_the_holes() -> None:
    # The measurement the coverage sentence is qualified by. A stamp that
    # carried a reading is credited with the distance to the next one, capped at
    # the spacing the window achieved — so the stamp on the near side of a
    # three-hour hole cannot claim the hole. The repository's synthetic nulls
    # are what mark the far side of it.
    out = _run(
        "const solid = [{watts: [10, 10, 10, 10]}];\n"
        "console.log('solid:' + circuitRecordedSeconds([0, 60, 120, 180], solid, 60));\n"
        "const holed = [{watts: [10, null, 10, 10]}];\n"
        "console.log('holed:' + circuitRecordedSeconds([0, 60, 10000, 10060], holed, 60));\n"
        "console.log('lone:' + circuitRecordedSeconds([0], [{watts: [10]}], 60));\n"
        "console.log('none:' + circuitRecordedSeconds([], [], 60));\n"
        "console.log('silent:' + circuitRecordedSeconds([0, 60], [{watts: [null, null]}], 60));",
        marker="circuit-summary",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["solid"] == "240", "four minute-spaced readings cover four minutes"
    assert results["holed"] == "180", "the hole belongs to nobody, not to the reading before it"
    assert results["lone"] == "60", "one stamp covers one poll, measured from the given cadence"
    assert results["none"] == "0"
    assert results["silent"] == "0", "stamps carrying nothing are not time recorded"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_coverage_line_says_the_right_one_of_its_four_things() -> None:
    # The line that can state a falsehood with every number in it correct. Four
    # states, and three of them are a statement about what cannot be divided.
    #
    # The fourth is the one measured on the bench: a seven-day window in which
    # the module had recorded for six hours returned circuits 18.392 kWh, house
    # 254.8 kWh, fraction 0.0722. "Monitored circuits cover 7% of the house"
    # invites the reader to conclude the house is barely monitored, when the
    # truth is that the module was not running. The percentage is withheld there
    # rather than re-based, because this response carries no house figure for
    # the recorded span and assuming the house drew evenly would be an estimate
    # wearing a meter reading's clothes.
    out = _run(
        "const w6 = 6 * 3600, w7 = 7 * 86400;\n"
        "console.log('normal:' + circuitCoverageWords("
        "{circuits_kwh: 8.1, house_kwh: 11.4, fraction: 0.7105}, w6, w6));\n"
        "console.log('unknown:' + circuitCoverageWords("
        "{circuits_kwh: 8.1, house_kwh: null, fraction: null}, w6, w6));\n"
        "console.log('over:' + circuitCoverageWords("
        "{circuits_kwh: 12.4, house_kwh: 9.8, fraction: 1.2653}, w6, w6));\n"
        "console.log('short:' + circuitCoverageWords("
        "{circuits_kwh: 18.392, house_kwh: 254.8, fraction: 0.0722}, 21600, w7));",
        marker="circuit-summary",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["normal"] == (
        "Monitored circuits cover 71% of the house this window — 8.10 kWh of 11.40 kWh."
    )
    assert results["unknown"] == (
        "The house's own energy is unknown for this window, so the share these circuits "
        "account for cannot be given."
    ), "a null share is neither 0% nor 100%"
    assert "0%" not in results["unknown"] and "100%" not in results["unknown"]
    assert results["over"].startswith(
        "These circuits account for 12.40 kWh against the house's own 9.80 kWh."
    ), "a part above the whole is a fault reporting itself, shown as both figures"
    assert "127%" not in results["over"] and "100%" not in results["over"], (
        "the endpoint stopped clamping so this line could tell the truth; it must not clamp either"
    )
    assert results["short"] == (
        "The circuits recorded for 6.0 h of this 7-day window, so their 18.39 kWh is not a "
        "share of the house's 254.8 kWh across the whole of it. A range the module was "
        "running for will give the share."
    )
    assert "7%" not in results["short"], (
        "the ratio of two different spans is not a coverage figure and must not be printed"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_healthy_window_is_not_qualified_and_a_gappy_one_is() -> None:
    # The threshold is 90% of the window, and it has to clear ordinary
    # operation: a healthy installation loses at most one poll at each edge,
    # which is two minutes of the shortest range this page offers.
    out = _run(
        "const w6 = 6 * 3600;\n"
        "console.log('healthy:' + circuitSpanShort(w6 - 120, w6));\n"
        "console.log('edge:' + circuitSpanShort(w6 * 0.9, w6));\n"
        "console.log('gappy:' + circuitSpanShort(w6 * 0.34, w6));\n"
        "console.log('nowindow:' + circuitSpanShort(0, 0));",
        marker="circuit-summary",
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["healthy"] == "false", "one poll lost at each edge is not a qualified window"
    assert results["edge"] == "false", "the threshold is a floor, not an exclusive bound"
    assert results["gappy"] == "true", "a third of the window recorded must be said out loud"
    assert results["nowindow"] == "false", "no window is not a short one"
