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
    # Scoped to the power-flow chart's own builder rather than the whole page:
    # the rule protects the tariff shading drawn behind THIS chart, and the
    # forecast chart fills its actual series legitimately — nothing is shaded
    # beneath it, and its solid-against-hatch mass is the owner's chosen way to
    # tell a measurement from a prediction.
    page = _web("index.html")
    start = page.index("function drawPower(")
    end = page.index("function drawBatt", start)
    flow = page[start:end]
    assert "gridFill" in flow, "grid lost the fill that stops it vanishing under home"
    assert "pvFill" not in flow, "solar is still filled, so shading cannot be read beneath it"


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


def _css_block(common: str, name: str) -> str:
    start = common.index(f"const {name}")
    open_backtick = common.index("`", start)
    return common[open_backtick + 1 : common.index("`", open_backtick + 1)]


def test_no_rule_in_the_shared_stylesheet_hardcodes_a_theme_surface() -> None:
    # Light mode was unreadable where a rule painted a fixed dark-theme colour
    # — a white tint, or a near-black fill — because a literal does not invert
    # with the theme. Token declarations (--foo:rgba(...)) are exactly where a
    # themed colour belongs and are declared in both themes, so this strips
    # every one and demands the rule bodies left behind contain no white tint
    # and no near-black background.
    common = _web("common.js")
    stylesheet = _css_block(common, "LIGHT_TOKENS") + "\n" + _css_block(common, "BASE_CSS")
    declared = _strip_token_declarations(stylesheet)
    assert not _WHITE_TINT.search(declared), (
        "a rule in the shared stylesheet hardcodes a white tint; it must read a "
        "token so light mode can invert it"
    )
    assert not _fixed_surfaces(declared), (
        "a rule in the shared stylesheet hardcodes a theme surface: "
        + ", ".join(_fixed_surfaces(declared))
    )


# The pages carry their own style blocks, and five of the surfaces this rule
# governs live in them rather than in common.js. Its neighbour above already
# globs *.html; scanning only common.js here would leave a hardcoded background
# on the dashboard or the efficiency page green.
def test_no_page_style_block_hardcodes_a_theme_surface() -> None:
    web = Path(__file__).resolve().parents[1] / "src" / "arraysense" / "web"
    offenders = []
    for path in sorted(web.glob("*.html")):
        for block in re.findall(r"<style[^>]*>(.*?)</style>", path.read_text(), re.S):
            offenders += [f"{path.name}: {hit}" for hit in _fixed_surfaces(block)]
    # A white *background* is a surface and must follow the theme. A white
    # foreground is not: several rules set text on a filled chart bar, whose
    # colour is the same in both themes, and those say so in a comment.
    assert not offenders, "a page hardcodes a theme surface: " + ", ".join(offenders)


_WHITE_TINT = re.compile(r"rgba\(\s*255\s*,\s*255\s*,\s*255", re.I)
_SURFACE = re.compile(
    r"background(?:-color)?\s*:\s*"
    r"(rgba?\(\s*\d+\s*,\s*\d+\s*,\s*\d+[^)]*\)|#[0-9a-f]{3,8}\b)",
    re.I,
)


def _strip_token_declarations(css: str) -> str:
    # Only inside a token block. Applied to the whole sheet it would also delete
    # a rule that overrides a token and then paints with it, hiding exactly the
    # kind of hardcoded surface this is looking for.
    return re.sub(r"(?<![-a-z])--[a-z0-9-]+\s*:\s*[^;}]+(?=[;}])", "", css, flags=re.I)


def _channels(value: str) -> tuple[int, int, int] | None:
    if value.startswith("#"):
        digits = value[1:]
        if len(digits) in (3, 4):
            digits = "".join(c * 2 for c in digits)
        if len(digits) not in (6, 8):
            return None
        return tuple(int(digits[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    numbers = [int(n) for n in re.findall(r"\d+", value)[:3]]
    return (numbers[0], numbers[1], numbers[2]) if len(numbers) == 3 else None


def _fixed_surfaces(css: str) -> list[str]:
    """Backgrounds pinned to one theme's end of the range.

    Near-black is unreadable under light mode's dark ink, near-white under dark
    mode's white ink. The threshold is deliberately loose: this project's own
    former panel colour was #131a2e, whose blue channel alone is 46, so a test
    that only caught channels under 40 would have let it through.
    """
    offenders = []
    for match in _SURFACE.finditer(_strip_token_declarations(css)):
        channels = _channels(match.group(1))
        if channels is None:
            continue
        if max(channels) < 60 or min(channels) > 200:
            offenders.append(match.group(0).strip())
    return offenders


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


def test_the_look_and_the_colour_mode_are_not_settings_in_the_database() -> None:
    # Both are per-browser on purpose: one household can want the wall tablet
    # dark and glassy and the laptop following the room, and a registry entry is
    # one value for the whole installation, so saving it on either device would
    # drag the other with it. The Settings page carries the two controls anyway,
    # which is exactly the shape somebody later "tidies up" into the registry —
    # so the decision is pinned here rather than left to the comment beside it.
    from arraysense.settings import SETTINGS

    for spec in SETTINGS:
        tail = spec.key.split(".")[-1]
        assert tail not in {"appearance", "look", "theme", "colour_mode", "color_mode"}, (
            f"{spec.key} would put a per-browser choice in the database, where it "
            "becomes one look for every device that opens this installation"
        )


def test_each_per_browser_choice_has_exactly_one_writer() -> None:
    # The theme is now changed from two places — the glyph in every header and
    # the list on the Settings page — and the look will be the moment a second
    # control wants it. A control that writes the key itself is a second source
    # of truth, and the failure is the quiet kind: two controls showing
    # different answers for one browser, each of them certain.
    common = _web("common.js")
    for key in ("THEME_KEY", "APPEARANCE_KEY"):
        writes = common.count(f"localStorage.setItem({key}")
        assert writes == 1, f"{key} is written from {writes} places in common.js, not one"
    # And not from a page at all, under the constant's name or its own.
    from arraysense.api.app import PAGES

    for name in PAGES.values():
        page = _web(name)
        for spelling in (
            "localStorage.setItem(THEME_KEY",
            "localStorage.setItem(APPEARANCE_KEY",
            "arraysense-theme",
            "arraysense-appearance",
        ):
            assert spelling not in page, f"{name} writes a per-browser choice behind common.js"


def _js_map_keys(source: str, name: str) -> list[str]:
    """The keys of a single-line object literal declared as `const <name> = {…}`."""
    start = source.index(f"const {name} = {{")
    body = source[start : source.index("};", start)]
    return re.findall(r"[{,]\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body)


def test_every_look_and_theme_a_control_can_offer_has_a_word_for_it() -> None:
    # The Settings page lists the looks off APPEARANCE_SHEET's own keys and the
    # themes off THEME_ORDER, and labels each from the name maps beside them —
    # which is what lets a third look appear on the page with no edit to it. The
    # failure that shape allows is a look with no entry in the names: a radio
    # labelled "glass2", or worse an empty one, on the page whose job is
    # choosing between them.
    common = _web("common.js")
    for offered, names in (
        ("APPEARANCE_SHEET", "APPEARANCE_NAMES"),
        ("THEME_GLYPH", "THEME_NAMES"),
    ):
        assert set(_js_map_keys(common, offered)) == set(_js_map_keys(common, names)), (
            f"{offered} and {names} name different sets, so a control rendered from "
            "the first would show a choice the second cannot label"
        )
    order = re.search(r"const THEME_ORDER = \[([^\]]*)\]", common)
    assert order is not None
    assert set(re.findall(r"'([a-z]+)'", order.group(1))) == set(
        _js_map_keys(common, "THEME_NAMES")
    )


def test_the_theme_area_is_outside_the_form_that_is_redrawn() -> None:
    # render() replaces the form's innerHTML on load and after every save. The
    # Theme controls are not registry fields and are not rebuilt by it, so
    # inside the form they would be wiped by the first save and not come back
    # until a reload — which reads as the page having lost them.
    page = _web("settings.html")
    assert page.index('id="lookSec"') > page.index("</form>"), (
        "the Theme area sits inside the form render() rebuilds, so a save deletes it"
    )


def test_a_marked_split_segment_is_actually_drawn() -> None:
    # The mark on a split bar was applied as class="part" while the only rule for
    # `part` was `td.part`, so every segment mark rendered as nothing — and the
    # DOM looked right, which is how it survived a check that read attributes
    # rather than pixels. The minimum width matters as much as the outline: the
    # segment #31 exists for is the 0.0 kWh band, which has no width to draw in.
    page = _web("costs.html")
    assert "td.part{" in page, "the table's own mark is gone"
    rule = re.search(r"\.splitbar span\.part\{([^}]*)\}", page)
    assert rule is not None, "split segments take class=part but nothing styles them"
    assert "min-width" in rule.group(1), (
        "a marked segment with a zero share has no width, so the mark cannot render"
    )
    drawn = re.search(r"\.splitbar span\.part::after\{([^}]*)\}", page, re.S)
    assert drawn is not None, "the marked segment has width but nothing is drawn in it"
    # --warn is the marker colour everywhere else on this page and is wrong here:
    # measured against the segment fills it sits on it reaches 1.0:1 in light mode.
    # --ink inverts with the theme, which is what holds the mark above 3:1 on both.
    assert "var(--ink)" in drawn.group(1), "the segment mark must use the theme's ink"
    assert "var(--warn)" not in drawn.group(1), (
        "--warn on the split fills measures 1.0:1 in light mode — invisible, not subtle"
    )


def test_the_shortfall_mark_claims_no_unplaced_amount() -> None:
    # A band is named whenever its window was partly unmeasured, and that happens
    # with nothing unplaced at all — a counter that never reported over the window
    # leaves `unattributed_kwh` at zero while every band stays a candidate. Wording
    # that says "some of the unplaced energy" asserts a quantity that need not
    # exist, which is the same class of error as presenting a partial as whole.
    page = _web("costs.html")
    assert "unplaced energy" not in page, (
        "the mark claims unplaced energy exists; a named band need not have any"
    )
    assert "was not measured" in page, "the mark no longer says why the figure is qualified"


def test_pvlib_is_a_development_dependency_only() -> None:
    # The physics ships as our own arithmetic; pvlib is the referee that keeps
    # it honest in tests. If it ever reached the runtime list the collector
    # would inherit numpy and pandas on a Raspberry Pi that has no use for
    # them, which is the trade the transcription exists to avoid.
    import tomllib

    config = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    runtime = " ".join(config["project"]["dependencies"])
    assert "pvlib" not in runtime
    assert "numpy" not in runtime and "pandas" not in runtime
    assert "pvlib" in " ".join(config["dependency-groups"]["dev"])


def test_no_shipped_module_imports_pvlib() -> None:
    # The dependency list is only half the guarantee: an import in src/ would
    # break the collector at runtime however the manifest reads. Imports, not
    # mentions — solar.py's docstring names pvlib to say what holds it honest,
    # and a check that could not tell prose from an import would forbid the
    # explanation along with the dependency.
    importing = re.compile(r"^\s*(?:import\s+pvlib|from\s+pvlib\b)", re.MULTILINE)
    source = Path(__file__).resolve().parents[1] / "src"
    offenders = sorted(p.name for p in source.rglob("*.py") if importing.search(p.read_text()))
    assert offenders == [], f"pvlib reached the shipped code: {offenders}"
