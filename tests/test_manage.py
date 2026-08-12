"""test_manage.py — the management CLI: service control, health, upgrade, rollback."""

from __future__ import annotations

import sqlite3
import subprocess
from typing import Any

import pytest

from arraysense import manage
from arraysense.store import schema


def test_health_returns_the_body_once_the_collector_is_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Healthy means the collector answered, not that the port opened.

    systemctl returns the moment the unit is active, which is before the
    service binds and long before the inverter has been reached. An upgrade
    that trusted that would call a dead collector a success.
    """
    replies = [
        None,
        {"running": True, "connected": False, "staleness": {"verdict": "stale"}},
        {"running": True, "connected": True, "staleness": {"verdict": "fresh"}},
    ]

    def fake_probe(url: str, timeout: float) -> Any:
        return replies.pop(0)

    monkeypatch.setattr(manage, "_probe", fake_probe)
    monkeypatch.setattr("arraysense.manage.time.sleep", lambda _s: None)
    body = manage.wait_until_healthy(8080, timeout=90.0, sleep=0.0)
    assert body is not None
    assert body["connected"] is True
    assert replies == [], "it must keep polling until the collector is live"


def test_health_gives_up_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout returns None rather than a body nobody checked."""
    monkeypatch.setattr(manage, "_probe", lambda url, timeout: None)
    ticks = iter([0.0, 30.0, 60.0, 91.0])
    monkeypatch.setattr("arraysense.manage.time.monotonic", lambda: next(ticks))
    monkeypatch.setattr("arraysense.manage.time.sleep", lambda _s: None)
    assert manage.wait_until_healthy(8080, timeout=90.0, sleep=0.0) is None


def test_status_reports_absent_facts_as_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A database with no rows reports None for its range, never a fake date.

    This is the project's oldest rule reaching the CLI: a missing reading is
    absent, and a support answer that invented a date would send somebody
    looking for data that was never there.
    """
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()

    facts = manage.database_facts(str(db))
    assert facts["first"] is None
    assert facts["last"] is None
    assert facts["bytes"] > 0


def test_status_reports_a_real_date_range_from_the_real_schema(
    tmp_path: Any,
) -> None:
    """Two real rows yield a real range, through the project's own DDL.

    A hand-built table could drift from the real schema and keep passing;
    building it from ddl_for means a renamed table or timestamp column breaks
    this test, which is the regression it exists to catch. The gap is asserted
    in days rather than as a hard-coded date because database_facts renders
    local dates deliberately, and one instant's local date depends on the zone
    the runner happens to be in.
    """
    import datetime as _dt
    from datetime import date

    db = tmp_path / "full.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)",
        (1783512004, "CE12345678"),
    )
    conn.execute(
        "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)",
        (1783512004 + 10 * 86400, "CE12345678"),
    )
    conn.commit()
    conn.close()

    facts = manage.database_facts(str(db))
    assert facts["first"] != facts["last"]
    first = date.fromisoformat(facts["first"])
    last = date.fromisoformat(facts["last"])
    assert (last - first).days == 10
    assert facts["first"] == _dt.datetime.fromtimestamp(1783512004).date().isoformat()


def test_status_reads_the_port_from_the_unit_drop_in(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The port lives in the drop-in the installer wrote, so status finds it there."""
    drop = tmp_path / "port.conf"
    drop.write_text(
        "[Service]\nExecStart=\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8099\n"
    )
    monkeypatch.setattr(manage, "PORT_DROPIN", str(drop))
    assert manage.configured_port() == 8099


def test_status_ignores_a_commented_port_line(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commented-out ExecStart must not win over the real one.

    The old scan matched '--port' anywhere on the line, so a commented
    'Was: ExecStart=... --port 8080' beat the live '--port 8099' and status
    probed a port nothing listened on.
    """
    drop = tmp_path / "port.conf"
    drop.write_text(
        "[Service]\n"
        "# Was: ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8080\n"
        "ExecStart=\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8099\n"
    )
    monkeypatch.setattr(manage, "PORT_DROPIN", str(drop))
    assert manage.configured_port() == 8099


def test_status_takes_the_last_execstart_line(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final ExecStart in a drop-in wins, exactly as systemd resolves it.

    Each assignment replaces the previous one, so status must probe the port on
    the last line, not the first.
    """
    drop = tmp_path / "port.conf"
    drop.write_text(
        "[Service]\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8080\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8099\n"
    )
    monkeypatch.setattr(manage, "PORT_DROPIN", str(drop))
    assert manage.configured_port() == 8099


def test_status_defaults_the_port_when_no_drop_in_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "PORT_DROPIN", "/nonexistent/port.conf")
    assert manage.configured_port() == 8080


def test_status_reads_a_path_with_an_inline_comment(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An inline comment after the value must not leak into the returned path.

    Without cutting at the '#', the comment survives quote-stripping and the
    path comes back with '  # on the SSD' appended — a file that does not exist.
    """
    conf = tmp_path / "config.toml"
    conf.write_text('database_path = "/var/lib/arraysense/arraysense.db"  # on the SSD\n')
    monkeypatch.setattr(manage, "CONFIG_PATH", str(conf))
    assert manage._database_path() == "/var/lib/arraysense/arraysense.db"


def test_driver_line_names_the_family_and_what_it_declares() -> None:
    """The one line a support conversation starts from."""
    line = manage.driver_line(
        {
            "devices": [
                {
                    "device": "CE12345678",
                    "driver": "eg4_luxpower",
                    "model": "18kPV",
                    "pv_strings": 3,
                    "energy": "counters",
                    "transport": "dongle",
                }
            ]
        }
    )
    assert "eg4_luxpower" in line
    assert "18kPV" in line
    assert "CE12345678" in line
    assert "3 PV strings" in line


def test_driver_line_leaves_an_undeclared_field_blank() -> None:
    """A source that names its device but declares nothing must not read as a
    guess. This is the project's absent-data rule reaching the terminal."""
    line = manage.driver_line(
        {
            "devices": [
                {
                    "device": "CE12345678",
                    "driver": None,
                    "model": None,
                    "pv_strings": None,
                    "energy": None,
                    "transport": None,
                }
            ]
        }
    )
    assert "CE12345678" in line
    assert "0 PV strings" not in line
    assert line.count("—") == 5


def test_driver_line_says_so_when_the_endpoint_cannot_be_read() -> None:
    assert manage.driver_line(None) == "driver:    unavailable"


def test_driver_line_with_no_devices_at_all() -> None:
    assert manage.driver_line({"devices": []}) == "driver:    none declared"


def test_an_unknown_subcommand_lists_the_real_ones() -> None:
    """A typo must not read as a failure of the program."""
    assert manage.main(["frobnicate"]) == 2


def test_restart_fails_loudly_when_the_service_does_not_come_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restart is not 'systemctl returned 0'. It is 'the collector answered'."""
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_healthy", lambda port, **kw: None)
    assert manage.cmd_restart([]) == 1


def test_restart_returns_zero_when_the_collector_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, a cmd_restart that always failed would pass the suite."""
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(manage, "wait_until_healthy", lambda port, **kw: {"running": True})
    assert manage.cmd_restart([]) == 0


def test_restart_gives_up_when_systemctl_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """systemctl failing must not be reported through the health check, which
    would blame a ninety-second timeout for something that failed at once."""
    calls: list[str] = []

    def record_poll(port: int, **kw: object) -> None:
        calls.append("polled")

    monkeypatch.setattr(manage, "service", lambda action: False)
    monkeypatch.setattr(manage, "wait_until_healthy", record_poll)
    assert manage.cmd_restart([]) == 1
    assert calls == [], "a failed systemctl must not then wait 90s for a health check"


def test_no_arguments_runs_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bare `arraysense` is what somebody types first, so it must do the
    harmless, informative thing rather than print usage."""
    seen: list[list[str]] = []

    def fake_status(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setitem(manage.COMMANDS, "status", fake_status)
    assert manage.main([]) == 0
    assert seen == [[]]


def test_logs_forwards_what_the_user_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_call(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(subprocess, "call", fake_call)

    manage.cmd_logs(["-f"])
    assert seen[-1] == ["journalctl", "-u", manage.SERVICE, "-n", "200", "-f"]

    manage.cmd_logs(["-n", "50"])
    assert seen[-1] == ["journalctl", "-u", manage.SERVICE, "-n", "50"]
    assert "200" not in seen[-1]


def test_usage_lists_only_commands_that_actually_run(capsys: Any) -> None:
    """Usage that advertises a command the parser rejects is worse than silence.

    An earlier draft carried None placeholders for commands not yet written;
    they appeared in this line and then failed as typos.
    """
    assert manage.main(["frobnicate"]) == 2
    usage = capsys.readouterr().out
    for name in manage.COMMANDS:
        assert name in usage
        assert callable(manage.COMMANDS[name])


def test_upgrade_rolls_back_when_the_collector_does_not_return(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point. A failed upgrade must leave a collecting service.

    Safe here for a specific reason rather than a hopeful one: migrations only
    ever add columns, so the older code coming back selects only the columns it
    knows and anything the newer version added sits inert. The code rolls back
    without the database having to.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> Any:
        calls.append(argv)
        out = "abc1234" if "rev-parse" in argv else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "is_dirty", lambda: False)
    monkeypatch.setattr(manage, "_pending_commits", lambda: ["deadbee new thing"])
    monkeypatch.setattr(manage, "_confirm", lambda _prompt: True)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_healthy", lambda port, **kw: None)

    assert manage.cmd_upgrade([]) == 1
    checkouts = [c for c in calls if "checkout" in c]
    assert checkouts, "a failed health check must check the old commit back out"
    assert "abc1234" in checkouts[-1]


def test_upgrade_says_so_when_the_rollback_checkout_itself_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollback that could not happen must never be reported as one.

    The health check can pass right after a failed checkout, because what is
    still checked out is the new code that just failed for a different reason.
    Reporting 'rolled back' there tells the owner they are running the old
    release when they are not.
    """

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, "abc1234def\n", "")
        if "checkout" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "error: pathspec did not match")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "is_dirty", lambda: False)
    monkeypatch.setattr(manage, "_pending_commits", lambda: ["deadbee a release"])
    monkeypatch.setattr(manage, "_incoming_changelog", lambda: "")
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(manage, "service", lambda action: True)
    healths = iter([None, {"ok": True}])
    monkeypatch.setattr(manage, "wait_until_healthy", lambda port, **kw: next(healths))

    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    )

    assert manage.cmd_upgrade([]) == 1
    said = "\n".join(printed)
    assert "ROLLBACK FAILED" in said
    assert "rolled back; running" not in said, "it must not claim a rollback that did not happen"


def test_a_dependency_failure_is_not_reported_as_a_dead_collector(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dependency that will not install must be named, not blamed on the collector.

    The old bool return collapsed 'uv sync failed' and 'collector never answered'
    into the same message, sending somebody to read the wrong log.
    """

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv and argv[0] == "uv":
            return subprocess.CompletedProcess(argv, 1, "", "no solution found")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_healthy", lambda port, **kw: {"ok": True})
    reason = manage._sync_and_restart(8080)
    assert reason is not None
    assert "dependencies" in reason


def test_upgrade_refuses_a_dirty_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Someone who hand-edited /opt/arraysense finds out now, not mid-merge."""
    monkeypatch.setattr(manage, "is_dirty", lambda: True)
    assert manage.cmd_upgrade([]) == 1


def test_upgrade_stops_when_there_is_nothing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "is_dirty", lambda: False)
    monkeypatch.setattr(manage, "_pending_commits", lambda: [])
    assert manage.cmd_upgrade([]) == 0


def test_the_changelog_entry_shown_is_the_incoming_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release that needs a migration says so at the top of its entry, and
    this is the only place the owner sees that before agreeing."""
    text = (
        "# Changelog\n\npreamble\n\n"
        "## 0.8.0 \u2014 12 August 2026\n\n### Added\n- a thing\n\n"
        "## 0.7.3 \u2014 11 August 2026\n\n### Fixed\n- an older thing\n"
    )
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, text, ""))
    entry = manage._incoming_changelog()
    assert entry.startswith("## 0.8.0")
    assert "a thing" in entry
    assert "0.7.3" not in entry


def test_a_missing_changelog_is_absent_not_invented(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changelog that cannot be read reads as no entry at all, never as an
    invented one \u2014 the project's absent-data rule reaching the CLI."""
    monkeypatch.setattr(
        manage,
        "run",
        lambda argv: subprocess.CompletedProcess(argv, 1, "", "no such path"),
    )
    assert manage._incoming_changelog() == ""


def test_a_fenced_heading_inside_the_entry_does_not_truncate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A release note quoting a fenced block must not lose the text after it.

    The truncation bug stops at the first '## ' anywhere, including one inside a
    ``` fence — and what it loses may be the migration warning.
    """
    text = (
        "# Changelog\n\npreamble\n\n"
        "## 0.8.0 \u2014 12 August 2026\n\n### Added\n- a thing\n"
        "```markdown\n"
        "## this heading is quoted inside the fence, not the next entry\n"
        "```\n"
        "- the migration warning\n\n"
        "## 0.7.3 \u2014 11 August 2026\n\n### Fixed\n- an older thing\n"
    )
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, text, ""))
    entry = manage._incoming_changelog()
    assert "migration warning" in entry
    assert "0.7.3" not in entry


def test_uninstall_never_removes_the_database_without_purge(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Years of readings must survive someone removing the software.

    The database is the only thing here that cannot be reinstalled, so removing
    it takes a second, explicit act.
    """
    db = tmp_path / "arraysense.db"
    db.write_bytes(b"x")
    removed: list[str] = []
    monkeypatch.setattr(manage, "_database_path", lambda: str(db))
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)

    def fake_remove(path: str) -> bool:
        removed.append(path)
        return True

    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))

    manage.cmd_uninstall([])
    assert str(db) not in removed
    assert db.exists()

    manage.cmd_uninstall(["--purge"])
    assert str(db) in removed


def test_uninstall_removes_the_service_the_code_and_the_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half of the promise: what it says it removes, it removes."""
    removed: list[str] = []
    actions: list[str] = []

    def fake_remove(path: str) -> bool:
        removed.append(path)
        return True

    def fake_service(action: str) -> bool:
        actions.append(action)
        return True

    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    monkeypatch.setattr(manage, "service", fake_service)
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)
    monkeypatch.setattr(manage, "_database_path", lambda: "/var/lib/arraysense/arraysense.db")
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))

    assert manage.cmd_uninstall([]) == 0
    assert manage.UNIT_PATH in removed
    assert manage.DROPIN_DIR in removed
    assert manage.CLI_SHIM in removed
    assert manage.INSTALL_DIR in removed
    assert actions == ["stop", "disable"]


def test_uninstall_purge_takes_the_wal_and_shm_sidecars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A purge that leaves arraysense.db-wal has left readings behind in a file
    the owner believes is gone."""
    removed: list[str] = []

    def fake_remove(path: str) -> bool:
        removed.append(path)
        return True

    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)
    monkeypatch.setattr(manage, "_database_path", lambda: "/db/a.db")
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))

    manage.cmd_uninstall(["--purge"])
    assert "/db/a.db" in removed
    assert "/db/a.db-wal" in removed
    assert "/db/a.db-shm" in removed
    assert "/etc/arraysense" in removed


def test_uninstall_declined_does_nothing_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answering no must not stop the service either."""
    touched: list[str] = []

    def fake_remove(path: str) -> bool:
        touched.append(path)
        return True

    def fake_service(action: str) -> bool:
        touched.append(action)
        return True

    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    monkeypatch.setattr(manage, "service", fake_service)
    monkeypatch.setattr(manage, "_confirm", lambda _p: False)
    assert manage.cmd_uninstall([]) == 0
    assert touched == []


def test_purge_refuses_a_database_path_that_is_a_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A purge is authorised against one file. A directory there would turn it
    into a recursive delete running as root."""
    removed: list[str] = []

    def fake_remove(path: str) -> bool:
        removed.append(path)
        return True

    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    assert manage._remove_database(str(tmp_path)) is False
    assert removed == []


def test_purge_refuses_an_empty_or_relative_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed config yields an empty string, and -wal would then resolve
    against the working directory."""
    removed: list[str] = []

    def fake_remove(path: str) -> bool:
        removed.append(path)
        return True

    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    assert manage._remove_database("") is False
    assert manage._remove_database("relative/a.db") is False
    assert removed == []


def test_purge_removes_the_database_and_both_sidecars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A purge that leaves a sidecar behind has left readings in a file the
    owner believes is gone."""
    db = tmp_path / "a.db"
    for name in ("a.db", "a.db-wal", "a.db-shm"):
        (tmp_path / name).write_bytes(b"x")
    assert manage._remove_database(str(db)) is True
    assert not (tmp_path / "a.db").exists()
    assert not (tmp_path / "a.db-wal").exists()
    assert not (tmp_path / "a.db-shm").exists()


def test_uninstall_refuses_to_remove_anything_when_the_stop_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A still-writing collector is the one thing a purge must not race.

    If the stop fails, the collector may still be appending when the purge
    deletes the database and its WAL — the one moment in this program where a
    still-running writer matters — so nothing at all is removed."""
    touched: list[str] = []

    def fake_remove(path: str) -> bool:
        touched.append(path)
        return True

    def fake_service(action: str) -> bool:
        return action != "stop"

    monkeypatch.setattr(manage, "service", fake_service)
    monkeypatch.setattr(manage, "_remove_path", fake_remove)
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)
    monkeypatch.setattr(manage, "_database_path", lambda: "/db/a.db")

    assert manage.cmd_uninstall(["--purge"]) == 1
    assert touched == []


def test_uninstall_says_so_when_daemon_reload_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reload that fails leaves systemd holding a stale unit reference.

    The uninstall must say so rather than print the success line, or the owner
    leaves satisfied with a unit systemd still knows by name."""

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        if argv == ["systemctl", "daemon-reload"]:
            return subprocess.CompletedProcess(argv, 1, "", "Unit arraysense.service not found")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)
    monkeypatch.setattr(manage, "_remove_path", lambda p: True)
    monkeypatch.setattr(manage, "_database_path", lambda: "/db/a.db")

    printed: list[str] = []
    monkeypatch.setattr(
        "builtins.print", lambda *a, **k: printed.append(" ".join(str(x) for x in a))
    )

    assert manage.cmd_uninstall([]) == 1
    said = "\n".join(printed)
    assert "reload" in said.lower()
