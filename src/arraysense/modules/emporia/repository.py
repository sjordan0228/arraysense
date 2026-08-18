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
from datetime import UTC, datetime
from itertools import pairwise

from arraysense.modules.emporia.parse import Circuit, Reading
from arraysense.spend import CircuitEnergy
from arraysense.store.sqlite_store import SqliteStore

logger = logging.getLogger(__name__)

# The tier names this module answers to, mapped to the tables that hold them. A
# name is validated against this map before it reaches a query string, so the
# table name interpolated into the SQL below is never a caller's string.
_CIRCUIT_TABLES = {"full": "circuit_reading", "hourly": "circuit_hourly"}

_HOUR_SECONDS = 3600
# What one reading is taken to cover when the caller does not say: the Emporia
# poll interval's own default, which settings.py registers as 60 s. It is wrong
# by six times on an installation polling at the ten seconds the setting
# permits, which is why ``history`` takes the figure rather than assuming this
# one.
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


def _reading_seconds(stamps: Sequence[int], cadence_seconds: int) -> int:
    """The most one raw reading may be credited with, in this window.

    A reading is a sample of one poll period, so it can never account for more
    than one poll interval: if two readings sit further apart than that, the
    polls between them were never recorded and their energy is unknown rather
    than attributable to the neighbours. Measured spacing can only *lower* that
    bound, never raise it — which is the whole of the rule, ``min(median gap,
    cadence)``, and what stops a window holding two 1 kW readings three hours
    apart at a sixty-second interval from reporting the 6 kWh the gap would carry
    where the two minutes of poll period actually sampled are worth 33 Wh.

    The median gap is what lowers it, and it is taken rather than the mean so a
    poller stopped for an hour inside the window does not inflate every
    reading's share. It matters for the one case the interval gets wrong in the
    other direction: history recorded at ten seconds, read back under a setting
    since raised to sixty, is genuinely six times denser than the setting says
    and the measurement says so.

    The residual, stated rather than hidden: the raw tier records no cadence of
    its own, so history recorded at a *longer* interval than the one now
    configured is under-counted — an hour of readings taken hourly, read under a
    sixty-second setting, accounts for sixty seconds and not for the hour. That
    is the safe direction to be wrong in by a wide margin: the alternative,
    trusting the spacing, over-reported a sparse window by two hundred times.
    Only the hourly tier can do better, and it does, by writing its coverage
    down at the moment the interval was known.

    Fewer than two readings leaves no spacing to measure, so this falls back to
    the interval the caller says is in force: a lone reading has to be worth
    some duration or a one-sample window reports no energy at all.
    """
    if len(stamps) < 2:
        return max(1, cadence_seconds)
    gaps = sorted(b - a for a, b in pairwise(stamps))
    return max(1, min(gaps[len(gaps) // 2], cadence_seconds))


def _mark_missing_hour(
    thin: set[str],
    windows: Sequence[tuple[int, int, str]],
    hour_start: int,
    first: int,
    last: int,
) -> None:
    """Flag every band an hour with no measured energy overlaps, for one circuit.

    ``band_kwh`` calls this from two places for what is the same fact told two
    ways: a row that exists but carries no watts (the circuit was listed and
    never answered once that hour) and an hour with no row for it at all
    (the poller was down, or an archive reply omitted it). Both mean this
    circuit's energy for that hour is unknown rather than zero, and both feed
    the same set through the same overlap arithmetic rather than keeping two
    copies of it that could drift apart.
    """
    hour_end = hour_start + _HOUR_SECONDS
    for iv_start, iv_end, band in windows:
        if min(hour_end, iv_end, last) - max(hour_start, iv_start, first) > 0:
            thin.add(band)


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

    ``circuit_id`` is the surrogate ``history()`` already keys its own series
    on. A page linking a live row to that circuit's chart has to name the same
    circuit both endpoints agree on, and identity here is ``(device_gid,
    channel_num)`` with this id as its handle — never the name, which
    ``sync_circuits`` updates in place the moment an owner renames a circuit in
    Emporia's app.
    """

    circuit_id: int
    device_gid: int
    channel_num: str
    name: str
    kind: str
    # None for a device nothing else contains. Only those may be added up.
    parent_device_gid: int | None
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
    # None for a device nothing else contains. Only those may be added to a
    # total: everything else is already inside a circuit that is counted.
    parent_device_gid: int | None
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

    ``recorded_seconds`` is how much of the window the module was recording for
    at all — the union across every circuit the window holds, since a poll that
    reached one clamp reached the monitor. Across every circuit and not only
    the requested ones: narrowing to a single outlet that has been offline
    since April would otherwise report a module outage and withhold a share the
    module could honestly support. It is here because it is measured from the
    same coverage the energy is, and a caller that recomputed it from the
    timestamps would be deriving in a second place the one figure that says
    whether these circuits and the house counter describe the same span. A
    seven-day window holding one reading an hour recorded seven hours, not
    seven days, and only this number can say so.
    """

    timestamps: tuple[int, ...]
    series: tuple[CircuitSeries, ...]
    tier: str
    recorded_seconds: int = 0


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
                    "  parent_device_gid, parent_channel_num, first_seen, last_seen)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (device_gid, channel_num) DO UPDATE SET"
                    "   name = excluded.name,"
                    "   multiplier = excluded.multiplier,"
                    "   kind = excluded.kind,"
                    "   type_gid = excluded.type_gid,"
                    "   parent_device_gid = excluded.parent_device_gid,"
                    "   parent_channel_num = excluded.parent_channel_num,"
                    "   last_seen = excluded.last_seen",
                    (
                        circuit.device_gid,
                        circuit.channel_num,
                        circuit.name,
                        circuit.multiplier,
                        circuit.kind,
                        circuit.type_gid,
                        circuit.parent_device_gid,
                        circuit.parent_channel_num,
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
                "       r.watts, r.timestamp, c.type_gid, c.id, c.parent_device_gid"
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
                circuit_id=int(row[8]),
                device_gid=int(row[0]),
                channel_num=str(row[1]),
                name=str(row[2]),
                kind=str(row[3]),
                parent_device_gid=None if row[9] is None else int(row[9]),
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
        bucket built from two of sixty samples covers two minutes, and how much
        of the hour was recorded is stored precisely so that hour is not read as
        a full one — coverage in minutes watched is not coverage in energy
        accounted for, and this is the figure the second question depends on.

        The hourly tier answers that from its own ``covered_seconds``, measured
        by the rollup at a moment when the interval that produced the readings
        was still the one in force. Nothing here guesses it, and that is the
        point: passing today's setting to rows recorded under a different one
        doubled the energy of every stored hour the day the bench interval was
        raised from ten seconds to sixty. A row written before that column
        existed holds NULL and falls back to exactly that guess —
        ``sample_count`` times the interval now in force, clamped to the hour —
        because its raw readings are pruned at thirty days and the measurement
        cannot be made after the fact. An old hour read imperfectly beats an old
        hour refused, but the fallback is written out here rather than left to
        look like arithmetic.

        ``cadence_seconds`` is the poll interval in force. It bounds the raw
        tier, where no coverage was ever written down: one reading accounts for
        at most one interval, and for less where the next reading came sooner.
        See ``_reading_seconds`` for the rule and for what it costs.

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
                "SELECT id, device_gid, channel_num, name, kind, multiplier,"
                "  parent_device_gid FROM circuit"
            ).fetchall()
            rows = self._store._conn.execute(
                "SELECT timestamp, circuit_id, watts,"
                f" {'sample_count, covered_seconds' if counted else '1, NULL'}"
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

        # The most one row of the raw tier may be credited with: one poll
        # interval, lowered to the spacing the window achieved where that is
        # tighter. Not consulted on the hourly tier, which stores its own
        # coverage. Measured before the breaks are added, or the synthetic
        # stamps would be counted as spacing and move the energy.
        reading_seconds = _reading_seconds(stamps, cadence_seconds)
        # An hourly row covers an hour however often the module polled, so that
        # tier is judged against the hour. The raw tier is judged against the
        # same bound its energy is, so a stretch nobody recorded breaks the line
        # instead of being drawn straight across — which it was while the
        # threshold came from the window's own spacing, since a window that is
        # mostly hole measures the hole as its ordinary spacing.
        drawn = _with_breaks(stamps, _HOUR_SECONDS if counted else reading_seconds)
        slot = {stamp: index for index, stamp in enumerate(drawn)}
        # The stamp after each recorded one, so a reading is credited with the
        # distance to its successor rather than with a flat interval — a retried
        # poll five seconds behind its predecessor is worth five seconds, not
        # sixty. The same rule the rollup applies inside an hour.
        following = dict(pairwise(stamps))
        watts: dict[int, list[int | None]] = {
            circuit_id: [None] * len(drawn) for circuit_id in meta
        }
        joules: dict[int, float] = dict.fromkeys(meta, 0.0)
        seen: set[int] = set()
        partial: set[int] = set()
        # How much of the window the module was recording for, per instant, as
        # the widest coverage any one circuit had there: a poll that reached one
        # clamp reached the monitor, and this is a fact about the module rather
        # than about a circuit.
        recorded_at: dict[int, int] = {}
        for stamp, circuit_id, raw, samples, stored in rows:
            if raw is None:
                continue
            key = int(circuit_id)
            when = int(stamp)
            if counted:
                # An hour holds 3,600 seconds however many readings landed in
                # it. ``covered_seconds`` is what the rollup measured while the
                # interval that produced those readings was still in force, and
                # ``partial`` is read off the same figure so the flag and the
                # arithmetic cannot drift apart. NULL is a row written before
                # that column existed, and only there is the old guess used:
                # the sample count times the interval running *now*, which is
                # right until somebody changes the setting and wrong by the
                # ratio afterwards.
                covered = (
                    min(int(stored), _HOUR_SECONDS)
                    if stored is not None
                    else min(int(samples) * cadence_seconds, _HOUR_SECONDS)
                )
            else:
                nxt = following.get(when)
                covered = reading_seconds if nxt is None else min(nxt - when, reading_seconds)
            # Taken from every circuit the window returned, before the narrowing
            # below. This is a fact about the module, not about a circuit: a
            # poll that reached one clamp reached the monitor. Measured after
            # the narrowing, a request for one circuit read that circuit's
            # silence as a module outage and withheld a share the module could
            # honestly support.
            recorded_at[when] = max(recorded_at.get(when, 0), covered)
            if key not in meta:
                continue
            value = round(float(raw) * float(meta[key][5]))
            watts[key][slot[when]] = value
            seen.add(key)
            if counted and covered < _HOUR_SECONDS:
                partial.add(key)
            joules[key] += value * covered

        series = [
            CircuitSeries(
                circuit_id=circuit_id,
                device_gid=int(row[1]),
                channel_num=str(row[2]),
                name=str(row[3]),
                kind=str(row[4]),
                parent_device_gid=None if row[6] is None else int(row[6]),
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
        return CircuitHistory(
            timestamps=tuple(drawn),
            series=tuple(series),
            tier=tier,
            recorded_seconds=sum(recorded_at.values()),
        )

    def band_kwh(
        self,
        start: datetime,
        end: datetime,
        intervals: Sequence[tuple[datetime, datetime, str | None]],
        *,
        tier: str = "hourly",
        circuit_ids: Sequence[int] | None = None,
        cadence_seconds: int = _POLL_SECONDS,
        now: datetime | None = None,
    ) -> tuple[CircuitEnergy, ...]:
        """Each circuit's energy over a window, split into the bands it ran in.

        ``history()`` with one accumulator per band instead of one per circuit,
        and deliberately the same query, the same coverage rule and the same
        multiplier: two readers of the same rows that disagree about how much
        energy is in them is how a page comes to price a month two ways.

        ``intervals`` are the band windows ``costs.band_intervals`` cut, as
        (start, end, band name) with the name None for a stretch no band covers.
        A stretch nobody priced contributes to nothing here — the energy in it
        is real and unpriced, and the Costs page already warns about unpriced
        minutes separately rather than inventing a band to hold them.

        The hourly tier is the one this is asked for, and the tolerance it
        carries is stated rather than hidden. A stored hour's energy is split
        across the intervals the *hour* overlaps, in proportion to that overlap.
        Every tariff this project has seen changes rate on the hour, so each
        hour lies in one band and the split is exact. A band edge at half past
        makes it an assumption that the hour's recorded seconds were spread
        evenly through it — bounded by one hour on one circuit at one boundary
        a day, which is the same kind and size of tolerance ``bucket_totals``
        already accepts at a bucket edge. The alternative is inventing a
        distribution the stored row does not carry. A stored hour is only ever
        spread over less than the full hour when it is genuinely still being
        written, which is a fact about the clock rather than about the query:
        ``now`` — the real instant, unless a caller pins it for a test — says
        whether the hour has actually finished, and ``covered_seconds`` says
        how much of it was sampled. A hole where a caller's ``last`` merely
        falls before the hour's nominal end is not the same claim: that is
        also true of a complete past hour on the last hour of a month whose
        UTC offset is not a whole number of hours, and of a thin hour with an
        ordinary collection gap that finished happening well before today —
        neither is still being written, so neither may be treated as a prefix
        starting at the top of the hour. Trusting ``last`` alone for that
        question is the mistake three rounds of review found in this
        function; ``now`` is what tells a bucket that has not finished
        forming from one that is complete but thin, and
        ``test_band_kwh_conserves_energy_across_bands`` asserts the
        conservation this whole function rests on directly, across many
        generated cases, rather than leaving it to be re-broken a fourth
        time.

        A circuit's band figure also carries a flag for the hours it is known
        to be missing rather than merely thin, in two shapes that are the
        same fact. ``rebuild_circuit_hourly`` writes a NULL-watts row for an
        hour a circuit was listed for but never answered once, and that row
        contributes no energy here — but every band its hour overlaps is
        marked partial for that circuit, the same completeness rule
        ``spend.missing_band`` already applies to a band that came back
        empty, at the scale of one hour of it rather than the whole thing.
        The other shape is a bucket with no row at all — the poller was down,
        or an archive reply omitted that hour — which is told from one this
        circuit was never asked about by sitting strictly between two hours
        it did report: every aligned hour in such a gap is marked partial the
        same way a NULL-watts row is, through the shared
        ``_mark_missing_hour``. A circuit that reported nothing at all,
        anywhere in the window, is left unflagged either way: it is already a
        dash through its empty ``by_band``, and marking it partial too would
        label a row with nothing on it to label.

        Everything is compared as epoch seconds, never as datetimes: two aware
        datetimes sharing a tzinfo subtract as though they were naive, and a
        month with a clock change in it is where that surfaces.

        A database error yields nothing rather than raising, for the reason
        ``history()`` gives.
        """
        if tier not in _CIRCUIT_TABLES:
            raise ValueError(f"unknown circuit tier {tier!r}")
        table = _CIRCUIT_TABLES[tier]
        counted = tier == "hourly"
        first = int(start.timestamp())
        last = int(end.timestamp())
        now_ts = int((now if now is not None else datetime.now(UTC)).timestamp())
        # circuit_hourly's own timestamp is the floor of the hour it covers
        # (rebuild_circuit_hourly), so the bucket straddling ``first`` is
        # stamped *before* it whenever the boundary does not land on the
        # hour — local midnight in a zone off a whole-hour UTC offset, for
        # one. Read from the hour that contains ``first`` rather than from
        # ``first`` itself, or that bucket is dropped whole and the window
        # silently loses the slice of it lying inside [first, last). The
        # overlap arithmetic below clips every row to [first, last) and to
        # ``intervals``' own bounds regardless of where the query started, so
        # widening it here cannot pull in energy from outside what was asked.
        query_first = (first // _HOUR_SECONDS) * _HOUR_SECONDS if counted else first
        try:
            circuits = self._store._conn.execute(
                "SELECT id, device_gid, channel_num, name, kind, multiplier,"
                "  parent_device_gid FROM circuit"
            ).fetchall()
            rows = self._store._conn.execute(
                "SELECT timestamp, circuit_id, watts,"
                f" {'sample_count, covered_seconds' if counted else '1, NULL'}"
                f"  FROM {table}"
                "  WHERE timestamp >= ? AND timestamp < ?"
                "  ORDER BY timestamp",
                (query_first, last),
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("could not read circuit band energy: %s", exc)
            return ()

        wanted = None if circuit_ids is None else set(circuit_ids)
        meta = {int(row[0]): row for row in circuits if wanted is None or int(row[0]) in wanted}
        stamps = sorted({int(row[0]) for row in rows})
        reading_seconds = _reading_seconds(stamps, cadence_seconds)
        following = dict(pairwise(stamps))
        # Named bands only, as epoch seconds. An unpriced stretch is dropped
        # here rather than carried as a None key, so nothing downstream has to
        # remember that one of its bands is not a band.
        windows = [
            (int(iv_start.timestamp()), int(iv_end.timestamp()), band)
            for iv_start, iv_end, band in intervals
            if band is not None
        ]

        joules: dict[int, dict[str, float]] = {cid: {} for cid in meta}
        thin: dict[int, set[str]] = {cid: set() for cid in meta}
        # Every hour this circuit has any row for, whether or not it carries
        # watts — the anchors a gap between two of them is measured from,
        # below. Only the hourly tier has aligned buckets to look for a gap
        # between; the raw tier's own holes are a different question,
        # already ``history()``'s to answer via ``_with_breaks``.
        reported_hours: dict[int, list[int]] = {cid: [] for cid in meta}
        # Circuits actually heard from somewhere in the window, at least
        # once. A circuit that never answered at all is already a dash —
        # ``by_band`` stays empty and a page reads that as ``cost is None`` —
        # and flagging it partial too would hatch a row with nothing behind
        # it to hatch.
        seen: set[int] = set()
        for stamp, circuit_id, raw, samples, stored in rows:
            key = int(circuit_id)
            if key not in meta:
                continue
            when = int(stamp)
            if counted:
                reported_hours[key].append(when)
            if raw is None:
                # A NULL-watts hourly row is a circuit that was listed and did
                # not answer once in the whole hour — rebuild_circuit_hourly
                # writes exactly this row, with a NULL average and a sample
                # count of nought, when every reading it saw that hour came
                # back with no watts. That is a fact evidenced by the row
                # itself, not a hole inferred from a gap in the timestamps —
                # a different question from the one CircuitSeries's own
                # docstring answers, which is only that a hole *elsewhere*
                # must not demote an hour that was itself recorded end to
                # end. A multi-hour sum known to be missing this specific
                # hour is the same defect spend.missing_band already guards
                # at the scale of a whole band gone rather than one hour of
                # it, so every band this hour overlaps is marked partial for
                # this circuit — contributing no energy, since none was
                # measured.
                if counted:
                    _mark_missing_hour(thin[key], windows, when, first, last)
                continue
            seen.add(key)
            if counted:
                covered = (
                    min(int(stored), _HOUR_SECONDS)
                    if stored is not None
                    else min(int(samples) * cadence_seconds, _HOUR_SECONDS)
                )
                # The span the energy is spread across is the hour, not the
                # part of it that was recorded — where in the hour those
                # seconds fell is precisely what the row does not say — except
                # at the trailing edge of a row that is genuinely still being
                # written, where it is not a guess at all. The current month
                # always ends mid-hour, and there the rollup's own
                # covered_seconds already IS the part of the hour that existed
                # by the time it was measured, because the rest of it had not
                # happened yet. Smearing that already-exact figure across a
                # remainder that does not exist applies the same fraction
                # twice: a 1 kW row covering 12:00-12:30, asked about a window
                # ending 12:30, returned 0.25 kWh instead of the 0.5 it
                # actually measured. Shrinking span to match is what stops the
                # second reduction — but only for a row that is actually
                # short. ``hour_end > last`` alone is not enough to tell that:
                # it is also true of a fully recorded *past* hour whose
                # nominal end merely falls after the window's own end, which
                # is every month's last hour on a site whose UTC offset is not
                # a whole number of hours — that hour was booked at three
                # quarters of its energy for a query that asked about half of
                # it. ``covered < _HOUR_SECONDS`` is what tells the two apart,
                # since only a row that is actually partial can measure less
                # than the whole hour. And the shrunk span is ``covered``
                # itself rather than ``last - when``: the two agree only when
                # the row was measured up to exactly ``last``, which is not
                # guaranteed when the caller's ``last`` is earlier than the
                # real "now" a still-forming row was recorded up to.
                #
                # ``hour_end > last`` alone finds the boundary row, but it
                # cannot tell a bucket still being written from one that
                # finished long ago and merely happens to end after the
                # window's own close — a finished month's last hour on a site
                # off a whole UTC offset is exactly that shape, and if a
                # collection gap also left that hour thin, ``covered_seconds``
                # records only how much of it was sampled, never where. Book
                # the whole thin figure as a top-of-hour prefix there and it
                # can claim energy that was actually measured on the far side
                # of ``last``, past the boundary rather than proportional to
                # it — the defect three rounds of review kept finding in this
                # line. ``hour_end > now_ts`` is what a fully elapsed hour can
                # never satisfy, live or not, so it is what actually tells
                # the two apart; ``last`` alone was only ever a proxy for it.
                hour_end = when + _HOUR_SECONDS
                span = (
                    covered
                    if hour_end > last and covered < _HOUR_SECONDS and hour_end > now_ts
                    else _HOUR_SECONDS
                )
            else:
                nxt = following.get(when)
                covered = reading_seconds if nxt is None else min(nxt - when, reading_seconds)
                span = covered
            if span <= 0 or covered <= 0:
                continue
            value = round(float(raw) * float(meta[key][5]))
            energy = float(value) * covered
            for iv_start, iv_end, band in windows:
                # ``first`` is intersected here, on the same footing as a band
                # edge, rather than folded into ``span`` above: a query start
                # landing mid-hour (a Kolkata month, half an hour off UTC) is
                # an ordinary clip on the smear, not a claim that the row's
                # own recorded seconds are known to sit exactly at the
                # window's edge the way the trailing one is. ``last`` is
                # intersected too, and it has to be now that it is no longer
                # always folded into ``span``: a fully recorded hour is never
                # shrunk any more, so without this clip a query ending
                # mid-hour would count that hour's energy past the window's
                # own end — exactly the over-credit this fixes.
                overlap = min(when + span, iv_end, last) - max(when, iv_start, first)
                if overlap <= 0:
                    continue
                joules[key][band] = joules[key].get(band, 0.0) + energy * (overlap / span)
                if counted and covered < _HOUR_SECONDS:
                    thin[key].add(band)

        # A bucket with no row at all, sitting strictly between two hours
        # this circuit did report — the NULL-watts row's own case above
        # covers a circuit that was asked and stayed silent; this is the
        # other way an hour can be unaccounted for, when nothing asked it
        # anything because the poller itself was down, or an archive reply
        # simply omitted that hour. Bounded to strictly between two rows this
        # circuit actually has, rather than from the edges of the query
        # window inward, because that is the only span in which a gap is
        # evidenced: a circuit with no row before its first appearance in the
        # window may just not have existed yet, and guessing past its last
        # row would flag hours nobody was ever going to hear from again.
        if counted:
            for key, hours in reported_hours.items():
                for previous, following_hour in pairwise(sorted(hours)):
                    missing = previous + _HOUR_SECONDS
                    while missing < following_hour:
                        _mark_missing_hour(thin[key], windows, missing, first, last)
                        missing += _HOUR_SECONDS

        return tuple(
            CircuitEnergy(
                name=str(row[3]),
                kind=str(row[4]),
                by_band={band: value / 3_600_000 for band, value in joules[circuit_id].items()},
                partial_bands=frozenset(thin[circuit_id]) if circuit_id in seen else frozenset(),
            )
            for circuit_id, row in meta.items()
        )


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
