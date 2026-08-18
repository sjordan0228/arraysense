"""test_canvas_tokens_js.py — a token a canvas reads must be one a canvas can parse.

The pages read design tokens back out of CSS with ``getComputedStyle`` and hand
the raw text to a canvas as a fill or a stroke. A custom property is untyped, so
what comes back is the token's text rather than a resolved colour — and
``light-dark()`` and ``color-mix()`` survive that round trip as text. The canvas
rejects them silently: the assignment is dropped, the previous colour stays, and
something is painted in a colour nobody chose.

This has now shipped twice from the same misunderstanding. ``--grid-line`` drew
every gridline in the home-series blue, and the fix left a warning beside it in
``theme-glass.css``; ``--tip`` then did the same to the band names on the
Circuits stack, outlining white text in the pale grey uPlot had last set, and the
warning did not stop it. The rule was written in prose twice and broken anyway,
so it is asserted here instead.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"

# What a canvas cannot parse out of a custom property's computed text.
UNPARSEABLE = ("light-dark(", "color-mix(")

# Tokens whose whole job is to hide what is behind them. Most canvas tokens are
# deliberately translucent — --grid-line is a whisper and --zero-rule is a rule
# over a chart — so this is a named few rather than a rule for all of them.
MUST_BE_OPAQUE = ("--label-halo",)

_INK_READ = re.compile(r"""ink\(\s*['"](--[a-z0-9-]+)['"]""")
_DECLARATION = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;{}]*);")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_ALPHA_HEX = re.compile(r"^#(?:[0-9a-f]{4}|[0-9a-f]{8})$", re.IGNORECASE)


def _sources() -> list[Path]:
    """Every file that may either read a token into a canvas or define one."""
    return sorted(p for p in WEB.iterdir() if p.suffix in {".js", ".css", ".html"})


def _without_comments(text: str) -> str:
    """Comments quote tokens to explain them; a quotation is not a declaration."""
    text = _BLOCK_COMMENT.sub("", text)
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("//"))


def _tokens_read_into_a_canvas() -> dict[str, list[str]]:
    """Token name -> the files that hand it to a canvas."""
    found: dict[str, list[str]] = {}
    for path in _sources():
        text = path.read_text(encoding="utf-8")
        for name in _INK_READ.findall(text):
            found.setdefault(name, []).append(path.name)
    return found


def _declarations() -> list[tuple[str, str, str]]:
    """Every custom-property declaration, as (token, value, filename)."""
    out: list[tuple[str, str, str]] = []
    for path in _sources():
        text = _without_comments(path.read_text(encoding="utf-8"))
        for name, value in _DECLARATION.findall(text):
            out.append((name, " ".join(value.split()), path.name))
    return out


def _carries_alpha(value: str) -> bool:
    """Whether a colour states a transparency, in any of the forms CSS allows.

    The slash form covers the modern syntaxes — ``rgb(r g b / a)``,
    ``oklch(l c h / a)`` — and the named ``rgba()``/``hsla()`` functions and the
    four- and eight-digit hex forms cover the rest.
    """
    lowered = value.strip().lower()
    if _ALPHA_HEX.match(lowered):
        return True
    if lowered.startswith(("rgba(", "hsla(")):
        return True
    return "/" in lowered and "(" in lowered


def test_the_halo_is_opaque_in_every_theme_that_states_it() -> None:
    """A halo that lets the band through is not a halo; it is a tint of it.

    This was half of the original defect: --tip is the tooltip's background and
    is deliberately 88% opaque, which over a mid-blue band mixed 12% of the band
    into the outline of every letter. Nothing in the check above would notice a
    theme quietly putting that transparency back, because a translucent rgba()
    is something a canvas parses perfectly well.
    """
    offences = [
        f"{name} is declared in {where} as {value!r}, which states a transparency"
        for name, value, where in _declarations()
        if name in MUST_BE_OPAQUE and _carries_alpha(value)
    ]
    assert not offences, "\n".join(offences)


def test_no_token_a_canvas_reads_is_declared_with_a_function_it_cannot_parse() -> None:
    read = _tokens_read_into_a_canvas()
    offences = [
        f"{name} is declared in {where} as {value!r}, and {', '.join(read[name])} "
        f"hands it to a canvas"
        for name, value, where in _declarations()
        if name in read and any(bad in value for bad in UNPARSEABLE)
    ]
    assert not offences, "\n".join(offences)


def test_the_scan_finds_the_two_tokens_this_rule_was_written_for() -> None:
    """The guard above passes trivially if either half of it matches nothing.

    Both halves are held to a known case: ``--label-halo`` is read into a canvas
    and ``--grid-line`` is declared per theme in the Glass sheet, each because
    the earlier form of it could not be parsed there.
    """
    read = _tokens_read_into_a_canvas()
    assert "--label-halo" in read, "the ink() scan found no --label-halo"
    assert "graphs.html" in read["--label-halo"]

    glass = [(name, value) for name, value, where in _declarations() if where == "theme-glass.css"]
    assert glass, "the declaration scan found nothing in theme-glass.css"
    assert any(name == "--grid-line" for name, _ in glass)
    assert any(name == "--label-halo" for name, _ in glass)
    # And the scan does see light-dark() where it genuinely is, or it would have
    # nothing to catch: the Glass sheet uses it widely on tokens no canvas reads.
    assert any("light-dark(" in value for _, value in glass)


def test_the_transparency_check_knows_a_transparency_when_it_sees_one() -> None:
    """The opacity guard is only worth its line if the helper under it can fail.

    Every form CSS offers for stating an alpha, against the forms the halo is
    actually declared in today. A helper that answered False to everything would
    let the guard above pass on any value at all.
    """
    for stated in (
        "rgba(255, 255, 255, .5)",
        "hsla(0, 0%, 100%, 0.5)",
        "#ffffff80",
        "#fff8",
        "rgb(8 13 26 / 0.88)",
        "oklch(16% 0.03 268 / 0.88)",
    ):
        assert _carries_alpha(stated), f"{stated} states a transparency and was read as opaque"

    for solid in ("#fff", "#060812", "rgb(8, 13, 26)", "rgb(255 255 255)", "oklch(16% 0.03 268)"):
        assert not _carries_alpha(solid), f"{solid} is opaque and was read as transparent"
