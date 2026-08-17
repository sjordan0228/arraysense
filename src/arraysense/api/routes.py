"""routes.py — the HTTP surface: live values, history, status, and yielding.

Everything here reads from the store and stays off the inverter's wire, with
one deliberate exception: setup detection, which stops the collector, probes,
and hands the wire straight back. The collector owns the one connection the
hardware allows, so any other route that reached for it would fight the poll
loop for the single slot.

History reads pick their resolution from the requested span and the caller's
pixel width rather than always serving the finest tier. A month at one-minute
resolution is 43,200 points for a chart perhaps a thousand pixels wide, which
measured at 107 ms against roughly 2 ms for the hourly tier and looked
identical. The chosen tier comes back in the response so a caller can tell what
it actually got.

Every reading in the store belongs to one inverter, and every endpoint here
answers about the one the store was opened for unless told otherwise. The
reads that map straight onto a store query take an optional ``device``, so a
second inverter's rows are reachable the day it starts recording; the derived
endpoints — cost, energy, calibration — do not, because each of them is an
interpretation of one system and would need more thought than a query
parameter to mean anything across two. No page sends the parameter.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Any, cast
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from arraysense import __version__, drivers
from arraysense import mode as operating_mode
from arraysense.alerts import NOT_A_CULPRIT, Contributor, high_usage
from arraysense.auth import (
    MIN_PASSWORD_LENGTH,
    Sessions,
    clear_password,
    password_hash,
    password_is_set,
    set_password,
    verify_password,
)
from arraysense.calibration import (
    CORROBORATING_ABSORB,
    PACK_RESET_LAG,
    assess,
    charge_completed_at,
    full_charge_windows,
)
from arraysense.config import Config, effective
from arraysense.costs import (
    band_intervals,
    bucket_energy,
    period_energy,
    price_period,
    unpriced_minutes,
)
from arraysense.curtailment import StringBaseline
from arraysense.efficiency import (
    CONFIG_VALID_FROM_KEY,
    CONFIG_VERSION_KEY,
    EfficiencyRow,
    TiltBenefit,
    compute_day,
    compute_hours,
    fitted_baselines,
    mppt_groups,
    rows_are_current,
    tilt_benefit,
)
from arraysense.energy import (
    ENERGY_FIELDS,
    MAX_EDGE_GAP,
    Period,
    # Private to energy.py, and imported rather than copied for the reason
    # ``tariff._elapsed`` is below: it decides which tier a counter read over a
    # window is answered from, and the coverage clamp has to look for the last
    # reading in that same tier. A second copy of the rule would drift, and the
    # symptom would be a coverage line that is silently blank.
    _window_tier,
    counter_kwh,
    read_energy,
    resolve_zone,
    with_zone,
)
from arraysense.metrics import INVERTER_METRICS, SITE_METRICS
from arraysense.modules.emporia import tokens as emporia_tokens
from arraysense.modules.emporia.client import (
    EmporiaAuthExpiredError,
    EmporiaChallengeError,
    EmporiaUnreachableError,
)
from arraysense.modules.emporia.control import clamp_rate
from arraysense.modules.emporia.poller import EmporiaPoller
from arraysense.modules.emporia.repository import OWNER, CircuitRepository
from arraysense.panels import StringSpec, parse_strings
from arraysense.settings import (
    BACKUP_DIRECTORY_KEY,
    CHARGE_CEILING_KEY,
    CHARGE_FLOOR_KEY,
    CHARGE_OVERRIDE_MINUTES_KEY,
    CHARGE_OVERRIDE_UNTIL_KEY,
    CHARGER_AUTHORITY_KEY,
    EMPORIA_ENABLED_KEY,
    HIGH_USAGE_WATTS_KEY,
    PANELS_STRINGS_KEY,
    SETTING_LATITUDE,
    SETTING_LONGITUDE,
    SETTING_TIMEZONE,
    WEATHER_INTERVAL_KEY,
    SettingsStore,
    _mask,
    check_backup_directory,
    check_serial_device,
    describe,
    emporia_interval_seconds,
    lookup_setting,
)
from arraysense.setup import describe_setup
from arraysense.store.schema import inverter_metric_columns, module_metric_columns
from arraysense.store.sqlite_store import SqliteStore
from arraysense.store.tiers import select_tier
from arraysense.tariff import (
    SETTING_BANDS,
    CostResult,
    EnergyShortfall,
    PeriodEnergy,
    Tariff,
    # Private to tariff.py, and imported here rather than rewritten: CLAUDE.md
    # names this and costs._real_minutes as the only two places a duration is
    # allowed to be measured, because the one that does not go through them is
    # the one that reads a 23-hour day as 24.
    _elapsed,
    apportion_fixed,
    estimate_bill,
    load_tariff,
    merge_shortfalls,
)
from arraysense.weather import fetch_archive_hours
from arraysense.weather.open_meteo import geocode

if TYPE_CHECKING:
    # For the annotation only. Nothing here calls into the collector: the
    # service arrives on the app's state, already built and already polling,
    # and the one thing asked of it is the verdict it has already reached.
    from arraysense.collector.service import CollectorService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


def _read_store(request: Request) -> Iterator[SqliteStore]:
    """Yield a per-request read view of the store, for handlers that scan tiers.

    The handlers this feeds are deliberately plain ``def``: FastAPI runs them in
    its threadpool, and the view gives each one its own connection — except a
    memory-backed store, which cannot be reopened and serves reads from its one
    connection; that is the test configuration, never a deployment. Both halves
    are load-bearing and both are measured. As ``async def`` running synchronous
    SQLite on the event loop, ``/api/calibration`` held every response on the
    installation for 1.6 to 3.2 seconds each time the dashboard's sixty-second
    timer fired — the once-a-minute freeze issue #63 chased through the rollup
    pass for days. And on its own connection a reader sees zero interference
    from writers under WAL, where a thread sharing the primary connection would
    serialise against the collector's writes and hand the stall back.
    """
    with request.app.state.store.read_view() as view:
        yield view


_ReadStore = Annotated[SqliteStore, Depends(_read_store)]

# The cookie the session token rides in. Named here because both the login
# route that sets it and the guard that reads it have to agree, and a literal
# typed in two places drifts the way every other repeated literal here does.
_SESSION_COOKIE = "arraysense_session"


def _has_session(request: Request) -> bool:
    """Whether this caller may see the protected read surface.

    With no password set the answer is always yes — that is the whole
    optional-ness of the feature. With one stored, only a session cookie the
    sessions table recognises counts. The read-side filtering and the write
    guard share this one answer, so they cannot disagree about who is allowed
    to see the identifying values.
    """
    settings = SettingsStore(request.app.state.store)
    if not password_is_set(settings):
        return True
    token = request.cookies.get(_SESSION_COOKIE)
    return token is not None and request.app.state.sessions.valid(token)


async def _require_write(request: Request) -> None:
    """Allow an unauthenticated write only while no password is set.

    This is the optional half and the whole point of #34: with no hash stored
    the request passes through exactly as it did before authentication
    existed. With one stored, the session cookie is checked against the
    in-memory sessions, and an absent, unknown or expired session is a 401.
    What this protects against is casual and accidental writes from anything
    on the LAN; the traffic is plain HTTP and observable, and the auth
    endpoints' docstrings say so rather than claiming more.
    """
    if not _has_session(request):
        raise HTTPException(status_code=401, detail="authentication required")


_INVERTER_NAMES = frozenset(spec.name for spec in INVERTER_METRICS)

# A live view gets everything the inverter reports, not a chosen subset. It is
# one row from one table either way, so narrowing it would save nothing and
# would mean editing this file every time a panel wants a reading it does not
# already have. Everything the INVERTER reports: the site metrics are excluded,
# because latest() answers with the newest row carrying any requested column,
# and a weather row carries its own two — including them handed the dashboard
# the sky's row, every inverter field null, for the seconds between a weather
# tick and the next poll. The sky reaches a page through its own request, not
# by riding this one.
_LIVE_INVERTER = tuple(spec.name for spec in INVERTER_METRICS if spec.name not in SITE_METRICS)

# How far back to look for the last full charge. Sixty days is twice the most
# lenient interval any vendor publishes, so a bank that has not charged fully
# within it has not charged fully in any sense that matters, and there is
# nothing to gain from searching further.
CALIBRATION_SEARCH_DAYS = 60.0

# The minute tier is retained for a year and holds 86,400 rows over that
# search — two columns each, which is a cheap scan. The raw tier is only kept
# for thirty days, so it cannot answer the question at all.
_CALIBRATION_TIER = "minute"

# Absorb windows examined, newest first. A bank charges fully at most about
# once a day, so this covers well over a month of candidates while bounding the
# per-window module reads that follow. Counted against the short candidates
# rather than the sustained ones: the reference installation's sixty days hold
# 27 of the former against 11 of the latter, so the budget still spans the
# search, and the slice keeps the newest — which is the one that decides the
# answer.
_MAX_WINDOWS_EXAMINED = 40

# How far before a costed period to start reading its counters. The first
# interval needs an earlier reading to be measured *from*; without one it
# starts at whatever row happens to fall inside it, and the first stretch of
# every month comes back short. Two hours matches the widening the energy
# endpoint does for the same reason.
COUNTER_LEAD = timedelta(hours=2)

# Settings the collector reads once, when it starts. Everything else — the
# tariff, the display defaults — is read afresh on each request that needs it,
# and takes effect as soon as it is saved.
RESTART_PREFIXES = ("connection.", "collector.")


def _request_zone(store: SqliteStore, tz: str | None) -> ZoneInfo:
    """Return the zone this request's answer is cut on.

    The installation's own zone first, then whatever the browser asked for,
    then the machine's — see ``resolve_zone``. Read from the settings on every
    request rather than held anywhere: nothing in this service memoises a
    figure, so a zone changed on the settings page takes effect on the next
    request with no cache to invalidate and no stale money to reconcile.
    """
    configured = SettingsStore(store).get(SETTING_TIMEZONE)
    return resolve_zone(tz, configured if isinstance(configured, str) else None)


def _energy_so_far_kwh(
    hourly_means: Sequence[tuple[datetime, float]],
    now: datetime,
) -> float | None:
    """Energy from a run of hourly mean powers, with the open hour left open.

    Each hourly row is a mean over the readings inside its bucket, so its energy
    is that mean times the hour — except for the hour in progress, which is a
    mean over the part of it that has happened. The maintenance pass rebuilds
    the hourly tier through the open bucket deliberately, so the current hour is
    always there and always short; multiplying it by a whole hour claims energy
    nobody has made yet, and the overstatement is largest at the top of the hour
    and worth 9-10 kWh against a 77 kWh day on the reference array.

    None when there is nothing to add up: a dash, never a zero, because zero is
    a claim the array produced nothing.

    What this still cannot see is an hour the collector covered only in part —
    a mean over five minutes of an hour it was up for reads as an hour. The
    hourly tier's ``sample_count`` is the only witness to that, and turning it
    into minutes needs a cadence figure that is 11 s configured, 12 s achieved
    and 16 s averaged over a long window, so it would trade a bounded error for
    an unbounded guess.
    """
    if not hourly_means:
        return None
    total_wh = 0.0
    for hour, mean_w in hourly_means:
        # Both are UTC here, but the duration goes through _elapsed anyway: it
        # is the habit that keeps the one that matters from being the exception.
        elapsed_hours = _elapsed(hour, now) / 3600.0
        total_wh += mean_w * min(max(elapsed_hours, 0.0), 1.0)
    return total_wh / 1000.0


def _weather_interval(store: SqliteStore) -> float:
    """How often the forecast is re-made, in seconds.

    Served to the page so its caption can say how fresh the prediction is
    without writing a number into the markup. The interval is a setting with a
    range of five minutes to a day, so a page that hard-coded fifteen minutes
    would be wrong for any owner who moved it — the same drift the settings page
    avoids by rendering itself from the registry.

    A stored value that is not a number falls back to the registry's own
    default, so the cadence a page shows and the cadence the poller keeps have
    one home between them.

    The literal behind that is reachable only if the registry itself is edited
    to declare this setting without a numeric default, which is a programming
    error. The weather poller stops on exactly that condition, and rightly: its
    task is restarted and a wrong interval would go unnoticed for hours. This is
    an HTTP handler, and failing the whole dashboard over a caption's cadence
    would be the worse trade, so it names a number and carries on.
    """
    value = SettingsStore(store).get(WEATHER_INTERVAL_KEY)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    default = lookup_setting(WEATHER_INTERVAL_KEY).default
    return float(default) if isinstance(default, (int, float)) else 900.0


# How old the newest stored reading may be before the reader is warned that the
# screen is behind. About eighty missed polls at the reference cadence, so
# neither a dropped read nor the minute a restart costs can reach it.
#
# Deliberately not the collector's stall threshold, and not derived from it.
# That one answers "is the poll loop running", which this endpoint asks the
# service directly; this one answers "is what you are looking at current",
# which is a question about the store and stays here.
STALE_AFTER = timedelta(minutes=15)

# How far back to look for a reading when the newest stored row is a recorded
# gap. Long enough to name the hour an outage began, short enough that the scan
# stays about two thousand raw rows — a status poll runs every thirty seconds on
# every open page and may not walk a month of history. Measured at 1.5 ms on a
# week-long raw table whose whole window was gaps, which is the worst this can
# do because the search then reads all of it and finds nothing, against 0.2 ms
# for the ordinary case where the newest row is a reading and nothing is
# scanned at all.
#
# Past the window the honest answer is that there is no reading in it. That is
# not a shrug: an outage of six hours and one of six days call for the same
# reaction, and inventing a number for the second would be the more precise lie.
READING_SEARCH = timedelta(hours=6)

# Which layer the service says failed, and what the page calls it. A reply the
# driver could not turn into a sample is neither an outage nor a disk fault: the
# inverter answered and our own decoding refused it, so naming it after either
# of the other two sends the reader somewhere there is nothing to find.
_FAULT_VERDICT = {"transport": "inverter", "store": "storage", "build": "driver"}

# The verdicts that name a fault, and so the ones allowed to quote the recorded
# reason. Derived from the mapping above rather than listed again, because the
# two drifted apart the moment a third fault was added: the new verdict rendered
# with its cause suppressed, which for a decode failure is the only thing on the
# page that says what was refused.
_NAMED_FAULTS = frozenset(_FAULT_VERDICT.values())


class YieldRequest(BaseModel):
    """How long to hand the dongle over for."""

    seconds: float = Field(default=300.0, gt=0, le=3600)


def _parse_metrics(raw: str, known: frozenset[str] | set[str], kind: str) -> list[str]:
    """Split a comma-separated metric list, rejecting anything unrecognised.

    A typo silently returning no data is worse than an error, so an unknown name
    is a 400 that says which one it was.
    """
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="no metrics requested")
    unknown = [n for n in names if n not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown {kind} metric(s): {unknown}")
    return names


def _device(device: str | None) -> str | None:
    """Normalise a ``device`` query parameter, refusing a blank one.

    A browser sends ``?device=`` readily — an empty input, a cleared select, a
    hand-built URL. Passing that through reached the store as a device nothing
    has ever recorded, which answered with no rows and no error and read as an
    inverter that had stopped reporting. The store refuses it now, so without
    this the same request became a 500. It is the caller's mistake either way,
    and a 400 that names it is the useful answer.
    """
    if device is None:
        return None
    if not device.strip():
        raise HTTPException(status_code=400, detail="device must not be blank")
    return device


def _check_range(start: datetime, end: datetime) -> None:
    """Reject a range that ends before it starts."""
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")


@contextmanager
def _inside_the_calendar() -> Iterator[None]:
    """Turn a walk that steps off the end of the calendar into a 400.

    Every one of these endpoints walks its range a day or a month at a time, and
    a bound near year 1 or year 9999 makes the next step land outside anything a
    ``datetime`` can hold. That is the caller's mistake — a mistyped year in a
    date field reaches it — and every other malformed input on the same
    endpoints already answers 400 or 422: an unknown metric, a blank device, a
    reversed range, an unknown zone, a bad period. Only this shape told somebody
    who typed a date wrongly that the server was broken, and left a traceback in
    the log of an unattended service saying so.

    ``OverflowError`` is always a calendar-bounds problem — the guard can prove
    it — so it carries a prefix saying so.  ``ValueError`` is passed through
    with its original detail because it can come from a domain check inside a
    calendar-walking function (a costed period that is too long, a reversed
    range) and those messages are already specific enough on their own.  This
    guard should only wrap calendar-walking code, never business logic, so a
    ``ValueError`` from an unrelated cause does not become a client error here.
    """
    try:
        yield
    except OverflowError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"the range steps outside the calendar: {exc}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _cadence_seconds(poll_interval: float) -> int:
    """The poll interval as whole seconds, never zero.

    ``select_tier`` is right to call a cadence of zero a programming error — it
    divides by it — but a sub-second interval is a legal file config: Config
    accepts anything above zero and only the settings overlay is bounded at one
    second, so ``poll_interval = 0.5`` in config.toml reached ``int()``, came out
    zero, and turned every chart on the Graphs page and the whole of the battery
    history into a 500 with nothing in it naming the interval. A poll faster
    than the tier resolution can express is one second's worth of cadence for
    the purpose of choosing a tier.
    """
    return max(1, int(poll_interval))


def _row_time(row: Mapping[str, Any] | None) -> datetime | None:
    """The timestamp of a stored row, or None if there is no row."""
    if row is None:
        return None
    stamp = row.get("timestamp")
    return stamp if isinstance(stamp, datetime) else None


# What witnesses "the inverter reported": every inverter metric that is not the
# site's. Two writers share the raw tier, and the sky landing a row every
# fifteen minutes must not read as the inverter answering — a stale banner the
# weather keeps quiet would hide exactly the outage it exists to announce.
# Computed lazily because the registry is read at call time everywhere else,
# and a test that extends the registry must see the new column here too.
def _inverter_witness() -> list[str]:
    """The metric columns whose presence means the inverter itself reported."""
    return [name for name in inverter_metric_columns() if name not in SITE_METRICS]


def _newest_reading(store: SqliteStore, now: datetime) -> tuple[datetime | None, bool]:
    """When the newest stored inverter reading was taken, and whether rows exist.

    The store's clock, deliberately, and not the collector's. ``last_success``
    lives in the process and comes back None the moment it restarts, so a
    collector crash-looping faster than the staleness threshold read as
    perfectly current every time — which is the one case the warning exists
    for. Rows outlive the process that wrote them.

    A recorded gap is not a reading. It carries a reason and no values, so a
    page drawing it shows dashes, and counting one as data reports a screen
    full of nothing as up to date. Nor is a weather row: the sky poller writes
    on its own clock, and its rows carry no inverter column at all — aging the
    dashboard by them would keep the banner quiet through an inverter outage.
    So the question is asked with the inverter's own columns as the witness,
    and ``latest`` answers with the newest row carrying any of them or a gap.

    Returns no timestamp when every row inside the search window is a gap,
    which is a longer outage rather than a fresh install — the second half of
    the answer tells those apart, and a caller that flattened them would either
    warn about an install that has simply not polled yet or stay quiet through
    an outage that has run all day.
    """
    witness = _inverter_witness()
    newest = store.latest(witness)
    if newest is None:
        # No inverter reading and no gap ever — but the tier may still hold
        # weather rows, and "holds anything" keeps its row-based meaning so a
        # sky-only database reads as an install that has not polled yet.
        return None, store.latest([]) is not None
    if not newest.get("error"):
        return _row_time(newest), True
    for row in reversed(store.query(witness, now - READING_SEARCH, now)):
        if not row.get("error") and any(row.get(name) is not None for name in witness):
            return _row_time(row), True
    return None, True


def _staleness(service: CollectorService, store: SqliteStore, now: datetime) -> dict[str, Any]:
    """Whether the screen is out of date and what is behind it, for the page to print.

    The banner used to work this out in the browser, from a copy of the poll
    loop's stall threshold and a copy of most of ``stalled_for``. The copies had
    already drifted: the service treats a dead poll task as stopped the instant
    it dies, while the page was still telling the reader to wait twenty minutes
    for a restart. So the verdict is reached once, here, and ``stalled_for`` is
    asked rather than reimplemented — with the same ``now`` it is measured
    against, so the two cannot disagree about a borderline second.

    Naming the fault is the other half. The page cannot tell an unreachable
    inverter from a database refusing writes, because the service records both
    in ``last_error``, and it guessed the inverter — sending whoever read it
    after the dongle, the WiFi and the breaker while the disk was the problem.
    The service now says which layer failed, so the answer is read rather than
    inferred. That mattered once there were three answers instead of two: a
    reply the driver could not turn into a sample arrives with the connection
    up, so deriving the fault from ``connected`` alone blamed the store, and
    marking the connection down to avoid that blamed the inverter while it was
    answering every poll. ``consecutive_failures`` gates the whole question,
    because it is cleared by a success and ``last_failure`` never is — reading
    that field alone is what let the banner fire over a poll that had just
    worked.
    """
    s = service.status
    stalled: timedelta | None = service.stalled_for(now)
    reading_at, any_rows = _newest_reading(store, now)
    age = (now - reading_at).total_seconds() if reading_at is not None else None
    stale = (
        stalled is not None
        or (age is None and any_rows)
        or (age is not None and age > STALE_AFTER.total_seconds())
    )

    if s.yielding:
        verdict = "yielding"
    elif stalled is not None:
        verdict = "stopped"
    elif not s.running:
        verdict = "not_running"
    elif s.consecutive_failures:
        verdict = _FAULT_VERDICT.get(
            s.last_failure_kind or "", "storage" if s.connected else "inverter"
        )
    elif stale:
        verdict = "silent"
    else:
        verdict = "fresh"

    return {
        "stale": stale,
        "verdict": verdict,
        "reading_at": reading_at.isoformat() if reading_at else None,
        "age_seconds": age,
        "any_rows": any_rows,
        "searched_seconds": READING_SEARCH.total_seconds(),
        "stalled_seconds": stalled.total_seconds() if stalled is not None else None,
        # Only where the verdict names a failure. The field is left set by the
        # service long after the fault it describes has cleared, so quoting it
        # beside any other verdict attaches an old cause to a new condition.
        "reason": s.last_error if verdict in _NAMED_FAULTS else None,
    }


@router.get("/geocode")
async def geocode_route(q: str, country: str | None = None) -> dict[str, Any]:
    """Resolve a postcode or place name to coordinates via Open-Meteo geocoding.

    Reads nothing and writes nothing — needs no store. Returns a list of
    candidates, empty when the service finds nothing, absent when the fetch
    itself failed. A page must show every candidate so the owner picks;
    a single candidate already fills the boxes.
    """
    results = geocode(q.strip(), country.strip() if country else None)
    if results is None:
        raise HTTPException(status_code=502, detail="geocoding service unreachable")
    return {"query": q.strip(), "candidates": results}


@router.get("/status")
async def status(request: Request, tz: str | None = None) -> dict[str, Any]:
    """Whether the collector is alive, connected, and holding the dongle.

    It also answers the one question a page has to settle before it can ask
    anything else: which calendar the service will cut its answers on.
    ``timezone`` is what ``resolve_zone`` makes of the installation's setting,
    the ``tz`` a caller passes, and the machine's own zone, in that order — so a
    page builds "this month" and "the last thirty days" in the zone the reply
    will come back in rather than in the browser's. It used to build them in the
    browser's, and on an install five hours west of the reader that lost the
    whole monthly connection charge and dropped a real day out of the history
    table while leaving it in the footer's total.

    Answered here because the precedence is the service's rule. A page working
    it out from ``site.timezone`` would be a second implementation of it, which
    is the shape of every disagreement this codebase has had.
    """
    store = request.app.state.store
    try:
        zone = _request_zone(store, tz)
    except KeyError:
        # A zone this tz database does not know is exactly what /api/energy
        # refuses, and a page told the truth here asks with one that works
        # instead of collecting a 400 first. Stepping past the name lands on
        # the same zone that request would have fallen back to.
        zone = _request_zone(store, None)
    service = request.app.state.service
    s = service.status
    return {
        "version": __version__,
        "timezone": str(zone),
        "running": s.running,
        "connected": s.connected,
        "yielding": s.yielding,
        "yield_until": s.yield_until.isoformat() if s.yield_until else None,
        "last_success": s.last_success.isoformat() if s.last_success else None,
        "last_failure": s.last_failure.isoformat() if s.last_failure else None,
        "last_error": s.last_error,
        "consecutive_failures": s.consecutive_failures,
        "total_samples": s.total_samples,
        "total_failures": s.total_failures,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        # Crossed replies the adapter retried. The dongle serves its vendor's
        # cloud on the same socket and the answers cross, so a read of register
        # 32 comes back carrying 5000. One recovered is the dongle being itself
        # and a climbing rate is a fault, but neither was visible from outside:
        # a successful retry logs at DEBUG and the service runs at INFO, which
        # made the healthy case and the failing case look identical.
        #
        # None rather than 0 from a source that does not count them. Zero is a
        # measurement meaning none happened, and claiming it from something that
        # never looked is the same error as rendering a missing reading as 0.
        "misroutes": getattr(service.source, "misroutes", None),
        # What a connect-time model read found, when it found a disagreement
        # worth saying. A family mismatch never reaches here — it raises at
        # connect and stops the collector, like a wrong serial does — so the
        # only shape this carries is the exact-model warning, and None is what
        # an unconfigured, a correctly configured, and an unreadable
        # installation all get. The dashboard polls this endpoint already; it
        # needs no second request to say so.
        "model_check": getattr(service.source, "model_check", None),
        # The verdict the stale banner prints. Reached here because it is a
        # judgement, and one made in the browser is one that can disagree with
        # the watchdog about whether the collector is running.
        "staleness": _staleness(service, request.app.state.store, datetime.now(tz=UTC)),
    }


@router.get("/live")
def live(request: Request, store: _ReadStore, device: str | None = None) -> dict[str, Any]:
    """The most recent inverter reading and every battery module's latest.

    What a wall display polls. Absent values stay null — a battery block empty
    because CAN is down must not arrive as 0% state of charge.

    ``device`` names an inverter and defaults to the configured one, so a page
    that sends nothing gets exactly what it always got.

    The ``sky`` block carries the site's own weather readings — outside
    temperature and cloud cover — written by a second poller on its own clock.
    It rides the live response as its own object so the dashboard needs no
    second request, but it never mixes into the inverter block: a weather row
    must not blank the inverter read, and an inverter gap must not blank the
    sky.
    """
    device = _device(device)
    inverter = store.latest(list(_LIVE_INVERTER), device=device)
    modules = store.latest_modules(list(module_metric_columns()), device=device)
    # Named here rather than in the browser. Which flow is powering the house
    # is an interpretation of five readings, and an interpretation computed in
    # two places drifts — the Costs page already proved that with money. The
    # page prints what this says.
    status = operating_mode.assess(inverter or {})
    # The battery block: how fast the bank is filling or emptying, and its
    # voltage. The rate is derived from readings the driver already collects:
    # capacity comes from battery_full_capacity_ah and battery_voltage_v, and
    # the sign follows battery_power_w so discharge reads negative. A bank
    # whose capacity or power nobody reported carries null for the rate; zero
    # is a real reading (an idle bank) and stays zero.
    battery_block = _battery_block(inverter)
    # Weather: the site metrics the sky poller writes on its own clock.
    # include_gaps=False so an inverter gap newer than the last weather tick
    # does not blank the sky — the walk continues past gaps to the last real
    # reading. None when no weather row exists or when the row has no values
    # at all, never an object of two nulls.
    sky: dict[str, Any] | None = None
    sky_row = store.latest(sorted(SITE_METRICS), device=device, include_gaps=False)
    if sky_row is not None:
        formatted = _isoformat_row(sky_row)
        temp = formatted.get("outside_temperature_c")
        cover = formatted.get("cloud_cover_pct")
        if temp is not None or cover is not None:
            sky = {
                "timestamp": formatted["timestamp"],
                "outside_temperature_c": temp,
                "cloud_cover_pct": cover,
            }
    return {
        "inverter": _isoformat_row(inverter) if inverter else None,
        "modules": [_isoformat_row(m) for m in modules],
        "mode": {
            "mode": status.mode.value,
            "battery": status.battery.value,
            "why": status.why,
            "known": status.known,
        },
        "battery": battery_block,
        "sky": sky,
        # The high-usage warning rides this response rather than a second poll:
        # it is decided from the same inverter reading already in hand, and a
        # wall display should not have to ask twice to be told its house is
        # drawing hard. Null when the threshold is off or the house is under it.
        "alert": _high_usage_alert(request, inverter),
    }


def _high_usage_alert(
    request: Request, inverter: Mapping[str, Any] | None
) -> dict[str, Any] | None:
    """Whether the house is drawing more than the owner asked to hear about.

    The verdict is computed here rather than in the browser for the reason the
    Costs page settled: a threshold compared in two places is two thresholds the
    day one of them changes. The page prints what this says.

    Attribution is the Emporia module's, and only if it is running. Without it
    the warning still fires and names nobody, which is the difference between
    "your house is drawing 11 kW" and silence.
    """
    settings = SettingsStore(request.app.state.store)
    threshold = settings.get(HIGH_USAGE_WATTS_KEY)
    if not isinstance(threshold, int) or threshold <= 0:
        return None
    load = inverter.get("load_power_w") if inverter else None
    poller = _emporia(request)
    contributors: tuple[Contributor, ...] = ()
    if poller is not None:
        contributors = tuple(
            Contributor(circuit.name, circuit.watts, circuit.kind)
            for circuit in poller.repository.latest()
        )
    verdict = high_usage(
        int(load) if isinstance(load, int | float) else None, threshold, contributors
    )
    if verdict is None:
        return None
    return {
        "load_w": verdict.load_w,
        "threshold_w": verdict.threshold_w,
        "accounted_w": verdict.accounted_w,
        "complete": verdict.complete,
        "contributors": [
            {"name": c.name, "watts": c.watts, "kind": c.kind} for c in verdict.contributors
        ],
    }


@router.get("/capabilities")
async def capabilities(request: Request) -> dict[str, Any]:
    """What each device is and which metrics it produces, for pages to draw from.

    The store answers every query for every registry metric — one this device
    cannot produce reads back null, the same null a reading nobody took gives —
    so nothing in the data itself separates "cannot produce" from "did not
    report". This list is the only thing that does, and a page honours the
    difference by drawing what is declared here instead of enumerating the
    reference inverter's registers by hand, which shows a one-string machine
    two permanently empty charts.

    The answer is the driver's own declaration, read off the already-built
    source. No inverter round trip: capabilities are what the device *is*, and
    a leaflet must not cost the dongle's single TCP slot.

    ``metrics`` holds the inverter-level registry names and
    ``battery_module_metrics`` the bare per-module names this device produces —
    the same bare form the battery endpoints take, though those accept any
    registry template — both in registry order so every page renders one
    metric order.
    ``energy`` says whether kWh figures are counters the inverter keeps itself
    or an estimate integrated from power — a page must be able to tell a meter
    reading from a guess.
    ``transport`` is how this installation reaches the inverter — the built
    source reports what its configuration chose, not the family default — so a
    page labels the connection it actually has instead of hard-coding the
    dongle's.

    ``devices`` is a list because a parallel stack is several inverters behind
    one service, even though today's collector polls one. The serial in each
    entry is masked unconditionally — an installation secret by this project's
    own rules, and nothing on any page renders it. Three states, kept apart on
    the project's own rule that absent capability is not absent data:
    a driver that describes itself gets a full entry; a bare InverterSource —
    which names its device but carries no declaration — gets an entry with its
    known serial and null everywhere a declaration would answer, because "not
    established" must not read as "produces nothing" (null metrics, never an
    empty list); and only a source that cannot even name a device contributes
    nothing at all.
    """
    source = request.app.state.service.source
    identity = getattr(source, "identity", None)
    declared = getattr(source, "capabilities", None)
    detection = getattr(source, "model_detection", None)
    serial = identity.serial if identity is not None else getattr(source, "device", None)
    devices: list[dict[str, Any]] = []
    if serial is not None:
        entry: dict[str, Any] = {
            "device": _mask(serial),
            "driver": identity.driver if identity is not None else None,
            # ``model`` is what the installation is configured as. Detection
            # reports, never reconfigures, so the wire's answer is a second
            # fact carried beside it — absent entirely (None) for a source that
            # has no detection to report, not absent data about the device.
            "model": identity.model if identity is not None else None,
            "model_detection": (
                {
                    "checked": detection.checked,
                    "detected": detection.detected,
                    "family": detection.family,
                }
                if detection is not None
                else None
            ),
            "pv_strings": None,
            "energy": None,
            "backup_output": None,
            "generator_input": None,
            "split_phase": None,
            "three_phase": None,
            "parallel_capable": None,
            "per_module_battery": None,
            "transport": None,
            "metrics": None,
            "battery_module_metrics": None,
        }
        if declared is not None:
            capabilities_update: dict[str, Any] = {
                "pv_strings": declared.pv_strings,
                "energy": declared.energy.value,
                "backup_output": declared.backup_output,
                "generator_input": declared.generator_input,
                "split_phase": declared.split_phase,
                "three_phase": declared.three_phase,
                "parallel_capable": declared.parallel_capable,
                "per_module_battery": declared.per_module_battery,
                "transport": declared.transport,
                "metrics": list(inverter_metric_columns(declared.metrics)),
                "battery_module_metrics": list(module_metric_columns(declared.metrics)),
            }
            if declared.conversion is not None:
                capabilities_update["conversion"] = {
                    "cec_pct": declared.conversion.cec_pct,
                    "max_pv_to_grid_pct": declared.conversion.max_pv_to_grid_pct,
                    "max_battery_to_grid_pct": declared.conversion.max_battery_to_grid_pct,
                    "max_pv_to_battery_pct": declared.conversion.max_pv_to_battery_pct,
                    "idle_normal_w": declared.conversion.idle_normal_w,
                    "idle_standby_w": declared.conversion.idle_standby_w,
                    "approximate": list(declared.conversion.approximate),
                    "citation": declared.conversion.citation,
                }
            entry.update(capabilities_update)
        devices.append(entry)
    return {"devices": devices}


def _packs_during(store: SqliteStore, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Read every module's state of charge across one absorb window and its margins.

    Tries the full-cadence tier first and falls back to hourly. Raw module data
    is kept for thirty days and the search reaches back sixty, so the older
    half of the range can only be answered from the rollup — where a pack that
    held at full for an hour still averages near 100, and one that touched it
    briefly does not. That errs towards not claiming a full charge, which is
    the right direction: the cost of missing one is a warning shown a few days
    early, and the cost of inventing one is silence for a month.

    The range is the window widened by ``PACK_RESET_LAG`` at both ends, and the
    two margins are read for opposite reasons: the later one catches a pack that
    snapped its counter after the bank left absorb, the earlier one is the
    evidence that the packs were not already reading full before it started.
    ``charge_completed_at`` slices the window itself back out.

    Both bounds are timezone-aware, and the result is empty when no pack
    reported at all during the window — which is a different thing from every
    pack reporting and none of them being full.
    """
    start -= PACK_RESET_LAG
    end += PACK_RESET_LAG
    rows: list[dict[str, Any]] = store.query_modules(["soc_pct"], start, end, tier="full")
    if not rows:
        rows = store.query_modules(["soc_pct"], start, end, tier="hourly")
    return rows


@router.get("/calibration")
def calibration(request: Request, store: _ReadStore) -> dict[str, Any]:
    """How far the per-pack state-of-charge estimates have drifted from the truth.

    Each pack counts amp-hours to estimate its charge and cannot correct itself
    until it charges fully, so the useful question is not what the packs say
    but how long it has been since anything forced them to agree with reality.

    The answer separates two conditions that look alike on a dashboard and are
    not alike at all. Packs that disagree on percentage while agreeing on
    voltage have drifting counters and healthy batteries. Packs that disagree
    on voltage have a hardware fault, because parallel packs are physically
    forced to the same voltage.
    """
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=CALIBRATION_SEARCH_DAYS)

    history = store.query(
        # battery_current_a is not decoration: full_charge_windows rejects a
        # window still pushing charge current, and without the column that
        # safeguard is silently inert — every absorb looks settled.
        ["battery_voltage_v", "bms_charge_voltage_ref_v", "battery_current_a"],
        start,
        now,
        tier=_CALIBRATION_TIER,
    )
    latest = store.latest_modules(["soc_pct", "voltage_v", "cycle_count"])
    # Every pack the bank is known to contain has to have reached full, not
    # merely every pack that happened to be talking at the time. A CAN dropout
    # during a charge would otherwise reset the drift clock for the whole bank
    # on behalf of a pack that never recalibrated.
    known = [str(row["serial"]) for row in latest if row.get("serial")]

    # A one-minute hold rather than twenty. What separates a charge from a
    # voltage excursion here is the packs, not the clock: the reference
    # installation crosses absorb and tapers to zero in three minutes, so a
    # twenty-minute candidate list contains none of its charges at all. Every
    # window on this list still has to end settled below the taper, and
    # ``charge_completed_at`` is what decides which of them a bank has passed.
    candidates = full_charge_windows(history, min_absorb=CORROBORATING_ABSORB)[
        -_MAX_WINDOWS_EXAMINED:
    ]
    last_full: datetime | None = None
    for index in reversed(range(len(candidates))):
        window_start, window_end = candidates[index]
        packs = _packs_during(store, window_start, window_end)
        # The previous candidate's end caps how far back the below-full evidence
        # may be read. Two touches closer together than PACK_RESET_LAG would
        # otherwise let the later one borrow the earlier charge's transition.
        reset = charge_completed_at(
            window_start,
            window_end,
            packs,
            expected=known or None,
            after=candidates[index - 1][1] if index else None,
        )
        if reset is not None:
            last_full = reset
            break

    status = assess(
        now=now,
        last_full=last_full,
        searched_days=CALIBRATION_SEARCH_DAYS,
        modules=latest,
    )
    payload = asdict(status)
    when = payload["last_full_charge"]
    payload["last_full_charge"] = when.isoformat() if when else None
    return payload


@router.get("/settings")
async def read_settings(request: Request) -> dict[str, Any]:
    """Every setting, its current value, and enough description to render a control.

    The registry travels with the values so the page does not hard-code labels,
    bounds or choices. A page that hard-codes them drifts from the validation
    the moment either changes, and the drift shows up as a control offering a
    value the server then refuses.

    Identifying values come back masked. With a password set and no session
    they are omitted entirely rather than masked — absent is the project's own
    answer to data a caller may not have, and a wall display that never logs
    in still gets the display defaults without ever seeing a contact email or
    a serial. With a valid session, or with no password set, the payload is
    exactly what it always was.
    """
    settings = SettingsStore(request.app.state.store)
    values = settings.public()
    if not _has_session(request):
        values = {key: value for key, value in values.items() if not lookup_setting(key).secret}
    return {"fields": describe(), "values": values}


def _reject_unbootable_connection(request: Request, wanted: dict[str, Any]) -> None:
    """Refuse a settings write whose connection group would not boot.

    Only fires when the write touches connection keys. It computes the exact
    config the next start will assemble — the FILE config, the stored settings,
    and this pending change layered on with the real merge semantics — and
    hands it to the same registry validation the collector runs. Reusing
    ``effective`` rather than re-deriving the merge is the whole point: an
    earlier version validated against the already-merged config and skipped
    empty values by hand, which modelled a cleared field as "keep the current
    value" when the next boot reverts it to the file's, and let an unbootable
    combination through. Raises ValueError, which the settings route maps to
    400 alongside every other validation failure.
    """
    if not any(key in _SETTING_KEYS.values() for key in wanted):
        return
    file_config = getattr(request.app.state, "file_config", request.app.state.config)
    settings = SettingsStore(request.app.state.store)
    drivers.validate(effective(file_config, settings, pending=wanted))


def _reject_undeclared_mppts(request: Request, wanted: dict[str, Any]) -> None:
    """Refuse an array whose strings name MPPTs the inverter does not have.

    The grammar cannot know the driver — check= is a pure function of the
    text — so the write path enforces it, where the built source's declaration
    is in scope. Same layering as the connection guard above: refuse at the
    write, never store a config a later reader chokes on.
    """
    text = wanted.get("panels.strings")
    if not isinstance(text, str) or not text.strip():
        return
    declared = getattr(request.app.state.service.source, "capabilities", None)
    if declared is None:
        return  # a bare source declares nothing; nothing to enforce against
    strings = parse_strings(text)  # already validated by the registry; cheap
    for s in strings:
        if s.mppt > declared.pv_strings:
            raise ValueError(
                f"string {s.name!r} is on MPPT {s.mppt}, but this inverter "
                f"declares {declared.pv_strings} string input(s)"
            )


def _reject_unwritable_backup_dir(request: Request, wanted: dict[str, Any]) -> None:
    """Refuse a backup destination this service cannot write to.

    The registry cannot ask this. Its ``check=`` callbacks are functions of the
    text and answer the same on every machine, while whether a directory can be
    written is a fact about the disk and the sandbox in front of it. So it is
    asked here, at the only moment somebody is present to read the remedy —
    a backup that first discovers the problem at 03:15 discovers it alone.

    Only when the value changes. The page posts what was edited, but nothing
    stops a client posting the whole form, and an installation whose backup
    disk has gone missing must still be able to change its tariff. Refusing
    every save over a fault the save did not introduce would lock the settings
    page over a broken backup.

    Raises ValueError, which the route maps to 400 with the rest.
    """
    wanted_dir = wanted.get(BACKUP_DIRECTORY_KEY)
    if not isinstance(wanted_dir, str):
        return
    settings = SettingsStore(request.app.state.store)
    if settings.get(BACKUP_DIRECTORY_KEY) == wanted_dir:
        return
    try:
        check_backup_directory(wanted_dir)
    except ValueError as exc:
        # Named the way the registry names its own failures. The settings page
        # finds the field a rejection belongs to by looking for the key in the
        # message, so a bare message lands in the page banner instead of under
        # the box that caused it — measured in a browser, where a remedy three
        # sections away from its own control is a remedy nobody reads.
        raise ValueError(f"{BACKUP_DIRECTORY_KEY}: {exc}") from exc


@router.put("/settings", dependencies=[Depends(_require_write)])
async def write_settings(request: Request, values: dict[str, Any]) -> dict[str, Any]:
    """Apply a settings change, validating every value before writing any of them.

    A masked value posted back unchanged is discarded rather than stored. The
    page renders identifying values with their middles replaced, so a form
    submitted without editing them would otherwise write the mask over the real
    serial and break the connection at the next poll.

    Returns the keys that actually changed, and whether any of them needs the
    collector restarted to take effect.
    """
    settings = SettingsStore(request.app.state.store)
    wanted = {k: v for k, v in values.items() if not _is_mask(k, v)}
    try:
        # A second write path to the same connection fields /setup writes; an
        # invalid combination here would boot-crash exactly as one there would,
        # so it is validated against the true next-boot merge before any row is
        # written. Inside the try so its ValueError maps to 400 like the rest.
        _reject_unbootable_connection(request, wanted)
        _reject_undeclared_mppts(request, wanted)
        _reject_unwritable_backup_dir(request, wanted)
        changed = settings.update(wanted)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, TypeError, OverflowError) as exc:
        # OverflowError rather than ValueError comes back from converting a
        # number too large for its type, and it was escaping as a 500.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "changed": changed,
        # Only what the collector reads once, at startup. The tariff is read on
        # every costs request and the display settings on every page load, so
        # telling the owner to restart after changing either is advice that
        # does nothing — and advice that does nothing is ignored when it counts.
        "restart_required": any(k.startswith(RESTART_PREFIXES) for k in changed),
        "values": settings.public(),
    }


def _is_mask(key: str, value: object) -> bool:
    """Whether this is a masked value being posted back rather than a real edit.

    Only applies to the identifying settings, and only to a value still
    carrying the mask character. A serial someone genuinely retyped has no
    bullets in it.
    """
    try:
        spec = lookup_setting(key)
    except KeyError:
        return False
    return spec.secret and isinstance(value, str) and "\N{BULLET}" in value


@router.get("/auth")
def auth_status(request: Request) -> dict[str, Any]:
    """Whether authentication is on, and whether this client holds a session.

    A read and deliberately open: the login form has to know whether to render
    at all, and this reveals only that a password is set — not the password,
    and not any other setting. That fact is not secret; every write endpoint
    already answers it, by answering 401 or not.
    """
    settings = SettingsStore(request.app.state.store)
    token = request.cookies.get(_SESSION_COOKIE)
    authenticated = token is not None and request.app.state.sessions.valid(token)
    return {"required": password_is_set(settings), "authenticated": authenticated}


class LoginRequest(BaseModel):
    """The password being presented for a session."""

    password: str


@router.post("/auth/login")
def login(request: Request, response: Response, body: LoginRequest) -> dict[str, Any]:
    """Start a session in exchange for the password.

    The cookie is HttpOnly so the page's own script cannot read it,
    SameSite=Strict so a request from another origin cannot ride it — the
    write API is CSRF-able today, and this closes that as a side effect — and
    Path=/ so it reaches every protected endpoint. Max-Age matches the session
    lifetime. Deliberately not Secure: this is plain HTTP on a LAN, and a
    Secure cookie would simply never be sent, so setting it would leave the
    owner unable to log in and nothing on the page to say why.
    """
    key = request.client.host if request.client else "unknown"
    now = time.time()
    if request.app.state.throttle.blocked(key, now):
        raise HTTPException(
            status_code=429,
            detail="too many failed attempts; try again shortly",
        )
    settings = SettingsStore(request.app.state.store)
    stored = password_hash(settings)
    if stored is None:
        # Nothing to guess yet, so nothing is counted. Counting here let a
        # stranger spend the owner's five attempts before the owner had set a
        # password at all, and the block is keyed on the address, so the
        # owner's own first login met a 429 somebody else earned — renewable
        # indefinitely, and waiting never cleared it. Measured before the fix:
        # five junk attempts with no password set, then the correct password
        # on a freshly set one answered 429.
        raise HTTPException(status_code=401, detail="incorrect password")
    if not verify_password(body.password, stored):
        request.app.state.throttle.record_failure(key, now)
        raise HTTPException(status_code=401, detail="incorrect password")
    request.app.state.throttle.record_success(key)
    token = request.app.state.sessions.issue()
    response.set_cookie(
        _SESSION_COOKIE,
        token,
        max_age=int(Sessions.SESSION_LIFETIME.total_seconds()),
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True}


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict[str, Any]:
    """End this session and clear its cookie.

    Always 200, even when nothing was logged in: logout is idempotent, and the
    login form must be able to call it without first asking whether a session
    exists. The cookie is deleted rather than merely expired, so the browser
    forgets it instead of re-sending a token the server no longer recognises.
    """
    token = request.cookies.get(_SESSION_COOKIE)
    if token is not None:
        request.app.state.sessions.revoke(token)
    response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return {"ok": True}


class PasswordRequest(BaseModel):
    """A new password, and the current one when one is already set."""

    new_password: str
    current_password: str | None = None


@router.post("/auth/password")
def change_password(request: Request, response: Response, body: PasswordRequest) -> dict[str, Any]:
    """Set, change or clear the password.

    Setting the first password needs no credential — there is none yet, and
    this endpoint is no more exposed than every other write it is protecting.
    Changing or clearing an existing password requires the current one,
    verified, whatever the session state: a session proves the browser once
    logged in, not that the person at it knows the password. An empty
    ``new_password`` clears the password — the owner's way of switching
    authentication off — and revokes every session.

    This shares the login throttle rather than keeping its own, because it
    verifies the same secret: guarding only the login endpoint would leave the
    backstop with a second door standing open, and the guessing rate through
    it was measured at unlimited against login's five-a-minute. Failures count
    only once a password is set — before that there is no secret to guess, and
    counting would let a stranger fill the throttle the owner is going to need.
    A consequence worth naming: five wrong guesses here also block login from
    that address for a minute, which is right, since it is one secret.
    """
    settings = SettingsStore(request.app.state.store)
    stored = password_hash(settings)
    if stored is not None:
        key = request.client.host if request.client else "unknown"
        now = time.time()
        if request.app.state.throttle.blocked(key, now):
            raise HTTPException(
                status_code=429,
                detail="too many failed attempts; try again shortly",
            )
        current = body.current_password
        if not current or not verify_password(current, stored):
            request.app.state.throttle.record_failure(key, now)
            raise HTTPException(status_code=401, detail="current password is required")
        request.app.state.throttle.record_success(key)
    if body.new_password:
        if len(body.new_password) < MIN_PASSWORD_LENGTH:
            raise HTTPException(
                status_code=400,
                detail=f"password must be at least {MIN_PASSWORD_LENGTH} characters",
            )
        set_password(settings, body.new_password)
    else:
        clear_password(settings)
        request.app.state.sessions.revoke_all()
        response.delete_cookie(_SESSION_COOKIE, path="/", httponly=True, samesite="strict")
    return {"ok": True}


@router.get("/costs")
def costs(
    request: Request,
    store: _ReadStore,
    start: datetime,
    end: datetime,
    tz: str | None = None,
) -> dict[str, Any]:
    """What the period cost, what it would have cost without solar, and the bill.

    The pricing lives here rather than in the page because a tariff has exactly
    one grammar and one meaning. When the browser had its own copy the two
    drifted immediately: the page rejected the seasonal band format the parser
    accepts, and then applied a summer peak window to a January evening.

    Money is absent, not zero, when no tariff is configured. An install that
    has never entered one shows its energy and says so.
    """
    settings = SettingsStore(store)
    tariff = load_tariff(settings.all())
    # The installation's zone decides which wall-clock hours a band covers, and
    # ``tz`` only speaks for the browser. This is the endpoint where getting it
    # wrong is a mispriced day rather than a shifted chart.
    #
    # So an unknown zone is refused, as ``/api/energy`` and ``/api/bands`` refuse
    # it, rather than fallen back on the way ``/api/status`` deliberately does.
    # A status banner cut on the wrong calendar is cosmetic; a month's cost is
    # not, and one cut on a zone the caller did not ask for is wrong in a way
    # nothing on the page reveals. Reaching here at all takes an install with no
    # zone configured — where one is set, ``resolve_zone`` never consults the
    # caller's name, not even to reject it (#49).
    try:
        zone = _request_zone(store, tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tariff is None:
        # Two different situations, and conflating them tells somebody staring
        # at the tariff they just typed that they have not entered one. Text
        # is stored but unusable only for a value saved before the grammar was
        # checked at write time, which is why the reason is worth carrying.
        stored = str(settings.all().get(SETTING_BANDS) or "").strip()
        return {
            "currency": None,
            "configured": False,
            # On this path as on the priced one. A page has to be able to say
            # which calendar it is showing whether or not there is money on it,
            # and a field that appears only sometimes is one every caller has to
            # branch on — the same reason the settings fields carry every key.
            "timezone": str(zone),
            "unreadable": bool(stored),
            "cost": None,
            "bill": None,
        }

    # A naive bound means the zone the caller asked about, not the server's.
    # Resolved here rather than deeper down, because the store query below
    # turns these into epoch seconds and would otherwise read a naive midnight
    # as midnight wherever the service happens to be installed.
    start, end = with_zone(start, zone), with_zone(end, zone)

    # Read back before the period starts so its first interval has a reading to
    # be measured *from*, rather than starting at whatever row happens to fall
    # inside it. Without the lead, the first stretch of every month is short by
    # however long it took the first sample to arrive.
    with _inside_the_calendar():
        lead = start - COUNTER_LEAD
    # Minute first, then coarser, then raw. Hourly has to be in the chain and
    # not just as a last resort: the minute tier is kept for a year, so a month
    # older than that has nothing there while the hourly tier holds it back to
    # the beginning. Falling straight from minute to raw — which is kept thirty
    # days — found nothing and priced August 2025 as unknown, while the History
    # page read the same month out of the hourly tier and showed $87.65.
    rows: list[dict[str, Any]] = []
    tier = "minute"
    for candidate in ("minute", "hourly", "full"):
        tier = candidate
        rows = store.query(list(ENERGY_FIELDS.values()), lead, end, tier=candidate)
        if rows:
            break
    with _inside_the_calendar():
        energy = period_energy(tariff, rows, start, end, zone)

    result = price_period(tariff, energy, fixed_charge=_month_charge(tariff, start, zone))
    bill = estimate_bill(tariff, energy)
    unpriced = round(unpriced_minutes(tariff, start, end, zone))

    # The page needs more than the priced totals to be honest about them: the
    # hours each band covers so it can be labelled, the house energy behind the
    # counterfactual, and whether any of the period fell outside every band.
    # Sending them from here keeps the page from deriving any of it a second
    # time, which is how the two implementations diverged in the first place.
    return {
        "currency": tariff.currency,
        "configured": True,
        "timezone": str(zone),
        "tier": tier,
        "cost": asdict(result) if result else None,
        "bill": asdict(bill) if bill else None,
        "rows": _band_rows(tariff, energy, result),
        "measured_minutes": energy.measured_minutes,
        "elapsed_minutes": energy.elapsed_minutes,
        "unpriced_minutes": unpriced,
        # Per counter: what the figures hold, what the meter counted that they
        # do not, and whether some loss has no statable size. The labels on
        # every money figure are worded from this, never from the minutes
        # above — minutes read fully covered while a counter sits silent,
        # which is precisely how the previous attempt overstated the savings.
        "shortfall": _shortfall_payload(energy.shortfall),
    }


def _month_charge(tariff: Tariff, start: datetime, zone: ZoneInfo) -> float | None:
    """The whole monthly connection charge, when the period is a billing month.

    The charge falls due once for the month however early in it you ask, so a
    month-to-date bill apportioning it showed "$3.11 of $15.00 so far" — an
    instalment nobody is billed, and an understatement of what the month will
    cost. A period beginning at local midnight on the first is that question.

    Anything else gets None and is apportioned, which is what keeps the two
    endpoints from answering the same question differently. Asked about a single
    day, this endpoint used to charge the whole fifteen dollars while the
    History page's row for that same day charged its share of it — 15.55
    against 0.74 for the identical day.

    Compared in the owner's zone, not in whatever the query string carried. The
    page asks for 05:00 UTC because that is local midnight in Chicago, and the
    same comparison against UTC midnight answers no to the one question this
    exists to ask. That is the third bug this project has had from reading a
    wall-clock question off a UTC instant.
    """
    local = start.astimezone(zone)
    return (
        tariff.fixed_monthly
        if local == local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        else None
    )


def _short_bands(entries: Mapping[str, EnergyShortfall], counter: str) -> frozenset[str]:
    """Bands one counter named as partly unmeasured, or none if it did not report.

    Written once rather than three times inline because the absent-counter case
    is easy to get subtly different between copies, and a band row marked from
    one counter and not another is precisely the bug this is here to prevent.
    """
    entry = entries.get(counter)
    return frozenset() if entry is None else entry.bands_possibly_short


def _band_rows(
    tariff: Tariff, energy: PeriodEnergy, result: CostResult | None
) -> list[dict[str, Any]]:
    """One finished row per band: its label, its energy, and every figure in money.

    Assembled here rather than in the page because the page multiplying a rate
    by a kilowatt-hour is the same mistake as the page parsing a tariff, only
    smaller and harder to spot. Every number below is either measured or
    absent; none of them is a zero standing in for something nobody knew.

    Per-row shortness flags map counter to column: import/cost ride on grid
    import, house columns on load, battery columns on battery discharge.
    """
    if result is None:
        return []
    house = dict(energy.load_kwh or {})
    battery = dict(energy.battery_discharge_kwh or {})
    by_name = {band.name: band for band in tariff.bands}
    # The candidate bands each counter reported, resolved once. A counter with no
    # accounting at all is not a counter with nothing to declare, but there is no
    # third state to send: the period-level flags already say the figures may not
    # be whole, and marking every row off a missing entry would say more than is
    # known. Empty here means "this counter named no band".
    entries = energy.shortfall or {}
    import_bands = _short_bands(entries, "grid_import")
    load_bands = _short_bands(entries, "load")
    discharge_bands = _short_bands(entries, "battery_discharge")

    rows: list[dict[str, Any]] = []
    for priced in result.bands:
        band = by_name.get(priced.name)
        house_kwh = house.get(priced.name)
        battery_kwh = battery.get(priced.name)
        house_cost = None if house_kwh is None else round(house_kwh * priced.price_per_kwh, 2)
        rows.append(
            {
                "name": priced.name,
                "price_per_kwh": priced.price_per_kwh,
                # A season is part of a band's identity: a peak window the
                # reader is not currently in, shown without saying so, reads as
                # a rate they are being charged today.
                "hours": ", ".join(r.label for r in band.hours) if band else "",
                "months": sorted(band.months) if band and band.months else None,
                "import_kwh": priced.kwh,
                "cost": priced.cost,
                "house_kwh": house_kwh,
                "house_cost": house_cost,
                "battery_kwh": battery_kwh,
                "battery_value": (
                    None if battery_kwh is None else round(battery_kwh * priced.price_per_kwh, 2)
                ),
                "saved": (
                    None
                    if house_cost is None or priced.cost is None
                    else round(house_cost - priced.cost, 2)
                ),
                # The band's name in one of these sets means its window was
                # partly unmeasured, so the columns drawn from that counter
                # must be qualified rather than read as whole (#31).
                "import_short": priced.name in import_bands,
                "house_short": priced.name in load_bands,
                "battery_short": priced.name in discharge_bands,
            }
        )
    return rows


@router.get("/history")
def history(
    request: Request,
    store: _ReadStore,
    start: datetime,
    end: datetime,
    metrics: str,
    width: int = Query(default=1000, ge=1, le=10000),
    device: str | None = None,
) -> dict[str, Any]:
    """One inverter's metrics over a range, at a resolution that suits the chart.

    ``device`` defaults to the configured inverter.
    """
    _check_range(start, end)
    names = _parse_metrics(metrics, _INVERTER_NAMES, "inverter")
    cadence = _cadence_seconds(request.app.state.config.poll_interval)
    tier = select_tier(end - start, width_px=width, cadence_seconds=cadence)
    # A mistyped year reaches ``int(start.timestamp())`` in the store and raises
    # OverflowError (or ValueError on some platforms).  Neither of these endpoints
    # goes through ``read_energy`` or ``band_intervals``, so they need their own
    # guard.  The fix at d7c3dcb missed them because the audit swept only the
    # endpoints the calendar-walk wrappers already covered.
    with _inside_the_calendar():
        rows = store.query(names, start, end, tier=tier, device=_device(device))
    return {"tier": tier, "count": len(rows), "points": [_isoformat_row(r) for r in rows]}


@router.get("/battery/history")
def battery_history(
    request: Request,
    store: _ReadStore,
    start: datetime,
    end: datetime,
    metrics: str = "soc_pct",
    width: int = Query(default=1000, ge=1, le=10000),
    serial: str | None = None,
    device: str | None = None,
) -> dict[str, Any]:
    """One inverter's per-module battery readings over a range, keyed by serial.

    Modules are identified by serial rather than slot, so a bank that rotates
    modules through the inverter's register slots neither splits one
    battery into two series nor merges two into one. ``device`` picks the
    inverter whose bank is being asked about and defaults to the configured
    one; a serial is unique within a device, not across them.
    """
    _check_range(start, end)
    names = _parse_metrics(metrics, set(module_metric_columns()), "module")
    cadence = _cadence_seconds(request.app.state.config.poll_interval)
    tier = select_tier(end - start, width_px=width, cadence_seconds=cadence, module=True)
    with _inside_the_calendar():
        rows = store.query_modules(
            names, start, end, tier=tier, serial=serial, device=_device(device)
        )
    return {"tier": tier, "count": len(rows), "points": [_isoformat_row(r) for r in rows]}


@router.get("/energy")
def energy(
    request: Request,
    store: _ReadStore,
    start: datetime,
    end: datetime,
    period: Period = "day",
    tz: str | None = None,
    priced: bool = False,
) -> dict[str, Any]:
    """Energy per calendar day or month, in kWh, over the owner's own calendar.

    Read off the inverter's lifetime counters rather than integrated from
    stored power, so a period containing a collection outage still totals what
    actually happened. Each bucket says whether it is whole: the one in
    progress is not, nor is the first one if collection started partway into
    it, and a bucket that reads low for that reason must not be presented as a
    quiet day.

    ``tz`` is an IANA zone name and decides where midnight falls, but only
    where the installation has not stated its own: ``site.timezone`` wins over
    it, and the machine's zone answers when neither is set. Naive timestamps in
    ``start`` and ``end`` are read in whichever zone that resolves to rather
    than the server's, since otherwise the answer depends on where the service
    happens to be installed. The zone actually used comes back as ``timezone``,
    so a page never has to assume the one it asked for is the one it got — and
    a page that means the owner's own midnight should send ``start`` naive and
    let this read it, rather than working out an instant from a clock that may
    be nowhere near the inverter. A bucket nothing was recorded for is left out
    of the reply rather than returned as zero.

    ``priced`` asks for what each bucket cost as well as what it used. The
    money rides on the bucket it belongs to rather than arriving from a second
    endpoint, because a page holding two lists of buckets has to line them up
    by date to draw one row — and lining up two lists that each omit what they
    had nothing to report for is the mistake that once billed seven off-peak
    kilowatt-hours at the peak rate. It also means one read of the counters
    answers both questions instead of two reads that have to agree.

    It also carries ``totals``: the whole span priced in one pass, which is not
    the sum of the buckets and must not be replaced by one. The per-bucket
    figures are rounded to the cent they are displayed at, and adding thirty-one
    of those together turns a $15.00 connection charge into $14.88. A page that
    added its own column up would drift from the month it is a view of.

    With no tariff entered the reply carries no money at all: no currency, no
    cost on any bucket, no totals, and ``configured`` false. Not zero — an
    install that has never entered a tariff shows its energy and says so.
    """
    try:
        zone = _request_zone(store, tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start, end = with_zone(start, zone), with_zone(end, zone)
    _check_range(start, end)

    with _inside_the_calendar():
        read = read_energy(store, start, end, period=period, zone=zone)

    tariff, stored = (None, "")
    if priced:
        values = SettingsStore(store).all()
        tariff, stored = load_tariff(values), str(values.get(SETTING_BANDS) or "").strip()

    # Both keyed by the bucket's own opening edge, so money reaches the bucket
    # it belongs to by identity rather than by counting: the reply leaves out a
    # bucket the counters said nothing about, and a cost matched by position
    # would then slide onto that bucket's neighbour.
    money: dict[datetime, CostResult | None] = {}
    splits: dict[datetime, PeriodEnergy] = {}
    # Every calendar bucket the table spans, including the ones nothing was
    # recorded for. The connection charge is drawn from these rather than from
    # the buckets that could be priced; see _period_total.
    spanned: list[PeriodEnergy] = []
    if tariff is not None:
        # ``end`` goes with it so the bucket in progress is priced over the
        # part of itself that has happened. A calendar month runs to the first
        # of the next, and pricing hours still in the future leaves the whole
        # month unmeasured and shows a dash beside a month that plainly used
        # electricity.
        with _inside_the_calendar():
            priced_buckets = bucket_energy(tariff, read.rows, read.edges, zone, until=end)
        for index, split in enumerate(priced_buckets):
            splits[read.edges[index]] = split
            money[read.edges[index]] = price_period(
                tariff, split, fixed_charge=_bucket_fixed(tariff, period, split)
            )
        if read.buckets:
            first, last = read.buckets[0].start, read.buckets[-1].start
            spanned = [
                splits[edge] for edge in read.edges if first <= edge <= last and edge in splits
            ]

    payload: dict[str, Any] = {
        "period": period,
        "timezone": str(zone),
        "buckets": [
            {
                "start": bucket.start.isoformat(),
                "end": bucket.end.isoformat(),
                "complete": bucket.complete,
                **bucket.totals,
                **_bucket_money(tariff, money.get(bucket.start), splits.get(bucket.start)),
            }
            for bucket in read.buckets
        ],
    }
    if priced:
        payload |= {
            "currency": tariff.currency if tariff else None,
            "configured": tariff is not None,
            # Nothing entered and something entered that cannot be read call
            # for opposite actions, and conflating them tells somebody staring
            # at the tariff they just typed that they have not entered one.
            "unreadable": tariff is None and bool(stored),
            # Over the buckets above and in their order, so the footer of a
            # table totals the rows of that same table and nothing else.
            "totals": _period_total(
                tariff,
                [
                    (splits[bucket.start], money.get(bucket.start))
                    for bucket in read.buckets
                    if bucket.start in splits
                ],
                spanned,
                period,
            ),
        }
    return payload


def _bucket_fixed(tariff: Tariff, period: Period, span: PeriodEnergy) -> float:
    """One bucket's share of the monthly connection charge, unrounded.

    A month bucket is a billing month and owes the whole charge; a day inside
    one owes a day's share. Decided here rather than at each call site because
    the per-bucket figure and the period total have to agree on it — a total
    that apportioned differently from the rows above it would be a footer that
    contradicts its own column.

    Unrounded on purpose. It is one input to a figure rounded once, and the
    whole reason this endpoint reports a total at all is that rounding first
    and adding afterwards turns thirty-one shares of $15.00 into $14.88.
    """
    if period == "month":
        return tariff.fixed_monthly
    return apportion_fixed(tariff.fixed_monthly, span.start, span.end)


def _merge_bands(
    parts: Iterable[Mapping[str, float | None] | None],
    partial: bool = False,
) -> dict[str, float | None]:
    """Add each band's kilowatt-hours across several buckets.

    Without ``partial``, a band one bucket could not measure makes that band
    unknown for the whole run rather than the sum of the buckets that did
    report it — a missing reading rendered as a smaller number, at the point
    where it turns into money. With it, the reported buckets sum and the band
    is None only when none of them reported: the caller carries a merged
    shortfall saying what the sum is missing, which is what lets the History
    footer show a labelled total instead of a dash over a column of flagged
    numbers (#23). A band simply absent from a bucket contributes nothing
    either way: the day before the season turns never entered the peak
    window, so it has nothing to say rather than something nobody watched.
    """
    out: dict[str, float | None] = {}
    for part in parts:
        for name, kwh in (part or {}).items():
            if partial:
                if kwh is None:
                    out.setdefault(name, None)
                else:
                    running = out.get(name)
                    out[name] = kwh if running is None else running + kwh
            else:
                running = out.get(name, 0.0)
                out[name] = None if kwh is None or running is None else running + kwh
    return out


def _price_together(
    tariff: Tariff,
    spans: Sequence[PeriodEnergy],
    fixed_charge: float,
    shortfall: Mapping[str, EnergyShortfall] | None,
) -> CostResult | None:
    """Price a run of buckets as one period rather than adding up their costs.

    ``spans`` arrives in calendar order, so the combined period runs from the
    first bucket's start to the last one's end without comparing two datetimes
    that share a zone — a comparison Python answers off the wall clock, which
    is the trap every other duration in this project goes out of its way to
    avoid.

    ``shortfall`` is the merge of the spans' own accounting, handed in rather
    than derived here because the caller reports it beside the total. It has
    to ride on the combined period: without it the pricing falls back to
    poisoning, and the footer dashes while every row above it shows a flagged
    number — a regression no clean-data test would catch.

    The connection charge is handed in rather than derived from ``spans``,
    because ``spans`` is only the part of the period that could be priced and
    the charge does not depend on that. See ``_period_total``.
    """
    if not spans:
        return None
    partial = shortfall is not None
    return price_period(
        tariff,
        PeriodEnergy(
            start=spans[0].start,
            end=spans[-1].end,
            grid_import_kwh=_merge_bands((span.grid_import_kwh for span in spans), partial),
            load_kwh=_merge_bands((span.load_kwh for span in spans), partial) or None,
            battery_discharge_kwh=_merge_bands(
                (span.battery_discharge_kwh for span in spans), partial
            )
            or None,
            shortfall=shortfall,
        ),
        fixed_charge=fixed_charge,
    )


def _period_total(
    tariff: Tariff | None,
    buckets: Sequence[tuple[PeriodEnergy, CostResult | None]],
    spanned: Sequence[PeriodEnergy],
    period: Period,
) -> dict[str, Any]:
    """What the whole span cost, priced once rather than added up from the rows.

    The History page's footer used to sum the column above it, and that column
    has already been rounded to the cent a reader can see. Thirty-one shares of
    a connection charge are $0.4838 apiece, shown as $0.48 and adding to $14.88
    against the $15.00 the supplier bills. Whether the roundings cancel is pure
    luck in the divisor — thirty shares of $15.00 come to exactly $15.00, and
    twenty-eight come to $15.12 — which is the argument for not summing
    displayed figures at all rather than an argument about magnitude. So the
    buckets' band energy is added up
    unrounded and priced in a single pass, exactly as the monthly row is, and
    the page draws what comes back instead of deriving money a second time.

    Only the buckets the service could price contribute *energy*. A month with
    a day nobody measured did not use the energy of the days that were, and
    this says so by covering fewer rows rather than by treating the hole as
    free; a span where nothing could be priced has no total at all, which is a
    dash and never a zero.

    The connection charge is the exception, and it is drawn from ``spanned`` —
    every calendar bucket between the first row and the last, whether anything
    was recorded for it or not. It is owed for being connected, not for being
    observed, so losing a day of telemetry does not reduce it: a July with a
    hole on the fifteenth still owes the whole $15.00, where summing the thirty
    buckets that could be priced owed $14.52 and quietly credited the owner for
    the outage. Calendar coverage is still what shares it out, so a query
    covering half of July owes half — the charge is reduced by asking about
    less of the month, never by failing to watch it.

    Cost and savings are totalled over their own rows, because they can be
    knowable for different ones. A counter reset takes one column backwards and
    not the other, leaving a day whose import is readable and whose house load
    is not — that day has a cost and no statable saving, and dropping it from
    both totals would understate the bill it is part of.
    """
    if tariff is None:
        return {}
    fixed = sum(_bucket_fixed(tariff, period, span) for span in spanned)
    costed = [energy for energy, result in buckets if result and result.cost is not None]
    saving = [energy for energy, result in buckets if result and result.savings is not None]
    whole = _price_together(tariff, costed, fixed, merge_shortfalls(costed))
    against = _price_together(tariff, saving, fixed, merge_shortfalls(saving))
    # Disclosure is merged over every bucket that carries accounting, not just
    # the rows that priced. A day whose import all fell inside a gap is a dash
    # in its row and absent from the money above — but its counted shortfall
    # is real, and a footer that merged only the priced rows read clean over a
    # month verifiably missing energy. The pricing basis stays the priced
    # rows; only what the footer *says about itself* widens.
    disclosure = merge_shortfalls([energy for energy, _ in buckets if energy.shortfall is not None])

    def entry_short(key: str) -> bool:
        entry = (disclosure or {}).get(key)
        return entry is not None and entry.short

    return {
        "cost": whole.cost if whole else None,
        "energy_cost": whole.energy_cost if whole else None,
        "fixed_charge": whole.fixed_charge if whole else None,
        "adjustment": whole.adjustment if whole else None,
        # Over a run of buckets this is usually "unknown" the moment the run
        # crosses a month the supplier changed the factors in, which is the
        # honest answer: the kilowatt-hours are not split by month, so no
        # single rider can be charged on them.
        "adjustment_status": whole.adjustment_status if whole else None,
        "saved": against.savings if against else None,
        "no_solar_cost": against.no_solar_cost if against else None,
        # The footer says what its rows say — including the dashed ones,
        # whose accounting is in the disclosure merge even though their money
        # is in no total. A null figure stays unflagged, as on the rows: the
        # dash is its own qualification, and the gate is on the figure
        # itself, since a run of priced months can still merge to a total
        # nothing can state — differing riders do it — and that dash must not
        # wear a dot.
        "cost_short": whole is not None
        and whole.cost is not None
        and (whole.cost_is_short or entry_short("grid_import")),
        "saved_short": against is not None
        and against.savings is not None
        and (against.savings_is_short or entry_short("grid_import") or entry_short("load")),
        "shortfall": _shortfall_payload(disclosure),
    }


def _shortfall_payload(
    shortfall: Mapping[str, EnergyShortfall] | None,
) -> dict[str, dict[str, Any]] | None:
    """The per-counter accounting in wire form, or None where nobody computed it.

    Forwarded whole rather than reduced to a boolean, because the brief's rule
    is a label saying what the figure covers — a dot that only says "short"
    cannot say 12.4 kWh, and the page must not derive the number itself.
    """
    if shortfall is None:
        return None
    return {name: asdict(entry) for name, entry in shortfall.items()}


def _bucket_money(
    tariff: Tariff | None, result: CostResult | None, split: PeriodEnergy | None
) -> dict[str, Any]:
    """What one bucket cost, or nothing at all when there is no tariff.

    The keys are absent rather than null without a tariff, so a page cannot
    draw a column of dashes over an install that simply has no rates entered.
    With a tariff they are present and may be null, which means something else
    entirely: those bands happened and nobody measured all of them.

    ``fixed_charge`` is this bucket's share of the monthly connection charge,
    apportioned by how much of that month it covers. It is inside ``cost``
    because it is money owed for that day, and it is broken out because a
    reader adding up a column of days should be able to see that the standing
    charge is in there once per month rather than once per row.

    ``saved`` is what the same house load would have cost bought entirely from
    the grid, less what the grid actually cost. It excludes the connection
    charge, which is payable whatever the roof does — counting it against the
    array would make the system look worse the more it saved. It is null rather
    than zero whenever either side of that subtraction is unknown, because a
    saving computed from a half-measured day is a number nobody can check.

    ``adjustment`` is the PCRF and SCRF riders, already inside ``cost``, and
    ``adjustment_status`` says whether it was charged at all. A null adjustment
    with a status of "unknown" is a bucket priced at the base rate because its
    month has no factors recorded — the page has to say so, since a bill
    quoted without a rider is not the bill that arrives.
    """
    if tariff is None:
        return {}
    if result is None:
        return {
            "cost": None,
            "energy_cost": None,
            "fixed_charge": None,
            "adjustment": None,
            "adjustment_status": None,
            "saved": None,
            "no_solar_cost": None,
            # A dash needs no flag — it is already not a figure — but the
            # accounting still says why there is nothing to show.
            "cost_short": False,
            "saved_short": False,
            "shortfall": _shortfall_payload(split.shortfall if split else None),
        }
    return {
        "cost": result.cost,
        "energy_cost": result.energy_cost,
        "fixed_charge": result.fixed_charge,
        "adjustment": result.adjustment,
        "adjustment_status": result.adjustment_status,
        "saved": result.savings,
        "no_solar_cost": result.no_solar_cost,
        # Which figures must not be read as whole, and the per-counter
        # accounting the cell titles and the chart hover word themselves
        # from. The reverted finding: a day can be complete in energy — the
        # counters span a mid-day gap exactly — while its money is short by
        # every peak hour inside that gap. These are the money's own flags.
        "cost_short": result.cost_is_short,
        "saved_short": result.savings_is_short,
        "shortfall": _shortfall_payload(split.shortfall if split else None),
    }


@router.get("/bands")
def bands(
    request: Request,
    store: _ReadStore,
    start: datetime,
    end: datetime,
    tz: str | None = None,
) -> dict[str, Any]:
    """Return the ordered tariff-band windows covering a time range.

    The chart shades its background by band so grid import can be read against
    what it cost. The windows are resolved here rather than in the page for the
    same reason the pricing is: the browser once had its own tariff parser and
    the two disagreed within a day, charging a January evening at the summer
    peak rate. A page that draws bands it worked out itself is that bug with a
    chart instead of a number.

    Absent data is not zero: with no tariff configured the windows are absent,
    not one window covering everything.
    """
    settings = SettingsStore(store)
    tariff = load_tariff(settings.all())
    # A zone this tz database does not know is refused, as ``/api/energy``
    # refuses it, rather than quietly falling back the way ``/api/status`` does.
    # The difference is what the answer is for: a status banner cut on the wrong
    # calendar is cosmetic, but a band window is a claim about which hours were
    # expensive, and one cut on a zone the caller did not ask for is wrong in a
    # way nothing on the page could reveal.
    try:
        zone = _request_zone(store, tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if tariff is None:
        return {
            "configured": False,
            "timezone": str(zone),
            "windows": [],
        }

    start, end = with_zone(start, zone), with_zone(end, zone)
    _check_range(start, end)

    # A range longer than the tariff walk will scan is the caller's mistake, not
    # the server's. ``band_intervals`` says so with a ValueError, which would
    # otherwise leave FastAPI to answer 500 — telling somebody who asked for five
    # years that the service is broken. ``/api/costs`` converts the same error
    # for the same reason, and a range that runs off the calendar entirely is
    # the same mistake one step further out.
    with _inside_the_calendar():
        intervals = band_intervals(tariff, start, end, zone)

    windows = [
        {
            "start": interval.start.isoformat(),
            "end": interval.end.isoformat(),
            "band": interval.band,
            "price_per_kwh": interval.price_per_kwh,
        }
        for interval in intervals
    ]

    return {
        "configured": True,
        "timezone": str(zone),
        "windows": windows,
    }


@router.get("/forecast")
def forecast(
    request: Request,
    store: _ReadStore,
    tz: str | None = None,
) -> dict[str, Any]:
    """The day's prediction, what the array has actually made, and how often it refreshes.

    One prediction curve, not two. The page used to draw a frozen morning
    baseline behind the live one and measure the day against it; two prediction
    curves on one chart read as clutter rather than as insight, so the baseline
    and the ahead/behind figure went with it. The gap between the prediction and
    the solid actual line is the same signal, without a number whose reference
    is not on screen.

    ``refresh_seconds`` is the weather poller's own interval, served rather than
    written into the page, because it is a setting an owner can change and a
    caption that hard-codes fifteen minutes would be wrong the moment they did.

    Actuals come from the hourly rollup — mean pv_total_power_w per hour, so each
    hour's energy is that mean multiplied by one hour.

    With no forecast rows for today every data field is null or empty: the page
    shows nothing, not zeros.
    """
    try:
        zone = _request_zone(store, tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    now = datetime.now(tz=UTC)
    local_now = now.astimezone(zone)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # The instant of local midnight in UTC, and the instant of the next local
    # midnight. Adding a day in local time is the only way to land on the right
    # UTC instant when the day is not 24 hours.
    day_start = local_midnight.astimezone(UTC)
    day_end = (local_midnight + timedelta(days=1)).astimezone(UTC)

    predicted = store.forecast_day(day_start, day_end)
    refresh_seconds = _weather_interval(store)

    if not predicted:
        return {
            "configured": False,
            "day": {"start": day_start.isoformat(), "end": day_end.isoformat()},
            "now": now.isoformat(),
            "prediction": [],
            "actual": [],
            "expected_today_kwh": None,
            "actual_so_far_kwh": None,
            "refresh_seconds": refresh_seconds,
        }

    # Hourly mean pv_total_power_w for the day so far. Each hour's energy is the
    # mean wattage multiplied by one hour, so the values are already in Wh.
    actual_rows: list[dict[str, object]] = store.query(
        ["pv_total_power_w"], day_start, day_end, tier="hourly"
    )
    actual: list[dict[str, object]] = [
        {
            "hour": cast(datetime, row["timestamp"]).isoformat(),
            "mean_w": cast(float, row["pv_total_power_w"]),
        }
        for row in actual_rows
        if not row.get("error") and row.get("pv_total_power_w") is not None
    ]

    prediction = [
        {
            "hour": cast(datetime, e["hour"]).isoformat(),
            "expected_w": cast(float, e["expected_w"]),
        }
        for e in predicted
    ]

    # Expected today: the prediction summed in Wh, divided by 1000 for kWh.
    expected_today_kwh: float | None = (
        sum(cast(float, e["expected_w"]) for e in predicted) / 1000.0 if predicted else None
    )

    # Actual production so far. Null when no hour has a reading yet — a dash,
    # never zero, because zero means the array produced nothing and that is a
    # different statement from "nobody has looked yet".
    actual_so_far_kwh = _energy_so_far_kwh(
        [
            (cast(datetime, row["timestamp"]), cast(float, row["pv_total_power_w"]))
            for row in actual_rows
            if not row.get("error") and row.get("pv_total_power_w") is not None
        ],
        now,
    )

    return {
        "configured": True,
        "day": {"start": day_start.isoformat(), "end": day_end.isoformat()},
        "now": now.isoformat(),
        "prediction": prediction,
        "actual": actual,
        "expected_today_kwh": round(expected_today_kwh, 3)
        if expected_today_kwh is not None
        else None,
        "actual_so_far_kwh": round(actual_so_far_kwh, 3) if actual_so_far_kwh is not None else None,
        "refresh_seconds": refresh_seconds,
    }


@router.get("/setup", dependencies=[Depends(_require_write)])
async def setup(request: Request) -> dict[str, Any]:
    """Everything the setup wizard renders, from one place.

    Served on the loop rather than the threadpool: the payload is registry
    metadata, a directory listing and the already-loaded config — no tier
    scan anywhere near it. The connection values it echoes are redacted the
    same way the settings API redacts them, because a serial number is an
    installation secret.

    It carries the connection editor values, so with a password set it
    requires a session like every other protected endpoint. The wall display
    never requests it, and its 401 is what gives the settings page a reason
    to prompt. First-run setup has no database and therefore no password, so
    the guard passes through there.
    """
    return describe_setup(request.app.state.config)


# The most days one backfill request may cover. Two years is more archive than
# any installation here has history for, and the ceiling is what stops a typo
# in a date from asking the free service for a decade in one go.
_BACKFILL_MAX_DAYS = 760


class BackfillRequest(BaseModel):
    """A date range to recover past conditions for."""

    start: str
    end: str


@router.post("/efficiency/backfill", dependencies=[Depends(_require_write)])
def backfill(request: Request, store: _ReadStore, body: BackfillRequest) -> dict[str, Any]:
    """Fetch past hourly conditions into the store, a day at a time.

    Owner-triggered rather than implicit: the archive is a few hundred
    requests for a year of history, and a page load must never start that.
    Resumable by construction — rows are keyed by timestamp, so re-running a
    range rewrites the same hours rather than duplicating them, and a failure
    reports the last day that landed so the next run can carry on from there.

    The appends run on FastAPI's threadpool while the collector appends on
    the event loop, so they take ``write_connection`` — a store bound to its
    own connection — rather than the primary one, or the read view above,
    whose contract forbids writers. On one connection ``with conn:`` is
    transaction state rather than a lock, and an append that failed here
    would roll the collector's in-flight poll back with it.
    """
    settings = SettingsStore(store)
    latitude = settings.get(SETTING_LATITUDE)
    longitude = settings.get(SETTING_LONGITUDE)
    if not isinstance(latitude, float) or not isinstance(longitude, float):
        raise HTTPException(
            status_code=400,
            detail="backfill needs a location; set latitude and longitude first",
        )
    try:
        start = date.fromisoformat(body.start)
        end = date.fromisoformat(body.end)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"dates must be YYYY-MM-DD: {exc}") from exc
    if end < start:
        raise HTTPException(status_code=400, detail="end must not precede start")
    if (end - start).days + 1 > _BACKFILL_MAX_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"range is longer than {_BACKFILL_MAX_DAYS} days; ask for less at a time",
        )

    written = 0
    days = 0
    last_day: str | None = None
    current = start
    with request.app.state.store.write_connection() as writer:
        while current <= end:
            samples = fetch_archive_hours(latitude, longitude, current, current)
            if samples is None:
                # The fetch failed. Stop here and report where, rather than
                # marching on and leaving holes nobody can see.
                break
            if not samples:
                # The archive simply had nothing for this day. Step over it.
                logger.info("archive holds no readings for %s; skipping", current)
                days += 1
                last_day = current.isoformat()
                current += timedelta(days=1)
                continue
            for sample in samples:
                try:
                    writer.append(sample)
                except sqlite3.Error as exc:
                    logger.warning("backfill could not store an hour: %s", exc)
                    break
                written += 1
            days += 1
            last_day = current.isoformat()
            current += timedelta(days=1)
    return {"days": days, "hours_written": written, "last_day": last_day}


@router.get("/panels")
def panels(request: Request, store: _ReadStore) -> dict[str, Any]:
    """The parsed array and bank, for anything that must not read the grammar.

    One shape, defaults resolved and named, so the future efficiency engine
    and the settings editor read identical truth — the page composes the
    grammar but never parses it, which is the tariff's own lesson applied.
    """
    settings = SettingsStore(store)
    text = settings.get("panels.strings")
    strings = parse_strings(text) if isinstance(text, str) else ()
    battery = {
        key.split(".", 1)[1]: settings.get(key)
        for key in (
            "battery.chemistry",
            "battery.count",
            "battery.capacity_kwh_each",
            "battery.round_trip_pct",
            "battery.min_soc_pct",
            "battery.max_charge_a",
            "battery.max_discharge_a",
            "battery.heater_w",
            "battery.heater_on_c",
            "battery.heater_off_c",
            "battery.idle_draw_w",
            "battery.installed",
        )
    }
    from arraysense.panels import PANEL_CATALOGUE

    declared = getattr(request.app.state.service.source, "capabilities", None)
    return {
        "strings": [{**asdict(s), "defaulted": sorted(s.defaulted)} for s in strings],
        "battery": battery,
        "declared_mppts": declared.pv_strings if declared is not None else None,
        "catalogue": [
            {
                "name": e.name,
                "description": e.description,
                "vmp": e.vmp,
                "voc": e.voc,
                "temp_coeff": e.temp_coeff,
                "noct": e.noct,
                "degradation": e.degradation,
                "citation": e.citation,
            }
            for e in PANEL_CATALOGUE
        ],
    }


# Meter accuracy the spec sheets quote, used where no site-specific figure has
# been measured. 3 % is the typical tolerance for a revenue-grade meter, and a
# system that has not entered its own is assumed to be at least that accurate.
_METER_TOLERANCE_PCT = 3.0


def _daily_range(
    day_start: datetime,
    period: str,
) -> list[tuple[datetime, datetime]]:
    """Return (start, end) pairs for each day in the period.

    ``day_start`` is the opening edge of the first day, already zone-aware.
    The result edges carry the same timezone.
    """
    if period == "day":
        return [(day_start, day_start + timedelta(days=1))]
    if period == "week":
        return [
            (day_start + timedelta(days=i), day_start + timedelta(days=i + 1)) for i in range(7)
        ]
    # Month: the calendar month that contains day_start, from its first day
    # through the first day of the following month.
    if period == "month":
        try:
            first_of_month = day_start.replace(day=1)
            if first_of_month.month == 12:
                next_month = first_of_month.replace(year=first_of_month.year + 1, month=1, day=1)
            else:
                next_month = first_of_month.replace(month=first_of_month.month + 1, day=1)
            days_count = (next_month - first_of_month).days
            return [
                (first_of_month + timedelta(days=i), first_of_month + timedelta(days=i + 1))
                for i in range(days_count)
            ]
        except ValueError:
            # ``replace(year=10000)`` raises ValueError rather than OverflowError
            # because the constructor rejects the year before any arithmetic
            # happens.  Convert it so the single ``_inside_the_calendar`` guard
            # catches every shape the same mistake can take.
            raise OverflowError("date value out of range") from None
    return []


def _hourly_efficiency_for_range(
    store: SqliteStore,
    settings: SettingsStore,
    start: datetime,
    end: datetime,
    strings: tuple[StringSpec, ...],
    zone: ZoneInfo,
) -> list[dict[str, Any]]:
    """Render the engine's hour-by-hour scoring for the page.

    Shape only. The arithmetic belongs to ``efficiency.compute_hours`` and is
    not repeated here -- an endpoint that modelled expected production a second
    time would drift from the summaries it sits beside, which is exactly how
    the Costs page came to price a January evening at the summer peak rate.
    """
    return [
        {
            "hour": hour.hour.astimezone(zone).isoformat(),
            "expected_kwh": round(hour.expected_kwh, 4),
            "actual_kwh": round(hour.actual_kwh, 4),
            "curtailed_kwh": round(hour.curtailed_kwh, 4),
            "unexplained_kwh": round(hour.unexplained_kwh, 4),
        }
        for hour in compute_hours(store, settings, start, end, strings)
    ]


_NO_BASELINE: dict[str, Any] = {"window_start": None, "window_end": None, "samples": None}


def _baseline_info(
    baselines: Mapping[int, StringBaseline | None],
    range_start: datetime,
    range_end: datetime,
) -> dict[str, Any]:
    """What the curtailment rule was actually calibrated against, or nothing.

    This used to report a window whenever any daily row existed, reasoning that
    a day whose baseline could not be fitted returns no rows. It does not:
    ``baseline_for`` returns None for a string with fewer than three producing
    hours, ``compute_hours`` then disables the signature test for that string
    and scores the day anyway, and the page said "Calibrated from <date>" for a
    system where nothing had been fitted and where curtailment could never be
    booked. On the reference installation that is the state every morning until
    the third producing hour lands.

    The window is the range the fit ran over rather than the first day of the
    period, which was never when anything was fitted either, and ``samples`` is
    the pairs behind the thinnest string's fit — the evidence the claim rests
    on, which the page had no way to show while it was always null.
    """
    fitted = [b for b in baselines.values() if b is not None]
    if not fitted or len(fitted) < len(baselines):
        # One unfitted string is enough to withhold the claim: it is exactly the
        # string whose curtailment cannot be seen, and a window covering it
        # would say the opposite.
        return dict(_NO_BASELINE)
    return {
        "window_start": range_start.isoformat(),
        "window_end": range_end.isoformat(),
        "samples": min(b.samples for b in fitted),
    }


def _string_kwp(s: StringSpec) -> float:
    """Nameplate kilowatts-peak for one string."""
    return s.panels * s.watts / 1000.0


@router.get("/efficiency")
def efficiency(
    request: Request,
    store: _ReadStore,
    start: str,
    period: str = Query(default="day"),
    tz: str | None = None,
) -> dict[str, Any]:
    """How the array performed against what the sun offered.

    The answer covers a day, a week or a calendar month.  Each day is scored
    by the efficiency engine and the results are aggregated: the summary is
    the total across all days, the waterfall reconciles expected to actual
    through unexplained and curtailed, and the per-string breakdown lets an
    underperformer be localised wherever the inverter exposes an independent
    MPPT reading. Strings sharing one MPPT are one group because the hardware
    cannot distinguish them.

    ``period=day`` also returns an hourly breakdown computed live from the
    stored irradiance and inverter readings rather than from any stored
    summary, because the stored summary nets across the day and an hour that
    genuinely lost energy can be cancelled by others the model under-called.

    With no array configured every figure is null or empty — never zero,
    because zero expected and zero actual is a claim that the array was
    meant to make nothing and made nothing.
    """
    if period not in ("day", "week", "month"):
        raise HTTPException(
            status_code=400,
            detail=f"period must be day, week or month, not {period!r}",
        )

    try:
        zone = _request_zone(store, tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        start_date = date.fromisoformat(start)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"start must be YYYY-MM-DD: {exc}") from exc

    day_start = datetime.combine(start_date, datetime.min.time(), tzinfo=zone)
    with _inside_the_calendar():
        days = _daily_range(day_start, period)
    if not days:
        raise HTTPException(
            status_code=400, detail=f"could not compute range for period={period!r}"
        )

    range_start = days[0][0]
    range_end = days[-1][1]

    settings = SettingsStore(store)
    text = settings.get(PANELS_STRINGS_KEY)
    strings = parse_strings(text) if isinstance(text, str) and text.strip() else ()

    now = datetime.now(tz=UTC)

    if not strings:
        return {
            "configured": False,
            "period": period,
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "now": now.isoformat(),
            "summary": None,
            "waterfall": [],
            "strings": [],
            "hours": None,
            "days": [],
            "worst_hour": None,
            "baseline": dict(_NO_BASELINE),
            # Present even here. A key that appears only sometimes is a key
            # every caller has to guard, and the one that forgets reads a
            # missing array as a missing answer.
            "tilt_benefit": None,
        }

    config_version_raw = settings.get(CONFIG_VERSION_KEY)
    config_version = config_version_raw if isinstance(config_version_raw, int) else 0
    valid_from_raw = settings.get(CONFIG_VALID_FROM_KEY)
    valid_from = valid_from_raw if isinstance(valid_from_raw, int) else 0

    # Collect daily rows: try stored first, compute live for missing days.
    daily_rows: list[EfficiencyRow] = []
    for ds, de in days:
        stored = store.read_efficiency_days(ds, de)
        # A stored day is only usable if it was scored against the array as it
        # is described now. The maintenance pass rescores today and yesterday,
        # but nothing revisits last month, so after the owner corrects a panel
        # count or moves the site every older day would keep a score taken
        # against an array that no longer exists -- and be served without a
        # word to say so. Recomputing is the honest answer and is cheap.
        if rows_are_current(stored, config_version, valid_from):
            daily_rows.extend(stored)
        else:
            daily_rows.extend(compute_day(store, settings, ds, de, strings, config_version))

    if not daily_rows:
        # No rows at all — likely no data for this range.
        return {
            "configured": True,
            "period": period,
            "start": range_start.isoformat(),
            "end": range_end.isoformat(),
            "now": now.isoformat(),
            "summary": None,
            "waterfall": [],
            "strings": [],
            "hours": None,
            "days": [],
            "worst_hour": None,
            "baseline": dict(_NO_BASELINE),
            # Present even here. A key that appears only sometimes is a key
            # every caller has to guard, and the one that forgets reads a
            # missing array as a missing answer.
            "tilt_benefit": None,
        }

    # Aggregate: group by string_name.  The total row has string_name == "".
    by_string: dict[str, list[EfficiencyRow]] = {}
    for r in daily_rows:
        by_string.setdefault(r.string_name, []).append(r)

    groups = mppt_groups(strings)
    groups_by_name = {group.label: group for group in groups}

    # The array's yield is its output over the nameplate that produced it, and a
    # string the inverter never reported produced no part of the numerator — so
    # it must be no part of the denominator either. Dividing two strings' output
    # by three strings' kWp understates the array by a third and says nothing:
    # the reference installation served 3.621 kWh/kWp for a day PV3 was never
    # read, against 14.04 kWp of which only 9.36 was measured.
    scored_names = {r.string_name for r in daily_rows if r.string_name}
    described_names = {s.name for s in strings}
    total_kwp = sum(
        _string_kwp(member)
        for group in groups
        if group.label in scored_names
        for member in group.members
    )

    # How much of the period the total was actually totalled over. A day the
    # engine could not model returns no rows at all, so it simply vanishes from
    # the aggregate: the week of a five-day outage was reported as the week's
    # figures, with a specific yield understated in exact proportion to the days
    # missing and nothing in the summary to say so. Only days that have begun
    # count as owed — a week asked for on its Tuesday is not missing Thursday.
    days_expected = sum(1 for ds, _ in days if ds <= now)
    days_scored = len({r.day for r in by_string.get("", [])})

    # A string read on some days and silent on others produces rows for only the
    # days it was read.  Counting it as "scored" because it appears at all is how
    # a week of four West-string days plus seven South-string days reported
    # itself complete, with a specific yield understated in exact proportion to
    # the days West was absent.  The total is only complete when every described
    # string was scored on every expected day.
    string_days: dict[str, int] = {}
    for group in groups:
        string_days[group.label] = len({r.day for r in by_string.get(group.label, [])})
    any_string_incomplete = any(d < days_expected for d in string_days.values())

    def _summarise(name: str, rows: list[EfficiencyRow]) -> dict[str, Any]:
        expected = sum(r.expected_kwh for r in rows)
        actual = sum(r.actual_kwh for r in rows)
        curtailed = sum(r.curtailed_kwh for r in rows)
        # Derived from the totals, never summed from the days. Each day's own
        # figure is clamped at zero, so adding them counts every day that fell
        # short while ignoring every day that ran ahead: a week of one 5 kWh
        # shortfall and one 5 kWh surplus would report 5 kWh unexplained beside
        # an expected and an actual that are equal, and the waterfall would
        # visibly fail to add up in front of the owner.
        residual = expected - curtailed - actual
        unexplained = max(0.0, residual)
        surplus = max(0.0, -residual)
        denom = expected - curtailed
        pr: float | None = actual / denom if denom > 0.0 else None
        # Per-string kWp for per-string yield; total for total.
        kwp = (
            total_kwp
            if name == ""
            else sum(_string_kwp(member) for member in groups_by_name[name].members)
        )
        sy: float | None = actual / kwp if kwp > 0.0 else None
        # The total is incomplete when any string is incomplete.  A per-string
        # row is incomplete when its own days fall short of what the period owes,
        # even if the string appeared on SOME days — four days scored of seven is
        # a partial figure presented as complete without this check.
        my_days = string_days.get(name, 0) if name else days_scored
        return {
            "expected_kwh": round(expected, 3),
            "actual_kwh": round(actual, 3),
            "curtailed_kwh": round(curtailed, 3),
            "unexplained_kwh": round(unexplained, 3),
            "unmodelled_gain_kwh": round(surplus, 3),
            "pr": round(pr, 4) if pr is not None else None,
            "specific_yield": round(sy, 3) if sy is not None else None,
            "tolerance_pct": _METER_TOLERANCE_PCT,
            # Four different incompletenesses, and the flag has to carry all of
            # them. ``r.partial`` is a within-day figure — how much of a day's
            # daylight the engine could model — and it is blind to a day that
            # produced no row to carry a flag on, to a string the inverter was
            # silent about for the whole period, and to a string that reported on
            # some days but not all of them.
            "partial": (
                any(r.partial for r in rows)
                or my_days < days_expected
                or (name == "" and any_string_incomplete)
            ),
            "days_scored": my_days,
            "days_expected": days_expected,
        }

    summary = {
        **_summarise("", by_string.get("", [])),
        "strings_scored": sum(
            len(groups_by_name[name].members) for name in scored_names if name in groups_by_name
        ),
        "strings_described": len(described_names),
    }

    # Per-MPPT summaries. A singleton group preserves the existing per-string
    # response shape; a shared MPPT carries its members so the page can explain
    # why the inverter cannot offer separate rows.
    string_summaries: list[dict[str, Any]] = []
    for group in groups:
        rows = by_string.get(group.label, [])
        if rows:
            summary_row = {"name": group.label, **_summarise(group.label, rows)}
            if len(group.members) > 1:
                summary_row["members"] = [member.name for member in group.members]
                summary_row["mppt"] = group.mppt
            string_summaries.append(summary_row)

    # Waterfall
    expected = summary["expected_kwh"]
    unexplained = summary["unexplained_kwh"]
    curtailed = summary["curtailed_kwh"]
    actual = summary["actual_kwh"]
    # The walk from expected to actual has to close, and it has to close in both
    # directions. An array can beat its model -- a nameplate typed in low, a
    # tilt guessed, a bifacial gain the model does not credit -- and a shortfall
    # figure clamped at zero can never account for that, so the segments would
    # silently fail to sum to what the inverter actually made. The surplus gets
    # its own segment and is not penalised, because producing more than
    # predicted is not a loss; a persistently large one means the array is
    # described wrongly, which is worth seeing rather than rounding away.
    surplus = summary["unmodelled_gain_kwh"]
    waterfall = [
        {"name": "expected", "kwh": round(expected, 3), "penalised": True},
        {"name": "unexplained", "kwh": round(unexplained, 3), "penalised": True},
        {"name": "curtailed", "kwh": round(curtailed, 3), "penalised": False},
        {"name": "unmodelled_gain", "kwh": round(surplus, 3), "penalised": False},
        {"name": "actual", "kwh": round(actual, 3), "penalised": True},
    ]

    # Hourly breakdown — only for period=day.
    hours: list[dict[str, Any]] | None = None
    worst_hour: dict[str, Any] | None = None
    if period == "day":
        hours = _hourly_efficiency_for_range(store, settings, range_start, range_end, strings, zone)
    elif len(days) <= 31:
        # For week and month, compute hourly to find the worst hour, but do not
        # ship the full array.
        hours = _hourly_efficiency_for_range(store, settings, range_start, range_end, strings, zone)

    # Worst hour: the one with the largest unexplained shortfall.
    if hours:
        candidates = [h for h in hours if h["unexplained_kwh"] > 0.0]
        if candidates:
            worst = max(candidates, key=lambda h: h["unexplained_kwh"])
            worst_hour = {
                "hour": worst["hour"],
                "unexplained_kwh": worst["unexplained_kwh"],
            }

    # Hours array: only for period=day.
    if period != "day":
        hours = None

    # Baselines are fitted per day by ``compute_hours`` inside ``compute_day``,
    # so a period longer than a day has no single fit to report — the range-wide
    # ``fitted_baselines`` can report itself calibrated on a week whose Tuesday
    # had too few producing hours for its own fit to succeed, and the page would
    # print a window that was not the evidence any of its numbers rested on.
    if period == "day":
        baseline = _baseline_info(
            fitted_baselines(store, settings, range_start, range_end, strings),
            range_start,
            range_end,
        )
    else:
        baseline = dict(_NO_BASELINE)

    return {
        "configured": True,
        "period": period,
        "start": range_start.isoformat(),
        "end": range_end.isoformat(),
        "now": now.isoformat(),
        "summary": summary,
        "waterfall": waterfall,
        "strings": string_summaries,
        "hours": hours,
        # The same days the summary was totalled from, so a week or a month has
        # a trend to draw. Without these the longer periods had a headline
        # figure and nothing underneath it -- the page simply dropped its chart,
        # which reads as a fault rather than as a period with no detail.
        "days": [
            {
                "day": r.day.isoformat(),
                "expected_kwh": round(r.expected_kwh, 3),
                "actual_kwh": round(r.actual_kwh, 3),
                "curtailed_kwh": round(r.curtailed_kwh, 3),
                "unexplained_kwh": round(r.unexplained_kwh, 3),
                "pr": round(r.pr, 4) if r.pr is not None else None,
                "partial": r.partial,
            }
            for r in sorted(by_string.get("", []), key=lambda r: r.day)
        ],
        "worst_hour": worst_hour,
        "baseline": baseline,
        # Null on a fixed mount, and null rather than zero on purpose: an owner
        # who has never adjusted anything would read a zero as "adjusting won
        # you nothing" rather than as "you have not adjusted".
        "tilt_benefit": _tilt_benefit_info(
            tilt_benefit(store, settings, range_start, range_end, strings)
        ),
    }


def _tilt_benefit_info(found: TiltBenefit | None) -> dict[str, Any] | None:
    """Shape the seasonal-adjustment comparison for the page, or nothing.

    ``hours`` travels with the figure rather than beside it, because the number
    means different things at eight hours and at eight hundred and a caller that
    can drop it will.
    """
    if found is None:
        return None
    return {
        "scheduled_kwh": round(found.scheduled_kwh, 3),
        "unadjusted_kwh": round(found.unadjusted_kwh, 3),
        "gain_kwh": round(found.gain_kwh, 3),
        "hours": found.hours,
        "adjustments": found.adjustments,
    }


def _reject_control_text(value: str) -> str:
    """Refuse text a device path, host or serial can never hold.

    Such a value would turn into a 500 at a raising sink downstream: a null
    byte makes pyserial raise on open; a lone surrogate makes any UTF-8
    encoding raise, including writing the config file and storing a setting.
    None of the fields this guards — a device path, a host, a serial — can
    contain a control character or unencodable text in any real installation,
    so rejecting them here is a clean 422 rather than a fault later.
    """
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("control characters are not allowed")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("value must be valid text") from exc
    return value


class DetectRequest(BaseModel):
    """Candidate connection parameters to probe. Nothing here is saved."""

    inverter_serial: str = ""
    transport: str
    serial_device: str = ""
    serial_baud: int = Field(default=19200, ge=1, le=4000000)
    serial_unit_id: int = Field(default=1, ge=1, le=247)
    dongle_host: str = ""
    dongle_port: int = Field(default=8000, ge=1, le=65535)
    dongle_serial: str = ""

    @field_validator("serial_device", "dongle_host", "dongle_serial", "inverter_serial")
    @classmethod
    def _clean(cls, value: str) -> str:
        return _reject_control_text(value)

    @field_validator("serial_device")
    @classmethod
    def _device_is_a_path(cls, value: str) -> str:
        check_serial_device(value)
        return value


@dataclass
class ProbeResult:
    """What a probe read off the wire: the serial, and the model when readable.

    The model fields are optional because the register read is best-effort —
    the serial is what the wizard cannot proceed without. ``model_read_failed``
    separates a register read that raised (a connection symptom, the thing a
    "check your connection" message is for) from a read that returned a code
    this project does not recognize (a real answer, just not one in the map).
    The frontend never has to guess which happened: the two are different
    fields.
    """

    serial: str
    model: str | None = None
    family_recognized: bool = False
    model_read_failed: bool = False


async def _probe_serial(body: DetectRequest) -> ProbeResult:
    """Open the candidate transport read-only and ask who is there.

    Split from the route so tests can stand in for the hardware: the route's
    job is borrowing, error mapping and never writing; this function's job is
    the wire. The library imports live inside so the module keeps no
    top-level dependency on the transport stack.

    The model registers are read on the same connection as the serial, before
    it is released. A second connection would cost a second open and release
    on a wire that admits one client, and on the serial transport a second
    open can fail while the first worked. The model read is best-effort and
    its failure never costs the serial: a register read raising beside a
    serial read that just succeeded is exactly what a flaky RS485 link looks
    like, and it is reported as its own field rather than failing the probe.
    """
    from pylxpweb.transports.dongle import DongleTransport
    from pylxpweb.transports.exceptions import TransportError
    from pylxpweb.transports.factory import create_dongle_transport, create_serial_transport
    from pylxpweb.transports.modbus_serial import ModbusSerialTransport

    from arraysense.drivers.eg4_luxpower.source import (
        RECOGNIZED_DEVICE_TYPE_CODES,
        identify_model,
    )

    transport: ModbusSerialTransport | DongleTransport
    if body.transport == "modbus_serial":
        transport = create_serial_transport(
            port=body.serial_device,
            serial="detect",
            baudrate=body.serial_baud,
            unit_id=body.serial_unit_id,
            timeout=10.0,
        )
    else:
        transport = create_dongle_transport(
            host=body.dongle_host,
            dongle_serial=body.dongle_serial,
            inverter_serial=body.inverter_serial,
            port=body.dongle_port,
            timeout=10.0,
        )
    try:
        await transport.connect()
    except (TransportError, OSError, UnicodeError) as exc:
        # A host that resolves through IDNA to an over-long DNS label raises
        # UnicodeError, not OSError, from connect. run_detect turns a
        # ConnectionError into a 502; without this it would surface as an
        # unhandled 500 on the unauthenticated setup surface.
        raise ConnectionError(str(exc)) from exc
    try:
        serial = str(await transport.read_serial_number())
    except (TransportError, OSError) as exc:
        raise ConnectionError(str(exc)) from exc

    model: str | None = None
    family_recognized = False
    model_read_failed = False
    try:
        regs = await transport.read_parameters(0, 20)
        device_type_code = regs.get(19)
        if device_type_code is not None:
            model = identify_model(device_type_code, regs.get(0, 0), regs.get(1, 0))
            family_recognized = device_type_code in RECOGNIZED_DEVICE_TYPE_CODES
    except (TransportError, OSError) as exc:
        # The serial already answered on this same connection; a register read
        # failing beside it is a connection symptom and must not fail the whole
        # probe. It is signalled so the page can name the transport, and logged
        # because an unattended service loses a flaky-link hint if nothing says.
        model_read_failed = True
        logger.warning("serial %s read but the model registers did not answer: %s", serial, exc)
    finally:
        await transport.disconnect()
    return ProbeResult(
        serial=serial,
        model=model,
        family_recognized=family_recognized,
        model_read_failed=model_read_failed,
    )


# One detect at a time across the process. Two concurrent probes would both
# find the collector already stopped by the first and probe the wire together,
# and the first's restart could begin polling while the second still holds it.
_DETECT_LOCK = asyncio.Lock()


async def run_detect(body: DetectRequest, service: CollectorService | None) -> dict[str, Any]:
    """Read the inverter's serial and model off a candidate connection. Writes nothing.

    Shared by the running-service route and first-run setup so the two cannot
    validate differently. A running collector holds the single client slot —
    the serial port is exclusive, the dongle takes one TCP client — so the
    probe borrows the wire by stopping the collector outright, which cancels
    the poll task and waits out an in-flight write where yield mode would race
    it, and starts it again on the way out whatever happened. In setup mode
    there is no collector, so nothing is borrowed. The mismatch decision
    belongs to the page: this returns what answered.

    The model fields only appear when they mean something. ``model`` when an
    exact model was identified, ``family_recognized`` when the family was
    recognized but no exact model could be asserted, ``model_read_failed``
    when the model register read raised on an otherwise-working connection.
    A probe that answered a serial and nothing else returns exactly
    ``{"serial": ...}``, the same shape it always did — which is also why a
    stand-in probe that returns a bare serial string still works here.
    """
    if body.transport not in ("dongle", "modbus_serial"):
        raise HTTPException(status_code=400, detail=f"unknown transport {body.transport!r}")
    if body.transport == "dongle" and not body.inverter_serial:
        # The dongle protocol authenticates every request with the inverter
        # serial, so a probe with a blank one can never receive an answer.
        # Serial-bus detection discovers the serial; dongle detection can only
        # confirm one the form already holds.
        raise HTTPException(
            status_code=400,
            detail="dongle detection needs the inverter serial; the dongle "
            "protocol authenticates with it",
        )
    async with _DETECT_LOCK:
        borrowed = False
        if service is not None and service.status.running:
            await service.stop()
            borrowed = True
        try:
            result = await _probe_serial(body)
        except (ConnectionError, OSError, TimeoutError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        finally:
            if borrowed and service is not None:
                await service.start()
    if isinstance(result, str):
        # A bare serial string is what the tests' stand-in probe returns; the
        # real probe returns a ProbeResult. The bare string means "no model
        # answer at all", which is also the honest reading of a model read that
        # could not be determined.
        result = ProbeResult(serial=result)
    payload: dict[str, Any] = {"serial": result.serial}
    if result.model is not None:
        payload["model"] = result.model
    if result.family_recognized:
        payload["family_recognized"] = True
    if result.model_read_failed:
        payload["model_read_failed"] = True
    return payload


def _fill_masked_detect(body: DetectRequest, config: Config) -> DetectRequest:
    """Probe the configured connection when its secrets were not retyped.

    The settings page prefills the connection with redacted values, so a Detect
    on an unchanged connection would otherwise carry bullet-filled host and
    serials and always fail to connect or authenticate. A masked field means
    "use what is already configured", so the real value from the effective config
    the collector runs on is substituted before the probe. First-run never
    reaches here with a mask — its form starts empty — so this only ever fills
    from a real configuration.
    """
    updates: dict[str, str] = {}
    for field, current in (
        ("dongle_host", config.dongle_host),
        ("dongle_serial", config.dongle_serial),
        ("inverter_serial", config.inverter_serial),
    ):
        value = getattr(body, field)
        if isinstance(value, str) and "\N{BULLET}" in value:
            updates[field] = current
    return body.model_copy(update=updates) if updates else body


@router.post("/setup/detect", dependencies=[Depends(_require_write)])
async def setup_detect(request: Request, body: DetectRequest) -> dict[str, Any]:
    """Read the inverter's serial and model off a candidate connection. Writes nothing."""
    file_config = getattr(request.app.state, "file_config", None) or request.app.state.config
    settings = SettingsStore(request.app.state.store)
    body = _fill_masked_detect(body, effective(file_config, settings))
    return await run_detect(body, getattr(request.app.state, "service", None))


class ApplyRequest(BaseModel):
    """The setup fields a page may change.

    Everything optional; only provided fields are validated and written.
    """

    driver: str | None = None
    transport: str | None = None
    serial_device: str | None = None
    serial_baud: int | None = Field(default=None, ge=1, le=4000000)
    serial_unit_id: int | None = Field(default=None, ge=1, le=247)
    dongle_host: str | None = None
    dongle_serial: str | None = None
    inverter_serial: str | None = None
    model: str | None = None
    battery_source: str | None = None

    @field_validator(
        "serial_device", "dongle_host", "dongle_serial", "inverter_serial", "model", "driver"
    )
    @classmethod
    def _clean(cls, value: str | None) -> str | None:
        return None if value is None else _reject_control_text(value)

    @field_validator("serial_device")
    @classmethod
    def _device_is_a_path(cls, value: str | None) -> str | None:
        if value is not None:
            check_serial_device(value)
        return value


_SETTING_KEYS: dict[str, str] = {
    "driver": "connection.driver",
    "transport": "connection.transport",
    "serial_device": "connection.serial_device",
    "serial_baud": "connection.serial_baud",
    "serial_unit_id": "connection.serial_unit_id",
    "dongle_host": "connection.dongle_host",
    "dongle_serial": "connection.dongle_serial",
    "inverter_serial": "connection.inverter_serial",
    "model": "connection.model",
    "battery_source": "connection.battery_source",
}


def _schedule_restart() -> None:
    """Exit cleanly in a moment, after the response has gone out.

    systemd's Restart policy brings the service back, and the next boot reads
    the overlay this request just wrote. SIGTERM rather than sys.exit because
    the shutdown path — releasing the inverter's single client slot — already
    hangs off it, and a restart that skipped disconnect would leave the next
    start finding the slot occupied.
    """
    import os
    import signal

    loop = asyncio.get_running_loop()
    loop.call_later(0.5, os.kill, os.getpid(), signal.SIGTERM)


@router.post("/setup/apply", dependencies=[Depends(_require_write)])
async def setup_apply(request: Request, body: ApplyRequest) -> dict[str, Any]:
    """Validate the merged result, write the overlay, restart the collector.

    Validation is against the exact config the next start will assemble — the
    file config, the stored settings, and this change layered on with the real
    merge semantics — so every rule the service enforces at boot refuses here
    first with the same words. Only after the whole merged picture validates
    does anything get written; a partial write of a bad combination would be a
    page-made outage.
    """
    provided = {k: v for k, v in body.model_dump().items() if v is not None}
    # A masked value posted back unchanged is the page echoing what /api/setup
    # showed it, not a choice. Discarded exactly as the settings endpoint
    # discards them, or an untouched full-form submit would write dots over
    # real serials.
    provided = {k: v for k, v in provided.items() if not _is_mask(_SETTING_KEYS[k], v)}
    pending = {_SETTING_KEYS[k]: v for k, v in provided.items()}
    file_config = getattr(request.app.state, "file_config", request.app.state.config)
    settings = SettingsStore(request.app.state.store)
    try:
        # The exact config the next start will assemble — file, stored
        # settings, this change layered on with the real merge — validated
        # against the registry's boot rules. Reusing effective() is why a
        # cleared field is modelled as reverting to the file value rather than
        # keeping the current one, which an earlier hand-rolled merge got wrong.
        drivers.validate(effective(file_config, settings, pending=pending))
        # All-or-nothing: every value validates against its spec before any
        # row is written, in one transaction.
        settings.set_many(pending)
    except (ValueError, TypeError, OverflowError) as exc:
        # A malformed value — wrong type, or a number too large to coerce —
        # is a bad request, not a server fault.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _schedule_restart()
    return {"applied": sorted(provided), "restarting": True}


@router.post("/yield", dependencies=[Depends(_require_write)])
async def yield_dongle(request: Request, body: YieldRequest) -> dict[str, Any]:
    """Release the dongle so the vendor's app can push a firmware update.

    The dongle accepts one TCP client, so the collector has to let go before
    anything else can connect.
    """
    until = await request.app.state.service.yield_for(body.seconds)
    logger.info("yield requested for %.0fs", body.seconds)
    return {"yielding": True, "seconds": body.seconds, "until": until.isoformat()}


@router.post("/resume", dependencies=[Depends(_require_write)])
async def resume(request: Request) -> dict[str, Any]:
    """Take the dongle back before the yield timer runs out."""
    await request.app.state.service.resume()
    return {"yielding": False}


# --- The Emporia module -------------------------------------------------------
#
# Every one of these answers when the module is absent rather than raising: a
# build with the module never started, or an installation that has not enabled
# it, must serve a page that says "off" rather than a 500. The reads stay open
# like every other read — the wall display is not logged in — while anything
# that stores a credential or changes what the service does sits behind the
# password.


class EmporiaLogin(BaseModel):
    """An Emporia account login. The password is used once and never stored."""

    email: str
    password: str


def _emporia(request: Request) -> EmporiaPoller | None:
    """The running poller, or None when this build is not running one."""
    poller = getattr(request.app.state, "emporia", None)
    return poller if isinstance(poller, EmporiaPoller) else None


# How far the house's own window may fall short of the circuits' before the two
# stop describing the same span. Five minutes is the floor, and it is sized on
# what the store can actually answer rather than chosen: the driver reads the
# energy registers on a sixty-second clock of their own, and a window of two
# days or under is answered from the minute tier, whose buckets are stamped at
# the start of the minute they cover and are rebuilt once a minute. The last
# instant the house is known for therefore sits a couple of minutes behind the
# wall clock even on a perfectly healthy installation.
COVERAGE_SLACK = timedelta(minutes=5)

# ...and one percent of the window besides, which is what carries the long
# ranges. Past two days a counter read is answered from the hourly tier, whose
# newest bucket is stamped on the hour and so trails the end of a live range by
# up to fifty-nine minutes — 0.6% of the seven-day range the Graphs page offers
# and less of the thirty-day one. A flat five minutes would refuse both of them
# for ever, which is not caution but a line that never works.
#
# What neither allowance may become is an excuse. Past it the numerator covers
# time the denominator does not and the share reads over 100% — a partial
# figure presented as a complete one, which is the one thing this line exists
# to prevent — so the honest answer there is that the house total is unknown.
COVERAGE_SHORTFALL = 0.01

# How much of a window the circuits must have recorded for before their energy
# may be read as a share of the house's own counter.
#
# Ninety per cent, and the number is the whole of this rule. A healthy
# installation loses at most one poll at each edge of the window — two minutes
# of the shortest range the Graphs page offers, three per cent — so this never
# fires on ordinary operation. Below it the share is understated by more than a
# tenth of its own value, which is more than a percentage rounded to whole
# numbers can absorb, and the sentence stops describing what a reader thinks it
# does: measured on the bench, a seven-day window in which the module had
# recorded for six hours returned circuits 18.392 kWh, house 254.8 kWh, fraction
# 0.0722. Every figure correct, and "monitored circuits cover 7% of the house"
# invites the reader to conclude the house is barely monitored when the truth is
# that the module was not running.
#
# Here rather than in the browser because a page draws what an endpoint tells
# it, and because ``docs/api.md`` has promised since this endpoint shipped that
# the fraction is null when the two do not cover closely enough the same span.
# Only the browser checked, so the promise was not kept for any other consumer.
CIRCUIT_SPAN_ENOUGH = 0.9

# An empty coverage answer, so a module that is not running says "unknown" in
# the same shape a running one says a number. A page that had to branch on a
# missing key would be one refactor away from rendering "0%" for "no module".
_NO_COVERAGE: dict[str, Any] = {
    "circuits_kwh": None,
    "house_kwh": None,
    "fraction": None,
    "recorded_seconds": 0,
    "window_seconds": 0,
    "spans_match": False,
}


def _aware(when: datetime) -> datetime:
    """A query bound with a zone attached, reading a naive one as the server's.

    The pages send instants with an offset, but a hand-typed URL need not, and
    ``counter_kwh`` refuses a naive bound outright rather than guessing — which
    would answer a mistyped range with a 500. ``astimezone`` on a naive value
    assumes the machine's own zone, which is exactly the assumption
    ``datetime.timestamp`` was already making one layer down, so nothing about
    which instant is meant changes here; it is only said out loud.
    """
    return when if when.tzinfo is not None else when.astimezone()


def _parse_circuit_ids(ids: str | None) -> list[int] | None:
    """Turn ``ids=3,7,11`` into a list, or None for every circuit.

    A malformed entry is a bad request rather than a silently narrowed answer:
    dropping an unparseable id would return four strips where five were asked
    for, and the page has no way to notice.
    """
    if ids is None or not ids.strip():
        return None
    try:
        return [int(part) for part in ids.split(",") if part.strip()]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"bad circuit ids: {ids!r}") from exc


def _emporia_cadence_seconds(request: Request) -> int:
    """The Emporia poll interval, which is the circuit raw tier's resolution.

    Not the inverter's. Scored at eleven seconds a seven-day circuit range comes
    out at 55,000 points and picks the raw tier, which holds ten thousand rows
    for it — the tier choice would be made against a cadence that does not
    describe this data at all. It is also the bound on what one raw reading may
    account for, since the raw tier records no cadence of its own.

    It is *not* the hourly tier's arithmetic any more. That tier stores the
    coverage the rollup measured while the interval that produced the readings
    was still in force, and handing this figure to rows recorded under another
    one is the defect that doubled every stored hour's energy the day the bench
    interval was raised.
    """
    return emporia_interval_seconds(SettingsStore(request.app.state.store))


def _coverage_end(
    store: SqliteStore, start: datetime, end: datetime, span: timedelta, field: str = "load_kwh"
) -> datetime | None:
    """The last instant the house's own counter is known for, or None.

    The house figure is read to the last instant the inverter actually
    reported, not to the wall clock. ``counter_kwh`` is right to refuse a bound
    it cannot bracket — that refusal is what stops an outage being billed to
    the day collection came back in — but every range the Graphs page offers
    ends at "now", and there is never a reading after now. Asked as written,
    the coverage line would be blank on every live request, permanently.

    The circuits' own energy is deliberately not re-cut to this instant. There
    is nothing in the tail to cut: the shortfall is a poll or two of the
    inverter's, and the two windows describe the same span for every purpose
    this percentage has. What keeps that true is the allowance below — past it
    the numerator would cover hours the denominator does not, and None is
    returned so the page can say the house total is unknown instead.

    The search runs against the tier ``counter_kwh`` will read and falls back
    the same way, because a clamp landing on an instant that tier has no row
    for is no better than no clamp at all. A minute bucket is stamped at the
    start of the minute it covers rather than at the reading inside it, so the
    newest raw reading is later than the newest minute row on all but the one
    second they can share — measured here as a raw counter at 17:59:18 against
    a minute bucket at 17:59:00, where clamping to the raw instant returned
    None and clamping to the bucket returned the figure. Where the mirror is
    imperfect the answer is None and the page says "unknown", which is the safe
    direction to be wrong in.

    Reading forward as far as ``MAX_EDGE_GAP`` is what keeps a historical
    window unclamped: if any reading lands at or after ``end`` then the window
    is bracketed as asked and nothing needs pulling back.

    ``span`` is handed in rather than measured from the two bounds, because
    subtracting two aware datetimes ignores the zone they share: a window
    running midnight to midnight across the November clock change is 49 hours
    long and reads as 48. The caller has already measured it through
    ``tariff._elapsed``, and one request must not measure one window twice.
    """
    metric = ENERGY_FIELDS[field]
    allowance = max(COVERAGE_SLACK, span * COVERAGE_SHORTFALL)
    floor, ceiling = end - allowance, end + MAX_EDGE_GAP
    rows = store.query([metric], floor, ceiling, tier=_window_tier(start, end))
    if not rows:
        rows = store.query([metric], floor, ceiling, tier="full")
    moments = [
        when for row in rows if row.get(metric) is not None and (when := _row_time(row)) is not None
    ]
    if not moments:
        return None
    return end if moments[-1] >= end else moments[-1]


@router.get("/emporia/status")
async def emporia_status(request: Request) -> dict[str, Any]:
    """What the module is doing, and whether it needs the owner.

    Reports the poller's own state because nothing else can: circuits live in
    their own tables specifically so they never satisfy the store's staleness
    witness, which means an outage here leaves no symptom anywhere else.
    """
    # ``enabled`` is the owner's setting and ``status`` is what the poller is
    # doing, and they are deliberately two questions. Deriving the first from
    # the second would make the module invisible for the first interval after
    # it was switched on, because a poller that has not ticked yet reports
    # "off" — which is exactly when somebody is looking for it.
    settings = SettingsStore(request.app.state.store)
    enabled = bool(settings.get(EMPORIA_ENABLED_KEY))
    poller = _emporia(request)
    if poller is None:
        return {"status": "off", "detail": "", "last_success": None, "enabled": enabled}
    state = poller.state
    return {
        "status": state.status,
        "detail": state.detail,
        "last_success": state.last_success,
        "enabled": enabled,
    }


@router.get("/emporia/circuits")
async def emporia_circuits(request: Request) -> dict[str, Any]:
    """Every known circuit with its latest reading, biggest draw first.

    ``watts`` is null for a circuit that has not reported, and stays null all
    the way to the page. Zero would be a claim that it drew nothing.

    ``connected`` and ``offline_since`` are what separates the two ways of
    drawing nothing. Two of the reference account's outlets have been offline
    since April and August, and without these a page can only render them the
    same as a circuit that happened to be idle. They belong to the device rather
    than the channel, so every circuit on a dead monitor carries the same answer.

    ``id`` is what lets a row on this page link to that circuit's own chart on
    ``/graphs#circuits=<id>``. It is the same surrogate ``/api/emporia/history``
    reports for the same circuit — the two must agree, since a link is only as
    good as the id it names being the id the other endpoint answers to.
    """
    poller = _emporia(request)
    if poller is None:
        return {"circuits": []}
    connections = poller.connections
    return {
        "circuits": [
            {
                "id": circuit.circuit_id,
                "name": circuit.name,
                "kind": circuit.kind,
                "watts": circuit.watts,
                "ts": circuit.ts,
                # Emporia's own category number, passed through raw. Which icon
                # it earns is the page's business: a number here and a picture
                # there keeps the mapping in one place, and it is presentation
                # rather than a reading.
                "type_gid": circuit.type_gid,
                # None for a device Emporia said nothing about. Silence is not
                # health, and a default of true here would quietly declare every
                # unmentioned device up.
                "connected": (
                    connections[circuit.device_gid].connected
                    if circuit.device_gid in connections
                    else None
                ),
                "offline_since": (
                    connections[circuit.device_gid].offline_since
                    if circuit.device_gid in connections
                    else None
                ),
            }
            for circuit in poller.repository.latest()
        ]
    }


def _circuit_coverage(
    store: SqliteStore,
    start: datetime,
    end: datetime,
    *,
    circuits_kwh: float | None,
    recorded_seconds: int,
    window_seconds: int,
) -> dict[str, Any]:
    """What share of the house's energy the monitored circuits account for.

    Written once and called twice: the circuit history endpoint draws bars
    against it and the Costs page's circuit ranking prices against it, and two
    copies of this arithmetic would answer the same question two ways on two
    tabs of the same page. It is also the arithmetic #23 was reverted over
    twice, which is reason enough for there to be one of it.

    Computed from energy, never from minutes watched. The monitored circuits are
    not the house — unmonitored branches are real, and two of the reference
    account's outlets have been offline since April and August — so a page
    naming a handful of circuits without this invites the reader to believe they
    are the whole bill.

    A numerator that recorded for a fraction of the window its denominator
    covers is arithmetic between two different spans, not a share. ``fraction``
    is withheld rather than re-based on the recorded span, because the answer
    carries no house figure for that span — only for the whole window — and
    inventing one by assuming the house drew power evenly is exactly the
    estimate this project refuses to dress as a meter reading.

    It is reported uncapped: a part cannot exceed the whole, so a figure above
    one is not coverage at all but a fault saying so — a mains channel that
    escaped the exclusion, a multiplier set for the wrong circuit, or two
    windows that stopped being comparable. Clamping it to 1.0 renders every
    one of those as perfect coverage, which is the one reading guaranteed to
    be wrong, and would hide the fault for as long as it lasted. The page
    renders anything above one as a disagreement rather than as a full bar.
    """
    span = timedelta(seconds=window_seconds)
    coverage_end = _coverage_end(store, start, end, span)
    house_kwh = (
        counter_kwh(store, start, coverage_end)
        if coverage_end is not None and coverage_end > start
        else None
    )
    # Whether the two figures describe the same stretch of time closely enough
    # for one to be read as a share of the other. ``_check_range`` has already
    # refused anything that does not run forwards, so the guard is for a range
    # whose seconds round to nothing: there is no shortfall to find in a window
    # of no length, and the house figure over one is zero anyway.
    spans_match = window_seconds <= 0 or recorded_seconds >= window_seconds * CIRCUIT_SPAN_ENOUGH
    return {
        "circuits_kwh": None if circuits_kwh is None else round(circuits_kwh, 3),
        "house_kwh": None if house_kwh is None else round(house_kwh, 3),
        "fraction": (
            None
            if circuits_kwh is None or house_kwh is None or house_kwh <= 0 or not spans_match
            else round(circuits_kwh / house_kwh, 4)
        ),
        # What the span check was decided on, so the page can say it without
        # measuring it again. The old browser-side count credited every hourly
        # bucket that held anything with a full 3,600 seconds, which reported
        # a seven-day window holding one reading an hour as seven days
        # recorded and defeated the check from the other side.
        "recorded_seconds": recorded_seconds,
        "window_seconds": window_seconds,
        "spans_match": spans_match,
    }


@router.get("/emporia/history")
def emporia_history(
    request: Request,
    store: _ReadStore,
    start: datetime,
    end: datetime,
    ids: str | None = None,
    width: int = Query(default=1000, ge=1, le=10000),
) -> dict[str, Any]:
    """Circuits over a range, ranked by energy, at a resolution that suits the chart.

    ``ids`` narrows the answer to named circuits; omitted, every circuit is
    returned and the page decides how many strips to draw. The narrowing is
    here rather than in the browser because the reference account has
    thirty-nine circuits and fetching all of them to draw five is the query
    this argument exists to avoid.

    ``coverage`` is the one figure on this endpoint that can mislead. The
    monitored circuits are not the house — unmonitored branches are real, and
    two of the reference account's outlets have been offline since April and
    August — so a page drawing five bars without it invites the reader to
    believe those five are the house. It is computed from energy rather than
    from minutes watched, which is the distinction #23 was reverted twice for
    missing, and it is None rather than 1.0 when the house's own figure is
    absent: a fraction taken against an unknown denominator would read as full
    coverage, which inverts the truth exactly. The circuits' own total is None
    on the same terms, since a monitor nobody has heard from did not measure
    nothing.

    Both sides of that comparison are bounded here. ``_coverage_end`` pulls the
    house figure back to the last instant the inverter's own counter is known
    for; ``spans_match`` is the other half, and it was missing — the circuits
    could have recorded for six hours of a seven-day window and the share was
    still divided out and reported. It is null past that point, with
    ``recorded_seconds`` and ``window_seconds`` alongside it so a page can say
    what happened rather than measure it again.

    A build with no poller answers an empty history rather than 404ing. The tab
    is gated on the module, but a bookmark outlives the account it was made on.
    """
    start, end = _aware(start), _aware(end)
    _check_range(start, end)
    poller = _emporia(request)
    # Through ``_elapsed``, not by subtraction, and measured once for the whole
    # request. A range that spans a clock change is 23 or 25 hours long and the
    # naive difference says 24, which would put a fully recorded autumn window
    # under the span threshold and withhold a share that was perfectly good —
    # and would have this one request answer "how long is this window" three
    # different ways, for the tier, for the coverage allowance, and here.
    window_seconds = int(_elapsed(start, end))
    span = timedelta(seconds=window_seconds)
    if poller is None:
        return {
            "tier": "full",
            "timestamps": [],
            "circuits": [],
            "coverage": {**_NO_COVERAGE, "window_seconds": window_seconds},
        }

    wanted = _parse_circuit_ids(ids)
    cadence = _emporia_cadence_seconds(request)
    tier = select_tier(span, width_px=width, cadence_seconds=cadence, circuit=True)
    with _inside_the_calendar():
        # Read through the injected view, not through ``poller.repository``.
        # The poller's repository holds the primary connection — the one the
        # collector writes through — and this is the heaviest read the module
        # makes: thirty days across thirty-nine circuits is tens of thousands
        # of rows. Running it there is the shape ``_read_store`` was written to
        # end, and its own docstring names the cost, measured at 1.6 to 3.2
        # seconds a response while issue #63 was chased through the rollup.
        # ``latest()`` stays on the poller's connection because it reads one row
        # per circuit and is not worth a second handle.
        history = CircuitRepository(store).history(
            start, end, tier=tier, circuit_ids=wanted, cadence_seconds=cadence
        )
        parts = [s for s in history.series if s.kind not in NOT_A_CULPRIT]
        measured = [s.kwh for s in parts if s.kwh is not None]
        circuits_kwh = sum(measured) if measured else None
        coverage = _circuit_coverage(
            store,
            start,
            end,
            circuits_kwh=circuits_kwh,
            recorded_seconds=history.recorded_seconds,
            window_seconds=window_seconds,
        )

    # The poller holds this, not the repository: it is what the last status call
    # said, refreshed on the module's own clock. emporia_circuits already reads
    # it from the same place.
    connections = poller.connections
    return {
        "tier": history.tier,
        "timestamps": list(history.timestamps),
        "circuits": [
            {
                "id": s.circuit_id,
                "name": s.name,
                "kind": s.kind,
                "watts": list(s.watts),
                "kwh": None if s.kwh is None else round(s.kwh, 3),
                "partial": s.partial,
                "offline_since": (
                    connections[s.device_gid].offline_since if s.device_gid in connections else None
                ),
            }
            for s in parts
        ],
        "coverage": coverage,
    }


@router.post("/emporia/login", dependencies=[Depends(_require_write)])
async def emporia_login(request: Request, body: EmporiaLogin) -> dict[str, Any]:
    """Exchange a password for tokens. The password is not retained anywhere."""
    poller = _emporia(request)
    if poller is None:
        raise HTTPException(status_code=404, detail="the Emporia module is not available")
    try:
        token_set = await asyncio.to_thread(poller.client.login, body.email, body.password)
    except EmporiaChallengeError as exc:
        raise HTTPException(status_code=409, detail=f"Emporia asked for {exc}") from exc
    except EmporiaAuthExpiredError as exc:
        raise HTTPException(
            status_code=401, detail="Emporia rejected that email or password"
        ) from exc
    except EmporiaUnreachableError as exc:
        raise HTTPException(status_code=503, detail=f"could not reach Emporia: {exc}") from exc
    await asyncio.to_thread(emporia_tokens.save, poller.token_path, token_set)
    # Tick once, here, before answering. The page reads the poller's state, and
    # the poller's clock is a minute wide — so without this a login that worked
    # leaves "the saved Emporia login has expired" on screen with the form still
    # open, for up to a minute. Somebody watching that types their password
    # again, which is precisely what happened the first time this was tried
    # against a real account. The tick is one extra call at the one moment the
    # owner is certainly watching, and it never raises.
    await poller.tick(datetime.now(tz=UTC))
    return {"ok": True}


class ChargeRate(BaseModel):
    """A charge rate somebody asked for, in amps."""

    amps: int


def _refuse_while_disabled(settings: SettingsStore) -> None:
    """Stop a write to the charger while the module is switched off.

    The enable is not the authority setting and neither implies the other, so
    both write routes checked ``charger_authority`` and neither checked this —
    which left a disabled module holding a stale charger, a live token, and two
    endpoints that would have reached a real car. Refused with a 409 in the
    manner of the app-authority refusal rather than accepted and dropped,
    because a control that takes a number and does nothing with it is worse than
    one that is not there.
    """
    if not bool(settings.get(EMPORIA_ENABLED_KEY)):
        raise HTTPException(
            status_code=409,
            detail="the Emporia module is switched off; switch it on in Settings first",
        )


@router.get("/emporia/charger")
async def emporia_charger(request: Request) -> dict[str, Any]:
    """The charger, who else is driving it, and what this service has done to it.

    ``conflicts`` names Emporia's own controllers that are switched on for this
    charger. It is a warning and never a refusal — it is the owner's charger and
    their account — but it has to be said, because two controllers moving one
    rate will undo each other and neither will look broken.

    A switched-off module answers with no charger at all, and that is the rule
    stated in one place so nothing downstream has to remember it: the nav draws
    the Charger tab from this answer, and it kept drawing it — over a page of
    live controls — for a module the owner had turned off.
    """
    settings = SettingsStore(request.app.state.store)
    enabled = bool(settings.get(EMPORIA_ENABLED_KEY))
    poller = _emporia(request)
    if poller is None or poller.charger is None or not enabled:
        return {"charger": None, "changes": [], "enabled": enabled}
    state = poller.charger
    return {
        "enabled": enabled,
        "charger": {
            "device_gid": state.device_gid,
            "rate_a": state.rate_a,
            "max_rate_a": state.max_rate_a,
            "on": state.on,
            "status": state.status,
            "message": state.message,
            "conflicts": list(state.conflicts),
            "plugged_in": state.plugged_in,
            "connected": state.connected,
            "offline_since": state.offline_since,
            "fault": state.fault,
            "authority": settings.get(CHARGER_AUTHORITY_KEY),
            "floor_a": settings.get(CHARGE_FLOOR_KEY),
            "ceiling_a": settings.get(CHARGE_CEILING_KEY),
        },
        "changes": [
            {
                "timestamp": change.timestamp,
                "from_a": change.from_a,
                "to_a": change.to_a,
                "reason": change.reason,
                "applied": change.applied,
                # Who decided it. Null on a line written before this was
                # recorded, which the page must not render as either party.
                "source": change.source,
            }
            for change in poller.audit.recent_changes()
        ],
    }


@router.post("/emporia/charger/rate", dependencies=[Depends(_require_write)])
async def emporia_set_rate(request: Request, body: ChargeRate) -> dict[str, Any]:
    """Set the charge rate by hand, through every guard the module has.

    A request from this route is the owner asking, so it is applied whatever the
    authority setting says — advisory means the *module* proposes rather than
    acts, not that the owner may not act. The floor, the ceiling and the
    hardware maximum still hold, because those are about what the charger and
    the wiring can take rather than about who is asking.

    It also starts the override window. Somebody who has just set a rate by hand
    should not have it moved out from under them by the next automatic decision.
    """
    # The enable is asked first, and before the charger is looked for. A tick
    # clears the cached charger the moment the module is switched off, so asking
    # about the charger first answers "no Emporia charger is being read" — true,
    # but not the reason, and the reason is the thing somebody who has just
    # turned the module off needs to be told.
    settings = SettingsStore(request.app.state.store)
    _refuse_while_disabled(settings)
    poller = _emporia(request)
    if poller is None or poller.charger is None:
        raise HTTPException(status_code=404, detail="no Emporia charger is being read")
    if settings.get(CHARGER_AUTHORITY_KEY) == "app":
        # Refused rather than quietly ignored. The owner said the Emporia app
        # has this charger, and a control that accepts a number and does
        # nothing with it is worse than one that is not there.
        raise HTTPException(
            status_code=409,
            detail="the Emporia app manages this charger; change that in Settings first",
        )
    charger = poller.charger
    rate, refused = clamp_rate(body.amps, poller.limits())
    now = datetime.now(tz=UTC)
    # The window opens when the owner presses, not when Emporia answers. The
    # write below is a round trip to a cloud service, and the restore runs on
    # the poller's own clock — so opening it afterwards left a gap in which an
    # automatic decision could look at a charger the owner was in the middle of
    # changing, find no hold, and write over it. A failed write holds too: they
    # reached for the charger either way, and the conservative reading of that
    # is the one this module owes them.
    minutes = settings.get(CHARGE_OVERRIDE_MINUTES_KEY)
    hold = int(minutes) if isinstance(minutes, int) else 120
    settings.set(CHARGE_OVERRIDE_UNTIL_KEY, int(now.timestamp()) + hold * 60)
    try:
        confirmed = await asyncio.to_thread(poller.write_rate, rate)
    except EmporiaAuthExpiredError as exc:
        raise HTTPException(status_code=401, detail="Emporia rejected the saved login") from exc
    except EmporiaUnreachableError as exc:
        poller.audit.record_change(
            charger.device_gid,
            from_a=charger.rate_a,
            to_a=rate,
            reason=f"failed: {exc}",
            applied=False,
            source=OWNER,
            now=now,
        )
        raise HTTPException(status_code=503, detail=f"could not reach Emporia: {exc}") from exc
    # Applied means the charger says so, not that Emporia returned a 200. The
    # write is accepted asynchronously, and auditing on the status code alone
    # made a working write look like a failed one the first time this ran
    # against a real car — worse, a restore that trusts a rate the charger is
    # not at will happily leave it there.
    took = confirmed is not None and confirmed.rate_a == rate
    poller.audit.record_change(
        charger.device_gid,
        from_a=charger.rate_a,
        to_a=rate,
        reason=("set by hand" if took else "set by hand, but the charger still reads differently")
        + (f" ({refused})" if refused else ""),
        applied=took,
        source=OWNER,
        now=now,
    )
    return {"ok": True, "rate_a": rate, "refused": refused, "confirmed": took}


class ChargerPower(BaseModel):
    """Whether the charger should be delivering at all."""

    on: bool


@router.post("/emporia/charger/power", dependencies=[Depends(_require_write)])
async def emporia_set_power(request: Request, body: ChargerPower) -> dict[str, Any]:
    """Stop or start charging.

    A different power from setting a rate, and a heavier one: a rate that is too
    low charges a car slowly, while a charger switched off charges it not at
    all. For the *module* that distinction is the ``full`` authority level. This
    route is the owner asking, so it acts either way — but it is audited like
    everything else, because "why is the car not charged" has to have an answer.
    """
    # The enable is asked first, and before the charger is looked for. A tick
    # clears the cached charger the moment the module is switched off, so asking
    # about the charger first answers "no Emporia charger is being read" — true,
    # but not the reason, and the reason is the thing somebody who has just
    # turned the module off needs to be told.
    settings = SettingsStore(request.app.state.store)
    _refuse_while_disabled(settings)
    poller = _emporia(request)
    if poller is None or poller.charger is None:
        raise HTTPException(status_code=404, detail="no Emporia charger is being read")
    if settings.get(CHARGER_AUTHORITY_KEY) == "app":
        raise HTTPException(
            status_code=409,
            detail="the Emporia app manages this charger; change that in Settings first",
        )
    charger = poller.charger
    now = datetime.now(tz=UTC)
    try:
        confirmed = await asyncio.to_thread(poller.write_charger, {"chargerOn": body.on})
    except EmporiaAuthExpiredError as exc:
        raise HTTPException(status_code=401, detail="Emporia rejected the saved login") from exc
    except EmporiaUnreachableError as exc:
        poller.audit.record_change(
            charger.device_gid,
            from_a=charger.rate_a,
            # No rate was decided. Recording the current one here made a power
            # press look like a decision about the rate, and the audit is what
            # restore-on-startup reads to work out whose rate the charger is
            # sitting at — so pressing stop once retired the restore for good.
            to_a=None,
            reason=f"failed to {'start' if body.on else 'stop'} charging: {exc}",
            applied=False,
            source=OWNER,
            now=now,
        )
        raise HTTPException(status_code=503, detail=f"could not reach Emporia: {exc}") from exc
    took = confirmed is not None and confirmed.on is body.on
    poller.audit.record_change(
        charger.device_gid,
        from_a=charger.rate_a,
        # See the failure path above: a power press decides nothing about the
        # rate, and an absent rate is absent rather than a repeat of the one
        # that happened to be set.
        to_a=None,
        reason=("started charging" if body.on else "stopped charging")
        + ("" if took else ", but the charger still reads otherwise"),
        applied=took,
        source=OWNER,
        now=now,
    )
    return {"ok": True, "on": body.on, "confirmed": took}


@router.post("/emporia/disconnect", dependencies=[Depends(_require_write)])
async def emporia_disconnect(request: Request) -> dict[str, Any]:
    """Forget the stored credential.

    Revoking it at AWS as well is the right behaviour and is Stage 3 work: the
    Cognito ``RevokeToken`` call is documented but has never been tested against
    Emporia's pool, and claiming to revoke while only forgetting would be worse
    than saying plainly that this forgets. ``revoked`` is false so that nothing
    reading this can believe otherwise.
    """
    poller = _emporia(request)
    if poller is None:
        raise HTTPException(status_code=404, detail="the Emporia module is not available")
    emporia_tokens.clear(poller.token_path)
    return {"ok": True, "revoked": False}


def _battery_block(inverter: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the battery block for the /api/live response.

    Returns capacity_kwh, rate_pct_per_hour, and pack_voltage_v. The capacity
    is derived from battery_full_capacity_ah and battery_voltage_v (Ah x V =
    Wh, divided by 1000 for kWh). The rate is battery_power_w divided by
    capacity_kwh, scaled to percent per hour. The sign follows the power:
    positive for charging, negative for discharging.

    Absent data is absent: null, never zero. A bank with no capacity reading
    shows no rate. An idle bank (power = 0) reports 0.0 for the rate.
    """
    if inverter is None:
        return {"capacity_kwh": None, "rate_pct_per_hour": None, "pack_voltage_v": None}

    capacity_ah = inverter.get("battery_full_capacity_ah")
    voltage_v = inverter.get("battery_voltage_v")
    power_w = inverter.get("battery_power_w")

    # Amp-hours at the pack's own voltage, in kWh. Kept unrounded for the
    # division below and rounded only on the way out: dividing by the rounded
    # figure would compute the rate from what the card displays rather than from
    # what the bank reported.
    #
    # This is energy at the voltage measured right now, not the bank's rated
    # size, and the two differ: 1120 Ah reads 57.2 kWh at the nominal 51.1 V and
    # 61.5 kWh at the 54.9 V a full bank sits at. So it drifts about seven
    # percent across a charge cycle, which is the right basis for "how fast is
    # this filling" and the wrong one for "how big is this bank". It is not shown
    # as a bank size anywhere for that reason — the card prints the rate, the
    # state of charge and the voltage, and leaves this as the working.
    exact_kwh: float | None = None
    if capacity_ah is not None and voltage_v is not None:
        exact_kwh = (capacity_ah * voltage_v) / 1000.0

    # A capacity of zero is inside the metric's own plausible range, so it
    # arrives as an ordinary reading rather than an error — and dividing by it
    # took down /api/live, which is the endpoint the whole dashboard polls. It
    # is also not a bank filling at any rate: a bank that holds nothing has no
    # rate to report, which is exactly what absent means here.
    rate_pct_per_hour: float | None = None
    if exact_kwh and power_w is not None:
        # W / kWh gives %/h once the watts are kilowatts: (power/1000)/kWh*100.
        rate_pct_per_hour = round(power_w / exact_kwh / 10.0, 2)

    return {
        "capacity_kwh": None if exact_kwh is None else round(exact_kwh, 1),
        "rate_pct_per_hour": rate_pct_per_hour,
        "pack_voltage_v": voltage_v,
    }


def _isoformat_row(row: dict[str, Any]) -> dict[str, Any]:
    """Render timestamps as ISO strings, leaving everything else alone.

    Nulls stay null rather than becoming zero on the way out; that distinction
    is the whole reason absent readings are stored as NULL.
    """
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
