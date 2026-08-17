"""test_costs_money_freshness_js.py — no month's money answers for another's heading.

renderMonth and renderEnergy repaint the instant their own fetch lands, but the
cards, the Sankey and the band table wait on a second, slower request. Between
those two points a reader who has just paged to a new month used to see the
*previous* month's figures sitting under the new heading — and if the second
request then failed, the mismatch never went away, because the old error path
deliberately left whatever was on screen alone. moneyIsStale is the one
comparison refresh() now makes before either fetch starts, to hide the old
figures on a genuine navigation while leaving them up, as before, through an
ordinary same-month retry.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COSTS = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "costs.html"

_START = "// >>> costs-money-freshness"
_END = "// <<< costs-money-freshness"


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-money-freshness markers are out of order in costs.html"
    return text[start:end]


def _call(month_key: str, seen: str | None) -> bool:
    assert NODE is not None
    seen_js = "null" if seen is None else f'"{seen}"'
    body = f'{_slice()}\nconsole.log(moneyIsStale("{month_key}", {seen_js}));'
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip() == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_first_ever_render_is_treated_as_stale() -> None:
    """moneyMonth starts null, and the markup already starts every money
    section hidden — so this only has to agree, not un-hide anything."""
    assert _call("2026-8", None) is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_same_month_refresh_is_not_stale() -> None:
    """The ordinary five-minute poll of the month already on screen must not
    hide figures that are still correct, even if the poll itself then fails."""
    assert _call("2026-8", "2026-8") is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_paging_to_a_different_month_is_stale() -> None:
    assert _call("2026-7", "2026-8") is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_paging_across_a_year_boundary_is_stale() -> None:
    """The month key carries the year, so December's "12" cannot be mistaken
    for the January that follows it."""
    assert _call("2027-1", "2026-12") is True
