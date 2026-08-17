"""repository.py — read and write circuits and their readings.

Every SQL statement this module runs lives here, so the poller is about timing
and the routes are about presentation, and neither has to know a column name.

Two decisions are recorded in the tests rather than only here. A circuit is
matched on ``(device_gid, channel_num)`` so renaming it in Emporia's app moves a
label instead of orphaning history. And the multiplier is stored raw and applied
on read, so a correction upstream does not mean rewriting what is already
recorded.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise

from arraysense.modules.emporia.parse import Circuit, Reading
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# The tier names this module answers to, mapped to the tables that hold them. A
# name is validated against this map before it reaches a query string, so the
# table name interpolated into the SQL below is never a caller's string.
_CIRCUIT_TABLES = {"full": "circuit_reading", "hourly": "circuit_hourly"}

_HOUR_SECONDS = 3600
# What one reading is taken to cover when the caller does not say: the Emporia
# poll interval's own default, which settings.py registers as 60 s. It is used
# twice — as how much of an hour each of a bucket's ``sample_count`` readings
# accounts for, and as the fallback when a window holds too few raw rows to
# measure their spacing — and both are wrong by six times on an installation
# polling at the ten seconds the setting permits, which is why ``history``
# takes the figure rather than assuming this one.
_POLL_SECONDS = 60

# How far apart two readings have to be before the stretch between them counts
# as unrecorded rather than as an ordinary late poll: two cadences, the first
# spacing that leaves a whole period with nothing written in it. Two is the
# allowance ``store.tiers.select_tier_for_range`` already spends on the same
# question of whether a tier holds a stretch, so it is this project's figure
# rather than a new one. The comparison is inclusive where that one is
# exclusive, and the hourly tier is why: its stamps are aligned to the hour, so
# an hour nobody recorded puts its neighbours at exactly two cadences and an
# exclusive test would draw a straight line through every one of them.
_GAP_CADENCES = 2


def _elapsed_or_default(stamps: Sequence[int], cadence_seconds: int = _POLL_SECONDS) -> int:
    """How long one raw reading covers, taken from the window's own spacing.

    Measured rather than assumed because the spacing actually achieved need not
    be the one configured — a poll takes as long as it takes, and the interval
    is a floor rather than a promise. The median gap is taken rather than the
    mean so that a poller stopped for an hour inside the window does not
    inflate every reading's share; the hole then simply goes uncounted, which
    is the right answer for a stretch nobody measured.

    Fewer than two readings leaves no spacing to measure, so this falls back to
    the interval the caller says is in force: a lone reading has to be worth
    some duration or a one-sample window reports no energy at all.
    """
    if len(stamps) < 2:
        return cadence_seconds
    gaps = sorted(b - a for a, b in pairwise(stamps))
    return max(1, gaps[len(gaps) // 2])


def _with_breaks(stamps: Sequence[int], cadence_seconds: int) -> list[int]:
    """The window's stamps, plus one carrying nothing at the start of each hole.

    A series is built from the rows that exist, so a poller stopped for three
    hours leaves two readings three hours apart sitting *adjacent* in the
    array — and a chart that breaks a line only where a null sits joins them,
    drawing an air conditioner ramping gently down and back up across three
    hours nobody measured. The inverter path never had this to deal with
    because the collector writes a row even when a poll fails, and that row
    enters the series as a null; the Emporia poller writes nothing at all while
    the module is off, so there is no row to become one and it has to be made
    here.

    Here rather than in the browser because a page draws what an endpoint tells
    it. A series a consumer has to know to distrust has only pushed the problem
    outward, and there is more than one consumer.

    One stamp per hole is all a break needs — measured on the bench against
    uPlot itself, which leaves 1,035 pixels of a strip unpainted on the
    strength of a single null. Filling the hole would say the same thing in the
    hundred and seventy-six points a three-hour hole takes at a minute's
    cadence. It sits at ``previous + cadence``, the first instant that should
    have carried a reading and did not; at the midpoint the break would float
    away from the moment collection stopped.
    """
    out: list[int] = []
    for previous, current in pairwise(stamps):
        out.append(previous)
        if current - previous >= _GAP_CADENCES * cadence_seconds:
            out.append(previous + cadence_seconds)
    out.extend(stamps[-1:])
    return out


@dataclass(frozen=True)
class CircuitLatest:
    """A circuit and its most recent reading, ready to render.

    ``watts`` is None both when the circuit has never been read and when its
    last reading was absent. A page must say "no reading" for either, so they do
    not need separating here — but neither may be drawn as a zero.
    """

    device_gid: int
    channel_num: str
    name: str
    kind: str
    watts: int | None
    ts: int | None
    # What the owner said this circuit is, in Emporia's own numbering. The page
    # picks an icon from it; None means nobody has categorised the clamp.
    type_gid: int | None = None


@dataclass(frozen=True)
class CircuitSeries:
    """One circuit's readings over a window, with the energy they add up to.

    ``kwh`` is None rather than 0.0 for a circuit that reported nothing at all
    across the window. The two are different claims — "it used no energy" and
    "nobody heard from it" — and a bar chart that renders the second as the
    first puts a dead outlet at the bottom of a ranking as though it were a
    quiet one.

    ``partial`` says the energy figure was built from buckets that were not
    fully recorded. It is not a doubt about the number; the number is what was
    measured. It is what lets the page label a figure rather than present a
    part as a whole. Only the hourly tier can raise it, because only the hourly
    tier stores a sample count — a raw row is one reading, and how much of the
    window those readings between them cover is already in the energy.

    A hole in the window does not raise it. "Thinly sampled bucket" and "the
    module was off for three hours" are different facts, and an hour recorded
    end to end is whole however long the silence either side of it ran.

    ``watts`` therefore holds two kinds of null and deliberately does not
    separate them: a circuit that was listed and did not answer, and an instant
    nothing was recorded at. Both are absences, and neither may be drawn as a
    number.
    """

    circuit_id: int
    device_gid: int
    channel_num: str
    name: str
    kind: str
    watts: tuple[int | None, ...]
    kwh: float | None
    partial: bool


@dataclass(frozen=True)
class CircuitHistory:
    """Every requested circuit over one window, on one shared clock.

    One timestamp array for all of them rather than one each. The circuits are
    polled together and stored under a single instant — ``append_readings``
    takes one ``now`` for the whole batch — so they genuinely share sample
    times, and a chart drawing five strips against five near-identical x arrays
    pays five times for one fact.

    Not every instant here was polled. Where the module went quiet, one stamp
    is added carrying nothing for any circuit, so a line breaks at the moment
    collection stopped instead of being drawn straight across the outage. See
    ``_with_breaks``; it is the same claim as any other null in the series, and
    a consumer needs no separate rule for it.
    """

    timestamps: tuple[int, ...]
    series: tuple[CircuitSeries, ...]
    tier: str


class CircuitRepository:
    """Storage for the Emporia module, and nothing else's."""

    def __init__(self, store: SqliteStore) -> None:
        """Bind to the store whose connection these statements run on."""
        self._store = store

    def sync_circuits(
        self, circuits: Sequence[Circuit], now: datetime
    ) -> dict[tuple[int, str], int]:
        """Insert new circuits, update the names of known ones, return their ids.

        A circuit absent from ``circuits`` is left alone rather than deleted: its
        readings remain valid history, and ``last_seen`` already carries the fact
        that Emporia has stopped mentioning it.
        """
        stamp = int(now.timestamp())
        conn = self._store._conn
        with conn:
            for circuit in circuits:
                conn.execute(
                    "INSERT INTO circuit"
                    " (device_gid, channel_num, name, multiplier, kind, type_gid,"
                    "  first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (device_gid, channel_num) DO UPDATE SET"
                    "   name = excluded.name,"
                    "   multiplier = excluded.multiplier,"
                    "   kind = excluded.kind,"
                    "   type_gid = excluded.type_gid,"
                    "   last_seen = excluded.last_seen",
                    (
                        circuit.device_gid,
                        circuit.channel_num,
                        circuit.name,
                        circuit.multiplier,
                        circuit.kind,
                        circuit.type_gid,
                        stamp,
                        stamp,
                    ),
                )
        return self._ids()

    def _ids(self) -> dict[tuple[int, str], int]:
        rows = self._store._conn.execute(
            "SELECT id, device_gid, channel_num FROM circuit"
        ).fetchall()
        return {(int(row[1]), str(row[2])): int(row[0]) for row in rows}

    def append_readings(self, readings: Sequence[Reading], now: datetime) -> int:
        """Store readings, returning how many were accepted for storage.

        A reading whose circuit is unknown is dropped and counted out rather than
        creating a circuit: circuits come from the device list, which carries the
        owner's name for them, and inventing one here would give it a label
        nobody chose.

        Accepted is not the same as rows added. Two readings for one circuit at
        one instant both count, and the later replaces the earlier — which is
        what a retried poll should do. The number is a measure of what this was
        handed, not of what the table grew by.
        """
        ids = self._ids()
        stamp = int(now.timestamp())
        rows = [
            (stamp, ids[reading.identity], reading.watts)
            for reading in readings
            if reading.identity in ids
        ]
        missing = len(readings) - len(rows)
        if missing:
            logger.debug("%d Emporia readings had no known circuit and were dropped", missing)
        if not rows:
            return 0
        conn = self._store._conn
        with conn:
            conn.executemany(
                "INSERT INTO circuit_reading (timestamp, circuit_id, watts) VALUES (?, ?, ?)"
                " ON CONFLICT (timestamp, circuit_id) DO UPDATE SET watts = excluded.watts",
                rows,
            )
        return len(rows)

    def latest(self) -> list[CircuitLatest]:
        """Every known circuit with its most recent reading, biggest draw first.

        The multiplier is applied here rather than at write time. A 240 V circuit
        reports one leg, so the dryer, the oven and both air conditioners read
        half their real draw without it — exactly the loads this module is for.

        Circuits nobody has heard from sort last, below a circuit measured at
        zero. Ordering them by an assumed number would put a silence above a
        fact, and this list is read top-down to answer "what is drawing all
        that" — the top of it has to mean something.
        """
        try:
            rows = self._store._conn.execute(
                "SELECT c.device_gid, c.channel_num, c.name, c.kind, c.multiplier,"
                "       r.watts, r.timestamp, c.type_gid"
                "  FROM circuit c"
                "  LEFT JOIN circuit_reading r"
                "    ON r.circuit_id = c.id"
                "   AND r.timestamp = (SELECT MAX(timestamp) FROM circuit_reading"
                "                       WHERE circuit_id = c.id)"
                " ORDER BY c.name"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("could not read circuits: %s", exc)
            return []
        out = [
            CircuitLatest(
                device_gid=int(row[0]),
                channel_num=str(row[1]),
                name=str(row[2]),
                kind=str(row[3]),
                watts=None if row[5] is None else round(float(row[5]) * float(row[4])),
                ts=None if row[6] is None else int(row[6]),
                type_gid=None if row[7] is None else int(row[7]),
            )
            for row in rows
        ]
        out.sort(key=lambda c: (c.watts is None, -(c.watts or 0), c.name))
        return out

    def history(
        self,
        start: datetime,
        end: datetime,
        tier: str,
        circuit_ids: Sequence[int] | None = None,
        cadence_seconds: int = _POLL_SECONDS,
    ) -> CircuitHistory:
        """Circuits over a window, ranked by the energy each one used.

        Ranked by energy rather than by the newest reading, because the live
        list already answers "what is drawing that" and this answers "what ate
        the power" — a kettle at 5 kW for a minute is not the day's biggest
        load and sorting on watts would say it was.

        The multiplier is applied here, as it is in ``latest()`` and for the
        same reason: both tiers store one leg of a 240 V circuit, so the dryer,
        the oven and both air conditioners read half without it. The series and
        the energy are multiplied in one place so they cannot disagree.

        Energy comes from the readings rather than from the window. An hourly
        bucket built from two of sixty samples covers two minutes, and
        ``sample_count`` is stored precisely so that hour is not read as a
        full one — coverage in minutes watched is not coverage in energy
        accounted for, and this is the figure the second question depends on.

        ``cadence_seconds`` is the poll interval in force, and on the hourly
        tier it is arithmetic rather than a hint: each of a bucket's
        ``sample_count`` readings accounts for that many seconds of the hour.
        The default matches the setting's own, but the owner may set it as low
        as ten, and at ten a half-recorded hour holds 180 readings — which
        multiplied by an assumed sixty runs past the hour, clamps back to it,
        and reports half an hour as a whole one. The caller knows the interval;
        an hourly row cannot be asked.

        The timestamps are the ones that were recorded plus one per hole, so a
        stretch the module missed arrives as a null rather than as a straight
        line drawn across it — ``_with_breaks`` says why that is this
        endpoint's job and not the page's. The energy is untouched by it: a
        synthetic stamp carries no reading, so it is worth nothing and cannot
        move a kWh figure or mark an hour partial.

        A database error yields an empty history rather than raising. This runs
        unattended on someone's inverter and a page saying it has no circuits
        tells the owner more than a page returning 500.
        """
        if tier not in _CIRCUIT_TABLES:
            raise ValueError(f"unknown circuit tier {tier!r}")
        table = _CIRCUIT_TABLES[tier]
        counted = tier == "hourly"
        first = int(start.timestamp())
        last = int(end.timestamp())
        try:
            circuits = self._store._conn.execute(
                "SELECT id, device_gid, channel_num, name, kind, multiplier FROM circuit"
            ).fetchall()
            rows = self._store._conn.execute(
                "SELECT timestamp, circuit_id, watts,"
                f" {'sample_count' if counted else '1'}"
                f"  FROM {table}"
                "  WHERE timestamp >= ? AND timestamp < ?"
                "  ORDER BY timestamp",
                (first, last),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("could not read circuit history: %s", exc)
            return CircuitHistory(timestamps=(), series=(), tier=tier)

        wanted = None if circuit_ids is None else set(circuit_ids)
        meta = {int(row[0]): row for row in circuits if wanted is None or int(row[0]) in wanted}
        stamps = sorted({int(row[0]) for row in rows})

        # What one row of the raw tier is taken to cover. Measured from the
        # stamps the poll actually landed on rather than from the window, so a
        # stretch nobody recorded contributes nothing instead of being smeared
        # over the readings either side of it. Not consulted on the hourly
        # tier, where sample_count says how much of each hour was recorded.
        # Measured before the breaks are added, or the synthetic stamps would
        # be counted as spacing and move the energy.
        reading_seconds = _elapsed_or_default(stamps, cadence_seconds)
        # An hourly row covers an hour however often the module polled, so that
        # tier is judged against the hour. The raw tier is judged against the
        # spacing it actually achieved rather than the interval in force, which
        # is what stops an installation whose polls run long — or a stretch of
        # history recorded at a different interval — from breaking at every
        # ordinary reading.
        drawn = _with_breaks(stamps, _HOUR_SECONDS if counted else reading_seconds)
        slot = {stamp: index for index, stamp in enumerate(drawn)}
        watts: dict[int, list[int | None]] = {
            circuit_id: [None] * len(drawn) for circuit_id in meta
        }
        joules: dict[int, float] = dict.fromkeys(meta, 0.0)
        seen: set[int] = set()
        partial: set[int] = set()
        for stamp, circuit_id, raw, samples in rows:
            key = int(circuit_id)
            if key not in meta or raw is None:
                continue
            value = round(float(raw) * float(meta[key][5]))
            watts[key][slot[int(stamp)]] = value
            seen.add(key)
            if counted:
                # An hour holds 3,600 seconds however many readings landed in
                # it. Told the interval that was actually running, a full hour's
                # sample count multiplies back to exactly 3,600 and a
                # half-recorded one to exactly 1,800 — but the interval is the
                # owner's to change, and rows recorded at ten seconds read back
                # under a setting since raised to sixty would credit an hour
                # with six hours of energy. The clamp is what stops that, and
                # ``partial`` is read off the same figure so the flag and the
                # arithmetic cannot drift apart.
                covered = min(int(samples) * cadence_seconds, _HOUR_SECONDS)
                if covered < _HOUR_SECONDS:
                    partial.add(key)
            else:
                covered = reading_seconds
            joules[key] += value * covered

        series = [
            CircuitSeries(
                circuit_id=circuit_id,
                device_gid=int(row[1]),
                channel_num=str(row[2]),
                name=str(row[3]),
                kind=str(row[4]),
                watts=tuple(watts[circuit_id]),
                kwh=(joules[circuit_id] / 3_600_000) if circuit_id in seen else None,
                partial=circuit_id in partial,
            )
            for circuit_id, row in meta.items()
        ]
        # A circuit nobody heard from sorts last, below one measured at nothing.
        # Ordering a silence above a fact is what latest() already refuses to do
        # and this list is read top-down for the same reason.
        series.sort(key=lambda s: (s.kwh is None, -(s.kwh or 0.0), s.name))
        return CircuitHistory(timestamps=tuple(drawn), series=tuple(series), tier=tier)


# Who decided a change. Two values rather than a boolean because the audit is
# read by a person: "module" and "owner" say what they mean in a table cell,
# where a `by_module` column would have to be decoded.
#
# This module decided it — a restore, or anything the control rules chose.
MODULE = "module"
# The person did, through a route: the slider on the charger page, or the stop
# and start buttons. Their rate is theirs, and this module never puts it back.
OWNER = "owner"


@dataclass(frozen=True)
class ChargerChange:
    """One decision about a charge rate, applied or not.

    ``applied`` separates what reached the charger from what was only decided.
    Both are worth keeping — a module that proposed twenty things and did none
    of them looks identical to one nobody asked, unless the refusals are
    recorded.

    ``source`` separates *whose* decision it was, and it is not a refinement of
    ``applied``: the owner moving the slider is applied too, because it really
    did reach the charger. Reading provenance out of ``applied`` alone is the
    defect that let restore-on-startup claim a hand-set rate as its own work and
    undo it. None on a row written before this was recorded — unknown is not a
    showing that the change was this module's, so restore leaves those alone.
    """

    timestamp: int
    device_gid: int
    from_a: int | None
    to_a: int | None
    reason: str
    applied: bool
    source: str | None = None

    def same_decision(
        self,
        *,
        from_a: int | None,
        to_a: int | None,
        reason: str,
        applied: bool,
        source: str | None,
    ) -> bool:
        """Whether a decision being made now is the one already written here.

        The timestamp is the one field deliberately left out: two identical
        decisions can never share a second, so counting it would make every
        comparison false and the check pointless.

        ``applied`` is the sharpest of them. A rate proposed and the same rate
        actually reaching the charger are different events, and a caller
        suppressing the second because it matched the first would hide a write.
        """
        return (self.from_a, self.to_a, self.reason, self.applied, self.source) == (
            from_a,
            to_a,
            reason,
            applied,
            source,
        )


class ChargerAudit:
    """The record of every charge rate this service decided on.

    Separate from CircuitRepository because it answers a different question
    about a different device, and because the one thing it must never do is get
    mixed up with a reading: a circuit reading is what the house did, and this
    is what this service did to it.
    """

    def __init__(self, store: SqliteStore) -> None:
        """Bind to the store whose connection these statements run on."""
        self._store = store

    def record_change(
        self,
        device_gid: int,
        *,
        from_a: int | None,
        to_a: int | None,
        reason: str,
        applied: bool,
        source: str,
        now: datetime,
    ) -> None:
        """Write one decision down, whether or not it reached the charger.

        ``source`` has no default on purpose. Every caller has to say whether it
        is the module deciding or the owner asking, because the one that forgot
        is the one whose rate gets undone on the next restart.
        """
        conn = self._store._conn
        with conn:
            conn.execute(
                # A plain insert. An audit line is never an update of an
                # earlier one, and two decisions in the same second are two
                # decisions — which is how stopping and starting a charger
                # within one second lost a row when this had a composite key.
                "INSERT INTO charger_change"
                " (timestamp, device_gid, from_a, to_a, reason, applied, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    int(now.timestamp()),
                    device_gid,
                    from_a,
                    to_a,
                    reason,
                    1 if applied else 0,
                    source,
                ),
            )

    def last_applied_rate(self, device_gid: int) -> int | None:
        """The rate this service last actually set, or None if it never has.

        What restore-on-startup compares the charger against: it puts back only
        a rate it can show was its own. A decision that was never applied never
        reached the charger, so it is not evidence of anything.

        **Its own** is the whole of the question, and ``applied`` does not
        answer it. The owner moving the slider is applied too, so without the
        source this returned the owner's rate, the charger was sitting at
        exactly that rate, and the restore concluded it had set it — then undid
        a rate somebody chose deliberately, inside the window meant to protect
        it.

        It is the *newest* rate that reached the charger that has to be asked,
        not the newest one this module set. Filtering the owner's rows out of
        the query instead would find an older row of the module's own and claim
        the rate on the strength of it: the module set 6 A months ago, the owner
        has since chosen 6 A deliberately, and a rate that agrees with an old
        one of ours is not thereby ours. So the last applied decision decides,
        and it answers None unless it was this module's.

        Only decisions about a rate count, which is what ``to_a IS NOT NULL``
        selects: stopping and starting the charger decides nothing about the
        rate, and reading those as the owner claiming it would retire
        restore-on-startup the first time anybody pressed stop. A row from
        before the source was recorded answers None for the reason at the top:
        unknown is not a showing.

        An owner's write counts whether or not it was confirmed, and this is
        the one place the two sides of ``applied`` are deliberately not
        symmetric. A write is audited as applied only when the charger is read
        back at the new rate, so a request Emporia accepted and then went quiet
        on records as *not* applied while having changed the rate anyway. For
        this module's own proposals that is exactly right — an unconfirmed one
        is not evidence of anything. For the owner it is not: they reached for
        the charger, and a rate that may be theirs is not one this module can
        show is its own.
        """
        try:
            row = self._store._conn.execute(
                "SELECT to_a, source, applied FROM charger_change"
                " WHERE device_gid = ? AND to_a IS NOT NULL"
                "   AND (applied = 1 OR source = ?)"
                " ORDER BY timestamp DESC, id DESC LIMIT 1",
                (device_gid, OWNER),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("could not read the charger audit: %s", exc)
            return None
        if row is None or row[1] != MODULE or not row[2]:
            return None
        return int(row[0])

    def last_change(self, device_gid: int) -> ChargerChange | None:
        """The newest decision about this charger, applied or not.

        A different question from ``last_applied_rate``, which wants the last
        rate that reached the charger. This one wants the last thing that was
        decided, refusals included — because the caller that needs it is one
        checking whether it is about to write the same proposal down twice, and
        a repeating proposal is by definition one that was never applied.
        """
        rows = self._read(
            "WHERE device_gid = ? ORDER BY timestamp DESC, id DESC LIMIT 1", (device_gid,)
        )
        return rows[0] if rows else None

    def _read(self, tail: str, params: tuple[object, ...]) -> list[ChargerChange]:
        """One SELECT over the audit, so the row-to-object mapping exists once."""
        try:
            rows = self._store._conn.execute(
                "SELECT timestamp, device_gid, from_a, to_a, reason, applied, source"
                f" FROM charger_change {tail}",
                params,
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("could not read the charger audit: %s", exc)
            return []
        return [
            ChargerChange(
                timestamp=int(row[0]),
                device_gid=int(row[1]),
                from_a=None if row[2] is None else int(row[2]),
                to_a=None if row[3] is None else int(row[3]),
                reason=str(row[4]),
                applied=bool(row[5]),
                source=None if row[6] is None else str(row[6]),
            )
            for row in rows
        ]

    def recent_changes(self, limit: int = 20) -> list[ChargerChange]:
        """The newest decisions first, for a page to show as a history."""
        return self._read("ORDER BY timestamp DESC, id DESC LIMIT ?", (limit,))
