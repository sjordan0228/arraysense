"""test_settings_heatmap_js.py — the rate-schedule heatmap's three states stay distinct.

The heatmap on the Rate bands tab draws one of three things: the grid, the
legitimate empty state (no tariff entered), and the failed-read error. The
three must never be confused — a swallowed read that rendered as an empty grid
has happened here before — so each state is exercised against the real
functions under node, the way test_settings_tabs_js.py slices the same page.

Also under test: the 422 refusal message, which arrives as a list of objects
and must reach the error card as a sentence a person can read rather than the
literal "[object Object]".

Skipped where node is not installed; loud if the extraction markers move.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "settings.html"

_HEAT_START = "// >>> rate-schedule-heatmap"
_HEAT_END = "// <<< rate-schedule-heatmap"


def _heat_slice() -> str:
    text = PAGE.read_text()
    start = text.index(_HEAT_START)
    end = text.index(_HEAT_END)
    assert start < end, "rate-schedule-heatmap markers out of order in settings.html"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    # `document` must exist before the slice evaluates: its last line, the
    # themechange listener, runs at definition time. Everything else the
    # functions need at call time — esc, fade, $, saved, fetch — is stubbed by
    # each body before it calls them.
    preamble = "const document = { addEventListener: () => {}, querySelector: () => null };\n"
    script = preamble + _heat_slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


# The stubs the heatmap functions read at call time. Each body includes them
# (after the slice, before the call) so the test can shape `saved` and `host`.
# Values are printed through String() before console.log: node colourises a
# raw boolean on the wire, and the assertion compares plain text.
_STUBS = """
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const DASH = '—';
const fade = () => '#123456';
const currencyNow = () => '$';
const host = { innerHTML: '' };
const $ = (sel) => sel === 'heatHost' ? host : null;
const BANDS_KEY = 'tariff.bands';
const saved = { 'tariff.bands': '' };
const siteZone = async () => 'America/Chicago';
"""


def _say(*values: str) -> str:
    return f"console.log([{', '.join(values)}].map(String).join(' '));"


# --- the three states, drawn directly --------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_empty_state_says_no_tariff_was_entered() -> None:
    # No tariff is one answer: the grid is legitimately empty, and the card
    # says so without a hint that anything went wrong. It must not look like
    # the error state below it.
    body = (
        _STUBS
        + """
    heatDrawEmpty();
    """
        + _say(
            "host.innerHTML.includes('No tariff entered')",
            "host.innerHTML.includes('role=\"alert\"')",
        )
    )
    assert _run(body) == "true false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_empty_state_distinguishes_a_stored_but_unreadable_tariff() -> None:
    # A tariff the service cannot read is a different answer from no tariff at
    # all, and the card has to say which: the reader who typed it must be told
    # it is not understood, not that they never entered one.
    body = (
        _STUBS
        + """
    saved['tariff.bands'] = 'Peak | 0.21 | 15:00-20:00';
    heatDrawEmpty();
    """
        + _say(
            "host.innerHTML.includes('The stored tariff cannot be read.')",
            "host.innerHTML.includes('No tariff entered')",
        )
    )
    assert _run(body) == "true false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_error_state_is_not_the_empty_state() -> None:
    # A failed read is its own state, visibly: the grid is missing because the
    # service did not answer, not because nothing is priced. If heatDrawError
    # regressed into rendering the empty card, this test fails.
    body = (
        _STUBS
        + """
    heatDrawError(new Error('the service answered 503'));
    """
        + _say(
            "host.innerHTML.includes('role=\"alert\"')",
            "host.innerHTML.includes('The rate schedule could not be read.')",
            "host.innerHTML.includes('The service answered 503')",
            "host.innerHTML.includes('No tariff entered')",
        )
    )
    assert _run(body) == "true true true false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_drawn_state_renders_a_grid_and_a_legend() -> None:
    # The success state: one cell per hour of each month, coloured by band,
    # with a legend naming the hues. Setting heatCells/heatHues and drawing is
    # the fast path; the grid must be a table carrying the band's name.
    body = (
        _STUBS
        + """
    heatCells = Array.from({ length: 12 }, () => Array.from({ length: 24 },
      (_, h) => (h >= 9 && h < 17)
        ? { band: 'Peak', price: 0.21 } : { band: 'Off-peak', price: 0.08 }));
    heatHues = { 'Peak': { hue: '--pv', alpha: 1 }, 'Off-peak': { hue: '--load', alpha: 1 } };
    heatDraw();
    """
        + _say(
            "host.innerHTML.includes('class=\"heatmap\"')",
            "host.innerHTML.includes('Peak')",
            "host.innerHTML.includes('Off-peak')",
        )
    )
    assert _run(body) == "true true true"


# --- the routes into those states ------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_mount_heatmap_reads_the_empty_state_when_the_service_says_no_tariff() -> None:
    # The whole-grid path. Every month answers configured:false, which is the
    # service saying there is no tariff at all — so the card is the empty one,
    # not an error and not a grid.
    body = (
        _STUBS
        + """
    globalThis.fetch = async () => ({ ok: true, json: async () =>
      ({ configured: false, windows: [] }) });
    (async () => {
      await mountHeatmap();
      """
        + _say(
            "host.innerHTML.includes('No tariff entered')",
            "host.innerHTML.includes('role=\"alert\"')",
        )
        + """
    })();
    """
    )
    assert _run(body) == "true false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_mount_heatmap_reads_the_error_state_when_every_month_fails() -> None:
    # The read-failed path. No month answers, so the card is the error one and
    # the reader is pointed at the service rather than at the tariff.
    body = (
        _STUBS
        + """
    globalThis.fetch = async () => { throw new Error('unreachable'); };
    (async () => {
      await mountHeatmap();
      """
        + _say(
            "host.innerHTML.includes('role=\"alert\"')",
            "host.innerHTML.includes('could not be read')",
            "host.innerHTML.includes('No tariff entered')",
        )
        + """
    })();
    """
    )
    assert _run(body) == "true true false"


# --- the 422 refusal is rendered readably ----------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_422_detail_arriving_as_objects_reads_as_a_sentence() -> None:
    # FastAPI answers a bad request with detail as a list of {loc, msg, type}
    # objects. Wrapped in a new Error, that array renders as "[object Object]"
    # — so heatMonth has to pull the messages out before throwing. A request
    # the service refuses must reach the reader as the reason, not as a
    # rendered object literal.
    body = (
        _STUBS
        + """
    globalThis.fetch = async () => ({
      ok: false,
      status: 422,
      json: async () => ({ detail: [
        { loc: ['query', 'start'], msg: 'Invalid datetime format', type: 'value_error' },
        { loc: ['query', 'end'], msg: 'Invalid datetime format', type: 'value_error' },
      ] }),
    });
    (async () => {
      try {
        await heatMonth(1, 'America/Chicago');
        console.log('no-throw');
      } catch (e) {
        console.log(e.message);
      }
    })();
    """
    )
    assert _run(body) == "Invalid datetime format; Invalid datetime format"
