"""test_costs_month_words_js.py — "so far" is a claim, and it has to be true.

Eleven strings on the Costs page assumed the month was the one in progress.
Once a reader can pick July, every one of them is a small lie: an estimated bill
for a month that is over is not an estimate, and "month to date" on a finished
month is the whole month. One helper decides all of them, because a page that
relabelled some and not others would mislead more than one that relabelled none.
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

_START = "// >>> costs-month-words"
_END = "// <<< costs-month-words"


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-month-words markers are out of order in costs.html"
    return text[start:end]


def _words(fraction: float | None, name: str = "July 2026") -> dict[str, Any]:
    assert NODE is not None
    body = (
        f"{_slice()}\n"
        f"console.log(JSON.stringify(monthWords({json.dumps(fraction)}, {json.dumps(name)})));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    out: dict[str, Any] = json.loads(result.stdout.strip())
    return out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_month_in_progress_keeps_every_qualifier() -> None:
    w = _words(0.55)
    assert w["running"] is True
    assert w["cost"] == "Cost so far"
    assert w["bill"] == "Estimated bill"
    assert w["total"] == "Month to date"
    assert w["energy"] == "Energy this month"
    assert w["inMonth"] == "this month"
    assert w["theMonth"] == "This month"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_finished_month_drops_them_and_names_itself() -> None:
    w = _words(1.0)
    assert w["running"] is False
    assert w["cost"] == "Cost"
    assert w["bill"] == "Bill"
    assert w["total"] == "Month total"
    assert w["energy"] == "Energy in July 2026"
    assert w["inMonth"] == "in July 2026"
    assert w["theMonth"] == "July 2026"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_threshold_matches_rendermonths_own() -> None:
    """0.999 — within three-quarters of an hour of a month's end, calling it
    unfinished is pedantry rather than honesty, and the two places that decide
    it must agree or the strip and the labels contradict each other."""
    assert _words(0.9989)["running"] is True
    assert _words(0.999)["running"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unmeasurable_fraction_keeps_the_qualifiers() -> None:
    """Absent is not complete. A month whose elapsed fraction could not be read
    must not be relabelled as a finished bill."""
    assert _words(None)["running"] is True
    assert _words(None)["bill"] == "Estimated bill"
