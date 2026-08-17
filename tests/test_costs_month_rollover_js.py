"""test_costs_month_rollover_js.py — one last refresh for the month that just ended.

Left open across midnight on the first, the page's own clock advances while
cursor still names the month that was current a moment before. shouldAutoRefresh
alone then reads false for ever, because cursor and today have parted ways and
nothing moves cursor back — so that month never picks up its last few hours of
counter history, and the forward arrow, which only refresh()'s own call to
renderPicker re-evaluates, never opens onto the month that just began.
monthRolledOver is what lets the five-minute timer through exactly once when
that happens, by comparing the month it saw on the previous tick against the
month the clock reads now.
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

_START = "// >>> costs-month-cursor"
_END = "// <<< costs-month-cursor"

_PRELUDE = "const civilDay = (year, month, day) => new Date(year, month - 1, day);\n"


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-month-cursor markers are out of order in costs.html"
    return text[start:end]


def _call(today: dict[str, Any], seen: dict[str, Any] | None) -> bool:
    assert NODE is not None
    body = (
        f"{_PRELUDE}\n{_slice()}\n"
        f"console.log(monthRolledOver({json.dumps(today)}, {json.dumps(seen)}));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip() == "true"


_AUGUST = {"year": 2026, "month": 8}
_SEPTEMBER = {"year": 2026, "month": 9}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_first_tick_has_no_baseline_and_is_not_a_rollover() -> None:
    """seen is null before the timer has ever run once; that must not itself
    be read as a rollover and force an extra refresh at startup."""
    assert _call(_AUGUST, None) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_same_month_as_last_tick_is_not_a_rollover() -> None:
    assert _call(_AUGUST, _AUGUST) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_month_that_advanced_since_the_last_tick_is_a_rollover() -> None:
    assert _call(_SEPTEMBER, _AUGUST) is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_rollover_across_the_turn_of_the_year_is_caught_too() -> None:
    """The month number alone repeats every year, so the comparison has to
    carry the year — December to January is a rollover though both are
    nobody's idea of "month 12 again"."""
    assert _call({"year": 2027, "month": 1}, {"year": 2026, "month": 12}) is True
