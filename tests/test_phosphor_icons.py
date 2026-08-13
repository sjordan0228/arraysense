"""test_phosphor_icons.py — every icon the pages draw exists in the sprite.

An icon is a <use href="#ph-..."> into the Phosphor sprite that common.js
fetches and injects, and a typo'd id is the one failure that ships invisible:
the page draws nothing, nothing errors, and no reader can point at what is
missing. These hold every reference — the literal <use> tags, the ids the nav
interpolates from its table and the stale banner's marks — against the symbols
phosphor.svg actually defines, and pin the two deliberate exclusions: Inverter
in the nav, and the pause mark on the banner's one non-fault tone. The Inverter
view's eight headings each keep the icon that reinforces their label, pinned
heading by heading so one losing its mark fails loudly.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"

# A literal <use href="#ph-..."> in any page or script.
_USE_HREF = re.compile(r'<use href="#(ph-[\w-]+)"')
# A sprite id quoted in code — the nav table's icon fields and the stale
# banner's marks, both of which become <use>s only at render time.
_QUOTED_ID = re.compile(r"'((?:ph-)[\w-]+)'")
# Every symbol the sprite actually defines.
_SYMBOL_ID = re.compile(r'<symbol id="(ph-[\w-]+)"')

_WEB_FILES = (
    "common.js",
    "index.html",
    "graphs.html",
    "history.html",
    "costs.html",
    "efficiency.html",
    "settings.html",
)


def _symbols() -> set[str]:
    return set(_SYMBOL_ID.findall((WEB / "phosphor.svg").read_text()))


def _references() -> set[str]:
    ids: set[str] = set()
    for name in _WEB_FILES:
        text = (WEB / name).read_text()
        ids.update(_USE_HREF.findall(text))
        ids.update(_QUOTED_ID.findall(text))
    return ids


def test_every_icon_reference_names_a_symbol_the_sprite_defines() -> None:
    missing = _references() - _symbols()
    assert not missing, (
        "these icons are referenced but phosphor.svg has no such symbol: "
        + ", ".join(sorted(missing))
    )


def test_the_nav_carries_seven_icons_and_inverter_is_the_one_text_only_entry() -> None:
    # Seven of the eight entries name a symbol; Inverter deliberately does not,
    # because no glyph in the sprite names the box in the middle. That exclusion
    # is the design decision being pinned, not the count itself.
    common = (WEB / "common.js").read_text()
    nav = re.search(r"const NAV = \[(.*?)\];", common, re.S)
    assert nav is not None, "the NAV table is no longer one array"
    body = nav.group(1)
    assert body.count("label:") == 8, "the nav should hold its eight entries"
    iconed = re.findall(r"icon:\s*'(ph-[\w-]+)'", body)
    assert len(iconed) == 7, f"expected seven iconed entries, got {iconed}"
    inverter = re.search(r"\{ key:\s*'inverter'[^}]*\}", body)
    assert inverter is not None, "the Inverter entry is missing"
    assert "icon:" not in inverter.group(0), "Inverter should stay text-only"


def test_the_four_summary_cards_each_lead_with_their_icon() -> None:
    # Each card's icon sits directly before its label inside the label line, so
    # the pair has to read as one mark + one word and not as an icon adrift.
    index = (WEB / "index.html").read_text()
    for icon, label in (
        ("ph-sun", "Producing now"),
        ("ph-house", "Home"),
        ("ph-battery-charging", "Battery"),
        ("ph-plug", "Grid"),
    ):
        assert re.search(rf'<use href="#{icon}"></use></svg>{label}', index), (
            f"the {label!r} card does not lead with {icon}"
        )


def test_each_inverter_heading_keeps_its_icon() -> None:
    # The eight Inverter panels are built by functions whose <h3> text is
    # literal in the source, so the icon has to be literal too — written into
    # the heading rather than interpolated from a table a future edit could
    # empty. Each heading keeps the text that names the panel and the icon that
    # reinforces it, so a panel that loses its mark fails here, as does one
    # whose icon is swapped for a different glyph or loses its aria-hidden.
    #
    # What this cannot catch is anything visual: an icon that exists but is
    # invisible because a CSS rule never loads, a heading that reflows its
    # panel, or a panel that stops appearing because its gating changed. The
    # sprite-existence half is covered here and by the reference test above,
    # which already sweeps every literal <use> on the page.
    index = (WEB / "index.html").read_text()
    for icon, label in (
        ("ph-solar-panel", "PV Strings"),
        ("ph-plugs", "Backup legs"),
        ("ph-thermometer-simple", "Inverter Temp"),
        ("ph-trend-down", "Cable drop"),
        ("ph-lightning", "Energy"),
        ("ph-circuitry", "BMS"),
        ("ph-plug", "Grid &amp; AC"),
        ("ph-pulse", "Status"),
    ):
        assert re.search(
            rf'<h3><svg class="ic" aria-hidden="true"><use href="#{icon}"></use></svg>{label}</h3>',
            index,
        ), f"the {label!r} heading does not lead with {icon}"


def test_each_stale_tone_is_told_apart_by_shape_and_not_only_by_colour() -> None:
    # Three tones: a deliberate yield, a fault the collector is still working
    # through, and a service that has stopped. They differ in colour, but colour
    # is not what distinguishes them — the owner of the reference installation is
    # colour blind, and severity resting on hue alone is the failure this project
    # spends most of its care avoiding. So each tone gets its own shape, and this
    # test fails the day two of them share one.
    common = (WEB / "common.js").read_text()
    marks = re.search(r"const STALE_MARKS = \{(.*?)\};", common, re.S)
    assert marks is not None, "the stale marks are no longer one table"
    shapes = re.findall(r"(\w+):\s*'([^']+)'", marks.group(1))
    assert len(shapes) == 3, f"expected three tones, found {shapes}"
    used = [shape for _, shape in shapes]
    assert len(set(used)) == 3, f"two tones share a shape and differ only by colour: {shapes}"
    icons = [shape for shape in used if shape.startswith("ph-")]
    for icon in icons:
        assert f'id="{icon}"' in (WEB / "phosphor.svg").read_text(), icon
