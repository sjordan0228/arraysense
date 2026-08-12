"""install.py — the one-line bootstrap that puts Solar ArraySense on a machine.

Fetched over HTTPS and piped into root, so it is written to be read first: it
downloads no further scripts, prints everything it intends to do before doing
any of it, and is safe to run twice. Questions are read from the controlling
terminal, never stdin: run this as `curl ... | sudo python3 -` and stdin is
the script itself, already at EOF.

Stdlib only, and it must PARSE on Python 3.8 — it runs on whatever interpreter
the distribution shipped, before uv has installed 3.12 for the service. Modern
annotation syntax is required and is safe because annotations are never
evaluated; what is genuinely forbidden is 3.9+ syntax in code that RUNS: match
statements, dict | dict merging, str.removeprefix/removesuffix,
functools.cache, subscripted generics outside annotations, and same-quote
nesting inside f-strings.
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
from collections.abc import Callable
from typing import NamedTuple

SUPPORTED_ARCHES = ("aarch64", "x86_64")

# uv fetches its own Python and the dependency tree, and the database grows
# about 52 MB a day at a ten-second poll. A host under this will fail within
# weeks whatever happens today, so it is refused now with a reason rather than
# later with a full disk.
MIN_FREE_BYTES = 2 * 1024**3

MIN_PYTHON = (3, 8)

# One layout, named here so install and upgrade agree about where things live:
# the clone under /opt, the configuration under /etc, the database under
# /var/lib, and one name a human types for the management command.
INSTALL_DIR = "/opt/arraysense"
CONFIG_DIR = "/etc/arraysense"
DATA_DIR = "/var/lib/arraysense"
SERVICE_USER = "arraysense"
CLI_SHIM = "/usr/local/bin/arraysense"
REPO_URL = "https://github.com/sjordan0228/arraysense"

# 80 is preferred (see resolve_port); 8080 is the fallback when it is taken.
DEFAULT_PORT = 8080


class Refusal(NamedTuple):
    """Why the installer stopped, and what the operator can do about it."""

    reason: str
    remedy: str


def _install_filesystem() -> str:
    """The nearest existing ancestor of the install directory.

    Free space has to be measured where the files will land. On a host with
    /opt on its own mount, measuring / reports a figure that has nothing to do
    with whether this install will fit.
    """
    path = INSTALL_DIR
    while not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            return "/"
        path = parent
    return path


def preflight(
    *,
    platform_name: str,
    has_systemd: bool,
    euid: int,
    machine: str,
    has_git: bool,
    free_bytes: int,
    python_version: tuple[int, ...],
) -> Refusal | None:
    """The first reason to stop, or None when the host is usable.

    Every input is passed in rather than read here, so the whole decision is
    testable without a machine that actually lacks systemd.
    """
    if python_version < MIN_PYTHON:
        return Refusal(
            f"Python {python_version[0]}.{python_version[1]} is too old to run this installer",
            "Install Python 3.8 or newer and run this again.",
        )
    if platform_name != "linux":
        return Refusal(
            f"this installs on Linux, and this host reports {platform_name!r}",
            "Run it on a Linux host — a Raspberry Pi, a VM, or an LXC container.",
        )
    if not has_systemd:
        return Refusal(
            "no systemd found, and the shipped service unit is the only supervision here",
            "Install on a systemd host, or run the service yourself from a checkout.",
        )
    if euid != 0:
        return Refusal(
            "this needs root to create a service user and install a unit",
            "Re-run it with sudo.",
        )
    if machine not in SUPPORTED_ARCHES:
        return Refusal(
            f"unsupported architecture {machine!r}",
            f"Supported: {', '.join(SUPPORTED_ARCHES)}.",
        )
    if not has_git:
        return Refusal(
            "git is not installed, and both install and upgrade are a fetch",
            "Install git with your package manager, then run this again.",
        )
    if free_bytes < MIN_FREE_BYTES:
        return Refusal(
            f"not enough free disk: {free_bytes / 1024**3:.1f} GB, "
            f"need {MIN_FREE_BYTES / 1024**3:.0f} GB",
            "Free some space. The database grows about 52 MB a day.",
        )
    return None


def observe_host() -> dict[str, object]:
    """Read the real machine, so preflight itself stays pure."""
    return {
        "platform_name": sys.platform,
        "has_systemd": os.path.isdir("/run/systemd/system"),
        "euid": os.geteuid(),
        "machine": os.uname().machine,
        "has_git": shutil.which("git") is not None,
        "free_bytes": shutil.disk_usage(_install_filesystem()).free,
        "python_version": sys.version_info[:2],
    }


def port_is_free(port: int) -> bool:
    """Whether nothing is already listening there.

    Binding fails for two unrelated reasons — the port is held, or binding it
    needs privileges this process lacks. Both mean the install would fail
    later, so both report False; the two are deliberately not told apart here,
    and the caller must not guess which one happened.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("0.0.0.0", port))
        except PermissionError:
            return False
        except OSError:
            return False
    return True


class NoTerminal(Exception):  # noqa: N818 — the spec's name, not the convention
    """Raised when there is nobody to ask — no controlling terminal at all."""


def ask_tty(prompt: str) -> str:
    """Ask the operator a question, reading the terminal rather than stdin.

    The documented way to run this is `curl ... | sudo python3 -`, which makes
    stdin the script itself: it is already at EOF when the first question is
    asked, so input() raises immediately and every prompt in this installer
    would be unanswerable. The controlling terminal is still there, and that is
    what a person is actually sitting at.
    """
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(prompt)
            tty.flush()
            answer = tty.readline()
    except OSError:
        raise NoTerminal() from None
    if not answer:
        raise NoTerminal()
    return answer.strip()


def resolve_port(
    *,
    probe: Callable[[int], bool] = port_is_free,
    ask: Callable[[str], str] = ask_tty,
    chosen: int | None = None,
) -> int:
    """Port 80 when it is free, otherwise ask, defaulting to 8080.

    Asked rather than assumed because the usual reason 80 is taken is a web
    server the owner cares about, and silently moving would leave them looking
    for the dashboard at an address nobody mentioned. A --port choice skips the
    question entirely: an unattended install has to state its port rather than
    have one chosen for it.
    """
    if chosen is not None:
        return chosen
    if probe(80):
        return 80
    while True:
        answer = ask("Port 80 is in use. Which port should the dashboard use? [8080] ").strip()
        if not answer:
            return DEFAULT_PORT
        if not answer.isdigit():
            print("  that is not a port number; pick one like 8080")
            continue
        port = int(answer)
        if not 1 <= port <= 65535:
            print("  ports run from 1 to 65535; pick another")
            continue
        if probe(port):
            return port
        print("  that port is in use or not permitted; pick another")


def parse_args(argv: list[str]) -> dict[str, object]:
    """The two flags that let this run without a person watching.

    Kept to exactly two, and both explicit: an unattended install has to state
    the port it wants rather than have one chosen for it, because the port is
    the one decision the operator cannot discover afterwards without looking.
    """
    assumed_yes = "--yes" in argv
    port: int | None = None
    if "--port" in argv:
        index = argv.index("--port")
        if index + 1 >= len(argv) or not argv[index + 1].isdigit():
            raise SystemExit("--port needs a number, for example: --port 8080")
        port = int(argv[index + 1])
    return {"yes": assumed_yes, "port": port}


def render_plan(port: int) -> str:
    """Everything the installer will do, before it does any of it."""
    lines = [
        "Solar ArraySense will:",
        "",
        "  install uv (which brings its own Python 3.12)",
        f"  clone {REPO_URL} into {INSTALL_DIR}",
        f"  create the system user {SERVICE_USER!r}",
        f"  create {CONFIG_DIR} and {DATA_DIR}",
        f"  install a systemd service listening on port {port}",
        f"  install the management command {CLI_SHIM}",
        "",
        "It will NOT write a configuration file — the first visit to the",
        "dashboard runs the setup wizard, and an existing config would skip it.",
        "",
    ]
    if port == 80:
        lines.insert(-1, "  grant CAP_NET_BIND_SERVICE so a non-root service can bind port 80")
    return "\n".join(lines)
