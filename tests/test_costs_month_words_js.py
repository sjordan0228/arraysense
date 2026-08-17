"""test_costs_month_words_js.py — "so far" is a claim, and it has to be true.

Eleven strings on the Costs page assumed the month was the one in progress.
Once a reader can pick July, every one of them is a small lie: an estimated bill
for a month that is over is not an estimate, and "month to date" on a finished
month is the whole month. One helper decides all of them, because a page that
relabelled some and not others would mislead more than one that relabelled none.

#216's second review found the calendar alone is not enough to license those
words either: an energy bucket with an unbracketed edge, or a bill whose
internal dropped span was restored at a blended rate, is exactly as partial on
the last day of the month as on the first. ``monthWords`` therefore answers two
independent questions — is the calendar done, and was every input behind these
particular figures actually measured — and the invariant test below states that
as one property over the combinations rather than as four separate patches.
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


def _words(
    fraction: float | None, name: str = "July 2026", complete: bool | None = None
) -> dict[str, Any]:
    assert NODE is not None
    args = [json.dumps(fraction), json.dumps(name)]
    if complete is not None:
        args.append(json.dumps(complete))
    body = f"{_slice()}\nconsole.log(JSON.stringify(monthWords({', '.join(args)})));"
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
    # No third argument: the default (every input the caller has nothing more
    # specific to say about) is "complete", so an ordinary finished month with
    # nothing else known reads exactly as it always did.
    w = _words(1.0)
    assert w["running"] is False
    assert w["whole"] is True
    assert w["cost"] == "Cost"
    assert w["bill"] == "Bill"
    assert w["total"] == "Month total"
    assert w["energy"] == "Energy in July 2026"
    assert w["inMonth"] == "in July 2026"
    assert w["theMonth"] == "July 2026"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_threshold_matches_rendermonths_own() -> None:
    """0.999 governs `running` alone — the grammar question of which tense to
    write the month's name in ("This month" versus "August"), which renderMonth
    now reads directly rather than keeping a second copy of the same threshold
    for its own day-count text. It is deliberately *not* the boundary the
    completeness words (`cost`/`bill`/`total`) use; see the next test."""
    assert _words(0.9989)["running"] is True
    assert _words(0.999)["running"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unmeasurable_fraction_keeps_the_qualifiers() -> None:
    """Absent is not complete. A month whose elapsed fraction could not be read
    must not be relabelled as a finished bill."""
    assert _words(None)["running"] is True
    assert _words(None)["bill"] == "Estimated bill"
    assert _words(None)["whole"] is False


# --- The completeness invariant (#216) --------------------------------------
#
# "No label on this page may present a figure as complete unless every input
# behind that figure is complete." Stated once, over the combinations, rather
# than as one test per finding: a month can be calendar-over with its own data
# still incomplete (a bracket gap, a blended-rate correction), and it can be
# well inside the 0.999 grammar slop with nothing wrong with it at all. Only
# the conjunction of "the calendar has truly finished" (`fraction >= 1`, not
# the display tolerance) and "the caller's own data was complete" earns the
# finished words.


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize(
    "fraction,complete,expect_whole",
    [
        # The calendar is done and nothing else was reported short: whole.
        (1.0, True, True),
        # The calendar is done, but the caller's own data was not — a bracket
        # that never reached the meter, or a bill that had to assume a rate
        # for energy nobody measured. This is finding 2/3/4: `is_projected`
        # or `bucket.complete` says no, and the calendar ending is not
        # evidence against that.
        (1.0, False, False),
        # 99.95% of the month has elapsed — comfortably past `running`'s own
        # 0.999 slop, so the month already reads as "This month" rather than
        # by name — but the true boundary has not arrived and the figures
        # still exclude the last stretch of it. This is finding 5: the
        # display tolerance must not leak into a completeness claim.
        (0.9995, True, False),
        # A month whose elapsed share could not be read at all is never
        # whole, regardless of what the caller claims about its own data.
        (None, True, False),
    ],
)
def test_whole_requires_the_calendar_and_the_data_together(
    fraction: float | None, complete: bool, expect_whole: bool
) -> None:
    w = _words(fraction, complete=complete)
    assert w["whole"] is expect_whole
    assert w["cost"] == ("Cost" if expect_whole else "Cost so far")
    assert w["bill"] == ("Bill" if expect_whole else "Estimated bill")
    assert w["total"] == ("Month total" if expect_whole else "Month to date")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_completeness_argument_never_touches_the_calendar_words() -> None:
    """`complete=False` qualifies the money and energy totals, and nothing
    else — the month's *name* is a grammar question the calendar alone
    answers, not a claim about whether any figure is finished. A version of
    this fix that routed `complete` into `running` as well would silently
    start calling a finished August "this month" again the moment its bill
    had to be projected, which is a second, unasked-for regression riding in
    on the first fix."""
    whole_month = _words(1.0, name="August 2026", complete=True)
    partial_data = _words(1.0, name="August 2026", complete=False)
    for key in ("running", "energy", "inMonth", "theMonth"):
        assert whole_month[key] == partial_data[key], key
