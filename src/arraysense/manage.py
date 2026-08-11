"""manage.py — the lifecycle CLI: status, upgrade, logs, restart, uninstall.

Run by /usr/local/bin/arraysense under the SYSTEM interpreter, never the
virtualenv. `upgrade` rebuilds that virtualenv while it is running, and a CLI
living inside it would be pulling the floor up behind itself.

Stdlib only, and written to parse on Python 3.8, because the distribution's own
interpreter is what runs this — uv's 3.12 belongs to the service, not here.
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

SERVICE = "arraysense"
INSTALL_DIR = "/opt/arraysense"
CONFIG_PATH = "/etc/arraysense/config.toml"
PORT_DROPIN = "/etc/systemd/system/arraysense.service.d/port.conf"
DEFAULT_PORT = 8080

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


def capabilities_url(port: int) -> str:
    """The declaration endpoint, written beside status_url so both agree on the host.

    Kept next to the health endpoint so neither can drift to a different one —
    the CLI must probe the same box the service runs on.
    """
    return f"http://127.0.0.1:{port}/api/capabilities"


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


def configured_port() -> int:
    """The port the unit was installed with, or the default.

    Read from the drop-in rather than remembered anywhere else: the drop-in is
    what systemd actually obeys, so anything else would be a second answer that
    can disagree with the running service. The LAST ExecStart= wins, which is
    how systemd itself resolves a drop-in — each assignment replaces the
    previous one.
    """
    port = DEFAULT_PORT
    try:
        with open(PORT_DROPIN) as handle:
            text = handle.read()
    except OSError:
        return port
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith(";"):
            continue
        if not stripped.startswith("ExecStart="):
            continue
        if "--port" not in stripped:
            continue
        parts = stripped.split("--port", 1)[1].split()
        if parts and parts[0].isdigit():
            port = int(parts[0])
    return port


def _cut_inline_comment(raw: str) -> str:
    """Drop a TOML inline comment, so a trailing '#' cannot leak into a path.

    TOML's comment starts at the first '#' outside a quoted string; a path that
    contains '#' is inside the quotes and survives. Without the cut, stripping
    quotes first would leave the comment attached to the value.
    """
    quote: str | None = None
    for index, char in enumerate(raw):
        if quote is not None:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#":
            return raw[:index]
    return raw


def _database_path() -> str:
    """Where the config says the database is, falling back to the default.

    Parsed with a plain scan rather than a TOML library because this file runs
    on the distribution's Python 3.8, which has no tomllib.
    """
    try:
        with open(CONFIG_PATH) as handle:
            for line in handle:
                if line.strip().startswith("database_path"):
                    value = _cut_inline_comment(line.split("=", 1)[1])
                    return value.strip().strip('"').strip("'")
    except OSError:
        pass
    return "/var/lib/arraysense/arraysense.db"


def database_facts(path: str) -> dict[str, Any]:
    """Size and date range, or None for a range that does not exist yet.

    Asked because "how big is it and how far back does it go" is most of what a
    support conversation needs, and because a fresh install legitimately has no
    range at all — which must read as absent rather than as a guessed date.
    """
    facts: dict[str, Any] = {"bytes": 0, "first": None, "last": None}
    try:
        facts["bytes"] = os.path.getsize(path)
    except OSError:
        return facts
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return facts
    try:
        row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM inverter_raw").fetchone()
    except sqlite3.Error:
        return facts
    finally:
        conn.close()
    if row and row[0] is not None:
        # Local dates, deliberately. energy.py cuts every calendar day in the
        # installation's local zone, so a UTC date here would disagree with the
        # History page about the same database. zoneinfo is 3.9+, so the
        # machine's zone is both the best available and normally the right one.
        facts["first"] = datetime.datetime.fromtimestamp(row[0]).date().isoformat()
        facts["last"] = datetime.datetime.fromtimestamp(row[1]).date().isoformat()
    return facts


def driver_line(body: dict[str, Any] | None) -> str:
    """Name the driver and what it declares, or say plainly that it is unknown.

    Which family the service thinks it is talking to, and how many strings it
    believes exist, is where a support conversation starts. Every field here is
    nullable at the source, and one that was never declared prints as a dash:
    printing a plausible default would be the absent-data rule broken in the
    place it is most likely to be believed.
    """
    if body is None:
        return "driver:    unavailable"
    devices = body.get("devices") or []
    if not devices:
        return "driver:    none declared"
    first = devices[0]
    dash = "—"
    name = first.get("driver") or dash
    model = first.get("model") or dash
    serial = first.get("device") or dash
    strings = first.get("pv_strings")
    shown = dash if strings is None else strings
    energy = first.get("energy") or dash
    transport = first.get("transport") or dash
    return (
        f"driver:    {name} {model} ({serial}), "
        f"{shown} PV strings, energy {energy}, via {transport}"
    )


def cmd_status(argv: list[str]) -> int:
    """Print what is running and what it sees. Read-only by design — see #34."""
    port = configured_port()
    body = _probe(status_url(port), timeout=5.0)
    if body is None:
        print(f"service: not answering on port {port}")
        print("  try: arraysense logs")
        return 1
    print(f"version:   {body.get('version')}")
    staleness = (body.get("staleness") or {}).get("verdict")
    print(
        f"collector: running={body.get('running')} connected={body.get('connected')} "
        f"staleness={staleness}"
    )
    print(driver_line(_probe(capabilities_url(port), timeout=5.0)))
    facts = database_facts(_database_path())
    span = "empty" if facts["first"] is None else f"{facts['first']} .. {facts['last']}"
    print(f"database:  {facts['bytes'] / 1048576:.1f} MB, {span}")
    return 0


def cmd_logs(argv: list[str]) -> int:
    """Journalctl for the unit, so nobody has to remember its name.

    Arguments are forwarded rather than filtered: this exists to help somebody
    diagnose an installation, and quietly discarding the `--since` or `-n` they
    typed would answer a different question from the one they asked.
    """
    asked_for_lines = any(a in ("-n", "--lines") or a.startswith("--lines=") for a in argv)
    args = ["journalctl", "-u", SERVICE]
    if not asked_for_lines:
        args += ["-n", "200"]
    args += argv
    return subprocess.call(args)


def cmd_restart(argv: list[str]) -> int:
    """Restart and prove it came back, rather than trusting systemctl."""
    if not service("restart"):
        print("systemctl restart failed; try: arraysense logs")
        return 1
    port = configured_port()
    if wait_until_healthy(port) is None:
        print("restarted, but the collector did not come back within 90s")
        print("  try: arraysense logs")
        return 1
    print("restarted and collecting")
    return 0


def cmd_version(argv: list[str]) -> int:
    """Name the installed code and what the running service reports."""
    commit = run(["git", "-C", INSTALL_DIR, "rev-parse", "--short", "HEAD"]).stdout.strip()
    body = _probe(status_url(configured_port()), timeout=5.0) or {}
    print(f"version: {body.get('version') or 'not answering'}")
    print(f"commit:  {commit or 'unknown'}")
    return 0


COMMANDS = {
    "status": cmd_status,
    "logs": cmd_logs,
    "restart": cmd_restart,
    "version": cmd_version,
}


def main(argv: list[str] | None = None) -> int:
    """Dispatch a subcommand; no argument means status, an unknown one means 2."""
    args = list(sys.argv[1:] if argv is None else argv)
    name = args[0] if args else "status"
    handler = COMMANDS.get(name)
    if handler is None:
        print("usage: arraysense {" + "|".join(sorted(COMMANDS)) + "}")
        return 2
    return handler(args[1:])


if __name__ == "__main__":
    raise SystemExit(main())
