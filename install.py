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
import subprocess
import sys
from collections.abc import Callable
from typing import NamedTuple, TypedDict

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

# The port drop-in lives one level down from the unit, in a directory of the
# unit's own name; this is the systemd convention for a partially-overriding
# file and it is named here so install and upgrade agree about it.
DROPIN_DIR = "/etc/systemd/system/arraysense.service.d"

# uv's installer puts the binary in ~/.local/bin — for the root that runs this,
# /root/.local/bin — which is not on the PATH of the process that just ran the
# installer, so a bare "uv" lookup fails. find_uv() checks these places after
# PATH, in the order a bootstrap actually leaves them.
UV_CANDIDATES = ("/root/.local/bin/uv", "/usr/local/bin/uv", "/usr/bin/uv")


class Refusal(NamedTuple):
    """Why the installer stopped, and what the operator can do about it."""

    reason: str
    remedy: str


class Args(TypedDict):
    """Everything parse_args extracts, typed so main can read it back safely.

    A plain dict of object values would force every read in main to prove its
    own type; the two new keys default to the project itself and its default
    branch, which is what an unattended run clones unless told otherwise.
    """

    yes: bool
    port: int | None
    repo: str
    ref: str | None


class Host(TypedDict):
    """The machine as observe_host reads it, typed for preflight's kwargs.

    preflight(**observe_host()) would not type-check with a dict of object
    values; spelling the keys lets the unpacking prove itself against the
    keyword-only parameters.
    """

    platform_name: str
    has_systemd: bool
    euid: int
    machine: str
    has_git: bool
    free_bytes: int
    python_version: tuple[int, ...]


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


def observe_host() -> Host:
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


def parse_args(argv: list[str]) -> Args:
    """The flags that let this run without a person watching.

    --yes and --port make an install answerable from a terminal-less cron or
    pipe; --repo and --ref let the same install target a fork or a pinned
    release instead of whatever sits on the project's default branch. All four
    default to the boring values, so a bare invocation is a normal install.
    """
    assumed_yes = "--yes" in argv
    port: int | None = None
    if "--port" in argv:
        index = argv.index("--port")
        if index + 1 >= len(argv) or not argv[index + 1].isdigit():
            raise SystemExit("--port needs a number, for example: --port 8080")
        port = int(argv[index + 1])
    repo = REPO_URL
    if "--repo" in argv:
        index = argv.index("--repo")
        if index + 1 >= len(argv):
            raise SystemExit("--repo needs a URL or path")
        repo = argv[index + 1]
    ref: str | None = None
    if "--ref" in argv:
        index = argv.index("--ref")
        if index + 1 >= len(argv):
            raise SystemExit("--ref needs a branch, tag or commit")
        ref = argv[index + 1]
    return {"yes": assumed_yes, "port": port, "repo": repo, "ref": ref}


def render_plan(port: int, repo: str = REPO_URL, ref: str | None = None) -> str:
    """Everything the installer will do, before it does any of it.

    The repository and ref are named because they are the two things a bootstrap
    that reached for the wrong fork would never otherwise be questioned about —
    the plan is the only text shown before root acts.
    """
    target = f"{repo} (ref {ref})" if ref is not None else repo
    lines = [
        "Solar ArraySense will:",
        "",
        "  install uv (which brings its own Python 3.12)",
        f"  clone {target} into {INSTALL_DIR}",
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


def unit_text() -> str:
    """The service unit, read from the clone so there is one copy of it.

    The unit travels with the source it runs; the drop-in is the right shape
    for a per-machine tweak, but the unit itself is part of the code. If the
    clone did not land, the file cannot be there, so the failure is a sentence
    rather than a traceback.
    """
    path = os.path.join(INSTALL_DIR, "packaging", "arraysense.service")
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        raise SystemExit(f"cannot read the service unit {path}; the clone is incomplete") from None


def dropin_text(port: int) -> str:
    """The port, and the one capability a low port needs.

    ExecStart is cleared before being re-set because systemd APPENDS to a list
    directive: without the empty assignment the unit would carry two ExecStart
    lines — the shipped one and this one — and refuse to start. The capability
    is granted only below 1024, where binding is privileged.
    """
    lines = [
        "[Service]",
        "ExecStart=",
        f"ExecStart={INSTALL_DIR}/.venv/bin/python -m arraysense "
        f"--config {CONFIG_DIR}/config.toml --port {port}",
    ]
    if port < 1024:
        lines.append("AmbientCapabilities=CAP_NET_BIND_SERVICE")
    return "\n".join(lines) + "\n"


def shim_text() -> str:
    """The management command, running manage.py under the SYSTEM python.

    Deliberately not the virtualenv: `arraysense upgrade` rebuilds that
    virtualenv while it is running, and a CLI living inside it would be pulling
    the floor up behind itself.
    """
    return f'#!/bin/sh\nexec /usr/bin/env python3 {INSTALL_DIR}/src/arraysense/manage.py "$@"\n'


def find_uv() -> str | None:
    """Where uv actually landed, or None if it did not.

    Its installer puts it in ~/.local/bin, which is not on the PATH of the
    process that just ran that installer — so looking it up by name finds
    nothing and raises FileNotFoundError halfway through an install that has
    already cloned the repository.
    """
    found = shutil.which("uv")
    if found:
        return found
    for candidate in UV_CANDIDATES:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return None


def _step(argv: list[str]) -> int:
    """Run one install step, reporting a missing executable as a failed step.

    subprocess raises FileNotFoundError for a command that is not there, and an
    uncaught traceback halfway through a root install tells the operator
    nothing about which half completed.
    """
    try:
        return subprocess.run(argv, check=False).returncode
    except OSError as exc:
        print(f"could not run {argv[0]}: {exc}")
        return 127


def outbound_ip() -> str | None:
    """The address other machines on the LAN would reach this host on.

    Deliberately not gethostbyname(gethostname()), which returns 127.0.1.1 on
    Debian and Raspberry Pi OS because that is what /etc/hosts says. Connecting
    a UDP socket sends nothing; it just asks the routing table which local
    address would be used, which is the one worth printing.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # TEST-NET-1, routed nowhere
        return str(probe.getsockname()[0])
    except OSError:
        return None
    finally:
        probe.close()


def render_handoff(port: int, host: str) -> str:
    """The addresses a person opens to reach the wizard.

    The .local name is always shown; the IP line comes from the routing table
    rather than the hostname lookup, which maps to 127.0.1.1 on Debian. When no
    route can be determined there is no IP line at all — never a placeholder
    that looks like an address.
    """
    suffix = "" if port == 80 else f":{port}"
    ip = outbound_ip()
    heading = (
        "Installed. Open either of these to run the setup wizard:"
        if ip is not None
        else "Installed. Open this to run the setup wizard:"
    )
    lines = ["", heading, f"  http://{host}.local{suffix}"]
    if ip is not None:
        lines.append(f"  http://{ip}{suffix}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Preflight, plan, confirm, install, then hand off to manage.py."""
    args = parse_args(list(sys.argv[1:]) if argv is None else argv)

    refusal = preflight(**observe_host())
    if refusal is not None:
        print(f"Cannot install: {refusal.reason}")
        print(refusal.remedy)
        return 1

    if os.path.isdir(os.path.join(INSTALL_DIR, ".git")):
        print(f"{INSTALL_DIR} already exists.")
        print("To update, run: arraysense upgrade")
        print(f"To repair this installation, remove {INSTALL_DIR} and run this again.")
        return 1

    port = resolve_port(chosen=args["port"])
    print(render_plan(port, repo=args["repo"], ref=args["ref"]))
    if not args["yes"]:
        try:
            if ask_tty("Continue? [y/N] ").lower() not in ("y", "yes"):
                print("nothing done")
                return 0
        except NoTerminal:
            print("no controlling terminal, so nothing can be confirmed.")
            print("Run this from a terminal, or state your choices explicitly:")
            print(f"  sudo python3 install.py --yes --port {port}")
            return 1

    clone = ["git", "clone", "--depth", "1"]
    if args["ref"]:
        clone += ["--branch", str(args["ref"])]
    clone += [str(args["repo"]), INSTALL_DIR]

    # useradd may fail because the user already exists from a previous run;
    # every other step failing means the install is broken and stops here.
    steps = [
        (["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"], False),
        (clone, False),
        (
            [
                "useradd",
                "--system",
                "--home",
                INSTALL_DIR,
                "--shell",
                "/usr/sbin/nologin",
                SERVICE_USER,
            ],
            True,
        ),
        (["install", "-d", "-o", SERVICE_USER, "-g", SERVICE_USER, CONFIG_DIR, DATA_DIR], False),
    ]
    for step_argv, may_fail in steps:
        if _step(step_argv) != 0 and not may_fail:
            print(f"failed: {' '.join(step_argv)}")
            return 1

    uv = find_uv()
    if uv is None:
        print("uv did not land where this process can find it.")
        print(f"Install uv and finish by hand with: uv sync --project {INSTALL_DIR}")
        return 1

    for step_argv in [
        [uv, "sync", "--project", INSTALL_DIR],
        ["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", INSTALL_DIR],
    ]:
        if _step(step_argv) != 0:
            print(f"failed: {' '.join(step_argv)}")
            return 1

    unit = unit_text()
    with open("/etc/systemd/system/arraysense.service", "w") as handle:
        handle.write(unit)
    os.makedirs(DROPIN_DIR, exist_ok=True)
    with open(os.path.join(DROPIN_DIR, "port.conf"), "w") as handle:
        handle.write(dropin_text(port))
    with open(CLI_SHIM, "w") as handle:
        handle.write(shim_text())
    os.chmod(CLI_SHIM, 0o755)

    _step(["systemctl", "daemon-reload"])
    _step(["systemctl", "enable", "--now", "arraysense"])

    # The health check has one home, in manage.py, and this is it being used.
    verify = _step(
        [
            "/usr/bin/env",
            "python3",
            os.path.join(INSTALL_DIR, "src", "arraysense", "manage.py"),
            "restart",
        ]
    )
    if verify != 0:
        print("Installed, but the service did not come up. Run: arraysense logs")
        return 1

    print(render_handoff(port, socket.gethostname()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
