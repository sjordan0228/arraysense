"""test_costs_month_window_js.py — the window the Costs page prices.

``monthWindow`` used to build its own end from ``Date.now()``, which is right
for the month in progress and wrong for every other one: a finished month asked
about that way is a span from its first to this instant, so September priced as
five weeks. It now takes the month to price, and these hold it to that.

The helpers it leans on are restated in the prelude rather than sliced out of
common.js, the way test_efficiency_waterfall_js.py does it: importing the whole
file would drag the stylesheet and the boot sequence in with it.
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

_START = "// >>> costs-month-window"
_END = "// <<< costs-month-window"

_PRELUDE = """
const civilDay = (year, month, day) => new Date(year, month - 1, day);
const pad2 = (n) => String(n).padStart(2, '0');
const naiveStamp = (d) =>
  `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T00:00:00`;
const elapsed = (a, b) => b.getTime() - a.getTime();
"""


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-month-window markers are out of order in costs.html"
    return text[start:end]


def _window(month: dict[str, int], today: dict[str, int]) -> dict[str, Any]:
    """Call monthWindow with this month and this civil clock."""
    assert NODE is not None
    body = (
        f"{_PRELUDE}\n{_slice()}\n"
        f"const w = monthWindow({json.dumps(month)}, {json.dumps(today)});\n"
        "console.log(JSON.stringify({days: w.days, done: w.done, current: w.current,"
        " startParam: w.startParam, endParam: w.endParam}));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    out: dict[str, Any] = json.loads(result.stdout.strip())
    return out


_MID_AUGUST = {"year": 2026, "month": 8, "day": 17, "hour": 12, "minute": 0, "second": 0}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_finished_month_ends_at_its_own_end() -> None:
    """Not at this instant. That difference priced September as five weeks."""
    w = _window({"year": 2026, "month": 7}, _MID_AUGUST)
    assert w["current"] is False
    assert w["startParam"] == "2026-07-01T00:00:00"
    assert w["endParam"] == "2026-08-01T00:00:00"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_finished_month_is_wholly_elapsed() -> None:
    """31 of 31 days, so renderMonth calls it a bill rather than a part month."""
    w = _window({"year": 2026, "month": 7}, _MID_AUGUST)
    assert w["days"] == 31
    assert w["done"] == pytest.approx(31.0)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_month_in_progress_still_ends_now() -> None:
    """The end has to stay a true instant: it is also what tells the service
    how much of the month has actually happened."""
    w = _window({"year": 2026, "month": 8}, _MID_AUGUST)
    assert w["current"] is True
    assert w["startParam"] == "2026-08-01T00:00:00"
    assert w["endParam"].endswith("Z")
    assert w["done"] == pytest.approx(16.5)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_february_is_measured_not_assumed() -> None:
    """A month's length is the calendar's answer, never 30 and never 31."""
    assert _window({"year": 2026, "month": 2}, _MID_AUGUST)["days"] == 28
    assert _window({"year": 2024, "month": 2}, _MID_AUGUST)["days"] == 29


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_month_ahead_of_the_clock_has_elapsed_nothing() -> None:
    """The picker refuses to go there; this is what it looks like if it ever
    does, and it is not a full month rendered as a finished bill."""
    w = _window({"year": 2026, "month": 9}, _MID_AUGUST)
    assert w["current"] is False
    assert w["done"] == 0
