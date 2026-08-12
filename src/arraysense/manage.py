"""manage.py — the lifecycle CLI: status, upgrade, logs, restart, uninstall.

Run by /usr/local/bin/arraysense under the SYSTEM interpreter, never the
virtualenv. `upgrade` rebuilds that virtualenv while it is running, and a CLI
living inside it would be pulling the floor up behind itself.

Stdlib only, and written to parse on Python 3.8, because the distribution's own
interpreter is what runs this — uv's 3.12 belongs to the service, not here.
"""

from __future__ import annotations

import datetime
import glob
import gzip
import json
import os
import shutil
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
CLI_SHIM = "/usr/local/bin/arraysense"
UNIT_PATH = "/etc/systemd/system/arraysense.service"
DROPIN_DIR = "/etc/systemd/system/arraysense.service.d"

# The backup lands on a different disk — the SD card on the reference
# installation, which is exactly where the database does not live because a card
# wears out under sustained writes. Only the compressed file is written there;
# the working copy stays beside the database.
BACKUP_DIR = "/var/backups/arraysense"
# How many compressed daily copies to keep. 14 of them at ~21 MB each is under
# half a gigabyte, and the measured 7.2 GB a year of card writes is the whole
# point of compressing.
BACKUP_KEEP = 14

# "Upgrade" fast-forwards the install to this remote-tracking branch: main is
# the branch that has run on the reference installation, while dev is where a
# change proves itself before it is merged to main.
TRACKING_BRANCH = "origin/main"

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


def setup_url(port: int) -> str:
    """The setup endpoint, which is all a not-yet-configured service serves."""
    return f"http://127.0.0.1:{port}/api/setup"


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


def service_state(port: int, timeout: float = 5.0) -> tuple[str, dict[str, Any] | None]:
    """What the service is doing: collecting, waiting for setup, or not there.

    A fresh install has no configuration, and that absence is deliberately what
    puts the service into setup mode so the wizard runs. Setup mode serves
    /api/setup and nothing else, so a check that only knows /api/status reads a
    perfectly healthy new installation as a dead one — which is what the
    installer told every new user before this existed.
    """
    body = _probe(status_url(port), timeout=timeout)
    if body is not None:
        return ("collecting", body)
    body = _probe(setup_url(port), timeout=timeout)
    if body is not None:
        return ("setup", body)
    return ("down", None)


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


def wait_until_up(
    port: int, timeout: float = HEALTH_TIMEOUT, sleep: float = 2.0
) -> tuple[str, dict[str, Any] | None]:
    """Wait until the service answers at all, either collecting or in setup.

    The installer needs this rather than wait_until_healthy: a first install
    has nothing to collect with yet, and demanding a live collector there fails
    every successful install.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state, body = service_state(port)
        if state != "down":
            return (state, body)
        time.sleep(sleep)
    return ("down", None)


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

    ``readable`` separates the two ways "no range" happens, and the whole
    project exists for this distinction: a file that could not be opened is not
    an empty database, and printing it as one told a real owner their 668 days
    of history were gone. The default is False because every early return here
    is a thing that was not read.
    """
    facts: dict[str, Any] = {"bytes": 0, "first": None, "last": None, "readable": False}
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
    # The query returned, so the file genuinely opened; whether it has rows is
    # a separate question answered below.
    facts["readable"] = True
    if row and row[0] is not None:
        # Local dates, deliberately. energy.py cuts every calendar day in the
        # installation's local zone, so a UTC date here would disagree with the
        # History page about the same database. zoneinfo is 3.9+, so the
        # machine's zone is both the best available and normally the right one.
        facts["first"] = datetime.datetime.fromtimestamp(row[0]).date().isoformat()
        facts["last"] = datetime.datetime.fromtimestamp(row[1]).date().isoformat()
    return facts


def backup_now(source: str, dest_dir: str, keep: int, stamp: str) -> str | None:
    """Write one verified, compressed copy of the database, then rotate.

    The uncompressed working copy is made beside the source with SQLite's own
    backup API, because the database lives in WAL mode and a plain file copy of
    the .db alone would miss the -wal and produce a torn snapshot. Nothing is
    trusted until PRAGMA quick_check says ok, and nothing is renamed into place
    until the gzip write has completed, so an interrupted run never leaves a
    file that looks finished. Rotation comes last, and only on success: deleting
    an old backup before the new one is written and verified turns a failed run
    into data loss. Returns the path written, or None when nothing was written
    — in which case no existing backup has been touched.
    """
    if not os.path.isfile(source):
        print(f"no database file at {source}; nothing to back up")
        return None
    try:
        conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"could not read the database at {source}: {exc}")
        return None
    work_path = os.path.join(
        os.path.dirname(source) or ".", os.path.basename(source) + ".backup.tmp"
    )
    try:
        # A temp file a crashed run left behind would otherwise fail the next
        # backup before it starts.
        _remove_path(work_path)
        work = sqlite3.connect(work_path)
        try:
            conn.backup(work)
        finally:
            work.close()
    except sqlite3.Error as exc:
        print(f"could not copy the database to {work_path}: {exc}")
        _remove_path(work_path)
        return None
    finally:
        conn.close()

    try:
        check = sqlite3.connect(work_path)
        try:
            row = check.execute("PRAGMA quick_check").fetchone()
        finally:
            check.close()
    except sqlite3.Error as exc:
        print(f"could not verify the copy at {work_path}: {exc}")
        _remove_path(work_path)
        return None
    if row is None or row[0] != "ok":
        print("the copy failed its integrity check; no backup written")
        _remove_path(work_path)
        return None

    dest_path = os.path.join(dest_dir, f"arraysense-{stamp}.db.gz")
    part_path = dest_path + ".part"
    try:
        with open(work_path, "rb") as src, gzip.open(part_path, "wb") as out:
            shutil.copyfileobj(src, out)
    except OSError as exc:
        print(f"could not compress the backup to {part_path}: {exc}")
        _remove_path(part_path)
        return None
    try:
        os.replace(part_path, dest_path)
    except OSError as exc:
        print(f"could not move the finished backup into place at {dest_path}: {exc}")
        _remove_path(part_path)
        return None
    _remove_path(work_path)

    for stale in sorted(glob.glob(os.path.join(dest_dir, "arraysense-*.db.gz")))[:-keep]:
        try:
            os.remove(stale)
        except OSError as exc:
            print(f"could not remove the old backup {stale}: {exc}")
    return dest_path


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
    state, body = service_state(port)
    if state == "down":
        print(f"service: not answering on port {port}")
        print("  try: arraysense logs")
        return 1
    # service_state returns a body for every state above "down"; a None body
    # here would be a contract violation, and crashing on it is better than
    # printing "None" where a version belongs.
    assert body is not None
    if state == "setup":
        # No collector and no driver exist yet, so printing either would be a
        # fault that is not there; absent is absent.
        print("service:   running, waiting for setup")
        print("           no configuration yet — open the dashboard to run the wizard")
        print(f"version:   {body.get('version')}")
        return 0
    print(f"version:   {body.get('version')}")
    staleness = (body.get("staleness") or {}).get("verdict")
    print(
        f"collector: running={body.get('running')} connected={body.get('connected')} "
        f"staleness={staleness}"
    )
    print(driver_line(_probe(capabilities_url(port), timeout=5.0)))
    facts = database_facts(_database_path())
    size = f"{facts['bytes'] / 1_048_576:.1f} MB"
    if not facts["readable"]:
        # The database is owned by the service user by design; reading it is a
        # privileged act, so the remedy is sudo rather than a repair.
        span = "unreadable — run with sudo to see the date range"
    elif facts["first"] is None:
        span = "empty"
    else:
        span = f"{facts['first']} .. {facts['last']}"
    print(f"database:  {size}, {span}")
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
    """Restart and prove it came back, rather than trusting systemctl.

    Up is not the same as collecting: a first install restarts into setup mode
    with no config to collect under, and that is a success, not the outage the
    health check would call it.
    """
    if not service("restart"):
        print("systemctl restart failed; try: arraysense logs")
        return 1
    port = configured_port()
    state, _body = wait_until_up(port)
    if state == "down":
        print("restarted, but the collector did not come back within 90s")
        print("  try: arraysense logs")
        return 1
    if state == "setup":
        print("restarted; waiting for setup — open the dashboard to run the wizard")
        return 0
    print("restarted and collecting")
    return 0


def cmd_version(argv: list[str]) -> int:
    """Name the installed code and what the running service reports."""
    commit = run(["git", "-C", INSTALL_DIR, "rev-parse", "--short", "HEAD"]).stdout.strip()
    state, body = service_state(configured_port())
    version = body.get("version") if body else None
    if state == "down":
        version = "not answering"
    elif version is None:
        version = "unknown"
    print(f"version: {version}")
    print(f"commit:  {commit or 'unknown'}")
    return 0


def current_commit() -> str:
    """The commit the install is on, which is also the rollback target.

    The rollback needs an unambiguous pointer, so a full hash is taken rather
    than the short one printed elsewhere for human reading.
    """
    return run(["git", "-C", INSTALL_DIR, "rev-parse", "HEAD"]).stdout.strip()


def is_dirty() -> bool:
    """Whether anything under the install has been edited by hand.

    A local modification would be silently overwritten by the merge, so the
    refusal has to happen before anything is fetched or applied — the file
    being replaced is the very code running this command.
    """
    return bool(run(["git", "-C", INSTALL_DIR, "status", "--porcelain"]).stdout.strip())


def _unshallow_if_needed() -> None:
    """Give a shallow clone its history back, so a fast-forward is possible.

    Installs made by earlier versions of the installer were cloned with
    --depth 1, and git refuses to fast-forward a shallow clone onto a fetched
    branch — it cannot see the common ancestor, so it calls the histories
    unrelated. Without this those installations could never be upgraded at all,
    which is the one thing this command exists to do.
    """
    shallow = run(["git", "-C", INSTALL_DIR, "rev-parse", "--is-shallow-repository"])
    if shallow.stdout.strip() != "true":
        return
    print("this installation was cloned without history; fetching it now")
    result = run(["git", "-C", INSTALL_DIR, "fetch", "--unshallow", "origin"])
    if result.returncode != 0:
        print("could not fetch the full history:")
        print(result.stderr.strip())


def _pending_commits() -> list[str]:
    """Subjects between here and the tracking branch, newest last.

    Shown before anything is applied so an owner can see what an upgrade means
    before agreeing to it. Fetched first so the comparison is against what the
    remote actually holds rather than a stale local view.
    """
    _unshallow_if_needed()
    run(["git", "-C", INSTALL_DIR, "fetch", "--quiet", "origin"])
    out = run(
        [
            "git",
            "-C",
            INSTALL_DIR,
            "log",
            "--oneline",
            "--no-merges",
            f"HEAD..{TRACKING_BRANCH}",
        ]
    ).stdout.strip()
    return [line for line in out.splitlines() if line]


def _confirm(prompt: str) -> bool:
    """Ask a yes/no question on the terminal; anything but yes means no.

    A refused upgrade must change nothing, so an empty answer defaults to no,
    and EOF (no terminal attached at all) is a no rather than a silent yes.
    """
    try:
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _incoming_changelog() -> str:
    """The top changelog entry on the tracking branch, or empty if unreadable.

    The commit subjects say what changed; this says what it means for somebody
    deciding whether to apply it, and it is where a release carrying a schema
    migration announces itself. Read from the tracking branch rather than the
    working tree, because the working tree is still on the old version at the
    point this is shown.
    """
    text = run(["git", "-C", INSTALL_DIR, "show", f"{TRACKING_BRANCH}:CHANGELOG.md"]).stdout
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if line.startswith("## "):
            start = index
            break
    if start is None:
        return ""
    body = [lines[start]]
    fenced = False
    for line in lines[start + 1 :]:
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()


def _sync_and_restart(port: int) -> str | None:
    """Reinstall dependencies and restart; return what went wrong, or None.

    Both the upgrade and the rollback run this same pair, and each of its three
    steps fails for a different reason — a dependency that would not install,
    a unit that would not start, and a service that never answered are three
    different problems, and telling somebody the third when it was the first
    sends them to read the wrong log.

    The final step asks whether the service came back, never whether the
    inverter answered. The dongle accepts exactly one TCP client, so an owner
    opening the EG4 app during an upgrade takes the inverter away for exactly
    as long as the check runs — and rolling back a release because of that
    undoes an upgrade that worked. Connectivity is a note, not a verdict.
    """
    sync = run(["uv", "sync", "--project", INSTALL_DIR])
    if sync.returncode != 0:
        return f"dependencies would not install: {sync.stderr.strip()[:200]}"
    if not service("restart"):
        return "systemctl could not restart the service"
    state, body = wait_until_up(port)
    if state == "down":
        return f"the service did not answer within {int(HEALTH_TIMEOUT)}s"
    if body is not None and body.get("connected") is False:
        print("  the service is up; the inverter is not answering yet")
    return None


def cmd_upgrade(argv: list[str]) -> int:
    """Fetch, show, confirm, apply, verify — and roll back if it did not work.

    The rollback is the reason this exists rather than three shell commands in
    the documentation. A home installation upgrades unattended in the evening,
    and an upgrade that quietly leaves the collector down loses a night of
    readings that cannot be recovered.
    """
    if is_dirty():
        print(f"{INSTALL_DIR} has local modifications; upgrade refused.")
        print(run(["git", "-C", INSTALL_DIR, "status", "--short"]).stdout)
        return 1

    pending = _pending_commits()
    if not pending:
        print("already up to date")
        return 0

    print(f"{len(pending)} change(s) to apply:")
    for line in pending:
        print(f"  {line}")
    entry = _incoming_changelog()
    if entry:
        print()
        print(entry)
        print()
    if not _confirm("Apply these and restart?"):
        print("nothing done")
        return 0

    previous = current_commit()
    port = configured_port()

    if run(["git", "-C", INSTALL_DIR, "merge", "--ff-only", TRACKING_BRANCH]).returncode != 0:
        print("could not fast-forward; upgrade abandoned, nothing changed")
        return 1

    reason = _sync_and_restart(port)
    if reason is None:
        print("upgraded and collecting")
        return 0
    print(reason)
    print(f"rolling back to {previous[:7]}")

    checkout = run(["git", "-C", INSTALL_DIR, "checkout", "--force", previous])
    if checkout.returncode != 0:
        print("ROLLBACK FAILED — could not check out the previous commit.")
        print(checkout.stderr.strip())
        print("the install is still on the new code. Run: arraysense logs")
        return 1

    reason = _sync_and_restart(port)
    if reason is None:
        print(f"rolled back; running {previous[:7]} again (detached HEAD, which is expected)")
    else:
        print(reason)
        print("ROLLBACK ALSO FAILED — the service is down. Run: arraysense logs")
    return 1


def _remove_path(path: str) -> bool:
    """Delete a file or a tree, tolerating one that is already gone.

    Returns whether the path is now absent. A removal that failed has to be
    said out loud: an uninstall that prints "removed" over a file it could not
    delete tells somebody their data is gone when it is still on the disk.
    """
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except FileNotFoundError:
        return True
    except OSError as exc:
        print(f"could not remove {path}: {exc}")
        return False
    return True


def _remove_database(path: str) -> bool:
    """Remove the database and its sidecars, and only ever regular files.

    The purge is authorised against one file. If the configured path names a
    directory — or nothing at all, which a malformed config produces — then
    what was authorised and what would be deleted are not the same thing, and
    this runs as root. A path that is not an ordinary file is refused rather
    than interpreted.
    """
    if not os.path.isabs(path):
        print(f"configured database path is not absolute: {path!r}; nothing deleted")
        return False
    if os.path.isdir(path):
        print(f"configured database path is a directory: {path}; nothing deleted")
        return False
    ok = True
    for suffix in ("", "-wal", "-shm"):
        target = path + suffix
        if os.path.exists(target) and not os.path.isfile(target):
            print(f"{target} is not an ordinary file; left alone")
            ok = False
            continue
        ok = _remove_path(target) and ok
    return ok


def cmd_uninstall(argv: list[str]) -> int:
    """Remove the software. The database survives unless --purge is given.

    Two confirmations rather than one when purging, because the second act
    destroys something no reinstall can bring back.
    """
    purge = "--purge" in argv
    db = _database_path()
    print(f"This removes the service, the code at {INSTALL_DIR}, and {CLI_SHIM}.")
    if purge:
        print(f"--purge given: {db} and /etc/arraysense WILL ALSO BE DELETED.")
    else:
        print("The database and config are kept. Pass --purge to remove them too.")
    if not _confirm("Continue?"):
        print("nothing done")
        return 0
    if purge and not _confirm(f"Really delete {db}? This cannot be undone."):
        print("nothing done")
        return 0

    if not service("stop"):
        print("could not stop the service; refusing to remove anything")
        print("  try: systemctl status arraysense")
        return 1
    # disable may fail for a unit that was never enabled, and that is not a
    # reason to abandon the uninstall.
    service("disable")
    ok = True
    for path in (UNIT_PATH, DROPIN_DIR, CLI_SHIM, INSTALL_DIR):
        ok = _remove_path(path) and ok
    if purge:
        ok = _remove_database(db) and ok
        ok = _remove_path("/etc/arraysense") and ok
    reload = run(["systemctl", "daemon-reload"])
    if not ok:
        print("some paths could not be removed; see above")
        return 1
    if reload.returncode != 0:
        print("removed, but systemd did not reload; a stale unit reference may remain")
        print(f"  systemctl daemon-reload: {reload.stderr.strip()[:200]}")
        return 1
    print("removed")
    return 0


def _backup_keep(raw: str) -> int | None:
    """Parse a --keep argument, or None after printing why it is refused."""
    try:
        keep = int(raw)
    except ValueError:
        print(f"--keep wants a number, not {raw!r}")
        return None
    if keep < 1:
        print("--keep must be at least 1, or rotation would delete every backup")
        return None
    return keep


def cmd_backup(argv: list[str]) -> int:
    """Write a compressed daily backup to a different disk, then rotate.

    The destination is the SD card on the reference installation, and the rule
    that shapes everything is that only the compressed file ever lands there:
    the uncompressed working copy is written beside the database itself, because
    a 264 MB copy gzips to 21 MB and writing the larger figure to the card would
    undo the reason the database was moved off it. The restore recipe is printed
    after a successful run because a backup nobody knows how to restore is not a
    backup.
    """
    dest_dir = BACKUP_DIR
    keep = BACKUP_KEEP
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg in ("--dir", "--keep"):
            index += 1
            if index >= len(argv):
                print(f"{arg} needs a value")
                return 1
            value = argv[index]
            if arg == "--dir":
                dest_dir = value
            else:
                parsed = _backup_keep(value)
                if parsed is None:
                    return 1
                keep = parsed
        elif arg.startswith("--dir="):
            dest_dir = arg.split("=", 1)[1]
        elif arg.startswith("--keep="):
            parsed = _backup_keep(arg.split("=", 1)[1])
            if parsed is None:
                return 1
            keep = parsed
        else:
            print("usage: arraysense backup [--dir PATH] [--keep N]")
            return 1
        index += 1

    source = _database_path()
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as exc:
        print(f"could not create {dest_dir}: {exc}")
        return 1
    written = backup_now(source, dest_dir, keep, datetime.date.today().isoformat())
    if written is None:
        return 1
    try:
        size_text = f"{os.path.getsize(written) / 1_048_576:.1f} MB"
    except OSError:
        size_text = "size unavailable"
    kept = len(glob.glob(os.path.join(dest_dir, "arraysense-*.db.gz")))
    print(f"backup: {written} ({size_text}), keeping {kept}")
    print("restore with:")
    print(f"  sudo systemctl stop {SERVICE}")
    print(f"  sudo gunzip -c {written} | sudo -u arraysense tee {source} >/dev/null")
    print(f"  sudo systemctl start {SERVICE}")
    return 0


COMMANDS = {
    "status": cmd_status,
    "logs": cmd_logs,
    "restart": cmd_restart,
    "version": cmd_version,
    "backup": cmd_backup,
}
COMMANDS["upgrade"] = cmd_upgrade
COMMANDS["uninstall"] = cmd_uninstall


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
