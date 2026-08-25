"""test_costs_circuit_panel_js.py — what the circuit list is allowed to claim.

The reference account has thirty-nine channels against a whole-home load and the
remainder is real: unmonitored branches, and two outlets offline since April and
August. Naming five circuits over a month's bill invites the reader to believe
those five are the bill, so the sentence beside the list is not decoration — it
is the thing that keeps the panel honest, and these hold it to saying only what
was measured.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

NODE = shutil.which("node")
COSTS = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "costs.html"

_START = "// >>> costs-coverage-words"
_END = "// <<< costs-coverage-words"

_PRELUDE = """
const DASH = '\\u2014';
const pctStr = (v) => v === null || v === undefined || !isFinite(v)
  ? DASH : `${(v * 100).toFixed(0)}%`;
"""


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-coverage-words markers are out of order in costs.html"
    return text[start:end]


def _words(coverage: dict[str, Any] | None) -> str:
    assert NODE is not None
    body = f"{_PRELUDE}\n{_slice()}\nconsole.log(coverageWords({json.dumps(coverage)}));"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip()


_MATCHED = {
    "circuits_kwh": 310.0,
    "house_kwh": 500.0,
    "fraction": 0.62,
    "recorded_seconds": 2_600_000,
    "window_seconds": 2_678_400,
    "spans_match": True,
}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_it_states_a_share_of_energy_in_those_words() -> None:
    """ "of the month's energy", not "of the month" — a reader comparing this
    with the strip's minutes-watched line has to be able to see that the two
    answer different questions."""
    said = _words(_MATCHED)
    assert "62%" in said
    assert "energy" in said


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_withheld_fraction_is_not_rendered_as_full_coverage() -> None:
    """The failure this exists to prevent: no denominator read as complete."""
    said = _words({**_MATCHED, "fraction": None, "house_kwh": None})
    assert "%" not in said
    assert "100" not in said


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_share_above_one_reads_as_a_disagreement_not_a_full_bar() -> None:
    """A part cannot exceed the whole, so this is a fault saying so — a mains
    channel that escaped the exclusion, or a multiplier on the wrong circuit."""
    said = _words({**_MATCHED, "fraction": 1.18})
    assert "more" in said or "disagree" in said


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_mismatched_spans_say_so_rather_than_dividing_them_out() -> None:
    said = _words({**_MATCHED, "fraction": None, "spans_match": False})
    assert "%" not in said


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_coverage_at_all_says_nothing_rather_than_nothing_measured() -> None:
    assert _words(None) == ""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_remainder_is_not_all_called_unclamped() -> None:
    """Finding 7. A monitored circuit gone quiet is part of the remainder too
    -- the reference account has had two outlets offline since April and
    August -- and a sentence that names only "branches nobody has clamped"
    claims more than coverage, measured in energy, can actually tell apart."""
    said = _words(_MATCHED)
    assert "nobody has clamped" not in said
    assert "unrecorded" in said or "unmonitored" in said


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_share_is_attributed_to_every_monitored_circuit_not_the_five_shown() -> None:
    """The sentence renders beside a panel titled "The five circuits that cost
    the most", and the coverage figure it states is computed server-side over
    every non-mains circuit before top_spenders truncates to five. "These
    circuits account for X%" beside five rows credits the five with all
    thirty-nine's share on the reference installation; the wording has to name
    what is actually being measured."""
    said = _words(_MATCHED)
    assert "monitor" in said.lower()
    assert "these circuits" not in said.lower()


# --- renderSpenders ---------------------------------------------------------
#
# The row renderer, sliced through renderBands' opening line rather than its
# own markers because it calls coverageWords and both have to be in scope
# together. fade() is stubbed to a string a test can grep for rather than
# reproduced, since the point here is that the renderer *calls* the
# token-derived helper, not what that helper's own arithmetic does.

_SPEND_SLICE_TO = "function renderBands("

_SPEND_PRELUDE = (
    _PRELUDE
    + """
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const gnum = (v, d) => v.toLocaleString(undefined,
  { minimumFractionDigits: d, maximumFractionDigits: d });
const uStr = (v, d, unit) => (v === null ? DASH : gnum(v, d) + (unit ? ' ' + unit : ''));
function money(v, cur) {
  if (v === null || v === undefined || !isFinite(v)) return DASH;
  return String(cur || '$') + Math.abs(v).toFixed(2);
}
function fade(name, alpha) { return `FADE(${name},${alpha})`; }
const _els = {};
function $(id) {
  return _els[id] || (_els[id] = { hidden: false, textContent: '', innerHTML: '' });
}
"""
)


def _spend_slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_SPEND_SLICE_TO, start)
    assert start < end, "costs-coverage-words / renderSpenders markers are out of order"
    return text[start:end]


def _render(
    circuits: list[dict[str, Any]], coverage: dict[str, Any] | None = None
) -> dict[str, Any]:
    assert NODE is not None
    body = {"circuits": circuits, "coverage": coverage}
    script = (
        f"{_SPEND_PRELUDE}\n{_spend_slice()}\n"
        f"renderSpenders({json.dumps(body)}, '$');\n"
        f"console.log(JSON.stringify(_els));"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    out: dict[str, Any] = json.loads(result.stdout)
    return out


def _circuit(
    name: str,
    cost: float | None,
    kwh: float | None,
    bands: list[dict[str, Any]],
    partial: bool = False,
    rider: float | None = None,
    grid_kwh: float | None = None,
    grid_cost: float | None = None,
    grid_partial: bool = False,
) -> dict[str, Any]:
    return {
        "name": name,
        "kind": "circuit",
        "cost": cost,
        "kwh": kwh,
        "partial": partial,
        "rider": rider,
        "grid_kwh": grid_kwh,
        "grid_cost": grid_cost,
        "grid_partial": grid_partial,
        "bands": bands,
    }


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_panel_hides_when_no_circuit_reported() -> None:
    """A selected month before circuit collection began still returns every
    known circuit's metadata with a null cost apiece -- five dashed rows, not
    the empty list the length check alone catches, so at least one circuit
    has to have actually reported before the panel is worth showing."""
    circuits = [_circuit(f"C{i}", None, None, []) for i in range(5)]
    els = _render(circuits)
    assert els["spendSec"]["hidden"] is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_panel_shows_once_one_circuit_has_reported() -> None:
    circuits = [
        _circuit("Dryer", 4.5, 15.0, [{"band": "peak", "kwh": 15.0, "cost": 4.5, "partial": False}])
    ]
    els = _render(circuits)
    assert els["spendSec"]["hidden"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_zero_cost_partial_band_stays_in_the_bar() -> None:
    """A band a circuit reported at exactly $0 is a different claim from one it
    never reported in at all, and filtering out the zero-cost one used to strip
    the very segment a partial hatch was about — leaving the caption's "a
    hatched segment" claim with no hatch anywhere on the row to point at."""
    circuits = [
        _circuit(
            "Fridge",
            2.0,
            5.0,
            [
                {"band": "peak", "kwh": 0.0, "cost": 0.0, "partial": True},
                {"band": "off-peak", "kwh": 5.0, "cost": 2.0, "partial": False},
            ],
            partial=True,
            grid_kwh=4.0,
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert "splitbar none" not in html, (
        "the zero-cost band was dropped and the row read as unmeasured"
    )
    # The band split moved out of the bar's segments and into its tooltip, so
    # that is where this test's subject now lives — and the assertion moved
    # with it rather than being dropped. The old check here was
    # `class="part" in html`, a hatch on the *band* segment that priced the
    # zero-cost band; segments are the supply split now and carry no band, so
    # that check could no longer fail for this test's reason. Whether the hatch
    # is drawn at all is a row-level question with two tests of its own below.
    assert "peak $0.00 (part)" in html, (
        "the zero-cost band vanished from the tooltip the band split now lives in"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_wholly_missing_band_marks_the_row_total_not_just_the_bar() -> None:
    """Finding 2. A band this circuit reported in another in-effect band but
    not in at all arrives ``cost: null, partial: false`` and is filtered out
    of the bar entirely -- there is no segment to hatch, since cost is null
    rather than merely thin. Before this, the row carried c.partial=true from
    the backend with nothing on screen showing it: no hatch (nothing to
    hatch), and the caption's "a hatched segment" explanation pointed at
    nothing on this row. The total itself needs its own mark."""
    circuits = [
        _circuit(
            "Fridge",
            2.0,
            5.0,
            [
                {"band": "peak", "kwh": None, "cost": None, "partial": False},
                {"band": "off-peak", "kwh": 5.0, "cost": 2.0, "partial": False},
            ],
            partial=True,
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert 'class="cv part"' in html, "the row total carries no mark for the missing band"
    assert "did not report in" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_fully_known_row_is_not_marked_missing() -> None:
    """The negative case for the marker above: a circuit that reported in
    every in-effect band gets no dot on its total."""
    circuits = [
        _circuit(
            "Dryer",
            4.5,
            15.0,
            [{"band": "peak", "kwh": 15.0, "cost": 4.5, "partial": False}],
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert 'class="cv part"' not in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_nonzero_rider_is_named_in_the_totals_title() -> None:
    """Finding 5. The PCRF/SCRF rider rides on ``cost`` but on no band beside
    it, so the bands below a row would sum to less than the total shown --
    $4.50 of segments beside a $4.65 total -- with nothing on screen saying
    where the other $0.15 came from until the total explains itself."""
    circuits = [
        _circuit(
            "Dryer",
            4.65,
            15.0,
            [{"band": "peak", "kwh": 15.0, "cost": 4.5, "partial": False}],
            rider=0.15,
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert "rate rider" in html
    assert "$0.15" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_zero_rider_says_nothing_about_a_rider_at_all() -> None:
    """No adjustment configured reads back as rider 0.0 from spend.py -- a
    known, real zero -- and a row whose bill genuinely carries no rider must
    not be told it does."""
    circuits = [
        _circuit(
            "Dryer",
            4.5,
            15.0,
            [{"band": "peak", "kwh": 15.0, "cost": 4.5, "partial": False}],
            rider=0.0,
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert "rider" not in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_circuit_measured_at_zero_everywhere_is_not_hatched_like_an_unmeasured_one() -> None:
    """Every measured band costing nothing is "it used no energy" -- the same
    claim ``spend.py`` already distinguishes from "nobody heard from it" at
    the circuit level, extended to the bar drawn from the individual bands."""
    circuits = [
        _circuit(
            "Quiet lamp",
            0.0,
            0.0,
            [
                {"band": "peak", "kwh": 0.0, "cost": 0.0, "partial": False},
                {"band": "off-peak", "kwh": 0.0, "cost": 0.0, "partial": False},
            ],
            grid_kwh=0.0,
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    # The fixture must stay at zero on every band. Moved off it, the row has a
    # supply split to draw and the assertion below passes for a reason that has
    # nothing to do with what this test is named for.
    assert "splitbar none" not in html, (
        "a circuit that reported zero all month rendered as one nobody heard from"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_bar_fill_is_derived_from_the_grid_token_not_a_literal() -> None:
    """A palette or theme override updates --grid but leaves a hard-coded rgba
    on the old colour, bypassing the CVD-validated palette the token carries."""
    circuits = [
        _circuit(
            "Dryer",
            4.5,
            15.0,
            [{"band": "peak", "kwh": 15.0, "cost": 4.5, "partial": False}],
            grid_kwh=12.0,
        )
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert "FADE(--grid,0.85)" in html
    assert "176,72,110" not in html


# --- renderSplit -------------------------------------------------------------
#
# The house-wide Energy/Cost bars predate this branch, but they hard-code the
# same literal renderSpenders' bar used to. Fixing only the new function would
# leave the two nearly-identical renderers in this file disagreeing about
# where their colour comes from, so both are derived from the token together.

_SPLIT_SLICE_FROM = "function renderSplit("

_SPLIT_PRELUDE = """
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const pctStr = (v) => v === null || v === undefined || !isFinite(v)
  ? '\\u2014' : `${(v * 100).toFixed(0)}%`;
function fade(name, alpha) { return `FADE(${name},${alpha})`; }
const _els = {};
function $(id) {
  return _els[id] || (_els[id] = { hidden: false, textContent: '', innerHTML: '' });
}
"""


def _split_slice() -> str:
    text = COSTS.read_text()
    start = text.index(_SPLIT_SLICE_FROM)
    end = text.index(_START, start)
    assert start < end, "renderSplit / costs-coverage-words markers are out of order"
    return text[start:end]


def _render_split(rows: list[dict[str, Any]]) -> str:
    assert NODE is not None
    script = (
        f"{_SPLIT_PRELUDE}\n{_split_slice()}\n"
        f"renderSplit({{ rows: {json.dumps(rows)} }});\n"
        f"console.log(_els.bandSplit.innerHTML);"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return result.stdout


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_house_wide_split_bars_are_also_derived_from_the_grid_token() -> None:
    rows = [
        {"band": {"name": "Peak"}, "cost": 4.0, "importKwh": 10.0, "importShort": False},
        {"band": {"name": "Off-peak"}, "cost": 1.0, "importKwh": 5.0, "importShort": False},
    ]
    html = _render_split(rows)
    assert "FADE(--grid," in html
    assert "176,72,110" not in html


# --- costs-supply-bar --------------------------------------------------------

_BAR_PRELUDE = """
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
// fade() reads a CSS custom property off the live document, which node has
// none of. The tests care which token a segment was painted from, not what
// hex it resolved to, so the stub returns the token's own name.
const fade = (name, alpha) => `${name}@${alpha}`;
const money = (v, cur) => v === null || v === undefined ? '\\u2014' : `$${v.toFixed(2)}`;
"""


def _bar_slice() -> str:
    text = COSTS.read_text()
    start = text.index("// >>> costs-supply-bar")
    end = text.index("// <<< costs-supply-bar")
    assert start < end, "costs-supply-bar markers are out of order in costs.html"
    return text[start:end]


def _bar(circuit: dict[str, Any], blanket: bool) -> str:
    assert NODE is not None
    body = (
        f"{_BAR_PRELUDE}\n"
        "function drawBar(c, priced, blanket, cur, everyRowPartial) {\n"
        f"{_bar_slice()}\n"
        "  return bar;\n"
        "}\n"
        f"console.log(drawBar({json.dumps(circuit)}, "
        f"{json.dumps(circuit['bands'])}, {json.dumps(blanket)}, '$', false));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_supply_bar_splits_grid_and_self_in_proportion() -> None:
    """A circuit that is 80% grid draws two segments in 4:1 proportion, the
    first carrying --grid and the second --pv. Pinning the flex values keeps
    the test from passing if the proportion is inverted."""
    circuit = {"grid_kwh": 8.0, "kwh": 10.0, "bands": [{"band": "Flat", "cost": 2.0}]}
    html = _bar(circuit, False)
    assert 'style="flex:800.00;background:--grid@0.85"' in html
    assert 'style="flex:200.00;background:--pv@0.85"' in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unknown_grid_share_draws_no_supply_segments() -> None:
    """A null grid_kwh means the house split was unreadable, and the row falls
    back to the hatch-or-nothing treatment rather than drawing a guess."""
    circuit = {"grid_kwh": None, "kwh": 10.0, "bands": [{"band": "Flat", "cost": 2.0}]}
    html = _bar(circuit, False)
    assert 'class="splitbar none"' in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_zero_grid_share_draws_only_the_pv_segment() -> None:
    """A zero grid share is a real measurement — the circuit ran entirely on
    solar and battery — so it must not render as a sliver of grid."""
    circuit = {"grid_kwh": 0.0, "kwh": 10.0, "bands": [{"band": "Flat", "cost": 2.0}]}
    html = _bar(circuit, False)
    assert 'style="flex:1000.00;background:--pv@0.85"' in html
    assert "--grid" not in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_wholly_grid_circuit_draws_only_the_grid_segment() -> None:
    """When grid_kwh equals kwh the circuit ran entirely off the meter, so
    only the --grid segment appears."""
    circuit = {"grid_kwh": 10.0, "kwh": 10.0, "bands": [{"band": "Flat", "cost": 2.0}]}
    html = _bar(circuit, False)
    assert 'style="flex:1000.00;background:--grid@0.85"' in html
    assert "--pv" not in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_row_partial_hatches_none_of_them() -> None:
    """#31 one level up, and only a rendered panel could have caught it.

    A month whose peak window no circuit has entered yet gives every row
    ``partial: true`` from the missing band, while the one band anybody
    reported in is not thin and so is not partial. The band-level gate
    therefore stays shut and every bar in the panel is hatched — which marks
    nothing and strikes out every label, exactly what the gate exists to
    prevent. The gate has to measure rows, because rows are what it gates.

    The fixture must have NO partial band and EVERY row partial, or it cannot
    tell the row gate from the band gate.
    """
    circuits = [
        _circuit(
            name,
            1.0,
            5.0,
            [
                {"band": "peak", "kwh": None, "cost": None, "partial": False},
                {"band": "off-peak", "kwh": 5.0, "cost": 1.0, "partial": False},
            ],
            partial=True,
            grid_kwh=4.0,
        )
        for name in ("Dryer", "Fridge")
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert 'class="part"' not in html, (
        "every bar was hatched, so the hatch marked nothing and struck out every label"
    )
    assert "Every circuit above was only partly recorded" in els["spendCap"]["innerHTML"], (
        "the hatch was withheld and nothing said so in words"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_one_whole_row_keeps_the_hatch_meaningful() -> None:
    """The other side of the same gate. One unqualified row among partial ones
    means the hatch distinguishes something, so it is drawn."""
    circuits = [
        _circuit(
            "Dryer",
            1.0,
            5.0,
            [{"band": "off-peak", "kwh": 5.0, "cost": 1.0, "partial": False}],
            partial=True,
            grid_kwh=4.0,
        ),
        _circuit(
            "Fridge",
            1.0,
            5.0,
            [{"band": "off-peak", "kwh": 5.0, "cost": 1.0, "partial": False}],
            partial=False,
            grid_kwh=4.0,
        ),
    ]
    els = _render(circuits)
    html = els["spendList"]["innerHTML"]
    assert 'class="part"' in html, "the one partial row lost the mark that qualifies it"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_grid_cell_carries_exactly_one_title_and_no_stray_attributes() -> None:
    """Built by string concatenation, an appended clause can land outside the
    quoted attribute — and every word of it then parses as an attribute of its
    own. That shipped here once: 24 junk attributes on a single span, through
    four green gates and the whole suite, visible only in the rendered DOM.

    All four states of the cell are exercised, because the clause that escaped
    was on one branch of three and the other two looked fine. Asserted on the
    raw markup, not through a parser — a parser is exactly what absorbs the
    damage into attributes nobody thinks to look at.
    """
    cases = [
        (0.24, 2.4, True),  # priced, and drawn from a short house window
        (0.24, 2.4, False),  # priced, whole
        (None, 2.4, False),  # energy known, rider unpublished, so no price
        (None, None, False),  # the house split itself was unreadable
    ]
    for grid_cost, grid_kwh, grid_partial in cases:
        circuits = [
            _circuit(
                "Dryer",
                0.6,
                3.0,
                [{"band": "peak", "kwh": 3.0, "cost": 0.6, "partial": False}],
                grid_kwh=grid_kwh,
                grid_cost=grid_cost,
                grid_partial=grid_partial,
            )
        ]
        html = _render(circuits)["spendList"]["innerHTML"]
        cell = html.split('<span class="ck"')[0].split('<span class="cv')[-1]
        # The tag must CLOSE straight after the title. Counting `="` cannot
        # catch this: in the raw string an escaped clause carries no `=` at
        # all — the junk attributes only appear once a browser parses it, and
        # a first version of this assertion passed with the bug reintroduced.
        # Matching the whole opening tag is what makes it falsifiable.
        opening = cell[: cell.index(">") + 1]
        assert re.fullmatch(r'( part)?" title="[^"]*">', opening), (
            f"something escaped the title attribute and will parse as markup: {opening!r}"
        )
