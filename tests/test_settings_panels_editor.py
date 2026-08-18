"""The panels editor composes; only Python parses. These prove the composer
emits exactly what parse_strings accepts — the two-parsers drift, forbidden
mechanically, the same way the band editor is held to parse_bands."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import date
from pathlib import Path

import pytest

from arraysense.panels import parse_strings

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "settings.html"
SLICE_FROM = "const PANELS_KEY"
SLICE_TO = "function panelsEditorEnd("


def _slice() -> str:
    text = PAGE.read_text()
    return text[text.index(SLICE_FROM) : text.index(SLICE_TO)]


def _run(body: str) -> str:
    assert NODE is not None
    result = subprocess.run(
        ["node", "-e", _slice() + "\n" + body],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_composer_emits_what_the_parser_accepts() -> None:
    row = {
        "name": "East",
        "mppt": 1,
        "panels": 9,
        "watts": 410,
        "tilt": 25,
        "azimuth": 90,
        "advanced": {"bifacial": "9", "note": "afternoon oak shade"},
    }
    line = _run(f"console.log(composeStringLine({json.dumps(row)}));")
    (spec,) = parse_strings(line)
    assert spec.name == "East"
    assert spec.bifacial_pct == 9.0
    assert spec.note == "afternoon oak shade"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_empty_advanced_fields_are_omitted_not_emitted() -> None:
    row = {
        "name": "West",
        "mppt": 2,
        "panels": 9,
        "watts": 410,
        "tilt": 25,
        "azimuth": 270,
        "advanced": {"noct": "", "vmp": ""},
    }
    line = _run(f"console.log(composeStringLine({json.dumps(row)}));")
    (spec,) = parse_strings(line)
    assert "noct" in spec.defaulted


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_split_round_trips_the_composer() -> None:
    row = {
        "name": "South",
        "mppt": 3,
        "panels": 8,
        "watts": 405,
        "tilt": 30,
        "azimuth": 180,
        "advanced": {"mounting": "ground"},
    }
    out = _run(
        f"const line = composeStringLine({json.dumps(row)});"
        "console.log(JSON.stringify(splitStringLine(line)));"
    )
    assert json.loads(out)["advanced"]["mounting"] == "ground"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_editor_defaults_match_the_parsers_defaults() -> None:
    # The placeholders tell the owner what will be assumed. If they drift from
    # what the server actually applies, the page shows one number while the
    # model uses another — the two-places rule, in the one place a comment
    # claimed a test already covered.
    from arraysense.panels import _DEFAULT_MOUNTING, _FLOAT_DEFAULTS

    shown = json.loads(_run("console.log(JSON.stringify(PANEL_DEFAULTS));"))
    for key, value in _FLOAT_DEFAULTS.items():
        assert float(shown[key]) == value, f"{key}: page shows {shown[key]}, parser uses {value}"
    assert shown["mounting"] == _DEFAULT_MOUNTING


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_positional_field_round_trips_through_the_editor() -> None:
    # The advanced fields were covered; a transposition in the positional
    # indices would have gone unnoticed and shown the owner someone else's
    # tilt.
    row = {
        "name": "South",
        "mppt": 3,
        "panels": 8,
        "watts": 405,
        "tilt": 30,
        "azimuth": 180,
        "advanced": {},
    }
    out = _run(
        f"const line = composeStringLine({json.dumps(row)});"
        "console.log(JSON.stringify(splitStringLine(line)));"
    )
    back = json.loads(out)
    assert back["name"] == "South"
    assert str(back["mppt"]) == "3"
    assert str(back["panels"]) == "8"
    assert str(back["watts"]) == "405"
    assert str(back["tilt"]) == "30"
    assert str(back["azimuth"]) == "180"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_quoted_note_survives_compose_and_split() -> None:
    row = {
        "name": "East",
        "mppt": 1,
        "panels": 9,
        "watts": 410,
        "tilt": 25,
        "azimuth": 90,
        "advanced": {"note": 'he said "shaded" at 4pm'},
    }
    line = _run(f"console.log(composeStringLine({json.dumps(row)}));")
    (spec,) = parse_strings(line)
    assert spec.note == 'he said "shaded" at 4pm'
    back = json.loads(_run(f"console.log(JSON.stringify(splitStringLine({json.dumps(line)})));"))
    assert back["advanced"]["note"] == 'he said "shaded" at 4pm'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_composer_emits_a_tilt_schedule_the_parser_accepts() -> None:
    # The editor holds the tilt field as free text and never parses it, so a
    # schedule has to survive composition untouched. If the composer ever
    # starts coercing this field to a number the seasonal mount silently
    # becomes a fixed one, which is the failure this whole issue is about.
    row = {
        "name": "East",
        "mppt": 1,
        "panels": 9,
        "watts": 410,
        "tilt": "25,40@2027-10-01,25@2028-03-15",
        "azimuth": 90,
        "advanced": {},
    }
    line = _run(f"console.log(composeStringLine({json.dumps(row)}));")
    (spec,) = parse_strings(line)
    assert len(spec.tilt_schedule) == 3
    assert spec.tilt_at(date(2026, 1, 1)) == 25.0
    assert spec.tilt_at(date(2027, 12, 1)) == 40.0
    assert spec.tilt_at(date(2028, 6, 1)) == 25.0


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_tilt_schedule_survives_the_round_trip_back_into_the_editor() -> None:
    row = {
        "name": "East",
        "mppt": 1,
        "panels": 9,
        "watts": 410,
        "tilt": "25,40@2027-10-01",
        "azimuth": 90,
        "advanced": {},
    }
    back = json.loads(
        _run(
            f"const line = composeStringLine({json.dumps(row)});"
            "console.log(JSON.stringify(splitStringLine(line)));"
        )
    )
    assert back["tilt"] == "25,40@2027-10-01"
