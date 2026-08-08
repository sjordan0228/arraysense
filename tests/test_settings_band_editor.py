"""Hold the settings page's band editor against the parser that judges it.

The editor in settings.html reads and writes the same pipe-separated string
parse_bands has always stored, which means the browser now carries a second
reading of the tariff grammar. That is the exact shape of this project's worst
money bug: the Costs page once held its own tariff parser beside the Python one
and the two disagreed within a day, refusing a real seasonal tariff outright and
charging a January evening at the summer peak rate.

So these tests run the page's own JavaScript — extracted from the file that is
served, not a copy of it — and check what it composes against what the service
makes of it. Nothing is asserted about the text the editor produces; only that
the tariff the service reads back out of it is the same tariff, band for band,
hour for hour, month for month, with no season and all twelve months treated as
the one thing they are.

They also pin the two edits that turned out to be able to change a bill without
looking like they had: adding a time range, which seeded the whole day and let a
band swallow every band below it while staying perfectly valid, and pressing a
month on a band with no season, which removed that month while every button on
the strip claimed the band applied in none of them.

Skipped where node is not installed. Anything the harness cannot do it fails
rather than passes quietly — an extraction that finds nothing is an error here,
because a green run that tested no JavaScript at all is the one result these
must never give.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from arraysense.settings import lookup_setting
from arraysense.tariff import EXAMPLE_BANDS, RateBand, Tariff, parse_bands

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

PAGE = Path(__file__).resolve().parents[1] / "src" / "arraysense" / "web" / "settings.html"

# Everything from the section's first line down to the first function that
# reads the document, which is the whole of the editor bar its wiring: the
# reader, the writer, the two edits that change a band, and the markup a band
# is drawn as. Sliced out of the file that is served rather than copied, so a
# change to the page is a change to what these tests run, and bounded by two
# names that would have to be deliberately moved to break it — at which point
# the extraction fails loudly rather than testing a stale copy.
SLICE_FROM = "const BANDS_KEY"
SLICE_TO = "function currencyNow("

# Tariffs the editor is expected to show as controls, and to give back meaning
# exactly what it was handed. The reference installation's own shape is here
# (seasonal before it is time-of-use), along with the ones that have historically
# been read wrongly: a range through midnight, a season through the year end, a
# band with several ranges, a band with no season at all, and the same tariff
# written "Jan-Dec" instead.
TARIFFS = [
    EXAMPLE_BANDS,
    "On-peak | 0.210321 | 15:00-20:00 | May-Oct\n"
    "Off-peak | 0.098765 | 20:00-15:00 | May-Oct\n"
    "Winter | 0.114000 | 00:00-24:00 | Nov-Apr",
    "Peak | 0.40 | 06:00-09:00, 17:00-20:00; Shoulder | 0.20 | 09:00-17:00; "
    "Night | 0.08 | 20:00-06:00",
    "Flat | 0.2135 | 00:00-24:00",
    "Flat | 0.2135 | 00:00-24:00 | Jan-Dec",
    "Winter night | 0.07 | 22:00-06:00 | Nov-Mar\n"
    "Winter day | 0.19 | 06:00-22:00 | Nov-Mar\n"
    "Summer | 0.25 | 00:00-24:00 | Apr-Oct",
    "Odd months | 0.30 | 08:00-18:00 | Jan,Mar,May,Jul,Sep,Nov\nRest | 0.10 | 00:00-24:00",
    "Summer peak | 0.33 | 14:00-19:00 | june-september; Other | 0.12 | 00:00-24:00",
    "Peak | 0.34 | 16:15-20:45; Off-peak | 0.11 | 20:45-16:15",
    "Peak | 0.34 | 16-21; Off-peak | 0.11 | 21-16",
    "Peak only | 0.34 | 16:00-21:00",
    "Half | .5 | 00:00-12:00; Whole | 1 | 12:00-24:00",
    "Deep winter | 0.07 | 00:00-24:00 | Dec-Feb; Rest | 0.15 | 00:00-24:00",
    "Overnight | 0.05 | 23:30-00:30; Day | 0.21 | 00:30-23:30",
]

# The same grammar, walked systematically: every shape of season the parser
# takes against every shape of hours, so a round trip that only works on the
# examples somebody thought of is not enough to pass.
_SEASONS = [
    "",
    "Jan-Dec",
    "May-Oct",
    "Nov-Apr",
    "Dec-Feb",
    "Jan",
    "Jul",
    "Jan,Feb,Dec",
    "Mar-Mar",
    "Apr-Sep",
    "Feb,Apr,Jun,Aug,Oct,Dec",
    "sept-oct",
]
_HOURS = [
    "00:00-24:00",
    "16:00-21:00",
    "21:00-16:00",
    "06:00-09:00, 17:00-20:00",
    "00:00-06:00, 06:00-12:00, 12:00-18:00, 18:00-24:00",
    "23:30-00:30",
]
GENERATED = [
    f"Band | 0.19 | {hours}" + (f" | {season}" if season else "")
    for season in _SEASONS
    for hours in _HOURS
]

# Entries the editor must not draw controls for. The first three the service
# refuses outright; the last two it accepts, and the editor still keeps them as
# text — reading less than the service does is the safe direction, since the
# entry is then stored exactly as it was written rather than re-emitted as
# something the owner did not type.
KEPT_AS_TEXT = [
    ("Bad | 0.10 | 16:00-16:00", False),
    ("Odd | 0.10 | 00:00-24:00 | constructor", False),
    ("Odd | 0.10 | 00:00-24:00 | __proto__", False),
    ("Exponent | 1e-2 | 00:00-24:00", True),
    ("All day | 0.10 | 24:00-24:00", True),
]

# A band whose hours are already taken by one above it, which parse_bands
# accepts and prices at nothing, and the same with an entry the controls cannot
# read sitting in front of it.
SHADOWED = "All | 0.20 | 00:00-24:00; Peak | 0.34 | 16:00-21:00"
SHADOWED_BEHIND_TEXT = "Bad | 0.10 | 16:00-16:00; Peak | 0.34 | 16:00-21:00"

ALL_TEXTS = (
    TARIFFS + GENERATED + [text for text, _ in KEPT_AS_TEXT] + [SHADOWED, SHADOWED_BEHIND_TEXT]
)

# The harness. It loads the editor exactly as the page does — a state object
# holding the text it was mounted on — and then performs each edit the way the
# click handlers do, so what is measured is the composed string a save would
# actually send.
#
# The month buttons are read out of the markup the page renders rather than
# from the rule behind it: which months a band *appears* to apply in is the
# thing that was wrong, and a test that asked the rule instead would have
# agreed with itself while the strip on screen said the opposite.
HARNESS = """
const SOURCE = __SOURCE__;
const JOB = __JOB__;
// Two things the markup wants from the rest of the page: escaping, and the
// currency symbol from the control beside the editor. Neither is grammar.
const esc = (s) => String(s).replace(/[&<>"]/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const currencyNow = () => '$';
const api = new Function('esc', 'currencyNow', SOURCE + `
  ; return {
      bandItems, composeBands, bandMarkup, nextRange, newBand, toggleMonth, shadowedBands,
      DAY_END, get state() { return bandState; }, set state(v) { bandState = v; },
    };`)(esc, currencyNow);

const MONTHS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];

function load(text) {
  api.state = { original: text, items: api.bandItems(text), changed: false };
  return api.state;
}

// The pressed state of each month button, as drawn.
function pressed(item, index) {
  const markup = api.bandMarkup(item, index);
  const found = {};
  const wanted = /data-role="month" data-m="(\\d+)" aria-pressed="(true|false)"/g;
  let hit = wanted.exec(markup);
  while (hit !== null) {
    found[Number(hit[1])] = hit[2] === 'true';
    hit = wanted.exec(markup);
  }
  return MONTHS.map((m) => found[m]);
}

const out = {};
for (const text of JOB.texts) {
  const st = load(text);
  const items = st.items.map((it, i) => ({
    type: it.type,
    name: it.type === 'band' ? it.name : null,
    pressed: it.type === 'band' ? pressed(it, i) : null,
  }));
  const shadowed = [...api.shadowedBands(st.items)].map((i) => st.items[i].name);
  const untouched = api.composeBands();

  // Every band marked as edited, which is what forces the composing path: an
  // untouched entry hands back its own text and would prove nothing.
  const whole = load(text);
  whole.changed = true;
  whole.items.forEach((it) => { if (it.type === 'band') it.dirty = true; });
  const recomposed = api.composeBands();

  // A band added and then named, which is the owner filling in the one the
  // button made. Its rate is a real number so the result is a tariff the
  // service will read.
  const more = load(text);
  const fresh = api.newBand();
  fresh.name = 'Added';
  fresh.price = '0.07';
  more.items.push(fresh);
  more.changed = true;
  const appended = api.composeBands();

  const added = [];
  const toggled = [];
  items.forEach((meta, i) => {
    if (meta.type !== 'band') return;
    const one = load(text);
    const range = api.nextRange(one.items[i].ranges);
    one.items[i].ranges.push(range);
    one.items[i].dirty = true;
    one.changed = true;
    added.push({ index: i, range, text: api.composeBands() });
    for (const m of MONTHS) {
      const each = load(text);
      each.items[i].months = api.toggleMonth(each.items[i].months, m);
      each.items[i].dirty = true;
      each.changed = true;
      toggled.push({
        index: i, month: m, text: api.composeBands(), pressed: pressed(each.items[i], i),
      });
    }
  });
  out[text] = { items, shadowed, untouched, recomposed, appended, added, toggled };
}

// Every end a range can have, to show what the button seeds after each one.
const seeds = [];
for (let e = 1; e <= api.DAY_END; e += 1) seeds.push(api.nextRange([{ s: 0, e }]));

process.stdout.write(JSON.stringify({ tariffs: out, seeds, dayEnd: api.DAY_END }));
"""


def _editor_source() -> str:
    """The page's own band-grammar JavaScript, sliced out of the file served."""
    page = PAGE.read_text(encoding="utf-8")
    start = page.find(SLICE_FROM)
    end = page.find(SLICE_TO)
    assert start >= 0, f"{SLICE_FROM!r} is no longer in settings.html"
    assert end > start, f"{SLICE_TO!r} is no longer after {SLICE_FROM!r} in settings.html"
    return page[start:end]


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    """Run every tariff through the page's editor, once, in node."""
    program = HARNESS.replace("__SOURCE__", json.dumps(_editor_source())).replace(
        "__JOB__", json.dumps({"texts": ALL_TEXTS})
    )
    assert NODE is not None
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"the editor would not run:\n{done.stderr}"
    parsed: dict[str, Any] = json.loads(done.stdout)
    return parsed


def _season(band: RateBand) -> frozenset[int]:
    """A band's months, with no season read as what it means: every one of them."""
    return frozenset(range(1, 13)) if band.months is None else band.months


def _same_tariff(left: str, right: str) -> None:
    """Assert two texts are the same tariff to the service, not the same string."""
    a, b = parse_bands(left), parse_bands(right)
    assert [x.name for x in a] == [x.name for x in b]
    for x, y in zip(a, b, strict=True):
        assert x.price_per_kwh == pytest.approx(y.price_per_kwh), x.name
        assert x.hours == y.hours, x.name
        assert _season(x) == _season(y), x.name


@cache
def _owners(text: str) -> dict[tuple[int, int], str | None]:
    """Which band prices each minute of each month, by the service's own rule.

    Read out of ``band_at`` rather than worked out here, because a second
    opinion on which band wins is the mistake being tested for. Cached and
    never written to by a caller; a full year of minutes is 17,280 answers.
    """
    tariff = Tariff(bands=parse_bands(text))
    out: dict[tuple[int, int], str | None] = {}
    for month in range(1, 13):
        for minute in range(1440):
            band = tariff.band_at(datetime(2026, month, 15, minute // 60, minute % 60, tzinfo=UTC))
            out[month, minute] = None if band is None else band.name
    return out


def _covers(range_: dict[str, int], minute: int) -> bool:
    """Whether a composed range holds a minute, wrapping midnight as TimeRange does."""
    start, end = range_["s"], range_["e"]
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


@pytest.mark.parametrize("text", TARIFFS + GENERATED)
def test_the_editor_gives_back_the_tariff_it_was_given(report: dict[str, Any], text: str) -> None:
    # Every band edited, so every field goes out through the editor's own
    # writer. This is the drift test: the string may be reshaped, the tariff
    # may not.
    _same_tariff(text, report["tariffs"][text]["recomposed"])


@pytest.mark.parametrize("text", TARIFFS + GENERATED)
def test_a_tariff_nobody_edited_is_handed_back_unchanged(report: dict[str, Any], text: str) -> None:
    # A visit that opened the page and saved without touching the tariff must
    # store what was already there, character for character.
    assert report["tariffs"][text]["untouched"] == text


@pytest.mark.parametrize("text", TARIFFS + GENERATED)
def test_a_tariff_the_service_takes_is_shown_as_controls(report: dict[str, Any], text: str) -> None:
    # An editor that quietly dropped to a text box for ordinary tariffs would
    # pass every test above while being no editor at all.
    assert [item["type"] for item in report["tariffs"][text]["items"]] == [
        "band" for _ in parse_bands(text)
    ]


@pytest.mark.parametrize(("text", "service_takes_it"), KEPT_AS_TEXT)
def test_an_entry_the_controls_cannot_show_keeps_its_own_text(
    report: dict[str, Any], text: str, service_takes_it: bool
) -> None:
    entry = report["tariffs"][text]
    assert [item["type"] for item in entry["items"]] == ["raw"]
    # Kept exactly, so the service still sees what the owner wrote — and is
    # still the one that decides whether it is a tariff.
    assert entry["recomposed"] == text
    if service_takes_it:
        parse_bands(text)
    else:
        with pytest.raises(ValueError):
            parse_bands(text)


def test_the_documented_example_survives_the_editor(report: dict[str, Any]) -> None:
    # The help text on the settings page quotes this exact tariff, and the page
    # that quotes it is the page that would mangle it.
    spec = lookup_setting("tariff.bands")
    assert spec is not None
    assert EXAMPLE_BANDS in spec.help
    _same_tariff(EXAMPLE_BANDS, report["tariffs"][EXAMPLE_BANDS]["recomposed"])


def test_a_new_time_range_never_covers_the_whole_day(report: dict[str, Any]) -> None:
    # The blocker, at its root. A seeded 00:00-24:00 is valid, so nothing
    # refuses it and nothing on any page reads as wrong; the band simply takes
    # every hour from every band below it.
    day = report["dayEnd"]
    for seed in report["seeds"]:
        span = seed["e"] - seed["s"] if seed["e"] > seed["s"] else day - seed["s"] + seed["e"]
        assert 0 < span <= 60, seed
        assert seed["s"] != day, seed


def test_adding_a_range_to_a_band_swallows_no_band_below_it() -> None:
    # The reproduction, priced. One click on the peak band of a two-band tariff
    # moved every hour of the day into the peak rate: still a valid tariff, so
    # nothing was null and nothing was flagged.
    text = "Peak | 0.34 | 16:00-21:00; Off | 0.11 | 21:00-16:00"
    before = _owners(text)
    assert before[1, 180] == "Off"
    swallowed = "Peak | 0.34 | 16:00-21:00, 00:00-24:00; Off | 0.11 | 21:00-16:00"
    assert _owners(swallowed)[1, 180] == "Peak"
    assert set(_owners(swallowed).values()) == {"Peak"}


@pytest.mark.parametrize("text", TARIFFS)
def test_adding_a_range_changes_only_the_hours_it_adds(report: dict[str, Any], text: str) -> None:
    # Stronger than "not the whole day": every minute whose price changed has
    # to be a minute inside the range the button actually added, in a month the
    # band actually applies to. Anything else is an edit the owner did not make.
    before = _owners(text)
    for step in report["tariffs"][text]["added"]:
        after = _owners(step["text"])
        edited = parse_bands(text)[step["index"]]
        for key, was in before.items():
            month, minute = key
            if after[key] == was:
                continue
            assert _covers(step["range"], minute), (text, step, key, was, after[key])
            assert month in _season(edited), (text, step, key)
            # And the only band that can take it is the one that was edited.
            assert after[key] == edited.name, (text, step, key)


@pytest.mark.parametrize("text", TARIFFS)
def test_adding_a_band_reprices_nothing_that_was_already_priced(
    report: dict[str, Any], text: str
) -> None:
    # A new band is seeded with the whole day, which is safe only because it
    # goes on the end: first-match-wins means it can take the hours nobody
    # claimed and no others. If it ever stops being last, this fails.
    before = _owners(text)
    after = _owners(report["tariffs"][text]["appended"])
    for key, was in before.items():
        assert after[key] == (was if was is not None else "Added"), (text, key, was)


@pytest.mark.parametrize("text", TARIFFS)
def test_a_band_with_no_season_shows_every_month_pressed(report: dict[str, Any], text: str) -> None:
    # A band applying all year rendered with twelve unpressed buttons reads as a
    # band applying in no month at all, and told a screen reader exactly that.
    for band, item in zip(parse_bands(text), report["tariffs"][text]["items"], strict=True):
        assert item["pressed"] == [m in _season(band) for m in range(1, 13)]


@pytest.mark.parametrize("text", TARIFFS + GENERATED)
def test_pressing_a_month_changes_that_month_and_no_other(
    report: dict[str, Any], text: str
) -> None:
    # Pressing January on an all-year band used to remove January — the one
    # button on the strip whose appearance did not change.
    original = parse_bands(text)
    for step in report["tariffs"][text]["toggled"]:
        was = _season(original[step["index"]])
        want = was ^ {step["month"]}
        # No season is the only way the format writes "every month", so a band
        # switched down to nothing lands back on all twelve.
        expected = want or frozenset(range(1, 13))
        after = parse_bands(step["text"])
        assert _season(after[step["index"]]) == expected, step
        # And the strip agrees with what was stored, which is the half that was
        # wrong: the button pressed is the only one whose state moved.
        assert step["pressed"] == [m in expected for m in range(1, 13)], step
        # Nothing else moved: not the other bands' seasons, not the hours.
        for i, (x, y) in enumerate(zip(original, after, strict=True)):
            assert x.hours == y.hours
            assert x.price_per_kwh == y.price_per_kwh
            if i != step["index"]:
                assert _season(x) == _season(y)


@pytest.mark.parametrize("text", [*TARIFFS, SHADOWED])
def test_the_page_reports_exactly_the_bands_that_price_nothing(
    report: dict[str, Any], text: str
) -> None:
    # The warning has to agree with the parser about which bands are dead, or
    # it is a second opinion on the tariff — the thing this whole editor is
    # careful not to be. Most of these have no dead band at all, which is the
    # half of the agreement that matters most: a warning nobody earned.
    priced = {name for name in _owners(text).values() if name is not None}
    dead = [band.name for band in parse_bands(text) if band.name not in priced]
    assert sorted(report["tariffs"][text]["shadowed"]) == sorted(dead)


def test_a_band_hidden_behind_a_wider_one_is_reported(report: dict[str, Any]) -> None:
    # And it is reported for the right reason: the tariff is valid, the service
    # stores it, and the rate sits in the editor looking like the rate in force.
    assert parse_bands(SHADOWED)
    assert report["tariffs"][SHADOWED]["shadowed"] == ["Peak"]


def test_an_entry_shown_as_text_stops_the_page_claiming_anything(
    report: dict[str, Any],
) -> None:
    # Nobody here knows what hours a kept entry claims, so no band below it can
    # be called dead. Saying nothing beats saying something untrue.
    assert report["tariffs"][SHADOWED_BEHIND_TEXT]["shadowed"] == []


def test_the_click_handlers_decide_nothing_for_themselves() -> None:
    # Everything above tests the half of the editor that composes text. The
    # other half is event handlers, and a handler that builds a range or a
    # season inline is a second copy of the rule that nothing here would catch
    # — which is exactly how the whole-day seed and the empty month strip got
    # in. So the wiring may only call the named rules, and these four spellings
    # of doing it itself must not appear below the slice.
    page = PAGE.read_text(encoding="utf-8")
    wiring = page[page.index(SLICE_TO) :]
    for done_by_hand in ("DAY_END", ".months.has(", "ranges.push({", "new Set("):
        assert done_by_hand not in wiring, done_by_hand
    for named in ("nextRange(item.ranges)", "newBand()", "toggleMonth(", "monthOn("):
        assert named in wiring, named
