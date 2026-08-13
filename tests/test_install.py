"""test_install.py — the bootstrap installer's preflight, port choice, and plan."""

from __future__ import annotations

import glob
import importlib
import os
import shutil
import socket
import subprocess
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
        has_curl=True,
        free_bytes=8 * 1024**3,
        python_version=(3, 11),
    )
    base.update(over)
    return base


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str] | None = None,
    step_codes: dict[tuple[str, ...], int] | None = None,
) -> tuple[int, list[tuple[list[str], dict[str, str] | None]]]:
    """Run main() with the host, filesystem and uv stubbed, recording _step.

    The install flow is long; every external command goes through _step and
    every file write through _write_file. Stubbing those two plus the host
    lets a test drive main() from parse_args to the handoff and inspect what
    it would have run, which is the only way to pin the ordering and the
    exit codes that no single function owns.
    """
    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_step(argv: list[str], *, env: dict[str, str] | None = None) -> int:
        calls.append((argv, env))
        if step_codes and tuple(argv) in step_codes:
            return step_codes[tuple(argv)]
        return 0

    monkeypatch.setattr(install, "observe_host", _ok)
    monkeypatch.setattr(install, "INSTALL_DIR", "/tmp/arraysense-audit-install")
    monkeypatch.setattr(install, "_step", fake_step)
    monkeypatch.setattr(install, "find_uv", lambda: "/usr/bin/uv")
    monkeypatch.setattr(install, "resolve_port", lambda **kwargs: 80)
    monkeypatch.setattr(install, "unit_text", lambda: "[Unit]\n")
    monkeypatch.setattr(install, "_packaging_file", lambda name: f"[{name}]\n")
    monkeypatch.setattr(install, "_write_file", lambda path, text: None)
    monkeypatch.setattr(install, "DROPIN_DIR", "/tmp/arraysense-audit-dropin")
    monkeypatch.setattr(install, "CLI_SHIM", "/tmp/arraysense-audit-cli")
    monkeypatch.setattr(os, "makedirs", lambda *a, **k: None)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)
    monkeypatch.setattr(install, "render_plan", lambda *a, **k: "")
    monkeypatch.setattr(install, "render_handoff", lambda *a, **k: "")
    monkeypatch.setattr(socket, "gethostname", lambda: "testhost")
    return install.main(["--yes"] if argv is None else argv), calls


def test_a_healthy_host_passes() -> None:
    assert install.preflight(**_ok()) is None


def test_the_module_loads_when_typing_lacks_typed_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a pre-3.8 host typing has no TypedDict, and an unconditional import
    killed the too-old refusal with an ImportError traceback before it could
    print. Simulate that typing gap in a fresh module load and prove the
    module still loads and preflight still refuses."""
    import typing

    monkeypatch.delattr(typing, "TypedDict")
    spec = importlib.util.spec_from_file_location("install_oldpy", install.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    refusal = module.preflight(**_ok(python_version=(3, 7)))
    assert refusal is not None
    assert "too old" in refusal.reason


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"platform_name": "darwin"}, "Linux"),
        ({"has_systemd": False}, "systemd"),
        ({"euid": 1000}, "root"),
        ({"machine": "armv7l"}, "architecture"),
        ({"has_git": False}, "git"),
        ({"has_curl": False}, "curl"),
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


def test_parse_args_accepts_the_equals_form() -> None:
    """--port=8080 must not be silently dropped, the way "in argv" parsing did."""
    assert install.parse_args(["--yes", "--port=8080"]) == {
        "yes": True,
        "port": 8080,
        "repo": install.REPO_URL,
        "ref": None,
    }


def test_parse_args_refuses_a_mistyped_flag() -> None:
    """A typo is a programming error the operator would never see otherwise."""
    with pytest.raises(SystemExit):
        install.parse_args(["--yes", "--prot", "8080"])
    with pytest.raises(SystemExit):
        install.parse_args(["--yes", "-p", "8080"])


def test_parse_args_refuses_a_unicode_digit_port() -> None:
    """isdigit() accepts '²' but int() rejects it; the guard must not traceback."""

    def _raises() -> None:
        install.parse_args(["--port", "²"])

    with pytest.raises(SystemExit):
        _raises()


def test_parse_args_refuses_ports_outside_the_range() -> None:
    """0 and 99999 pass isdigit() and int(); only the range stops them."""
    for bad in ("0", "99999", "0000"):
        with pytest.raises(SystemExit):
            install.parse_args(["--port", bad])
    with pytest.raises(SystemExit):
        install.parse_args(["--port=0"])


def test_parse_args_accepts_the_equals_form_for_repo_and_ref() -> None:
    parsed = install.parse_args(["--repo=/srv/a.git", "--ref=v1.0"])
    assert parsed["repo"] == "/srv/a.git"
    assert parsed["ref"] == "v1.0"


def test_a_chosen_port_must_be_in_range() -> None:
    with pytest.raises(SystemExit):
        install.resolve_port(probe=lambda p: True, chosen=0)
    with pytest.raises(SystemExit):
        install.resolve_port(probe=lambda p: True, chosen=99999)


def test_a_chosen_port_that_is_in_use_is_refused() -> None:
    """--port must not land on a busy port and fail 90 seconds later."""
    with pytest.raises(SystemExit):
        install.resolve_port(probe=lambda p: False, chosen=8080)


def test_enter_at_the_port_prompt_is_probed_before_accepting_8080() -> None:
    """Entering the offered 8080 must be checked just as typing it would be."""
    answers = iter(["", "9000"])

    def ask(_prompt: str) -> str:
        return next(answers)

    assert install.resolve_port(probe=lambda p: p == 9000, ask=ask) == 9000


def test_the_uv_install_step_reports_curl_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pipeline must return curl's status, not sh's: a failed download must
    read as a failure at this step, not surface four steps later as a missing
    uv."""
    code, calls = _run_main(monkeypatch)
    assert code == 0
    uv_step = calls[0][0]
    assert uv_step[0] == "sh"
    script = uv_step[2]
    assert "&&" in script
    assert "| sh" not in script
    assert "-o /tmp/uv-install.sh" in script


def test_the_uv_sync_step_steers_uvs_python_out_of_roots_home(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """uv installs its own Python under $HOME by default — /root for the root
    running this — and the service unit's ProtectHome=true would mask it. The
    sync step must point uv elsewhere."""
    code, calls = _run_main(monkeypatch)
    assert code == 0
    sync = next(c for c in calls if c[0][:2] == ["/usr/bin/uv", "sync"])
    env = sync[1]
    assert env is not None
    assert env["UV_PYTHON_INSTALL_DIR"] == install.UV_PYTHON_INSTALL_DIR


def test_the_plan_names_where_uvs_python_lands() -> None:
    """The one line that explains why the sandbox is not weakened is worth
    putting in front of the operator."""
    assert "uv-python" in install.render_plan(8080)


def test_main_turns_an_unanswerable_port_question_into_a_message(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The terminal-less --yes path must refuse, not traceback, when it has to
    ask about a port nobody chose."""
    monkeypatch.setattr(install, "observe_host", _ok)
    monkeypatch.setattr(install, "INSTALL_DIR", "/tmp/arraysense-audit-install")

    def no_tty(**kwargs: object) -> int:
        raise install.NoTerminal()

    monkeypatch.setattr(install, "resolve_port", no_tty)
    assert install.main(["--yes"]) == 1
    out = capsys.readouterr().out
    assert "no controlling terminal" in out
    assert "--port" in out


def test_the_backup_unit_makes_no_false_clock_claim() -> None:
    """After=time-sync.target gave no clock guarantee (the target is passive,
    and nothing that could order before it is enabled), so the unit must not
    claim it does; the comment stating it as fact was the defect."""
    text = (
        Path(__file__).resolve().parents[1] / "packaging" / "arraysense-backup.service"
    ).read_text()
    assert "Wants=time-sync.target" not in text
    assert "After=arraysense.service time-sync.target" not in text


def _packaging(name: str) -> str:
    return (Path(__file__).resolve().parents[1] / "packaging" / name).read_text()


def test_the_backup_timer_asks_often_and_lets_the_settings_decide() -> None:
    """The schedule moved into the settings, so the timer's job is only to ask.

    A daily OnCalendar would put the hour back in a file only root can edit,
    which is the thing the setting replaced. Persistent stays: a machine that
    was off must back up when it returns rather than skip the day.
    """
    text = _packaging("arraysense-backup.timer")
    assert "OnCalendar=*-*-* *:00/15:00" in text
    assert "Persistent=true" in text
    # Jitter would make the configured minute a lie for no benefit — nothing
    # here contends for a network resource, and the CLI decides when to run.
    # The directive, not the word: the comment explains its own absence.
    assert not [line for line in text.splitlines() if line.startswith("RandomizedDelaySec")]


def test_the_backup_unit_runs_the_scheduled_mode() -> None:
    """Without --scheduled the unit would back up every fifteen minutes."""
    assert "manage.py backup --scheduled" in _packaging("arraysense-backup.service")


def test_the_service_can_prove_the_default_backup_directory_is_writable() -> None:
    """The settings write path refuses a destination it cannot write to, and
    under ProtectSystem=strict a directory outside the unit's writable set is
    read-only however good its permissions are. Without this line the service
    would refuse its own default destination. The leading '-' keeps a machine
    that never installed the backup fragments from failing to start."""
    assert "ReadWritePaths=-/var/backups/arraysense" in _packaging("arraysense.service")


def test_unit_text_reads_the_unit_from_the_clone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unit travels with the source, so this checkout is the source."""
    monkeypatch.setattr(install, "INSTALL_DIR", str(Path(__file__).resolve().parents[1]))
    text = install.unit_text()
    assert "[Unit]" in text
    assert "Solar ArraySense" in text


def test_a_failed_file_write_is_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The unit and drop-in writes used to be bare open() calls; a read-only
    /etc or a full disk would traceback at the worst possible point."""

    def refuse(path: str, mode: str = "w") -> Any:
        raise OSError("no space left on device")

    monkeypatch.setattr("builtins.open", refuse)
    with pytest.raises(SystemExit) as exc:
        install._write_file("/etc/systemd/system/arraysense.service", "[Unit]\n")
    assert "arraysense.service" in str(exc.value)


def test_a_missing_packaging_file_is_a_message_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "INSTALL_DIR", "/nonexistent/clone")
    with pytest.raises(SystemExit) as exc:
        install._packaging_file("arraysense.service")
    assert "arraysense.service" in str(exc.value)


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


def test_the_plan_discloses_the_capability_for_any_privileged_port() -> None:
    """The drop-in grants the capability below 1024, so the plan must say so
    for 443 just as it did for 80 — a privilege grant is the line a
    security-minded reader is looking for."""
    assert "CAP_NET_BIND_SERVICE" in install.render_plan(443)
    assert "CAP_NET_BIND_SERVICE" not in install.render_plan(8080)


def test_the_capability_line_sits_with_the_actions_not_after_the_paragraph() -> None:
    """insert(-1) put the bullet after the 'no config file' paragraph; the one
    line that discloses a privilege grant must read as one of the actions."""
    plan = install.render_plan(80)
    assert plan.index("CAP_NET_BIND_SERVICE") < plan.index("It will NOT write")


def test_the_docstring_no_longer_claims_no_further_scripts() -> None:
    """The old opening lied: the installer downloads and runs uv's installer.
    That claim was the stated mitigation for piping the script into root, so a
    regression here matters more than a docstring."""
    assert "downloads no further scripts" not in (install.__doc__ or "")
    assert "uv" in (install.__doc__ or "")


def test_the_disk_refusal_no_longer_quotes_the_wrong_growth_figure() -> None:
    """52 MB a day was the disk-write volume restated as file growth, ~10x."""
    refusal = install.preflight(**_ok(free_bytes=512 * 1024**2))
    assert refusal is not None
    assert "52 MB" not in refusal.remedy
    assert "MB a day" in refusal.remedy


def test_the_shim_runs_manage_under_the_system_interpreter() -> None:
    """Never the virtualenv: upgrade rebuilds it while this is running."""
    shim = install.shim_text()
    assert "/opt/arraysense/src/arraysense/manage.py" in shim
    assert ".venv" not in shim


def test_the_shim_finds_uv_before_running_manage() -> None:
    """sudo runs `arraysense upgrade` with a PATH that has no uv, and the
    upgrade needs uv; the shim must put uv's directory in front."""
    shim = install.shim_text()
    assert "for d in /root/.local/bin" in shim
    assert 'PATH="$d:$PATH"' in shim
    assert "manage.py" in shim


def test_find_uv_checks_the_candidate_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "UV_CANDIDATES", (str(uv),))
    assert install.find_uv() == str(uv)


def test_find_uv_checks_a_non_root_users_local_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production install's uv lives under /home/*/.local/bin; the
    installer running as root must still find it."""
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(install, "UV_CANDIDATES", ())

    def fake_glob(pattern: str) -> list[str]:
        return [str(uv)] if "/home/" in pattern else []

    monkeypatch.setattr(glob, "glob", fake_glob)
    assert install.find_uv() == str(uv)


def test_main_refuses_a_repeat_run_with_the_repair_remedy_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A run over an existing clone points at the remedy that exists in that
    state — removing the clone — before it suggests the command that may not."""
    monkeypatch.setattr(install, "observe_host", _ok)
    monkeypatch.setattr(install, "INSTALL_DIR", "/tmp/arraysense-audit-install")
    monkeypatch.setattr(os.path, "isdir", lambda p: True)
    assert install.main(["--yes"]) == 1
    out = capsys.readouterr().out
    assert out.index("remove") < out.index("arraysense upgrade")


def test_main_checks_out_a_pinned_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    code, calls = _run_main(monkeypatch, ["--yes", "--ref", "8861c77"])
    assert code == 0
    checkouts = [c[0] for c in calls if c[0][:2] == ["git", "-C"]]
    assert checkouts == [["git", "-C", install.INSTALL_DIR, "checkout", "8861c77"]]


def test_main_installs_and_enables_the_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-line install must leave a daily backup running, not a promise."""
    code, calls = _run_main(monkeypatch)
    assert code == 0
    enables = [c[0] for c in calls if c[0][:2] == ["systemctl", "enable"]]
    assert ["systemctl", "enable", "--now", "arraysense"] in enables
    assert ["systemctl", "enable", "--now", "arraysense-backup.timer"] in enables
    assert any(c[0][0] == "systemd-tmpfiles" for c in calls)


def test_the_backup_fragments_land_in_systemds_directories() -> None:
    assert install.BACKUP_FILES == (
        ("arraysense-backup.service", "/etc/systemd/system/arraysense-backup.service"),
        ("arraysense-backup.timer", "/etc/systemd/system/arraysense-backup.timer"),
        ("arraysense-backup.tmpfiles.conf", "/etc/tmpfiles.d/arraysense-backup.conf"),
    )


def test_the_plan_names_the_backup_install() -> None:
    """The plan is the disclosure; a backup that installs unannounced is not."""
    plan = install.render_plan(8080)
    assert "backup" in plan
    assert "start at boot" in plan


def test_main_stops_when_enable_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An install that will not survive a reboot must not report success."""
    code, _ = _run_main(
        monkeypatch,
        step_codes={("systemctl", "enable", "--now", "arraysense"): 1},
    )
    assert code == 1
    assert "not enabled for boot" in capsys.readouterr().out


def test_main_stops_when_daemon_reload_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _ = _run_main(
        monkeypatch,
        step_codes={("systemctl", "daemon-reload"): 1},
    )
    assert code == 1
    assert "daemon-reload" in capsys.readouterr().out


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
    text = install.render_handoff(8080, "pi", local=True)
    assert "http://pi.local:8080" in text
    assert "None" not in text


def test_the_handoff_omits_the_local_line_when_mdns_is_absent() -> None:
    """A .local name that resolves nowhere is the same kind of guess the handoff
    refuses to make for the IP line."""
    text = install.render_handoff(8080, "pi", local=False)
    assert "pi.local" not in text
    assert "Open this" in text


def test_the_handoff_uses_the_short_hostname() -> None:
    """An FQDN hostname must not render as box.example.com.local."""
    text = install.render_handoff(8080, "box.example.com", local=True)
    assert "http://box.local:8080" in text


def test_the_handoff_says_either_only_when_there_are_two_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "outbound_ip", lambda: "192.168.1.5")
    assert "either of these" in install.render_handoff(8080, "pi", local=True)
    only_ip = install.render_handoff(8080, "pi", local=False)
    assert "either of these" not in only_ip
    assert "http://192.168.1.5:8080" in only_ip


def test_mdns_active_reports_the_systemctl_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(install, "_step", lambda argv, **kw: 0 if "avahi-daemon" in argv else 1)
    assert install.mdns_active() is True
    monkeypatch.setattr(install, "_step", lambda argv, **kw: 1)
    assert install.mdns_active() is False


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
    plan = install.clone_argv(repo="https://example.invalid/a.git")
    assert "--depth" not in plan


def test_the_clone_step_does_not_branch_and_the_checkout_pins_the_ref() -> None:
    """--ref may name a commit, which git clone --branch refuses; the ref is
    applied with a checkout in the clone instead, which takes all three."""
    clone = install.clone_argv(repo="https://example.invalid/a.git")
    assert "--branch" not in clone
    assert install.checkout_argv("8861c77") == [
        "git",
        "-C",
        install.INSTALL_DIR,
        "checkout",
        "8861c77",
    ]


def test_a_pinned_commit_clones_and_checks_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented --ref COMMIT form, end to end against a real repository:
    git clone --branch refuses a bare commit, so the installer clones and then
    checks out, and that must actually pin the earlier commit."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "file.txt").write_text("one\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.invalid"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "one"], cwd=source, check=True)
    first = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=source, check=True, capture_output=True, text=True
    ).stdout.strip()
    (source / "file.txt").write_text("two\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-qm", "two"], cwd=source, check=True)

    monkeypatch.setattr(install, "INSTALL_DIR", str(tmp_path / "target"))
    assert subprocess.run(install.clone_argv(repo=str(source)), check=True).returncode == 0
    assert subprocess.run(install.checkout_argv(first), check=True).returncode == 0
    assert (tmp_path / "target" / "file.txt").read_text() == "one\n"
