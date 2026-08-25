"""guard.py — hold the inverter under the owner's limit by moving the car's amps.

The guard is a control loop that reads how much power the inverter itself is
supplying, compares it against a limit the owner sets, and moves the EV charger's
amps to keep the inverter under that limit. It cuts fast and releases slowly: a
cut is taken immediately, but the rate only goes back up after the house has been
continuously clear for five minutes.

The pure half — allowance() and plan() — imports nothing from the store, the
client, or asyncio. It mirrors control.py, whose docstring explains why:
everything there is a decision, which is what lets the safety rules be stated as
facts in a test rather than assembled out of fixtures.

The impure half — InverterGuard — is the loop. It reads from the store and the
poller, decides what to do through decide(), and writes only when it has a verdict.
It never raises: every expected failure becomes a hold, and the loop keeps going.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from arraysense.modules.emporia.client import (
    EmporiaAuthExpiredError,
    EmporiaUnreachableError,
)
from arraysense.modules.emporia.control import APP, Limits, decide
from arraysense.modules.emporia.parse import ChargerState
from arraysense.modules.emporia.poller import LOOP_RESTART_SECONDS, STORE_ERRORS
from arraysense.modules.emporia.repository import MODULE, ChargerAudit, CircuitRepository
from arraysense.settings import (
    CHARGE_DEFAULT_KEY,
    EMPORIA_ENABLED_KEY,
    INVERTER_LIMIT_KEY,
    SettingsStore,
    emporia_interval_seconds,
)
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


GUARD_INTERVAL_SECONDS = 10
MARGIN_W = 1000
SETTLE_SECONDS = 90
RELEASE_HOLD_SECONDS = 300
INVERTER_STALE_SECONDS = 60
EVSE_STALE_MULTIPLE = 2


class PollerLike(Protocol):
    """The part of EmporiaPoller the guard needs, so a test can fake it."""

    charger: ChargerState | None
    repository: CircuitRepository
    audit: ChargerAudit

    def limits(self) -> Limits:
        """The floor, ceiling and hardware maximum for this charger."""

    def override_until(self) -> datetime | None:
        """When the owner's manual override stops holding, or None."""

    def write_rate(self, amps: int) -> ChargerState | None:
        """Command a charge rate and return the charger's confirmed state."""


@dataclass(frozen=True)
class GuardReading:
    """What one tick of the guard managed to read."""

    load_w: float | None
    grid_w: float | None
    eps_voltage_v: float | None
    grid_voltage_v: float | None
    charger_w: int | None
    rate_a: int | None


@dataclass(frozen=True)
class Allowance:
    """How many amps the car may have, or None when nothing can be said."""

    amps: int | None
    supplied_w: int | None
    charger_w: int | None
    limit_w: int
    reason: str


@dataclass(frozen=True)
class GuardPlan:
    """What the guard wants done about it."""

    amps: int | None  # None means do nothing
    kind: str  # "cut" | "release" | "hold"
    reason: str


def allowance(reading: GuardReading, *, limit_w: int) -> Allowance:
    """Decide how many amps the car may have, or hold when nothing can be said.

    The arithmetic is pinned: supplied_w = round(load_w - max(grid_w, 0.0)),
    other_w = max(supplied_w - charger_w, 0), room_w = limit_w - MARGIN_W - other_w,
    and allowed_a = int(room_w // volts). The voltage is eps_voltage_v if present
    and > 0, else grid_voltage_v if present and > 0, else absent.

    Every hold path returns an Allowance with amps=None and supplied_w=None,
    carrying the reason that explains which condition fired. The success path
    carries an empty reason string — plan() supplies the sentence that gets shown.
    """
    if limit_w <= 0:
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=reading.charger_w,
            limit_w=limit_w,
            reason="the inverter limit is off",
        )
    if reading.load_w is None:
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=reading.charger_w,
            limit_w=limit_w,
            reason="the house reading is absent",
        )
    if reading.grid_w is None:
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=reading.charger_w,
            limit_w=limit_w,
            reason="the grid reading is absent",
        )
    if (reading.eps_voltage_v is None or reading.eps_voltage_v <= 0) and (
        reading.grid_voltage_v is None or reading.grid_voltage_v <= 0
    ):
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=reading.charger_w,
            limit_w=limit_w,
            reason="the voltage reading is absent",
        )
    if reading.charger_w is None:
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=None,
            limit_w=limit_w,
            reason="the charger reading is absent",
        )
    if reading.rate_a is None:
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=reading.charger_w,
            limit_w=limit_w,
            reason="the charge rate is unknown",
        )

    supplied_w = round(reading.load_w - max(reading.grid_w, 0.0))
    other_w = max(supplied_w - reading.charger_w, 0)
    volts = (
        reading.eps_voltage_v
        if reading.eps_voltage_v is not None and reading.eps_voltage_v > 0
        else (
            reading.grid_voltage_v
            if reading.grid_voltage_v is not None and reading.grid_voltage_v > 0
            else None
        )
    )
    if volts is None:
        return Allowance(
            amps=None,
            supplied_w=None,
            charger_w=reading.charger_w,
            limit_w=limit_w,
            reason="the voltage reading is absent",
        )

    room_w = limit_w - MARGIN_W - other_w
    allowed_a = int(room_w // volts)
    return Allowance(
        amps=allowed_a,
        supplied_w=supplied_w,
        charger_w=reading.charger_w,
        limit_w=limit_w,
        reason="",
    )


def plan(
    allowed: Allowance,
    *,
    rate_a: int | None,
    default_a: int,
    settled: bool,
    safe_for_s: float | None,
) -> GuardPlan:
    """Decide what to do about an allowance, in exactly the pinned order.

    The settle check sits above the cut check on purpose: a cut computed from a
    reading the car has not responded to yet is the oscillation this feature would
    otherwise have. The release waits for the house to stay clear, and is capped
    at the owner's default.

    The reason on a success path is one sentence, not two: plan() decides and
    names the action, so allowance()'s reason is never echoed.
    """
    if allowed.amps is None:
        return GuardPlan(amps=None, kind="hold", reason=allowed.reason)
    if rate_a is None:
        return GuardPlan(amps=None, kind="hold", reason="the charge rate is unknown")
    if not settled:
        return GuardPlan(amps=None, kind="hold", reason="settling after the last change")
    if allowed.amps < rate_a:
        return GuardPlan(
            amps=allowed.amps,
            kind="cut",
            reason=(
                f"inverter supplying {allowed.supplied_w} W against a {allowed.limit_w} W limit"
            ),
        )
    if safe_for_s is None or safe_for_s < RELEASE_HOLD_SECONDS:
        return GuardPlan(amps=None, kind="hold", reason="waiting for the house to stay clear")
    target = min(allowed.amps, default_a)
    if target <= rate_a:
        return GuardPlan(
            amps=None,
            kind="hold",
            reason="already at the rate the house can afford",
        )
    return GuardPlan(
        amps=target,
        kind="release",
        reason=(
            f"inverter supplying {allowed.supplied_w} W, clear of the "
            f"{allowed.limit_w} W limit for {int(safe_for_s // 60)} minutes"
        ),
    )


class InverterGuard:
    """Hold the inverter under the owner's limit by moving the car's amps.

    The guard ticks every ten seconds, reads the inverter row and the charger's
    own circuit reading, computes an allowance, decides a plan through decide(),
    and writes only when the verdict says so. It never raises: every expected
    failure becomes a hold and the loop keeps going.

    last_plan and last_allowance are updated on every tick so the endpoint can
    report without recomputing anything. A disabled module clears both to None,
    and a zero limit holds with the "the inverter limit is off" reason.
    """

    def __init__(self, poller: PollerLike, store: SqliteStore) -> None:
        """Wire the guard to its poller and its store."""
        self._poller = poller
        self._store = store
        self._settings = SettingsStore(store)
        self._last_plan: GuardPlan | None = None
        self._last_allowance: Allowance | None = None
        self._safe_since: datetime | None = None
        self._settled_until: datetime | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def last_plan(self) -> GuardPlan | None:
        """The guard's newest plan, or None when the module is disabled."""
        return self._last_plan

    @property
    def last_allowance(self) -> Allowance | None:
        """The guard's newest allowance, or None when the module is disabled."""
        return self._last_allowance

    def read(self, now: datetime) -> GuardReading:
        """Gather one tick's worth of readings, applying staleness rules."""
        inverter_row = self._store.latest(
            ["load_power_w", "grid_power_w", "eps_voltage_v", "grid_voltage_v"],
            include_gaps=False,
        )
        if inverter_row is None:
            return GuardReading(
                load_w=None,
                grid_w=None,
                eps_voltage_v=None,
                grid_voltage_v=None,
                charger_w=None,
                rate_a=None,
            )
        row_ts = inverter_row.get("timestamp")
        if not isinstance(row_ts, datetime):
            return GuardReading(
                load_w=None,
                grid_w=None,
                eps_voltage_v=None,
                grid_voltage_v=None,
                charger_w=None,
                rate_a=None,
            )
        if (now - row_ts).total_seconds() > INVERTER_STALE_SECONDS:
            return GuardReading(
                load_w=None,
                grid_w=None,
                eps_voltage_v=None,
                grid_voltage_v=None,
                charger_w=None,
                rate_a=None,
            )

        load_w = inverter_row.get("load_power_w")
        grid_w = inverter_row.get("grid_power_w")
        eps_voltage_v = inverter_row.get("eps_voltage_v")
        grid_voltage_v = inverter_row.get("grid_voltage_v")

        charger_w = self._read_charger_w(now)
        rate_a = getattr(self._poller.charger, "rate_a", None) if self._poller.charger else None

        return GuardReading(
            load_w=float(load_w) if isinstance(load_w, (int, float)) else None,
            grid_w=float(grid_w) if isinstance(grid_w, (int, float)) else None,
            eps_voltage_v=(
                float(eps_voltage_v) if isinstance(eps_voltage_v, (int, float)) else None
            ),
            grid_voltage_v=(
                float(grid_voltage_v) if isinstance(grid_voltage_v, (int, float)) else None
            ),
            charger_w=charger_w,
            rate_a=int(rate_a) if isinstance(rate_a, int) else None,
        )

    def _read_charger_w(self, now: datetime) -> int | None:
        """Read the charger's own circuit draw, or None when absent or stale."""
        circuits = self._poller.repository.latest()

        if self._poller.charger is None:
            return None
        device_gid = self._poller.charger.device_gid

        matches = [c for c in circuits if getattr(c, "device_gid", None) == device_gid]
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "multiple charger circuits match device_gid %d; using the first",
                device_gid,
            )

        circuit = matches[0]
        ts = getattr(circuit, "ts", None)
        if ts is None:
            return None

        interval = emporia_interval_seconds(self._settings)
        if (now.timestamp() - ts) > EVSE_STALE_MULTIPLE * interval:
            return None

        watts = getattr(circuit, "watts", None)
        if watts is None:
            return None
        return int(watts)

    async def tick(self, now: datetime) -> None:
        """One cycle. Never raises: every expected failure becomes a hold."""
        if not bool(self._settings.get(EMPORIA_ENABLED_KEY)):
            self._last_plan = None
            self._last_allowance = None
            return

        limit_w = self._settings.get(INVERTER_LIMIT_KEY)
        if not isinstance(limit_w, int) or limit_w <= 0:
            # A setting somebody hand-edited into the database must not be able
            # to stop the guard from running — pass a genuine int so allowance()
            # never sees a non-numeric value.
            safe_limit = limit_w if isinstance(limit_w, int) else 0
            self._last_allowance = allowance(
                GuardReading(
                    load_w=None,
                    grid_w=None,
                    eps_voltage_v=None,
                    grid_voltage_v=None,
                    charger_w=None,
                    rate_a=None,
                ),
                limit_w=safe_limit,
            )
            self._last_plan = GuardPlan(amps=None, kind="hold", reason="the inverter limit is off")
            return

        reading = self.read(now)
        allowed = allowance(reading, limit_w=limit_w)

        # Update the safe clock before plan() uses it.
        if allowed.amps is None or (reading.rate_a is not None and allowed.amps < reading.rate_a):
            self._safe_since = None
        elif self._safe_since is None:
            self._safe_since = now

        safe_for_s: float | None = None
        if self._safe_since is not None:
            safe_for_s = (now - self._safe_since).total_seconds()

        default_a = self._settings.get(CHARGE_DEFAULT_KEY)
        if not isinstance(default_a, int):
            default_a = 32

        settled = self._settled_until is None or now >= self._settled_until
        p = plan(
            allowed,
            rate_a=reading.rate_a,
            default_a=default_a,
            settled=settled,
            safe_for_s=safe_for_s,
        )

        self._last_allowance = allowed
        self._last_plan = p

        if p.amps is None:
            return

        authority = self._settings.get("emporia.charger_authority")
        if not isinstance(authority, str):
            authority = APP

        verdict = decide(
            p.amps,
            authority=authority,
            limits=self._poller.limits(),
            now=now,
            override_until=self._poller.override_until(),
        )

        if not verdict.apply:
            level = logging.WARNING if (verdict.refused and p.kind == "cut") else logging.INFO
            logger.log(level, "%s", verdict.reason)
            return

        if verdict.rate_a is None or verdict.rate_a == reading.rate_a:
            return

        try:
            confirmed: ChargerState | None = await asyncio.to_thread(
                self._poller.write_rate,
                verdict.rate_a,
            )
        except (EmporiaUnreachableError, EmporiaAuthExpiredError) as exc:
            logger.warning("guard write failed: %s", exc)
            if self._poller.charger is None:
                return
            self._poller.audit.record_change(
                self._poller.charger.device_gid,
                from_a=reading.rate_a,
                to_a=verdict.rate_a,
                reason=f"failed: {exc}",
                applied=False,
                source=MODULE,
                now=now,
            )
            self._settled_until = now + timedelta(seconds=SETTLE_SECONDS)
            return

        took = confirmed is not None and getattr(confirmed, "rate_a", None) == verdict.rate_a
        if self._poller.charger is None:
            return
        self._poller.audit.record_change(
            self._poller.charger.device_gid,
            from_a=reading.rate_a,
            to_a=verdict.rate_a,
            reason=p.reason + (f" ({verdict.refused})" if verdict.refused else ""),
            applied=took,
            source=MODULE,
            now=now,
        )
        self._settled_until = now + timedelta(seconds=SETTLE_SECONDS)

    async def start(self) -> None:
        """Begin polling. Safe to call when the module is disabled."""
        if self._task is None:
            self._task = asyncio.create_task(self._supervise())

    async def stop(self) -> None:
        """Stop polling and wait for the loop to finish."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick(datetime.now(tz=UTC))
            except STORE_ERRORS as exc:
                logger.warning("guard tick failed: %s", exc)
            await asyncio.sleep(GUARD_INTERVAL_SECONDS)

    async def _supervise(self) -> None:
        while True:
            try:
                await self._loop()
            except asyncio.CancelledError:
                raise
            except STORE_ERRORS as exc:
                logger.warning("guard loop ended unexpectedly, restarting: %s", exc)
            await asyncio.sleep(LOOP_RESTART_SECONDS)
