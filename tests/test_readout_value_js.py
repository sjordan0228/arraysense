"""The readout plugin resolves a row's value, and six charts depend on it.

A row's second element used to be a series index and nothing else. It may now
also be a function computing a derived value, which is what lets the efficiency
chart state a ratio no single series holds. That dispatch is the part of the
change that can break pages nobody was editing, so it is tested directly rather
than through any one chart.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"

_START = "// >>> readout-value"
_END = "// <<< readout-value"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "readout-value markers are out of order in common.js"
    numornull = next(line for line in text.splitlines() if line.startswith("const numOrNull"))
    return f"{numornull}\n{text[start:end]}"


def _eval(expression: str) -> str:
    """Run an expression against the real readoutValue and describe the result.

    Deliberately not JSON: JSON.stringify renders NaN and Infinity as the
    literal null, which would make every non-finite case here agree with the
    dash it is supposed to be distinguished from and pass whatever the code did.
    The type is carried too, so a string that looks like a number cannot be
    mistaken for one.
    """
    assert NODE is not None
    describe = f"(v => v === null ? 'null' : typeof v + ':' + String(v))({expression})"
    body = f"{_slice()}\nconsole.log({describe});"
    result = subprocess.run([NODE, "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip()


# A stand-in for the uPlot instance: three series, one absent reading.
_U = "{data:[[1700000000,1700003600],[1.5,null],[9.0,4.0]]}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_series_index_reads_that_series() -> None:
    """The path every pre-existing row takes, unchanged."""
    assert _eval(f"readoutValue({_U},0,1)") == "number:1.5"
    assert _eval(f"readoutValue({_U},0,2)") == "number:9"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_series_zero_is_a_series_and_not_a_falsy_nothing() -> None:
    """Series 0 is the x axis. Dispatching on truthiness rather than on type
    would call it as a function and throw on the first hover of every chart on
    every page — the regression this test exists to catch."""
    assert _eval(f"readoutValue({_U},0,0)") == "number:1700000000"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_absent_series_reading_stays_absent() -> None:
    assert _eval(f"readoutValue({_U},1,1)") == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_function_row_is_called_with_the_chart_and_the_index() -> None:
    """It receives (u, idx), which is what lets it reach across series."""
    derived = "(u,i)=>u.data[2][i]/u.data[1][i]"
    assert _eval(f"readoutValue({_U},0,{derived})") == "number:6"


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize(
    "returns",
    ["null", "undefined", "NaN", "1/0", "-1/0", "'93'"],
    ids=["null", "undefined", "nan", "infinity", "-infinity", "string"],
)
def test_anything_that_is_not_a_finite_number_becomes_the_dash(returns: str) -> None:
    """Reaching the formatter with any of these is how a gap starts looking
    like a reading."""
    assert _eval(f"readoutValue({_U},0,()=>{returns})") == "null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_row_function_that_throws_does_not_escape_into_the_cursor_handler() -> None:
    """It would strand the tooltip mid-update: stale text, stale position, and
    the rest of uPlot's hook chain skipped for that chart."""
    assert _eval(f"readoutValue({_U},0,()=>{{throw new Error('x')}})") == "null"
