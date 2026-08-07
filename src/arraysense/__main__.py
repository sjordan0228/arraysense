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
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from arraysense import __version__
from arraysense.api.app import create_app
from arraysense.collector.pylxp_source import PylxpSource
from arraysense.collector.service import CollectorService
from arraysense.config import DEFAULT_PATH, Config, effective, load
from arraysense.settings import SettingsStore
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
    parser.add_argument("--version", action="version", version=f"arraysense {__version__}")
    return parser


def build_app(config: Config) -> tuple[FastAPI, SqliteStore, CollectorService]:
    """Open the store, build the collector, and assemble the application.

    Returns all three because the caller has to close the store and stop the
    collector on the way out, and the app alone does not expose them.
    """
    Path(config.database_path).parent.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(config.database_path)

    # Anything set from the settings page wins over the file. The merge happens
    # here rather than inside load() because the settings live in the database,
    # and the file is what says where the database is.
    config = effective(config, SettingsStore(store))

    # Logged here rather than in main() because this is the first point at
    # which the values are the ones the collector will actually use. Logging
    # the file's values before the merge would print a startup line that
    # disagrees with what the service then does.
    logger.info(
        "arraysense %s — inverter %s via %s:%d, every %.0fs, database %s",
        __version__,
        config.inverter_serial,
        config.dongle_host,
        config.dongle_port,
        config.poll_interval,
        config.database_path,
    )

    service = CollectorService(
        source=PylxpSource(config),
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
            # Release the dongle's single slot before the process goes away,
            # or the next start — and the vendor's app — find it occupied.
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


def main(argv: list[str] | None = None) -> int:
    """Load the configuration and serve until interrupted."""
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format=LOG_FORMAT)

    try:
        config = load(args.config)
    except (FileNotFoundError, ValueError) as exc:
        # A misconfigured service should say what is wrong and stop, not crash
        # with a traceback that buries the one useful line.
        logger.error("%s", exc)
        return 1

    app, _store, _service = build_app(config)
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    sys.exit(main())
