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
    # settings overlay can now name a different driver than the file did, so
    # this first open lays the schema and reads the settings table — its metric
    # declaration is provisional and re-derived from the effective driver
    # below, and the store is reopened if the overlay changed the driver. The
    # settings table carries no driver-specific columns, so the provisional
    # declaration only affects which tier columns are writable — the reopen
    # sets the writable set for the effective driver and adds any missing
    # columns; a column the provisional driver had and the effective one does
    # not is left in place, unwritten, which is harmless.
    file_config = config
    opened_driver = config.driver
    declared = drivers.get(config.driver).capabilities.metrics
    store = SqliteStore(
        config.database_path,
        device=config.inverter_serial,
        metrics=declared,
        synchronous=config.synchronous,
    )

    # Anything set from the settings page wins over the file. The merge happens
    # here rather than inside load() because the settings live in the database,
    # and the file is what says where the database is.
    config = effective(config, SettingsStore(store))
    # The effective driver is what the collector will build, so the store's
    # schema must be declared for it — not for whatever the file named. A
    # first-run "fake" installation switched to eg4_luxpower through the
    # overlay would otherwise write EG4 samples into a fake-declared store and
    # KeyError on the first EG4-only metric, killing the poll loop.
    declared = drivers.get(config.driver).capabilities.metrics

    # Which leaves an ordering problem now that the store is opened for a
    # device: the serial the settings page may override is the identity the
    # store reads by, and it is only known after the store has been opened to
    # read it. Reopening is the cheap and visible answer. Without it the API
    # would read one serial's rows while the collector wrote another's, and
    # every page would go blank with the collector apparently healthy.
    if config.inverter_serial != store.device or config.driver != opened_driver:
        # Reopen whenever the identity or the driver the store was first built
        # with no longer matches the effective configuration. The serial is the
        # row identity; the driver decides the column set. Either one wrong
        # means the collector and the store disagree about what they are
        # writing, and the reopen is cheap and visible.
        logger.info(
            "settings override the connection; reopening the store as %s on the %s driver",
            config.inverter_serial,
            config.driver,
        )
        store.close()
        store = SqliteStore(
            config.database_path,
            device=config.inverter_serial,
            metrics=declared,
            synchronous=config.synchronous,
        )

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
    # The file config, not the effective one, is what a write path needs to
    # predict the next boot: clearing an overlay field reverts to the file
    # value, and only this base can see it.
    app = create_app(store=store, service=service, config=config, file_config=file_config)

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


def _schedule_setup_restart() -> None:
    """Exit cleanly in a moment, after the apply response has gone out.

    Extracted so a test can stand in for it: the real thing SIGTERMs this
    process, which under TestClient is the test runner itself — a test that
    schedules it un-stubbed kills the whole suite half a second later.
    """
    loop = asyncio.get_running_loop()
    loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)


def build_setup_app(config_path: Path | str) -> FastAPI:
    """The app a brand-new installation serves: pages, setup, nothing else.

    No store and no collector, because there is no inverter serial to open a
    store under — a placeholder identity would attribute rows to a machine
    that does not exist, which is the one sin the store's docstring forbids.
    The wizard's apply writes the config file this path points at, then
    restarts into the ordinary application. A file that exists but refuses to
    parse never reaches here: that is a broken installation, and entering
    setup mode over it would hide the breakage behind a welcome screen.
    """
    from fastapi import HTTPException

    from arraysense.api.app import install_text_guard, mount_pages
    from arraysense.api.routes import DetectRequest, run_detect
    from arraysense.config import DEFAULT_DATABASE_PATH
    from arraysense.setup import describe_setup, render_config

    app = FastAPI(title="Solar ArraySense setup", version=__version__)
    mount_pages(app)
    install_text_guard(app)

    @app.get("/api/setup")
    async def setup_describe() -> dict[str, object]:
        # The placeholder exists only to satisfy describe_setup's signature;
        # its values are TEST-NET noise, and the shell knows from first_run
        # that "current" describes nothing real yet.
        placeholder = Config(
            dongle_host="192.0.2.1",
            dongle_serial="BA00000000",
            inverter_serial="unconfigured",
            database_path=str(DEFAULT_DATABASE_PATH),
            transport="dongle",
        )
        payload = describe_setup(placeholder)
        payload["first_run"] = True
        return payload

    @app.post("/api/setup/detect")
    async def setup_detect(body: dict[str, object]) -> dict[str, str]:
        # A plain dict rather than DetectRequest: a nested endpoint annotated
        # with the imported class is an unresolved forward reference under this
        # module's postponed annotations, which FastAPI turns into a 422 for a
        # missing "body" parameter. Build the model here and hand it to the
        # same run_detect the running-service route uses, so setup mode gets
        # the unknown-transport and missing-serial checks rather than skipping
        # them. No collector exists here, so nothing is borrowed.
        from pydantic import ValidationError

        try:
            parsed = DetectRequest.model_validate(body)
        except ValidationError as exc:
            # A running-service route gets 422 from FastAPI for a malformed
            # body; setup mode parses by hand, so it maps the same failure to
            # the same status. Only the location and message are reported, never
            # the raw input or the exception object — a lone surrogate input is
            # not JSON-serializable and would turn the 422 into a 500.
            detail = [
                {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", ""))}
                for err in exc.errors()
            ]
            raise HTTPException(status_code=422, detail=detail) from exc
        return await run_detect(parsed, service=None)

    @app.post("/api/setup/apply")
    async def setup_apply_first_run(body: dict[str, object]) -> dict[str, object]:
        target = Path(config_path)
        probe = target.with_suffix(".candidate")
        try:
            # Everything that can fault on a hostile body runs inside the guard:
            # render_config and the write itself raise UnicodeEncodeError on a
            # lone surrogate, so writing before the try would 500 and strand a
            # candidate. Nothing outside the try touches the request.
            body.setdefault("database_path", str(DEFAULT_DATABASE_PATH))
            text = render_config(body)
            target.parent.mkdir(parents=True, exist_ok=True)
            probe.write_text(text)
            candidate = load(probe)
            drivers.validate(candidate)
            # database_path must not name the configuration file. The store the
            # next line opens to prove the path is usable would then be the file
            # replace() overwrites with TOML, and the boot after would read TOML
            # as sqlite and crash — a persisted config that cannot start.
            db = Path(candidate.database_path).resolve()
            if db == target.resolve() or db == probe.resolve():
                raise ValueError("database_path must not be the configuration file")
            # Prove the candidate boots, not merely that it parses: open the
            # store at the configured path, the one thing load() and the
            # registry do not check. A path load() accepts but sqlite cannot
            # open would otherwise become the real config and crash-loop every
            # restart with no setup mode left to offer.
            declared = drivers.get(candidate.driver).capabilities.metrics
            SqliteStore(
                candidate.database_path,
                device=candidate.inverter_serial,
                metrics=declared,
                synchronous=candidate.synchronous,
            ).close()
        except (
            ValueError,
            FileNotFoundError,
            OverflowError,
            TypeError,
            OSError,
            UnicodeError,
            sqlite3.Error,
        ) as exc:
            probe.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        probe.replace(target)
        _schedule_setup_restart()
        return {"written": str(target), "restarting": True}

    return app


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
    except FileNotFoundError:
        # A brand-new installation, not a broken one: serve setup mode, whose
        # wizard writes the first config and restarts into normal life.
        logger.info("no configuration at %s — serving first-run setup", args.config)
        uvicorn.run(
            build_setup_app(args.config),
            host=args.host,
            port=args.port,
            log_level=args.log_level,
        )
        return 0
    except ValueError as exc:
        # A file that exists but refuses to parse is a broken installation.
        # Say what is wrong and stop; entering setup mode over it would hide
        # the breakage behind a welcome screen.
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
