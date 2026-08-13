"""test_live_strip_js.py — the header's live figures, and the light they stand in.

Two things now ride on every page from common.js: four power figures in the
header, and a `data-glow` attribute that tells the Glass sheet whether the room
is warm or cool. Both are read off one /api/live response, and both have a way
of going quietly wrong that no rendering test would catch.

The figures must answer a reading nobody took with a dash. This strip is on
every page, so a zero invented here is the founding bug of this project in the
one place it would be seen most.

The light must never be a second opinion. `arraysense.mode` names the state and
the browser only picks an ambience for the name it was given — so the map from
mode to ambience is checked against the enum itself, and a mode added in Python
with nothing said about it here fails rather than silently lighting the page as
if the sun were out.

The pure halves run under node, the way the wizard and series-wash slices do.
The staleness verdict is the one thing this strip shipped broken: three-day-old
readings stayed up under a "Live power" label while the banner said the
collector had stopped. The slice therefore extends past the figures to the
verdict machinery — liveStaleFrom, paintStale, the stale branch of applyLive,
and checkStale's handling of an unreachable status — and those tests run the
real functions under node with a mocked DOM, so a regression there fails here.

The stylesheets are checked as text, since a sheet cannot be executed — and
Classic is checked for the *absence* of the treatment, because Classic keeping
the look it has is the whole of what "the glow lives in theme-glass.css" means.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from arraysense import metrics
from arraysense.mode import Mode

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"
COMMON = WEB / "common.js"
GLASS = WEB / "theme-glass.css"

_START = "// >>> live-strip"
_END = "// <<< live-strip"


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "live-strip markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", _slice() + "\n" + body], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


# --- the figures ----------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_reported_reading_is_taken_as_it_arrives() -> None:
    # Zero among them deliberately: an idle bank really does read 0 W, and that
    # is a measurement rather than an absence. The rule is that absence must not
    # look like zero, never that zero is suspect.
    body = """
    const row = { pv_total_power_w: 4210.5, battery_power_w: 0, grid_power_w: -900 };
    console.log([
      stripReading(row, 'pv_total_power_w'),
      stripReading(row, 'battery_power_w'),
      stripReading(row, 'grid_power_w'),
    ].join(' '));
    """
    assert _run(body) == "4210.5 0 -900"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_anything_that_is_not_a_number_is_absent_rather_than_zero() -> None:
    # null is what the store answers for a column nobody wrote; undefined is a
    # device that does not produce the metric at all; a boolean is a number to
    # typeof's neighbours and has to be excluded by name; NaN survives every
    # loose check there is. All four are the same answer: not measured.
    body = """
    const row = { a: null, c: 'x', d: true, e: NaN };
    // String() around each: Array.join renders null as an empty string, so a
    // function that started answering null for everything would print the same
    // blank line as one that answered correctly.
    console.log([
      stripReading(row, 'a'), stripReading(row, 'b'), stripReading(row, 'c'),
      stripReading(row, 'd'), stripReading(row, 'e'),
      stripReading(null, 'a'), stripReading(undefined, 'a'),
    ].map(String).join(' '));
    """
    assert _run(body) == "null null null null null null null"


def test_every_figure_names_a_metric_the_registry_serves() -> None:
    # The strip prints four columns straight out of /api/live. A name that has
    # drifted from the registry reads back undefined, which stripReading answers
    # as absent — so the strip would print four honest dashes for ever and
    # nothing would fail. The registry is the source of truth for what a metric
    # is called, so ask it.
    for name in re.findall(r"metric: '([a-z0-9_]+)'", _slice()):
        spec = metrics.lookup(name)
        assert spec.name not in metrics.SITE_METRICS, (
            f"{name} is a site reading written by the weather poller; the live "
            "inverter row does not carry it"
        )


def test_the_figures_take_the_validated_palette_and_add_nothing() -> None:
    # The four hues were found by searching against a protan, deutan and tritan
    # checker. A fifth introduced here would be one nobody measured, on a strip
    # that is on every page. Checked in both places a colour can reach the page:
    # the STRIP_FIGURES table names the tokens, and mountLiveStrip — where they
    # reach the DOM — is executed under node and must hand every figure a var()
    # of its own token rather than a literal, so a hard-coded hex in the builder
    # cannot pass a check that only read the table.
    tokens = re.findall(r"token: '(--[a-z-]+)'", _slice())
    assert tokens == ["--pv", "--load", "--batt", "--grid"]
    if NODE is None:
        pytest.skip("node not installed; cannot execute the strip builder")
    body = """
    const esc = (s) => String(s);
    const DASH = '—';
    const header = {
      querySelector: (sel) => sel === '.nowstrip' ? null : (sel === '.hright' ? {} : null),
      insertBefore: () => {},
    };
    const document = {
      querySelector: (sel) => sel === 'header' ? header : null,
      createElement: () => ({
        className: '', id: '', hidden: false,
        setAttribute: () => {}, innerHTML: '',
      }),
    };
    const html = mountLiveStrip().innerHTML;
    const wanted = ['--pv', '--load', '--batt', '--grid'];
    const all = wanted.every((t) => html.includes('var(' + t + ')'));
    const hex = /#[0-9a-fA-F]{3,8}/.test(html);
    console.log(String(all), String(!hex));
    """
    assert _run(body) == "true true"


# --- the light ------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_producing_is_warm_and_carrying_the_house_otherwise_is_cool() -> None:
    body = """
    const say = (m) => glowState({ mode: m, known: true });
    console.log([say('Solar'), say('Solar and battery'), say('Battery discharging'),
                 say('On grid'), say('Importing')].join(' '));
    """
    assert _run(body) == "warm warm cool cool cool"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_state_nobody_could_judge_lights_nothing() -> None:
    # Unknown is a real member of the enum and not a failure to categorise, so
    # it has to answer *no claim* rather than a default mood. Absent readings
    # and an absent response go the same way, and a mode name this build has
    # never heard of does too — inventing an ambience for it would be the page
    # asserting a state from something it could not read.
    body = """
    console.log([
      glowState({ mode: 'Unknown', known: false }),
      glowState({ mode: 'Solar', known: false }),
      glowState(null), glowState(undefined),
      glowState({ mode: 'Something later', known: true }),
    ].map(String).join(' '));
    """
    assert _run(body) == "null null null null null"


def test_every_mode_the_service_can_name_has_an_ambience() -> None:
    # The judgement is made once, in arraysense.mode. The browser's only job is
    # to pick a light for the answer it was handed — so a mode added there with
    # nothing said here would light the page as if the sun were out, whatever it
    # actually meant. UNKNOWN is the one deliberate omission and is asserted as
    # such rather than skipped.
    body = _slice()
    named = set(re.findall(r"'([^']+)': '(?:warm|cool)'", body))
    for member in Mode:
        if member is Mode.UNKNOWN:
            assert member.value not in named, (
                "UNKNOWN is not a state; giving it an ambience asserts something "
                "no reading supports"
            )
            continue
        assert member.value in named, f"arraysense.mode.Mode.{member.name} has no ambience"


# --- the staleness verdict -----------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_live_stale_from_answers_null_for_an_unreachable_status() -> None:
    # Absence of a verdict is not a verdict of fresh: a status poll that could
    # not be reached must answer null (so nothing is painted from it) rather
    # than an invented "fresh". Only a reply that names the collector stale
    # produces a stale verdict.
    body = """
    const say = (status) => {
      const v = liveStaleFrom(status);
      return v === null ? 'null' : v.short;
    };
    console.log([
      say(null), say(undefined), say({}), say({ staleness: {} }),
      say({ staleness: { stale: false, reading_at: '1970-01-01T00:00:00Z', age_seconds: 10800 } }),
      say({ staleness: { stale: true, reading_at: '1970-01-01T00:00:00Z', age_seconds: 10800 } }),
    ].join(' | '));
    """
    assert _run(body) == "null | null | null | null | null | 3 hours ago"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_paint_stale_applies_the_verdict() -> None:
    # The strip's staleness is painted from the verdict and from nothing else:
    # a stale verdict marks the strip, a null one clears it back to "Live
    # power". If paintStale stopped applying the verdict the strip would read
    # live while the banner beside it said the collector had stopped — the
    # exact defect these tests are the regression guard for.
    body = """
    const ageEl = { textContent: '', hidden: true };
    const strip = {
      classList: { toggle: (name, on) => { strip._stale = on; } },
      querySelector: (sel) => sel === '.nsage' ? ageEl : null,
      setAttribute: (k, v) => { strip[k] = v; },
      removeAttribute: (k) => { delete strip[k]; },
      title: '',
    };
    const out = [];
    paintStale(strip, { short: '3 hours ago', long: 'Last reading 14:32, 3 hours ago.' });
    out.push(
      strip._stale,
      strip['aria-label'] === 'Power readings — Last reading 14:32, 3 hours ago.',
      ageEl.textContent === 'stale · 3 hours ago',
      ageEl.hidden === false,
    );
    paintStale(strip, null);
    out.push(
      strip._stale === false,
      strip['aria-label'] === 'Live power',
      ageEl.textContent === '',
      ageEl.hidden === true,
    );
    console.log(out.map(String).join(' '));
    """
    assert _run(body) == "true true true true true true true true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_apply_live_consults_the_stale_info() -> None:
    # applyLive repaints the strip from whatever the status watch last decided.
    # A stale verdict already painted must survive a fresh live payload — the
    # readings are real, just not recent — and a clear verdict must restore the
    # live label. If applyLive stopped consulting liveStaleInfo, the strip and
    # the banner would disagree.
    body = """
    const DASH = '—';
    const kw = (v) => String(v);
    const ageEl = { textContent: '', hidden: true };
    const cells = {};
    for (const f of STRIP_FIGURES) cells[`[data-fig="${f.key}"]`] = { textContent: '' };
    const strip = {
      hidden: false,
      classList: { toggle: (name, on) => { strip._stale = on; } },
      querySelector: (sel) => sel === '.nsage' ? ageEl : (cells[sel] || null),
      setAttribute: (k, v) => { strip[k] = v; },
      removeAttribute: (k) => { delete strip[k]; },
      title: '',
    };
    const documentElement = {
      setAttribute: (k, v) => { documentElement[k] = v; },
      removeAttribute: (k) => { delete documentElement[k]; },
    };
    const document = { getElementById: (id) => id === 'nowstrip' ? strip : null, documentElement };
    const payload = {
      inverter: {
        pv_total_power_w: 4210, load_power_w: 700,
        battery_power_w: -900, grid_power_w: 1100,
      },
      mode: { mode: 'Solar', known: true },
    };
    const out = [];
    liveStaleInfo = { short: '3 hours ago', long: 'Last reading 14:32, 3 hours ago.' };
    applyLive(payload);
    out.push(strip._stale === true, String(strip['aria-label']).startsWith('Power readings'));
    liveStaleInfo = null;
    applyLive(payload);
    out.push(
      strip._stale === false,
      strip['aria-label'] === 'Live power',
      documentElement['data-glow'] === 'warm',
    );
    console.log(out.map(String).join(' '));
    """
    assert _run(body) == "true true true true true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_status_outage_does_not_un_stale_the_strip() -> None:
    # The strip is marked stale by the last reachable /api/status reply. When
    # the endpoint then becomes unreachable the outage must not clear that mark
    # — absence of a verdict is not a verdict of fresh — or the strip would
    # reclaim "Live power" in the same moment the banner said the service was
    # not answering.
    body = """
    const zoneQuery = () => '';
    const noteZone = () => {};
    const $ = () => null;
    const ageEl = { textContent: '', hidden: true };
    const strip = {
      classList: { toggle: (name, on) => { strip._stale = on; } },
      querySelector: (sel) => sel === '.nsage' ? ageEl : null,
      setAttribute: (k, v) => { strip[k] = v; },
      removeAttribute: (k) => { delete strip[k]; },
      title: '',
    };
    const documentElement = { setAttribute: () => {}, removeAttribute: () => {} };
    const document = {
      getElementById: (id) => id === 'nowstrip' ? strip : null,
      querySelector: () => null,
      body: null,
      documentElement,
    };
    globalThis.fetch = () => Promise.reject(new Error('unreachable'));
    (async () => {
      staleMisses = STALE_TOLERATED_MISSES + 1;
      liveStaleInfo = { short: '3 hours ago', long: 'Last reading 14:32, 3 hours ago.' };
      strip._stale = true;
      await checkStale();
      console.log(String(strip._stale), strip['aria-label'] === 'Live power' ? 'live' : 'stale');
    })();
    """
    assert _run(body) == "true stale"


# --- where the treatment lives -------------------------------------------


def test_classic_says_nothing_about_the_state() -> None:
    # The state is written to the document on every look, because a look can be
    # swapped mid-session and the page's mood must not have to be recomputed to
    # follow it. Classic then ignores it entirely: the base stylesheet declaring
    # a rule against data-glow is the one way Classic could stop looking like
    # Classic without anyone choosing that.
    assert "data-glow" not in COMMON.read_text().split("const BASE_CSS = `")[1].split("`;")[0]


def test_glass_crossfades_both_layers_rather_than_swapping_a_colour() -> None:
    # A background-image does not transition, so a look that changed --glow
    # would snap the corner from amber to indigo in a single frame. Both layers
    # of the change — the corner lamp and the sky behind it — have to be
    # opacity, which does.
    sheet = GLASS.read_text()
    for selector in (
        ':root[data-glow="cool"] .sunglow::before',
        ':root[data-glow="cool"] .sunglow::after',
        ':root[data-glow="cool"] body::after',
    ):
        assert selector in sheet, f"{selector} does not react to the state"
    assert sheet.count("transition: opacity var(--mood-fade) ease") == 2


def test_the_change_of_light_is_opt_out() -> None:
    # Reduced motion means no large slow crossfade across the whole viewport.
    # It does not mean no change: the state is information, and opting out of
    # motion is not opting out of being told.
    reduced = GLASS.read_text().split("@media (prefers-reduced-motion: reduce) {")[1]
    reduced = reduced.split("\n}")[0]
    for element in (".sunglow::before", ".sunglow::after", "body::after"):
        assert element in reduced, f"{element} keeps its transition under reduced motion"
