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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from arraysense import __version__
from arraysense import mode as operating_mode
from arraysense.calibration import assess, full_charge_windows, packs_recalibrated
from arraysense.costs import bucket_energy, period_energy, price_period, unpriced_minutes
from arraysense.energy import ENERGY_FIELDS, Period, read_energy, resolve_zone, with_zone
from arraysense.metrics import INVERTER_METRICS
from arraysense.settings import SettingsStore, describe, lookup_setting
from arraysense.store.schema import module_metric_columns
from arraysense.store.sqlite_store import SqliteStore
from arraysense.store.tiers import select_tier
from arraysense.tariff import (
    SETTING_BANDS,
    CostResult,
    PeriodEnergy,
    Tariff,
    apportion_fixed,
    estimate_bill,
    load_tariff,
)

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
    zone = resolve_zone(tz)

    if tariff is None:
        # Two different situations, and conflating them tells somebody staring
        # at the tariff they just typed that they have not entered one. Text
        # is stored but unusable only for a value saved before the grammar was
        # checked at write time, which is why the reason is worth carrying.
        stored = str(settings.all().get(SETTING_BANDS) or "").strip()
        return {
            "currency": None,
            "configured": False,
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
    priced: bool = False,
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
        zone = resolve_zone(tz)
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    start, end = with_zone(start, zone), with_zone(end, zone)
    _check_range(start, end)

    store = request.app.state.store
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

    payload: dict[str, Any] = {
        "period": period,
        "timezone": str(zone),
        "buckets": [
            {
                "start": bucket.start.isoformat(),
                "end": bucket.end.isoformat(),
                "complete": bucket.complete,
                **bucket.totals,
                **_bucket_money(tariff, money.get(bucket.start)),
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
                period,
                [
                    (splits[bucket.start], money.get(bucket.start))
                    for bucket in read.buckets
                    if bucket.start in splits
                ],
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
) -> dict[str, float | None]:
    """Add each band's kilowatt-hours across several buckets, keeping unknown unknown.

    A band one bucket could not measure makes that band unknown for the whole
    run rather than the sum of the buckets that did report it — which would be
    a missing reading rendered as a smaller number, at the point where it turns
    into money. A band simply absent from a bucket is different and is skipped:
    the day before the season turns never entered the peak window, so it has
    nothing to contribute rather than something nobody watched.
    """
    out: dict[str, float | None] = {}
    for part in parts:
        for name, kwh in (part or {}).items():
            running = out.get(name, 0.0)
            out[name] = None if kwh is None or running is None else running + kwh
    return out


def _price_together(
    tariff: Tariff, period: Period, spans: Sequence[PeriodEnergy]
) -> CostResult | None:
    """Price a run of buckets as one period rather than adding up their costs.

    ``spans`` arrives in calendar order, so the combined period runs from the
    first bucket's start to the last one's end without comparing two datetimes
    that share a zone — a comparison Python answers off the wall clock, which
    is the trap every other duration in this project goes out of its way to
    avoid.

    The connection charge is summed from the buckets rather than apportioned
    across the span, because the span may have holes in it. A month missing its
    fifteenth owes thirty days of the charge, not thirty-one, and apportioning
    over the whole month would quietly charge for the day nobody could price.
    """
    if not spans:
        return None
    return price_period(
        tariff,
        PeriodEnergy(
            start=spans[0].start,
            end=spans[-1].end,
            grid_import_kwh=_merge_bands(span.grid_import_kwh for span in spans),
            load_kwh=_merge_bands(span.load_kwh for span in spans) or None,
            battery_discharge_kwh=_merge_bands(span.battery_discharge_kwh for span in spans)
            or None,
        ),
        fixed_charge=sum(_bucket_fixed(tariff, period, span) for span in spans),
    )


def _period_total(
    tariff: Tariff | None,
    period: Period,
    buckets: Sequence[tuple[PeriodEnergy, CostResult | None]],
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

    Only the buckets the service could price are in it. A month with a day
    nobody measured does not cost the sum of the days that were, and this says
    so by covering fewer rows rather than by treating the hole as free; a span
    where nothing could be priced has no total at all, which is a dash and
    never a zero.

    Cost and savings are totalled over their own rows, because they can be
    knowable for different ones. A counter reset takes one column backwards and
    not the other, leaving a day whose import is readable and whose house load
    is not — that day has a cost and no statable saving, and dropping it from
    both totals would understate the bill it is part of.
    """
    if tariff is None:
        return {}
    costed = [energy for energy, result in buckets if result and result.cost is not None]
    saving = [energy for energy, result in buckets if result and result.savings is not None]
    whole = _price_together(tariff, period, costed)
    against = _price_together(tariff, period, saving)
    return {
        "cost": whole.cost if whole else None,
        "energy_cost": whole.energy_cost if whole else None,
        "fixed_charge": whole.fixed_charge if whole else None,
        "saved": against.savings if against else None,
        "no_solar_cost": against.no_solar_cost if against else None,
    }


def _bucket_money(tariff: Tariff | None, result: CostResult | None) -> dict[str, Any]:
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
    """
    if tariff is None:
        return {}
    if result is None:
        return {
            "cost": None,
            "energy_cost": None,
            "fixed_charge": None,
            "saved": None,
            "no_solar_cost": None,
        }
    return {
        "cost": result.cost,
        "energy_cost": result.energy_cost,
        "fixed_charge": result.fixed_charge,
        "saved": result.savings,
        "no_solar_cost": result.no_solar_cost,
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
