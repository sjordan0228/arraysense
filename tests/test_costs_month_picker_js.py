"""test_costs_month_picker_js.py — where the Costs page's month picker may go.

Two rules, both of which the Efficiency page's picker gets wrong and is not
copied on. Forward stops at the month in progress: the service answers a request
for next month rather than refusing it — partially, with its elapsed fraction
clamped — so a month that has not happened would arrive labelled exactly like an
ordinary part month. And the five-minute poll must not move the cursor, or a
reader looking at July is back on August five minutes after arriving, with
nothing on screen to say why.
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


def _call(expression: str) -> Any:
    assert NODE is not None
    body = f"{_PRELUDE}\n{_slice()}\nconsole.log(JSON.stringify({expression}));"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip())


_AUGUST = {"year": 2026, "month": 8}
_TODAY = {"year": 2026, "month": 8, "day": 17}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_stepping_back_from_january_lands_in_december() -> None:
    """Month numbers are not integers to subtract from. 1 - 1 = 0 is no month."""
    assert _call('shiftMonth({"year": 2026, "month": 1}, -1)') == {"year": 2025, "month": 12}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_stepping_forward_from_december_lands_in_january() -> None:
    assert _call('shiftMonth({"year": 2025, "month": 12}, 1)') == {"year": 2026, "month": 1}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_forward_is_refused_on_the_month_in_progress() -> None:
    assert _call(f"canGoForward({json.dumps(_AUGUST)}, {json.dumps(_TODAY)})") is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_forward_is_refused_on_a_month_already_ahead() -> None:
    """Belt and braces: if anything ever puts the cursor past today, the button
    does not carry it further."""
    ahead = {"year": 2026, "month": 9}
    assert _call(f"canGoForward({json.dumps(ahead)}, {json.dumps(_TODAY)})") is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_forward_is_allowed_from_a_finished_month() -> None:
    past = {"year": 2025, "month": 12}
    assert _call(f"canGoForward({json.dumps(past)}, {json.dumps(_TODAY)})") is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_poll_runs_on_the_month_in_progress() -> None:
    assert _call(f"shouldAutoRefresh({json.dumps(_AUGUST)}, {json.dumps(_TODAY)})") is True
    assert _call(f"shouldAutoRefresh(null, {json.dumps(_TODAY)})") is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_poll_leaves_a_finished_month_alone() -> None:
    """A month that is over cannot change, and re-reading it is a month of
    counter history read for nothing."""
    past = {"year": 2026, "month": 7}
    assert _call(f"shouldAutoRefresh({json.dumps(past)}, {json.dumps(_TODAY)})") is False
