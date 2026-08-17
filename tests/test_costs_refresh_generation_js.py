"""test_costs_refresh_generation_js.py — a stale refresh must not win the page.

refresh() awaits two network reads, and nothing stops the month picker's
buttons or the five-minute timer from starting a second refresh() while the
first is still in flight. Whichever call's fetches resolve last would win the
page — not whichever the reader asked for last — unless every checkpoint in
refresh() can tell its own ticket apart from a newer one. isStale is that one
comparison, pulled out by name so it cannot drift between the three places
refresh() calls it.

refresh() itself is not tested here: it calls fetch() and writes the DOM, so
proving its three checkpoints are wired to the right places is a job for
reading the code, not this harness. What this harness can and does prove is
the invariant every one of those checkpoints leans on — that a ticket reads
as current only for as long as nothing newer has been issued.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COSTS = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "costs.html"

_START = "// >>> costs-refresh-generation"
_END = "// <<< costs-refresh-generation"


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-refresh-generation markers are out of order in costs.html"
    return text[start:end]


def _run(script: str) -> str:
    assert NODE is not None
    body = f"{_slice()}\n{script}"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_first_call_holds_the_only_ticket_issued() -> None:
    assert _run("const gen = ++refreshGen; console.log(isStale(gen));") == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_ticket_overtaken_by_a_later_refresh_reads_stale() -> None:
    """The shape of the real bug: refresh() #1 takes ticket 1, then refresh()
    #2 starts — a click, or the five-minute timer — and takes ticket 2 before
    #1's own await resolves. #1 has to recognise it has been overtaken."""
    script = "const first = ++refreshGen; ++refreshGen; console.log(isStale(first));"
    assert _run(script) == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_later_call_that_overtook_it_is_not_itself_stale() -> None:
    """Overtaking is not mutual: the newer call is exactly the one allowed to
    paint, however many older ones it left behind."""
    script = "++refreshGen; const second = ++refreshGen; console.log(isStale(second));"
    assert _run(script) == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_three_overlapping_calls_leave_only_the_last_current() -> None:
    """The three-checkpoint case in miniature: a third refresh — paging
    through several months quickly — overtakes a second that had already
    overtaken a first. Only the last ticket issued is current; both earlier
    ones, including the one that briefly looked current, read stale."""
    script = (
        "const a = ++refreshGen; const b = ++refreshGen; const c = ++refreshGen; "
        "console.log(JSON.stringify([isStale(a), isStale(b), isStale(c)]));"
    )
    assert _run(script) == "[true,true,false]"
