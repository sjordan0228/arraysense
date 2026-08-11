"""manage.py — the lifecycle CLI: status, upgrade, logs, restart, uninstall.

Run by /usr/local/bin/arraysense under the SYSTEM interpreter, never the
virtualenv. `upgrade` rebuilds that virtualenv while it is running, and a CLI
living inside it would be pulling the floor up behind itself.

Stdlib only, and written to parse on Python 3.8, because the distribution's own
interpreter is what runs this — uv's 3.12 belongs to the service, not here.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

SERVICE = "arraysense"
INSTALL_DIR = "/opt/arraysense"
CONFIG_PATH = "/etc/arraysense/config.toml"

# What "healthy" means, and why it is not "systemctl says active": the unit is
# active the moment the process starts, which is before it binds the port and
# well before the first poll has reached the inverter. An upgrade that trusted
# `systemctl start` would report success over a collector that never came back.
HEALTH_TIMEOUT = 90.0


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """One subprocess call, captured, so callers can report what failed."""
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def service(action: str) -> bool:
    """Run systemctl <action> arraysense; True when it returned success."""
    return run(["systemctl", action, SERVICE]).returncode == 0


def status_url(port: int) -> str:
    """The health endpoint on loopback — the CLI runs on the same box as the service."""
    return f"http://127.0.0.1:{port}/api/status"


def _probe(url: str, timeout: float) -> dict[str, Any] | None:
    """One GET, or None for every failure.

    The service not being up yet is the expected case here, not an
    exceptional one.
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as reply:
            body = json.loads(reply.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return body if isinstance(body, dict) else None


def wait_until_healthy(
    port: int, timeout: float = HEALTH_TIMEOUT, sleep: float = 2.0
) -> dict[str, Any] | None:
    """Poll until the service answers AND reports a live collector, or give up.

    Returns the status body so the caller can print what it found, and None on
    timeout — which the caller must treat as failure rather than as silence.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = _probe(status_url(port), timeout=5.0)
        if body is not None and body.get("running") and body.get("connected"):
            return body
        time.sleep(sleep)
    return None
