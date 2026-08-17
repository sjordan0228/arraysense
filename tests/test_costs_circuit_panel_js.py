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
