"""install.py — the one-line bootstrap that puts Solar ArraySense on a machine.

Fetched over HTTPS and piped into root, so it is written to be read first. It
fetches exactly two things — uv's installer, downloaded and run by sh, and the
repository it clones — prints everything it intends to do before doing any of
it, and is safe to run twice. Questions are read from the controlling terminal,
never stdin: run this as `curl ... | sudo python3 -` and stdin is the script
itself, already at EOF.

Stdlib only, and it must PARSE on Python 3.8 — it runs on whatever interpreter
the distribution shipped, before uv has installed 3.12 for the service. Modern
annotation syntax is required and is safe because annotations are never
evaluated; what is genuinely forbidden is 3.9+ syntax in code that RUNS: match
statements, dict | dict merging, str.removeprefix/removesuffix,
functools.cache, subscripted generics outside annotations, and same-quote
nesting inside f-strings.
"""

from __future__ import annotations

import glob
import os
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable

try:
    from typing import NamedTuple, TypedDict
except ImportError:  # Python < 3.8: still load so preflight can refuse the
    # interpreter with its own message instead of a traceback from the import
    from typing import NamedTuple

    TypedDict = dict  # type: ignore[assignment]

SUPPORTED_ARCHES = ("aarch64", "x86_64")

# uv fetches its own Python and the dependency tree, and the database grows
# about 5 MB a day at a ten-second poll — file growth, measured on the
# reference install, not the disk-write volume. A host under this will run out
# within about a year whatever happens today, so it is refused now with a
# reason rather than later with a full disk.
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

# uv downloads its own Python when no system interpreter satisfies
# requires-python, and puts it under the caller's home by default — /root for
# the root that runs this. The service runs as an unprivileged user under
# ProtectHome=true, so a Python under /root would be masked and the service
# could not exec it. Directing it to /opt/uv-python, outside any home, is where
# the production installation also keeps it.
UV_PYTHON_INSTALL_DIR = "/opt/uv-python"

# The backup is a second unit, a timer and a tmpfiles fragment, each named as
# it ships in the clone and where it must land. Installing all three is part of
# the install, not an extra: the management table promises a daily compressed
# copy, and a promise nothing on the machine keeps is how a database loss stays
# lost.
BACKUP_FILES = (
    ("arraysense-backup.service", "/etc/systemd/system/arraysense-backup.service"),
    ("arraysense-backup.timer", "/etc/systemd/system/arraysense-backup.timer"),
    ("arraysense-backup.tmpfiles.conf", "/etc/tmpfiles.d/arraysense-backup.conf"),
)


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
    has_curl: bool
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
    has_curl: bool,
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
    if not has_curl:
        return Refusal(
            "curl is not installed, and the first step downloads uv's installer",
            "Install curl with your package manager, then run this again.",
        )
    if free_bytes < MIN_FREE_BYTES:
        return Refusal(
            f"not enough free disk: {free_bytes / 1024**3:.1f} GB, "
            f"need {MIN_FREE_BYTES / 1024**3:.0f} GB",
            "Free some space. The database grows about 5 MB a day.",
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
        "has_curl": shutil.which("curl") is not None,
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
        if not 1 <= chosen <= 65535:
            raise SystemExit(f"--port must be a number from 1 to 65535, not {chosen}")
        if not probe(chosen):
            raise SystemExit(f"port {chosen} is in use or not permitted; pick another with --port")
        return chosen
    if probe(80):
        return 80
    while True:
        answer = ask("Port 80 is in use. Which port should the dashboard use? [8080] ").strip()
        if not answer:
            if probe(DEFAULT_PORT):
                return DEFAULT_PORT
            print("  that port is in use or not permitted; pick another")
            continue
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


def _flag_value(argv: list[str], index: int, error: str) -> tuple[str, int]:
    """The value of a space-separated flag, advancing past it.

    A flag at the very end of argv has no value, which is a different mistake
    from a value that is not a valid choice, and gets its own message.
    """
    if index + 1 >= len(argv):
        raise SystemExit(error)
    return argv[index + 1], index + 1


def _parse_port(text: str) -> int:
    """A --port value as an integer, or the reason it is not one.

    isdigit() alone is not enough: it accepts superscript digits such as '²'
    that int() then rejects with ValueError, which would traceback out of a
    script piped into root. The range check mirrors the interactive prompt.
    """
    try:
        port = int(text)
    except ValueError:
        raise SystemExit("--port needs a number, for example: --port 8080") from None
    if not 1 <= port <= 65535:
        raise SystemExit("--port must be a number from 1 to 65535")
    return port


def parse_args(argv: list[str]) -> Args:
    """The flags that let this run without a person watching.

    --yes and --port make an install answerable from a terminal-less cron or
    pipe; --repo and --ref let the same install target a fork or a pinned
    release instead of whatever sits on the project's default branch. All four
    default to the boring values, so a bare invocation is a normal install.
    Both the space and the = form are accepted, because the management command
    accepts both and a typo'd flag has to be refused rather than silently
    ignored — the previous membership-test parsing did exactly that with
    --port=8080, landing an install on port 80 while the operator believed
    they had chosen 8080.
    """
    yes = False
    port: int | None = None
    repo = REPO_URL
    ref: str | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--yes":
            yes = True
        elif arg == "--port":
            value, index = _flag_value(
                argv, index, "--port needs a number, for example: --port 8080"
            )
            port = _parse_port(value)
        elif arg.startswith("--port="):
            port = _parse_port(arg.split("=", 1)[1])
        elif arg == "--repo":
            value, index = _flag_value(argv, index, "--repo needs a URL or path")
            repo = value
        elif arg.startswith("--repo="):
            repo = arg.split("=", 1)[1]
        elif arg == "--ref":
            value, index = _flag_value(argv, index, "--ref needs a branch, tag or commit")
            ref = value
        elif arg.startswith("--ref="):
            ref = arg.split("=", 1)[1]
        else:
            raise SystemExit(f"unrecognized argument: {arg}")
        index += 1
    return {"yes": yes, "port": port, "repo": repo, "ref": ref}


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
        "  install uv (its Python 3.12 lands in /opt/uv-python when the system's is older)",
        f"  clone {target} into {INSTALL_DIR}",
        f"  create the system user {SERVICE_USER!r}",
        "  give that user ownership of the clone and its data",
        f"  create {CONFIG_DIR} and {DATA_DIR}",
        f"  install a systemd service listening on port {port}",
        f"  install the management command {CLI_SHIM}",
        "  install the daily backup service and timer (writing /var/backups/arraysense)",
        "  enable the service and the backup timer to start at boot",
    ]
    if port < 1024:
        lines.append("  grant CAP_NET_BIND_SERVICE so the service can bind a privileged port")
    lines += [
        "",
        "It will NOT write a configuration file — the first visit to the",
        "dashboard runs the setup wizard, and an existing config would skip it.",
        "",
    ]
    return "\n".join(lines)


def clone_argv(repo: str) -> list[str]:
    """The git clone command, exposed so a test can pin its shape.

    Deliberately no --depth: a shallow clone holds a single commit and git then
    refuses to fast-forward it onto the fetched branch — it cannot see the
    common ancestor, so it calls the histories unrelated. That made every
    installation this installer created unable to upgrade at all, which is the
    one thing the lifecycle CLI exists for; the saving is small and the cost is
    the whole upgrade path.

    The pinned ref is not on this command: git clone --branch takes a branch or
    tag and refuses a bare commit, and --ref is documented as pinning a commit
    too. checkout_argv applies the ref in the clone instead, where git accepts
    branch, tag and commit alike.
    """
    return ["git", "clone", repo, INSTALL_DIR]


def checkout_argv(ref: str) -> list[str]:
    """Check out the pinned ref in the fresh clone.

    A full clone carries every branch's history, so any commit reachable from a
    branch or tag is present to check out; git itself disambiguates a seven-hex
    name between a branch and a shortened sha.
    """
    return ["git", "-C", INSTALL_DIR, "checkout", ref]


def _packaging_file(name: str) -> str:
    """A file from the clone's packaging directory.

    The unit and the backup fragments travel with the source they run; if the
    clone did not land, the file cannot be there, so the failure is a sentence
    rather than a traceback.
    """
    path = os.path.join(INSTALL_DIR, "packaging", name)
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        raise SystemExit(f"cannot read {path}; the clone is incomplete") from None


def _write_file(path: str, text: str) -> None:
    """Write one file the install leaves behind, with a message on failure.

    Every subprocess step gets its OSError turned into a sentence by _step; the
    filesystem writes were the only steps that could still traceback. A failed
    write at this point is a half-install either way, so the message says which
    file failed rather than pretending nothing did.
    """
    try:
        with open(path, "w") as handle:
            handle.write(text)
    except OSError as exc:
        raise SystemExit(f"could not write {path}: {exc}") from None


def unit_text() -> str:
    """The service unit, read from the clone so there is one copy of it.

    The unit travels with the source it runs; the drop-in is the right shape
    for a per-machine tweak, but the unit itself is part of the code.
    """
    return _packaging_file("arraysense.service")


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
    return (
        "#!/bin/sh\n"
        "# sudo runs this with a PATH that does not include uv, and the upgrade\n"
        "# command needs uv; put the directory it lands in first.\n"
        "for d in /root/.local/bin /home/*/.local/bin /usr/local/bin /usr/bin; do\n"
        '  if [ -x "$d/uv" ]; then\n'
        '    PATH="$d:$PATH"\n'
        "    export PATH\n"
        "    break\n"
        "  fi\n"
        "done\n"
        f'exec /usr/bin/env python3 {INSTALL_DIR}/src/arraysense/manage.py "$@"\n'
    )


def find_uv() -> str | None:
    """Where uv actually landed, or None if it did not.

    Its installer puts it in ~/.local/bin, which is not on the PATH of the
    process that just ran that installer — so looking it up by name finds
    nothing and raises FileNotFoundError halfway through an install that has
    already cloned the repository. A uv a non-root user installed in their own
    home is found the same way.
    """
    found = shutil.which("uv")
    if found:
        return found
    candidates = list(UV_CANDIDATES) + sorted(glob.glob("/home/*/.local/bin/uv"))
    for candidate in candidates:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return None


def _step(argv: list[str], *, env: dict[str, str] | None = None) -> int:
    """Run one install step, reporting a missing executable as a failed step.

    subprocess raises FileNotFoundError for a command that is not there, and an
    uncaught traceback halfway through a root install tells the operator
    nothing about which half completed. env is passed through untouched, so the
    uv sync step can steer uv's own Python out of root's home without weakening
    the service sandbox.
    """
    try:
        return subprocess.run(argv, check=False, env=env).returncode
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


def mdns_active() -> bool:
    """Whether this host answers .local names, so the handoff may claim one.

    avahi-daemon is the mDNS responder on Debian and Raspberry Pi OS; without
    it a .local name resolves nowhere at all, and printing it as an address a
    person can open sends them chasing a name that does not answer. A host that
    has no avahi gets only the IP line, never a guessed name.
    """
    return _step(["systemctl", "is-active", "--quiet", "avahi-daemon"]) == 0


def render_handoff(port: int, host: str, *, local: bool) -> str:
    """The addresses a person opens to reach the wizard.

    The .local name is shown only when the host actually answers mDNS — the
    same rule that drops the IP line when no route can be determined, never a
    placeholder that looks like an address. The IP line comes from the routing
    table rather than the hostname lookup, which maps to 127.0.1.1 on Debian.
    The hostname is split at the first dot so an FQDN does not become
    box.example.com.local.
    """
    suffix = "" if port == 80 else f":{port}"
    short_host = host.split(".")[0]
    ip = outbound_ip()
    lines = [""]
    if local:
        lines.append(f"  http://{short_host}.local{suffix}")
    if ip is not None:
        lines.append(f"  http://{ip}{suffix}")
    heading = (
        "Installed. Open either of these to run the setup wizard:"
        if len(lines) == 3
        else "Installed. Open this to run the setup wizard:"
    )
    lines.insert(1, heading)
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
        print(f"To repair a half-installed machine, remove {INSTALL_DIR} and run this again.")
        print("If the install completed, update it with: arraysense upgrade")
        return 1

    try:
        port = resolve_port(chosen=args["port"])
    except NoTerminal:
        print("no controlling terminal, so the port cannot be chosen for you.")
        print("Run this from a terminal, or say which port to use:")
        print(f"  sudo python3 install.py --yes --port {DEFAULT_PORT}")
        return 1
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

    clone = clone_argv(repo=args["repo"])

    # useradd may fail because the user already exists from a previous run;
    # every other step failing means the install is broken and stops here.
    steps = [
        (
            [
                "sh",
                "-c",
                "curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh "
                "&& sh /tmp/uv-install.sh; rc=$?; rm -f /tmp/uv-install.sh; exit $rc",
            ],
            False,
        ),
        (clone, False),
    ]
    if args["ref"] is not None:
        steps.append((checkout_argv(args["ref"]), False))
    steps += [
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
        print(f"Install uv, remove {INSTALL_DIR}, and run this again.")
        return 1

    if (
        _step(
            [uv, "sync", "--project", INSTALL_DIR],
            env={**os.environ, "UV_PYTHON_INSTALL_DIR": UV_PYTHON_INSTALL_DIR},
        )
        != 0
    ):
        print(f"failed: {uv} sync --project {INSTALL_DIR}")
        return 1
    if _step(["chown", "-R", f"{SERVICE_USER}:{SERVICE_USER}", INSTALL_DIR]) != 0:
        print("failed: chown the clone to the arraysense user")
        return 1

    unit = unit_text()
    _write_file("/etc/systemd/system/arraysense.service", unit)
    try:
        os.makedirs(DROPIN_DIR, exist_ok=True)
    except OSError as exc:
        raise SystemExit(f"could not create {DROPIN_DIR}: {exc}") from None
    _write_file(os.path.join(DROPIN_DIR, "port.conf"), dropin_text(port))
    _write_file(CLI_SHIM, shim_text())
    try:
        os.chmod(CLI_SHIM, 0o755)
    except OSError as exc:
        raise SystemExit(f"could not make {CLI_SHIM} executable: {exc}") from None

    # The documented one-line install is the only install, so it installs the
    # backup too — the management table promises a daily compressed copy, and a
    # promise nothing on the machine keeps is how a database loss stays lost.
    for source, dest in BACKUP_FILES:
        _write_file(dest, _packaging_file(source))
    if _step(["systemd-tmpfiles", "--create"]) != 0:
        print("failed: systemd-tmpfiles --create")
        return 1
    if _step(["systemctl", "daemon-reload"]) != 0:
        print("systemctl daemon-reload failed; the units may not be loadable")
        return 1
    if _step(["systemctl", "enable", "--now", "arraysense"]) != 0:
        print("the service is installed but was not enabled for boot:")
        print("  systemctl enable --now arraysense")
        return 1
    if _step(["systemctl", "enable", "--now", "arraysense-backup.timer"]) != 0:
        print("the backup timer is installed but was not enabled for boot:")
        print("  systemctl enable --now arraysense-backup.timer")
        return 1

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

    print(render_handoff(port, socket.gethostname(), local=mdns_active()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
