"""test_about_facts_js.py — the About panel's database rows keep the absences apart.

The panel prints what ``/api/database`` answers: how big the file is and how
far back the readings go. Each answer has a failure shape that reads like a
real answer when the drawing code is careless — an unmeasured size printed as
"0.0 KB", an unreadable file printed as an empty database — so the slice of
``drawFacts`` in settings.html (marker about-facts) is run under node the way
the settings-page slices already are. The rows are read back out of the markup
the function actually wrote, so a test asserts what the page would show rather
than a second rendering of the rule. The clock means nothing to these rows —
the endpoint sends finished ISO dates and the page prints them as-is — but the
zone is pinned like the other node slices so a formatter that started reading
a clock would fail here rather than only on some machines.

Skipped where node is not installed; loud if the extraction markers move.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "settings.html"

_START = "// >>> about-facts"
_END = "// <<< about-facts"


def _slice() -> str:
    text = PAGE.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "about-facts markers are out of order in settings.html"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    env = {**os.environ, "TZ": "UTC"}
    out = subprocess.run(
        [NODE, "-e", _slice() + "\n" + body],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout.strip()


# The stub drawFacts runs against. ``esc`` lives in common.js and ``$`` is the
# page's own; both are stubbed rather than imported, because what is under
# test is what the rows say, not the escaping or the DOM. The rows are parsed
# back out of the innerHTML drawFacts just wrote — a test that reads a label
# reads the row the page would show, and a row renamed or dropped fails here
# rather than passing on a reimplementation in the test.
_STUBS = r"""
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// drawFacts reaches into the release-check slice below this one for the row it
// pushes between the database rows and the device rows. It is stubbed for the
// reason esc is: the slice under test here is the database rows, and the real
// releaseWords is what test_release_check_js.py runs.
const releaseWords = () => '<span class="muted">not this file</span>';
const els = {};
const $ = (id) => (els[id] ??= { innerHTML: '' });
const factsMarkup = () => els['facts'].innerHTML;
const rowValue = (label) => {
  const rows = [...factsMarkup()
    .matchAll(/<div class="row"><u>([^<]*)<\/u><b>([\s\S]*?)<\/b><\/div>/g)];
  const hit = rows.find((m) => m[1] === label);
  return hit === undefined ? 'MISSING-ROW' : hit[2];
};
"""


def _db(**fields: object) -> str:
    """A /api/database body as JSON, the shape the endpoint really sends."""
    facts: dict[str, object] = {
        "bytes": None,
        "first": None,
        "last": None,
        "readable": False,
        "reason": None,
    }
    facts.update(fields)
    return json.dumps(facts)


def _draw(db: str) -> list[str]:
    # status and caps are null: the capability rows are this slice's stub's
    # business elsewhere, and null keeps them from crowding the two rows under
    # test out of the parse.
    out = _run(
        _STUBS
        + f"\ndrawFacts(null, null, {db});\n"
        + "console.log(rowValue('Database size'));\n"
        + "console.log(rowValue('Readings from'));\n"
    )
    return out.splitlines()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_small_database_prints_in_kb() -> None:
    assert _draw(_db(bytes=32768, first="2024-11-03", last="2026-08-30", readable=True))[0] == (
        "32.0 KB"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_one_megabyte_is_where_kb_becomes_mb() -> None:
    # The threshold is where the number stops being the awkward one: a file
    # of exactly a megabyte must not print as "1024.0 KB".
    assert _draw(_db(bytes=5 * 1024 * 1024, readable=True))[0] == "5.0 MB"
    assert _draw(_db(bytes=1024 * 1024, readable=True))[0] == "1.0 MB"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_gigabyte_is_where_mb_becomes_gb() -> None:
    assert _draw(_db(bytes=3 * 1024 * 1024 * 1024, readable=True))[0] == "3.0 GB"
    assert _draw(_db(bytes=1024 * 1024 * 1024, readable=True))[0] == "1.0 GB"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unmeasured_size_never_prints_as_zero() -> None:
    # ``bytes`` is null when nothing stat'ed the file. Printing 0.0 KB for it
    # would be the empty-database lie in bytes: a measurement of nothing.
    value = _draw(_db(readable=False, reason="could not stat /nope: boom"))[0]
    assert value == '<span class="muted">could not be measured</span>'
    assert "0.0" not in value


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_empty_database_says_no_readings_yet() -> None:
    # readable with no rows is a measured absence, and it says so in its own
    # words — not muted, because nothing went wrong.
    value = _draw(_db(bytes=4096, readable=True))[1]
    assert value == "no readings yet"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_unreadable_database_says_could_not_be_read() -> None:
    # The distinction database_facts exists for: a file that could not be
    # opened is not an empty database, and the row that used to tell a real
    # owner their 668 days were gone must never render as one.
    lines = _draw(_db(readable=False, reason="could not open /nope: [errno 13]"))
    assert lines[1] == '<span class="muted">could not be read</span>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_never_say_the_same_words_for_the_two_absences() -> None:
    empty = _draw(_db(bytes=4096, readable=True))[1]
    unreadable = _draw(_db(readable=False))[1]
    assert empty != unreadable


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dates_print_exactly_as_the_endpoint_sent_them() -> None:
    # The endpoint answers in local ISO dates because the service cuts every
    # calendar day in the installation's zone. Re-cutting them here would be
    # a second calendar, which is the shape of every disagreement this
    # codebase has had, so the row prints the strings as they arrive.
    value = _draw(_db(bytes=4096, first="2024-11-03", last="2026-08-30", readable=True))[1]
    assert value == "2024-11-03 through 2026-08-30"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_failed_fetch_shows_unknown_on_both_rows() -> None:
    # A page that could not ask shows unknown, the same muted word the
    # version row already uses — never a guess, and never the endpoint's own
    # failure wording, which the page never received.
    lines = _draw("null")
    assert lines[0] == '<span class="muted">unknown</span>'
    assert lines[1] == '<span class="muted">unknown</span>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_body_missing_its_fields_is_not_drawn_as_data() -> None:
    # A 200 that is not the shape the endpoint sends — a proxy's error page,
    # a truncated reply — must fall back to the muted words, never to "NaN"
    # printed as a size or the string "undefined" dressed up as a date.
    lines = _draw("{}")
    assert lines[0] == '<span class="muted">could not be measured</span>'
    assert lines[1] == '<span class="muted">could not be read</span>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_measured_zero_is_a_measurement_and_prints() -> None:
    # A file stat'ed at zero bytes is a real measurement, not an absence —
    # the lie would be printing 0.0 KB for a file nothing stat'ed, which is
    # the distinct null case pinned above.
    assert _draw(_db(bytes=0, readable=True))[0] == "0.0 KB"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_dates_are_escaped_like_every_other_value() -> None:
    # The dates arrive as strings and go into innerHTML, so whatever the
    # service sent must reach the page as text, never as markup the row renders.
    value = _draw(_db(bytes=4096, first="<b>2024-11-03</b>", last="2026-08-30", readable=True))[1]
    assert value == "&lt;b&gt;2024-11-03&lt;/b&gt; through 2026-08-30"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_failure_reason_stays_off_the_page() -> None:
    # ``reason`` names the failing step for the CLI, where the remedy differs
    # per cause. On a settings page it would leak a filesystem path to
    # whoever holds a screenshot of it, and the row already says what the
    # reader can act on: that it could not be read.
    out = _run(
        _STUBS
        + "\ndrawFacts(null, null, "
        + _db(readable=False, reason="could not open /var/lib/arraysense/arraysense.db: [errno 13]")
        + ");\n"
        + "console.log(['/var/lib', 'errno', 'could not open']"
        + ".map((s) => String(factsMarkup().includes(s))).join(' '));\n"
    )
    assert out == "false false false"
