"""Tests for the per-string panel grammar: one parser, defaults named, refusals quoted."""

from __future__ import annotations

import pytest

from arraysense.panels import EXAMPLE_STRINGS, MOUNTINGS, parse_strings


def test_a_minimal_line_parses_with_every_default_named() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90")
    assert s.name == "East"
    assert (s.mppt, s.panels, s.watts, s.tilt, s.azimuth) == (1, 9, 410.0, 25.0, 90.0)
    # Defaults applied AND named — the UI labels them; silence is the bug.
    assert s.temp_coeff == -0.35
    assert s.noct == 45.0
    assert s.mounting == "open_rack"
    assert s.bifacial_pct == 0.0
    assert s.degradation == 0.5
    assert s.installed is None and s.vmp is None and s.voc is None
    assert {"temp_coeff", "noct", "mounting", "bifacial", "installed"} <= set(s.defaulted)


def test_tail_values_override_and_leave_the_defaulted_set() -> None:
    (s,) = parse_strings(
        "East | 1 | 9 | 410 | 25 | 90 | temp_coeff=-0.30 bifacial=9 mounting=ground "
        'installed=2024-08 vmp=41.5 note="afternoon oak shade"'
    )
    assert s.temp_coeff == -0.30
    assert s.bifacial_pct == 9.0
    assert s.mounting == "ground"
    assert s.installed == "2024-08"
    assert s.vmp == 41.5
    assert s.note == "afternoon oak shade"
    assert "temp_coeff" not in s.defaulted and "bifacial" not in s.defaulted


def test_multiple_lines_comments_and_blanks() -> None:
    text = """
    # the west field
    West | 2 | 9 | 410 | 25 | 270
    South | 3 | 8 | 405 | 30 | 180 | noct=47
    """
    strings = parse_strings(text)
    assert [s.name for s in strings] == ["West", "South"]
    assert strings[1].noct == 47.0


def test_empty_text_is_a_valid_unconfigured_array() -> None:
    assert parse_strings("") == ()
    assert parse_strings("  \n  # only a comment\n") == ()


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ("East | 1 | 9 | 410 | 95 | 90", "tilt"),  # tilt past vertical
        ("East | 1 | 9 | 410 | 25 | 361", "azimuth"),  # azimuth past a circle
        ("East | 0 | 9 | 410 | 25 | 90", "mppt"),  # MPPT numbering starts at 1
        ("East | 1 | 0 | 410 | 25 | 90", "panels"),  # a string of no panels
        ("East | 1 | 9 | 20 | 25 | 90", "watts"),  # implausible nameplate
        ("East | 1 | 9 | 410 | 25 | 90 | mounting=carport", "mounting"),
        ("East | 1 | 9 | 410 | 25 | 90 | shade=heavy", "unknown key"),
        ("East | 1 | 9 | 410 | 25 | 90 | installed=aug-24", "installed"),
        ("East | 1 | 9 | 410 | 25", "six"),  # too few fields
        ("East | 1 | nine | 410 | 25 | 90", "panels"),  # not a number
    ],
)
def test_refusals_quote_the_offending_line(line: str, fragment: str) -> None:
    with pytest.raises(ValueError) as exc:
        parse_strings(line)
    message = str(exc.value)
    assert fragment.lower() in message.lower()
    assert line.strip().split("|")[0].strip() in message  # the line is identifiable


def test_duplicate_names_are_refused() -> None:
    text = "East | 1 | 9 | 410 | 25 | 90\nEast | 2 | 9 | 410 | 25 | 90"
    with pytest.raises(ValueError, match="East"):
        parse_strings(text)


def test_the_example_parses_and_mountings_are_the_grammars() -> None:
    assert len(parse_strings(EXAMPLE_STRINGS)) >= 2
    assert MOUNTINGS == ("open_rack", "close_roof", "ground")


def test_a_note_may_quote_something_and_survive_the_round_trip() -> None:
    # A note is free text the owner writes about a string; refusing it for
    # containing a quotation mark would be the grammar dictating prose.
    (s,) = parse_strings('East | 1 | 9 | 410 | 25 | 90 | note="he said \\"shaded\\" at 4pm"')
    assert s.note == 'he said "shaded" at 4pm'


def test_a_quoted_note_may_contain_the_separator() -> None:
    # The tail is rejoined before its tokens are read, so a note naming two
    # arrays does not tear the line in half.
    (s,) = parse_strings('W | 2 | 9 | 410 | 25 | 270 | note="East|West roofline"')
    assert "East" in s.note and "West" in s.note


def test_a_tail_of_unreadable_text_is_still_refused() -> None:
    with pytest.raises(ValueError, match="could not read"):
        parse_strings("E | 1 | 9 | 410 | 25 | 90 | garbage here")
