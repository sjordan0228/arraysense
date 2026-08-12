"""The partial-period note names which incompleteness it is talking about.

``summary.partial`` carries two quite different facts: hours inside a day that
could not be modelled, and whole days that produced no score at all. The note
described only the first, so a week totalled over four days told the owner its
performance ratio came from a fraction of a day while its specific yield was
understated by nearly half. ``effPartialReason`` in efficiency.html decides
which sentence applies; these hold it to that.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
EFFICIENCY = (
    Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "efficiency.html"
)

_START = "// >>> eff-partial-reason"
_END = "// <<< eff-partial-reason"


def _slice() -> str:
    text = EFFICIENCY.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "eff-partial-reason markers are out of order in efficiency.html"
    return text[start:end]


def _call(summary: dict[str, object] | None) -> str | None:
    """Call effPartialReason with this summary and return what it says."""
    assert NODE is not None
    body = f"{_slice()}\nconsole.log(JSON.stringify(effPartialReason({json.dumps(summary)})));"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    out: str | None = json.loads(result.stdout.strip())
    return out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_complete_period_says_nothing() -> None:
    assert _call({"partial": False, "days_scored": 7, "days_expected": 7}) is None
    assert _call(None) is None


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_missing_days_are_counted_in_the_note() -> None:
    """The count is the whole point: four sevenths of a week is not a week."""
    reason = _call({"partial": True, "days_scored": 4, "days_expected": 7})
    assert reason is not None
    assert "4 of the 7 days" in reason


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_complete_set_of_days_falls_back_to_the_hours_wording() -> None:
    """Every day scored and still partial means hours inside them went unmodelled."""
    reason = _call({"partial": True, "days_scored": 7, "days_expected": 7})
    assert reason is not None
    assert "daylight hours" in reason


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_silent_string_is_named_rather_than_blamed_on_the_hours() -> None:
    """A day every hour of which was modelled can still be missing a third of the array."""
    reason = _call(
        {
            "partial": True,
            "days_scored": 1,
            "days_expected": 1,
            "strings_scored": 2,
            "strings_described": 3,
        }
    )
    assert reason is not None
    assert "1 of the 3 strings" in reason


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_single_partial_day_is_not_described_as_missing_days() -> None:
    """One day scored of one owed cannot be a missing-days problem."""
    reason = _call({"partial": True, "days_scored": 1, "days_expected": 1})
    assert reason is not None
    assert "daylight hours" in reason


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_older_payload_without_the_counts_still_gets_a_note() -> None:
    """A page held in a browser tab across an upgrade must not print undefined."""
    reason = _call({"partial": True})
    assert reason is not None
    assert "undefined" not in reason
