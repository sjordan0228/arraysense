"""backfill_emporia_history.py — fill the circuit hourly tier from Emporia's own archive.

A Vue has been measuring the house for months before this project was pointed at
it, and all of that sits in Emporia's cloud while the Circuits page can only draw
from the hour the poller was switched on. The page offers thirty days and thirteen
months; without this it has hours. One run closes that gap.

**It writes only hours the local rollup has not already measured.** The two
figures are the same quantity — verified against thirteen overlapping hours on the
reference installation, where 315 of 390 (circuit, hour) pairs agreed within 2%
and every disagreement was an hour the poller had only partly watched — but ours
is measured here and theirs is taken on trust, so where both exist ours stands.

``covered_seconds`` is written as a full hour because that is what an Emporia 1H
bucket is: the device's own aggregate of its own second-by-second record, not a
sample of it. An hour the cloud has nothing for writes no row at all, rather than
a row claiming an hour of nothing — absence is not zero, and a bucket with no
value is the module having been unheard from, exactly as a missed poll is.

``sample_count`` is 1: this hour rests on one figure. That is deliberately not the
sixty a polled hour carries, so a reader can still tell a backfilled hour from a
watched one.

Run it by hand, once, against a database nothing is writing to at the time — or
against a live one, which is safe, since every write is an insert that yields to
any row already there.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from arraysense.modules.emporia import tokens
from arraysense.modules.emporia.client import API_BASE, EmporiaClient, EmporiaError

logger = logging.getLogger(__name__)

# Emporia answers HTTP 400 above roughly a month of hourly buckets — 30 days
# (721 buckets) is served and 35 is refused — so a longer request is cut into
# windows of this many days rather than sent whole and lost.
MAX_WINDOW_DAYS = 30

# Emporia's cloud is somebody else's, and this is the only place in the project
# that asks it for months at a time. One pause per circuit keeps a backfill of
# thirty-nine circuits to something a rate limiter will not notice.
PAUSE_SECONDS = 1.0

SECONDS_PER_HOUR = 3600


def _instant(when: datetime) -> str:
    """Emporia's timestamp format: UTC, whole seconds, a literal Z."""
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_hours(
    id_token: str, device_gid: int, channel: str, start: datetime, end: datetime
) -> dict[int, float]:
    """One circuit's hourly kWh over one window, keyed by the hour it starts.

    The keys come from ``firstUsageInstant`` and the list's own position rather
    than from the range that was asked for. Emporia decides where its first
    bucket falls, and reading the answer back against the request would misfile
    every hour by whatever it decided differently.
    """
    path = (
        f"/AppAPI?apiMethod=getChartUsage&deviceGid={device_gid}&channel={channel}"
        f"&start={_instant(start)}&end={_instant(end)}"
        "&scale=1H&energyUnit=KilowattHours"
    )
    request = urllib.request.Request(API_BASE + path, headers={"authtoken": id_token})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:200]
        raise EmporiaError(f"HTTP {exc.code} for gid {device_gid} ch {channel}: {detail}") from exc
    except OSError as exc:
        raise EmporiaError(f"could not reach Emporia: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise EmporiaError("Emporia returned non-JSON") from exc

    if not isinstance(body, dict):
        raise EmporiaError("Emporia returned an unexpected shape")
    usage = body.get("usageList")
    first_raw = body.get("firstUsageInstant")
    if not isinstance(usage, list) or not isinstance(first_raw, str):
        raise EmporiaError("Emporia returned no usage list")
    first = datetime.strptime(first_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    hours: dict[int, float] = {}
    for index, value in enumerate(usage):
        # A bool is an int to Python and a flag to Emporia; the same guard is in
        # parse.py for the same reason.
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        hours[int((first + timedelta(hours=index)).timestamp())] = float(value)
    return hours


def _windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """The range, cut into pieces Emporia will serve."""
    spans: list[tuple[datetime, datetime]] = []
    edge = start
    while edge < end:
        stop = min(edge + timedelta(days=MAX_WINDOW_DAYS), end)
        spans.append((edge, stop))
        edge = stop
    return spans


def backfill(
    conn: sqlite3.Connection, id_token: str, days: int, *, dry_run: bool
) -> tuple[int, int]:
    """Fetch every circuit's archive and insert the hours not already held.

    Returns what was written and what was left alone, because those are the two
    numbers that say whether a run did what it claimed: a second run of the same
    command should write nothing at all.
    """
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    circuits = list(
        conn.execute("SELECT id, device_gid, channel_num, name FROM circuit ORDER BY id")
    )
    logger.info(
        "backfilling %d circuits from %s to %s", len(circuits), _instant(start), _instant(now)
    )

    written = 0
    skipped = 0
    for circuit_id, device_gid, channel, name in circuits:
        held = {
            row[0]
            for row in conn.execute(
                "SELECT timestamp FROM circuit_hourly WHERE circuit_id = ? AND timestamp >= ?",
                (circuit_id, int(start.timestamp())),
            )
        }
        hours: dict[int, float] = {}
        for window_start, window_end in _windows(start, now):
            try:
                hours.update(
                    _fetch_hours(id_token, int(device_gid), str(channel), window_start, window_end)
                )
            except EmporiaError as exc:
                logger.warning("%s: %s", name, exc)
            time.sleep(PAUSE_SECONDS)

        rows = [
            (timestamp, circuit_id, round(kwh * 1000), 1, SECONDS_PER_HOUR)
            for timestamp, kwh in sorted(hours.items())
            if timestamp not in held and timestamp < int(now.timestamp())
        ]
        skipped += len(hours) - len(rows)
        logger.info(
            "%-28s fetched %4d  new %4d  already held %3d",
            name[:28],
            len(hours),
            len(rows),
            len(held),
        )
        if rows and not dry_run:
            with conn:
                conn.executemany(
                    "INSERT INTO circuit_hourly"
                    " (timestamp, circuit_id, watts, sample_count, covered_seconds)"
                    " VALUES (?, ?, ?, ?, ?)"
                    # A measured hour outranks a fetched one, so an existing row
                    # wins outright rather than being merged with this.
                    " ON CONFLICT (timestamp, circuit_id) DO NOTHING",
                    rows,
                )
        written += len(rows)
    return written, skipped


def main(argv: list[str] | None = None) -> int:
    """Parse the arguments, log in with the stored tokens, and run the backfill."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True, type=Path, help="the arraysense database")
    parser.add_argument("--tokens", required=True, type=Path, help="the Emporia token file")
    parser.add_argument("--days", type=int, default=30, help="how far back to reach")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    stored = tokens.load(args.tokens)
    if stored is None:
        logger.error("no usable token file at %s", args.tokens)
        return 1
    try:
        id_token = EmporiaClient().refresh(stored).id_token
    except EmporiaError as exc:
        logger.error("could not authenticate: %s", exc)
        return 1

    conn = sqlite3.connect(args.database)
    try:
        written, skipped = backfill(conn, id_token, args.days, dry_run=args.dry_run)
    finally:
        conn.close()
    verb = "would write" if args.dry_run else "wrote"
    logger.info("%s %d hourly rows; left %d already-measured hours alone", verb, written, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
