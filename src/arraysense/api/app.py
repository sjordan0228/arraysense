"""app.py — build the FastAPI application around its dependencies.

The store, the collector and the configuration are attached to app.state rather
than reached for through module globals, so a test can stand the whole API up
around a temporary database and a stub collector without touching real hardware
or a real config file.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from arraysense import __version__
from arraysense.api.routes import router
from arraysense.collector.service import CollectorService
from arraysense.config import Config
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


def create_app(store: SqliteStore, service: CollectorService, config: Config) -> FastAPI:
    """Assemble the application from an open store, a collector and a config.

    Nothing is constructed here — the caller owns the lifecycle of all three,
    because the entry point needs to start the collector before serving and
    shut it down afterwards.
    """
    app = FastAPI(
        title="Solar ArraySense",
        version=__version__,
        description="Local solar and battery monitoring for EG4 and LuxPower inverters.",
    )
    app.state.store = store
    app.state.service = service
    app.state.config = config
    app.include_router(router)

    index = Path(__file__).parent.parent / "web" / "index.html"

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        """Serve the single-page dashboard.

        Read from disk on each request rather than cached at import: editing the
        page during development should not need a restart, and the file is a few
        kilobytes.
        """
        return FileResponse(index)

    logger.debug("application assembled")
    return app
