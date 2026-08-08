"""test_history_totals.py — the period total the History page's footer draws.

The footer used to add up the cost column itself, and the column it added up had
already been rounded to the cent for the reader. Thirty-one roundings of a
thirty-first of a connection charge came to $14.88 against the $15.00 the owner
is billed, so a table of correct rows carried an incorrect total. These tests
pin the total to the thing being totalled: the same energy, priced once.

The counters here climb faster in the afternoon so the peak band carries real
money — with one flat rate a band landing in the wrong place is invisible.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from arraysense.api.app import create_app
from arraysense.collector.service import CollectorService
from arraysense.collector.source import FakeSource
from arraysense.config import Config
from arraysense.models import Sample
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

# The reference installation's own tariff: time-of-use May to October, one flat
# rate the rest of the year, and a connection charge every month regardless.
COSERV_BANDS = (
    "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
    "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
    "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
)
CHICAGO = ZoneInfo("America/Chicago")
FEBRUARY = datetime(2026, 2, 1, tzinfo=CHICAGO)
MARCH = datetime(2026, 3, 1, tzinfo=CHICAGO)
JULY = datetime(2026, 7, 1, tzinfo=CHICAGO)
MID_JULY = datetime(2026, 7, 16, tzinfo=CHICAGO)
AUGUST = datetime(2026, 8, 1, tzinfo=CHICAGO)
FIXED_MONTHLY = 15.0


def _counters(store: SqliteStore, first: datetime, last: datetime, skip: Any = None) -> None:
    """Hourly lifetime counters climbing between two instants."""
    when = first
    imported, load = 1000.0, 4000.0
    while when < last:
        peak = 15 <= when.astimezone(CHICAGO).hour < 20
        imported += 2.5 if peak else 0.8
        load += 4.0 if peak else 1.5
        if skip is None or not skip(when):
            store.append(
                Sample(
                    timestamp=when,
                    readings={
                        "grid_import_energy_total_kwh": round(imported, 1),
                        "load_energy_total_kwh": round(load, 1),
                        "grid_export_energy_total_kwh": 5.0,
                    },
                )
            )
        when += timedelta(hours=1)


def _client(tmp_path: Path, build: Any) -> Any:
    store = SqliteStore(str(tmp_path / "energy.db"), device=TEST_DEVICE)
    build(store)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "energy.db"),
        poll_interval=11.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    client = TestClient(create_app(store=store, service=service, config=config))
    client.put(
        "/api/settings",
        json={"tariff.bands": COSERV_BANDS, "tariff.fixed_monthly": FIXED_MONTHLY},
    )
    return client


def _span(client: Any, first: datetime, last: datetime, **extra: Any) -> Any:
    return client.get(
        "/api/energy",
        params={
            "start": first.isoformat(),
            "end": last.isoformat(),
            "tz": "America/Chicago",
            **extra,
        },
    ).json()


def _july(client: Any, **extra: Any) -> Any:
    return _span(client, JULY, AUGUST, **extra)


def test_the_connection_charge_totals_what_is_billed_not_thirty_one_roundings(
    tmp_path: Path,
) -> None:
    # A thirty-first of fifteen dollars is 0.4838, which every row shows as
    # 0.48. Adding the column up gives 14.88 for a charge the supplier bills at
    # 15.00, and the gap grows with the number of rows rather than staying put.
    with _client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        body = _july(c, period="day", priced=True)
    rows = [b for b in body["buckets"] if b["cost"] is not None]
    assert len(rows) == 31
    assert sum(b["fixed_charge"] for b in rows) == pytest.approx(14.88, abs=0.005)
    assert body["totals"]["fixed_charge"] == pytest.approx(FIXED_MONTHLY, abs=0.005)


def test_the_total_of_the_days_is_the_month_priced_once(tmp_path: Path) -> None:
    # Thirty-one daily rows and one monthly row are two views of one bill, so
    # the footer under the days has to say what the month says. Summing the
    # displayed rows cannot: they are rounded, and the total is not.
    with _client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        daily = _july(c, period="day", priced=True)
        monthly = _july(c, period="month", priced=True)
    month = monthly["buckets"][0]
    assert daily["totals"]["cost"] == month["cost"]
    assert daily["totals"]["energy_cost"] == month["energy_cost"]
    assert daily["totals"]["saved"] == month["saved"]
    # And it is not what the footer used to draw, or there would be nothing to
    # fix — the drift is real money and this pins its direction as well as its
    # size.
    assert sum(b["cost"] for b in daily["buckets"] if b["cost"] is not None) < month["cost"]


def test_a_month_with_an_unpriced_day_still_owes_the_whole_connection_charge(
    tmp_path: Path,
) -> None:
    # The collector was down across the peak window on the 15th, so that day's
    # energy costs an amount nobody can state and is left out of the energy
    # total. The connection charge is not energy. It is owed for being
    # connected, not for being observed, and the supplier bills the whole
    # fifteen dollars for a July whether or not anybody watched the 15th.
    def build(store: SqliteStore) -> None:
        _counters(
            store,
            JULY - timedelta(hours=2),
            AUGUST,
            skip=lambda w: (
                w.astimezone(CHICAGO).day == 15 and 14 <= w.astimezone(CHICAGO).hour < 21
            ),
        )

    with _client(tmp_path, build) as c:
        body = _july(c, period="day", priced=True)
    priced = [b for b in body["buckets"] if b["cost"] is not None]
    assert len(priced) == 30
    totals = body["totals"]
    assert totals["fixed_charge"] == pytest.approx(FIXED_MONTHLY, abs=0.005)
    # Each figure is rounded once from unrounded inputs, so the two parts can
    # be a cent away from the whole. That is the right way round — it is the
    # opposite habit, rounding first and adding after, that this fixes.
    parts = totals["energy_cost"] + totals["fixed_charge"]
    assert totals["cost"] == pytest.approx(parts, abs=0.02)
    # Still the energy of thirty days, and still above the rounded column it
    # replaces: each row lost a fraction of a cent of connection charge.
    assert totals["cost"] > sum(b["cost"] for b in priced)


def test_a_whole_month_of_february_owes_the_same_charge_as_a_whole_july(
    tmp_path: Path,
) -> None:
    # The charge is monthly, so a short month is not a cheap one. Twenty-eight
    # daily rows carry the same fifteen dollars thirty-one rows do, and the
    # rounded column beneath them adds to something else again.
    with _client(tmp_path, lambda s: _counters(s, FEBRUARY - timedelta(hours=2), MARCH)) as c:
        body = _span(c, FEBRUARY, MARCH, period="day", priced=True)
    rows = [b for b in body["buckets"] if b["cost"] is not None]
    assert len(rows) == 28
    assert body["totals"]["fixed_charge"] == pytest.approx(FIXED_MONTHLY, abs=0.005)


def test_half_a_month_owes_half_the_connection_charge(tmp_path: Path) -> None:
    # Calendar coverage is what the charge is shared by. Fifteen days of a
    # thirty-one-day July is fifteen thirty-firsts of fifteen dollars — the one
    # case where the total is genuinely less than a whole month's charge.
    with _client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), MID_JULY)) as c:
        body = _span(c, JULY, MID_JULY, period="day", priced=True)
    rows = [b for b in body["buckets"] if b["cost"] is not None]
    assert len(rows) == 15
    assert body["totals"]["fixed_charge"] == pytest.approx(FIXED_MONTHLY * 15 / 31, abs=0.005)


def test_a_day_nothing_was_recorded_for_at_all_does_not_discount_the_charge(
    tmp_path: Path,
) -> None:
    # The 15th is not merely unpriceable, it is absent from the reply — the
    # counters said nothing about it, so no row is drawn. The month still ran
    # for thirty-one days and the supplier still billed for them.
    def build(store: SqliteStore) -> None:
        _counters(
            store,
            JULY - timedelta(hours=2),
            AUGUST,
            skip=lambda w: w.astimezone(CHICAGO).day == 15,
        )

    with _client(tmp_path, build) as c:
        body = _july(c, period="day", priced=True)
    assert not any(b["start"].startswith("2026-07-15") for b in body["buckets"])
    assert body["totals"]["fixed_charge"] == pytest.approx(FIXED_MONTHLY, abs=0.005)


def test_a_period_nothing_could_be_priced_in_has_no_total_rather_than_zero(
    tmp_path: Path,
) -> None:
    # Every day unpriced is not a free month. The footer draws a dash from this
    # and never a currency symbol in front of nothing.
    def build(store: SqliteStore) -> None:
        _counters(
            store,
            JULY - timedelta(hours=2),
            AUGUST,
            skip=lambda w: 14 <= w.astimezone(CHICAGO).hour < 21,
        )

    with _client(tmp_path, build) as c:
        body = _july(c, period="day", priced=True)
    assert all(b["cost"] is None for b in body["buckets"])
    assert body["totals"]["cost"] is None
    assert body["totals"]["energy_cost"] is None
    assert body["totals"]["fixed_charge"] is None
    assert body["totals"]["saved"] is None


def test_an_install_with_no_tariff_is_told_no_total_at_all(tmp_path: Path) -> None:
    # Not zero and not a dash: with no rates entered there is nothing to say
    # about money, and the page has no total row to draw.
    store = SqliteStore(str(tmp_path / "energy.db"), device=TEST_DEVICE)
    _counters(store, JULY - timedelta(hours=2), AUGUST)
    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "energy.db"),
        poll_interval=11.0,
    )
    service = CollectorService(source=FakeSource(), store=store, interval=3600)
    with TestClient(create_app(store=store, service=service, config=config)) as c:
        body = _july(c, period="day", priced=True)
    assert body["configured"] is False
    assert body["totals"] == {}


def test_a_recorded_month_carries_its_riders_onto_every_priced_day(tmp_path: Path) -> None:
    # PCRF and SCRF are charged per kilowatt-hour on top of the band rate, so
    # they belong on the day's cost and not only on the month's estimate.
    with _client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        c.put("/api/settings", json={"tariff.adjustments": "2026-07 | -0.002000 | 0.005000"})
        body = _july(c, period="day", priced=True)
    day = body["buckets"][0]
    assert day["adjustment_status"] == "applied"
    kwh = day["grid_imported_kwh"]
    assert day["adjustment"] == pytest.approx(kwh * 0.003, abs=0.005)
    assert day["cost"] == pytest.approx(
        day["energy_cost"] + day["fixed_charge"] + day["adjustment"], abs=0.02
    )


def test_a_month_with_no_riders_recorded_cannot_be_priced_and_says_so(
    tmp_path: Path,
) -> None:
    # August's factors are recorded and July's are not. July is not adjusted by
    # nothing — the owner has said this supplier charges riders and has not said
    # what July's were, so July's bill cannot be stated. Pricing it at the base
    # rate would quote a total the supplier will not send, and quote it with no
    # sign that anything was missing.
    with _client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        c.put("/api/settings", json={"tariff.adjustments": "2026-08 | -0.002000 | 0.005000"})
        body = _july(c, period="month", priced=True)
    month = body["buckets"][0]
    assert month["adjustment_status"] == "unknown"
    assert month["adjustment"] is None
    assert month["cost"] is None, "an unknown rider cannot be spent as zero"
    # The parts that ARE known still arrive, so the page can show the energy and
    # the connection charge and say only the total is unavailable.
    assert month["energy_cost"] is not None
    assert month["fixed_charge"] is not None


def test_an_install_with_no_riders_configured_says_none_rather_than_unknown(
    tmp_path: Path,
) -> None:
    with _client(tmp_path, lambda s: _counters(s, JULY - timedelta(hours=2), AUGUST)) as c:
        body = _july(c, period="month", priced=True)
    assert body["buckets"][0]["adjustment_status"] == "none"
    assert body["buckets"][0]["adjustment"] is None
