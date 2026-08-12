"""manage.py — the lifecycle CLI: status, upgrade, logs, restart, uninstall.

Run by /usr/local/bin/arraysense under the SYSTEM interpreter, never the
virtualenv. `upgrade` rebuilds that virtualenv while it is running, and a CLI
living inside it would be pulling the floor up behind itself.

Stdlib only, and written to parse on Python 3.8, because the distribution's own
interpreter is what runs this — uv's 3.12 belongs to the service, not here.
"""

from __future__ import annotations

import contextlib
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

# Where the backup writes, how many it keeps, whether it runs and when — all
# four are settings now, read from the running service over HTTP. What follows
# is only what to use when the service cannot be asked, which happens on a box
# whose service is down and is exactly when a backup matters most.
#
# This module cannot import the registry those defaults belong to: it runs under
# the distribution's Python 3.8 while the package needs 3.12. So the values are
# duplicated here, deliberately and visibly, and ``tests/test_manage.py`` fails
# the moment the copy disagrees with the registry. Without that test this is the
# "computed in two places" mistake, one of which nobody is looking at.
#
# The backup lands on a different disk — the SD card on the reference
# installation, which is exactly where the database does not live because a card
# wears out under sustained writes. Only the compressed file is written there;
# the working copy stays beside the database.
BACKUP_DIR = "/var/backups/arraysense"
# How many compressed daily copies to keep. 14 of them at ~21 MB each is under
# half a gigabyte, and the measured 7.2 GB a year of card writes is the whole
# point of compressing.
BACKUP_KEEP = 14
# Whether the timer's run does anything. A hand-run backup ignores this: somebody
# typing the command wants a copy now, and a paused schedule is not a refusal.
BACKUP_ENABLED = True
# The hour and minute, on the installation's own clock, after which the day's
# backup may run. The timer fires every fifteen minutes and asks; these decide.
BACKUP_HOUR = 3
BACKUP_MINUTE = 15

# The registry keys these stand in for, mapped to the value used when the
# service does not report one. Spelled as the registry spells them so a renamed
# setting fails the drift test rather than falling back silently forever.
BACKUP_FALLBACK = {
    "backup.enabled": BACKUP_ENABLED,
    "backup.directory": BACKUP_DIR,
    "backup.keep": BACKUP_KEEP,
    "backup.hour": BACKUP_HOUR,
    "backup.minute": BACKUP_MINUTE,
}
# Not in the fallback map: an unset zone is the registry's own default and means
# "follow the machine's clock", which is a decision rather than a gap, so it is
# never reported as one.
TIMEZONE_KEY = "site.timezone"

# What a held lock means, in one place so the reason _take_lock reports and the
# message the caller prints cannot drift apart.
LOCK_BUSY = "another backup is running"

# "Upgrade" fast-forwards the install to this remote-tracking branch: main is
# the branch that has run on the reference installation, while dev is where a
# change proves itself before it is merged to main.
TRACKING_BRANCH = "origin/main"

# What "healthy" means, and why it is not "systemctl says active": the unit is
# active the moment the process starts, which is before it binds the port and
# well before the first poll has reached the inverter. An upgrade that trusted
# `systemctl start` would report success over a collector that never came back.
HEALTH_TIMEOUT = 90.0


# uv's installer puts the binary in ~/.local/bin — for the root that runs this,
# /root/.local/bin — which is not on sudo's secure_path. A bare "uv" lookup fails
# there, and the failure is an uncaught FileNotFoundError halfway through an
# upgrade that has already fast-forwarded the code. Searching these places after
# PATH, in the order a bootstrap leaves them, matches what install.py does.
UV_CANDIDATES = ("/root/.local/bin/uv", "/usr/local/bin/uv", "/usr/bin/uv")


def find_uv() -> str | None:
    """Where uv actually landed, or None so the caller can say why it cannot proceed.

    uv's installer puts it in ~/.local/bin, which is not on sudo's secure_path
    and therefore not on the PATH of this process when invoked as
    ``sudo arraysense upgrade``. A bare name lookup fails there, and the
    failure is an uncaught FileNotFoundError after git has already
    fast-forwarded the install to the new commit — leaving new code with old
    dependencies and no rollback.
    """
    found = shutil.which("uv")
    if found:
        return found
    for candidate in UV_CANDIDATES:
        expanded = os.path.expanduser(candidate)
        if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
            return expanded
    return None


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


def settings_url(port: int) -> str:
    """Where the stored settings are read from, on the same box as everything else.

    The service already merges registry defaults over whatever is stored, so
    one GET answers "what is configured" without this file knowing anything
    about the registry, the database schema, or how a default is spelled.
    """
    return f"http://127.0.0.1:{port}/api/settings"


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

    "collecting" means the service answered /api/status AND reports its
    collector running. An HTTP reply from a process whose collector loop is
    dead is not "collecting" — calling it that printed "restarted and
    collecting" over a dead collector, and let an upgrade rollback skip its
    one job.
    """
    body = _probe(status_url(port), timeout=timeout)
    if body is not None:
        if body.get("running"):
            return ("collecting", body)
        return ("answering", body)
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

    Read from every drop-in in lexicographic order, because systemd merges all
    ``*.conf`` files in the drop-in directory and the last ``ExecStart=`` in
    the merged set wins. Reading only ``port.conf`` missed a port set in any
    other drop-in, and then every command probed a port the service was not
    listening on — reporting a healthy service as down.
    """
    port = DEFAULT_PORT
    dropins: list[str] = []
    try:
        dropins = sorted(os.listdir(DROPIN_DIR))
    except OSError:
        return port
    for name in dropins:
        if not name.endswith(".conf"):
            continue
        try:
            with open(os.path.join(DROPIN_DIR, name)) as handle:
                text = handle.read()
        except OSError:
            continue
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

    A line starting ``database_path`` with no ``=`` on it would crash every
    command that resolves the database path with an uncaught IndexError —
    including uninstall before its first confirmation prompt, making the
    software un-uninstallable through its own CLI.
    """
    try:
        with open(CONFIG_PATH) as handle:
            for line in handle:
                if line.strip().startswith("database_path"):
                    parts = line.split("=", 1)
                    if len(parts) < 2:
                        continue
                    value = _cut_inline_comment(parts[1])
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

    ``bytes`` is None when the file was not measured — printing 0.0 MB for a
    file nothing stat'ed is the same class of bug as printing "empty" for a
    database nothing opened. ``reason`` says which step failed, so the caller
    can print the right remedy instead of blaming permissions for an absent
    file or a file that opened but had the wrong schema.
    """
    facts: dict[str, Any] = {
        "bytes": None,
        "first": None,
        "last": None,
        "readable": False,
        "reason": None,
    }
    try:
        facts["bytes"] = os.path.getsize(path)
    except OSError as exc:
        facts["reason"] = f"could not stat {path}: {exc}"
        return facts
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        facts["reason"] = f"could not open {path}: {exc}"
        return facts
    try:
        row = conn.execute("SELECT MIN(timestamp), MAX(timestamp) FROM inverter_raw").fetchone()
    except sqlite3.Error as exc:
        facts["reason"] = f"could not read from {path}: {exc}"
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


def _take_lock(path: str) -> tuple[int | None, str]:
    """Claim the right to run, and say which way it failed if it could not.

    Two runs overlapping is not hypothetical — a hand-run backup does not go
    through systemd's serialisation of a oneshot unit. They share a working
    path and a destination name, and interleaved they can publish a verified
    archive of an empty database while rotating a real backup away. And two
    runs overlapping and a directory that cannot be written are different
    problems with different remedies, so the caller is told which happened: a
    backup that blames the first when it was the second sends somebody looking
    for a process that does not exist.

    A lock left behind by a SIGKILL, an OOM kill or a power cut during a
    backup is not a running backup — but without a staleness check every
    subsequent nightly run sees FileExistsError and prints "another backup is
    running" forever. The lock records the holder's PID so staleness can be
    detected: if the process is gone the lock is stale and is removed, letting
    the next backup proceed rather than failing silently every night.
    """
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _check_stale_lock(path)
    except OSError as exc:
        return (None, f"could not create the backup lock at {path}: {exc}")
    try:
        os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(path)
        return (None, f"could not write the lock at {path}")
    return (fd, "")


def _check_stale_lock(path: str) -> tuple[int | None, str]:
    """Read a lock file and decide whether its holder is still alive.

    A lock whose PID is gone is stale and is removed, so the next attempt
    proceeds rather than printing "another backup is running" forever.
    A lock that cannot be read at all is treated as live — the directory
    may be unwritable, and the caller's existing message for that is clearer.
    """
    try:
        with open(path) as handle:
            lines = handle.readlines()
    except OSError:
        return (None, LOCK_BUSY)
    if len(lines) < 2:
        # Lock written by an older version that did not record a PID — treat
        # it as live because we cannot establish staleness.
        return (None, LOCK_BUSY)
    try:
        pid = int(lines[0].strip())
    except (ValueError, IndexError):
        return (None, LOCK_BUSY)
    if _pid_is_alive(pid):
        return (None, LOCK_BUSY)
    # The holder is gone; remove the stale lock and try again.
    with contextlib.suppress(OSError):
        os.remove(path)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except (FileExistsError, OSError) as exc:
        if isinstance(exc, FileExistsError):
            return (None, LOCK_BUSY)
        return (None, f"could not create the backup lock at {path}: {exc}")
    try:
        os.write(fd, f"{os.getpid()}\n{time.time()}\n".encode())
    except OSError:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.remove(path)
        return (None, f"could not write the lock at {path}")
    return (fd, "")


def _pid_is_alive(pid: int) -> bool:
    """True when a process with the given PID exists and can be signalled."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _verify_working_copy(path: str) -> bool:
    """A working copy must exist and be non-empty before it is trusted.

    sqlite3.connect creates a new file when the path does not exist, and
    quick_check on an empty database returns ok. Two overlapping runs, one
    deletes the other's copy, the survivor opens a path that is now absent
    and gets an empty database passing — this guard catches that path.
    """
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _whole(raw: object, low: int, high: int | None = None) -> int | None:
    """A reported number as a whole one inside its range, or None for anything else.

    None means the service said nothing usable, never a plausible stand-in: an
    hour of 99 believed is a backup that never becomes due again, and a value
    invented here would be indistinguishable from one somebody chose.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        number = raw
    elif isinstance(raw, float) and raw == int(raw):
        number = int(raw)
    else:
        return None
    if number < low or (high is not None and number > high):
        return None
    return number


def _read_backup_value(key: str, raw: object) -> object:
    """Coerce one reported setting, or None when the service gave nothing usable.

    Each is read to the shape the CLI needs rather than trusted as it arrives.
    The service validates on write, but this file also has to survive a
    database edited by hand and a service older than these settings.
    """
    if key == "backup.enabled":
        return raw if isinstance(raw, bool) else None
    if key == "backup.directory":
        return raw.strip() if isinstance(raw, str) and raw.strip() else None
    if key == "backup.keep":
        # The floor is this file's own rule: rotation slices the list by
        # ``[:-keep]``, which stops rotating at all below one. The ceiling is
        # the registry's business, and a bound copied here would be one more
        # thing to keep in step.
        return _whole(raw, 1)
    if key == "backup.hour":
        return _whole(raw, 0, 23)
    if key == "backup.minute":
        return _whole(raw, 0, 59)
    return None


def backup_settings(port: int, timeout: float = 5.0) -> tuple[dict[str, Any], str]:
    """What the service says the backup should do, and what was not answered.

    Read over HTTP because this file cannot import the settings registry: it
    runs on the distribution's Python 3.8 and the package needs 3.12. The
    endpoint already merges the registry's defaults over whatever is stored,
    so one GET answers the whole question without this file knowing how a
    default is spelled.

    The second element is empty when every value came from the service, and
    otherwise says which ones did not and that the built-in copies were used
    instead. A fallback nobody is told about is a second source of truth
    pretending to be the first — and on a box whose service is down, which is
    exactly when a backup matters most, that is the state the CLI is in.
    """
    body = _probe(settings_url(port), timeout=timeout)
    values = body.get("values") if body is not None else None
    conf: dict[str, Any] = {}
    if not isinstance(values, dict):
        conf.update(BACKUP_FALLBACK)
        conf[TIMEZONE_KEY] = ""
        return (
            conf,
            f"the service is not answering on port {port}, so the built-in backup "
            "defaults are being used rather than the stored settings",
        )
    unanswered = []
    for key in sorted(BACKUP_FALLBACK):
        value = _read_backup_value(key, values.get(key))
        if value is None:
            unanswered.append(key)
            value = BACKUP_FALLBACK[key]
        conf[key] = value
    zone = values.get(TIMEZONE_KEY)
    conf[TIMEZONE_KEY] = zone.strip() if isinstance(zone, str) else ""
    if not unanswered:
        return (conf, "")
    named = ", ".join(unanswered)
    return (
        conf,
        f"the service reported nothing usable for {named}, so the built-in "
        "defaults are being used for them",
    )


def _zone(name: str) -> datetime.tzinfo | None:
    """The installation's zone, or None meaning the machine's own clock.

    An empty setting means "follow the machine", which is the registry's
    default and what every date in this CLI used before the setting existed.
    zoneinfo arrived in 3.9 and this file has to run on 3.8, so its absence is
    the same answer rather than an error.
    """
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        return None
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError):
        # ZoneInfoNotFoundError is a KeyError. A zone this machine's tz
        # database has never heard of is not worth failing a backup over.
        return None


def _at_zone(moment: datetime.datetime, zone_name: str) -> datetime.datetime:
    """Place an instant on the installation's calendar.

    Both questions the scheduled run asks — has the configured time passed,
    and has today already been backed up — are local-calendar questions, and
    they are cut in the installation's own zone for the same reason energy.py
    cuts a day there: the archive is named for a date, and a UTC date names a
    different day for anybody east or west of Greenwich after their evening.
    """
    return moment.astimezone(_zone(zone_name))


def _local_now(zone_name: str) -> datetime.datetime:
    """Now, on the installation's clock.

    Taken as an instant and then placed, rather than read straight off the
    local clock, so the conversion the schedule depends on is the same line of
    code the tests drive with a known instant.
    """
    # datetime.UTC is the 3.11 spelling of this and this file runs on 3.8.
    return _at_zone(datetime.datetime.now(datetime.timezone.utc), zone_name)  # noqa: UP017


def _time_has_passed(now: datetime.datetime, hour: int, minute: int) -> bool:
    """Whether the installation's clock has reached the configured time today.

    Compared as wall-clock fields, never by subtracting or differencing two
    datetimes. Two datetimes sharing a tzinfo subtract as though they were
    naive, which is the trap this project has already paid for twice — and the
    wall clock is what the question is actually about: a backup configured for
    03:15 runs when the clock in the house says 03:15.

    That reading is also what makes the odd days come out right. On a
    23-hour day a configured 02:30 may never exist, and asking whether the
    clock has passed it still says yes at 03:05, so the day is not skipped. On
    a 25-hour day the configured time passes twice; the caller's check for
    today's archive is what stops the second one writing a duplicate.
    """
    return (now.hour, now.minute) >= (hour, minute)


def archive_name(stamp: str) -> str:
    """The filename for one day's archive, in the one place that decides it.

    Written and looked for by different code paths — the writer, the rotation
    glob, and the scheduled run's "has today already been done" — so a name
    spelled twice is a scheduler that backs up every fifteen minutes.
    """
    return f"arraysense-{stamp}.db.gz"


def _already_written(dest_dir: str, stamp: str) -> bool:
    """Whether the archive for this local day is already on disk."""
    return os.path.exists(os.path.join(dest_dir, archive_name(stamp)))


def _check_backup_dir(path: str) -> bool:
    """Verify the backup destination exists and is writable by this user.

    Creating it here would bake in whatever user happens to run — root on a
    hand-run backup, arraysense on a timer — and the one that runs second
    finds a directory owned by the wrong user. The tmpfiles.d fragment in
    packaging/ sets the right owner once, regardless of who runs first.

    The two failures need different remedies. A directory that does not exist
    needs the tmpfiles fragment applied. A directory that exists with the
    right owner but is not writable by this caller needs sudo — the directory
    is already correct; the caller lacks the privilege to write there.
    """
    if not os.path.isdir(path):
        print(f"{path} does not exist.")
        print(f"  sudo install -d -o arraysense -g arraysense -m 0750 {path}")
        return False
    if not os.access(path, os.W_OK):
        print(f"{path} is not writable by this user; run with sudo")
        return False
    return True


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
    # os.path.isfile swallows PermissionError and returns False, so an
    # unreadable database is reported as absent — the same defect that once
    # told an owner their 668 days of history were gone. os.path.exists has
    # the same flaw. Use os.stat, which raises on any failure, so a
    # permission problem is caught and an inaccessible file is not reported
    # as a missing one.
    try:
        st = os.stat(source)
    except OSError as exc:
        print(f"could not read the database at {source}: {exc}; nothing to back up")
        return None
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        print(f"no database file at {source}; nothing to back up")
        return None

    lock_path = os.path.join(
        os.path.dirname(source) or ".", os.path.basename(source) + ".backup.lock"
    )
    lock_fd, lock_reason = _take_lock(lock_path)
    if lock_fd is None:
        print(lock_reason + "; nothing done")
        if lock_reason != LOCK_BUSY:
            # The lock and the working copy are written beside the database, and
            # a directory that cannot be written is the failure the caller has
            # in front of them — the remedy is not obvious from the error.
            print("  the backup needs write access beside the database. If the database is not in")
            print("  /var/lib/arraysense, add a drop-in naming its directory:")
            print("    [Service]")
            print("    ReadWritePaths=<that directory>")
        return None
    try:
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

        if not _verify_working_copy(work_path):
            print("the working copy is missing or empty; no backup written")
            _remove_path(work_path)
            return None

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

        dest_path = os.path.join(dest_dir, archive_name(stamp))
        part_path = dest_path + ".part"
        try:
            with open(work_path, "rb") as src, gzip.open(part_path, "wb") as out:
                shutil.copyfileobj(src, out)
                out.flush()
                os.fsync(out.fileno())
            # GzipFile.close writes the CRC footer after any fsync taken inside the
            # block, so the finished file is synced again before it is published.
            with open(part_path, "rb") as synced:
                os.fsync(synced.fileno())
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
        # Flush the directory entry so a power cut after rename cannot lose it.
        dir_fd = os.open(dest_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        _remove_path(work_path)

        for stale in sorted(glob.glob(os.path.join(dest_dir, "arraysense-*.db.gz")))[:-keep]:
            try:
                os.remove(stale)
            except OSError as exc:
                print(f"could not remove the old backup {stale}: {exc}")
        return dest_path
    finally:
        os.close(lock_fd)
        with contextlib.suppress(OSError):
            os.remove(lock_path)


def driver_line(body: dict[str, Any] | None) -> str:
    """Name the driver and what it declares, or say plainly that it is unknown.

    Which family the service thinks it is talking to, and how many strings it
    believes exist, is where a support conversation starts. Every field here is
    nullable at the source, and one that was never declared prints as a dash:
    printing a plausible default would be the absent-data rule broken in the
    place it is most likely to be believed.

    A missing ``devices`` key means the reply was not the shape this CLI
    understands — not the same as an explicit empty list, which means the
    source cannot name a device. Conflating them printed "none declared" for
    a body nobody declared, asserting a statement that was never made.
    """
    if body is None:
        return "driver:    unavailable"
    devices = body.get("devices")
    if devices is None:
        return "driver:    unknown (no devices key in response)"
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
    if state == "answering":
        print("service:   the HTTP server is answering but the collector is not running")
        print(f"version:   {body.get('version')}")
        print("  try: arraysense logs")
    else:
        print(f"version:   {body.get('version')}")
        staleness = (body.get("staleness") or {}).get("verdict")
        print(
            f"collector: running={body.get('running')} connected={body.get('connected')} "
            f"staleness={staleness}"
        )
    print(driver_line(_probe(capabilities_url(port), timeout=5.0)))
    facts = database_facts(_database_path())
    size = f"{facts['bytes'] / 1_048_576:.1f} MB" if facts["bytes"] is not None else "unknown size"
    if not facts["readable"]:
        # Say why it could not be read rather than naming sudo for four
        # different causes, three of which sudo cannot fix. The file-missing
        # case on the reference box means the SSD did not mount, which is an
        # urgent fault reported as a permissions nuisance.
        reason = facts.get("reason", "")
        span = f"unreadable — {reason}" if reason else "unreadable"
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
        print(f"restarted, but the service did not answer within {int(HEALTH_TIMEOUT)}s")
        print("  try: arraysense logs")
        return 1
    if state == "setup":
        print("restarted; waiting for setup — open the dashboard to run the wizard")
        return 0
    if state == "answering":
        print("restarted, but the collector is not running")
        print("  try: arraysense logs")
        return 1
    print("restarted and collecting")
    return 0


def cmd_version(argv: list[str]) -> int:
    """Name the installed code and what the running service reports."""
    commit = run(["git", "-C", INSTALL_DIR, "rev-parse", "--short", "HEAD"]).stdout.strip()
    state, body = service_state(configured_port())
    version: str | None = None
    if body is not None:
        version = body.get("version")
    ok = True
    if state == "down":
        version = "not answering"
        ok = False
    elif version is None:
        version = "unknown"
    print(f"version: {version}")
    print(f"commit:  {commit or 'unknown'}")
    return 0 if ok else 1


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

    A non-repo directory (git status exits non-zero) is not "clean" — it is
    an install that cannot be upgraded at all, and returning False here printed
    "already up to date" for a checkout that was not one.
    """
    result = run(["git", "-C", INSTALL_DIR, "status", "--porcelain"])
    if result.returncode != 0:
        return True
    return bool(result.stdout.strip())


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


def _pending_commits() -> list[str] | None:
    """Subjects between here and the tracking branch, newest last; None on failure.

    Shown before anything is applied so an owner can see what an upgrade means
    before agreeing to it. Fetched first so the comparison is against what the
    remote actually holds rather than a stale local view.

    An empty list means there are no commits to apply. None means the
    comparison could not run — the remote was unreachable, the branch does not
    exist, or the directory is not a git repo at all. The caller must
    distinguish them because telling an offline owner they are "already up to
    date" hides a release they need.
    """
    _unshallow_if_needed()
    fetch = run(["git", "-C", INSTALL_DIR, "fetch", "--quiet", "origin"])
    if fetch.returncode != 0:
        print("could not fetch from origin; the remote may be unreachable")
        print(fetch.stderr.strip()[:200])
        return None
    log = run(
        [
            "git",
            "-C",
            INSTALL_DIR,
            "log",
            "--oneline",
            "--no-merges",
            f"HEAD..{TRACKING_BRANCH}",
        ]
    )
    if log.returncode != 0:
        print("could not compare against the tracking branch")
        print(log.stderr.strip()[:200])
        return None
    return [line for line in log.stdout.strip().splitlines() if line]


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
    uv = find_uv()
    if uv is None:
        return (
            "uv was not found on PATH or in the usual locations; "
            + "the upgrade cannot install dependencies"
        )
    try:
        sync = run([uv, "sync", "--project", INSTALL_DIR])
    except OSError as exc:
        return f"could not run uv: {exc}"
    if sync.returncode != 0:
        return f"dependencies would not install: {sync.stderr.strip()[:200]}"
    if not service("restart"):
        return "systemctl could not restart the service"
    state, body = wait_until_up(port)
    if state == "down":
        return f"the service did not answer within {int(HEALTH_TIMEOUT)}s"
    if state == "answering":
        return "the service answered but the collector is not running"
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
    if pending is None:
        print("upgrade abandoned — could not determine what is available")
        return 1
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
        state, _body = service_state(port)
        if state == "collecting":
            print("upgraded and collecting")
        elif state == "answering":
            print("upgraded, but the collector is not running")
        elif state == "setup":
            print("upgraded; waiting for setup — open the dashboard to run the wizard")
        else:
            print("upgraded, but the service did not answer after restart")
        return 0
    print(reason)
    print(f"rolling back to {previous[:7]}")

    checkout = run(["git", "-C", INSTALL_DIR, "checkout", "--force", previous])
    if checkout.returncode != 0:
        print("ROLLBACK FAILED — could not check out the previous commit.")
        print(checkout.stderr.strip())
        print("the install is still on the new code. Run: arraysense logs")
        return 1

    rollback_reason = _sync_and_restart(port)
    if rollback_reason is None:
        print(f"rolled back; running {previous[:7]} again (detached HEAD, which is expected)")
    else:
        print(rollback_reason)
        print("ROLLBACK ALSO FAILED. Run: arraysense logs")
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
    destroys something no reinstall can bring back. The backup units and
    their tmpfiles fragment are also removed — an uninstall that prints
    "removed" while leaving an enabled timer that fails every night forever
    is not an uninstall.
    """
    purge = "--purge" in argv
    db = _database_path()
    print(f"This removes the service, the code at {INSTALL_DIR}, and {CLI_SHIM}.")
    if purge:
        print(f"--purge given: {db} and /etc/arraysense WILL ALSO BE DELETED.")
        # Where the backups actually are, asked while the service is still
        # running to answer. The destination is a setting, so naming the
        # built-in default would send somebody to an empty directory and
        # leave the copies they were told about sitting on a disk elsewhere.
        conf, _reason = backup_settings(configured_port())
        # Bound out of the f-string: nesting the same quote inside one is a
        # syntax error before 3.12, and this file runs on 3.8.
        where = conf["backup.directory"]
        print(f"  compressed backups in {where} are not removed by --purge; delete them by hand")
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
    # Stop and disable the backup timer so it does not fire every night
    # pointing at an ExecStart that no longer exists.
    run(["systemctl", "stop", "arraysense-backup.timer"])
    run(["systemctl", "disable", "arraysense-backup.timer"])
    ok = True
    for path in (
        UNIT_PATH,
        DROPIN_DIR,
        CLI_SHIM,
        INSTALL_DIR,
        "/etc/systemd/system/arraysense-backup.service",
        "/etc/systemd/system/arraysense-backup.timer",
        "/etc/systemd/system/arraysense-backup.service.d",
        "/etc/tmpfiles.d/arraysense-backup.conf",
    ):
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

    Where it writes, how many it keeps, whether it runs at all and when are the
    installation's settings, read from the running service. A flag still beats
    them, because somebody typing ``--dir`` is answering a different question
    from the one the settings answer.

    ``--scheduled`` is the timer's mode and nobody else's. The timer fires
    every fifteen minutes and this decides whether there is anything to do:
    the backup must be enabled, the configured time must have passed on the
    installation's own clock, and today's archive must not already exist. A
    run that is not due returns silently — ninety-six firings a day, each
    announcing that it did nothing, would bury the one that mattered. A
    hand-run ``arraysense backup`` ignores all three and backs up now.
    """
    scheduled = False
    dir_flag: str | None = None
    keep_flag: int | None = None
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--scheduled":
            scheduled = True
        elif arg in ("--dir", "--keep"):
            index += 1
            if index >= len(argv):
                print(f"{arg} needs a value")
                return 1
            value = argv[index]
            if arg == "--dir":
                dir_flag = value
            else:
                parsed = _backup_keep(value)
                if parsed is None:
                    return 1
                keep_flag = parsed
        elif arg.startswith("--dir="):
            dir_flag = arg.split("=", 1)[1]
        elif arg.startswith("--keep="):
            parsed = _backup_keep(arg.split("=", 1)[1])
            if parsed is None:
                return 1
            keep_flag = parsed
        else:
            print("usage: arraysense backup [--dir PATH] [--keep N] [--scheduled]")
            return 1
        index += 1

    conf, fallback = backup_settings(configured_port())
    dest_dir = conf["backup.directory"] if dir_flag is None else dir_flag
    keep = conf["backup.keep"] if keep_flag is None else keep_flag
    now = _local_now(conf[TIMEZONE_KEY])
    stamp = now.date().isoformat()

    if scheduled:
        if not conf["backup.enabled"]:
            return 0
        if not _time_has_passed(now, conf["backup.hour"], conf["backup.minute"]):
            return 0
        if _already_written(dest_dir, stamp):
            return 0
    # Said here rather than at the read, so a firing that decided there was
    # nothing to do stays silent while a backup actually written under the
    # built-in defaults says which settings it ran on.
    if fallback:
        print(fallback)

    source = _database_path()
    if not _check_backup_dir(dest_dir):
        return 1
    written = backup_now(source, dest_dir, keep, stamp)
    if written is None:
        return 1
    try:
        size_text = f"{os.path.getsize(written) / 1_048_576:.1f} MB"
    except OSError:
        size_text = "size unavailable"
    kept = len(glob.glob(os.path.join(dest_dir, "arraysense-*.db.gz")))
    print(f"backup: {written} ({size_text}), keeping {kept}")
    print(f"restore with: arraysense restore {written}")
    return 0


def cmd_restore(argv: list[str]) -> int:
    """Restore the database from a compressed archive, safely.

    The shell recipe this replaced could destroy a live database in five
    keystrokes: gunzip writes nothing on a corrupt archive, the shell redirect
    creates a zero-byte file, PRAGMA quick_check prints "ok" on zero bytes,
    and the mv overwrites the live database with an empty file. sqlite3 is
    not installed on the reference Pi, so the step that should have caught
    this silently did nothing.

    This command, in order:
      1. refuses if the archive does not exist or cannot be read
      2. unpacks to a temporary file beside the live database
      3. verifies the unpacked file has content — page count, expected
         tables present, inverter_raw has rows — naming which check failed
      4. only then stops the service, removes the -wal and -shm sidecars,
         renames the old database to .prev, and moves the new one in
      5. starts the service and waits for it to answer
      6. deletes the .prev only after the service has started

    The live database is never overwritten until every check above has
    passed, and a pre-restore copy is kept until the new file is proven
    to start. Use --yes for unattended restores.
    """
    yes = False
    archive: str | None = None
    for arg in argv:
        if arg == "--yes":
            yes = True
        elif not arg.startswith("-"):
            archive = arg
        else:
            print("usage: arraysense restore [--yes] <archive.db.gz>")
            return 1
    if archive is None:
        print("usage: arraysense restore [--yes] <archive.db.gz>")
        return 1

    # Step 1: refuse if the archive does not exist or cannot be read.
    try:
        st = os.stat(archive)
    except OSError as exc:
        print(f"cannot read the archive at {archive}: {exc}")
        return 1
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode):
        print(f"{archive} is not a regular file")
        return 1
    if st.st_size == 0:
        print(f"{archive} is empty; nothing to restore")
        return 1

    db_path = _database_path()
    db_dir = os.path.dirname(db_path) or "."
    db_name = os.path.basename(db_path)
    restore_path = os.path.join(db_dir, db_name + ".restore")

    # Step 2: unpack to a temporary file beside the live database.
    try:
        with gzip.open(archive, "rb") as src, open(restore_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
    except (gzip.BadGzipFile, OSError) as exc:
        print(f"could not unpack {archive}: {exc}")
        _remove_path(restore_path)
        return 1

    # Step 3: verify the unpacked file is a real database with content.
    try:
        if not _verify_working_copy(restore_path):
            print("the unpacked file is empty; the archive contained no data")
            _remove_path(restore_path)
            return 1
    except OSError as exc:
        print(f"could not check the unpacked file at {restore_path}: {exc}")
        _remove_path(restore_path)
        return 1

    try:
        check_conn = sqlite3.connect(f"file:{restore_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        print(f"the unpacked file at {restore_path} is not a valid database: {exc}")
        _remove_path(restore_path)
        return 1
    try:
        try:
            page_row = check_conn.execute("PRAGMA page_count").fetchone()
        except sqlite3.Error as exc:
            print(f"the unpacked file at {restore_path} is not a valid database: {exc}")
            _remove_path(restore_path)
            return 1
        if page_row is None or page_row[0] == 0:
            print("the unpacked database has no pages; the archive contained an empty file")
            _remove_path(restore_path)
            return 1
        integrity = check_conn.execute("PRAGMA quick_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            print(f"the unpacked database failed its integrity check: {integrity}")
            _remove_path(restore_path)
            return 1
        tables = check_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inverter_raw'"
        ).fetchone()
        if tables is None:
            print(
                "the unpacked database has no inverter_raw table; it is not an ArraySense database"
            )
            _remove_path(restore_path)
            return 1
        row_count = check_conn.execute("SELECT COUNT(*) FROM inverter_raw").fetchone()
        if row_count is None or row_count[0] == 0:
            print(
                "the unpacked database has an inverter_raw table with no rows; nothing to restore"
            )
            _remove_path(restore_path)
            return 1
    finally:
        check_conn.close()

    # The file passes every check. Show what will happen and confirm.
    print(f"archive:       {archive}")
    print(f"database:      {db_path}")
    print(f"rows to restore: {row_count[0]}")
    if not yes and not _confirm("Restore this archive over the live database?"):
        print("nothing done")
        _remove_path(restore_path)
        return 0

    # Step 4: stop the service, preserve the old database, remove sidecars,
    # and move the new file into place.
    if not service("stop"):
        print("could not stop the service; restore abandoned")
        _remove_path(restore_path)
        return 1

    prev_path = db_path + ".prev"
    _remove_path(prev_path)
    try:
        os.rename(db_path, prev_path)
    except OSError as exc:
        print(f"could not preserve the current database at {prev_path}: {exc}")
        print("restore abandoned; the live database is untouched")
        _remove_path(restore_path)
        service("start")
        return 1

    for suffix in ("-wal", "-shm"):
        _remove_path(db_path + suffix)

    try:
        st_prev = os.stat(prev_path)
        os.chown(restore_path, st_prev.st_uid, st_prev.st_gid)
    except OSError:
        # chown failed — the file will be owned by whoever is running this,
        # which is ordinarily root. The service runs as arraysense and needs
        # write access, so this matters.
        pass
    try:
        os.rename(restore_path, db_path)
    except OSError as exc:
        print(f"could not move the restored file into place: {exc}")
        print("the pre-restore database is at " + prev_path)
        service("start")
        return 1

    # Step 5: start the service and wait for it to answer.
    if not service("start"):
        print("the database was restored but the service did not start")
        print("  the pre-restore database is at " + prev_path)
        print("  try: arraysense logs")
        return 1

    port = configured_port()
    state, _body = wait_until_up(port)
    if state == "down":
        print(f"restore complete, but the service did not answer within {int(HEALTH_TIMEOUT)}s")
        print("  the pre-restore database is at " + prev_path)
        print("  try: arraysense logs")
        return 1
    if state == "setup":
        print("restore complete; the service is in setup mode")
        print("  the pre-restore database is at " + prev_path)
        _remove_path(prev_path)
        return 0
    if state == "answering":
        print("restore complete; the service is up but the collector is not running")
        print("  the pre-restore database is at " + prev_path)
        _remove_path(prev_path)
        return 1
    print("restore complete; the service is collecting")
    _remove_path(prev_path)
    return 0


COMMANDS = {
    "status": cmd_status,
    "logs": cmd_logs,
    "restart": cmd_restart,
    "version": cmd_version,
    "backup": cmd_backup,
    "restore": cmd_restore,
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
