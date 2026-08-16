"""Tests for the per-string panel grammar: one parser, defaults named, refusals quoted."""

from __future__ import annotations

from datetime import date

import pytest

from arraysense.panels import (
    EXAMPLE_STRINGS,
    MOUNTINGS,
    PANEL_CATALOGUE,
    TiltEntry,
    parse_strings,
    parse_tilt_schedule,
)


def test_a_minimal_line_parses_with_every_default_named() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90")
    assert s.name == "East"
    assert (s.mppt, s.panels, s.watts, s.azimuth) == (1, 9, 410.0, 90.0)
    # A fixed mount is a schedule of one, in force since before anything was
    # recorded — the shape every array described before schedules existed takes.
    assert s.tilt_schedule == (TiltEntry(degrees=25.0, effective_from=None),)
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


# --- panel catalogue -----------------------------------------------------------


def test_panel_catalogue_entries_exist() -> None:
    assert len(PANEL_CATALOGUE) == 2
    names = [e.name for e in PANEL_CATALOGUE]
    assert "108cell_perc" in names
    assert "120halfcell_perc" in names


def test_panel_entry_fills_vmp_and_voc() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90 | panel=108cell_perc")
    assert s.vmp == pytest.approx(31.01)
    assert s.voc == pytest.approx(37.07)
    assert s.panel == "108cell_perc"
    assert "vmp" in s.defaulted
    assert "voc" in s.defaulted


def test_explicit_key_beats_catalogue_value() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90 | panel=108cell_perc vmp=32.5")
    assert s.vmp == 32.5  # owner's own, not the catalogue's 31.01
    assert "vmp" not in s.defaulted  # explicitly set, not defaulted


def test_unknown_panel_entry_refused_by_name() -> None:
    with pytest.raises(ValueError, match="unknown panel entry"):
        parse_strings("East | 1 | 9 | 410 | 25 | 90 | panel=nonesuch")


def test_panel_catalogue_fields_join_defaulted() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90 | panel=108cell_perc")
    for field in ("vmp", "voc", "temp_coeff", "noct", "degradation"):
        assert field in s.defaulted, f"{field} should be defaulted from the catalogue"


def test_catalogue_values_match_source_documents() -> None:
    """Pin the transcribed figures against drift.

    The values must match panel-specs.md, which was researched from the
    manufacturer datasheets. Changing one means re-reading the datasheet
    and noting it in the changelog.
    """
    perc = {e.name: e for e in PANEL_CATALOGUE}["108cell_perc"]
    assert perc.vmp == 31.01
    assert perc.voc == 37.07
    assert perc.temp_coeff == -0.35
    assert perc.noct == 45.0
    assert perc.degradation == 0.45
    assert "Runergy" in perc.citation

    half = {e.name: e for e in PANEL_CATALOGUE}["120halfcell_perc"]
    assert half.vmp == 34.5
    assert half.voc == 41.4
    assert half.temp_coeff == -0.36
    assert half.noct == 45.0
    assert half.degradation == 0.5
    assert "Aptos" in half.citation


def test_panel_is_none_when_not_chosen() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90")
    assert s.panel is None


def test_panel_is_stored_in_known_string_keys() -> None:
    # A test the settings page reads to learn which keys the grammar accepts.
    from arraysense.panels import KNOWN_STRING_KEYS

    assert "panel" in KNOWN_STRING_KEYS


def test_a_bare_number_is_a_schedule_of_one_entry() -> None:
    result = parse_tilt_schedule("25", "25")
    assert len(result) == 1
    assert result[0].degrees == 25.0
    assert result[0].effective_from is None


def test_a_number_with_date_sets_effective_from() -> None:
    result = parse_tilt_schedule("25@2024-03-01", "25@2024-03-01")
    assert len(result) == 1
    assert result[0].degrees == 25.0
    assert result[0].effective_from == date(2024, 3, 1)


def test_multiple_entries_with_dates_are_parsed_in_order() -> None:
    result = parse_tilt_schedule("25,40@2024-10-01,25@2025-03-15", "25,40@2024-10-01,25@2025-03-15")
    assert len(result) == 3
    assert result[0].degrees == 25.0 and result[0].effective_from is None
    assert result[1].degrees == 40.0 and result[1].effective_from == date(2024, 10, 1)
    assert result[2].degrees == 25.0 and result[2].effective_from == date(2025, 3, 15)


def test_whitespace_around_commas_and_at_sign_is_stripped() -> None:
    result = parse_tilt_schedule(" 25 , 40 @ 2024-10-01 ", " 25 , 40 @ 2024-10-01 ")
    assert len(result) == 2
    assert result[0].degrees == 25.0 and result[0].effective_from is None
    assert result[1].degrees == 40.0 and result[1].effective_from == date(2024, 10, 1)


def test_zero_and_ninety_degrees_are_accepted() -> None:
    result0 = parse_tilt_schedule("0", "0")
    assert result0[0].degrees == 0.0
    result90 = parse_tilt_schedule("90", "90")
    assert result90[0].degrees == 90.0


def test_empty_string_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("", "")
    with pytest.raises(ValueError):
        parse_tilt_schedule("   ", "   ")


def test_empty_entry_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("25,,30@2024-03-01", "25,,30@2024-03-01")


def test_degrees_outside_0_to_90_raise_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("-1", "-1")
    with pytest.raises(ValueError):
        parse_tilt_schedule("91", "91")


def test_non_numeric_input_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("abc", "abc")


def test_later_entry_without_date_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("25,30", "25,30")


def test_equal_dates_do_not_increase_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("25@2024-03-01,30@2024-03-01", "25@2024-03-01,30@2024-03-01")


def test_descending_dates_raise_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("25@2024-03-01,30@2024-02-01", "25@2024-03-01,30@2024-02-01")


def test_malformed_dates_raise_value_error() -> None:
    with pytest.raises(ValueError):
        parse_tilt_schedule("25@2024-13-01", "25@2024-13-01")
    with pytest.raises(ValueError):
        parse_tilt_schedule("25@2024-03", "25@2024-03")
    with pytest.raises(ValueError):
        parse_tilt_schedule("25@march", "25@march")


def test_fixed_mount_returns_same_tilt_for_any_day() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25 | 90")
    assert s.tilt_at(date(1999, 1, 1)) == 25.0


def test_earlier_day_returns_first_tilt_in_schedule() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2024-10-01 | 90")
    assert s.tilt_at(date(2024, 9, 30)) == 25.0


def test_the_boundary_day_takes_the_new_tilt() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2024-10-01 | 90")
    assert s.tilt_at(date(2024, 10, 1)) == 40.0


def test_far_future_day_returns_last_tilt_in_schedule() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2024-10-01 | 90")
    assert s.tilt_at(date(2030, 1, 1)) == 40.0


def test_three_entry_schedule_returns_correct_tilt_per_day() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2024-10-01,30@2025-03-15 | 90")
    assert s.tilt_at(date(2024, 12, 25)) == 40.0
    assert s.tilt_at(date(2025, 6, 1)) == 30.0


def test_schedule_survives_tail_and_parses_additional_fields() -> None:
    (s,) = parse_strings('East | 1 | 9 | 410 | 25,40@2024-10-01 | 90 | bifacial=9 note="hi there"')
    assert len(s.tilt_schedule) == 2
    assert s.bifacial_pct == 9.0


def test_appending_an_adjustment_to_a_fixed_tilt_keeps_the_old_angle_behind_it() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2027-10-01 | 90")
    assert len(s.tilt_schedule) == 2
    assert s.tilt_at(date(2027, 9, 30)) == 25.0
    assert s.tilt_at(date(2027, 10, 1)) == 40.0


def test_appending_twice_gives_three_angles_each_in_force_in_turn() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2027-10-01,30@2028-03-15 | 90")
    assert len(s.tilt_schedule) == 3
    assert s.tilt_at(date(2027, 1, 1)) == 25.0
    assert s.tilt_at(date(2027, 12, 1)) == 40.0
    assert s.tilt_at(date(2028, 6, 1)) == 30.0


def test_appending_to_an_empty_tilt_gives_a_dated_schedule_of_one() -> None:
    # The composer produces this when the box was empty. A day before the only
    # entry still reports its angle: it is the only one anybody has stated.
    (s,) = parse_strings("East | 1 | 9 | 410 | 40@2027-10-01 | 90")
    assert len(s.tilt_schedule) == 1
    assert s.tilt_at(date(1999, 1, 1)) == 40.0


def test_appending_an_out_of_order_date_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_strings("East | 1 | 9 | 410 | 25,40@2028-03-15,30@2027-10-01 | 90")


def test_appending_the_same_date_twice_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_strings("East | 1 | 9 | 410 | 25,40@2027-10-01,30@2027-10-01 | 90")


def test_appending_degrees_past_vertical_is_refused() -> None:
    with pytest.raises(ValueError):
        parse_strings("East | 1 | 9 | 410 | 25,95@2027-10-01 | 90")


def test_a_schedule_and_a_tail_coexist() -> None:
    (s,) = parse_strings("East | 1 | 9 | 410 | 25,40@2027-10-01 | 90 | mounting=ground bifacial=9")
    assert s.tilt_schedule == (
        TiltEntry(degrees=25.0, effective_from=None),
        TiltEntry(degrees=40.0, effective_from=date(2027, 10, 1)),
    )
    assert s.mounting == "ground"
    assert s.bifacial_pct == 9.0


def test_a_schedule_on_one_string_leaves_the_others_fixed() -> None:
    east, west = parse_strings(
        "East | 1 | 9 | 410 | 25,40@2027-10-01 | 90\nWest | 2 | 9 | 410 | 30 | 270"
    )
    assert len(east.tilt_schedule) == 2
    assert len(west.tilt_schedule) == 1
    assert west.tilt_at(date(2030, 1, 1)) == 30.0


def test_a_refused_schedule_quotes_the_line_it_came_from() -> None:
    with pytest.raises(ValueError) as exc:
        parse_strings("East | 1 | 9 | 410 | 25,30 | 90")
    assert "East" in str(exc.value)
