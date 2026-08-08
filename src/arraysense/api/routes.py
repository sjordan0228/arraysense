"""routes.py — the HTTP surface: live values, history, status, and yielding.

Everything here reads from the store and never touches the inverter. The
collector owns the one connection the dongle allows, so an API that reached for
it directly would fight the poll loop for the single TCP slot.

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

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from arraysense import __version__
from arraysense import mode as operating_mode
from arraysense.calibration import (
    CORROBORATING_ABSORB,
    PACK_RESET_LAG,
    assess,
    charge_completed_at,
    full_charge_windows,
)
from arraysense.costs import (
    band_intervals,
    bucket_energy,
    period_energy,
    price_period,
    unpriced_minutes,
)
from arraysense.energy import ENERGY_FIELDS, Period, read_energy, resolve_zone, with_zone
from arraysense.metrics import INVERTER_METRICS
from arraysense.settings import SETTING_TIMEZONE, SettingsStore, describe, lookup_setting
from arraysense.store.schema import inverter_metric_columns, module_metric_columns
from arraysense.store.sqlite_store import SqliteStore
from arraysense.store.tiers import select_tier
from arraysense.tariff import (
    SETTING_BANDS,
    CostResult,
    EnergyShortfall,
    PeriodEnergy,
    Tariff,
    apportion_fixed,
    estimate_bill,
    load_tariff,
    merge_shortfalls,
)

if TYPE_CHECKING:
    # For the annotation only. Nothing here calls into the collector: the
    # service arrives on the app's state, already built and already polling,
    # and the one thing asked of it is the verdict it has already reached.
    from arraysense.collector.service import CollectorService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

_INVERTER_NAMES = frozenset(spec.name for spec in INVERTER_METRICS)

# A live view gets everything the inverter reports, not a chosen subset. It is
# one row from one table either way, so narrowing it would save nothing and
# would mean editing this file every time a panel wants a reading it does not
# already have.
_LIVE_INVERTER = tuple(spec.name for spec in INVERTER_METRICS)

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


def _row_time(row: Mapping[str, Any] | None) -> datetime | None:
    """The timestamp of a stored row, or None if there is no row."""
    if row is None:
        return None
    stamp = row.get("timestamp")
    return stamp if isinstance(stamp, datetime) else None


def _newest_reading(store: SqliteStore, now: datetime) -> tuple[datetime | None, bool]:
    """When the newest stored reading was taken, and whether the store holds anything.

    The store's clock, deliberately, and not the collector's. ``last_success``
    lives in the process and comes back None the moment it restarts, so a
    collector crash-looping faster than the staleness threshold read as
    perfectly current every time — which is the one case the warning exists
    for. Rows outlive the process that wrote them.

    A recorded gap is not a reading. It carries a reason and no values, so a
    page drawing it shows dashes, and counting one as data reports a screen
    full of nothing as up to date. No metric columns are asked for: only the
    timestamp and the gap marker decide this.

    Returns no timestamp when every row inside the search window is a gap,
    which is a longer outage rather than a fresh install — the second half of
    the answer tells those apart, and a caller that flattened them would either
    warn about an install that has simply not polled yet or stay quiet through
    an outage that has run all day.
    """
    newest = store.latest([])
    if newest is None:
        return None, False
    if not newest.get("error"):
        return _row_time(newest), True
    for row in reversed(store.query([], now - READING_SEARCH, now)):
        if not row.get("error"):
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
        # The verdict the stale banner prints. Reached here because it is a
        # judgement, and one made in the browser is one that can disagree with
        # the watchdog about whether the collector is running.
        "staleness": _staleness(service, request.app.state.store, datetime.now(tz=UTC)),
    }


@router.get("/live")
async def live(request: Request, device: str | None = None) -> dict[str, Any]:
    """The most recent inverter reading and every battery module's latest.

    What a wall display polls. Absent values stay null — a battery block empty
    because CAN is down must not arrive as 0% state of charge.

    ``device`` names an inverter and defaults to the configured one, so a page
    that sends nothing gets exactly what it always got.
    """
    store = request.app.state.store
    device = _device(device)
    inverter = store.latest(list(_LIVE_INVERTER), device=device)
    modules = store.latest_modules(list(module_metric_columns()), device=device)
    # Named here rather than in the browser. Which flow is powering the house
    # is an interpretation of five readings, and an interpretation computed in
    # two places drifts — the Costs page already proved that with money. The
    # page prints what this says.
    status = operating_mode.assess(inverter or {})
    return {
        "inverter": _isoformat_row(inverter) if inverter else None,
        "modules": [_isoformat_row(m) for m in modules],
        "mode": {
            "mode": status.mode.value,
            "battery": status.battery.value,
            "why": status.why,
            "known": status.known,
        },
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

    ``devices`` is a list because a parallel stack is several inverters behind
    one service, even though today's collector polls one. Three states, kept
    apart on the project's own rule that absent capability is not absent data:
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
    serial = identity.serial if identity is not None else getattr(source, "device", None)
    devices: list[dict[str, Any]] = []
    if serial is not None:
        entry: dict[str, Any] = {
            "device": serial,
            "driver": identity.driver if identity is not None else None,
            "model": identity.model if identity is not None else None,
            "pv_strings": None,
            "energy": None,
            "backup_output": None,
            "generator_input": None,
            "split_phase": None,
            "three_phase": None,
            "parallel_capable": None,
            "per_module_battery": None,
            "metrics": None,
            "battery_module_metrics": None,
        }
        if declared is not None:
            entry.update(
                {
                    "pv_strings": declared.pv_strings,
                    "energy": declared.energy.value,
                    "backup_output": declared.backup_output,
                    "generator_input": declared.generator_input,
                    "split_phase": declared.split_phase,
                    "three_phase": declared.three_phase,
                    "parallel_capable": declared.parallel_capable,
                    "per_module_battery": declared.per_module_battery,
                    "metrics": list(inverter_metric_columns(declared.metrics)),
                    "battery_module_metrics": list(module_metric_columns(declared.metrics)),
                }
            )
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
async def calibration(request: Request) -> dict[str, Any]:
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
    store = request.app.state.store
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

    Identifying values come back masked. There is no authentication here, so
    this answers anything that can reach the port.
    """
    settings = SettingsStore(request.app.state.store)
    return {"fields": describe(), "values": settings.public()}


@router.put("/settings")
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
        changed = settings.update(wanted)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (ValueError, OverflowError) as exc:
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


@router.get("/costs")
async def costs(
    request: Request,
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
    store = request.app.state.store
    settings = SettingsStore(store)
    tariff = load_tariff(settings.all())
    # The installation's zone decides which wall-clock hours a band covers, and
    # ``tz`` only speaks for the browser. This is the endpoint where getting it
    # wrong is a mispriced day rather than a shifted chart.
    zone = _request_zone(store, tz)

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
    try:
        energy = period_energy(tariff, rows, start, end, zone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    result = price_period(tariff, energy, fixed_charge=_month_charge(tariff, start, zone))
    bill = estimate_bill(tariff, energy)

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
        "unpriced_minutes": round(unpriced_minutes(tariff, start, end, zone)),
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


def _band_rows(
    tariff: Tariff, energy: PeriodEnergy, result: CostResult | None
) -> list[dict[str, Any]]:
    """One finished row per band: its label, its energy, and every figure in money.

    Assembled here rather than in the page because the page multiplying a rate
    by a kilowatt-hour is the same mistake as the page parsing a tariff, only
    smaller and harder to spot. Every number below is either measured or
    absent; none of them is a zero standing in for something nobody knew.
    """
    if result is None:
        return []
    house = dict(energy.load_kwh or {})
    battery = dict(energy.battery_discharge_kwh or {})
    by_name = {band.name: band for band in tariff.bands}

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
            }
        )
    return rows


@router.get("/history")
async def history(
    request: Request,
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
    cadence = int(request.app.state.config.poll_interval)
    tier = select_tier(end - start, width_px=width, cadence_seconds=cadence)
    rows = request.app.state.store.query(names, start, end, tier=tier, device=_device(device))
    return {"tier": tier, "count": len(rows), "points": [_isoformat_row(r) for r in rows]}


@router.get("/battery/history")
async def battery_history(
    request: Request,
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
    cadence = int(request.app.state.config.poll_interval)
    tier = select_tier(end - start, width_px=width, cadence_seconds=cadence, module=True)
    rows = request.app.state.store.query_modules(
        names, start, end, tier=tier, serial=serial, device=_device(device)
    )
    return {"tier": tier, "count": len(rows), "points": [_isoformat_row(r) for r in rows]}


@router.get("/energy")
async def energy(
    request: Request,
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
    store = request.app.state.store
    try:
        zone = _request_zone(store, tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start, end = with_zone(start, zone), with_zone(end, zone)
    _check_range(start, end)

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
async def bands(
    request: Request,
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
    store = request.app.state.store
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
    # for the same reason.
    try:
        intervals = band_intervals(tariff, start, end, zone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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


@router.post("/yield")
async def yield_dongle(request: Request, body: YieldRequest) -> dict[str, Any]:
    """Release the dongle so the vendor's app can push a firmware update.

    The dongle accepts one TCP client, so the collector has to let go before
    anything else can connect.
    """
    until = await request.app.state.service.yield_for(body.seconds)
    logger.info("yield requested for %.0fs", body.seconds)
    return {"yielding": True, "seconds": body.seconds, "until": until.isoformat()}


@router.post("/resume")
async def resume(request: Request) -> dict[str, Any]:
    """Take the dongle back before the yield timer runs out."""
    await request.app.state.service.resume()
    return {"yielding": False}


def _isoformat_row(row: dict[str, Any]) -> dict[str, Any]:
    """Render timestamps as ISO strings, leaving everything else alone.

    Nulls stay null rather than becoming zero on the way out; that distinction
    is the whole reason absent readings are stored as NULL.
    """
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in row.items()}
