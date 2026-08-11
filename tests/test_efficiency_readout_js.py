"""The hourly chart's hover readout now includes a raw ratio row.

That row is backed by effRawRatio in efficiency.html, which returns the ratio
of actual to expected or null when it cannot be stated. A computed percentage
must never be 0%, 100%, Infinity or NaN for the same reason a missing reading
is a dash and never a zero. These tests hold it to those rules.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
EFFICIENCY = (
    Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "efficiency.html"
)

_START = "// >>> eff-raw-ratio"
_END = "// <<< eff-raw-ratio"


def _slice() -> str:
    text = EFFICIENCY.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "eff-raw-ratio markers are out of order in efficiency.html"
    return text[start:end]


def _call(expected: float | None, actual: float | None) -> str:
    """Call effRawRatio and return the result as a JSON string."""
    assert NODE is not None
    body = (
        f"{_slice()}\nconsole.log(JSON.stringify(effRawRatio({_json(expected)}, {_json(actual)})));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _json(v: float | None) -> str:
    if v is None:
        return "null"
    return str(v)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_normal_point_returns_the_ratio() -> None:
    """A point where both values are present and expected > 0."""
    out = _call(10.0, 8.8)
    assert float(out) == pytest.approx(0.88)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_zero_expected_returns_null() -> None:
    """A zero denominator is not a ratioble quantity — show a dash."""
    out = _call(0.0, 0.0)
    assert out == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_absent_expected_returns_null() -> None:
    """No expected value means no percentage to state."""
    out = _call(None, 5.0)
    assert out == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_absent_actual_returns_null() -> None:
    """No actual value means no percentage to state."""
    out = _call(10.0, None)
    assert out == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_both_absent_returns_null() -> None:
    """Two missing readings are not a ratio of 100%."""
    out = _call(None, None)
    assert out == "null"


# The hours either side of sunrise and sunset are the ones that actually reach
# this function every day. compute_hours keeps every hour whose solar elevation
# is above zero, and the payload rounds expected to four decimals, so a
# denominator of 0.0001 kWh is an ordinary shipped value rather than a
# contrived one. A ratio taken there is arithmetic on rounding noise.


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_denominator_that_rounds_to_no_production_returns_null() -> None:
    """The rows above state kWh to two places. If expected shows as 0.00 there
    is no ratio the reader could check, and 100% printed under two zeros is the
    confident wrong number this row exists to avoid."""
    assert _call(0.004, 0.004) == "null"
    assert _call(0.0001, 0.15) == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_production_the_rows_can_show_still_states_its_ratio() -> None:
    """The floor is about unreadable denominators, not surprising ones. A dawn
    hour that genuinely beat its model is a real reading and must survive."""
    assert float(_call(0.02, 0.2)) == pytest.approx(10.0)
    assert float(_call(0.005, 0.005)) == pytest.approx(1.0)


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_negative_actual_returns_null() -> None:
    """A negative measurement is not a share of what was expected."""
    assert _call(10.0, -0.8) == "null"
