"""test_install.py — the bootstrap installer's preflight, port choice, and plan."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import install


def _ok(**over: object) -> dict[str, Any]:
    base = dict(
        platform_name="linux",
        has_systemd=True,
        euid=0,
        machine="aarch64",
        has_git=True,
        free_bytes=8 * 1024**3,
        python_version=(3, 11),
    )
    base.update(over)
    return base


def test_a_healthy_host_passes() -> None:
    assert install.preflight(**_ok()) is None


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"platform_name": "darwin"}, "Linux"),
        ({"has_systemd": False}, "systemd"),
        ({"euid": 1000}, "root"),
        ({"machine": "armv7l"}, "architecture"),
        ({"has_git": False}, "git"),
        ({"free_bytes": 512 * 1024**2}, "disk"),
        ({"python_version": (3, 7)}, "Python"),
    ],
)
def test_each_refusal_names_what_is_wrong(override: dict[str, object], expected: str) -> None:
    """One reason at a time, and each says what to do about it.

    A bootstrap piped into root that fails halfway is the worst outcome here, so
    every reason to stop is found before anything is touched.
    """
    refusal = install.preflight(**_ok(**override))
    assert refusal is not None
    assert expected.lower() in refusal.reason.lower()
    assert refusal.remedy, "a refusal without a remedy is a dead end"


def test_the_install_filesystem_falls_back_to_an_existing_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "INSTALL_DIR", "/nonexistent/deeply/nested/path")
    assert os.path.exists(install._install_filesystem())


def test_port_80_is_used_when_it_is_free() -> None:
    assert install.resolve_port(probe=lambda p: True, ask=lambda _prompt: "") == 80


def test_a_taken_port_80_offers_8080_as_the_default() -> None:
    """Enter accepts 8080 rather than silently failing to bind later."""
    assert install.resolve_port(probe=lambda p: p != 80, ask=lambda _prompt: "") == 8080


def test_a_taken_port_80_accepts_a_chosen_port() -> None:
    assert install.resolve_port(probe=lambda p: p not in (80,), ask=lambda _prompt: "9000") == 9000


def test_the_plan_names_every_path_it_will_create() -> None:
    """The mitigation for piping a script into root is that you see it first."""
    plan = install.render_plan(8080)
    for expected in (
        install.INSTALL_DIR,
        install.CONFIG_DIR,
        install.DATA_DIR,
        install.SERVICE_USER,
        install.CLI_SHIM,
        "8080",
    ):
        assert expected in plan


def test_the_plan_says_no_config_is_written() -> None:
    """The config's absence is what runs the wizard, so the plan says so."""
    assert "wizard" in install.render_plan(80).lower()


def test_a_given_port_skips_the_question_entirely() -> None:
    """The unattended path must not depend on a terminal being there."""

    def refuse(_prompt: str) -> str:
        raise AssertionError("resolve_port must not ask when --port was given")

    assert install.resolve_port(probe=lambda p: True, ask=refuse, chosen=9001) == 9001


def test_no_terminal_is_not_consent() -> None:
    """A root install that could not ask must never behave as though it did."""

    def no_tty(_prompt: str) -> str:
        raise install.NoTerminal()

    with pytest.raises(install.NoTerminal):
        install.resolve_port(probe=lambda p: p != 80, ask=no_tty)


def test_parse_args_reads_the_two_unattended_flags() -> None:
    assert install.parse_args(["--yes", "--port", "8099"]) == {"yes": True, "port": 8099}
    assert install.parse_args([]) == {"yes": False, "port": None}


def test_parse_args_refuses_a_port_that_is_not_a_number() -> None:
    with pytest.raises(SystemExit):
        install.parse_args(["--port", "eighty"])
