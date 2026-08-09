"""__main__.py — wire the pieces together and run the service.

Reads the configuration, opens the store, points a collector at the inverter
and serves the API over the same process. The collector owns the inverter
connection because the dongle allows exactly one client; the API only ever
reads from the store, so the two never contend for it.

Shutdown matters more than it looks. The collector holds that single TCP slot,
and a process that exits without letting go leaves the dongle unavailable to
the vendor's app and to the next start of this service, so the lifespan hook
stops the collector before the socket closes.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sqlite3
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from arraysense import __version__, drivers
from arraysense.api.app import create_app
from arraysense.collector.service import CollectorService
from arraysense.config import DEFAULT_PATH, Config, effective, load
from arraysense.settings import SettingsStore
from arraysense.store.migrate import migrate_devices, needs_device_migration
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def build_parser() -> argparse.ArgumentParser:
    """Command-line arguments: where the config lives and where to listen."""
    parser = argparse.ArgumentParser(
        prog="arraysense",
        description="Solar and battery monitoring for EG4 and LuxPower inverters.",
    )
    parser.add_argument("--config", default=str(DEFAULT_PATH), help="path to config.toml")
    # Binding all interfaces is the point: the service is reached from other
    # machines on the LAN, not from localhost.
    parser.add_argument("--host", default="0.0.0.0", help="bind address")
    parser.add_argument("--port", type=int, default=8080, help="bind port")
    parser.add_argument("--log-level", default="info", help="debug, info, warning or error")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="stamp a database written before device identity with the configured "
        "inverter serial, then exit",
    )
    parser.add_argument("--version", action="version", version=f"arraysense {__version__}")
    return parser


def build_app(config: Config) -> tuple[FastAPI, SqliteStore, CollectorService]:
    """Open the store, build the collector, and assemble the application.

    Returns all three because the caller has to close the store and stop the
    collector on the way out, and the app alone does not expose them.
    """
    Path(config.database_path).parent.mkdir(parents=True, exist_ok=True)
    # The store lays its schema for what the configured driver declares it can
    # produce, so a fresh database has no column that can never be filled. The
    # settings overlay below cannot change the driver — only the file names it
    # — so the declaration is safe to resolve before the store exists to read
    # settings from.
    declared = drivers.get(config.driver).capabilities.metrics
    store = SqliteStore(config.database_path, device=config.inverter_serial, metrics=declared)

    # Anything set from the settings page wins over the file. The merge happens
    # here rather than inside load() because the settings live in the database,
    # and the file is what says where the database is.
    config = effective(config, SettingsStore(store))

    # Which leaves an ordering problem now that the store is opened for a
    # device: the serial the settings page may override is the identity the
    # store reads by, and it is only known after the store has been opened to
    # read it. Reopening is the cheap and visible answer. Without it the API
    # would read one serial's rows while the collector wrote another's, and
    # every page would go blank with the collector apparently healthy.
    if config.inverter_serial != store.device:
        logger.info(
            "settings override the inverter serial; reopening the store as %s",
            config.inverter_serial,
        )
        store.close()
        store = SqliteStore(config.database_path, device=config.inverter_serial, metrics=declared)

    # Logged here rather than in main() because this is the first point at
    # which the values are the ones the collector will actually use. Logging
    # the file's values before the merge would print a startup line that
    # disagrees with what the service then does.
    # Name the endpoint that will actually be used. Printing a dongle host and
    # port on a serial installation would be a startup line that disagrees with
    # what the service then does, which is the thing this log exists to avoid.
    if config.transport == "modbus_serial":
        endpoint = f"{config.serial_device} unit {config.serial_unit_id}"
    else:
        endpoint = f"{config.dongle_host}:{config.dongle_port}"
    logger.info(
        "arraysense %s — inverter %s via %s using the %s driver, every %.0fs, database %s",
        __version__,
        config.inverter_serial,
        endpoint,
        config.driver,
        config.poll_interval,
        config.database_path,
    )

    service = CollectorService(
        source=drivers.create(config),
        store=store,
        interval=config.poll_interval,
    )
    app = create_app(store=store, service=service, config=config)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await service.start()
        watchdog = asyncio.create_task(_watch(service))
        try:
            yield
        finally:
            watchdog.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watchdog
            # Release the inverter's single client slot before the process
            # goes away, or the next start finds it occupied — the dongle's one
            # TCP slot, which the vendor's app also wants, or the serial port,
            # which is opened exclusively.
            await service.stop()
            store.close()

    app.router.lifespan_context = lifespan
    return app, store, service


# How often the watchdog looks. Frequent enough that a stall is caught within a
# minute of the threshold, cheap enough to be invisible: it reads two timestamps.
WATCH_INTERVAL = 30.0


async def _watch(service: CollectorService) -> None:
    """Kill the process if the poll loop stops running, so systemd restarts it.

    Restart=always covers a process that exits. What it cannot see is this
    process still serving pages perfectly while collecting nothing — which is
    what happens when the poll task dies, because it is created with
    ``create_task`` and nobody awaits it, so its exception is logged to asyncio
    and the web server carries on. Every chart would keep drawing, growing
    quietly staler, and the service would look healthy throughout.

    A read that never returns produces the same silence without any exception at
    all, and neither is distinguishable from outside.

    Exiting is the blunt answer and the right one here: this process holds the
    dongle's single TCP slot, so there is no restarting the loop in place
    without first letting go of the socket, and systemd already knows how to
    bring the whole thing back in ten seconds. Twenty minutes of data is the
    most this can cost, against an unbounded outage nobody notices.

    Note what this deliberately does *not* trigger on: an inverter that is
    simply not answering. Those polls fail, and a failure is the loop working —
    it records the gap and backs off. Restarting over that would lose the
    backoff and thrash for as long as the inverter was away.
    """
    while True:
        await asyncio.sleep(WATCH_INTERVAL)
        stalled = service.stalled_for()
        if stalled is None:
            continue
        logger.error(
            "collector has produced neither a reading nor an error for %.0f minutes; "
            "exiting so the supervisor restarts it",
            stalled.total_seconds() / 60,
        )
        # SIGTERM rather than sys.exit: this is a background task, and raising
        # here would be swallowed exactly the way the dead poll loop was. The
        # signal runs uvicorn's own shutdown, so the lifespan hook still gets to
        # release the dongle before the process goes.
        os.kill(os.getpid(), signal.SIGTERM)
        return


SETTING_INVERTER_SERIAL = "connection.inverter_serial"


def _configured_serial(config: Config) -> str:
    """The serial the running service will read by, settings first.

    ``effective()`` lets the settings page override the file, and the store is
    opened for whatever that resolves to. The migration has to agree with it or
    it stamps rows nothing will ever query.

    Read with a bare connection because the store will not open a database that
    still needs migrating. A database with no settings table yet — a fresh
    install, or one written before settings existed — falls back to the file,
    which is also what ``effective()`` does with nothing to overlay.
    """
    path = Path(config.database_path)
    if not path.exists():
        return config.inverter_serial
    try:
        with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key = ?", (SETTING_INVERTER_SERIAL,)
            ).fetchone()
    except sqlite3.DatabaseError as exc:
        # No settings table, or nothing readable. The file's value is then the
        # only answer there is, and it is the right one.
        logger.debug("no stored serial to read (%s); using the configured one", exc)
        return config.inverter_serial
    if row is None or not str(row[0]).strip():
        return config.inverter_serial
    stored = str(row[0]).strip()
    if stored != config.inverter_serial:
        logger.info(
            "the settings page overrides the inverter serial; migrating as %s, not %s",
            stored,
            config.inverter_serial,
        )
    return stored


def run_migration(config: Config) -> int:
    """Give every stored reading the configured inverter's serial, and report.

    A separate command rather than something startup does by itself. It
    rewrites every table in a database that may hold years of history, and the
    person running it should be the one who decided to, with a backup taken and
    the numbers in front of them afterwards. It is safe to run on a database
    that has already been migrated, which is what makes it safe to put in a
    deployment script.

    The serial is read from the settings table first and only then from the
    file, because that is the order the running service resolves it in. Taking
    the file's value here while the service reads the overridden one would
    stamp every row with an identity nothing ever queries — the whole history
    orphaned in place, by a command whose entire purpose is not to lose it, and
    reported as a success. The settings are read with a plain connection rather
    than through the store, because the store refuses to open a database that
    has not been migrated yet and this is the command that migrates it.
    """
    serial = _configured_serial(config)
    report = migrate_devices(config.database_path, serial)
    if report.already_migrated:
        logger.info("%s already carries a device on every reading", config.database_path)
        return 0
    # The per-table counts are logged by the migration itself; this is the one
    # line somebody scrolling a deployment log needs to see.
    logger.info("stamped %s row(s) as %s", f"{report.total:,}", report.device)
    return 0


def main(argv: list[str] | None = None) -> int:
    """Load the configuration and serve until interrupted."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format=LOG_FORMAT)

    try:
        config = load(args.config)
        # Resolved here rather than where the source is built, so a mistyped
        # driver name is reported alongside every other thing wrong with the
        # file — one line naming the drivers that exist, not a traceback out of
        # the middle of application assembly.
        drivers.get(config.driver)
    except (FileNotFoundError, ValueError) as exc:
        # A misconfigured service should say what is wrong and stop, not crash
        # with a traceback that buries the one useful line.
        logger.error("%s", exc)
        return 1

    if args.migrate:
        try:
            return run_migration(config)
        except (OSError, sqlite3.Error, RuntimeError) as exc:
            logger.error("migration failed, database unchanged: %s", exc)
            return 1

    if needs_device_migration(config.database_path):
        # Refused rather than done silently. The alternative is a service that
        # rewrites a year of history on a restart nobody was watching, which is
        # the wrong thing to discover from a log afterwards.
        logger.error(
            "%s was written before readings carried a device. Back it up, then run "
            "`arraysense --config %s --migrate`.",
            config.database_path,
            args.config,
        )
        return 1

    app, _store, _service = build_app(config)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
