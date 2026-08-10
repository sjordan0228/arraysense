"""The panels editor composes; only Python parses. These prove the composer
emits exactly what parse_strings accepts — the two-parsers drift, forbidden
mechanically, the same way the band editor is held to parse_bands."""

from __future__ import annotations

import json
import shutil
import subprocess
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
