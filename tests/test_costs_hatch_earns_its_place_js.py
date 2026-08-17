"""test_costs_hatch_earns_its_place_js.py — a hatch marks nothing if it marks everything.

A colour-blind reader has lightness and the hatch texture in place of hue. On an
installation whose circuit history is younger than the month being priced, every
band of every circuit comes back partial, and a hatch on every segment then marks
nothing — it struck the labels out and washed the lightness difference between
peak and off-peak out with it (#31). everyBandPartial is the gate that decides
when the texture is worth withholding in favour of saying the same fact once, in
the caption, instead — and the one case it must never trip on is a panel that
measured nothing at all: that would put a sentence on screen claiming every band
was partly recorded when in truth none was recorded at all.
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

_START = "// >>> costs-hatch-earns-its-place"
_END = "// <<< costs-hatch-earns-its-place"


def _slice() -> str:
    text = COSTS.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "costs-hatch-earns-its-place markers are out of order in costs.html"
    return text[start:end]


def _band(cost: float | None, partial: bool) -> dict[str, Any]:
    return {"cost": cost, "partial": partial}


def _every_band_partial(rows: list[dict[str, Any]]) -> bool:
    assert NODE is not None
    body = f"{_slice()}\nconsole.log(JSON.stringify(everyBandPartial({json.dumps(rows)})));"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    out: bool = json.loads(result.stdout.strip())
    return out


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_every_measured_band_partial_trips_the_blanket() -> None:
    """The case the block exists for: a young installation whose every band of
    every circuit is partial, where a hatch on every segment would mark nothing
    and the caption should carry the fact instead."""
    rows = [
        {"bands": [_band(4.0, True), _band(1.0, True)]},
        {"bands": [_band(2.0, True)]},
    ]
    assert _every_band_partial(rows) is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_one_fully_recorded_band_keeps_the_hatch_meaningful() -> None:
    """One band measured in full among the partial ones is exactly what the
    hatch exists to distinguish, so the flag must not fire and blank it out."""
    rows = [
        {"bands": [_band(4.0, True), _band(1.0, False)]},
        {"bands": [_band(2.0, True)]},
    ]
    assert _every_band_partial(rows) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_measured_bands_at_all_is_not_a_blanket_partial() -> None:
    """The failure this test exists to prevent: a panel with nothing measured
    must not claim every band was partly recorded. That is a stronger and false
    claim than silence — it asserts a measurement that never happened."""
    rows = [
        {"bands": [_band(None, False), _band(None, False)]},
        {"bands": [_band(None, True)]},
    ]
    assert _every_band_partial(rows) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_unmeasured_bands_are_ignored_rather_than_counted_against_the_blanket() -> None:
    """A band a circuit never reported in draws no segment at all, so it must
    not dilute the vote: measured bands that are all partial still trip the
    flag beside an unreported one, even though that one happens to carry
    partial: false — its cost is null, so it never reaches the check."""
    rows = [
        {"bands": [_band(4.0, True), _band(None, False)]},
        {"bands": [_band(2.0, True)]},
    ]
    assert _every_band_partial(rows) is True
