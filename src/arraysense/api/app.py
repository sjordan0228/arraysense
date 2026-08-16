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
from arraysense.auth import LoginThrottle, Sessions
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
    # An optional module's page. Served whether or not the module is enabled:
    # the page says "off" for itself, and a 404 that depends on a setting is a
    # link that rots in somebody's bookmarks the day they switch it off.
    "/emporia": "emporia.html",
    # The charger gets a page rather than a card, because it is a thing an
    # owner acts on. Its nav entry appears only on an account that has one.
    "/charger": "charger.html",
}

# The code every page is built from — the palette, the formatters, the nav and
# the chart factory. Served once so the pages cannot drift apart.
SHARED_SCRIPT = "common.js"

# The appearance sheets, one file per look, layered over the base styling common.js
# injects. Classic is the absence of any of them, so this set holds only the
# alternatives. Adding one needs no new routing code — a filename here is enough
# for it to be served — but the look itself is not complete until common.js names
# it too, in APPEARANCE_SHEET and APPEARANCE_NAMES, which is what puts it on the
# Settings page with a word for it. The tests hold all three to the same set.
#
# These are pages, not vendored files, and the difference is the caching rule
# below. A theme sheet is edited under its own name for as long as the look is
# maintained, so a browser holding yesterday's copy shows yesterday's design
# against today's markup — the same class of failure as a stale common.js, and
# harder to read, because a design that is merely wrong looks like a design.
# Serving it from /vendor/ would invite exactly that: that route sets no
# cache-control at all, which leaves the browser to invent a freshness lifetime
# of its own from how long ago the file was last modified, and so to go on
# using its copy without asking.
#
# A browser on Classic pays for this file and gets nothing for it. The <link>
# is in the markup — it has to be, or the first paint is in the wrong look —
# and the inline script that removes it comes after, where a synchronous script
# cannot run until the stylesheet ahead of it has loaded. So Classic fetches
# the whole sheet on every page load, waits for it, and throws it away, with no
# 304 to soften it (see _file_route). That is accepted rather than overlooked:
# it is a small file on a home network, and the complete fix is to keep the
# choice in a cookie so the server can render the right link, which is a larger
# change than the cost justifies. A separate issue carries it.
THEME_SHEETS = ("theme-glass.css",)

# Ask again rather than assume. The pages, the shared script and the theme
# sheets change together and are cached separately, so a browser is free to pair
# a fresh page with a stale script unless told to check. The vendored chart
# library and the fonts are deliberately not in this set: this project replaces
# those files rather than editing them, so a browser may keep its copy for as
# long as it likes.
NO_CACHE = {"Cache-Control": "no-cache"}

# uPlot is vendored rather than fetched from a CDN. The service runs on a home
# network that may have no route to the internet at all, and a chart library
# that silently fails to load leaves a blank panel with no clue why. Named
# explicitly for the same reason the pages are.
#
# The fonts are here for the same network reason and belong under the same
# caching rule: nothing in this project edits a released binary, so what changes
# is which release is vendored, and that is a job for a new filename.
#
# None of these names carry a version, uPlot's included, so nothing mechanical
# enforces that — drop a newer release in under an old name and a browser
# already holding the old bytes goes on drawing them, for as long as the
# freshness it invented lasts, which grows with the age of the file it is
# holding. Replacing one of these means giving the new file a name of its own.
# theme-glass.css records which upstream release each font came from and the
# hash of the bytes, which is how a maintainer can tell what is on disk.
#
# Each ships beside its licence, as uPlot does — the OFL requires the notice to
# travel with the font, and a vendored file whose licence stayed behind is the
# one way vendoring becomes a problem.
#
# Space Grotesk is one variable file because upstream releases one; JetBrains
# Mono is four static files because upstream releases no variable webfont at
# all. The stylesheet's @font-face blocks say the same thing and have to keep
# saying it: a range declared over a static file is a weight the browser fakes.
#
# The Phosphor icon sprite is here for the same network reason, and it is
# replaced the same way uPlot is — a newer upstream drop arrives under a name
# of its own, never overwriting this one, because a browser already holding the
# old bytes goes on drawing them. Its MIT licence rides beside it like the
# others, so the notice travels with the file it licenses.
#
# The name carries a 2 because this is the second drop, not a second package:
# the first sprite held 24 symbols and the circuits page needed eleven more.
# Adding them under the old name would have left every browser holding the
# old bytes drawing blank squares where the new icons are — which is what
# happened on the bench before this rename, and is the whole reason the rule
# above exists.
#
# Provenance, written down for the same reason the fonts carry it:
# phosphor-2.svg
# is drawn from @phosphor-icons/core@2.1.1 (github.com/phosphor-icons/core),
# its <symbol> paths taken from that package's regular/ SVGs — ph-gear's path
# data matches regular/gear.svg byte for byte. phosphor.LICENSE is that
# package's MIT notice unaltered, "Copyright (c) 2023 Phosphor Icons". A
# maintainer comparing against the phosphor-icons/web repository instead will
# find a differently-dated notice and may mistake it for an alteration; the
# core package is the source this file came from.
VENDORED = {
    "uPlot.iife.min.js": "text/javascript",
    "uPlot.min.css": "text/css",
    "uPlot.LICENSE": "text/plain",
    "SpaceGrotesk-wght.woff2": "font/woff2",
    "SpaceGrotesk.LICENSE": "text/plain",
    "JetBrainsMono-Light.woff2": "font/woff2",
    "JetBrainsMono-Regular.woff2": "font/woff2",
    "JetBrainsMono-Medium.woff2": "font/woff2",
    "JetBrainsMono-Bold.woff2": "font/woff2",
    "JetBrainsMono.LICENSE": "text/plain",
    "phosphor-2.svg": "image/svg+xml",
    "phosphor.LICENSE": "text/plain",
}


def _file_route(path: Path, media_type: str) -> Callable[[], Awaitable[FileResponse]]:
    """Build a handler that serves one fixed file, or 404s when it is not there.

    Read from disk on each request rather than cached at import: editing a page
    during development should not need a restart, and the read is a local one.

    The existence check is why this is a helper rather than four FileResponses.
    Starlette raises from inside the response when the file has gone, which
    reaches the browser as a 500 and the log as a traceback — but a page nobody
    has written yet, or one left out of a deployment, is a missing page and not
    a broken server.

    Every page, the shared script and each theme sheet are sent ``no-cache``,
    which asks the browser to check with the service on every load rather than
    forbidding it to store anything. Nothing here answers that check cheaply:
    Starlette's ``FileResponse`` sends an etag and a last-modified but reads
    neither ``If-None-Match`` nor ``If-Modified-Since``, so the reply is always
    the whole file and never a 304. That is still the right trade here, but it
    is not free and the figure is not small: common.js alone is over 130 kB and
    the larger pages around 110 kB, sent in full on every load. What it prevents
    is worse over a home network than the bytes are.
    Without it the pages and common.js are cached independently, and a browser
    holding yesterday's common.js against today's page calls a helper that does
    not exist yet. That happened: the chart threw, and the page reported it as
    the history being unavailable.
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
    # Per-process authentication state. Sessions and the login throttle must not be
    # module globals: the test suite stands up more than one app in a process and
    # they would share sessions, so a login to one would unlock the others.
    app.state.sessions = Sessions()
    app.state.throttle = LoginThrottle()
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
    """Attach the pages, shared script, theme sheets and vendored files to an app.

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

    for sheet in THEME_SHEETS:
        app.get(f"/{sheet}", include_in_schema=False, name=sheet)(
            _file_route(web / sheet, "text/css")
        )

    @app.get("/vendor/{name}", include_in_schema=False)
    async def vendored(name: str) -> FileResponse:
        """Serve one of the vendored front-end files."""
        media = VENDORED.get(name)
        if media is None:
            raise HTTPException(status_code=404, detail=f"no vendored file {name!r}")
        return FileResponse(web / name, media_type=media)
