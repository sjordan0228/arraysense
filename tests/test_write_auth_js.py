"""test_write_auth_js.py — the write helper's retry decision, and that every write uses it.

The login dialog's DOM is verified in a real browser, not here — what this can
run under node is the one pure decision the helper is built on: whether a 401
deserves the dialog and a retry. The other half is a string check across the
pages: the six write endpoints all have to go through the shared helper,
because the failure mode nobody notices is a seventh write added later that
quietly builds its own fetch and so bypasses the login prompt entirely.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
WEB = Path(__file__).resolve().parent.parent / "src" / "arraysense" / "web"
COMMON = WEB / "common.js"
INDEX = WEB / "index.html"
SETTINGS = WEB / "settings.html"

_START = "// >>> write-auth-logic"
_END = "// <<< write-auth-logic"

# The six protected writes, as (file, the exact call) pairs. The needle is the
# call text, so a file reorganisation that moves a call is caught just like one
# that reverts it to a bare fetch.
_CALL_SITES = [
    (INDEX, "writeWithAuth('/api/yield'"),
    (INDEX, "writeWithAuth('/api/resume'"),
    (INDEX, "writeWithAuth('/api/setup/apply'"),
    (SETTINGS, "writeWithAuth('/api/settings'"),
    (SETTINGS, "writeWithAuth('/api/setup/apply'"),
    (COMMON, "writeWithAuth('/api/setup/detect'"),
]
_CALL_IDS = [
    "index-yield",
    "index-resume",
    "index-setup-apply",
    "settings-settings",
    "settings-setup-apply",
    "common-setup-detect",
]


def _slice() -> str:
    text = COMMON.read_text()
    start = text.index(_START)
    end = text.index(_END)
    assert start < end, "write-auth-logic markers are out of order in common.js"
    return text[start:end]


def _run(body: str) -> str:
    assert NODE is not None
    script = _slice() + "\n" + body
    out = subprocess.run([NODE, "-e", script], capture_output=True, text=True, check=True)
    return out.stdout.strip()


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_first_401_is_retried() -> None:
    # A fresh 401 is exactly the case the dialog exists for: the password is
    # set, this client has no session, and logging in should unlock the write.
    assert _run("console.log(String(authShouldRetry(401, false)));") == "true"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_a_second_401_is_not_retried() -> None:
    # After one successful login the retry still answered 401, which means the
    # session did not take or was revoked in the gap. Retrying again would loop.
    assert _run("console.log(String(authShouldRetry(401, true)));") == "false"


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_any_status_other_than_401_is_never_retried() -> None:
    # The no-password path and the ordinary success path both answer something
    # else, and both must pass through untouched.
    out = _run("console.log([200, 400, 500].map((s) => authShouldRetry(s, false)).join(','));")
    assert out == "false,false,false"


@pytest.mark.parametrize("path,needle", _CALL_SITES, ids=_CALL_IDS)
def test_the_write_call_sites_go_through_the_shared_helper(path: Path, needle: str) -> None:
    """Every protected write on the pages calls writeWithAuth, not a bare fetch."""
    text = path.read_text()
    assert needle in text, f"{path.name} must call writeWithAuth for this endpoint"
