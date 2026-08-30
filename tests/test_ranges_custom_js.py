"""test_ranges_custom_js.py — the fifth range button hands the window to the owner.

The four presets answer "how wide"; a custom window answers "which hours", and
the two are different questions with one shared danger: a custom range that
slides with the clock is not the window anybody picked, and a picked range the
width functions cannot price is a request for a resolution no tier answers.
Three pieces carry the feature and each has its own way of going quietly wrong,
so each is sliced and run under node the way the caps and live-strip slices
are:

* ``drawRanges``/``customWindow``/``rangeSpanLabel`` (common.js, marker
  ranges-custom) — the fifth button, the fields behind it, and the two rules
  Apply enforces: a window needs both ends, and an end in the future is
  clamped to now rather than drawn past it.
* ``windowNow`` (graphs.html, marker custom-window) — the query every fetcher
  on that page shares. A preset must stay byte-identical to the old
  now-derived ask; a custom range must answer the stored stamps even as the
  clock moves under it.
* ``historyWindow`` (index.html, marker custom-window) — the same rule on the
  dashboard side of the split, where polling redraws the same window instead
  of sliding it.

The pick object carries ``seconds`` alongside its two Dates precisely so
``widthFor`` and its siblings need no custom branch; the custom-window tests
assert the stamps are fixed, and the preset branch of the shared query is
asserted byte-identical to the old now-derived ask. The width helpers are not
executed here — what the pick's ``seconds`` must not do is carry a custom
branch of its own into them. The DOM stub is a recorder, not an implementation:
the buttons are
parsed out of the markup ``drawRanges`` wrote, so a test that presses a button
presses the one the page would show. The clock is frozen and TZ is pinned to
UTC in the subprocess, because datetime-local values parse as local time and
the label names local days.

Skipped where node is not installed; loud if the extraction markers move.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"
COMMON = WEB / "common.js"
GRAPHS = WEB / "graphs.html"
INDEX = WEB / "index.html"


def _slice(path: Path, marker: str) -> str:
    text = path.read_text()
    start = text.index(f"// >>> {marker}")
    end = text.index(f"// <<< {marker}")
    assert start < end, f"{marker} markers are out of order in {path.name}"
    return text[start:end]


def _run(path: Path, marker: str, body: str) -> str:
    assert NODE is not None
    # datetime-local values and the span label are local-time things; pin the
    # zone so the frozen clock and the month names mean one thing everywhere.
    env = {**os.environ, "TZ": "UTC"}
    out = subprocess.run(
        [NODE, "-e", _slice(path, marker) + "\n" + body],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return out.stdout.strip()


def _common(body: str) -> str:
    return _run(COMMON, "ranges-custom", body)


def _graphs(body: str) -> str:
    return _run(GRAPHS, "custom-window", body)


def _index(body: str) -> str:
    return _run(INDEX, "custom-window", body)


# The stub every drawRanges test drives. The buttons come back parsed out of
# the innerHTML the function just wrote — a test that presses '7d' presses the
# button the page would show, and a markup change that drops aria-pressed or
# data-k fails here rather than passing on a second implementation of the row.
STUB = """
const esc = (s) => String(s ?? '');
const pad2 = (n) => String(n).padStart(2, '0');
const REAL_DATE = Date;
const NOW = new REAL_DATE('2026-03-09T18:00:00Z');
globalThis.Date = class extends REAL_DATE {
  constructor(...a) { if (a.length) super(...a); else super(NOW.getTime()); }
};
const fakeRangesEl = () => {
  const fields = { hidden: null };
  const from = { value: '' };
  const to = { value: '' };
  const el = {
    innerHTML: '', fields, from, to, buttons: [],
    querySelector: (sel) => {
      // The row's hidden is read off the markup the redraw just wrote —
      // "rebuilt closed" is an assertion about the markup, not a stub flag.
      if (sel === '.rngfields') {
        fields.hidden = el.innerHTML.includes('<span class="rngfields" hidden>');
        return fields;
      }
      if (sel === '[data-k="from"]') return from;
      if (sel === '[data-k="to"]') return to;
      return null;
    },
    querySelectorAll: () => {
      el.buttons = [...el.innerHTML.matchAll(/<button([^>]*)>([^<]*)</g)]
        .map((m) => ({
          dataset: { k: (m[1].match(/data-k="([^"]+)"/) || [])[1] },
          pressed: (m[1].match(/aria-pressed="([^"]*)"/) || [])[1],
          text: m[2],
          onclick: null,
        }));
      return el.buttons;
    },
  };
  return el;
};
const press = (el, k) => el.buttons.find((b) => b.dataset.k === k).onclick();
const rangeKeys = (el) => el.buttons
  .filter((b) => b.pressed !== undefined).map((b) => b.dataset.k).join(',');
const pressedKeys = (el) => el.buttons
  .filter((b) => b.pressed === 'true').map((b) => b.dataset.k).join(',');
const buttonLabel = (el, k) => el.buttons.find((b) => b.dataset.k === k).text;
const pickedCustom = (w) => ({ key: 'custom', label: w.label, seconds: w.seconds,
  start: w.start, end: w.end });
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_fifth_button_comes_last_and_only_the_current_range_is_pressed() -> None:
    # The presets keep their order and their pressed state; Custom joins the
    # row last. Pressing it opens fields rather than picking anything, so it
    # must not be pressed while a preset is still the current range.
    out = _common(
        STUB
        + "const picks = [];\n"
        + "const el = fakeRangesEl();\n"
        + "drawRanges(el, RANGES[1], (p) => picks.push(p));\n"
        + "console.log('keys:' + rangeKeys(el));\n"
        + "console.log('pressed:' + pressedKeys(el));\n"
        + "console.log('label:' + buttonLabel(el, 'custom'));\n"
        + "console.log('hidden:' + el.fields.hidden);\n"
        + "console.log('picks:' + picks.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["keys"] == "6h,24h,7d,30d,custom", "Custom joins the row last"
    assert results["pressed"] == "24h", "the current preset holds the pressed state alone"
    assert results["label"] == "Custom", "an unchosen Custom names no span"
    assert results["hidden"] == "true", "the fields wait hidden until Custom is pressed"
    assert results["picks"] == "0", "drawing picks nothing"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_fields_open_on_request_and_close_on_cancel_picking_nothing() -> None:
    # Opening fields is not picking a range: only Apply changes the current
    # range, and Cancel must leave the page exactly where it was — the preset
    # still current, nothing handed to the caller.
    out = _common(
        STUB
        + "const picks = [];\n"
        + "const el = fakeRangesEl();\n"
        + "drawRanges(el, RANGES[0], (p) => picks.push(p));\n"
        + "const states = [el.fields.hidden];\n"
        + "press(el, 'custom'); states.push(el.fields.hidden);\n"
        + "press(el, 'cancel'); states.push(el.fields.hidden);\n"
        + "console.log('states:' + states.join(','));\n"
        + "console.log('picks:' + picks.length);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["states"] == "true,false,true", "hidden, shown by Custom, hidden again by Cancel"
    assert results["picks"] == "0", "neither press changed the current range"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_apply_refuses_every_window_it_could_not_draw() -> None:
    # An empty field, a field that is not a date, a window running backwards
    # and a window that opens and closes at the same instant are all requests
    # /api/history would answer 400 for (_check_range rejects end <= start),
    # and a start at or after the clamp point is the same zero-length window
    # wearing a future end. None of them may reach the caller.
    out = _common(
        STUB
        + "const picks = [];\n"
        + "const el = fakeRangesEl();\n"
        + "drawRanges(el, RANGES[0], (p) => picks.push(p));\n"
        + "press(el, 'custom');\n"
        + "const bad = ['', '2026-03-09T10:00'];\n"
        + "const cases = [bad, ['2026-03-02T08:00', ''], ['bogus', '2026-03-09T10:00'],\n"
        + "  ['2026-03-02T08:00', 'bogus'],\n"
        + "  ['2026-03-05T00:00', '2026-03-02T00:00'],\n"
        + "  ['2026-03-05T00:00', '2026-03-05T00:00'],\n"
        + "  ['2026-03-09T18:00', '2026-03-10T10:00']];\n"
        + "for (const c of cases) {\n"
        + "  el.from.value = c[0]; el.to.value = c[1]; press(el, 'apply');\n"
        + "}\n"
        + "console.log('picks:' + picks.length);"
    )
    assert out == "picks:0", "a refused window is never handed to the caller"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_valid_apply_hands_back_the_window_it_claims_to_cover() -> None:
    # The picked object is the only copy of the window: the two Dates are what
    # both pages request, the seconds are what the width asks divide, and the
    # label is what the button wears. An end in the future is clamped to the
    # frozen now — the store cannot answer for an hour that has not happened —
    # and a same-day window names its hours because two days that are the same
    # day are not the same window.
    out = _common(
        STUB
        + "const picks = [];\n"
        + "const el = fakeRangesEl();\n"
        + "drawRanges(el, RANGES[0], (p) => picks.push(p));\n"
        + "press(el, 'custom');\n"
        + "el.from.value = '2026-03-02T08:00'; el.to.value = '2026-03-05T20:00';\n"
        + "press(el, 'apply');\n"
        + "el.from.value = '2026-03-09T08:00'; el.to.value = '2026-03-09T23:00';\n"
        + "press(el, 'apply');\n"
        + "const a = picks[0];\n"
        + "const b = picks[1];\n"
        + "console.log('key:' + a.key);\n"
        + "console.log('start:' + a.start.toISOString());\n"
        + "console.log('end:' + a.end.toISOString());\n"
        + "console.log('seconds:' + a.seconds);\n"
        + "console.log('label:' + a.label);\n"
        + "console.log('dates:' + (a.start instanceof REAL_DATE && a.end instanceof REAL_DATE));\n"
        + "console.log('clamped_end:' + b.end.toISOString());\n"
        + "console.log('clamped_seconds:' + b.seconds);\n"
        + "console.log('clamped_label:' + b.label);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["key"] == "custom"
    assert results["start"] == "2026-03-02T08:00:00.000Z", (
        "a datetime-local value is the browser's local reading, serialised as the "
        "presets serialise theirs"
    )
    assert results["end"] == "2026-03-05T20:00:00.000Z"
    assert results["seconds"] == "302400", "the span is what widthFor divides by cadence"
    assert results["label"] == "Mar 2 \u2013 Mar 5"
    assert results["dates"] == "true"
    assert results["clamped_end"] == "2026-03-09T18:00:00.000Z", "the future is clamped to now"
    assert results["clamped_seconds"] == "36000"
    assert results["clamped_label"] == "Mar 9 08:00 \u2013 18:00", (
        "a window whose ends share a day names its hours or the label lies"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_span_label_names_the_days_the_window_covers() -> None:
    # The button is inside a pill of 6h/24h buttons; the label stays short
    # until the year changes under it. Dates come in as instants and go out
    # as local calendar parts, matching what the datetime-local fields showed.
    out = _common(
        STUB
        + "const REAL = REAL_DATE;\n"
        + "console.log('days:' + rangeSpanLabel("
        + "new REAL('2026-03-03T06:00:00Z'), new REAL('2026-03-09T18:00:00Z')));\n"
        + "console.log('hours:' + rangeSpanLabel("
        + "new REAL('2026-03-09T08:00:00Z'), new REAL('2026-03-09T20:30:00Z')));\n"
        + "console.log('years:' + rangeSpanLabel("
        + "new REAL('2025-12-31T23:00:00Z'), new REAL('2026-01-02T01:00:00Z')));"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["days"] == "Mar 3 \u2013 Mar 9"
    assert results["hours"] == "Mar 9 08:00 \u2013 20:30"
    assert results["years"] == "Dec 31 2025 \u2013 Jan 2 2026", (
        "two Mays in different years are not the same day"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_chosen_window_presses_the_button_and_wears_its_span() -> None:
    # The redraw after Apply — the caller setting range to the picked object
    # and calling drawRanges again — must show the custom range as current:
    # Custom pressed with the span for a label, no preset pressed, and the
    # fields closed again, because Apply answered the question the fields asked.
    out = _common(
        STUB
        + "const w = customWindow('2026-03-02T08:00', '2026-03-05T20:00', NOW);\n"
        + "const el = fakeRangesEl();\n"
        + "drawRanges(el, pickedCustom(w), () => {});\n"
        + "console.log('pressed:' + pressedKeys(el));\n"
        + "console.log('label:' + buttonLabel(el, 'custom'));\n"
        + "console.log('hidden:' + el.fields.hidden);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n") if ":" in ln)
    assert results["pressed"] == "custom"
    assert results["label"] == "Mar 2 \u2013 Mar 5", "the button says which window is current"
    assert results["hidden"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_preset_after_a_custom_restores_its_own_pressed_state() -> None:
    # The redraw is the reset: picking a preset hands the caller a RANGES
    # entry, and the row rebuilt around it shows no custom press, a plain
    # Custom label and closed fields. The custom object leaving circulation
    # takes its span with it.
    out = _common(
        STUB
        + "const picks = [];\n"
        + "const w = customWindow('2026-03-02T08:00', '2026-03-05T20:00', NOW);\n"
        + "const el = fakeRangesEl();\n"
        + "drawRanges(el, pickedCustom(w), (p) => picks.push(p));\n"
        + "press(el, '7d');\n"
        + "console.log('picked:' + picks[0].key + ':' + picks[0].seconds);\n"
        + "const again = fakeRangesEl();\n"
        + "drawRanges(again, picks[0], () => {});\n"
        + "console.log('pressed:' + pressedKeys(again));\n"
        + "console.log('label:' + buttonLabel(again, 'custom'));\n"
        + "console.log('hidden:' + again.fields.hidden);"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["picked"] == "7d:604800", "a preset comes back as the RANGES entry itself"
    assert results["pressed"] == "7d"
    assert results["label"] == "Custom"
    assert results["hidden"] == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_shared_query_stays_now_based_for_presets_and_frozen_for_custom() -> None:
    # The Graphs page builds every fetcher's query from one function, so the
    # preset branch must stay byte-identical to the old now-derived ask — a
    # drift here moves four families' windows — while a custom range answers
    # the stored stamps on every call, whatever the clock has since done.
    out = _graphs(
        "const REAL = Date;\n"
        "let NOW = new REAL('2026-03-09T18:00:00Z');\n"
        "globalThis.Date = class extends REAL {\n"
        "  constructor(...a) { if (a.length) super(...a); else super(NOW.getTime()); }\n"
        "};\n"
        "let range = { key: '6h', seconds: 6 * 3600 };\n"
        "console.log('preset:' + windowNow());\n"
        "range = { key: 'custom', start: new REAL('2026-02-01T00:00:00Z'),\n"
        "  end: new REAL('2026-02-08T12:34:56Z') };\n"
        "console.log('custom:' + windowNow());\n"
        "NOW = new REAL('2026-03-10T09:00:00Z');\n"
        "console.log('later:' + windowNow());"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["preset"] == "start=2026-03-09T12:00:00.000Z&end=2026-03-09T18:00:00.000Z"
    assert results["custom"] == "start=2026-02-01T00:00:00.000Z&end=2026-02-08T12:34:56.000Z"
    assert results["custom"] == results["later"], (
        "a custom window is the hours that were picked, not the hours since"
    )


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_dashboard_poll_rereads_a_custom_window_instead_of_sliding_it() -> None:
    # The dashboard polls, and a preset's window slides with every poll — that
    # is what 24h means. A custom range is an answer to a different question:
    # a later poll must re-read the same hours, or the charts drift out from
    # under the label on the button. The window comes out as two Dates so the
    # /api/history ask and the /api/bands ask keep sharing the pair they
    # already share.
    out = _index(
        "const preset = historyWindow({ key: '24h', seconds: 24 * 3600 },\n"
        "  new Date('2026-03-09T18:00:00Z'));\n"
        "console.log('preset:' + preset.start.toISOString() + '|' + preset.end.toISOString());\n"
        "const custom = historyWindow({ key: 'custom',\n"
        "  start: new Date('2026-02-01T00:00:00Z'), end: new Date('2026-02-08T12:34:56Z') },\n"
        "  new Date('2099-01-01T00:00:00Z'));\n"
        "console.log('custom:' + custom.start.toISOString() + '|' + custom.end.toISOString());"
    )
    results = dict(ln.split(":", 1) for ln in out.split("\n"))
    assert results["preset"] == "2026-03-08T18:00:00.000Z|2026-03-09T18:00:00.000Z", (
        "the preset still measures back from the moment of asking"
    )
    assert results["custom"] == "2026-02-01T00:00:00.000Z|2026-02-08T12:34:56.000Z", (
        "a poll a century later asks the same two instants"
    )
