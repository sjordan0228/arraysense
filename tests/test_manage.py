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
