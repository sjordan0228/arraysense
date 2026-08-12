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
    assert install.parse_args(["--yes", "--port", "8099"]) == {
        "yes": True,
        "port": 8099,
        "repo": install.REPO_URL,
        "ref": None,
    }
    assert install.parse_args([]) == {
        "yes": False,
        "port": None,
        "repo": install.REPO_URL,
        "ref": None,
    }


def test_parse_args_refuses_a_port_that_is_not_a_number() -> None:
    with pytest.raises(SystemExit):
        install.parse_args(["--port", "eighty"])


def test_unit_text_reads_the_unit_from_the_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unit travels with the source, so this checkout is the source."""
    monkeypatch.setattr(install, "INSTALL_DIR", str(Path(__file__).resolve().parents[1]))
    text = install.unit_text()
    assert "[Unit]" in text
    assert "Solar ArraySense" in text


def test_the_dropin_sets_the_port_and_only_the_port() -> None:
    """ExecStart is cleared then re-set: systemd appends otherwise, and the
    unit would try to start twice with different arguments."""
    text = install.dropin_text(8099)
    assert "ExecStart=\n" in text
    assert "--port 8099" in text


def test_port_80_gets_the_capability_and_other_ports_do_not() -> None:
    """Without it an unprivileged service cannot bind 80, and the failure
    reads as nothing to do with permissions."""
    assert "CAP_NET_BIND_SERVICE" in install.dropin_text(80)
    assert "CAP_NET_BIND_SERVICE" not in install.dropin_text(8080)


def test_the_shim_runs_manage_under_the_system_interpreter() -> None:
    """Never the virtualenv: upgrade rebuilds it while this is running."""
    shim = install.shim_text()
    assert "/opt/arraysense/src/arraysense/manage.py" in shim
    assert ".venv" not in shim


def test_the_handoff_never_prints_a_loopback_address() -> None:
    """127.0.1.1 is what /etc/hosts says on Debian, and it is useless to
    somebody opening the dashboard from their laptop."""
    ip = install.outbound_ip()
    if ip is not None:
        assert not ip.startswith("127."), ip


def test_the_handoff_omits_the_ip_line_when_there_is_no_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "outbound_ip", lambda: None)
    text = install.render_handoff(8080, "pi")
    assert "http://pi.local:8080" in text
    assert "None" not in text


def test_parse_args_reads_the_repository_and_ref() -> None:
    parsed = install.parse_args(["--repo", "/srv/a.git", "--ref", "feat/x"])
    assert parsed["repo"] == "/srv/a.git"
    assert parsed["ref"] == "feat/x"


def test_the_defaults_are_the_project_and_its_default_branch() -> None:
    parsed = install.parse_args([])
    assert parsed["repo"] == install.REPO_URL
    assert parsed["ref"] is None


def test_the_plan_names_a_non_default_repository() -> None:
    """Installing from somewhere other than the project must be visible in the
    plan, because the plan is the only thing shown before root acts."""
    plan = install.render_plan(8080, repo="/srv/a.git", ref="feat/x")
    assert "/srv/a.git" in plan
    assert "feat/x" in plan


def test_the_clone_step_keeps_history() -> None:
    """--depth 1 is what broke every upgrade; it must not come back."""
    plan = install.clone_argv(repo="https://example.invalid/a.git", ref=None)
    assert "--depth" not in plan


def test_the_clone_step_still_pins_a_ref_when_one_is_given() -> None:
    """A pinned ref travels on the command just as it did before the depth went."""
    argv = install.clone_argv(repo="https://example.invalid/a.git", ref="v1.0.0")
    assert argv == [
        "git",
        "clone",
        "--branch",
        "v1.0.0",
        "https://example.invalid/a.git",
        install.INSTALL_DIR,
    ]
