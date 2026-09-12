"""test_overnight_page_js.py -- what the overnight page is allowed to draw.

The planner's honesty rules live in the page: a crossing is a window and not a
point when the inputs support only a window, an unknown state of charge draws
a badge and never a confident curve, and interpolated text arrives escaped.
These run the marker slice from overnight.html under node with a stubbed
/api/overnight response -- the pattern test_settings_tabs_js.py uses -- so a
page that smoothed a range into a point, or interpolated a raw string, fails
here rather than in Chrome. The stubbed response is the healthy shape of the
slice-3 contract: scenarios keyed by name with their trajectory pairs, the
replay block, the inputs block, the guidance list, and the assumptions list.

Two of these boot the whole page instead of the slice, because the fault that
reached production on 2026-09-12 lived below the slice: the page drew nothing
at all, and the sentence it left on screen blamed the endpoint. What the page
does with a failed fetch and what it does with a drawing fault are separate
questions from how it draws, and both are asked here.

Skipped where node is not installed; loud if the extraction markers move.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"
PAGE = WEB / "overnight.html"
COMMON = WEB / "common.js"
SPRITE = WEB / "phosphor-2.svg"

_PAGE_START = "// >>> overnight-page"
_PAGE_END = "// <<< overnight-page"


def _slice() -> str:
    text = PAGE.read_text()
    start = text.index(_PAGE_START)
    end = text.index(_PAGE_END)
    assert start < end, "overnight-page markers are out of order in overnight.html"
    return text[start:end]


# esc is common.js verbatim so the escape assertions below measure the page's
# use of it, not a paraphrase. document and uPlot are the minimum the slice
# touches: id-keyed boxes that record their innerHTML, and a chart
# constructor that keeps every argument it was handed. The constructor takes
# three of them because the vendored build does -- see
# test_the_chart_is_handed_its_data_before_its_element for what the middle
# one has to be.
_PRELUDE = """
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const captured = {};
const document = {
  getElementById: (id) => (captured[id] ??= { innerHTML: '' }),
};
const recorded = [];
class FakeChart {
  constructor(cfg, data, el) {
    recorded.push({ cfg, data, el });
  }
}
const uPlot = FakeChart;
"""


def _run(data: dict[str, Any], body: str) -> str:
    """Run the marker slice under node with DATA bound, and return stdout."""
    assert NODE is not None
    script = _PRELUDE + "\n" + _slice() + "\nconst DATA = " + json.dumps(data) + ";\n" + body
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _page_script() -> str:
    """The page's own inline script: the last of them, since the first settles
    the appearance before the body exists."""
    blocks: list[str] = re.findall(r"<script>(.*?)</script>", PAGE.read_text(), re.S)
    assert len(blocks) >= 2, "overnight.html no longer has its two inline scripts"
    script = blocks[-1]
    assert "function drawPlan(" in script, "the last inline script is not the page's"
    return script


# Booting the page needs more of the browser than drawing it does: every box
# the wiring touches, a fetch that answers the plan route and never settles the
# charge routes (so the panel's own reads cannot race this one), and a chart
# constructor that behaves like the vendored build.
_BOOT_PRELUDE = """
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const captured = {};
const document = {
  getElementById: (id) => (captured[id] ??= {
    id, innerHTML: '', textContent: '', hidden: false, disabled: false,
    value: '3', style: {}, dataset: {}, clientWidth: 600,
    addEventListener() {}, appendChild() {},
    classList: {add() {}, remove() {}, toggle() {}},
  }),
  querySelector: () => null,
  querySelectorAll: () => [],
};
const recorded = [];
// The vendored build takes whatever it is handed second as its columns and
// reads a length off the first of them, so an element passed there throws
// inside the constructor. Emulating that is the point of this stub: a page
// that gets the order wrong has to fail here, not only in a browser. The
// same goes for a series' dash, which the build hands straight to the
// canvas's setLineDash and which therefore has to be a pattern.
class FakeChart {
  constructor(cfg, data, el) {
    const columns = data || cfg.data || [];
    const first = columns[0];
    if (typeof (first || {}).length !== 'number') {
      throw new TypeError("Cannot read properties of undefined (reading 'length')");
    }
    for (const s of cfg.series) {
      if (s.dash !== undefined && !Array.isArray(s.dash)) {
        throw new TypeError(
          'setLineDash: the object must have a callable @@iterator property');
      }
    }
    recorded.push({ cfg, data, el });
  }
}
const uPlot = FakeChart;
const PLAN = __PLAN__;
const PLAN_FAILS = __PLAN_FAILS__;
const fetch = (url) => {
  if (String(url).startsWith('/api/overnight')) {
    if (PLAN_FAILS) return Promise.reject(new TypeError('Failed to fetch'));
    return Promise.resolve({ ok: true, json: async () => PLAN });
  }
  return new Promise(() => {});
};
const drawNav = (name) => { captured.nav = name; };
"""


def _boot(plan: dict[str, Any], body: str, plan_fails: bool = False) -> str:
    """Run the whole page under node, with the plan route stubbed, and return
    stdout. The page boots itself, so the body waits for it to settle."""
    assert NODE is not None
    script = (
        _BOOT_PRELUDE.replace("__PLAN__", json.dumps(plan)).replace(
            "__PLAN_FAILS__", "true" if plan_fails else "false"
        )
        + "\n"
        + _page_script()
        + "\n"
        + body
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return result.stdout.strip()


_SETTLED = """
(async () => {
  await new Promise((resolve) => setTimeout(resolve, 25));
  %s
})();
"""


def _curve(end_soc: float) -> list[list[Any]]:
    return [
        ["2026-09-08T19:35:00-05:00", 62.1],
        ["2026-09-09T02:10:00-05:00", 44.0],
        ["2026-09-09T05:00:00-05:00", end_soc],
    ]


def _scenario(
    assumption: str,
    end_soc: float,
    crossing: str | None,
    import_start: str | None,
    window: tuple[str, str] | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "status": "ok",
        "reason": None,
        "trajectory": _curve(end_soc),
        "trajectory_band": [],
        "reserve_crossing": crossing,
        "import_start": import_start,
        "assumptions": [assumption],
    }
    if window is not None:
        entry["reserve_window"] = list(window)
        entry["range_basis"] = "the spread across three comparable nights"
    return entry


def _healthy(stale: bool = False) -> dict[str, Any]:
    return {
        "scenarios": {
            "typical": _scenario(
                "Average of three comparable nights.",
                31.2,
                "2026-09-09T03:40:00-05:00",
                "2026-09-09T03:55:00-05:00",
            ),
            # The essential scenario is the one whose inputs support a range:
            # its reserve window spans ninety minutes, so the page must show
            # both edges and never the midpoint alone.
            "essential": _scenario(
                "Essential loads only, 100 W held throughout.",
                38.4,
                "2026-09-09T03:35:00-05:00",
                "2026-09-09T04:35:00-05:00",
                window=(
                    "2026-09-09T02:50:00-05:00",
                    "2026-09-09T04:20:00-05:00",
                ),
            ),
            "scheduled": _scenario(
                "A 1200 W scheduled load runs before midnight.",
                22.6,
                "2026-09-09T01:15:00-05:00",
                "2026-09-09T01:20:00-05:00",
            ),
        },
        "replay": {"nights": [], "crossing_errors_minutes": [], "wh_errors": []},
        "inputs": {
            "soc_now_pct": 62.1,
            "usable_capacity_ah": 280.0,
            "min_soc_pct": 30.0,
            "efficiency_pct": 96.0,
            "discharge_limit_w": 3000.0,
            "calibration_severity": "ok",
            "drift_band_pct": None,
            "stale": stale,
            "emporia_enabled": False,
        },
        "guidance": ["The reserve floor is crossed before dawn in every scenario."],
        "assumptions": [
            "The grid is assumed available for this projection.",
            "Unmeasured branch loads are <script>estimates</script>.",
        ],
    }


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_overnight_page_is_a_registered_route() -> None:
    """The file exists and app.py names it, or the nav link is a 404."""
    from arraysense.api import app as api_app

    assert api_app.PAGES.get("/overnight") == "overnight.html"
    assert PAGE.exists()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_nav_links_the_overnight_page() -> None:
    """The shared nav carries the entry, and any icon it names exists in the
    sprite -- a missing symbol renders as a blank square, not an error."""
    nav = re.search(r"const NAV = \[(.*?)\];", COMMON.read_text(), re.S)
    assert nav is not None, "the NAV table is no longer one array"
    assert "href:'/overnight'" in nav.group(1), "no nav link to /overnight"
    entry = re.search(r"\{[^{}]*href:'/overnight'[^{}]*\}", nav.group(1))
    assert entry is not None, "the overnight nav entry is malformed"
    icon = re.search(r"icon:\s*'(ph-[a-z0-9-]+)'", entry.group(0))
    if icon is not None:
        assert f'id="{icon.group(1)}"' in SPRITE.read_text(), (
            f"{icon.group(1)} is referenced but the sprite has no such symbol"
        )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_chart_draws_three_scenario_lines_and_a_reserve_line() -> None:
    """The series carry the three scenario names the API returns, in that
    order, and the reserve line holds at min_soc_pct rather than a guessed
    height. Node prints booleans lowercase; the assertions read that."""
    out = _run(
        _healthy(),
        """
drawPlan(DATA);
const cfg = recorded[recorded.length - 1].cfg;
console.log('SERIES:' + cfg.series.map((s) => s.label).join(','));
const reserve = cfg.series.find((s) => s.label === 'reserve');
console.log('RESERVE:' + String(
  reserve !== undefined && reserve.data.every((v) => v === 30)));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["SERIES"].split(",") == ["x", "typical", "essential", "scheduled", "reserve"]
    assert lines["RESERVE"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_reserve_line_is_dashed_with_a_pattern_and_not_a_function() -> None:
    """The floor line is drawn dashed so it reads as a reference and not as a
    measurement, and the library hands whatever is in `dash` to the canvas's
    setLineDash, which takes a pattern of numbers. A function there throws
    inside the draw pass: the axes and the scenario lines are already painted
    and the reserve line is the series that never lands, which is a chart that
    looks complete while missing the one line the page's honesty rule is
    about. The rest of the repo's chart specs pass a pattern."""
    out = _run(
        _healthy(),
        """
drawPlan(DATA);
const cfg = recorded[recorded.length - 1].cfg;
const reserve = cfg.series.find((s) => s.label === 'reserve');
const dash = reserve === undefined ? null : reserve.dash;
console.log('PATTERN:' + String(
  Array.isArray(dash) && dash.length > 0 && dash.every((n) => typeof n === 'number')));
console.log('OTHERS:' + String(cfg.series
  .filter((s) => s.label !== 'reserve')
  .every((s) => s.dash === undefined)));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["PATTERN"] == "true", "the reserve line's dash is not a canvas pattern"
    assert lines["OTHERS"] == "true", "a solid scenario line came with a dash"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_chart_is_handed_its_data_before_its_element() -> None:
    """The vendored build reads its data from the constructor's second
    argument and falls back to opts.data, so an element handed over second is
    taken for the data: the constructor then reads a column off it, finds
    none, and throws. On the page that is not a blank chart but a whole plan
    section that never fills, because the throw happens before the rows, the
    guidance and the assumptions are written -- measured on production
    2026-09-12, where the chart had been built this way since #242 and the
    plan had therefore never drawn at all."""
    out = _run(
        _healthy(),
        """
drawPlan(DATA);
const last = recorded[recorded.length - 1];
console.log('COLUMNS:' + String(Array.isArray(last.data) && last.data.length === 5));
console.log('STAMPS:' + String(Array.isArray(last.data[0]) && last.data[0].length === 3));
console.log('ELEMENT:' + String(last.el === captured['planChartWrap']));
console.log('NOTDATA:' + String(last.data !== last.el));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["COLUMNS"] == "true", "the chart was not handed its data columns"
    assert lines["STAMPS"] == "true", "the first column is not the shared timestamps"
    assert lines["ELEMENT"] == "true", "the chart was not handed the wrap element"
    assert lines["NOTDATA"] == "true", "the wrap element arrived as the data"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_plan_that_answers_is_drawn_in_full() -> None:
    """Booting the page with a healthy plan fills every part of the section:
    the state of charge line, the three scenario rows, the guidance and the
    assumptions. Production 2026-09-12 reached the reader with all four empty
    and an exception in the note, because the throw happened part-way through
    the drawing and nothing after it ran."""
    out = _boot(
        _healthy(),
        _SETTLED
        % """
  console.log('NOTE:' + captured.planNote.innerHTML);
  console.log('ROWS:' + (captured.scenRows.innerHTML.match(/<tr/g) || []).length);
  console.log('GUIDANCE:' + (captured.guidanceBox.innerHTML.match(/<li/g) || []).length);
  console.log('ASSUMPTIONS:' + (captured.assumptionsBox.innerHTML.match(/<li/g) || []).length);
  console.log('CHARTS:' + recorded.length);
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert "62.1% state of charge" in lines["NOTE"], lines["NOTE"]
    assert "did not answer" not in lines["NOTE"]
    assert "could not draw" not in lines["NOTE"]
    assert lines["ROWS"] == "3"
    assert lines["GUIDANCE"] == "1"
    assert lines["ASSUMPTIONS"] == "2"
    assert lines["CHARTS"] == "1"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_plan_the_page_cannot_draw_does_not_blame_the_endpoint() -> None:
    """A drawing fault and an unanswered request are different faults, and the
    sentence has to say which one happened. One catch around both of them sent
    the reader to the service for a fault that was on the page, which is the
    misdirection that cost the diagnosis on 2026-09-12."""
    broken = _healthy()
    broken["scenarios"]["typical"]["trajectory"] = "not a curve"
    page_out = _boot(
        broken,
        _SETTLED
        % """
  console.log('NOTE:' + captured.planNote.innerHTML);
""",
    )
    assert "could not draw" in page_out, page_out
    assert "did not answer" not in page_out

    fetch_out = _boot(
        _healthy(),
        _SETTLED
        % """
  console.log('NOTE:' + captured.planNote.innerHTML);
""",
        plan_fails=True,
    )
    assert "did not answer" in fetch_out, fetch_out
    assert "could not draw" not in fetch_out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_direct_chart_call_passes_the_data_before_the_element() -> None:
    """The same shape, checked across the sources rather than in one page.

    A chart built with two arguments does not fail loudly: it throws inside
    the vendored library, and only a page that catches that and says so keeps
    the symptom on screen at all. The vendored build is what fixes the order,
    so the version it is taken from is asserted here as well -- a new drop
    under the same name means measuring the call shape again, which is the
    point of failing here rather than in a browser.
    """
    vendor = (WEB / "uPlot.iife.min.js").read_text()
    version = re.search(r"uPlot \(v([0-9.]+)\)", vendor)
    assert version is not None, "the vendored uPlot no longer names its version"
    assert version.group(1) == "1.6.32", (
        "the vendored uPlot changed: re-check that it still takes its data as "
        "the constructor's second argument before trusting this guard"
    )

    found: list[str] = []
    for path in [*sorted(WEB.glob("*.html")), COMMON]:
        text = path.read_text()
        for call in _chart_call_arguments(text):
            assert len(call) >= 3, (
                f"{path.name}: new uPlot(...) is called with {len(call)} "
                "argument(s); the vendored build takes (opts, data, element)"
            )
            assert not call[1].lstrip().startswith("document."), (
                f"{path.name}: the element is being passed where the data goes"
            )
            found.append(path.name)
    # Two call sites are known: the shared paint() helper and the overnight
    # page. A sweep that finds none has stopped looking, not found clean code.
    assert len(found) >= 2, f"the sweep found {len(found)} direct chart call(s): {found}"


def _chart_call_arguments(text: str) -> list[list[str]]:
    """Every new uPlot(...) call's arguments, split on top-level commas."""
    calls: list[list[str]] = []
    for match in re.finditer(r"new uPlot\(", text):
        depth = 1
        index = match.end()
        while index < len(text) and depth:
            if text[index] in "([{":
                depth += 1
            elif text[index] in ")]}":
                depth -= 1
            index += 1
        arguments: list[str] = []
        current = ""
        depth = 1
        for char in text[match.end() : index - 1]:
            if char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            if char == "," and depth == 1:
                arguments.append(current)
                current = ""
                continue
            current += char
        if current.strip():
            arguments.append(current)
        calls.append([argument.strip() for argument in arguments])
    return calls


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_scenario_rows_carry_names_end_soc_and_times() -> None:
    """One row per scenario the response answered, with its end SoC read off
    the trajectory's last point, and the two times worth acting on."""
    out = _run(
        _healthy(),
        """
const rows = scenarioRows(DATA);
console.log('ROWS:' + (rows.match(/<tr/g) || []).length);
console.log('NAMES:' + ['typical', 'essential', 'scheduled']
  .every((n) => rows.includes(n)));
console.log('SOCS:' + ['31.2', '38.4', '22.6'].every((v) => rows.includes(v)));
console.log('TIMES:' + String(rows.includes('03:40') && rows.includes('03:55')));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["ROWS"] == "3"
    assert lines["NAMES"] == "true"
    assert lines["SOCS"] == "true"
    assert lines["TIMES"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_ranged_crossing_shows_the_window_and_not_its_midpoint() -> None:
    """The honesty rule this page exists to keep: when the inputs support a
    range, the page shows the range. A page that rendered the midpoint alone
    would be the fabricated-precision bug the issue calls out, so the exact
    midpoint is present in the data and must not reach the row."""
    out = _run(
        _healthy(),
        """
const rows = scenarioRows(DATA);
const cells = rows.split('<tr').slice(1);
console.log('WINDOW:' + cells[1].includes('02:50 to 04:20'));
console.log('NOMID:' + !cells[1].includes('03:35'));
console.log('POINT:' + String(cells[0].includes('03:40') && !cells[0].includes(' to ')));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["WINDOW"] == "true"
    assert lines["NOMID"] == "true"
    assert lines["POINT"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unanswered_scenario_gets_no_row() -> None:
    """The API sends null for a scenario it did not model. Inventing a row
    for it would draw a line and a number for a night nobody projected."""
    data = copy.deepcopy(_healthy())
    data["scenarios"]["scheduled"] = None
    out = _run(
        data,
        """
const rows = scenarioRows(DATA);
console.log('ROWS:' + (rows.match(/<tr/g) || []).length);
console.log('SCHED:' + rows.includes('scheduled'));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["ROWS"] == "2"
    assert lines["SCHED"] == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_guidance_entries_render_as_list_items_escaped() -> None:
    """Guidance arrives as plain sentences and leaves as <li> items; markup
    inside one stays data, which is what esc is for."""
    data = _healthy()
    data["guidance"] = [
        'One entry with <b>markup</b> and "quotes".',
        "A second entry that must also render.",
    ]
    out = _run(
        data,
        """
const items = guidanceList(DATA);
console.log('ITEMS:' + (items.match(/<li/g) || []).length);
console.log('SAFE:' + String(!items.includes('<b>') && items.includes('&lt;b&gt;')));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["ITEMS"] == "2"
    assert lines["SAFE"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_assumption_text_is_escaped_wherever_it_lands() -> None:
    """The assumptions carry a deliberate <script> pair. Rendered raw it is a
    page that trusts its data; rendered escaped it reads as the sentence it
    is. The escaped form must be present and the raw form absent."""
    out = _run(
        _healthy(),
        """
drawPlan(DATA);
const box = captured['assumptionsBox'];
console.log('ESCAPED:' + box.innerHTML.includes('&lt;script&gt;'));
console.log('RAWDONE:' + box.innerHTML.includes('<script>'));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["ESCAPED"] == "true"
    assert lines["RAWDONE"] == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_stale_soc_gets_a_badge_and_a_fresh_one_does_not() -> None:
    """The stale marker is the page's honest-limitation state: the projection
    still draws, but it says its reading is old. A fresh reading must not
    carry the warning, or the badge stops meaning anything."""
    stale_out = _run(
        _healthy(stale=True),
        """
drawPlan(DATA);
console.log('STALE:' + captured['planNote'].innerHTML.includes('badge stale'));
""",
    )
    fresh_out = _run(
        _healthy(stale=False),
        """
drawPlan(DATA);
console.log('FRESH:' + captured['planNote'].innerHTML.includes('badge stale'));
""",
    )
    assert stale_out == "STALE:true"
    assert fresh_out == "FRESH:false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_scenario_without_a_curve_invents_no_number() -> None:
    """A refused projection answers with a reason and no trajectory. The row
    may still name the scenario, but a 0.0% drawn there would read as a
    night that ended full -- a missing reading is a dash, never a zero."""
    data = _healthy()
    data["scenarios"]["typical"] = {
        "status": "no-data",
        "reason": "Only one comparable night of history answered.",
        "trajectory": [],
        "trajectory_band": [],
        "reserve_crossing": None,
        "import_start": None,
        "assumptions": [],
    }
    out = _run(
        data,
        """
const rows = scenarioRows(DATA);
console.log('NOZERO:' + !rows.includes('0.0%'));
console.log('REASON:' + rows.includes('Only one comparable night'));
""",
    )
    lines = dict(line.split(":", 1) for line in out.split("\n") if ":" in line)
    assert lines["NOZERO"] == "true"
    assert lines["REASON"] == "true"
