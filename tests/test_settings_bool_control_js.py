"""A boolean setting has to render as something that can only hold a boolean.

The page builds every control from the registry's declared kind, and until the
daily backup arrived nothing in the registry was a bool — so ``kind: "bool"``
fell through to a text box holding the word "true". The setting appeared, could
be edited, and could never be saved: the service wants a JSON boolean and
refuses the string. These drive the two functions that decide it, so a kind
the page cannot render fails here rather than on somebody's settings page.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
PAGE = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web" / "settings.html"

# `esc` lives in common.js, and `document`/`CSS` are the browser's. Both are
# stubbed rather than imported: what is under test is the branch on the
# registry's kind, not the escaping or the DOM.
PRELUDE = """
const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
  .replace(/"/g, '&quot;');
"""


def _slice(start: str, end: str) -> str:
    text = PAGE.read_text()
    return text[text.index(start) : text.index(end)]


def _run(body: str, start: str, end: str) -> str:
    assert NODE is not None
    result = subprocess.run(
        ["node", "-e", PRELUDE + _slice(start, end) + "\n" + body],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


RENDER = ("const isLong = (spec)", "// What is wrong with a value")
READ = ("function currentValue(spec)", "// How a value reads")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_boolean_setting_renders_as_a_checkbox() -> None:
    html = _run(
        "console.log(control({key:'backup.enabled',kind:'bool',choices:[]}, true));",
        *RENDER,
    )
    assert 'type="checkbox"' in html
    assert " checked" in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_boolean_that_is_off_renders_unticked() -> None:
    html = _run(
        "console.log(control({key:'backup.enabled',kind:'bool',choices:[]}, false));",
        *RENDER,
    )
    assert 'type="checkbox"' in html
    assert "checked" not in html


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_checkbox_is_read_from_checked_and_not_from_value() -> None:
    """A checkbox's `value` is the string "on" whether it is ticked or not, so
    reading it the way a text box is read posts the same answer both ways."""
    stub = """
    const CSS = { escape: (s) => s };
    let box = { checked: false, value: 'on' };
    const document = { querySelector: () => box };
    const spec = { key: 'backup.enabled', kind: 'bool' };
    console.log(JSON.stringify(currentValue(spec)));
    box.checked = true;
    console.log(JSON.stringify(currentValue(spec)));
    """
    assert _run(stub, *READ).splitlines() == ["false", "true"]
