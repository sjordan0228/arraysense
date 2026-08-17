"""test_costs_month_rollover_js.py — one last refresh for the month that just ended.

Left open across midnight on the first, the page's own clock advances while
cursor still names the month that was current a moment before. shouldAutoRefresh
alone then reads false for ever, because cursor and today have parted ways and
nothing moves cursor back — so that month never picks up its last few hours of
counter history, and the forward arrow, which only refresh()'s own call to
renderPicker re-evaluates, never opens onto the month that just began.
monthRolledOver is what lets the five-minute timer through exactly once when
that happens.

Comparing only the previous tick's month against the current one is not
enough: that mismatch is true for every reader the instant the calendar turns
over, including one who navigated away to a past month on purpose — the very
"do not poll a deliberately selected past month" rule this mechanism was
written not to break. monthRolledOver also has to read cursor, and fire the
bonus refresh only when cursor names the month that just became historical.
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


def _call(
    cursor: dict[str, Any] | None, today: dict[str, Any], seen: dict[str, Any] | None
) -> bool:
    assert NODE is not None
    body = (
        f"{_PRELUDE}\n{_slice()}\n"
        f"console.log(monthRolledOver({json.dumps(cursor)}, {json.dumps(today)}, "
        f"{json.dumps(seen)}));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return result.stdout.strip() == "true"


_JULY = {"year": 2026, "month": 7}
_AUGUST = {"year": 2026, "month": 8}
_SEPTEMBER = {"year": 2026, "month": 9}


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_first_tick_has_no_baseline_and_is_not_a_rollover() -> None:
    """seen is null before the timer has ever run once; that must not itself
    be read as a rollover and force an extra refresh at startup."""
    assert _call(_AUGUST, _AUGUST, None) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_same_month_as_last_tick_is_not_a_rollover() -> None:
    assert _call(_AUGUST, _AUGUST, _AUGUST) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_month_that_advanced_since_the_last_tick_is_a_rollover_for_its_own_reader() -> None:
    # cursor still names August — the month that just became historical — which
    # is the ordinary case: nobody moves cursor forward automatically, so a
    # reader who had the current month open keeps naming it after midnight.
    assert _call(_AUGUST, _SEPTEMBER, _AUGUST) is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_rollover_across_the_turn_of_the_year_is_caught_too() -> None:
    """The month number alone repeats every year, so the comparison has to
    carry the year — December to January is a rollover though both are
    nobody's idea of "month 12 again"."""
    december = {"year": 2026, "month": 12}
    assert _call(december, {"year": 2027, "month": 1}, december) is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_reader_deliberately_parked_on_a_past_month_gets_no_bonus_refresh() -> None:
    # The bug this closes: the real calendar turning from July to August is a
    # rollover by the clock alone, but a reader who navigated away to June and
    # left it there is not the reader that rollover happened *for*. Comparing
    # only today against seen fired anyway, giving that reader one unwanted
    # refresh of a June nobody asked to re-read.
    june = {"year": 2026, "month": 6}
    assert _call(june, _AUGUST, _JULY) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_reader_on_the_month_before_the_one_that_just_ended_gets_no_bonus_either() -> None:
    # cursor names July, the month before the one seen last tick (August) — a
    # reader who paged back one month, not to the month that just rolled off.
    assert _call(_JULY, _SEPTEMBER, _AUGUST) is False


# --- rolloverTick, and the boot-time seeding bug (#216) ---------------------
#
# boot() calls refresh() once for the month it opens on, then hands the timer
# whatever it last saw. The bug: it used to hand the timer null, which reads
# exactly like "the timer has never ticked" and is indistinguishable from a
# tab that booted moments before midnight on the first — the very case the
# bonus refresh exists for. These drive rolloverTick the way boot() now does,
# seeded from the month the initial refresh() actually displayed.


def _tick(
    cursor: dict[str, Any] | None, today: dict[str, Any], seen: dict[str, Any] | None
) -> dict[str, Any]:
    assert NODE is not None
    body = (
        f"{_PRELUDE}\n{_slice()}\n"
        f"console.log(JSON.stringify(rolloverTick({json.dumps(cursor)}, {json.dumps(today)}, "
        f"{json.dumps(seen)})));"
    )
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    out: dict[str, Any] = json.loads(result.stdout.strip())
    return out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_boot_seeded_from_null_misses_the_rollover_it_landed_on() -> None:
    """The regression this closes, stated directly: boot() opens on August,
    the tab sits idle, and the clock turns to September before the first
    tick — exactly the "boots less than one interval before midnight" case.
    Seeded with null, as boot() used to, the tick has no baseline and reads
    no rollover at all, leaving August stale and the forward button dead."""
    assert _tick(_AUGUST, _SEPTEMBER, None)["shouldRefresh"] is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_boot_seeded_from_the_displayed_month_catches_the_same_rollover() -> None:
    """The fix: boot() seeds ``seen`` from ``cursor`` itself — the month its
    own initial refresh() just displayed — rather than null. On the very same
    tick as the test above, that baseline is enough for rolloverTick to
    recognise the rollover and ask for one more refresh of August."""
    seeded_at_boot = {"year": _AUGUST["year"], "month": _AUGUST["month"]}
    tick = _tick(_AUGUST, _SEPTEMBER, seeded_at_boot)
    assert tick["shouldRefresh"] is True
    assert tick["seen"] == _SEPTEMBER


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_tick_with_nothing_to_do_still_updates_what_it_saw() -> None:
    """No rollover and no month in progress: shouldRefresh is false, but
    ``seen`` still advances, or the next tick would be reasoning from a stale
    baseline of its own."""
    tick = _tick(_JULY, _SEPTEMBER, _AUGUST)
    assert tick["shouldRefresh"] is False
    assert tick["seen"] == _SEPTEMBER
