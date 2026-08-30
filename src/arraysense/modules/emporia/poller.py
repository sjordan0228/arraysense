"""poller.py — read Emporia on its own clock, without ever touching the inverter.

Modelled on ``collector/weather.py``, which solved this shape first: a second
writer whose failure must not implicate the first. Emporia's cloud being down,
slow, or refusing the credential costs solar collection nothing at all, and that
is the module's central promise rather than a nicety.

The HTTP runs in a worker thread. ``urllib`` blocks, and a blocking call on the
event loop stalls every open page for the length of the timeout. ``asyncio.to_thread``
keeps the loop free for the fraction of a second the calls take.

Settings are the enable and are read fresh every tick, so switching the module
on or off takes effect within one interval and needs no restart. A disabled tick
makes no call and writes no row.

Nothing above this loop watches it: the service's watchdog follows the inverter
collector alone, and circuits live in their own tables precisely so they can
never satisfy the store's staleness witness. So the poller says for itself what
state it is in, and ``state`` is what the API reports.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from arraysense.modules.emporia import tokens
from arraysense.modules.emporia.client import (
    EmporiaAuthExpiredError,
    EmporiaChallengeError,
    EmporiaClient,
    EmporiaUnreachableError,
)
from arraysense.modules.emporia.control import (
    APP,
    Limits,
    decide,
    restore_target,
)
from arraysense.modules.emporia.parse import (
    SCALE_MINUTE,
    ChargerState,
    DeviceConnection,
    charger_from_status,
    circuits_from_devices,
    connections_from_status,
    device_gids,
    readings_from_usage,
)
from arraysense.modules.emporia.repository import MODULE, ChargerAudit, CircuitRepository
from arraysense.settings import (
    CHARGE_CEILING_KEY,
    CHARGE_DEFAULT_KEY,
    CHARGE_FLOOR_KEY,
    CHARGE_OVERRIDE_UNTIL_KEY,
    CHARGER_AUTHORITY_KEY,
    EMPORIA_ENABLED_KEY,
    SettingsStore,
    emporia_interval_seconds,
)
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


def _raw_charger(status: object) -> dict[str, object] | None:
    """The charger's record exactly as Emporia sent it, for echoing on a write.

    Kept apart from ``ChargerState`` on purpose. This one carries ``breakerPIN``
    — which the write needs and nothing else may see — so it is held privately
    by the poller and read only by the one call that sends it straight back.
    """
    if not isinstance(status, dict):
        return None
    chargers = status.get("evChargers")
    if not isinstance(chargers, list) or not chargers:
        return None
    record = chargers[0]
    return dict(record) if isinstance(record, dict) else None


# The same tuple the other writers catch. Declared here rather than imported so
# this module stays separable from the collector; a busy database is the same
# condition whichever writer met it.
STORE_ERRORS = (sqlite3.Error,)

# How long before restarting a loop that ended. The loop is not supposed to be
# able to end — every expected failure is caught inside it — so this covers only
# what nobody predicted. The alternative is a module that stops for the life of
# the process with no symptom at all.
LOOP_RESTART_SECONDS = 5.0

# How often the circuit list is re-read. Names change rarely and a device list
# costs a call; daily is often enough to notice a rename or a new monitor
# without spending a call a minute on a question whose answer almost never
# changes.
NAME_SYNC_SECONDS = 24 * 60 * 60


class EmporiaApi(Protocol):
    """The two calls a tick makes, so a test can answer them without a network.

    Narrower than ``EmporiaClient`` on purpose: the poller is about timing and
    state, and stating exactly what it needs is what lets the tests hand it
    something that is not an HTTP client at all.
    """

    def login(self, email: str, password: str) -> tokens.TokenSet:
        """Exchange a password for tokens. Used by the login route, not by a tick."""
        ...

    def refresh(self, token_set: tokens.TokenSet) -> tokens.TokenSet:
        """Renew the hour-long id token."""
        ...

    def get(self, path: str, id_token: str) -> object:
        """One authenticated GET."""
        ...

    def set_charge_rate(self, record: dict[str, object], amps: int, id_token: str) -> object:
        """Command a charge rate, echoing the charger's whole record back."""
        ...

    def write_charger(
        self, record: dict[str, object], changes: dict[str, object], id_token: str
    ) -> object:
        """Change fields on the charger, echoing its whole record back."""
        ...


@dataclass(frozen=True)
class PollerState:
    """What the module is doing, in terms a page can render.

    ``reconnect_required`` is deliberately not an error. It is the one state
    only the owner can clear, and conflating it with ``unreachable`` is the
    defect that makes a flaky connection look like being logged out.
    """

    status: str
    detail: str
    last_success: int | None


class EmporiaPoller:
    """Read circuits from Emporia every interval, while the module is enabled."""

    def __init__(
        self,
        store: SqliteStore,
        token_path: Path,
        client: EmporiaApi | None = None,
    ) -> None:
        """Wire the poller to its store, its credential, and its client."""
        self._store = store
        self._settings = SettingsStore(store)
        # Public because the login and disconnect routes need both, and a route
        # reaching into a private name is how a refactor breaks an endpoint
        # silently. They are the module's seam, not its internals.
        self.token_path = token_path
        self.client: EmporiaApi = client if client is not None else EmporiaClient()
        self.repository = CircuitRepository(store)
        self.audit = ChargerAudit(store)
        # The charger as it was last read, for the page and for the restore.
        # None on an account that has none, which is most of them.
        self.charger: ChargerState | None = None
        # Which devices are still answering Emporia, keyed by device gid. Held
        # rather than stored, like the charger: it is what Emporia says now, and
        # a stored copy would keep calling a device offline after it came back.
        # Empty until the first successful read, which reads as "nothing known"
        # rather than as "everything is fine".
        self.connections: dict[int, DeviceConnection] = {}
        # Restore is a startup behaviour, not a per-tick one. Repeating it
        # every minute would fight the owner for the slider all afternoon.
        self._restore_considered = False
        # The charger's raw record, kept only so a write can echo it back
        # whole — it carries the breaker PIN, so nothing else reads it and
        # it never reaches a page, a log or the audit.
        self._charger_record: dict[str, object] | None = None
        # The id token from the last successful refresh, so a write from a
        # route does not have to refresh again to send one field.
        self._id_token: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._state = PollerState("off", "", None)
        self._names_synced_at: float | None = None
        # The devices the usage call asks about. Held between ticks because
        # the device list is read daily while usage is read every interval,
        # so the identifiers have to outlive the call that found them.
        self._gids: tuple[int, ...] = ()

    @property
    def state(self) -> PollerState:
        """What to tell anybody who asks how the module is faring."""
        return self._state

    def _interval(self) -> float:
        return float(emporia_interval_seconds(self._settings))

    def override_until(self) -> datetime | None:
        """When the owner's hand stops holding the charger, or None if it is not.

        Stored as a Unix second by the route that sets a rate, and read back
        here as an aware UTC instant because everything it is compared against
        is one. A naive datetime would compare against the wall clock of
        whichever machine this is, which is the class of mistake this project
        has already paid for once in the tariff bands.

        A second no clock can represent is held rather than ignored. Settings
        are decoded on read but not clamped to their registered bounds, so a
        database written by another build — or edited by hand — can hand this a
        number ``fromtimestamp`` refuses; and reading that as "no override"
        would fail open, towards writing to somebody's car. It would also have
        raised out of the poller's own task, which catches store errors and
        nothing else, and stopped the module for the life of the process with
        no symptom anywhere.
        """
        value = self._settings.get(CHARGE_OVERRIDE_UNTIL_KEY)
        if not isinstance(value, int) or isinstance(value, bool):
            return None
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            logger.warning(
                "%s holds %r, which is not a time; holding the charger rather than acting",
                CHARGE_OVERRIDE_UNTIL_KEY,
                value,
            )
            return datetime.max.replace(tzinfo=UTC)

    def limits(self) -> Limits:
        """The owner's floor and ceiling, and what the charger admits."""
        floor = self._settings.get(CHARGE_FLOOR_KEY)
        ceiling = self._settings.get(CHARGE_CEILING_KEY)
        return Limits(
            floor_a=int(floor) if isinstance(floor, int) else 6,
            ceiling_a=int(ceiling) if isinstance(ceiling, int) else 32,
            hardware_max_a=self.charger.max_rate_a if self.charger else None,
        )

    def write_charger(self, changes: dict[str, object]) -> ChargerState | None:
        """Send changes to the charger, then read it back and return what it says.

        Blocking; callers move it off the loop. No policy here — the caller has
        already decided. It exists so the raw record, which carries the breaker
        PIN, never leaves this object.

        **The read-back is not optional.** A 200 from the write means Emporia
        accepted the request, not that the charger took the value; the prototype
        established that the only way to know is to ask again. Auditing a change
        as applied on the strength of the status code alone was exactly the
        mistake that made a working write look like a failed one the first time
        this ran against a real car — and a restore that trusts a rate the
        charger is not actually at will leave it there.

        The freshly read state replaces the cached one, so a page asking a
        moment later sees what just happened rather than the last poll's answer.
        """
        if self._charger_record is None or self._id_token is None:
            raise EmporiaUnreachableError("the charger has not been read yet")
        self.client.write_charger(self._charger_record, changes, self._id_token)
        status = self.client.get("/customers/devices/status", self._id_token)
        self.charger = charger_from_status(status)
        self.connections = connections_from_status(status)
        self._charger_record = _raw_charger(status)
        return self.charger

    def write_rate(self, amps: int) -> ChargerState | None:
        """Command a rate and read the charger back. See ``write_charger``."""
        return self.write_charger({"chargingRate": amps})

    def _consider_restore(self, id_token: str, now: datetime) -> None:
        """Put a rate back if this service set it and has no reason to hold it.

        The behaviour the whole control stage exists for: a service that died
        mid-throttle leaves a car at the floor until somebody walks out to it.

        Considered once per process, because it is a startup behaviour. And
        gated on authority, because a module that may not write must not write
        here either — under advisory it records what it would have done, which
        is what lets the page say "the charger is at a rate I set" without
        touching it.

        A proposal identical to the newest audit line is not written down again.
        Under an authority that may not write nothing about the charger moves,
        so the same sentence was recorded on every restart for ever.

        **The override window was the argument nobody passed.** ``decide``
        accepts it and implements the hold correctly, and this call left it out,
        so a service that restarted inside the window put back a rate the owner
        had set by hand an hour earlier. The window is read here rather than
        remembered from the write that opened it, because a restart is exactly
        what loses anything remembered.

        A held restore is still written down. Under an authority that may write,
        "restored to 32 A on startup: override holds until 18:29" is the module
        saying out loud that it saw a rate of its own and left it alone — which
        is what somebody reading the history after a restart wants to know.
        """
        # The enable is read again here, not only at the top of the tick. A tick
        # awaits a token refresh and two HTTP calls between the two points, and
        # the setting is written from the web server's threadpool — so somebody
        # switching the module off mid-poll had their charger written to
        # afterwards by a decision taken while it was still on. Checked before
        # ``_restore_considered`` is set, so a restore skipped for this reason
        # is still considered once the module comes back.
        charger = self.charger
        if charger is None or self._restore_considered:
            return
        if not bool(self._settings.get(EMPORIA_ENABLED_KEY)):
            return
        self._restore_considered = True
        default = self._settings.get(CHARGE_DEFAULT_KEY)
        default_a = int(default) if isinstance(default, int) else 32
        target = restore_target(
            charger_rate_a=charger.rate_a,
            last_set_a=self.audit.last_applied_rate(charger.device_gid),
            default_a=default_a,
        )
        if target is None:
            return
        authority = self._settings.get(CHARGER_AUTHORITY_KEY)
        verdict = decide(
            target,
            authority=authority if isinstance(authority, str) else APP,
            limits=self.limits(),
            now=now,
            override_until=self.override_until(),
        )
        reason = f"restored to {verdict.rate_a} A on startup: {verdict.reason}"
        # A proposal that has not changed is not news. Under an authority that
        # may not write, nothing about the charger moves, so the identical
        # sentence was written down on every restart for ever — and the audit is
        # read to answer "what has this service done to my car", which a hundred
        # copies of one line answers less well than none. An *applied* change is
        # never suppressed: that one did something, and it happened again.
        previous = self.audit.last_change(charger.device_gid)
        if (
            not verdict.apply
            and previous is not None
            and previous.same_decision(
                from_a=charger.rate_a,
                to_a=verdict.rate_a,
                reason=reason,
                applied=verdict.apply,
                source=MODULE,
            )
        ):
            return
        if verdict.apply and verdict.rate_a is not None and self._charger_record is not None:
            self.client.set_charge_rate(self._charger_record, verdict.rate_a, id_token)
        self.audit.record_change(
            charger.device_gid,
            from_a=charger.rate_a,
            to_a=verdict.rate_a,
            reason=reason,
            applied=verdict.apply,
            # This one is the module's, and saying so is what lets the *next*
            # restart tell it from a rate the owner chose. A line recorded
            # without it would be read as nobody's, and never put back.
            source=MODULE,
            now=now,
        )

    def _fetch(self, id_token: str, want_names: bool) -> tuple[object | None, object | None]:
        """Every blocking call of one tick, so one to_thread covers them all.

        The device list comes first when it is wanted, because the usage call
        cannot be made without the identifiers it carries: asked without a
        ``deviceGids`` parameter the endpoint answers HTTP 400 and says
        "Could not get attribute 'deviceGids' from input" (measured against the
        real API, 16 August 2026). With no identifiers at all there is nothing
        to ask about, so the usage call is skipped rather than sent to be
        refused.
        """
        devices = self.client.get("/customers/devices", id_token) if want_names else None
        if devices is not None:
            self._gids = device_gids(devices)
        if not self._gids:
            return devices, None
        instant = datetime.now(tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
        asked = "+".join(str(gid) for gid in self._gids)
        usage = self.client.get(
            "/AppAPI?apiMethod=getDeviceListUsages"
            f"&deviceGids={asked}&instant={instant}"
            f"&scale={SCALE_MINUTE}&energyUnit=KilowattHours",
            id_token,
        )
        # The charger, and whoever else is driving it. Read in the same worker
        # as the rest, because it is the same blocking library and the event
        # loop should wait once rather than three times.
        status = self.client.get("/customers/devices/status", id_token)
        self.charger = charger_from_status(status)
        self.connections = connections_from_status(status)
        self._charger_record = _raw_charger(status)
        return devices, usage

    async def tick(self, now: datetime) -> None:
        """One cycle. Never raises: every expected failure becomes a state."""
        if not bool(self._settings.get(EMPORIA_ENABLED_KEY)):
            # Forget the charger and the connection list, not just stop reading
            # them. They were assigned on every successful read and never
            # cleared, so a module switched off went on reporting the last
            # charger it saw — at a rate, with a live-looking slider over it —
            # for the whole life of the process. "Off" has to mean off. The raw
            # record goes with them: it carries the breaker PIN, and holding it
            # for a module nobody is running buys nothing.
            self.charger = None
            self.connections = {}
            self._charger_record = None
            self._state = PollerState("off", "", self._state.last_success)
            return

        stored = tokens.load(self.token_path)
        if stored is None:
            self._state = PollerState(
                "reconnect_required", "no Emporia login has been saved", self._state.last_success
            )
            return

        # No identifiers means no usable usage call, whatever the clock says:
        # a restart empties them, and waiting a day for the next name sync
        # would be a day of reading nothing.
        want_names = (
            not self._gids
            or self._names_synced_at is None
            or (now.timestamp() - self._names_synced_at) >= NAME_SYNC_SECONDS
        )
        try:
            refreshed = await asyncio.to_thread(self.client.refresh, stored)
            tokens.save(self.token_path, refreshed)
            self._id_token = refreshed.id_token
            devices, usage = await asyncio.to_thread(self._fetch, refreshed.id_token, want_names)
        except EmporiaAuthExpiredError as exc:
            # Only the owner can fix this, and no amount of retrying will.
            logger.warning("Emporia rejected the stored credential: %s", exc)
            self._state = PollerState(
                "reconnect_required", "Emporia rejected the saved login", self._state.last_success
            )
            return
        except EmporiaChallengeError as exc:
            self._state = PollerState(
                "reconnect_required", f"Emporia asked for {exc}", self._state.last_success
            )
            return
        except EmporiaUnreachableError as exc:
            logger.info("Emporia unreachable, will retry: %s", exc)
            self._state = PollerState("unreachable", str(exc), self._state.last_success)
            return
        except OSError as exc:
            logger.info("Emporia token file unwritable: %s", exc)
            self._state = PollerState("unreachable", str(exc), self._state.last_success)
            return

        try:
            if devices is not None:
                self.repository.sync_circuits(circuits_from_devices(devices), now)
                self._names_synced_at = now.timestamp()
            if usage is not None:
                self.repository.append_readings(readings_from_usage(usage, SCALE_MINUTE), now)
        except STORE_ERRORS as exc:
            # Whatever SQLite objected to, said in its own words. An earlier
            # version reported every one of these as "database busy", which sent
            # somebody looking for contention when the real answer was a column
            # that did not exist — the store had refused the write for a reason
            # it was perfectly willing to state.
            logger.warning("could not store Emporia readings: %s", exc)
            self._state = PollerState(
                "unreachable", f"could not store readings: {exc}", self._state.last_success
            )
            return

        self._consider_restore(refreshed.id_token, now)
        self._state = PollerState("ok", "", int(now.timestamp()))

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick(datetime.now(tz=UTC))
            except STORE_ERRORS as exc:
                logger.warning("Emporia tick failed: %s", exc)
            await asyncio.sleep(self._interval())

    async def _supervise(self) -> None:
        while True:
            try:
                await self._loop()
            except asyncio.CancelledError:
                raise
            except STORE_ERRORS as exc:
                logger.warning("Emporia loop ended unexpectedly, restarting: %s", exc)
            await asyncio.sleep(LOOP_RESTART_SECONDS)

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
