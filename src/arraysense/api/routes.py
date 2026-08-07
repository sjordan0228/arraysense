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
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from arraysense import __version__
from arraysense.calibration import assess, full_charge_windows, packs_recalibrated
from arraysense.energy import Period, energy_totals, resolve_zone, with_zone
from arraysense.metrics import INVERTER_METRICS
from arraysense.settings import SettingsStore, describe, lookup_setting
from arraysense.store.schema import module_metric_columns
from arraysense.store.sqlite_store import SqliteStore
from arraysense.store.tiers import select_tier

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
# per-window module reads that follow.
_MAX_WINDOWS_EXAMINED = 40


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


def _check_range(start: datetime, end: datetime) -> None:
    """Reject a range that ends before it starts."""
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be after start")


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Whether the collector is alive, connected, and holding the dongle."""
    service = request.app.state.service
    s = service.status
    return {
        "version": __version__,
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
    }


@router.get("/live")
async def live(request: Request) -> dict[str, Any]:
    """The most recent inverter reading and every battery module's latest.

    What a wall display polls. Absent values stay null — a battery block empty
    because CAN is down must not arrive as 0% state of charge.
    """
    store = request.app.state.store
    inverter = store.latest(list(_LIVE_INVERTER))
    modules = store.latest_modules(list(module_metric_columns()))
    return {
        "inverter": _isoformat_row(inverter) if inverter else None,
        "modules": [_isoformat_row(m) for m in modules],
    }


def _packs_during(store: SqliteStore, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Read every module's state of charge across one absorb window.

    Tries the full-cadence tier first and falls back to hourly. Raw module data
    is kept for thirty days and the search reaches back sixty, so the older
    half of the range can only be answered from the rollup — where a pack that
    held at full for an hour still averages near 100, and one that touched it
    briefly does not. That errs towards not claiming a full charge, which is
    the right direction: the cost of missing one is a warning shown a few days
    early, and the cost of inventing one is silence for a month.

    Both bounds are timezone-aware, and the result is empty when no pack
    reported at all during the window — which is a different thing from every
    pack reporting and none of them being full.
    """
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

    last_full: datetime | None = None
    for window_start, window_end in reversed(full_charge_windows(history)[-_MAX_WINDOWS_EXAMINED:]):
        during = _packs_during(store, window_start, window_end)
        if packs_recalibrated(during, expected=known or None):
            last_full = window_end
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
        "restart_required": any(not k.startswith("display.") for k in changed),
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


@router.get("/history")
async def history(
    request: Request,
    start: datetime,
    end: datetime,
    metrics: str,
    width: int = Query(default=1000, ge=1, le=10000),
) -> dict[str, Any]:
    """Inverter metrics over a range, at a resolution that suits the chart."""
    _check_range(start, end)
    names = _parse_metrics(metrics, _INVERTER_NAMES, "inverter")
    cadence = int(request.app.state.config.poll_interval)
    tier = select_tier(end - start, width_px=width, cadence_seconds=cadence)
    rows = request.app.state.store.query(names, start, end, tier=tier)
    return {"tier": tier, "count": len(rows), "points": [_isoformat_row(r) for r in rows]}


@router.get("/battery/history")
async def battery_history(
    request: Request,
    start: datetime,
    end: datetime,
    metrics: str = "soc_pct",
    width: int = Query(default=1000, ge=1, le=10000),
    serial: str | None = None,
) -> dict[str, Any]:
    """Per-module battery readings over a range, keyed by serial.

    Modules are identified by serial rather than slot, so a bank that rotates
    modules through the inverter's four register slots neither splits one
    battery into two series nor merges two into one.
    """
    _check_range(start, end)
    names = _parse_metrics(metrics, set(module_metric_columns()), "module")
    cadence = int(request.app.state.config.poll_interval)
    tier = select_tier(end - start, width_px=width, cadence_seconds=cadence, module=True)
    rows = request.app.state.store.query_modules(names, start, end, tier=tier, serial=serial)
    return {"tier": tier, "count": len(rows), "points": [_isoformat_row(r) for r in rows]}


@router.get("/energy")
async def energy(
    request: Request,
    start: datetime,
    end: datetime,
    period: Period = "day",
    tz: str | None = None,
) -> dict[str, Any]:
    """Energy per calendar day or month, in kWh, over the owner's own calendar.

    Read off the inverter's lifetime counters rather than integrated from
    stored power, so a period containing a collection outage still totals what
    actually happened. Each bucket says whether it is whole: the one in
    progress is not, nor is the first one if collection started partway into
    it, and a bucket that reads low for that reason must not be presented as a
    quiet day.

    ``tz`` is an IANA zone name and decides where midnight falls, defaulting to
    the machine's own zone. Naive timestamps in ``start`` and ``end`` are read
    in that same zone rather than the server's, since otherwise the answer
    depends on where the service happens to be installed. A bucket nothing was
    recorded for is left out of the reply rather than returned as zero.
    """
    try:
        zone = resolve_zone(tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start, end = with_zone(start, zone), with_zone(end, zone)
    _check_range(start, end)
    buckets = energy_totals(request.app.state.store, start, end, period=period, zone=zone)
    return {
        "period": period,
        "timezone": str(zone),
        "buckets": [
            {
                "start": bucket.start.isoformat(),
                "end": bucket.end.isoformat(),
                "complete": bucket.complete,
                **bucket.totals,
            }
            for bucket in buckets
        ],
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
