"""install.py — the one-line bootstrap that puts Solar ArraySense on a machine.

Fetched over HTTPS and piped into root, so it is written to be read first: it
downloads no further scripts, prints everything it intends to do before doing
any of it, and is safe to run twice.

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
import sys
from typing import NamedTuple

SUPPORTED_ARCHES = ("aarch64", "x86_64")

# uv fetches its own Python and the dependency tree, and the database grows
# about 52 MB a day at a ten-second poll. A host under this will fail within
# weeks whatever happens today, so it is refused now with a reason rather than
# later with a full disk.
MIN_FREE_BYTES = 2 * 1024**3

MIN_PYTHON = (3, 8)

INSTALL_DIR = "/opt/arraysense"


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
