"""Scaffold smoke test.

Proves the package is importable and every module in the planned layout exists.
Replace nothing here — add real tests alongside it as features land.
"""

import importlib
import re
from pathlib import Path

import pytest

import arraysense

MODULES = [
    "arraysense.metrics",
    "arraysense.models",
    "arraysense.config",
    "arraysense.validate",
    "arraysense.collector.source",
    "arraysense.collector.service",
    "arraysense.drivers",
    "arraysense.drivers.base",
    "arraysense.drivers.eg4_luxpower",
    "arraysense.drivers.eg4_luxpower.source",
    "arraysense.drivers.fake",
    "arraysense.drivers.fake.source",
    "arraysense.store.base",
    "arraysense.store.schema",
    "arraysense.store.sqlite_store",
    "arraysense.store.rollup",
    "arraysense.store.tiers",
    "arraysense.api.app",
    "arraysense.api.routes",
]


def test_version_is_exposed() -> None:
    assert isinstance(arraysense.__version__, str)
    assert arraysense.__version__.count(".") >= 2


@pytest.mark.parametrize("name", MODULES)
def test_planned_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None


def test_the_unit_file_and_the_docs_agree_on_the_restart_policy() -> None:
    # A watchdog that kills the process by SIGTERM only causes a restart if
    # systemd treats that as a failure. The shipped unit says on-failure while
    # docs/installation.md and __main__.py both assert always, so an install made
    # from this repository would stop dead where the reference box restarts. The
    # two have to say the same thing, whichever way it is settled.
    unit = (Path(__file__).resolve().parents[1] / "packaging" / "arraysense.service").read_text()
    docs = (Path(__file__).resolve().parents[1] / "docs" / "installation.md").read_text()
    policy = [
        line.split("=", 1)[1].strip()
        for line in unit.splitlines()
        if line.strip().startswith("Restart=")
    ]
    assert policy, "the unit file states no Restart policy"
    for stated in policy:
        assert f"Restart={stated}" in docs, (
            f"the unit ships Restart={stated} and the installation docs do not say so"
        )


def test_the_measured_battery_colours_are_pinned_in_common_js() -> None:
    # Charge green and discharge red were chosen by measuring every pair under
    # simulated protanopia, deuteranopia and tritanopia against the panel
    # surface, not by eye. Nudging either value by eye is exactly the failure
    # this project cares about, so the declarations the palette ships are pinned
    # here rather than trusted to a comment.
    #
    # Searching the whole file is not enough: both values also appear in the
    # INK_FALLBACK map, so deleting the real CSS declaration while leaving the
    # fallback behind would still pass and the charts would silently change.
    # The :root block is what the browser actually reads, so that is what is
    # pinned, and the fallback is checked separately for agreeing with it.
    common = (
        Path(__file__).resolve().parents[1] / "src" / "arraysense" / "web" / "common.js"
    ).read_text()
    # Parsed rather than searched for a literal, and not by splitting on ":root{"
    # — the stylesheet writes ":root {" with a space, so that split silently
    # matched nothing and fell through to the whole file.
    declared = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", common))
    assert declared.get("--batt") == "#2aa198", "the shipped palette no longer declares --batt"
    assert declared.get("--batt-dis") == "#d1495b", (
        "the shipped palette no longer declares --batt-dis"
    )
    fallback = common.split("INK_FALLBACK", 1)[-1].split("};", 1)[0]
    assert "'--batt':'#2aa198'" in fallback and "'--batt-dis':'#d1495b'" in fallback, (
        "INK_FALLBACK has drifted from the :root palette it stands in for"
    )


def _web(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "arraysense" / "web" / name).read_text()


_CHART_HUES = {
    "--pv": "#cf7b26",
    "--load": "#4678cc",
    "--batt": "#2aa198",
    "--grid": "#b0486e",
    "--batt-dis": "#d1495b",
}


def test_the_power_flow_chart_fills_only_the_grid_series() -> None:
    # Band shading is drawn behind the series, and it cannot be read under two
    # competing area fills — on a sunny day the solar area covers most of the
    # plot. Grid keeps its fill for a reason recorded beside gridFill: when the
    # house runs on the grid, import equals house load to the watt, so a grid
    # *line* lies exactly under the home line and vanishes beneath it. Solar has
    # no such coincidence, so as a line it stays legible.
    page = _web("index.html")
    assert "gridFill" in page, "grid lost the fill that stops it vanishing under home"
    assert "pvFill" not in page, "solar is still filled, so shading cannot be read beneath it"


def test_pv_fill_is_kept_available_even_though_unused() -> None:
    # Removed from the chart, not deleted from the codebase: the volume reading
    # it gave is a real if minor loss and may be wanted back.
    assert "function pvFill" in _web("common.js")


def test_band_shading_adds_no_colour_to_the_palette() -> None:
    # The whole point of shading by luminance rather than hue. The owner is
    # colour blind and every hue has to be measured against every other, so a
    # band that needed its own colour would be a hue nobody checked — and a
    # tariff has as many bands as it likes, which two colours could never say.
    declared = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*(#[0-9a-fA-F]{6})", _web("common.js")))
    assert {k: v for k, v in declared.items() if k in _CHART_HUES} == _CHART_HUES, (
        f"the chart palette changed; band shading must add no hue. found {declared}"
    )
    assert not [k for k in declared if k.startswith("--band")], (
        "a per-band colour was added; shading must vary opacity, not hue"
    )


def test_nothing_drawn_to_canvas_hardcodes_white() -> None:
    # Light mode inverts the surface, and a white gridline, zero rule or band
    # wash simply disappears on it. CSS can answer to a theme; a string handed
    # to ctx.fillStyle cannot, so these have to read a token that changes with
    # the theme rather than a literal that does not.
    web = Path(__file__).resolve().parents[1] / "src" / "arraysense" / "web"
    offenders = []
    for path in sorted(web.glob("*.html")) + sorted(web.glob("common.js")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if ("fillStyle" in line or "strokeStyle" in line) and "rgba(255,255,255" in line:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, (
        "canvas drawing hardcodes white and will vanish in light mode: " + ", ".join(offenders)
    )


def test_the_band_shading_legend_does_not_hardcode_a_direction() -> None:
    # On a dark panel the wash is white, so more opacity reads brighter. On a
    # light one it must be dark, and "brighter" becomes exactly wrong — the
    # reader is sent to the cheap hours. Whatever the page says about direction
    # has to be decided with the theme, not written into the string.
    page = _web("index.html")
    assert "brighter = higher rate" not in page, (
        "the legend states a fixed direction; it reverses in light mode"
    )


def test_both_themes_declare_the_chart_palette() -> None:
    # The hues themselves separate identically whatever the surface — a pair's
    # distance does not depend on the background. What changes is contrast
    # against it, so a light theme has to declare its own values rather than
    # inherit ones chosen against #131a2e.
    common = _web("common.js")
    assert "prefers-color-scheme" in common, "no light theme is declared at all"
    light = common.split("prefers-color-scheme", 1)[-1]
    for token in ("--ink", "--panel", "--grid-line"):
        assert token in light, f"the light theme does not redeclare {token}"


def test_the_theme_is_applied_after_the_constants_it_reads() -> None:
    # A `const` is not hoisted, so calling the resolver above its own constants
    # lands in the temporal dead zone. That happened: the ReferenceError was
    # caught by the guard around localStorage and answered "system", so a saved
    # theme was silently ignored — nothing in the console, every test green, and
    # only visible by loading the page and reading the attribute that was never
    # set. Source order is the only thing that prevents it, so source order is
    # what this checks.
    common = _web("common.js")
    declared = common.index("const THEME_KEY")
    applied = common.index("applyStoredTheme();")
    assert declared < applied, (
        "applyStoredTheme() is called above the constants it reads; the resolver "
        "will hit the temporal dead zone and quietly fall back to 'system'"
    )
