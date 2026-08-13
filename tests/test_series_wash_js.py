"""test_series_wash_js.py — the wash under a series is a look's to give, and zero by default.

Each chart series now stands on a gradient of its own colour, and how strong
that gradient is comes from the --series-wash token rather than from a number
in the chart code. Two things have to hold or the wash stops being a property
of the look. A token that is missing, empty or unreadable must mean *no wash*
— a chart that invented a fill because it could not read a token would be
drawing something the data never said — and the base stylesheet must go on
declaring zero, because Classic is the base look and its charts are what they
have always been.

washStrength lives between the series-wash markers in common.js and is run here
under node, the way the wizard and geocode slices are. The declarations are
checked as text, since a stylesheet cannot be executed.
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

_START = "// >>> series-wash"
_END = "// <<< series-wash"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "series-wash markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", _slice() + "\n" + body], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_declared_strength_is_taken_as_written() -> None:
    # getComputedStyle hands back the token's text with its whitespace intact,
    # so the padded form is the one that actually arrives.
    assert _run("console.log(`${washStrength('0.2')} ${washStrength('  0.14  ')}`);") == "0.2 0.14"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unreadable_strength_is_no_wash_rather_than_a_guess() -> None:
    # An empty string is what ink() answers with when the token is not declared
    # at all, which is every look that has not asked for a wash.
    out = _run(
        "for (const raw of ['', '   ', 'none', 'inherit', undefined, null])"
        " console.log(String(washStrength(raw)));"
    )
    assert out.split() == ["0", "0", "0", "0", "0", "0"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_strength_outside_the_range_is_clamped_and_not_passed_on() -> None:
    # The browser does not refuse an alpha outside the range, which is why this
    # has to. Chrome reads rgba(...,1.6) as fully opaque and rgba(...,-0.3) as
    # fully clear, both in silence, so a look that typed 2 where it meant 0.2
    # would lay a solid slab of the series colour over its own chart.
    assert _run("console.log(`${washStrength('1.6')} ${washStrength('-0.3')}`);") == "1 0"


def test_the_base_stylesheet_gives_classic_no_wash() -> None:
    # Classic is the absence of a look, and its charts are lines with the two
    # fills the pages ask for by name. The wash arrives with a look or not at
    # all, so the base declaration has to be zero.
    #
    # Searched inside the BASE_CSS template literal rather than the whole file:
    # INK_FALLBACK also spells the token ('--series-wash':'0'), so deleting the
    # real declaration while leaving the fallback behind would pass a whole-file
    # search and Classic would silently take whatever the look sheets declare.
    common = COMMON.read_text()
    base = common.split("const BASE_CSS = `", 1)[1].split("`;", 1)[0]
    declared = re.findall(r"--series-wash\s*:\s*([0-9.]+)", base)
    assert declared, "the base stylesheet no longer declares --series-wash"
    assert all(float(v) == 0 for v in declared), (
        f"the base stylesheet gives Classic a wash: {declared}"
    )


def test_the_glass_look_declares_its_wash_in_every_theme_it_answers_in() -> None:
    # Three declarations, exactly as --bloom needs: a number has no light-dark()
    # form, so a light value stated in only one of the two light selectors
    # leaves an explicit choice of light and the device's own preference
    # disagreeing about how strong the wash is.
    css = (WEB / "theme-glass.css").read_text()
    explicit = re.search(
        r':root\[data-theme="light"\]\s*\{[^}]*--series-wash\s*:\s*0?\.[0-9]+', css
    )
    device = re.search(
        r"@media \(prefers-color-scheme: light\)\s*\{\s*"
        r":root:not\(\[data-theme\]\)\s*\{[^}]*--series-wash\s*:\s*0?\.[0-9]+",
        css,
    )
    assert explicit is not None, "no light wash for an explicit choice of light"
    assert device is not None, "no light wash for a device that prefers light"
    # The third is the palette block the two above override, which is what the
    # dark theme is left holding. Counted rather than matched by selector: that
    # block names four selectors at once, and a test that pinned their spelling
    # would fail on a fifth being added rather than on the wash going missing.
    declared = re.findall(r"--series-wash\s*:\s*([0-9.]+)", css)
    assert len(declared) == 3, f"expected a dark wash and two light ones, found {declared}"
    assert all(float(v) > 0 for v in declared), (
        f"the glass look declares a wash of nothing somewhere: {declared}"
    )


def test_the_wash_is_not_handed_to_the_canvas_as_a_function_it_cannot_parse() -> None:
    # The trap that has already cost this project a chart: a canvas parses
    # neither light-dark() nor color-mix(), ignores what it cannot read, and
    # keeps whatever colour was stroked last. An unregistered custom property is
    # not resolved by getComputedStyle either, so a token holding either
    # function would arrive at fillStyle as text. The wash is a bare number and
    # the mixing happens in fade().
    css = (WEB / "theme-glass.css").read_text()
    for line in css.splitlines():
        if "--series-wash" in line and ":" in line.split("--series-wash", 1)[1]:
            assert "light-dark(" not in line and "color-mix(" not in line, (
                f"--series-wash is declared as something canvas cannot parse: {line.strip()}"
            )
