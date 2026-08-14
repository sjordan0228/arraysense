"""test_model_check_js.py — the dashboard draws the backend's model warning, verbatim.

The model warning is the one part of #128 the page has to get right alone: an
exact-model mismatch keeps collecting, so the banner is the only place the owner
of a mis-configured inverter is told. The decision — given a status payload, is
there a banner and with what text — lives in a pure function between the
model-check markers in index.html, and runs here under node the way the caps,
live-strip and sankey slices do.

Three rules are held here. A null model_check — the ordinary answer, and the
reference installation's — must produce no banner at all. A verdict must carry
the backend's own message untouched. And the page must never compose a sentence
of its own from the verdict, which is the same rule the staleness banner
follows: the wording is the backend's.

What this does not do is render the banner in a DOM; the layout is verified in
a browser. Skipped where node is not installed, and loud if the markers move so
the slice cannot drift out from under it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
INDEX = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "index.html"

_START = "// >>> model-check"
_END = "// <<< model-check"


def _slice() -> str:
    text = INDEX.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "model-check markers are out of order in index.html"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    out = subprocess.run(
        [NODE, "-e", _slice() + "\n" + body], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_null_model_check_produces_no_banner() -> None:
    # None is what the reference installation gets — no model configured, so no
    # verdict is ever produced — and a missing status or a status without the
    # field must answer the same way. A banner shown for any of these would
    # shout at every install that simply has nothing to say.
    body = """
    console.log([
      modelCheckBanner(null), modelCheckBanner(undefined), modelCheckBanner({}),
      modelCheckBanner({ model_check: null }),
    ].map(v => v === null ? 'null' : 'banner').join(' '));
    """
    assert _run(body) == "null null null null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_verdict_carries_the_backend_message_verbatim() -> None:
    # The message is the backend's sentence naming the risk; the banner prints
    # it unchanged. Anything short of identity means the page reworded what the
    # service decided to say.
    body = """
    const message = 'configured as 12kPV; the inverter reports 18kPV — both ' +
      'are hybrid, so the registers mean the same things, but the string count ' +
      'and the conversion figures differ. Check the model setting.';
    const check = { verdict: 'model_mismatch', message };
    console.log(String(modelCheckBanner({ model_check: check }) === message));
    """
    assert _run(body) == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_verdict_without_a_message_is_nothing_to_show() -> None:
    # The page may not supply the words a verdict lacks: composing a sentence
    # here is how a second wording comes to exist beside the backend's.
    body = """
    console.log([
      modelCheckBanner({ model_check: { verdict: 'model_mismatch' } }),
      modelCheckBanner({ model_check: { verdict: 'model_mismatch', message: '' } }),
      modelCheckBanner({ model_check: { verdict: 'model_mismatch', message: 0 } }),
    ].map(v => v === null ? 'null' : 'banner').join(' '));
    """
    assert _run(body) == "null null null"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_banner_body_is_the_message_escaped_verbatim() -> None:
    # modelCheckHtml builds the body from the message alone, through the same
    # .cal shape the calibration advisory uses. esc is stubbed because it comes
    # from common.js; what matters is that the message reaches the markup
    # unchanged (the stub marks it) and no verdict wording is invented.
    body = """
    const esc = (s) => '[' + s + ']';
    const out = modelCheckHtml('check the model setting');
    console.log([
      out.includes('[check the model setting]'),
      out.indexOf('verdict'),
    ].map(String).join(' '));
    """
    assert _run(body) == "true -1"


def test_the_decision_never_reads_the_verdict() -> None:
    # The verdict names only the class of disagreement. Reading it would be a
    # page deriving what to say from a field the backend did not word — the
    # start of a second wording. The decision is a function of the message
    # alone.
    assert ".verdict" not in _slice()
