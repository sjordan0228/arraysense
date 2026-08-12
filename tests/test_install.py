"""test_install.py — the bootstrap installer's preflight."""

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
