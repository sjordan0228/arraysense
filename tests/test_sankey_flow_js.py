"""test_sankey_flow_js.py — the two Sankeys say the same things in the same way.

Both diagrams now come from one renderer in common.js, and these hold the parts
of it that would be wrong in silence. A share that divided by something other
than the total the picture is scaled by would put a plausible percentage in a
tooltip nobody could check. A path with no reading behind it that still flowed
would claim a measurement. And the hue the losses ribbon must never take is the
one thing a colour-blind reader cannot catch by looking.

The arithmetic lives between the sankey-flow markers and runs here under node,
the way the wizard, geocode and series-wash slices do. The rest is checked as
text, since a stylesheet and a page's markup cannot be executed.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"
COMMON = WEB / "common.js"

_START = "// >>> sankey-flow"
_END = "// <<< sankey-flow"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "sankey-flow markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", _slice() + "\n" + body], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_share_is_the_value_over_the_total_the_diagram_is_scaled_by() -> None:
    # The dashboard's own frame: 64.6 kWh of solar in 91.9 kWh through the
    # inverter. Anything but 0.702938 here means the tooltip and the picture
    # disagree about what the ribbon is a share of.
    assert _run("console.log(sankeyShare(64.6, 91.9).toFixed(6));") == "0.702938"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_share_of_nothing_is_not_a_share() -> None:
    # A diagram with no total has no percentages in it, and an unread value has
    # none either. Either one falling through to a number would put a figure in
    # the readout that no reading supports.
    out = _run(
        "for (const args of [[1, 0], [1, -3], [1, null], [null, 10], [undefined, 10],"
        " ['4', 10], [NaN, 10], [1, NaN]]) console.log(String(sankeyShare(...args)));"
    )
    assert out.split() == ["null"] * 8


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_sliver_that_is_present_is_never_written_as_none() -> None:
    # 0.03% of the total is a real quantity that happens to be small. Rounded to
    # a tenth it would print as 0.0%, which reads as "this path carried nothing"
    # — the same mistake as rendering an absent value as zero, one step further
    # down. A genuine zero still prints as zero.
    out = _run(
        "for (const s of [0.0003, 0, 0.0272, 0.099, 0.0999, 0.4502, 1])"
        " console.log(sankeyPercent(s));"
    )
    assert out.split() == ["<0.1%", "0.0%", "2.7%", "9.9%", "10%", "45%", "100%"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_path_with_nothing_on_it_does_not_flow() -> None:
    # Motion is a claim that something is moving. A ribbon whose live power is
    # zero, or was never reported, must be still — the width still says what the
    # day did, and the readout still prints the rate as a dash or as a zero.
    out = _run(
        "for (const rate of [0, -50, null, undefined, NaN, '900'])"
        " console.log(String(sankeyMotion(rate, 5000)));"
    )
    assert out.split() == ["null"] * 6


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_nothing_flows_when_nothing_is_moving_anywhere() -> None:
    # No peak means no reading on any path in the frame, which is the state a
    # dashboard is in before its first response and during an outage. Pacing
    # every ribbon off a peak of zero would either divide by nothing or run them
    # all flat out.
    out = _run("console.log(String(sankeyMotion(500, 0)), String(sankeyMotion(500, null)));")
    assert out.split() == ["null", "null"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_more_power_is_a_faster_and_stronger_flow() -> None:
    # The whole encoding, in one assertion: the busiest path in the frame moves
    # fastest and shows most, and a path carrying a fraction of it moves slower.
    # Reversed, the diagram would say the opposite of what is happening.
    out = _run(
        "const a = sankeyMotion(15696, 15696), b = sankeyMotion(5536, 15696);"
        "console.log([a.seconds < b.seconds, a.opacity > b.opacity, b.opacity > 0].join(' '));"
    )
    assert out.split() == ["true", "true", "true"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_rate_above_the_peak_cannot_outrun_the_frame() -> None:
    # The peak is the largest rate the renderer found, so nothing should exceed
    # it — but a reading arriving between the two would otherwise produce a
    # duration below the floor, and a negative one animates backwards.
    out = _run(
        "const top = sankeyMotion(100, 100), over = sankeyMotion(400, 100);"
        "console.log([over.seconds === top.seconds, over.opacity === top.opacity].join(' '));"
    )
    assert out.split() == ["true", "true"]


def test_the_dash_and_the_offset_that_scrolls_it_are_one_number() -> None:
    # A dash pattern and the offset that advances it by exactly one cycle are
    # the same figure written twice. Written twice they drift by a unit and the
    # flow stutters once per cycle, which is the kind of fault that survives
    # every test that is not this one.
    common = COMMON.read_text()
    assert re.search(r"stroke-dasharray:\$\{SANKEY_DASH\} \$\{SANKEY_GAP\}", common), (
        "the dash pattern no longer comes from the constants"
    )
    assert re.search(r"stroke-dashoffset:-\$\{SANKEY_DASH \+ SANKEY_GAP\}", common), (
        "the keyframe no longer scrolls exactly one cycle"
    )


def test_reduced_motion_stops_the_flow_in_both_looks() -> None:
    # The base sheet is what Classic gets, so the opt-out has to live there. And
    # the glass sheet loads after it: a transition declared there and not taken
    # back would win the cascade and move anyway.
    common = COMMON.read_text()
    reduced = re.search(r"@media\(prefers-reduced-motion:reduce\)\{(.*?)\n  \}", common, re.S)
    assert reduced is not None, "the base stylesheet no longer opts out of the flow"
    assert ".rflow{display:none}" in reduced.group(1).replace(" ", ""), (
        f"the flow still runs under reduced motion: {reduced.group(1)}"
    )
    glass = (WEB / "theme-glass.css").read_text()
    block = glass.split("@media (prefers-reduced-motion: reduce) {")[1].split("\n}")[0]
    assert ".sank .rib { transition: none; }" in block, (
        "the glass look re-enables the ribbon transition it added, under reduced motion"
    )


def test_the_flow_is_declared_where_classic_can_see_it() -> None:
    # Everything the diagram uses to say what it says belongs to both looks. The
    # glass sheet may add atmosphere on top; it must not be where the picture
    # comes from, or Classic loses the encoding rather than the flourish.
    common = COMMON.read_text()
    for rule in (".sank.dim .rib", ".sank .rflow", "@keyframes sankflow"):
        assert rule in common, f"{rule} is not in the base stylesheet"


def test_gradient_stop_colours_arrive_through_a_style_never_an_attribute() -> None:
    # The gradient stops are written with style="stop-color:…" rather than as a
    # stop-color attribute, colour and opacity in one place. That is a style
    # choice, not a compatibility constraint — var() resolves in either spelling
    # — so the test pins the spelling and says nothing about what an attribute
    # can express. The node rects are a separate question: they are not stops
    # and are not covered here.
    common = COMMON.read_text()
    assert 'stop-color="' not in common, "a gradient stop is written as an attribute"
    assert 'style="stop-color:' in common, "the gradient stops no longer carry a style"
    for page in ("index.html", "costs.html"):
        text = (WEB / page).read_text()
        assert 'stop-color="' not in text, f"{page} writes a stop colour as an attribute"


def test_the_losses_ribbon_is_marked_by_texture_and_not_by_a_hue() -> None:
    # Losses are the one quantity on the dashboard's diagram that nobody meters,
    # and the reader who has to be able to tell it apart is colour blind. It
    # takes the junction's own neutral plus the hatch this site already spends
    # on "not measured", and no colour of its own.
    index = (WEB / "index.html").read_text()
    losses = re.search(r"\{ name: 'Losses'.*?\}", index, re.S)
    assert losses is not None, "the Losses node is no longer declared as one object"
    assert "hatch: true" in losses.group(0), "Losses is no longer hatched"
    assert "var(--ink3)" in losses.group(0), "Losses took a colour of its own"
    for hue in ("--pv", "--load", "--batt", "--grid", "--batt-dis", "--warn", "--bad"):
        assert hue not in losses.group(0), f"Losses was given {hue}"
    common = COMMON.read_text()
    hatch = re.search(r"const SANKEY_HATCH =(.*?);\n", common, re.S)
    assert hatch is not None and "var(--ink)" in hatch.group(1), (
        "the hatch no longer uses the page's own foreground ink"
    )


def test_losses_carries_no_live_rate_it_could_not_have_measured() -> None:
    # The two sides of the diagram come off the inverter's own counters, and the
    # loss is what they do not account for. There is no register for it, so it
    # gets no pace — inventing one from four power readings taken moments apart
    # would put a derived figure in the one diagram whose claim is that it is
    # not derived.
    index = (WEB / "index.html").read_text()
    losses = re.search(r"\{ name: 'Losses'.*?\}", index, re.S)
    assert losses is not None
    assert "rate: null" in losses.group(0), "Losses was given a live rate"


def test_both_pages_draw_their_ribbons_through_the_one_renderer() -> None:
    # Two hand-written copies of the same bezier pair is how the Costs page and
    # the dashboard would come to disagree about the diagram they both claim is
    # the same picture. The geometry stays on the pages; the ribbon does not.
    for page in ("index.html", "costs.html"):
        text = (WEB / page).read_text()
        assert "sankeyRender(" in text, f"{page} no longer draws through the shared renderer"
        assert 'fill-opacity="0.34"' not in text, f"{page} still paints a ribbon of its own"
        assert "const ribbon = (" not in text, f"{page} kept its own ribbon builder"


def test_a_ribbon_is_reachable_by_keyboard_and_says_its_whole_figure() -> None:
    # Hover is not an interface everybody has. Each ribbon is focusable and
    # carries the same sentence the readout shows, and the diagram is a group
    # rather than an image so those children are not pruned out of the
    # accessibility tree by the role on the <svg>.
    common = COMMON.read_text()
    assert 'tabindex="0"' in common and 'aria-label="${esc(parts.label)}"' in common, (
        "a ribbon is no longer focusable, or no longer names itself"
    )
    for page in ("index.html", "costs.html"):
        text = (WEB / page).read_text()
        assert re.search(r'<svg id="sankey"[^>]*role="group"', text), (
            f"{page}'s diagram is not a group, so its ribbons are hidden from a reader"
        )
