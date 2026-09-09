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
# constructor that keeps the last config it was handed.
_PRELUDE = """
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const captured = {};
const document = {
  getElementById: (id) => (captured[id] ??= { innerHTML: '' }),
};
const recorded = [];
class FakeChart {
  constructor(cfg, el) {
    recorded.push(cfg);
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
const cfg = recorded[recorded.length - 1];
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
