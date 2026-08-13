"""test_sync_groups_js.py — a chart syncs only with charts drawn from the same
x array, on the same page.

The chart factory once put every chart on every page into one sync group
(key "arraysense"). uPlot syncs a drag-zoom as the *values* of the dragged
window, so a zoom on one family of bands was applied to every other band on
every page — and on the graphs page the weather bands arrive on a fifteen-
minute clock and the per-pack bands from a separate endpoint, so a zoom on the
inverter bands pushed those bands' x window outside the range their data lives
in and their y axes collapsed to uPlot's un-ranged default. That is the blank
second band onward: the page has the data, the chart has no x window to draw it
in.

Sync is now keyed per page, and within a page per x array: the default group
comes from the path a chart is served under, and a page that draws more than
one x-array family passes each other family its own key. This runs the key
resolution — the slice between the sync-groups markers — under node, the same
way tests/test_dashboard_caps_js.py runs the caps logic, and checks the two
pages that draw several families declare their groups. Skipped where node is
not installed, and loud if the markers move so the slice cannot drift out from
under it.

What this cannot catch: it does not render a chart, so a regression that puts
two x-array families back into one group while still resolving keys correctly
per page would pass here unless the page assertion below names it. That page
assertion exists because of exactly that gap.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
ROOT = Path(__file__).resolve().parent.parent
COMMON = ROOT / "src" / "arraysense" / "web" / "common.js"
GRAPHS = ROOT / "src" / "arraysense" / "web" / "graphs.html"
INDEX = ROOT / "src" / "arraysense" / "web" / "index.html"

_START = "// >>> sync-groups"
_END = "// <<< sync-groups"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "sync-groups markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_pages_get_distinct_sync_groups() -> None:
    # A chart on the dashboard must not drag a chart on the graphs page into
    # its zoom, and vice versa. Distinct pages => distinct default keys.
    out = _run("""
const cases = {
  '/': 'arraysense-dashboard',
  '/graphs': 'arraysense-graphs',
  '/graphs.html': 'arraysense-graphs',
  '/history': 'arraysense-history',
  '/efficiency': 'arraysense-efficiency',
};
const bad = [];
for (const [path, want] of Object.entries(cases)) {
  const got = pageSyncKeyFor(path);
  if (got !== want) bad.push(`${path}: want ${want}, got ${got}`);
}
if (bad.length) throw new Error(bad.join('\\n'));
console.log('pages distinct: ok');
""")
    assert "pages distinct: ok" in out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_false_opts_out_and_key_overrides_default() -> None:
    # `sync: false` must mean no sync at all — the forecast chart relies on it.
    # `sync: { key }` names a family group within the page; anything else falls
    # back to the page default.
    out = _run("""
const bad = [];
const eq = (label, got, want) => {
  if (got !== want) bad.push(`${label}: want ${JSON.stringify(want)}, got ${JSON.stringify(got)}`);
};
eq('false', syncKeyFor(false, '/graphs'), null);
eq('null', syncKeyFor(null, '/graphs'), null);
eq('undefined', syncKeyFor(undefined, '/graphs'), 'arraysense-graphs');
eq('true', syncKeyFor(true, '/graphs'), 'arraysense-graphs');
eq('empty object', syncKeyFor({}, '/graphs'), 'arraysense-graphs');
eq('explicit key', syncKeyFor({ key: 'graphs-weather' }, '/graphs'), 'graphs-weather');
eq('explicit key on dashboard', syncKeyFor({ key: 'graphs-packs' }, '/'), 'graphs-packs');
if (bad.length) throw new Error(bad.join('\\n'));
console.log('sync option honoured: ok');
""")
    assert "sync option honoured: ok" in out


def test_graphs_page_names_its_other_x_array_families() -> None:
    # The graphs page draws three x-array families: the inverter bands (which
    # take the page default), the weather bands, and the per-pack / spread
    # bands. The latter two must each name a key of their own, or they would
    # share the inverter group and a drag on the inverter bands would push
    # their axes to a range with no points.
    text = GRAPHS.read_text()
    assert "{ key: 'graphs-weather' }" in text, (
        "graphs.html draws the weather bands into their own sync group"
    )
    assert "{ key: 'graphs-packs' }" in text, (
        "graphs.html draws the per-pack and spread bands into their own sync group"
    )


def test_dashboard_forecast_still_opts_out() -> None:
    # The forecast chart is pinned to a fixed calendar day while its neighbours
    # follow the selected window, so it must stay out of the dashboard group.
    text = INDEX.read_text()
    assert "sync: false" in text, "index.html's forecast chart passes sync: false"


def test_chart_base_uses_the_resolution_not_a_global_key() -> None:
    # chartBase must resolve the key through syncKeyFor rather than carrying one
    # hardcoded constant — the moment a second global key appears, the per-page
    # guarantee is gone and nothing in the other tests can see it.
    common = COMMON.read_text()
    assert "syncKeyFor(out.sync, window.location.pathname)" in common, (
        "chartBase resolves each chart's sync key through syncKeyFor"
    )
