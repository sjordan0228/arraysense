"""test_costs_bill_caveat_js.py — the one sentence saying whether the bill is guessed.

Sol's review named two failures in the same sentence. The first: a month whose
calendar has finished is not the same thing as a month the collector recorded
in full, and on an install where collection started mid-month the old wording
called a genuine projection "what it came to rather than a projection" — the
exact claim #23's completeness rule exists to forbid. The second: the 0.999
threshold borrowed from renderMonth's display tolerance made the claim false
again in the last forty-three minutes of a real month, while the service was
still extrapolating. billCaveat reads isProjected — a fact the service decides
from the span its own counters cover, never the calendar — so both are fixed by
reading the right variable rather than by tuning a threshold.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COSTS = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "costs.html"

_START = "// >>> costs-bill-caveat"
_END = "// <<< costs-bill-caveat"


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-bill-caveat markers are out of order in costs.html"
    return text[start:end]


def _caveat(is_projected: bool, season_changes: bool = False) -> str:
    assert NODE is not None
    js_bool = "true" if is_projected else "false"
    season = "true" if season_changes else "false"
    body = f"{_slice()}\nconsole.log(billCaveat({js_bool}, {season}));"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_genuine_projection_says_so() -> None:
    assert _caveat(True) == (
        "An estimate: it assumes the rest of the month uses each rate band at the rate this "
        "one has so far."
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_not_projected_says_the_month_came_to_this() -> None:
    assert _caveat(False) == (
        "The month is over, so this is what it came to rather than a projection."
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_season_change_warns_even_though_it_is_still_a_projection() -> None:
    warned = _caveat(True, season_changes=True)
    assert "changes season" in warned
    assert "roughest of" in warned


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_season_changing_is_moot_once_the_month_is_not_projected() -> None:
    # A finished month is priced from what actually happened in each band, so
    # the season-change caveat — about scaling one band's measured rate onto
    # another's remaining days — has nothing left to warn about.
    assert _caveat(False, season_changes=True) == _caveat(False, season_changes=False)
