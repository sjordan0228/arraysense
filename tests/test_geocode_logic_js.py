"""test_geocode_logic_js.py — the postcode lookup decides nothing the reply has not said.

The wizard and the settings page both turn a /api/geocode reply into one of
three states — nothing matched, a single pick, or a list the owner chooses
from — and both caption a candidate with its place name, region and country.
Those decisions live as pure functions in common.js (placeLabel and
geocodeOutcome, between the geocode-logic markers), and this runs the exact
slice under node so an ambiguous reply can never be silently resolved to its
first entry and a reply that found nothing can never be shown as a found
place. Skipped where node is not installed, and loud if the markers move, so
the slice cannot drift out from under it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"

_START = "// >>> geocode-logic"
_END = "// <<< geocode-logic"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "geocode-logic markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


# Three candidates in the shapes /api/geocode actually serves: the single
# Argyle result for 76226, and two entries of the ambiguous 2000 reply that
# must never be resolved to their first entry by the page.
CANDIDATES = """
const ARGYLE = { name: "Argyle", admin1: "Texas", country: "United States",
  country_code: "US", latitude: 33.12123, longitude: -97.18335, timezone: "America/Chicago" };
const ANTWERP = { name: "Antwerp", admin1: "Flanders", country: "Belgium",
  country_code: "BE", latitude: 51.22047, longitude: 4.40026, timezone: "Europe/Brussels" };
const FREDERIKSBERG = { name: "Frederiksberg", admin1: "Capital Region", country: "Denmark",
  country_code: "DK", latitude: 55.67938, longitude: 12.53463, timezone: "Europe/Copenhagen" };
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_candidate_is_named_by_place_region_and_country() -> None:
    out = _run(CANDIDATES + "console.log(placeLabel(ARGYLE));")
    assert out == "Argyle, Texas, United States"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_place_label_drops_what_the_geocoder_did_not_supply() -> None:
    # A candidate can arrive with no admin1; the label must not print
    # "undefined" or a trailing comma for a region that was never given.
    out = _run(
        CANDIDATES
        + 'console.log(placeLabel({name:"Westminster Abbey", country:"United Kingdom"}));'
    )
    assert out == "Westminster Abbey, United Kingdom"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_single_candidate_is_a_resolved_pick() -> None:
    out = _run(
        CANDIDATES
        + "const o = geocodeOutcome([ARGYLE]);"
        + "console.log(JSON.stringify([o.status, o.candidate.name, o.candidate.timezone, o.note]));"
    )
    assert out == (
        '["single","Argyle","America/Chicago",'
        '"Argyle, Texas, United States (33.12123, -97.18335)."]'
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_multiple_candidates_are_never_resolved_silently() -> None:
    # The ambiguity path hands the whole list back and asks the owner to
    # choose; it must not pick the first entry as if it were the answer.
    out = _run(
        CANDIDATES
        + "const o = geocodeOutcome([ANTWERP, FREDERIKSBERG]);"
        + "console.log(JSON.stringify([o.status, o.candidates.length, o.note]));"
    )
    assert out == '["multiple",2,"2 results — pick one."]'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_reply_that_found_nothing_is_not_a_place() -> None:
    # A query matching nothing comes back with no results key, which the
    # service maps to an empty list; the box must read it as "nothing
    # matched", never as a candidate with null coordinates.
    out = _run(
        CANDIDATES
        + "const o = geocodeOutcome([]);"
        + "console.log(JSON.stringify([o.status, o.note]));"
    )
    assert out == '["none","Nothing matched that name."]'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_null_reply_is_treated_like_an_empty_one() -> None:
    # A reply that failed to parse also arrives as nothing; it must take the
    # same "no match" path rather than crash the renderer.
    out = _run(CANDIDATES + "const o = geocodeOutcome(null); console.log(o.status);")
    assert out == "none"
