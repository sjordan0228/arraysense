"""test_release_check_js.py — the release row says what the check knows and no more.

The About panel asks GitHub for the repository's tags and prints one answer out
of five. Four of them can be dressed up as one of the others: a fetch that
failed reads exactly like a release that matches, the API's own ordering is a
convenience rather than an answer, and a prerelease name reads like a release.
So the slice in settings.html (marker release-check) runs under node the way the
other settings-page slices do — and it runs through the real ``drawFacts`` and
the real row markup, so a test that reads a label reads the row the page would
show and a row that renamed itself fails here instead of passing on a second
rendering of the rule.

The row is also the thing an owner copies: the command is text and never a
control, because running something from the page is issue #34's work and this is
the half that only looks.

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

_RELEASE = ("// >>> release-check", "// <<< release-check")
_FACTS = ("// >>> about-facts", "// <<< about-facts")

# The words are decided in the release-check slice and pushed into the row by
# the about-facts slice, so both are run: the row shape comes from the real
# factRow and the answer comes from the real rules. Each marker pair is looked
# up on its own, so a slice that moved or vanished fails loudly here.
_SLICES = (_FACTS, _RELEASE)

_ESC = """const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));"""

# The stubs drawFacts runs against — the same set test_about_facts_js.py uses,
# because what is under test is the row's words and not the escaping or the DOM.
# The rows are parsed back out of the innerHTML drawFacts just wrote.
_STUBS = rf"""
{_ESC}
const els = {{}};
const $ = (id) => (els[id] ??= {{ innerHTML: '' }});
const factsMarkup = () => els['facts'].innerHTML;
const rowValue = (label) => {{
  const rows = [...factsMarkup()
    .matchAll(/<div class="row"><u>([^<]*)<\/u><b>([\s\S]*?)<\/b><\/div>/g)];
  const hit = rows.find((m) => m[1] === label);
  return hit === undefined ? 'MISSING-ROW' : hit[2];
}};
const rowLabels = () => [...factsMarkup()
  .matchAll(/<div class="row"><u>([^<]*)<\/u>/g)].map((m) => m[1]);
const says = (haystack, needle) => String(haystack.includes(needle));
"""


def _run(body: str) -> str:
    assert NODE is not None
    env = {**os.environ, "TZ": "UTC"}
    out = subprocess.run(
        [NODE, "-e", "\n".join(_slice(start, end) for start, end in _SLICES) + "\n" + body],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout.strip()


def _slice(marker_start: str, marker_end: str) -> str:
    text = PAGE.read_text()
    start = text.index(marker_start)
    end = text.index(marker_end)
    assert start < end, f"{marker_start} markers are out of order in settings.html"
    return text[start:end]


def _status(version: str | None) -> str:
    """A /api/status body as the page received it, or the null of a failed fetch."""
    return "null" if version is None else json.dumps({"version": version})


def _tags(*names: str) -> str:
    """GitHub's tags answer: an array whose objects carry the name and nothing else."""
    return json.dumps([{"name": name} for name in names])


def _release_row(status: str, tags: str) -> str:
    # caps and db are null: the capability and database rows are another file's
    # business, and null keeps them from crowding the row under test out of the
    # parse. The tags answer arrives as the fourth argument, the way the db body
    # arrives as the third.
    return _run(
        _STUBS
        + f"\ndrawFacts({status}, null, null, {tags});\n"
        + "console.log(rowValue('Latest release'));\n"
    )


_UPGRADE = "<code>sudo arraysense upgrade</code>"
_NEWER = f"available \u2014 {_UPGRADE}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_higher_tag_prints_the_command_the_owner_runs() -> None:
    # The whole point of the row: the page found something newer and hands over
    # the command rather than a hint about one.
    assert _release_row(_status("1.1.9"), _tags("v1.2.0")) == f"v1.2.0 {_NEWER}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_newest_tag_is_picked_rather_than_the_first_or_the_last() -> None:
    # The endpoint sends newest first today. A yanked or draft tag moves that
    # order without moving what the newest release is, so the newest tag has to
    # be the maximum of the list and not an element of it.
    value = _release_row(_status("1.1.9"), _tags("v1.1.8", "v1.3.0", "v1.2.0"))
    assert value == f"v1.3.0 {_NEWER}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_triples_compare_as_numbers_not_as_text() -> None:
    # The case every string comparison gets wrong: 1.10 sorts below 1.9 by
    # characters, and a page that compared text would tell an owner on 1.9.9
    # that their install is newer than a release a whole minor ahead of it.
    assert _release_row(_status("1.9.9"), _tags("v1.10.0")) == f"v1.10.0 {_NEWER}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_highest_tag_matching_the_install_says_current() -> None:
    # The answer an owner wants most, and the one a check that could not run is
    # not allowed to hand out — that answer is the row two below this one.
    value = _release_row(_status("1.1.9"), _tags("v1.1.9", "v1.1.8"))
    assert value == "this install is current"
    assert "muted" not in value


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_install_ahead_of_the_tags_says_so_without_alarm() -> None:
    # A checkout ahead of the last tag is what every machine whose owner
    # upgrades by hand looks like. It is a fact about a local build, not a
    # fault, so it takes no muted span and no command.
    value = _release_row(_status("1.2.0"), _tags("v1.1.9"))
    assert value == "this install is newer than the latest tag"
    assert "muted" not in value


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_failed_check_never_reads_as_current() -> None:
    # The row this issue exists for. GitHub answers unauthenticated requests
    # sixty an hour per address, so a failed fetch is the ordinary state of a
    # busy network, and "up to date" is the one answer an owner acts on by
    # doing nothing.
    value = _release_row(_status("1.1.9"), "null")
    assert value == '<span class="muted">could not check</span>'
    assert "current" not in value


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_an_answer_with_no_usable_tag_is_a_failed_check() -> None:
    # An empty array, a body that is not the array at all, and a failed fetch
    # are one fact here: nothing was learned. None of the three may read as the
    # all-clear, and a rate limit answers with an object rather than a list.
    empty = _release_row(_status("1.1.9"), "[]")
    assert empty == '<span class="muted">could not check</span>'
    refused = _release_row(_status("1.1.9"), '{"message":"API rate limit exceeded"}')
    assert refused == '<span class="muted">could not check</span>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_names_that_are_not_releases_are_ignored_and_not_compared() -> None:
    # A tag list carries branch tags, prereleases and names in the wrong shape
    # beside its releases. Each of those takes no part in the comparison, and
    # the release among them still decides the row rather than erroring.
    value = _release_row(
        _status("1.1.9"),
        _tags("v2.0.0-rc1", "v2.0", "2.0.0", "latest", "v1.2.0"),
    )
    assert value == f"v1.2.0 {_NEWER}"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_running_version_leaves_the_row_unknown_whatever_the_tags_said() -> None:
    # With no version to compare against there is no answer to give, however
    # well the tags fetch went: the newest release in the world is not news
    # about an install whose own version is unknown.
    assert _release_row("null", _tags("v9.9.9")) == '<span class="muted">unknown</span>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_version_that_is_not_three_numbers_leaves_the_row_unknown() -> None:
    # A dev build or a fork's version string has no place in the comparison,
    # and inventing three numbers out of one would be a comparison made up here
    # rather than one the two answers support.
    suffix = _release_row(_status("2.0.0rc1"), _tags("v1.1.9"))
    assert suffix == '<span class="muted">unknown</span>'
    absent = _release_row(json.dumps({}), _tags("v1.1.9"))
    assert absent == '<span class="muted">unknown</span>'


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_tag_name_passes_through_esc_like_every_other_value() -> None:
    # The tag name comes from a third party and lands in innerHTML, so it goes
    # in through esc the way the dates and the citation do. A harmless name
    # cannot show that by an assertion on the finished row — escaping it changes
    # nothing — so esc is stubbed with a marker function that shows the tag
    # itself was passed through, and the row is read back with that marker in it.
    marker = _STUBS.replace(_ESC, "const esc = (s) => 'ESCAPED(' + String(s ?? '') + ')';")
    out = _run(
        marker
        + f"\ndrawFacts({_status('1.1.9')}, null, null, {_tags('v1.2.0')});\n"
        + "console.log(says(factsMarkup(), 'ESCAPED(v1.2.0)'));\n"
    )
    assert out == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_name_carrying_markup_never_reaches_the_page() -> None:
    # What keeps the escaping from ever being needed for a tag name: the
    # pattern admits v and three numbers and nothing else, so a name that is not
    # a release is dropped before it can be printed as anything at all.
    out = _run(
        _STUBS
        + "\ndrawFacts("
        + _status("1.1.9")
        + ", null, null, "
        + _tags("<img src=x onerror=alert(1)>", "v1.2.0")
        + ");\n"
        + "console.log(rowValue('Latest release'));\n"
        + "console.log(says(factsMarkup(), '<img'));\n"
    )
    lines = out.splitlines()
    assert lines[0] == f"v1.2.0 {_NEWER}"
    assert lines[1] == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_row_sits_with_the_facts_about_this_install() -> None:
    # The row answers "is this the version to be on", which is the question the
    # version row already answers, so it goes with those rows and not at the end
    # of the panel behind the capability rows.
    out = _run(
        _STUBS
        + f"\ndrawFacts({_status('1.1.9')}, null, null, {_tags('v1.2.0')});\n"
        + "console.log(rowLabels().join(', '));\n"
    )
    assert out == "Version, Database size, Readings from, Latest release, Inverter"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_row_offers_text_to_copy_and_nothing_that_runs() -> None:
    # #34 is the work that would let the page run something. Until it is done the
    # row prints a command an owner runs themselves, so the answer has to be
    # selectable text — and the slice that decides the words asks nothing of the
    # network and prints no control, which is the same rule said from the other
    # side: a check cannot reach out and change the machine on its own.
    value = _release_row(_status("1.1.9"), _tags("v1.2.0"))
    assert value == f"v1.2.0 {_NEWER}"
    slice_text = _slice(*_RELEASE)
    assert "<button" not in slice_text
    assert "fetch(" not in slice_text
