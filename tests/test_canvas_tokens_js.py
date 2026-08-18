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

_INK_READ = re.compile(r"""ink\(\s*['"](--[a-z0-9-]+)['"]""")
_DECLARATION = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;{}]*);")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


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
