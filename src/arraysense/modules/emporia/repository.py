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

from arraysense.modules.emporia.parse import Circuit, Reading
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class ChargerChange:
    """One decision about a charge rate, applied or not.

    ``applied`` separates what reached the charger from what was only decided.
    Both are worth keeping — a module that proposed twenty things and did none
    of them looks identical to one nobody asked, unless the refusals are
    recorded — but only an applied change is this service's own work, and
    restore-on-startup depends on telling those apart.
    """

    timestamp: int
    device_gid: int
    from_a: int | None
    to_a: int | None
    reason: str
    applied: bool


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
        now: datetime,
    ) -> None:
        """Write one decision down, whether or not it reached the charger."""
        conn = self._store._conn
        with conn:
            conn.execute(
                # A plain insert. An audit line is never an update of an
                # earlier one, and two decisions in the same second are two
                # decisions — which is how stopping and starting a charger
                # within one second lost a row when this had a composite key.
                "INSERT INTO charger_change"
                " (timestamp, device_gid, from_a, to_a, reason, applied)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (int(now.timestamp()), device_gid, from_a, to_a, reason, 1 if applied else 0),
            )

    def last_applied_rate(self, device_gid: int) -> int | None:
        """The rate this service last actually set, or None if it never has.

        What restore-on-startup compares the charger against: it puts back only
        a rate it can show was its own. A decision that was never applied never
        reached the charger, so it is not evidence of anything.
        """
        try:
            row = self._store._conn.execute(
                "SELECT to_a FROM charger_change"
                " WHERE device_gid = ? AND applied = 1 AND to_a IS NOT NULL"
                " ORDER BY timestamp DESC, id DESC LIMIT 1",
                (device_gid,),
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("could not read the charger audit: %s", exc)
            return None
        return None if row is None else int(row[0])

    def recent_changes(self, limit: int = 20) -> list[ChargerChange]:
        """The newest decisions first, for a page to show as a history."""
        try:
            rows = self._store._conn.execute(
                "SELECT timestamp, device_gid, from_a, to_a, reason, applied"
                " FROM charger_change ORDER BY timestamp DESC, id DESC LIMIT ?",
                (limit,),
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
            )
            for row in rows
        ]
