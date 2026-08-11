"""app.py — build the FastAPI application around its dependencies.

The store, the collector and the configuration are attached to app.state rather
than reached for through module globals, so a test can stand the whole API up
around a temporary database and a stub collector without touching real hardware
or a real config file.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from arraysense import __version__
from arraysense.api.routes import router
from arraysense.collector.service import CollectorService
from arraysense.config import Config
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# The front end, named a file at a time rather than mounted as a directory. An
# allow-list is what keeps a file that lands in web/ later — an editor backup, a
# note, a half-written page — from becoming a URL because it happens to be
# sitting there. This repository is public and the machine it runs on is not, so
# the difference matters.
#
# Now and Energy flow are two views of index.html reached by hash, not two
# paths: they poll the same endpoint and switching between them must not throw
# away the live data or the zoom on three charts.
PAGES = {
    "/": "index.html",
    "/graphs": "graphs.html",
    "/history": "history.html",
    "/costs": "costs.html",
    "/efficiency": "efficiency.html",
    "/settings": "settings.html",
}

# The code every page is built from — the palette, the formatters, the nav and
# the chart factory. Served once so the pages cannot drift apart.
SHARED_SCRIPT = "common.js"

# Revalidate rather than re-download. The pages and the shared script change
# together and are cached separately, so a browser is free to pair a fresh page
# with a stale script unless told to check. The vendored chart library is
# deliberately not in this set: it is versioned by its filename and never
# changes under the same name.
NO_CACHE = {"Cache-Control": "no-cache"}

# uPlot is vendored rather than fetched from a CDN. The service runs on a home
# network that may have no route to the internet at all, and a chart library
# that silently fails to load leaves a blank panel with no clue why. Named
# explicitly for the same reason the pages are.
VENDORED = {
    "uPlot.iife.min.js": "text/javascript",
    "uPlot.min.css": "text/css",
    "uPlot.LICENSE": "text/plain",
}


def _file_route(path: Path, media_type: str) -> Callable[[], Awaitable[FileResponse]]:
    """Build a handler that serves one fixed file, or 404s when it is not there.

    Read from disk on each request rather than cached at import: editing a page
    during development should not need a restart, and these are a few kilobytes.

    The existence check is why this is a helper rather than four FileResponses.
    Starlette raises from inside the response when the file has gone, which
    reaches the browser as a 500 and the log as a traceback — but a page nobody
    has written yet, or one left out of a deployment, is a missing page and not
    a broken server.

    Every page and the shared script are sent ``no-cache``, which asks the
    browser to revalidate rather than forbidding it to store anything: the file
    still comes back 304 and unchanged most of the time, so the cost is one
    conditional request. Without it the pages and common.js are cached
    independently, and a browser holding yesterday's common.js against today's
    page calls a helper that does not exist yet. That happened: the chart threw,
    and the page reported it as the history being unavailable.
    """

    async def serve() -> FileResponse:
        if not path.is_file():
            logger.debug("no file at %s", path)
            raise HTTPException(status_code=404, detail=f"no file {path.name!r}")
        return FileResponse(path, media_type=media_type, headers=NO_CACHE)

    return serve


def create_app(
    store: SqliteStore,
    service: CollectorService,
    config: Config,
    file_config: Config | None = None,
) -> FastAPI:
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
    # The file config, before any settings overlay, is what a write path needs
    # to predict the next boot: clearing an overlay field reverts to the file
    # value. A direct caller that passes only the effective config would make
    # the validation model the wrong base, so this is set here, always, and
    # build_app passes the real file config through it.
    app.state.file_config = file_config if file_config is not None else config
    app.include_router(router)
    install_text_guard(app)

    mount_pages(app)

    logger.debug("application assembled")
    return app


def install_text_guard(app: FastAPI) -> None:
    """Answer a malformed body as 422 without echoing the un-encodable input.

    FastAPI's default validation-error response includes the offending input,
    and a lone surrogate — valid JSON syntax through a uXXXX escape, but not
    encodable text — makes rendering that response raise UnicodeError deep in
    the framework, escaping as a 500. Field validators cannot help: the failure
    is in serializing the error, after they have run. This handler reports only
    each error's location and message, never the raw input, so the 422 renders
    and the value that could not be encoded is dropped. Nothing is persisted on
    this path, so the status is the whole of it.
    """
    from fastapi.exceptions import RequestValidationError
    from starlette.requests import Request as StarletteRequest
    from starlette.responses import JSONResponse

    async def on_validation_error(
        request: StarletteRequest, exc: RequestValidationError
    ) -> JSONResponse:
        detail = [
            {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", ""))}
            for err in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": detail})

    app.add_exception_handler(RequestValidationError, on_validation_error)  # type: ignore[arg-type]


def mount_pages(app: FastAPI) -> None:
    """Attach the pages, shared script and vendored files to an app.

    Split from create_app so first-run setup mode serves the same pages
    byte-identically: a second page-mounting loop would drift from this one
    the first time a page was added, and the wizard would 404 on exactly the
    installation that needs it most.
    """
    web = Path(__file__).parent.parent / "web"

    for route, filename in PAGES.items():
        app.get(route, include_in_schema=False, name=filename)(
            _file_route(web / filename, "text/html")
        )

    app.get(f"/{SHARED_SCRIPT}", include_in_schema=False, name=SHARED_SCRIPT)(
        _file_route(web / SHARED_SCRIPT, "text/javascript")
    )

    @app.get("/vendor/{name}", include_in_schema=False)
    async def vendored(name: str) -> FileResponse:
        """Serve one of the vendored front-end files."""
        media = VENDORED.get(name)
        if media is None:
            raise HTTPException(status_code=404, detail=f"no vendored file {name!r}")
        return FileResponse(web / name, media_type=media)
