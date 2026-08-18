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

The second rule here is opacity, and it is the other half of that defect. A halo
that lets the band through is not a halo, and a translucent ``rgba()`` is
something a canvas parses perfectly well — so the parseability rule above would
never notice it.

Both guards take their declarations as an argument, so each is driven twice: once
over the real sheets, and once over synthetic declarations that state the
outcome directly. A guard only ever run against a tree that already passes it
cannot show that it would catch anything.
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

# (token, value, filename), which is what both guards below are handed.
Declaration = tuple[str, str, str]

_INK_READ = re.compile(r"""ink\(\s*['"](--[a-z0-9-]+)['"]""")
_DECLARATION = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;{}]*);")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_HEX = re.compile(r"^#([0-9a-f]{3,8})$")
_FUNCTIONAL = re.compile(r"^[a-z-]+\((.*)\)$", re.DOTALL)


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


def _declarations() -> list[Declaration]:
    """Every custom-property declaration across the web assets."""
    out: list[Declaration] = []
    for path in _sources():
        text = _without_comments(path.read_text(encoding="utf-8"))
        for name, value in _DECLARATION.findall(text):
            out.append((name, " ".join(value.split()), path.name))
    return out


def _alpha_component(text: str) -> str | None:
    """The alpha slot of a functional colour, or None where it states none.

    The modern syntaxes put it after a slash and the legacy ones make it a
    fourth comma-separated component, which ``rgb()`` and ``hsl()`` now accept
    as well as ``rgba()`` and ``hsla()`` — reading only the named functions is
    how a translucent ``rgb(8, 13, 26, 0.5)`` gets through.
    """
    call = _FUNCTIONAL.match(text)
    if call is None:
        return None
    args = call.group(1)
    if "/" in args:
        return args.split("/", 1)[1].strip()
    parts = [part.strip() for part in args.split(",")]
    return parts[3] if len(parts) == 4 else None


def _stated_opacity(value: str) -> float:
    """How opaque a colour says it is, as a fraction, where 1.0 states nothing.

    An alpha slot that is present but unreadable comes back as 0.0 rather than
    as 1.0, so the guards below fail on it and somebody looks. A halo whose
    transparency nobody can read is not a thing to wave through on the grounds
    that this parser is short.
    """
    text = " ".join(value.split()).lower()
    if text == "transparent":
        return 0.0

    digits = _HEX.match(text)
    if digits is not None:
        found = digits.group(1)
        if len(found) == 4:
            return int(found[3] * 2, 16) / 255
        if len(found) == 8:
            return int(found[6:], 16) / 255
        return 1.0

    alpha = _alpha_component(text)
    if alpha is None:
        # A keyword such as ``white``, or a colour stating no transparency.
        return 1.0
    try:
        return float(alpha[:-1]) / 100 if alpha.endswith("%") else float(alpha)
    except ValueError:
        return 0.0


def translucent_offences(declarations: list[Declaration]) -> list[str]:
    """Every declaration of an opaque-only token that states a transparency."""
    return [
        f"{name} is declared in {where} as {value!r}, which is "
        f"{_stated_opacity(value) * 100:.0f}% opaque"
        for name, value, where in declarations
        if name in MUST_BE_OPAQUE and _stated_opacity(value) < 1
    ]


def unparseable_offences(declarations: list[Declaration], read: dict[str, list[str]]) -> list[str]:
    """Every declaration a canvas cannot parse, of a token a canvas reads."""
    return [
        f"{name} is declared in {where} as {value!r}, and {', '.join(read[name])} "
        f"hands it to a canvas"
        for name, value, where in declarations
        if name in read and any(bad in value for bad in UNPARSEABLE)
    ]


def test_no_token_a_canvas_reads_is_declared_with_a_function_it_cannot_parse() -> None:
    assert not unparseable_offences(_declarations(), _tokens_read_into_a_canvas())


def test_the_halo_is_opaque_in_every_theme_that_states_it() -> None:
    """--tip is 88% opaque by design, which mixed 12% of the band into the text."""
    assert not translucent_offences(_declarations())


def test_the_opacity_guard_catches_every_way_a_halo_can_be_translucent() -> None:
    """Driven over declarations written to be caught, in each syntax CSS allows.

    The comma form is here because ``rgb()`` takes a fourth component now, so a
    guard that watched only for the ``rgba()`` spelling would miss it; the
    keyword because ``transparent`` names no channels at all.
    """
    for value in (
        "rgba(255, 255, 255, .5)",
        "rgb(8, 13, 26, 0.5)",
        "hsla(0, 0%, 100%, 0.5)",
        "rgb(8 13 26 / 0.88)",
        "oklch(16% 0.03 268 / 0.88)",
        "oklch(16% 0.03 268 / 50%)",
        "#ffffff80",
        "#fff8",
        "transparent",
    ):
        caught = translucent_offences([("--label-halo", value, "a-sheet.css")])
        assert caught, f"{value} states a transparency and passed the guard"


def test_the_opacity_guard_lets_a_solid_colour_through() -> None:
    """The other direction, or the guard could reject everything and still pass.

    ``rgba(8, 13, 26, 1)`` and ``#ffffffff`` matter most: both spell out an
    alpha and both are completely opaque, so a guard that read the syntax
    instead of the value would refuse two colours that are perfectly correct.
    """
    for value in (
        "#fff",
        "#060812",
        "#ffffffff",
        "rgb(8, 13, 26)",
        "rgb(255 255 255)",
        "rgba(8, 13, 26, 1)",
        "oklch(16% 0.03 268)",
        "white",
    ):
        assert not translucent_offences([("--label-halo", value, "a-sheet.css")]), (
            f"{value} is opaque and was refused"
        )


def test_the_opacity_guard_ignores_a_token_that_is_allowed_its_transparency() -> None:
    """--grid-line is a whisper on purpose. Only the named tokens are held here."""
    assert not translucent_offences([("--grid-line", "rgba(214, 218, 232, 0.055)", "a-sheet.css")])


def test_the_parseability_guard_catches_a_function_a_canvas_drops() -> None:
    """Both halves matter: the declaration is unparseable *and* a canvas reads it."""
    read = {"--label-halo": ["graphs.html"]}
    unread = {"--something-else": ["graphs.html"]}

    for value in ("light-dark(white, black)", "color-mix(in oklab, white, black)"):
        declared = [("--label-halo", value, "a-sheet.css")]
        assert unparseable_offences(declared, read), f"{value} passed the guard"
        assert not unparseable_offences(declared, unread), (
            f"{value} was flagged on a token no canvas reads"
        )

    assert not unparseable_offences([("--label-halo", "rgb(8, 13, 26)", "a-sheet.css")], read)


def test_the_scans_find_the_tokens_these_rules_were_written_for() -> None:
    """Both guards pass trivially if the scans feeding them come back empty.

    So each scan is held to a known case: ``--label-halo`` is read into a canvas
    from ``graphs.html``, and the Glass sheet declares both it and ``--grid-line``
    per theme, each because the earlier form of it could not be parsed there.
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
