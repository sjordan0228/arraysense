"""test_charge_panel_js.py -- the charge control's own rules, run under node.

The panel sits under the Overnight plan note, where the calibration line has
already said the state of charge is not trustworthy enough to project a night
on top of. Three rules decide what it shows: an installation whose driver
cannot report charge configuration gets no panel at all, a control the API
would only refuse is not offered, and a field the device never reported is
never drawn as a zero. These run the marked charge-panel slice from
overnight.html under node, so a page that offers a button which can only be
refused fails here rather than on a real inverter.

Skipped where node is not installed; loud if the extraction markers move.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "overnight.html"

_START = "// >>> charge-panel"
_END = "// <<< charge-panel"

# A fixed clock, so a test never depends on the time it ran. The status line
# takes its time from the response and not from now, which is the only way the
# two window branches can be told apart here at all.
NOW_MS = 1_757_500_000_000

# One window edge used by both window tests: 23:45 in the response's own clock
# convention, the same slice(11, 16) the rest of the page prints.
UNTIL = "2026-09-10T23:45:00+00:00"

# The page's own fallback for a refusal whose body said nothing usable. It has
# to be a sentence: an empty paragraph tells the owner nothing happened and not
# one word about why.
FALLBACK = "That charge request was refused."


def _slice() -> str:
    text = PAGE.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "charge-panel markers are out of order in overnight.html"
    return text[start:end]


def _call(fn: str, *args: object) -> Any:
    """Run one slice function under node and return its result as Python data.

    check=True is deliberate: a slice that throws surfaces as a failed run
    rather than as a passed assertion over garbage.
    """
    assert NODE is not None
    expr = fn + "(" + ", ".join(json.dumps(a) for a in args) + ")"
    body = f"{_slice()}\nconsole.log(JSON.stringify({expr}));"
    result = subprocess.run(["node", "-e", body], capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip())


def _wiring() -> str:
    """The page's charge wiring: everything after the slice, up to the script end.

    The handlers cannot live in the slice — they touch the DOM and the network,
    which is exactly what the slice is kept free of — so they are extracted on
    their own for the one test that has to press the button rather than call a
    rule.
    """
    text = PAGE.read_text()
    start = text.index(_END) + len(_END)
    end = text.index("</script>", start)
    assert start < end, "the charge-panel markers sit after the page's last script"
    return text[start:end]


# document and fetch are the minimum the wiring touches: id-keyed boxes that
# record what was written to them and keep the listeners bound to them, and a
# fetch that hands back queued answers in order and notices what the page looked
# like at the moment each request went out.
_HARNESS = """
// The page's own escape helper, verbatim from common.js, so the plan request
// that the page fires on load cannot reject on a missing name and take the
// process down with it before the press this test is about.
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const boxes = {};
const element = (id) => (boxes[id] ??= {
  id,
  textContent: '',
  innerHTML: '',
  hidden: false,
  disabled: false,
  value: id === 'chargePower' ? '3000' : '',
  listeners: {},
  addEventListener(type, fn) { this.listeners[type] = fn; },
});
const document = { getElementById: element };
const calls = [];
const during = [];
const queued = JSON.parse(JSON.stringify(RESPONSES));
globalThis.fetch = async (url, options) => {
  const target = String(url);
  const method = (options && options.method) || 'GET';
  calls.push(method + ' ' + target);
  let answer = { status: 200, body: {} };
  if (target === '/api/charge/start') {
    answer = queued.start.shift();
    // What the panel said while the write was in flight: this is the reading
    // the whole test exists for, and it cannot be taken afterwards.
    during.push({
      method,
      disabled: element('chargeGo').disabled,
      button: element('chargeGo').textContent,
      status: element('chargeStatus').textContent,
    });
  } else if (target === '/api/charge') {
    answer = queued.charge.shift();
  }
  return { ok: answer.status === 200, status: answer.status, json: async () => answer.body };
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
"""


def _override(
    recorded: bool = False,
    readable: bool = False,
    active: bool = False,
    until: str | None = None,
    requested_w: int | None = None,
) -> dict[str, Any]:
    """The record block exactly as /api/charge reports it."""
    return {
        "recorded": recorded,
        "readable": readable,
        "active": active,
        "until": until,
        "requested_w": requested_w,
    }


def _charge(
    power_w: int | None = 3000,
    stop_soc_pct: float | None = 100.0,
    ac_charge_enabled: bool | None = False,
    windows: int = 2,
    override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One whole /api/charge body: the configuration plus the record."""
    window = {
        "start_hour": 2,
        "start_minute": 0,
        "end_hour": 6,
        "end_minute": 0,
        "is_set": True,
    }
    return {
        "ac_charge_enabled": ac_charge_enabled,
        "power_w": power_w,
        "stop_soc_pct": stop_soc_pct,
        "windows": [dict(window) for _ in range(windows)],
        "schedule_type": "original",
        "start_soc_pct": None,
        "window_end_soc_pct": None,
        "start_voltage_v": None,
        "stop_voltage_v": None,
        "quick_charge_remaining_s": None,
        "registers": {},
        "read_at": "2026-09-10T20:00:00+00:00",
        "override": _override() if override is None else override,
    }


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_there_is_no_panel_when_the_driver_cannot_report_charge() -> None:
    """A driver that cannot report charge configuration answers 404, which is a
    different claim from "nothing is configured". An empty panel makes the
    first claim look like the second, so the page renders no panel, no status
    line, no buttons and no configuration line."""
    assert _call("chargePanelVisible", None) is False
    assert _call("chargeStatusLine", None, NOW_MS) == ""
    assert _call("chargeStartVisible", None) is False
    assert _call("chargeStopVisible", None) is False
    assert _call("chargeConfigLine", None) == ""
    # The control that makes the five answers above mean something. A page that
    # answers "hidden" and "nothing to say" to every input, a real device
    # included, fails here rather than passing on its blankness.
    cfg = _charge()
    assert _call("chargePanelVisible", cfg) is True
    assert _call("chargeStatusLine", cfg, NOW_MS) != ""
    assert _call("chargeConfigLine", cfg) != ""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_panel_appears_with_a_configuration_and_no_record() -> None:
    """The shape the panel was built for: the device answers, and nothing this
    service started is standing."""
    cfg = _charge()
    assert _call("chargePanelVisible", cfg) is True
    assert _call("chargeStatusLine", cfg, NOW_MS) == "No grid charge is running."
    assert "3 kW" in _call("chargeConfigLine", cfg)
    # A body with no record block is still a configuration. The page must not
    # treat a missing block as damage and hide a device it can talk to.
    bare = {key: value for key, value in cfg.items() if key != "override"}
    assert _call("chargePanelVisible", bare) is True


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_start_button_is_offered_only_when_nothing_is_recorded() -> None:
    """A second start is refused while a record stands, in either window state.
    A control that can only be refused is not shown: it invites the press and
    then explains."""
    assert _call("chargeStartVisible", _charge()) is True
    running = _charge(override=_override(True, True, True, UNTIL, 3000))
    assert _call("chargeStartVisible", running) is False
    closed = _charge(override=_override(True, True, False, UNTIL, 3000))
    assert _call("chargeStartVisible", closed) is False
    assert _call("chargeStartVisible", None) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_stop_button_is_offered_for_a_readable_record() -> None:
    """Both record states are undoable. The window may still be open, or it may
    have closed with the inverter still holding what this service wrote; stop
    puts the previous configuration back either way."""
    running = _charge(override=_override(True, True, True, UNTIL, 3000))
    closed = _charge(override=_override(True, True, False, UNTIL, 3000))
    assert _call("chargeStopVisible", running) is True
    assert _call("chargeStopVisible", closed) is True
    assert _call("chargeStopVisible", _charge()) is False
    assert _call("chargeStopVisible", None) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_no_buttons_are_offered_when_the_record_cannot_be_read() -> None:
    """An unreadable record cannot be stopped either: there is no saved
    configuration to write back, and a button that promises the impossible is
    worse than none. The status line carries the warning in their place."""
    cfg = _charge(override=_override(True, False, False))
    assert _call("chargeStartVisible", cfg) is False
    assert _call("chargeStopVisible", cfg) is False
    line = _call("chargeStatusLine", cfg, NOW_MS)
    assert "cannot be read" in line
    assert "may still be holding" in line
    assert "before starting another charge" in line


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_running_charge_reports_its_power_and_the_time_its_window_closes() -> None:
    """The line says what the inverter is doing and until when, both read from
    the response. A charge at 3 kW whose window ends at 23:45 says exactly
    that, and does not also say no charge is running."""
    cfg = _charge(override=_override(True, True, True, UNTIL, 3000))
    line = _call("chargeStatusLine", cfg, NOW_MS)
    assert "3 kW" in line
    assert "23:45" in line
    assert "No grid charge" not in line


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_closed_window_says_so_and_still_offers_the_stop() -> None:
    """A window that ran out is not a charge that stopped. The override is on
    the inverter until the owner clears it, so the line names the time it
    closed and the stop stays offered."""
    cfg = _charge(override=_override(True, True, False, UNTIL, 3000))
    line = _call("chargeStatusLine", cfg, NOW_MS)
    assert "23:45" in line
    assert "closed" in line
    assert "back the way it was" in line
    assert _call("chargeStopVisible", cfg) is True
    assert _call("chargeStartVisible", cfg) is False


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_action_label_says_what_the_press_will_do() -> None:
    """The label describes the press, never the device. A whole kilowatt count
    reads as 3 kW and a half as 2.5 kW; an argument that is not a number falls
    back to the bare sentence rather than printing an undefined power."""
    assert _call("chargeActionLabel", 3000) == "Charge to full from grid at 3 kW"
    assert _call("chargeActionLabel", 2500) == "Charge to full from grid at 2.5 kW"
    assert _call("chargeActionLabel", "3000") == "Charge to full from grid"
    assert _call("chargeActionLabel", None) == "Charge to full from grid"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_field_the_device_did_not_report_is_not_shown_as_zero() -> None:
    """A null power is not 0 kW and a null stop setting is not 0%. Both read as
    a configuration the owner could act on, and the device never said either
    one. A value it did report still has to print."""
    line = _call("chargeConfigLine", _charge(power_w=None, stop_soc_pct=None))
    assert "not reported" in line
    assert "0 kW" not in line
    assert "0%" not in line
    assert "AC charging is off" in line
    reported = _call("chargeConfigLine", _charge())
    assert "3 kW" in reported
    assert "100%" in reported


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_press_is_announced_while_it_is_in_flight() -> None:
    """A write to the inverter takes about a second and a half, and the first
    live attempt showed what an unannounced press costs: the owner pressed, the
    request went out, the inverter did not answer, and nothing on screen had
    said the press was taken at all. The line says which way the press is going
    and is not the resting label, so the button visibly changes under the
    press."""
    start = _call("chargeBusyLine", "start")
    stop = _call("chargeBusyLine", "stop")
    assert start == "Starting the charge…"
    assert stop == "Stopping the charge…"
    # Each press has its own words: one line for both would tell the owner
    # something is happening and not which thing.
    assert start != stop
    assert start != _call("chargeActionLabel", 3000)
    assert stop != start


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_refused_press_announces_itself_and_re_reads_the_panel() -> None:
    """The two failures of the first live press, together, in the order the
    owner met them. The press has to say it was taken while the write is out,
    and the panel has to be re-read afterwards whether the write worked or not:
    a refused start still leaves a record, so a page that keeps offering the
    start it had is offering a button the API can only refuse."""
    assert NODE is not None
    refused = {
        "charge": [
            {"status": 200, "body": _charge()},
            # What the server holds after a refused write: the record is kept,
            # because the inverter may have taken part of the write.
            {"status": 200, "body": _charge(override=_override(True, True, True, UNTIL, 3000))},
        ],
        "start": [{"status": 409, "body": {"detail": "the inverter did not answer the write"}}],
    }
    driver = """
(async () => {
  await settle();
  const before = {
    calls: calls.slice(),
    startHidden: element('chargeGo').hidden,
    stopHidden: element('chargeStop').hidden,
  };
  await startCharge();
  await settle();
  console.log(JSON.stringify({
    before,
    during,
    calls,
    after: {
      startHidden: element('chargeGo').hidden,
      stopHidden: element('chargeStop').hidden,
      startDisabled: element('chargeGo').disabled,
      status: element('chargeStatus').textContent,
      why: element('chargeWhy').textContent,
      whyHidden: element('chargeWhy').hidden,
    },
  }));
})();
"""
    script = (
        # The queued answers are bound first: the harness copies them as it is
        # evaluated, so a RESPONSES declared afterwards is a temporal dead zone
        # rather than a stub.
        "const RESPONSES = "
        + json.dumps(refused)
        + ";\n"
        + _HARNESS
        + "\n"
        + _slice()
        + "\n"
        + _wiring()
        + "\n"
        + driver
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    out = json.loads(result.stdout.strip())

    # The panel starts on the healthy shape: a start to offer, no stop.
    assert out["before"]["startHidden"] is False
    assert out["before"]["stopHidden"] is True

    # The press announces itself on the way out, and it is taken out of the
    # owner's hands while it is out.
    assert [press["button"] for press in out["during"]] == ["Starting the charge…"]
    assert [press["status"] for press in out["during"]] == ["Starting the charge…"]
    assert [press["disabled"] for press in out["during"]] == [True]

    # And the panel is re-read after it: the refusal is named, the button is
    # handed back, and the control on offer is now the stop the record calls
    # for rather than the start that was just refused.
    assert out["calls"].count("GET /api/charge") == 2
    assert out["calls"][-1] == "GET /api/charge"
    assert out["after"]["startHidden"] is True
    assert out["after"]["stopHidden"] is False
    assert out["after"]["startDisabled"] is False
    assert out["after"]["why"] == "the inverter did not answer the write"
    assert out["after"]["whyHidden"] is False
    assert "Charging from the grid" in out["after"]["status"]


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_refusal_shows_the_servers_reason_and_never_renders_nothing() -> None:
    """The refusal paragraph is the only thing telling the owner why nothing
    happened, so it is never empty, never undefined and never "[object
    Object]". A server that names the reason gets printed verbatim."""
    named = {"detail": "The bank is already at its stop setting."}
    assert _call("chargeRefusalText", named) == "The bank is already at its stop setting."
    assert _call("chargeRefusalText", {}) == FALLBACK
    assert _call("chargeRefusalText", None) == FALLBACK
    assert _call("chargeRefusalText", {"detail": ""}) == FALLBACK
    assert _call("chargeRefusalText", {"detail": None}) == FALLBACK
    assert _call("chargeRefusalText", {"detail": {"code": "busy"}}) == FALLBACK
