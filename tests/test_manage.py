"""test_manage.py — the management CLI: service control, health, upgrade, rollback."""

from __future__ import annotations

import contextlib
import datetime
import gzip
import os
import shutil
import sqlite3
import subprocess
from typing import Any

import pytest

from arraysense import manage
from arraysense.store import schema


def test_a_backup_round_trips_every_row(tmp_path: Any) -> None:
    """The point of the whole thing: what comes back must be what went in."""
    source = tmp_path / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    for ts in range(1783512004, 1783512004 + 50):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)", (ts, "CE12345678")
        )
    conn.commit()
    conn.close()

    dest = tmp_path / "backups"
    dest.mkdir()
    written = manage.backup_now(str(source), str(dest), keep=14, stamp="2026-08-12")
    assert written is not None

    restored = tmp_path / "restored.db"
    with gzip.open(written, "rb") as src, open(restored, "wb") as out:
        shutil.copyfileobj(src, out)
    conn = sqlite3.connect(str(restored))
    assert conn.execute("SELECT COUNT(*) FROM inverter_raw").fetchone()[0] == 50
    conn.close()


def test_an_unreadable_source_writes_nothing_and_deletes_nothing(tmp_path: Any) -> None:
    """A run that could not read the database must not be mistaken for one that
    found nothing — and must never rotate a good backup away."""
    dest = tmp_path / "backups"
    dest.mkdir()
    keeper = dest / "arraysense-2026-08-01.db.gz"
    keeper.write_bytes(b"older backup")

    assert (
        manage.backup_now(str(tmp_path / "missing.db"), str(dest), keep=1, stamp="2026-08-12")
        is None
    )
    assert keeper.exists(), "an old backup must survive a failed run"


def test_rotation_keeps_the_newest_and_only_after_success(tmp_path: Any) -> None:
    source = tmp_path / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()

    dest = tmp_path / "backups"
    dest.mkdir()
    for day in ("2026-08-01", "2026-08-02", "2026-08-03"):
        (dest / f"arraysense-{day}.db.gz").write_bytes(b"x")

    manage.backup_now(str(source), str(dest), keep=2, stamp="2026-08-12")
    names = sorted(p.name for p in dest.glob("arraysense-*.db.gz"))
    assert names == ["arraysense-2026-08-03.db.gz", "arraysense-2026-08-12.db.gz"]


def test_no_half_written_backup_is_left_behind(tmp_path: Any) -> None:
    """The compressed file is renamed into place, so an interrupted run cannot
    leave something that looks finished."""
    source = tmp_path / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()
    dest = tmp_path / "backups"
    dest.mkdir()
    manage.backup_now(str(source), str(dest), keep=14, stamp="2026-08-12")
    assert list(dest.glob("*.part")) == []


def test_the_working_copy_is_made_beside_the_source_not_the_destination(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destination is an SD card on the reference installation, and the
    uncompressed copy is six times the size of the compressed one. Writing it
    to the card would undo the reason the database lives on a separate disk."""
    source_dir = tmp_path / "ssd"
    source_dir.mkdir()
    source = source_dir / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()

    dest = tmp_path / "card"
    dest.mkdir()
    seen: list[str] = []
    real_connect: Any = sqlite3.connect

    def watching_connect(target: str, *a: Any, **k: Any) -> Any:
        seen.append(str(target))
        return real_connect(target, *a, **k)

    # manage.sqlite3 is the same module object as sqlite3 here, so patching the
    # module patches what backup_now sees.
    monkeypatch.setattr(sqlite3, "connect", watching_connect)
    manage.backup_now(str(source), str(dest), keep=14, stamp="2026-08-12")
    working = [s for s in seen if s.endswith(".tmp") or "backup" in os.path.basename(s)]
    assert all(not s.startswith(str(dest)) for s in working), seen


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


def test_a_service_in_setup_mode_is_up_not_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh install has no config, serves only /api/setup, and is healthy.

    Treating that as 'down' made the installer report failure on every
    successful first install.
    """

    def fake_probe(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
        return {"first_run": True, "version": "0.7.3"} if "setup" in url else None

    monkeypatch.setattr(manage, "_probe", fake_probe)
    state, body = manage.service_state(8080)
    assert state == "setup"
    assert body is not None and body["first_run"] is True


def test_a_collecting_service_is_reported_as_collecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_probe(url: str, timeout: float = 5.0) -> dict[str, Any] | None:
        return {"running": True, "connected": True} if "status" in url else None

    monkeypatch.setattr(manage, "_probe", fake_probe)
    assert manage.service_state(8080)[0] == "collecting"


def test_nothing_listening_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "_probe", lambda url, timeout=5.0: None)
    assert manage.service_state(8080) == ("down", None)


def test_wait_until_up_accepts_setup_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installer's verification runs through here, and a first install
    always lands in setup mode: that must count as up."""
    states: list[tuple[str, dict[str, Any] | None]] = [
        ("down", None),
        ("setup", {"first_run": True}),
    ]

    def fake_state(port: int, timeout: float = 5.0) -> tuple[str, dict[str, Any] | None]:
        return states.pop(0)

    monkeypatch.setattr(manage, "service_state", fake_state)
    monkeypatch.setattr("arraysense.manage.time.sleep", lambda _s: None)
    state, body = manage.wait_until_up(8080, timeout=90.0, sleep=0.0)
    assert state == "setup"
    assert body is not None and body["first_run"] is True


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
    assert facts["readable"] is True


def test_an_unreadable_database_is_not_reported_as_empty(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database that could not be opened must never read as one with no rows.

    Measured on the reference installation: the file is mode 0640 owned by the
    service user, so an ordinary user got "empty" for 668 days of history.
    """
    if os.geteuid() == 0:
        pytest.skip("root can read a 0000 file, so this cannot be exercised")
    db = tmp_path / "locked.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()
    db.chmod(0o000)

    facts = manage.database_facts(str(db))
    assert facts["bytes"] > 0
    assert facts["readable"] is False
    assert facts["first"] is None


def test_an_empty_database_is_readable_and_says_so(tmp_path: Any) -> None:
    """An empty file that opened is a measured absence; only an unopened one is
    not. The distinction is the whole reason readable exists as a fact."""
    db = tmp_path / "empty.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()

    facts = manage.database_facts(str(db))
    assert facts["readable"] is True
    assert facts["first"] is None


def test_status_says_unreadable_rather_than_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A database that could not be read must say so, not claim it is empty."""
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(
        manage,
        "service_state",
        lambda port, timeout=5.0: ("collecting", {"version": "0.7.3", "running": True}),
    )
    monkeypatch.setattr(manage, "_probe", lambda url, timeout=5.0: None)
    monkeypatch.setattr(manage, "_database_path", lambda: "/nope/a.db")
    monkeypatch.setattr(
        manage,
        "database_facts",
        lambda path: {
            "bytes": 277057536,
            "first": None,
            "last": None,
            "readable": False,
            "reason": "could not stat /nope/a.db",
        },
    )
    manage.cmd_status([])
    out = capsys.readouterr().out
    assert "unreadable" in out
    assert "empty" not in out


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
    drop_dir = tmp_path / "dropins"
    drop_dir.mkdir()
    drop = drop_dir / "port.conf"
    drop.write_text(
        "[Service]\nExecStart=\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8099\n"
    )
    monkeypatch.setattr(manage, "DROPIN_DIR", str(drop_dir))
    assert manage.configured_port() == 8099


def test_status_ignores_a_commented_port_line(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commented-out ExecStart must not win over the real one.

    The old scan matched '--port' anywhere on the line, so a commented
    'Was: ExecStart=... --port 8080' beat the live '--port 8099' and status
    probed a port nothing listened on.
    """
    drop_dir = tmp_path / "dropins"
    drop_dir.mkdir()
    drop = drop_dir / "port.conf"
    drop.write_text(
        "[Service]\n"
        "# Was: ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8080\n"
        "ExecStart=\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8099\n"
    )
    monkeypatch.setattr(manage, "DROPIN_DIR", str(drop_dir))
    assert manage.configured_port() == 8099


def test_status_takes_the_last_execstart_line(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The final ExecStart in a drop-in wins, exactly as systemd resolves it.

    Each assignment replaces the previous one, so status must probe the port on
    the last line, not the first.
    """
    drop_dir = tmp_path / "dropins"
    drop_dir.mkdir()
    drop = drop_dir / "port.conf"
    drop.write_text(
        "[Service]\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8080\n"
        "ExecStart=/opt/arraysense/.venv/bin/python -m arraysense --port 8099\n"
    )
    monkeypatch.setattr(manage, "DROPIN_DIR", str(drop_dir))
    assert manage.configured_port() == 8099


def test_status_defaults_the_port_when_no_drop_in_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "DROPIN_DIR", "/nonexistent/dropins")
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
    """Restart is not 'systemctl returned 0'. It is 'the service answered'."""
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_up", lambda port, **kw: ("down", None))
    assert manage.cmd_restart([]) == 1


def test_restart_returns_zero_when_the_collector_comes_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without this, a cmd_restart that always failed would pass the suite."""
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(
        manage, "wait_until_up", lambda port, **kw: ("collecting", {"running": True})
    )
    assert manage.cmd_restart([]) == 0


def test_restart_succeeds_when_the_service_comes_back_in_setup_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installer's verification runs through here, and a first install
    always lands in setup mode."""
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(manage, "wait_until_up", lambda port, **kw: ("setup", {"first_run": True}))
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
    monkeypatch.setattr(manage, "wait_until_up", record_poll)
    assert manage.cmd_restart([]) == 1
    assert calls == [], "a failed systemctl must not then wait 90s for a health check"


def test_status_in_setup_mode_says_so_rather_than_not_answering(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A service waiting for its wizard is up, not down, and has no collector
    or driver to report — printing 'connected=False' would describe a fault
    that does not exist."""
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(
        manage, "service_state", lambda port, timeout=5.0: ("setup", {"version": "0.7.3"})
    )
    assert manage.cmd_status([]) == 0
    out = capsys.readouterr().out
    assert "wizard" in out.lower()
    assert "not answering" not in out
    assert "connected=" not in out


def test_version_reports_the_running_version_in_setup_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A service waiting for its wizard is answering, so version says so."""
    monkeypatch.setattr(
        manage,
        "run",
        lambda argv: subprocess.CompletedProcess(argv, 0, "abc1234\n", ""),
    )
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(
        manage, "service_state", lambda port, timeout=5.0: ("setup", {"version": "0.7.3"})
    )
    assert manage.cmd_version([]) == 0
    out = capsys.readouterr().out
    assert "0.7.3" in out
    assert "not answering" not in out


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


def test_upgrade_rolls_back_when_the_service_does_not_return(
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

    monkeypatch.setattr(manage, "find_uv", lambda: "uv")
    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "is_dirty", lambda: False)
    monkeypatch.setattr(manage, "_pending_commits", lambda: ["deadbee new thing"])
    monkeypatch.setattr(manage, "_confirm", lambda _prompt: True)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_up", lambda port, **kw: ("down", None))

    assert manage.cmd_upgrade([]) == 1
    checkouts = [c for c in calls if "checkout" in c]
    assert checkouts, "a service that never answers must check the old commit back out"
    assert "abc1234" in checkouts[-1]


def test_an_upgrade_is_not_rolled_back_because_the_inverter_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dongle takes one client, so an owner opening the EG4 app during an
    upgrade must not cause a rollback of a release that works."""
    monkeypatch.setattr(manage, "find_uv", lambda: "uv")
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(
        manage,
        "wait_until_up",
        lambda port, **kw: ("collecting", {"running": True, "connected": False}),
    )
    assert manage._sync_and_restart(8080) is None


def test_a_service_that_never_answers_is_still_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A service that never comes back is still the failure the rollback exists
    for; only an unreachable inverter was wrongly being treated as that failure."""
    monkeypatch.setattr(manage, "find_uv", lambda: "uv")
    monkeypatch.setattr(manage, "run", lambda argv: subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_up", lambda port, **kw: ("down", None))
    reason = manage._sync_and_restart(8080)
    assert reason is not None and "did not answer" in reason


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

    monkeypatch.setattr(manage, "find_uv", lambda: "uv")
    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "is_dirty", lambda: False)
    monkeypatch.setattr(manage, "_pending_commits", lambda: ["deadbee a release"])
    monkeypatch.setattr(manage, "_incoming_changelog", lambda: "")
    monkeypatch.setattr(manage, "_confirm", lambda _p: True)
    monkeypatch.setattr(manage, "configured_port", lambda: 8080)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(manage, "wait_until_up", lambda port, **kw: ("down", None))

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

    monkeypatch.setattr(manage, "find_uv", lambda: "uv")
    monkeypatch.setattr(manage, "run", fake_run)
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(
        manage, "wait_until_up", lambda port, **kw: ("collecting", {"running": True})
    )
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


def test_a_shallow_clone_is_unshallowed_before_comparing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shallow clone cannot fast-forward: git calls the histories unrelated.

    Installs made with --depth 1 could never be upgraded at all, which was
    measured on a real machine before this existed.
    """
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--is-shallow-repository" in argv:
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(manage, "run", fake_run)
    manage._pending_commits()
    assert any("--unshallow" in c for c in calls), "a shallow clone must be repaired"


def test_a_complete_clone_is_not_refetched(monkeypatch: pytest.MonkeyPatch) -> None:
    """--unshallow on a complete clone is a wasted download every upgrade."""
    calls: list[list[str]] = []

    def fake_run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "--is-shallow-repository" in argv:
            return subprocess.CompletedProcess(argv, 0, "false\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(manage, "run", fake_run)
    manage._pending_commits()
    assert not any("--unshallow" in c for c in calls)


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


def test_backup_prints_a_pointer_to_the_restore_command(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """After writing a backup, tell the operator to use the restore command."""
    source = tmp_path / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()
    dest = tmp_path / "backups"
    dest.mkdir()

    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    assert manage.cmd_backup(["--dir", str(dest)]) == 0
    out = capsys.readouterr().out
    assert "arraysense restore" in out
    assert "PRAGMA quick_check" not in out


def test_restore_returns_the_same_rows_as_were_backed_up(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: what comes out of restore matches what went into the backup."""
    db = tmp_path / "live.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    for ts in range(1783512004, 1783512004 + 100):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)", (ts, "CE12345678")
        )
    conn.commit()
    conn.close()

    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(db))
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(
        manage,
        "wait_until_up",
        lambda port, **kw: ("collecting", {"running": True, "connected": True}),
    )
    assert manage.cmd_backup(["--dir", str(dest)]) == 0
    archives = sorted(dest.glob("arraysense-*.db.gz"))
    assert len(archives) == 1

    # Restore into a fresh database — the target must exist because the
    # restore command renames it to .prev before placing the new file.
    restored = tmp_path / "restored.db"
    restored.write_bytes(b"old database placeholder")
    monkeypatch.setattr(manage, "_database_path", lambda: str(restored))
    result = manage.cmd_restore([str(archives[0]), "--yes"])
    assert result == 0

    conn = sqlite3.connect(str(restored))
    count = conn.execute("SELECT COUNT(*) FROM inverter_raw").fetchone()[0]
    assert count == 100
    conn.close()


def test_restore_refuses_a_corrupt_archive_and_preserves_the_live_database(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shell recipe's data-loss chain, proved impossible.

    gunzip exits 1 on a corrupt archive, the shell redirect creates a zero-byte
    file, PRAGMA quick_check prints "ok" on zero bytes, and the mv destroys the
    live database — 1000 rows replaced by an empty file. The restore command
    must refuse the archive before the live database is ever touched.
    """
    # Create a live database with 1000 rows
    db = tmp_path / "live.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    for ts in range(1000):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)",
            (1783512004 + ts * 60, "CE12345678"),
        )
    conn.commit()
    conn.close()

    # Create a corrupt archive — not gzip at all
    archive = tmp_path / "corrupt.db.gz"
    archive.write_bytes(b"this is not a gzip file")

    monkeypatch.setattr(manage, "_database_path", lambda: str(db))
    result = manage.cmd_restore([str(archive), "--yes"])
    assert result == 1, "restore must refuse a corrupt archive"

    # The live database must still have every row
    conn = sqlite3.connect(str(db))
    count = conn.execute("SELECT COUNT(*) FROM inverter_raw").fetchone()[0]
    assert count == 1000, f"live database was destroyed: {count} rows instead of 1000"
    conn.close()


def test_restore_refuses_an_archive_that_is_not_an_arraysense_database(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gzip file containing random bytes is not a database and must be refused."""
    db = tmp_path / "live.db"
    db.write_bytes(b"SQLite format 3\0...")
    archive = tmp_path / "random.db.gz"
    with gzip.open(archive, "wb") as fh:
        fh.write(b"not a sqlite database at all")

    monkeypatch.setattr(manage, "_database_path", lambda: str(db))
    result = manage.cmd_restore([str(archive), "--yes"])
    assert result == 1


def test_restore_refuses_an_empty_archive(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """An archive that unpacks to zero bytes must be refused."""
    db = tmp_path / "live.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()

    archive = tmp_path / "empty.db.gz"
    with gzip.open(archive, "wb"):
        pass  # write nothing

    monkeypatch.setattr(manage, "_database_path", lambda: str(db))
    result = manage.cmd_restore([str(archive), "--yes"])
    assert result == 1


def test_restore_refuses_an_archive_that_does_not_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path to nothing must be refused, not create a zero-byte file."""
    monkeypatch.setattr(manage, "_database_path", lambda: "/tmp/nonexistent-db.db")
    result = manage.cmd_restore(["/tmp/no-such-archive.db.gz", "--yes"])
    assert result == 1


def test_restore_asks_confirmation_unless_yes_is_given(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """Without --yes, the command must confirm before touching the live database."""
    db = tmp_path / "live.db"
    conn = sqlite3.connect(str(db))
    conn.execute(schema.ddl_for("inverter_raw"))
    for ts in range(10):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)",
            (1783512004 + ts * 60, "CE12345678"),
        )
    conn.commit()
    conn.close()

    archive = tmp_path / "test.db.gz"
    with gzip.open(archive, "wb") as fh:
        fh.write(db.read_bytes())

    monkeypatch.setattr(manage, "_database_path", lambda: str(db))
    # Decline confirmation
    monkeypatch.setattr(manage, "_confirm", lambda _p: False)
    result = manage.cmd_restore([str(archive)])
    assert result == 0, "a declined restore is not an error"
    assert "nothing done" in capsys.readouterr().out


def test_restore_removes_the_write_ahead_log_and_shm_sidecars(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale -wal is replayed over the restored file and silently undoes it.
    The restore command must remove both sidecars before placing the new file."""
    target = tmp_path / "restore_target.db"
    target.write_bytes(b"old database placeholder - will be replaced")

    # Create stale sidecars
    (tmp_path / "restore_target.db-wal").write_bytes(b"stale wal content")
    (tmp_path / "restore_target.db-shm").write_bytes(b"stale shm content")

    # Create a valid archive with 50 rows
    source = tmp_path / "source.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    for ts in range(50):
        conn.execute(
            "INSERT INTO inverter_raw (timestamp, device) VALUES (?, ?)",
            (1783512004 + ts * 60, "CE12345678"),
        )
    conn.commit()
    conn.close()

    archive = tmp_path / "test.db.gz"
    with gzip.open(archive, "wb") as fh:
        fh.write(source.read_bytes())

    monkeypatch.setattr(manage, "_database_path", lambda: str(target))
    monkeypatch.setattr(manage, "service", lambda action: True)
    monkeypatch.setattr(
        manage,
        "wait_until_up",
        lambda port, **kw: ("collecting", {"running": True, "connected": True}),
    )
    result = manage.cmd_restore([str(archive), "--yes"])
    assert result == 0

    # Sidecars must be gone
    assert not (tmp_path / "restore_target.db-wal").exists(), (
        "the -wal sidecar survived the restore"
    )
    assert not (tmp_path / "restore_target.db-shm").exists(), (
        "the -shm sidecar survived the restore"
    )


# --- Lock ---


def test_lock_refuses_a_second_run(tmp_path: Any) -> None:
    """Two overlapping runs share a working path and a destination name.

    A hand-run backup does not go through systemd's serialisation of a oneshot
    unit. Interleaved, two runs can publish a verified archive of an empty
    database while rotating a real backup away.
    """
    source = tmp_path / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()
    dest = tmp_path / "backups"
    dest.mkdir()

    lock_path = str(source) + ".backup.lock"
    lock_fd, _reason = manage._take_lock(lock_path)
    assert lock_fd is not None

    try:
        written = manage.backup_now(str(source), str(dest), keep=14, stamp="2026-08-12")
        assert written is None
        # Nothing was written — no backup, no .part, no .tmp
        assert list(dest.glob("arraysense-*.db.gz")) == []
        assert list(dest.glob("*.part")) == []
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.remove(lock_path)


def test_a_lock_that_cannot_be_created_is_not_reported_as_a_busy_one(
    tmp_path: Any, capsys: Any
) -> None:
    """Measured on a real machine: ProtectSystem=strict made the database's
    directory read-only, and the backup announced that another backup was
    running. No backup was running."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions, so this cannot be exercised")
    locked_dir = tmp_path / "readonly"
    locked_dir.mkdir()
    source = locked_dir / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()
    locked_dir.chmod(0o500)
    try:
        dest = tmp_path / "backups"
        dest.mkdir()
        assert manage.backup_now(str(source), str(dest), keep=14, stamp="2026-08-12") is None
        out = capsys.readouterr().out
        assert "another backup is running" not in out
        assert "ReadWritePaths" in out
    finally:
        locked_dir.chmod(0o700)


def test_a_genuinely_held_lock_still_says_so(tmp_path: Any, capsys: Any) -> None:
    """A lock held by a real run must still say so, or the two failures would
    become indistinguishable the other way."""
    source = tmp_path / "live.db"
    conn = sqlite3.connect(str(source))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()
    dest = tmp_path / "backups"
    dest.mkdir()
    with open(str(source) + ".backup.lock", "x"):
        assert manage.backup_now(str(source), str(dest), keep=14, stamp="2026-08-12") is None
        assert "another backup is running" in capsys.readouterr().out


def test_a_zero_length_working_copy_is_rejected(tmp_path: Any) -> None:
    """sqlite3.connect creates a new file when the path does not exist, and
    quick_check on an empty database returns 'ok' — so a missing or empty
    working copy must be caught before the file is opened, not after."""
    empty = tmp_path / "empty.db"
    empty.write_bytes(b"")
    assert manage._verify_working_copy(str(empty)) is False

    missing = str(tmp_path / "nonexistent.db")
    assert manage._verify_working_copy(missing) is False

    real = tmp_path / "real.db"
    real.write_bytes(b"SQLite format 3\0...")
    assert manage._verify_working_copy(str(real)) is True


def test_sqlite_connect_creates_an_empty_database_that_passes_quick_check(
    tmp_path: Any,
) -> None:
    """Prove the threat the zero-length guard exists for.

    sqlite3.connect creates a new file when the path does not exist, and
    quick_check on an empty database returns 'ok' — which is how two
    overlapping runs can publish a verified-looking archive of nothing.
    """
    missing = str(tmp_path / "nonexistent.db")
    conn = sqlite3.connect(missing)
    try:
        row = conn.execute("PRAGMA quick_check").fetchone()
        assert row[0] == "ok"
        assert os.path.getsize(missing) == 0
    finally:
        conn.close()


# --- Destination directory check ---


def test_check_backup_dir_refuses_a_missing_directory(tmp_path: Any) -> None:
    """A backup that silently invents its own destination with the wrong owner
    is how this failed in the first place."""
    assert manage._check_backup_dir(str(tmp_path / "does_not_exist")) is False


def test_check_backup_dir_refuses_an_unwritable_directory(tmp_path: Any) -> None:
    """root running first creates the directory root:root; the service running
    as arraysense then cannot write there and fails silently every night."""
    if os.geteuid() == 0:
        pytest.skip("root can write to a 0500 directory")
    unwritable = tmp_path / "unwritable"
    unwritable.mkdir(mode=0o500)
    assert manage._check_backup_dir(str(unwritable)) is False


def test_check_backup_dir_accepts_a_writable_directory(tmp_path: Any) -> None:
    """The happy path must be confirmed, because a false refusal would stop a
    working installation from running its daily backup."""
    writable = tmp_path / "writable"
    writable.mkdir()
    assert manage._check_backup_dir(str(writable)) is True


# --- The schedule, which now lives in the settings ---------------------------


def _settings_reply(**values: Any) -> dict[str, Any]:
    """What GET /api/settings answers with, shaped as the service shapes it."""
    return {"fields": [], "values": values}


def _a_database(path: Any) -> None:
    """A real database with the one table the backup checks for."""
    conn = sqlite3.connect(str(path))
    conn.execute(schema.ddl_for("inverter_raw"))
    conn.commit()
    conn.close()


def test_the_cli_fallback_constants_match_the_registry_defaults() -> None:
    """manage.py cannot import the registry — it runs on the distribution's
    3.8 while the package needs 3.12 — so it keeps its own copy of the
    defaults. This is the only thing stopping that copy from drifting into a
    second source of truth, which is what the registry exists to prevent."""
    from arraysense import settings as registry

    for constant, key in (
        (manage.BACKUP_ENABLED, registry.BACKUP_ENABLED_KEY),
        (manage.BACKUP_DIR, registry.BACKUP_DIRECTORY_KEY),
        (manage.BACKUP_KEEP, registry.BACKUP_KEEP_KEY),
        (manage.BACKUP_HOUR, registry.BACKUP_HOUR_KEY),
        (manage.BACKUP_MINUTE, registry.BACKUP_MINUTE_KEY),
    ):
        assert constant == registry.lookup_setting(key).default, key
    # And the mapping the CLI reads them through names the same keys, so a
    # renamed setting fails here rather than silently falling back forever.
    assert set(manage.BACKUP_FALLBACK) == {
        registry.BACKUP_ENABLED_KEY,
        registry.BACKUP_DIRECTORY_KEY,
        registry.BACKUP_KEEP_KEY,
        registry.BACKUP_HOUR_KEY,
        registry.BACKUP_MINUTE_KEY,
    }


def test_the_backup_settings_are_read_from_the_service(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(
            **{
                "backup.enabled": False,
                "backup.directory": "/srv/copies",
                "backup.keep": 30,
                "backup.hour": 22,
                "backup.minute": 45,
                "site.timezone": "America/Denver",
            }
        ),
    )
    conf, reason = manage.backup_settings(8080)
    assert reason == ""
    assert conf["backup.enabled"] is False
    assert conf["backup.directory"] == "/srv/copies"
    assert conf["backup.keep"] == 30
    assert conf["backup.hour"] == 22
    assert conf["backup.minute"] == 45
    assert conf["site.timezone"] == "America/Denver"


def test_a_service_that_is_not_answering_falls_back_and_says_which(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent fallback is a second source of truth pretending to be the
    first. Whoever reads the output has to be able to tell which one answered."""
    monkeypatch.setattr(manage, "_probe", lambda url, timeout: None)
    conf, reason = manage.backup_settings(8080)
    assert conf["backup.directory"] == manage.BACKUP_DIR
    assert conf["backup.keep"] == manage.BACKUP_KEEP
    assert reason
    assert "8080" in reason


def test_a_setting_the_service_did_not_report_falls_back_by_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older service knows nothing of these keys. Each missing one falls
    back on its own and is named, rather than the whole reply being discarded."""
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(**{"backup.directory": "/srv/copies"}),
    )
    conf, reason = manage.backup_settings(8080)
    assert conf["backup.directory"] == "/srv/copies"
    assert conf["backup.keep"] == manage.BACKUP_KEEP
    assert "backup.keep" in reason


def test_an_out_of_range_hour_is_refused_rather_than_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An hour of 99 is a time that never arrives, so a backup that believed
    it would simply never run again."""
    monkeypatch.setattr(
        manage, "_probe", lambda url, timeout: _settings_reply(**{"backup.hour": 99})
    )
    conf, reason = manage.backup_settings(8080)
    assert conf["backup.hour"] == manage.BACKUP_HOUR
    assert "backup.hour" in reason


def test_the_day_is_the_installations_own_day(monkeypatch: pytest.MonkeyPatch) -> None:
    """Half past eleven on the twelfth in New York is the thirteenth in UTC.
    The date the archive is named for, and the date checked for "has today
    already run", are both local-calendar questions — the same cut energy.py
    makes."""
    instant = datetime.datetime(2026, 8, 13, 3, 30, tzinfo=datetime.UTC)
    local = manage._at_zone(instant, "America/New_York")
    assert local.date().isoformat() == "2026-08-12"
    assert (local.hour, local.minute) == (23, 30)


def test_a_time_that_has_not_arrived_is_not_due() -> None:
    zone = "America/New_York"
    early = manage._at_zone(
        datetime.datetime(2026, 8, 12, 6, 0, tzinfo=datetime.UTC), zone
    )  # 02:00 local
    assert manage._time_has_passed(early, 3, 15) is False
    late = manage._at_zone(
        datetime.datetime(2026, 8, 12, 8, 0, tzinfo=datetime.UTC), zone
    )  # 04:00 local
    assert manage._time_has_passed(late, 3, 15) is True


def test_a_spring_forward_day_does_not_skip_the_backup() -> None:
    """On 8 March 2026 New York's clocks go 02:00 to 03:00 and a 02:30 backup
    time never arrives. The question is whether the local clock has passed the
    configured time today, not whether that exact instant existed — a 23-hour
    day must still get its backup."""
    just_after = manage._at_zone(
        datetime.datetime(2026, 3, 8, 7, 5, tzinfo=datetime.UTC), "America/New_York"
    )
    assert (just_after.hour, just_after.minute) == (3, 5)
    assert manage._time_has_passed(just_after, 2, 30) is True


def test_a_fall_back_day_does_not_write_two_backups(tmp_path: Any) -> None:
    """On 1 November 2026 New York lives 01:00 to 02:00 twice. The configured
    time passes twice, and the only thing standing between that and two
    backups for one calendar day is that today's archive already exists."""
    dest = tmp_path / "backups"
    dest.mkdir()
    zone = "America/New_York"
    first = manage._at_zone(datetime.datetime(2026, 11, 1, 5, 20, tzinfo=datetime.UTC), zone)
    second = manage._at_zone(datetime.datetime(2026, 11, 1, 6, 20, tzinfo=datetime.UTC), zone)
    assert (first.hour, first.minute) == (1, 20)
    assert (second.hour, second.minute) == (1, 20)
    assert first.date() == second.date()
    assert manage._time_has_passed(first, 1, 15) is True
    assert manage._time_has_passed(second, 1, 15) is True

    stamp = first.date().isoformat()
    assert manage._already_written(str(dest), stamp) is False
    (dest / manage.archive_name(stamp)).write_bytes(b"today's backup")
    assert manage._already_written(str(dest), stamp) is True


def test_a_scheduled_run_before_the_configured_time_does_nothing_at_all(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """This fires ninety-six times a day. A run that is not due must leave no
    trace, or the journal is full of a backup that did not happen."""
    source = tmp_path / "live.db"
    _a_database(source)
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(
            **{
                "backup.directory": str(dest),
                "backup.hour": 23,
                "backup.minute": 59,
            }
        ),
    )
    monkeypatch.setattr(manage, "_local_now", lambda name: datetime.datetime(2026, 8, 12, 3, 20))
    assert manage.cmd_backup(["--scheduled"]) == 0
    assert capsys.readouterr().out == ""
    assert list(dest.glob("*.gz")) == []


def test_a_scheduled_run_after_the_configured_time_writes_one_backup(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "live.db"
    _a_database(source)
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(
            **{"backup.directory": str(dest), "backup.hour": 3, "backup.minute": 15}
        ),
    )
    monkeypatch.setattr(manage, "_local_now", lambda name: datetime.datetime(2026, 8, 12, 3, 20))
    assert manage.cmd_backup(["--scheduled"]) == 0
    assert [p.name for p in dest.glob("*.gz")] == ["arraysense-2026-08-12.db.gz"]


def test_a_second_scheduled_run_the_same_day_does_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    source = tmp_path / "live.db"
    _a_database(source)
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(
            **{"backup.directory": str(dest), "backup.hour": 3, "backup.minute": 15}
        ),
    )
    monkeypatch.setattr(manage, "_local_now", lambda name: datetime.datetime(2026, 8, 12, 3, 20))
    assert manage.cmd_backup(["--scheduled"]) == 0
    written = dest / "arraysense-2026-08-12.db.gz"
    stamped = written.stat().st_mtime_ns
    capsys.readouterr()

    assert manage.cmd_backup(["--scheduled"]) == 0
    assert capsys.readouterr().out == ""
    assert written.stat().st_mtime_ns == stamped, "the day's archive was rewritten"


def test_a_disabled_backup_skips_the_scheduled_run_silently(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    source = tmp_path / "live.db"
    _a_database(source)
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(
            **{"backup.enabled": False, "backup.directory": str(dest)}
        ),
    )
    monkeypatch.setattr(manage, "_local_now", lambda name: datetime.datetime(2026, 8, 12, 23, 0))
    assert manage.cmd_backup(["--scheduled"]) == 0
    assert capsys.readouterr().out == ""
    assert list(dest.glob("*.gz")) == []


def test_a_hand_run_backs_up_whatever_the_schedule_says(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--scheduled is for the timer. Somebody typing the command wants a
    backup now, and a disabled schedule is not a refusal to make one."""
    source = tmp_path / "live.db"
    _a_database(source)
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(
            **{
                "backup.enabled": False,
                "backup.directory": str(dest),
                "backup.hour": 23,
                "backup.minute": 59,
            }
        ),
    )
    monkeypatch.setattr(manage, "_local_now", lambda name: datetime.datetime(2026, 8, 12, 3, 20))
    assert manage.cmd_backup([]) == 0
    assert [p.name for p in dest.glob("*.gz")] == ["arraysense-2026-08-12.db.gz"]


def test_a_hand_run_writes_where_the_settings_say(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The destination is a setting now, so a bare `arraysense backup` must
    honour it rather than the constant it used to be."""
    source = tmp_path / "live.db"
    _a_database(source)
    configured = tmp_path / "configured"
    configured.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(**{"backup.directory": str(configured)}),
    )
    assert manage.cmd_backup([]) == 0
    assert len(list(configured.glob("*.gz"))) == 1


def test_a_flag_still_beats_the_setting(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "live.db"
    _a_database(source)
    configured = tmp_path / "configured"
    configured.mkdir()
    asked_for = tmp_path / "asked_for"
    asked_for.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(**{"backup.directory": str(configured)}),
    )
    assert manage.cmd_backup(["--dir", str(asked_for)]) == 0
    assert len(list(asked_for.glob("*.gz"))) == 1
    assert list(configured.glob("*.gz")) == []


def test_a_run_that_used_the_fallback_says_so_in_its_output(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """A backup written under the built-in defaults must not read as one
    written under the settings somebody edited."""
    source = tmp_path / "live.db"
    _a_database(source)
    dest = tmp_path / "backups"
    dest.mkdir()
    monkeypatch.setattr(manage, "_database_path", lambda: str(source))
    monkeypatch.setattr(manage, "_probe", lambda url, timeout: None)
    assert manage.cmd_backup(["--dir", str(dest)]) == 0
    assert "built-in" in capsys.readouterr().out


def test_uninstall_names_the_backup_directory_that_is_actually_configured(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    """The destination is a setting, so naming the built-in default would send
    somebody to an empty directory while the copies they were told about sit on
    a disk somewhere else."""
    monkeypatch.setattr(
        manage,
        "_probe",
        lambda url, timeout: _settings_reply(**{"backup.directory": "/mnt/usb/copies"}),
    )
    monkeypatch.setattr(manage, "_confirm", lambda _p: False)
    assert manage.cmd_uninstall(["--purge"]) == 0
    out = capsys.readouterr().out
    assert "/mnt/usb/copies" in out
    assert manage.BACKUP_DIR not in out
