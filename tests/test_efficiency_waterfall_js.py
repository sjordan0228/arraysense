"""The budget bar is drawn by hand, so these hold it to what it must never do.

Refused energy is not a loss. A bar that shades it like one tells the owner
their array is faulty on exactly the days it behaved perfectly, which is the
whole reason this feature exists and the reason the segment is drawn outside a
gap rather than tinted differently. The gap is the argument, and a gap is
checkable.

Extracted and run under node rather than asserted about by reading, because a
renderer that silently emits nothing looks identical to one that works until
somebody opens the page.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"
SLICE_FROM = "function drawWaterfall("
SLICE_TO = "// Called again whenever the current view changes"

# The helpers the renderer leans on, restated rather than sliced: pulling in the
# whole of common.js would drag the stylesheet and the boot sequence with it.
_PRELUDE = """
const DASH = '\\u2014';
const numOrNull = (v) => typeof v === 'number' && isFinite(v) ? v : null;
const gnum = (v, d) => v.toLocaleString(undefined,
  {minimumFractionDigits: d, maximumFractionDigits: d});
function Host() { this.innerHTML = ''; }
"""


def _slice() -> str:
    text = COMMON.read_text()
    return text[text.index(SLICE_FROM) : text.index(SLICE_TO)]


def _draw(segments: list[dict[str, object]]) -> str:
    """Render the bar for these segments and return the HTML it produced."""
    assert NODE is not None
    body = (
        f"{_PRELUDE}\n{_slice()}\n"
        f"const h = new Host();\n"
        f"drawWaterfall(h, {json.dumps(segments)});\n"
        f"console.log(h.innerHTML);"
    )
    result = subprocess.run(
        ["node", "-e", body], capture_output=True, text=True, check=True
    )
    return result.stdout


def _segments(
    expected: float, actual: float, unexplained: float, curtailed: float, gain: float = 0.0
) -> list[dict[str, object]]:
    return [
        {"name": "expected", "kwh": expected, "penalised": True},
        {"name": "unexplained", "kwh": unexplained, "penalised": True},
        {"name": "curtailed", "kwh": curtailed, "penalised": False},
        {"name": "unmodelled_gain", "kwh": gain, "penalised": False},
        {"name": "actual", "kwh": actual, "penalised": True},
    ]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_curtailed_energy_is_drawn_outside_the_gap() -> None:
    """The one thing this drawing exists to say.

    Curtailed energy sits past a gap and is outlined rather than filled, so a
    reader can see it was never scored. Shade it like the shortfall beside it
    and a demand-limited afternoon reads as a fault.
    """
    html = _draw(_segments(80.0, 67.0, 11.0, 2.0))
    assert "wf-gap" in html, "nothing separates refused energy from lost energy"
    assert html.index("wf-gap") < html.index("wf-curt"), "the gap must precede it"
    assert "not a fault" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_day_with_nothing_refused_draws_no_gap() -> None:
    # A gap standing there with nothing beyond it invites the reader to wonder
    # what is missing.
    html = _draw(_segments(80.0, 70.0, 10.0, 0.0))
    assert "wf-gap" not in html
    assert "wf-curt" not in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_nothing_measured_says_so_rather_than_drawing_an_empty_bar() -> None:
    """An empty bar is a claim; a dash is an admission.

    A bar drawn from nothing reads as "the array was expected to make nothing
    and made nothing", which is the absent-as-zero mistake this project exists
    to avoid.
    """
    for segments in (
        [],
        _segments(0.0, 0.0, 0.0, 0.0),
        [{"name": "expected", "kwh": None, "penalised": True}],
    ):
        html = _draw(segments)  # type: ignore[arg-type]
        assert "wf-actual" not in html, f"drew a bar for {segments}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_day_that_beat_its_model_still_fits_inside_the_bar() -> None:
    """The surplus is already inside actual, so it must not be drawn twice.

    The walk is expected - unexplained - curtailed + gain = actual. Giving the
    gain a segment of its own added its width a second time and pushed the bar
    past the end of its own track by exactly that much -- caught here rather
    than by looking at it, which is the point of drawing under node.
    """
    html = _draw(_segments(60.0, 66.0, 0.0, 4.0, gain=6.0))
    widths = [
        float(part.split("width:")[1].split("%")[0])
        for part in html.split("<div")
        if "width:" in part and "wf-seg" in part
    ]
    assert widths, "no segment was given a width"
    assert sum(widths) <= 100.5, f"the bar overflows its track: {widths}"
    # And the reader is still shown where the model said the day should end.
    assert "wf-mark" in html
    assert "above the model" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_bar_describes_itself_to_a_screen_reader() -> None:
    # The figures are the point, and a div of coloured strips says none of them.
    html = _draw(_segments(80.0, 67.0, 11.0, 2.0))
    assert 'role="img"' in html
    assert "aria-label" in html
    label = html.split('aria-label="')[1].split('"')[0]
    for figure in ("80", "67", "11", "2"):
        assert figure in label, f"the description never mentions {figure}"
    assert "not counted against the array" in label


PAGES = (Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web").glob("*.html")


def test_every_chart_container_carries_the_shared_chart_class() -> None:
    """uPlot escapes any container the shared stylesheet has not positioned.

    `paint()` looks up `<id>Wrap` and builds the plot inside it, and the base
    stylesheet hangs position:relative and the uplot width rule off `.chart`.
    A wrapper that misses that class lets uPlot's absolutely positioned layers
    resolve against the page instead, and the plot draws itself full width
    behind everything else -- which is exactly how the Efficiency chart
    shipped, because nothing in Python or in a linter can see a missing CSS
    class.
    """
    import re

    offenders: list[str] = []
    for page in PAGES:
        html = page.read_text()
        for match in re.finditer(r'<div([^>]*\bid="(\w+)Wrap"[^>]*)>', html):
            attrs, name = match.group(1), match.group(2)
            classes = re.search(r'class="([^"]*)"', attrs)
            if not classes or "chart" not in classes.group(1).split():
                offenders.append(f"{page.name}: {name}Wrap")
    assert not offenders, f"chart wrappers missing the 'chart' class: {offenders}"


def test_every_charting_page_loads_the_uplot_stylesheet() -> None:
    """The library draws; its stylesheet is what lays the canvas out.

    uPlot sizes its canvas to logical pixels times the device pixel ratio and
    relies on its own CSS to position it inside `.u-wrap`. Load the script
    without the stylesheet and the canvas keeps its raw size -- twice the
    container on a retina screen -- flows in normal document flow, and paints
    itself across the page behind everything else. The Efficiency page shipped
    exactly that way: the script tag was there, the link was not, so the chart
    rendered and every gate passed while the plot sat on top of the document.
    """
    offenders: list[str] = []
    for page in (Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web").glob(
        "*.html"
    ):
        html = page.read_text()
        draws = "paint(" in html or "uPlot.iife" in html
        if draws and "uPlot.min.css" not in html:
            offenders.append(page.name)
    assert not offenders, f"pages drawing uPlot without its stylesheet: {offenders}"
