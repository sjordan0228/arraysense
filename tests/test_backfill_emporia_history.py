"""Tests for the Emporia archive backfill: tools/backfill_emporia_history.py.

This runs by hand against a live database holding months of history, so the
cases below are the ones where a mistake would be silent. The one that matters
most is that a fetched hour never displaces a measured one: our own rollup is
measured on this machine from readings we took, and Emporia's figure is taken
on trust, so where both exist ours has to stand.

The network is never touched, and the reply is faked at two different depths on
purpose. The storage tests replace ``_fetch_hours`` outright, because what they
are about is what reaches the database. The first six replace ``urlopen`` and
let the real ``_fetch_hours`` run, because everything above it is blind to how a
bucket gets its hour — an implementation that keyed the list off the requested
start instead of ``firstUsageInstant`` would misfile every reading and still
pass every storage test here.
"""

from __future__ import annotations

import email.message
import io
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

import backfill_emporia_history
from arraysense.modules.emporia.client import EmporiaError
from backfill_emporia_history import (
    MAX_WINDOW_DAYS,
    SECONDS_PER_HOUR,
    _fetch_hours,
    _instant,
    _windows,
    backfill,
)

CIRCUIT_DDL = (
    "CREATE TABLE circuit ("
    " id INTEGER PRIMARY KEY,"
    " device_gid INTEGER NOT NULL,"
    " channel_num TEXT NOT NULL,"
    " name TEXT NOT NULL,"
    " multiplier REAL NOT NULL DEFAULT 1.0,"
    " kind TEXT NOT NULL DEFAULT 'circuit',"
    " type_gid INTEGER,"
    " first_seen INTEGER NOT NULL,"
    " last_seen INTEGER NOT NULL,"
    " UNIQUE (device_gid, channel_num))"
)

CIRCUIT_HOURLY_DDL = (
    "CREATE TABLE circuit_hourly ("
    " timestamp INTEGER NOT NULL,"
    " circuit_id INTEGER NOT NULL,"
    " watts INTEGER,"
    " sample_count INTEGER NOT NULL,"
    " covered_seconds INTEGER,"
    " PRIMARY KEY (timestamp, circuit_id))"
)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The courtesy pause between circuits is real politeness and dead time here."""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    """A database with the two tables the tool reads and writes, and one circuit."""
    connection = sqlite3.connect(":memory:")
    connection.execute(CIRCUIT_DDL)
    connection.execute(CIRCUIT_HOURLY_DDL)
    connection.execute(
        "INSERT INTO circuit"
        " (id, device_gid, channel_num, name, first_seen, last_seen)"
        " VALUES (1, 100000, '1', 'Garage plugs', 0, 0)"
    )
    connection.commit()
    yield connection
    connection.close()


def _an_hour_ago() -> int:
    """A whole hour that has already finished, which is what the tool will store."""
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    return int((now - timedelta(hours=1)).timestamp())


def _answering(hours: dict[int, float], monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every fetch return the same canned hours, whatever is asked for."""

    def canned(
        id_token: str, device_gid: int, channel: str, start: datetime, end: datetime
    ) -> dict[int, float]:
        return dict(hours)

    monkeypatch.setattr(backfill_emporia_history, "_fetch_hours", canned)


class _Answer:
    """The slice of a urllib response that ``_fetch_hours`` actually touches."""

    def __init__(self, body: str) -> None:
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _replying(body: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Answer any GET with this body, and hand back the URLs that were asked for."""
    asked: list[str] = []

    def fake_urlopen(request: urllib.request.Request, timeout: float = 0) -> _Answer:
        asked.append(request.full_url)
        return _Answer(body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return asked


def test_a_bucket_is_keyed_from_the_instant_emporia_names_not_the_one_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Emporia decides where its first bucket falls and it is not always the
    # start of the request. Keying the list off the requested start instead
    # would misfile every hour by the difference, silently and permanently —
    # and every other test here would still pass, because they all replace this
    # function. Asked from 09:00, answered from 11:00.
    _replying(
        '{"firstUsageInstant": "2026-07-18T11:00:00Z", "usageList": [1.0, 2.0, 3.0]}', monkeypatch
    )
    hours = _fetch_hours(
        "token",
        100000,
        "1",
        datetime(2026, 7, 18, 9, tzinfo=UTC),
        datetime(2026, 7, 18, 14, tzinfo=UTC),
    )
    assert hours == {
        int(datetime(2026, 7, 18, 11, tzinfo=UTC).timestamp()): 1.0,
        int(datetime(2026, 7, 18, 12, tzinfo=UTC).timestamp()): 2.0,
        int(datetime(2026, 7, 18, 13, tzinfo=UTC).timestamp()): 3.0,
    }


def test_a_null_bucket_is_dropped_without_moving_the_hours_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The list is positional, so a hole has to be stepped over rather than
    # skipped: dropping it and carrying on would shift every later hour one
    # bucket earlier, which is a whole afternoon filed under lunchtime.
    _replying(
        '{"firstUsageInstant": "2026-07-18T00:00:00Z", "usageList": [1.0, null, 3.0]}', monkeypatch
    )
    hours = _fetch_hours(
        "token", 100000, "1", datetime(2026, 7, 18, tzinfo=UTC), datetime(2026, 7, 19, tzinfo=UTC)
    )
    assert hours == {
        int(datetime(2026, 7, 18, 0, tzinfo=UTC).timestamp()): 1.0,
        int(datetime(2026, 7, 18, 2, tzinfo=UTC).timestamp()): 3.0,
    }


def test_a_flag_where_a_reading_should_be_is_not_read_as_a_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bool is an int to Python. Unguarded, `true` becomes 1 kWh — a kilowatt
    # for an hour, invented. The same guard is in parse.py for the same reason.
    _replying(
        '{"firstUsageInstant": "2026-07-18T00:00:00Z", "usageList": [true, 2.0]}', monkeypatch
    )
    hours = _fetch_hours(
        "token", 100000, "1", datetime(2026, 7, 18, tzinfo=UTC), datetime(2026, 7, 19, tzinfo=UTC)
    )
    assert hours == {int(datetime(2026, 7, 18, 1, tzinfo=UTC).timestamp()): 2.0}


def test_the_request_names_the_device_the_channel_and_the_hourly_scale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Emporia answers HTTP 400 to a request missing any of these, so a typo here
    # is the difference between a backfill and a wall of warnings.
    asked = _replying('{"firstUsageInstant": "2026-07-18T00:00:00Z", "usageList": []}', monkeypatch)
    _fetch_hours(
        "token",
        100000,
        "1,2,3",
        datetime(2026, 7, 18, tzinfo=UTC),
        datetime(2026, 8, 17, tzinfo=UTC),
    )
    assert len(asked) == 1
    url = asked[0]
    assert "apiMethod=getChartUsage" in url
    assert "deviceGid=100000" in url
    assert "channel=1,2,3" in url
    assert "scale=1H" in url
    assert "start=2026-07-18T00:00:00Z" in url
    assert "end=2026-08-17T00:00:00Z" in url


def test_a_reply_that_is_not_a_usage_list_is_an_error_rather_than_no_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Returning {} here would book the circuit as "the cloud has nothing" and
    # move on, which is how a broken account reads as a quiet house.
    _replying('{"message": "something went wrong"}', monkeypatch)
    with pytest.raises(EmporiaError):
        _fetch_hours(
            "token",
            100000,
            "1",
            datetime(2026, 7, 18, tzinfo=UTC),
            datetime(2026, 7, 19, tzinfo=UTC),
        )


def test_a_refused_request_says_what_emporia_answered(monkeypatch: pytest.MonkeyPatch) -> None:
    def refusing(request: urllib.request.Request, timeout: float = 0) -> _Answer:
        raise urllib.error.HTTPError(
            request.full_url,
            400,
            "Bad Request",
            email.message.Message(),
            io.BytesIO(b"window too long"),
        )

    monkeypatch.setattr(urllib.request, "urlopen", refusing)
    with pytest.raises(EmporiaError, match="400"):
        _fetch_hours(
            "token",
            100000,
            "1",
            datetime(2026, 7, 18, tzinfo=UTC),
            datetime(2026, 9, 18, tzinfo=UTC),
        )


def test_a_range_longer_than_one_request_is_cut_into_contiguous_pieces() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=90)
    spans = _windows(start, end)
    assert len(spans) == 3
    assert all((stop - begin).days <= MAX_WINDOW_DAYS for begin, stop in spans)
    # No overlap and no hole: an overlap wastes a request and a hole loses days
    # silently, which is the failure nobody would notice.
    assert spans[0][0] == start
    assert spans[-1][1] == end
    assert all(spans[i][1] == spans[i + 1][0] for i in range(len(spans) - 1))


def test_a_range_short_enough_to_ask_for_at_once_is_one_request() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=29)
    assert _windows(start, end) == [(start, end)]


def test_an_instant_is_formatted_the_way_emporia_wants_it() -> None:
    # Emporia answers HTTP 400 to anything else, so this is the format the whole
    # tool depends on rather than a cosmetic choice.
    assert _instant(datetime(2026, 3, 15, 14, 30, 45, tzinfo=UTC)) == "2026-03-15T14:30:45Z"


def test_a_fetched_hour_is_stored_as_one_reading_covering_the_whole_hour(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    hour = _an_hour_ago()
    _answering({hour: 1.5}, monkeypatch)

    written, skipped = backfill(conn, "token", 30, dry_run=False)

    assert (written, skipped) == (1, 0)
    assert conn.execute(
        "SELECT timestamp, circuit_id, watts, sample_count, covered_seconds FROM circuit_hourly"
    ).fetchall() == [(hour, 1, 1500, 1, SECONDS_PER_HOUR)]


def test_an_hour_the_rollup_already_measured_is_left_exactly_as_it_stands(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point of the tool's insert. Ours is measured here from readings
    # we took; theirs is taken on trust, and a fetched hour that overwrote a
    # measured one would replace evidence with hearsay and leave no trace.
    hour = _an_hour_ago()
    conn.execute(
        "INSERT INTO circuit_hourly"
        " (timestamp, circuit_id, watts, sample_count, covered_seconds)"
        " VALUES (?, 1, 2000, 57, 3540)",
        (hour,),
    )
    conn.commit()
    _answering({hour: 1.5}, monkeypatch)

    written, skipped = backfill(conn, "token", 30, dry_run=False)

    assert (written, skipped) == (0, 1)
    assert conn.execute(
        "SELECT watts, sample_count, covered_seconds FROM circuit_hourly"
    ).fetchall() == [(2000, 57, 3540)]


def test_running_it_twice_writes_nothing_the_second_time(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    hour = _an_hour_ago()
    _answering({hour: 1.5}, monkeypatch)

    assert backfill(conn, "token", 30, dry_run=False) == (1, 0)
    assert backfill(conn, "token", 30, dry_run=False) == (0, 1)
    assert conn.execute("SELECT count(*) FROM circuit_hourly").fetchone() == (1,)


def test_an_hour_the_cloud_has_no_figure_for_gets_no_row_at_all(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Absence is not zero. A row of NULL watts claiming a full hour of coverage
    # would tell a reader the circuit was watched and drew nothing.
    _answering({}, monkeypatch)

    written, skipped = backfill(conn, "token", 30, dry_run=False)

    assert (written, skipped) == (0, 0)
    assert conn.execute("SELECT count(*) FROM circuit_hourly").fetchone() == (0,)


def test_the_hour_still_running_is_not_stored_from_a_part_of_itself(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Emporia will answer for the hour in progress, and its figure covers only
    # the minutes so far. Stored with a full hour's coverage it would understate
    # the circuit and could never be corrected, since the tool never overwrites.
    now = datetime.now(tz=UTC).replace(minute=0, second=0, microsecond=0)
    current = int(now.timestamp())
    finished = _an_hour_ago()
    _answering({finished: 1.5, current: 0.2}, monkeypatch)

    written, _ = backfill(conn, "token", 30, dry_run=False)

    assert written == 1
    assert conn.execute("SELECT timestamp FROM circuit_hourly").fetchall() == [(finished,)]


def test_a_dry_run_reports_what_it_would_write_and_writes_none_of_it(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    hour = _an_hour_ago()
    _answering({hour: 1.5}, monkeypatch)

    written, _ = backfill(conn, "token", 30, dry_run=True)

    assert written == 1
    assert conn.execute("SELECT count(*) FROM circuit_hourly").fetchone() == (0,)


def test_one_circuit_that_cannot_be_fetched_does_not_stop_the_others(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Thirty-nine circuits and one bad reply. Stopping there would leave the
    # database half filled with no way to tell how far it got.
    conn.execute(
        "INSERT INTO circuit"
        " (id, device_gid, channel_num, name, first_seen, last_seen)"
        " VALUES (2, 100001, '2', 'Bathroom', 0, 0)"
    )
    conn.commit()
    hour = _an_hour_ago()

    def sometimes(
        id_token: str, device_gid: int, channel: str, start: datetime, end: datetime
    ) -> dict[int, float]:
        if device_gid == 100000:
            raise EmporiaError("Emporia returned HTTP 500")
        return {hour: 1.5}

    monkeypatch.setattr(backfill_emporia_history, "_fetch_hours", sometimes)

    written, _ = backfill(conn, "token", 30, dry_run=False)

    assert written == 1
    assert conn.execute("SELECT circuit_id FROM circuit_hourly").fetchall() == [(2,)]
