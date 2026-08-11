"""test_manage.py — the management CLI: service control, health, upgrade, rollback."""

from __future__ import annotations

from typing import Any

import pytest

from arraysense import manage


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
    import sqlite3

    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE inverter_raw (timestamp INTEGER)")
    conn.commit()
    conn.close()

    facts = manage.database_facts(str(db))
    assert facts["first"] is None
    assert facts["last"] is None
    assert facts["bytes"] > 0


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


def test_status_defaults_the_port_when_no_drop_in_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(manage, "PORT_DROPIN", "/nonexistent/port.conf")
    assert manage.configured_port() == 8080


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
