"""test_appearance_cookie_js.py — the look a browser holds, read from a cookie.

The look moved from localStorage to a cookie so the page route can read it and
send the glass ``<link>`` only to the browsers that will paint with it. Three
pieces carry the move and each has its own way of going quietly wrong, so they
are sliced out of common.js between the appearance-cookie markers and run under
node the way the custom-range slice is:

* ``readCookie`` — ``document.cookie`` is one flat string of pairs. Matching a
  regex against the whole string instead of pair by pair finds the wrong pair
  the day another cookie's name contains this one, so the parse splits on the
  separators and cuts at the first ``=``.
* ``writeCookie`` — the one place the cookie string is built. Path, Max-Age and
  SameSite are what make the choice reach every route, outlive a session, and
  stay off cross-site requests, and a second writer would drift from them.
* ``appearanceChoice`` — the cookie first, the vetted legacy localStorage
  entry second with its copy into the cookie as the one-time migration, and
  the default only when neither holds a look. A cookie the look table does not
  name is answered the default, the same answer the page route gives it, and
  it does not fall through to the legacy entry — two stores holding two looks
  for one browser is the drift this moved away from.

The stub records every ``document.cookie`` write verbatim, so a test asserts
the exact cookie string rather than a re-assembly of it, and localStorage can
be made to refuse the way private browsing does. Skipped where node is not
installed; loud if the extraction markers move.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
COMMON = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "common.js"

STUB = """
const writes = [];
globalThis.document = {
  _raw: '',
  get cookie() { return this._raw; },
  set cookie(raw) { writes.push(raw); this._raw = raw; },
};
globalThis.localStorage = {
  _items: new Map(),
  put(k, v) { this._items.set(k, v); },
  getItem(k) { return this._items.has(k) ? this._items.get(k) : null; },
};
const seedCookie = (pairs) => { document._raw = pairs.join('; '); };
const denyStorage = () => {
  globalThis.localStorage = { getItem() { throw new Error('private browsing'); } };
};
"""


def _run(body: str) -> str:
    assert NODE is not None
    text = COMMON.read_text(encoding="utf-8")
    start = text.index("// >>> appearance-cookie")
    end = text.index("// <<< appearance-cookie")
    assert start < end, "appearance-cookie markers are out of order in common.js"
    # check=False with stderr in the assertion, because a throw inside the node
    # body surfaces as a CalledProcessError that hides the real line otherwise.
    done = subprocess.run(
        [NODE, "-e", text[start:end] + "\n" + body],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, f"node threw: {done.stderr}"
    return done.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_read_cookie_finds_the_named_pair_in_a_shared_string() -> None:
    # document.cookie holds every cookie the host gets, semicolon-paired and in
    # no order the app controls. The parse takes the value of the pair whose
    # name matches exactly — not of a pair whose name contains it — and answers
    # null, not the empty string's look, when no pair matches or none at all
    # was sent.
    out = _run(
        STUB
        + "seedCookie(['theme=light', 'arraysense-appearance=classic', 'x=1']);\n"
        + "console.log('mine:' + readCookie(APPEARANCE_KEY));\n"
        + "console.log('theirs:' + readCookie('theme'));\n"
        + "console.log('absent:' + readCookie('arraysense-nothing'));\n"
        + "document._raw = '';\n"
        + "console.log('empty:' + readCookie(APPEARANCE_KEY));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["mine"] == "classic", "the pair whose name matches exactly"
    assert results["theirs"] == "light", "a neighbouring cookie's value is not ours"
    assert results["absent"] == "null", "no pair of that name is no value at all"
    assert results["empty"] == "null", "no cookies at all is no value either"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_cookie_value_the_look_table_does_not_name_answers_the_default() -> None:
    # hasOwn rather than a prototype-chain lookup: "toString" is a fine cookie
    # value and not a look. And a present-but-unknown cookie answers the
    # default without consulting the legacy store — the page route reads only
    # the cookie, so falling through would put the browser and the served page
    # into different looks for the whole navigation.
    out = _run(
        STUB
        + "seedCookie(['arraysense-appearance=toString']);\n"
        + "localStorage.put(APPEARANCE_KEY, 'classic');\n"
        + "console.log('choice:' + appearanceChoice());\n"
        + "console.log('writes:' + writes.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["choice"] == "glass", "an unknown cookie value is no look"
    assert results["writes"] == "0", "an unknown cookie is not rewritten and not migrated"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_legacy_local_choice_moves_into_the_cookie_once() -> None:
    # The migration is the reason the read still falls through to localStorage:
    # a browser that chose Classic in the old store still wants Classic, and
    # copying the value into the cookie is what lets the next navigation arrive
    # with it so the server can render the page without the link. One write,
    # and the second read finds the cookie and writes nothing more; the old
    # entry stays where it is because nothing reads it a second time.
    out = _run(
        STUB
        + "localStorage.put(APPEARANCE_KEY, 'classic');\n"
        + "console.log('first:' + appearanceChoice());\n"
        + "console.log('writes:' + writes.length);\n"
        + "console.log('string:' + (writes[0] ===\n"
        + "  'arraysense-appearance=classic; Path=/; Max-Age=31536000; SameSite=Lax'));\n"
        + "console.log('again:' + appearanceChoice());\n"
        + "console.log('still:' + writes.length);\n"
        + "console.log('legacy:' + localStorage.getItem(APPEARANCE_KEY));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["first"] == "classic", "the legacy choice still chooses"
    assert results["writes"] == "1", "the migration writes once"
    assert results["string"] == "true", (
        "the exact string: a year of Max-Age, Path for every route, "
        "SameSite=Lax, and no Secure the plain-HTTP LAN cannot serve"
    )
    assert results["again"] == "classic", "the second read answers from the cookie"
    assert results["still"] == "1", "the migration does not repeat"
    assert results["legacy"] == "classic", "the old entry is left where it is"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_junk_legacy_entry_is_never_migrated() -> None:
    # The same hasOwn check vets the legacy store as it vets the cookie: a
    # value that is not a look is not copied forward, because the cookie would
    # then outlive the stray it came from and keep answering the default for a
    # year.
    out = _run(
        STUB
        + "localStorage.put(APPEARANCE_KEY, 'toString');\n"
        + "console.log('choice:' + appearanceChoice());\n"
        + "console.log('writes:' + writes.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["choice"] == "glass", "an unknown stored value is no look"
    assert results["writes"] == "0", "nothing unvetted reaches the cookie"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_cookie_needs_no_legacy_entry_and_writes_nothing() -> None:
    # The ordinary read after the migration: the cookie answers, localStorage
    # is never opened, and no write happens — the resolver must not be writing
    # on every page load.
    out = _run(
        STUB
        + "seedCookie(['arraysense-appearance=classic']);\n"
        + "console.log('choice:' + appearanceChoice());\n"
        + "console.log('writes:' + writes.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["choice"] == "classic"
    assert results["writes"] == "0", "a read of the cookie is not a write of it"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_choice_anywhere_answers_the_default_without_writing() -> None:
    # A browser that has never chosen gets Glass, the look every page carries
    # in its markup, and nothing is persisted for a choice nobody made.
    out = _run(
        STUB
        + "console.log('choice:' + appearanceChoice());\n"
        + "console.log('writes:' + writes.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["choice"] == "glass"
    assert results["writes"] == "0", "a browser that never chose keeps choosing nothing"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_refused_localstorage_still_answers_the_default() -> None:
    # Private browsing refuses localStorage outright, so the fall-through is
    # guarded no wider than the read itself: the default answers while a
    # programming error below the guard still reaches the console.
    out = _run(
        STUB
        + "denyStorage();\n"
        + "console.log('choice:' + appearanceChoice());\n"
        + "console.log('writes:' + writes.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["choice"] == "glass", "the refusal is answered, not thrown"
    assert results["writes"] == "0", "there is no store to migrate into"
