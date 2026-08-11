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
