"""test_emporia_repository.py — storing circuits and their readings.

The two properties worth guarding are identity and absence. A circuit is
``(device_gid, channel_num)``, so renaming it in Emporia's app moves a label
rather than starting a new circuit and stranding a year of readings. And an
absent reading stays absent all the way to the page — an outlet that has been
offline since April must not be drawn as one using no power.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.modules.emporia.parse import Circuit, Reading
from arraysense.modules.emporia.repository import (
    MODULE,
    OWNER,
    ChargerAudit,
    ChargerChange,
    CircuitRepository,
)
from arraysense.store.rollup import rebuild_circuit_hourly
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def _repo(tmp_path: Path) -> tuple[CircuitRepository, SqliteStore]:
    store = SqliteStore(str(tmp_path / "c.db"), device=TEST_DEVICE)
    return CircuitRepository(store), store


def _audit(tmp_path: Path) -> tuple[ChargerAudit, SqliteStore]:
    store = SqliteStore(str(tmp_path / "a.db"), device=TEST_DEVICE)
    return ChargerAudit(store), store


def test_syncing_returns_an_id_per_circuit(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    ids = repo.sync_circuits([Circuit(100000, "1", "Garage plugs", 1.0, "circuit")], NOW)
    assert ids[(100000, "1")] > 0
    store.close()


def test_a_renamed_circuit_keeps_its_id_and_its_history(tmp_path: Path) -> None:
    # The reason identity is the gid and channel. Renaming "Dryer" to "Dryer
    # (garage)" in Emporia's app must move a label, not start a new circuit and
    # strand everything recorded under the old one.
    repo, store = _repo(tmp_path)
    first = repo.sync_circuits([Circuit(100000, "5", "Dryer", 2.0, "circuit")], NOW)
    repo.append_readings([Reading(100000, "5", 3000)], NOW)
    second = repo.sync_circuits([Circuit(100000, "5", "Dryer (garage)", 2.0, "circuit")], NOW)

    assert second[(100000, "5")] == first[(100000, "5")]
    latest = {c.channel_num: c for c in repo.latest()}
    assert latest["5"].name == "Dryer (garage)"
    assert latest["5"].watts == 6000, "the reading recorded under the old name survived"
    store.close()


def test_an_absent_reading_stores_as_null_not_zero(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "1", "Bathroom", 1.0, "circuit")], NOW)
    repo.append_readings([Reading(100000, "1", None)], NOW)
    assert repo.latest()[0].watts is None
    store.close()


def test_the_multiplier_is_applied_when_reading_back(tmp_path: Path) -> None:
    # Stored raw, applied on read, so a correction upstream does not require
    # rewriting history.
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "5", "Dryer", 2.0, "circuit")], NOW)
    repo.append_readings([Reading(100000, "5", 1500)], NOW)
    assert repo.latest()[0].watts == 3000
    store.close()


def test_a_reading_for_an_unknown_circuit_is_dropped_not_invented(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    stored = repo.append_readings([Reading(999999, "1", 100)], NOW)
    assert stored == 0
    assert repo.latest() == []
    store.close()


def test_a_circuit_that_disappears_is_kept_with_its_last_seen(tmp_path: Path) -> None:
    # Its readings stay valid history. Deleting it would delete the past.
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "1", "Bathroom", 1.0, "circuit")], NOW)
    later = NOW + timedelta(days=1)
    repo.sync_circuits([Circuit(100000, "2", "Utility", 1.0, "circuit")], later)

    names = {c.name for c in repo.latest()}
    assert "Bathroom" in names, "a vanished circuit is marked, never deleted"
    store.close()


def test_appending_twice_at_the_same_instant_does_not_duplicate(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "1", "Bathroom", 1.0, "circuit")], NOW)
    repo.append_readings([Reading(100000, "1", 100)], NOW)
    repo.append_readings([Reading(100000, "1", 120)], NOW)
    assert repo.latest()[0].watts == 120, "the later value wins rather than raising"
    store.close()


def test_the_biggest_draw_leads_and_an_unread_circuit_does_not(tmp_path: Path) -> None:
    # The page is read top-down to answer "what is drawing all that". A circuit
    # nobody has heard from has no claim on the top of that list, and it must
    # not outrank a circuit measured at zero either — one is a silence and the
    # other is a fact.
    repo, store = _repo(tmp_path)
    repo.sync_circuits(
        [
            Circuit(100000, "1", "Quiet", 1.0, "circuit"),
            Circuit(100000, "2", "Dryer", 1.0, "circuit"),
            Circuit(100000, "3", "Unread", 1.0, "circuit"),
        ],
        NOW,
    )
    repo.append_readings([Reading(100000, "1", 0), Reading(100000, "2", 3000)], NOW)
    assert [c.name for c in repo.latest()] == ["Dryer", "Quiet", "Unread"]
    store.close()


def test_syncing_nothing_is_not_an_error(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    assert repo.sync_circuits([], NOW) == {}
    assert repo.append_readings([], NOW) == 0
    store.close()


def test_the_same_channel_on_two_monitors_gets_two_ids(tmp_path: Path) -> None:
    # Both Vues have a channel 1. Keyed on the channel alone one house's dryer
    # would overwrite the other's.
    repo, store = _repo(tmp_path)
    ids = repo.sync_circuits(
        [
            Circuit(100000, "1", "A", 1.0, "circuit"),
            Circuit(100001, "1", "B", 1.0, "circuit"),
        ],
        NOW,
    )
    assert ids[(100000, "1")] != ids[(100001, "1")]
    repo.append_readings([Reading(100000, "1", 100), Reading(100001, "1", 200)], NOW)
    latest = {c.name: c.watts for c in repo.latest()}
    assert latest["A"] == 100
    assert latest["B"] == 200
    store.close()


def test_a_fractional_multiplier_lands_on_a_whole_watt(tmp_path: Path) -> None:
    # 7 x 2.5 is 17.5, and Python rounds half to even, so this is 18 rather
    # than 17. Stated here because a chart that disagrees with a table by one
    # watt is the sort of thing that gets chased for an afternoon.
    repo, store = _repo(tmp_path)
    repo.sync_circuits(
        [
            Circuit(100000, "1", "Plain", 1.0, "circuit"),
            Circuit(100000, "2", "Scaled", 2.5, "circuit"),
        ],
        NOW,
    )
    repo.append_readings([Reading(100000, "1", 7), Reading(100000, "2", 7)], NOW)
    latest = {c.channel_num: c.watts for c in repo.latest()}
    assert latest["1"] == 7
    assert latest["2"] == 18
    store.close()


def test_when_nothing_has_been_read_the_list_falls_back_to_names(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    repo.sync_circuits(
        [
            Circuit(100000, "3", "Zebra", 1.0, "circuit"),
            Circuit(100000, "1", "Apple", 1.0, "circuit"),
            Circuit(100000, "2", "Mango", 1.0, "circuit"),
        ],
        NOW,
    )
    latest = repo.latest()
    assert [c.name for c in latest] == ["Apple", "Mango", "Zebra"]
    assert all(c.watts is None for c in latest)
    store.close()


def test_a_reading_carries_the_instant_it_was_stored(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "1", "A", 1.0, "circuit")], NOW)
    repo.append_readings([Reading(100000, "1", 100)], NOW)
    assert repo.latest()[0].ts == int(NOW.timestamp())
    store.close()


def test_only_the_readings_with_a_known_circuit_are_counted(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "1", "A", 1.0, "circuit")], NOW)
    accepted = repo.append_readings(
        [
            Reading(100000, "1", 100),
            Reading(999999, "1", 200),
            Reading(100000, "1", 300),
        ],
        NOW,
    )
    # Two readings were accepted; they share an instant and a circuit, so the
    # later one replaces the earlier and one row remains. The count is what was
    # taken in, not how many rows survived the conflict.
    assert accepted == 2
    assert repo.latest()[0].watts == 300
    store.close()


def test_a_second_sync_updates_the_multiplier_and_the_kind(tmp_path: Path) -> None:
    # Emporia is the authority on both. A clamp moved to a 240 V circuit must
    # start doubling, and it must do so without a migration.
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "1", "A", 1.0, "circuit")], NOW)
    repo.sync_circuits([Circuit(100000, "1", "A", 2.5, "outlet")], NOW + timedelta(hours=1))
    repo.append_readings([Reading(100000, "1", 4)], NOW + timedelta(hours=2))
    latest = repo.latest()[0]
    assert latest.kind == "outlet"
    assert latest.watts == 10
    store.close()


def test_a_circuits_category_survives_the_round_trip(tmp_path: Path) -> None:
    # The page marks a row with an icon chosen from this. Lost in storage, the
    # icon could only come back by re-reading the device list, which happens
    # once a day.
    repo, store = _repo(tmp_path)
    repo.sync_circuits(
        [
            Circuit(100000, "8", "air conditioner main", 2.0, "circuit", type_gid=1),
            Circuit(100000, "7", "Device 100000 ch 7", 1.0, "circuit"),
        ],
        NOW,
    )
    got = {c.channel_num: c for c in repo.latest()}
    assert got["8"].type_gid == 1
    assert got["7"].type_gid is None, "no category is absent, never a zero"
    store.close()


def test_a_recategorised_circuit_takes_the_new_category(tmp_path: Path) -> None:
    repo, store = _repo(tmp_path)
    repo.sync_circuits([Circuit(100000, "8", "AC", 2.0, "circuit", type_gid=1)], NOW)
    repo.sync_circuits([Circuit(100000, "8", "AC", 2.0, "circuit", type_gid=11)], NOW)
    assert repo.latest()[0].type_gid == 11
    store.close()


# --- reading a circuit's history ------------------------------------------


def test_history_applies_the_multiplier_to_watts_and_kwh(tmp_path: Path) -> None:
    # Both tiers store one leg of a 240 V circuit. A dryer stored at 2,000 W
    # with a multiplier of 2.0 really drew 4,000 W, and an endpoint that
    # forgot would halve every large appliance on the page — quietly, and in a
    # direction that looks plausible.
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    ids = repo.sync_circuits([Circuit(100000, "1,2,3", "Dryer", 2.0, "circuit")], start)
    for minute in range(4):
        repo.append_readings([Reading(100000, "1,2,3", 2000)], start + timedelta(minutes=minute))

    got = repo.history(start, start + timedelta(minutes=5), tier="full")

    (series,) = got.series
    assert series.circuit_id == ids[(100000, "1,2,3")]
    assert series.watts == (4000, 4000, 4000, 4000)
    # 4,000 W held across four one-minute readings is 4000 * (4/60) / 1000 kWh.
    assert series.kwh == pytest.approx(4000 * (4 / 60) / 1000)


def test_history_keeps_a_silent_reading_as_none(tmp_path: Path) -> None:
    # A circuit that was listed but did not answer stores NULL. Rendering that
    # as zero is the defect this whole project exists to avoid: a dead outlet
    # and an idle one are different facts.
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "4", "Porch", 1.0, "circuit")], start)
    repo.append_readings([Reading(100000, "4", 40)], start)
    repo.append_readings([Reading(100000, "4", None)], start + timedelta(minutes=1))
    repo.append_readings([Reading(100000, "4", 41)], start + timedelta(minutes=2))

    got = repo.history(start, start + timedelta(minutes=3), tier="full")

    (series,) = got.series
    assert series.watts == (40, None, 41)


def test_history_gives_a_circuit_with_no_readings_a_null_kwh(tmp_path: Path) -> None:
    # Not zero kWh. A circuit offline since April used no measured energy and
    # also used an unknown amount; zero would assert the first and hide the
    # second, and the page has to be able to say "offline" rather than "0.0".
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "9", "Shed outlet", 1.0, "circuit")], start)

    got = repo.history(start, start + timedelta(hours=1), tier="full")

    (series,) = got.series
    assert series.kwh is None
    assert set(series.watts) <= {None}


def test_history_marks_a_partly_recorded_hour_partial(tmp_path: Path) -> None:
    # circuit_hourly carries sample_count precisely so an hour built from two
    # readings does not claim the coverage of one built from sixty. Energy read
    # off the average without it overstates a partly recorded hour.
    repo, store = _repo(tmp_path)
    hour = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1,2,3", "Dryer", 1.0, "circuit")], hour)
    for minute in (0, 1):
        repo.append_readings([Reading(100000, "1,2,3", 3000)], hour + timedelta(minutes=minute))
    # Integer epoch seconds, not datetimes.
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=60,
    )

    got = repo.history(hour, hour + timedelta(hours=1), tier="hourly")

    (series,) = got.series
    assert series.partial is True
    # Two readings a minute apart is two minutes of coverage, not sixty.
    assert series.kwh == pytest.approx(3000 * (2 / 60) / 1000)


def test_history_marks_a_half_recorded_hour_partial_at_a_fast_poll(tmp_path: Path) -> None:
    # settings.py permits a 10 s interval (lower=10). Thirty minutes of it is
    # 180 samples, which at a hard-coded 60 s multiplies past a full hour and
    # clamps to one — reporting a half-recorded hour as whole. Told the real
    # cadence, the same row is 1,800 seconds and says so.
    repo, store = _repo(tmp_path)
    hour = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], hour)
    for tick in range(180):
        repo.append_readings([Reading(100000, "1", 1000)], hour + timedelta(seconds=10 * tick))
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=10,
    )

    got = repo.history(hour, hour + timedelta(hours=1), tier="hourly", cadence_seconds=10)

    (series,) = got.series
    assert series.partial is True
    # Half an hour at 1 kW is 0.5 kWh, not the 1.0 a clamped full hour claims.
    assert series.kwh == pytest.approx(0.5)


def test_history_can_be_narrowed_to_named_circuits(tmp_path: Path) -> None:
    # The page draws five strips out of thirty-nine. Fetching all of them and
    # discarding thirty-four is the query this argument exists to avoid.
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    ids = repo.sync_circuits(
        [
            Circuit(100000, "1", "Dryer", 1.0, "circuit"),
            Circuit(100000, "2", "Porch", 1.0, "circuit"),
        ],
        start,
    )
    repo.append_readings([Reading(100000, "1", 3000), Reading(100000, "2", 40)], start)

    got = repo.history(
        start, start + timedelta(minutes=1), tier="full", circuit_ids=[ids[(100000, "1")]]
    )

    assert [s.name for s in got.series] == ["Dryer"]


def test_history_ranks_by_energy_not_by_the_last_reading(tmp_path: Path) -> None:
    # A circuit at 5 kW for one minute used less than one at 1 kW for half an
    # hour. Ranking on the latest watts would answer "what is on now", which the
    # live list already answers, rather than "what ate the power".
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits(
        [
            Circuit(100000, "1", "Kettle", 1.0, "circuit"),
            Circuit(100000, "2", "Heat pump", 1.0, "circuit"),
        ],
        start,
    )
    repo.append_readings([Reading(100000, "1", 5000)], start)
    for minute in range(30):
        repo.append_readings([Reading(100000, "2", 1000)], start + timedelta(minutes=minute))

    got = repo.history(start, start + timedelta(hours=1), tier="full")

    assert [s.name for s in got.series] == ["Heat pump", "Kettle"]


def test_history_returns_nothing_rather_than_raising_on_a_database_error(
    tmp_path: Path,
) -> None:
    # latest() already swallows sqlite3.Error into a warning and an empty list:
    # this runs unattended and a page that 500s tells the owner less than a page
    # that says it has no circuits. history() matches it rather than inventing a
    # second policy for the same failure.
    repo, store = _repo(tmp_path)
    store._conn.execute("DROP TABLE circuit_reading")
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    got = repo.history(start, start + timedelta(hours=1), tier="full")

    assert got.series == ()


def test_history_breaks_the_series_where_nothing_was_recorded(tmp_path: Path) -> None:
    # The defect this guards is a line, not a number. The stamps come from the
    # rows that exist, so two readings three hours apart land next to each other
    # in the array and uPlot — which breaks a line only at a null — draws a
    # straight diagonal across the outage. A reader sees an air conditioner
    # ramping down over three hours that were never measured at all.
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "8", "Air conditioner", 1.0, "circuit")], start)
    for minute in range(5):
        repo.append_readings([Reading(100000, "8", 2000)], start + timedelta(minutes=minute))
    resume = start + timedelta(hours=3)
    for minute in range(5):
        repo.append_readings([Reading(100000, "8", 2100)], resume + timedelta(minutes=minute))

    got = repo.history(start, resume + timedelta(minutes=10), tier="full")

    (series,) = got.series
    quiet = int((start + timedelta(minutes=4)).timestamp()) + 60
    assert quiet in got.timestamps, "the break sits one cadence after the last real reading"
    assert series.watts[got.timestamps.index(quiet)] is None
    # Exactly one of them, and it is the synthetic one: filling the whole hole
    # with nulls would add a hundred and seventy points to say what one says.
    assert series.watts.count(None) == 1
    assert len(got.timestamps) == 11


def test_history_does_not_break_between_ordinary_consecutive_readings(tmp_path: Path) -> None:
    # The other half of the rule. A break inserted between readings a cadence
    # apart would dash every line on the page and make a healthy poller look
    # like a failing one.
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Fridge", 1.0, "circuit")], start)
    for minute in range(6):
        repo.append_readings([Reading(100000, "1", 120)], start + timedelta(minutes=minute))

    got = repo.history(start, start + timedelta(minutes=10), tier="full")

    (series,) = got.series
    assert got.timestamps == tuple(
        int((start + timedelta(minutes=minute)).timestamp()) for minute in range(6)
    )
    assert None not in series.watts


def test_a_gap_changes_the_line_and_not_the_energy(tmp_path: Path) -> None:
    # A stretch nobody recorded is missing knowledge, not missing energy: the
    # synthetic reading is worth nothing, so the ten readings either side of the
    # hole add up to exactly what the same ten in a row do. A break that moved
    # the kWh would be pricing a circuit off a drawing decision.
    dense, holed = tmp_path / "dense", tmp_path / "holed"
    dense.mkdir()
    holed.mkdir()
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)

    unbroken, _store = _repo(dense)
    unbroken.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], start)
    for minute in range(10):
        unbroken.append_readings([Reading(100000, "1", 3000)], start + timedelta(minutes=minute))
    steady = unbroken.history(start, start + timedelta(hours=4), tier="full")

    broken, _store2 = _repo(holed)
    broken.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], start)
    for minute in range(5):
        broken.append_readings([Reading(100000, "1", 3000)], start + timedelta(minutes=minute))
    resume = start + timedelta(hours=3)
    for minute in range(5):
        broken.append_readings([Reading(100000, "1", 3000)], resume + timedelta(minutes=minute))
    interrupted = broken.history(start, start + timedelta(hours=4), tier="full")

    assert interrupted.series[0].watts.count(None) == 1, "the interrupted window really broke"
    assert interrupted.series[0].kwh == steady.series[0].kwh
    # Ten readings a minute apart at 3 kW, and not a watt-second of the hole.
    assert interrupted.series[0].kwh == pytest.approx(3000 * (10 / 60) / 1000)


def test_a_gap_does_not_make_a_full_hour_look_thinly_sampled(tmp_path: Path) -> None:
    # ``partial`` says this bucket was thinly sampled, which is a claim about
    # the hours that were recorded. An hour recorded end to end stays whole
    # however long the module was off either side of it, and a flag raised by
    # the break would put a "part of the window" label on a complete figure.
    repo, store = _repo(tmp_path)
    first = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    third = first + timedelta(hours=2)
    repo.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], first)
    for hour in (first, third):
        for tick in range(60):
            repo.append_readings([Reading(100000, "1", 1000)], hour + timedelta(minutes=tick))
    rebuild_circuit_hourly(
        store._conn,
        int(first.timestamp()),
        int((third + timedelta(hours=1)).timestamp()),
        cadence_seconds=60,
    )

    got = repo.history(first, third + timedelta(hours=1), tier="hourly")

    (series,) = got.series
    assert series.partial is False
    assert series.watts.count(None) == 1, "the missing hour still broke the line"
    # Two whole hours at 1 kW. The hour nobody recorded contributes nothing.
    assert series.kwh == pytest.approx(2.0)


def test_the_hourly_tier_breaks_a_missing_hour_at_the_hour(tmp_path: Path) -> None:
    # An hourly row covers an hour whatever the poll interval is, so the cadence
    # this tier is judged against is 3,600 seconds. Measured against a sixty
    # second poll instead, the break would land a minute after the last bucket
    # rather than on the hour that went unrecorded — pointing at the wrong
    # moment on a chart whose whole job is to say when.
    repo, store = _repo(tmp_path)
    first = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    third = first + timedelta(hours=2)
    repo.sync_circuits([Circuit(100000, "1", "Oven", 1.0, "circuit")], first)
    for hour in (first, third):
        for tick in (0, 30):
            repo.append_readings([Reading(100000, "1", 2000)], hour + timedelta(minutes=tick))
    rebuild_circuit_hourly(
        store._conn,
        int(first.timestamp()),
        int((third + timedelta(hours=1)).timestamp()),
        cadence_seconds=60,
    )

    got = repo.history(first, third + timedelta(hours=1), tier="hourly")

    (series,) = got.series
    missing = int((first + timedelta(hours=1)).timestamp())
    assert got.timestamps == (int(first.timestamp()), missing, int(third.timestamp()))
    assert series.watts[1] is None


def test_a_sparse_raw_window_credits_one_interval_per_reading(tmp_path: Path) -> None:
    # The raw tier records no cadence of its own, so a reader has only the
    # interval in force and the spacing it can measure. Trusting the spacing
    # read two readings three hours apart as six kilowatt-hours where thirty
    # watt-hours were measured — a reading is a sample of one poll period, and
    # the polls that were never taken are unknown rather than the neighbours'.
    repo, _store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Freezer", 1.0, "circuit")], start)
    repo.append_readings([Reading(100000, "1", 1000)], start)
    repo.append_readings([Reading(100000, "1", 1000)], start + timedelta(hours=3))

    got = repo.history(start, start + timedelta(hours=4), tier="full", cadence_seconds=60)

    (series,) = got.series
    # Two readings, one minute each, at a kilowatt.
    assert series.kwh == pytest.approx(2 * 60 * 1000 / 3_600_000)
    assert got.recorded_seconds == 120, "two polls is two minutes recorded, not three hours"


def test_a_window_that_is_mostly_hole_still_breaks_its_line(tmp_path: Path) -> None:
    # The other half of the same defect. The break threshold came from the
    # window's own spacing, and a window holding one gap measures that gap as
    # its ordinary spacing — so the sparsest window there is, the one that most
    # needs the break, was the one that never got it and drew a straight line
    # across three unrecorded hours.
    repo, store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Freezer", 1.0, "circuit")], start)
    repo.append_readings([Reading(100000, "1", 1000)], start)
    repo.append_readings([Reading(100000, "1", 1000)], start + timedelta(hours=3))

    got = repo.history(start, start + timedelta(hours=4), tier="full", cadence_seconds=60)

    (series,) = got.series
    assert series.watts.count(None) == 1
    assert got.timestamps[1] == int(start.timestamp()) + 60, (
        "the break sits at the first instant a reading should have appeared"
    )
    store.close()


def test_history_recorded_at_a_slower_interval_than_the_setting_is_not_inflated(
    tmp_path: Path,
) -> None:
    # The residual, pinned rather than left implicit. Readings taken an hour
    # apart and read back under a sixty-second setting account for a minute
    # each, not an hour each. That under-reports, and it is the direction to be
    # wrong in: the alternative over-reported a sparse window two hundredfold.
    repo, store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Freezer", 1.0, "circuit")], start)
    for hour in range(4):
        repo.append_readings([Reading(100000, "1", 1000)], start + timedelta(hours=hour))

    got = repo.history(start, start + timedelta(hours=4), tier="full", cadence_seconds=60)

    assert got.series[0].kwh == pytest.approx(4 * 60 * 1000 / 3_600_000)
    store.close()


def test_a_stored_hour_does_not_move_when_the_poll_interval_changes(tmp_path: Path) -> None:
    # The rollup writes down how much of the hour its readings account for,
    # measured while the interval that produced them was still in force. Read
    # back under a setting since raised from ten seconds to sixty, the same
    # hour used to report twice the energy and call itself whole; now the
    # setting cannot reach it at all.
    repo, store = _repo(tmp_path)
    hour = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], hour)
    for tick in range(180):
        repo.append_readings([Reading(100000, "1", 1000)], hour + timedelta(seconds=10 * tick))
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=10,
    )

    at_ten = repo.history(hour, hour + timedelta(hours=1), tier="hourly", cadence_seconds=10)
    at_sixty = repo.history(hour, hour + timedelta(hours=1), tier="hourly", cadence_seconds=60)

    assert at_ten.series[0].kwh == pytest.approx(0.5)
    assert at_sixty.series[0].kwh == pytest.approx(0.5)
    assert at_ten.series[0].partial is True
    assert at_sixty.series[0].partial is True
    store.close()


def test_an_hour_stored_before_coverage_was_recorded_falls_back_to_the_count(
    tmp_path: Path,
) -> None:
    # An installation recording circuits since 1.1.0 has hours with no coverage
    # figure, and their raw readings are pruned at thirty days so the
    # measurement cannot be made after the fact. Those keep the old guess —
    # sample count times the interval now in force — because an old hour read
    # imperfectly beats an old hour refused. The fallback is deliberate and
    # visible; what must never happen is a row like this reading as zero.
    repo, store = _repo(tmp_path)
    hour = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Heat pump", 1.0, "circuit")], hour)
    for tick in range(30):
        repo.append_readings([Reading(100000, "1", 1000)], hour + timedelta(minutes=tick))
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=60,
    )
    with store._conn:
        store._conn.execute("UPDATE circuit_hourly SET covered_seconds = NULL")

    got = repo.history(hour, hour + timedelta(hours=1), tier="hourly", cadence_seconds=60)

    (series,) = got.series
    assert series.kwh == pytest.approx(0.5), "thirty readings at a minute each is half an hour"
    assert series.partial is True
    store.close()


def test_recorded_seconds_counts_coverage_and_not_the_buckets_that_hold_it(
    tmp_path: Path,
) -> None:
    # What the span check turns on, and the figure the browser used to get
    # wrong from the other side: it credited every hourly bucket holding
    # anything with a full 3,600 seconds, so a week of one reading an hour read
    # as a week recorded and no window was ever short.
    repo, store = _repo(tmp_path)
    first = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Oven", 1.0, "circuit")], first)
    for hour in range(3):
        repo.append_readings([Reading(100000, "1", 900)], first + timedelta(hours=hour))
    rebuild_circuit_hourly(
        store._conn,
        int(first.timestamp()),
        int((first + timedelta(hours=3)).timestamp()),
        cadence_seconds=60,
    )

    got = repo.history(first, first + timedelta(hours=3), tier="hourly", cadence_seconds=60)

    assert got.recorded_seconds == 180, "three readings a minute each, not three hours"
    store.close()


def test_recorded_seconds_describes_the_module_even_when_one_circuit_is_asked_for(
    tmp_path: Path,
) -> None:
    # A poll that reached one clamp reached the monitor, so this is a fact about
    # the module. Measured after the narrowing, a request for the shed outlet
    # that has been offline since April reported the module as barely running
    # and the endpoint withheld a share the module could honestly support.
    repo, store = _repo(tmp_path)
    start = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)
    ids = repo.sync_circuits(
        [
            Circuit(100000, "1", "Dryer", 1.0, "circuit"),
            Circuit(100000, "9", "Shed outlet", 1.0, "circuit"),
        ],
        start,
    )
    for minute in range(60):
        when = start + timedelta(minutes=minute)
        readings = [Reading(100000, "1", 1000)]
        # The outlet stops answering half way through; the monitor does not.
        if minute < 30:
            readings.append(Reading(100000, "9", 40))
        repo.append_readings(readings, when)

    shed = repo.history(
        start,
        start + timedelta(hours=1),
        tier="full",
        circuit_ids=[ids[(100000, "9")]],
        cadence_seconds=60,
    )
    everything = repo.history(start, start + timedelta(hours=1), tier="full", cadence_seconds=60)

    assert shed.recorded_seconds == everything.recorded_seconds == 3600
    assert [s.name for s in shed.series] == ["Shed outlet"], "the series is still narrowed"
    store.close()


# --- circuit energy split by rate band ------------------------------------


def _fill_hour(
    repo: CircuitRepository,
    store: SqliteStore,
    device_gid: int,
    channel: str,
    hour: datetime,
    *,
    watts: int,
    minutes: int = 60,
    cadence_seconds: int = 60,
) -> None:
    # One reading a minute for `minutes` minutes from the top of `hour`, then
    # the real rollup over it -- so band_kwh is tested against a circuit_hourly
    # row built the way a production one is, not one typed into the table by
    # hand. `minutes < 60` is how the thinly-covered-hour case is built: the
    # rollup measures real coverage from the spacing between real readings.
    for minute in range(minutes):
        repo.append_readings(
            [Reading(device_gid, channel, watts)], hour + timedelta(minutes=minute)
        )
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=cadence_seconds,
    )


def _null_hour(
    repo: CircuitRepository,
    store: SqliteStore,
    device_gid: int,
    channel: str,
    hour: datetime,
    *,
    cadence_seconds: int = 60,
) -> None:
    # An hour where the circuit was listed and never answered once -- every
    # reading it saw that hour carries no watts, so the real rollup writes a
    # row with a NULL average and a sample count of nought rather than no row
    # at all (rebuild_circuit_hourly's own docstring: "An hour in which
    # nothing was heard averages to NULL rather than to zero"). That is the
    # row band_kwh has to notice and label, not a gap in the timestamps.
    for minute in range(60):
        repo.append_readings([Reading(device_gid, channel, None)], hour + timedelta(minutes=minute))
    rebuild_circuit_hourly(
        store._conn,
        int(hour.timestamp()),
        int((hour + timedelta(hours=1)).timestamp()),
        cadence_seconds=cadence_seconds,
    )


def test_band_kwh_splits_a_circuits_energy_between_the_bands_it_ran_in(tmp_path: Path) -> None:
    # The arithmetic the whole panel rests on: one circuit, two bands, and the
    # energy landing where the clock says it did.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    # Two whole hours at 1000 W: 1 kWh each, one in each band.
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=16), watts=1000)
    intervals = [
        (day, day + timedelta(hours=15), "off-peak"),
        (day + timedelta(hours=15), day + timedelta(hours=20), "peak"),
        (day + timedelta(hours=20), day + timedelta(days=1), "off-peak"),
    ]

    result = repo.band_kwh(day, day + timedelta(days=1), intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    assert energy.by_band["off-peak"] == pytest.approx(1.0)
    assert energy.by_band["peak"] == pytest.approx(1.0)
    store.close()


def test_band_kwh_applies_the_multiplier(tmp_path: Path) -> None:
    # circuit_hourly.watts is stored unmultiplied. A 240 V circuit reads half
    # without this, which under-prices the dryer, the oven and both air
    # conditioners by exactly half.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Oven", 2.0, "circuit")], day)
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    intervals = [(day, day + timedelta(days=1), "off-peak")]

    result = repo.band_kwh(day, day + timedelta(days=1), intervals, tier="hourly")

    assert {c.name: c for c in result}["Oven"].by_band["off-peak"] == pytest.approx(2.0)
    store.close()


def test_band_kwh_reports_no_entry_for_a_band_a_circuit_never_touched(tmp_path: Path) -> None:
    # Not 0.0. The band occurred and this circuit was never heard from in it.
    # spend.CircuitEnergy's own contract treats an absent key and a None value
    # as the same claim, so this checks membership rather than `is None` --
    # asserting `is None` would raise KeyError instead of failing cleanly.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    intervals = [
        (day, day + timedelta(hours=15), "off-peak"),
        (day + timedelta(hours=15), day + timedelta(hours=20), "peak"),
    ]

    result = repo.band_kwh(day, day + timedelta(hours=20), intervals, tier="hourly")

    assert "peak" not in {c.name: c for c in result}["Dryer"].by_band
    store.close()


def test_band_kwh_marks_the_band_whose_hour_was_thinly_covered(tmp_path: Path) -> None:
    # The flag the page labels a figure from, and it belongs to the band
    # rather than to the circuit.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    # Hours 11-14 are filled too, fully, so off-peak has no hole in its own
    # story -- an unfilled stretch there would also be a bucket with no row
    # at all, which is its own case with its own test
    # (test_band_kwh_marks_a_band_partial_when_an_hour_is_missing_entirely)
    # and would mark off-peak partial for a reason this test is not about.
    # Hour 15 is left unfilled: it falls in "peak", the band already expected
    # to come back partial from hour 16's thin coverage, and it contributes
    # no energy either way, so it does not disturb the 0.25 kWh below.
    for hour in range(11, 15):
        _fill_hour(repo, store, 100000, "1", day + timedelta(hours=hour), watts=1000)
    # Fifteen one-minute-apart readings cover fifteen of the hour's sixty
    # minutes -- 900 of 3,600 seconds.
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=16), watts=1000, minutes=15)
    intervals = [
        (day, day + timedelta(hours=15), "off-peak"),
        (day + timedelta(hours=15), day + timedelta(hours=20), "peak"),
    ]

    result = repo.band_kwh(day, day + timedelta(hours=20), intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    assert energy.partial_bands == frozenset({"peak"})
    # 1 kW held for 900 of the hour's 3,600 seconds is a quarter of a kWh.
    assert energy.by_band["peak"] == pytest.approx(0.25)
    store.close()


def test_band_kwh_spreads_a_half_covered_hour_evenly_across_a_boundary_inside_it(
    tmp_path: Path,
) -> None:
    # Pins the choice of span in band_kwh's proportioning, not just the total.
    # A stored hour's covered_seconds says *how much* of the hour was
    # measured, never *where* -- the row does not carry that. Anchoring the
    # split at the reading's own covered duration (span = covered) looks like
    # a tightening of the real span = _HOUR_SECONDS, and it would pass every
    # other case in this file, because the other cases keep a partial hour
    # entirely inside one band, where the two choices agree. Here a boundary
    # sits at the hour's midpoint, and the two choices diverge hard: span =
    # covered would book the whole 0.5 kWh into the first half and nothing
    # into the second (its 1,800-second span ends exactly at the boundary and
    # never reaches the far side). The real code assumes the measured energy
    # was spread evenly across the *hour*, so the boundary at the midpoint
    # splits it down the middle instead: 0.25 kWh on each side.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    hour = day + timedelta(hours=10)
    # Thirty one-minute-apart readings cover half the hour -- 1,800 of 3,600
    # seconds, through the real rollup rather than a hand-written
    # covered_seconds.
    _fill_hour(repo, store, 100000, "1", hour, watts=1000, minutes=30)
    intervals = [
        (hour, hour + timedelta(minutes=30), "off-peak"),
        (hour + timedelta(minutes=30), hour + timedelta(hours=1), "peak"),
    ]

    result = repo.band_kwh(day, day + timedelta(days=1), intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    # Not 0.5 / 0.0, which is the answer span = covered would give.
    assert energy.by_band["off-peak"] == pytest.approx(0.25)
    assert energy.by_band["peak"] == pytest.approx(0.25)
    assert energy.partial_bands == frozenset({"off-peak", "peak"})
    store.close()


def test_band_kwh_does_not_double_apportion_the_in_progress_hour(tmp_path: Path) -> None:
    # The current month, always: a window ending mid-hour asks about a bucket
    # whose covered_seconds already IS the part of the hour that has
    # happened, because the rest of it has not happened yet. Spreading that
    # already-exact figure across a remainder that does not exist applies the
    # same 30-of-60-minutes fraction a second time and halves it.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    hour = day + timedelta(hours=12)
    # Thirty one-minute-apart readings, the real rollup's own coverage for an
    # hour that has only half elapsed.
    _fill_hour(repo, store, 100000, "1", hour, watts=1000, minutes=30)
    window_end = hour + timedelta(minutes=30)
    intervals = [(hour, window_end, "off-peak")]

    # ``now`` is pinned to the window's own end, matching the current month
    # this test is about: the hour genuinely has not finished yet, which is
    # what makes covered_seconds a real prefix rather than a guess.
    result = repo.band_kwh(day, window_end, intervals, tier="hourly", now=window_end)

    energy = {c.name: c for c in result}["Dryer"]
    # Not 0.25, which is what applying the 30-of-60-minute fraction twice gives.
    assert energy.by_band["off-peak"] == pytest.approx(0.5)
    store.close()


def test_band_kwh_reads_the_hour_the_windows_start_lands_inside(tmp_path: Path) -> None:
    # Local midnight in a zone off a whole-hour UTC offset -- Asia/Kolkata, at
    # +5:30 -- puts the query's own start half an hour into an hourly bucket.
    # circuit_hourly stamps a bucket at the floor of the hour it covers, so
    # that bucket sits *before* first and a plain ``timestamp >= first``
    # dropped it whole, losing the slice of it the window actually asked for
    # instead of apportioning it the way an interior band boundary is.
    repo, store = _repo(tmp_path)
    hour = datetime(2026, 7, 1, 10, 0, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], hour)
    _fill_hour(repo, store, 100000, "1", hour, watts=1000)  # a full, fully-recorded hour
    window_start = hour + timedelta(minutes=30)
    window_end = hour + timedelta(hours=2)
    intervals = [(window_start, window_end, "off-peak")]

    result = repo.band_kwh(window_start, window_end, intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    # Only the second half of the hour lies inside the window; not 0.0, which
    # is what dropping the bucket entirely gives, and not 1.0, which is what
    # counting the whole hour without clipping to the window's own start gives.
    assert energy.by_band["off-peak"] == pytest.approx(0.5)
    store.close()


def test_band_kwh_ignores_a_stretch_no_band_covers(tmp_path: Path) -> None:
    # A gap in the schedule is unpriced energy, not energy in a band named
    # None -- the Costs page already warns about unpriced minutes separately.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    intervals = [(day, day + timedelta(days=1), None)]

    result = repo.band_kwh(day, day + timedelta(days=1), intervals, tier="hourly")

    assert {c.name: c for c in result}["Dryer"].by_band == {}
    store.close()


def test_band_kwh_yields_nothing_rather_than_raising_on_a_database_error(tmp_path: Path) -> None:
    # The same rule history() follows: this runs unattended on someone's
    # inverter, and a page with no circuits tells the owner more than a 500.
    # sqlite3.Connection.execute is a read-only slot -- it cannot be
    # monkeypatched onto an instance -- so this forces the same real error
    # history()'s own database-error test does: drop a table the query needs.
    repo, store = _repo(tmp_path)
    store._conn.execute("DROP TABLE circuit")
    day = datetime(2026, 7, 1, tzinfo=UTC)

    assert repo.band_kwh(day, day + timedelta(days=1), []) == ()
    store.close()


def test_band_kwh_does_not_over_credit_a_fully_recorded_hour_the_window_cuts_into(
    tmp_path: Path,
) -> None:
    # The critical defect, reproduced exactly: a fully-recorded hour queried
    # through a window that starts and ends inside it. The old guard,
    # ``hour_end > last``, only asks whether the query ends before the hour's
    # nominal end -- true here even though nothing about this row is actually
    # partial -- and shrank span to ``last - when`` anyway, crediting three
    # quarters of the hour's energy (0.75 kWh) to a query that asked about
    # exactly half of it.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    hour = day + timedelta(hours=12)
    _fill_hour(repo, store, 100000, "1", hour, watts=1000)  # a full, fully-recorded hour
    window_start = hour + timedelta(minutes=10)
    window_end = hour + timedelta(minutes=40)
    intervals = [(window_start, window_end, "off-peak")]

    result = repo.band_kwh(window_start, window_end, intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    # 1000 W held for the 30 minutes the window actually covers is 0.5 kWh --
    # not the 0.75 kWh the old formula returned.
    assert energy.by_band["off-peak"] == pytest.approx(0.5)
    store.close()


def test_band_kwh_does_not_over_credit_a_fully_recorded_final_hour_cut_by_last(
    tmp_path: Path,
) -> None:
    # The reachable shape behind the critical defect: for a site whose local
    # UTC offset is not a whole number of hours (Asia/Kolkata, +5:30), a
    # calendar month's boundary does not land on a UTC hour, so the last hour
    # of every month is a fully-recorded hour whose nominal end merely exceeds
    # the query's own ``last`` -- exactly the test a genuinely in-progress
    # hour passes, and the old code had no way to tell a fully recorded hour
    # cut short by an arbitrary ``last`` from one still being written.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    hour = day + timedelta(hours=12)
    _fill_hour(repo, store, 100000, "1", hour, watts=1000)  # a full, fully-recorded hour
    window_end = hour + timedelta(minutes=15)
    intervals = [(day, window_end, "off-peak")]

    result = repo.band_kwh(day, window_end, intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    # Only the first 15 of the hour's 60 minutes lie inside the window: 0.25
    # kWh -- not the full hour's 1.0 kWh the old formula returned once
    # ``overlap / span`` came out to exactly 1.
    assert energy.by_band["off-peak"] == pytest.approx(0.25)
    store.close()


def test_band_kwh_marks_a_band_partial_when_one_of_its_hours_reported_nothing(
    tmp_path: Path,
) -> None:
    # A NULL-watts hourly row -- a circuit listed and never answered for a
    # whole hour, evidenced by the row itself rather than inferred from a
    # gap in the timestamps -- was silently skipped before anything recorded
    # that its band lost an hour. A circuit with one valid 1 kW hour and one
    # such silent hour in the same band must not report a complete-looking
    # 1 kWh with nothing marking it otherwise.
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits(
        [
            Circuit(100000, "1", "Dryer", 1.0, "circuit"),
            Circuit(100000, "2", "Shed outlet", 1.0, "circuit"),
        ],
        day,
    )
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    _null_hour(repo, store, 100000, "1", day + timedelta(hours=11))  # same band, no answer
    _null_hour(repo, store, 100000, "2", day + timedelta(hours=10))  # never answers at all
    intervals = [(day, day + timedelta(days=1), "off-peak")]

    result = repo.band_kwh(day, day + timedelta(days=1), intervals, tier="hourly")
    by_name = {c.name: c for c in result}

    dryer = by_name["Dryer"]
    assert dryer.by_band["off-peak"] == pytest.approx(1.0), "only the valid hour's energy"
    assert dryer.partial_bands == frozenset({"off-peak"})

    # "Shed outlet" reported nothing at all, anywhere in the window -- already
    # a dash (``by_band`` empty, ``cost`` None downstream). Flagging it
    # partial too would hatch a row with nothing behind it to hatch.
    outlet = by_name["Shed outlet"]
    assert outlet.by_band == {}
    assert outlet.partial_bands == frozenset()
    store.close()


def test_band_kwh_marks_a_band_partial_when_an_hour_is_missing_entirely(
    tmp_path: Path,
) -> None:
    """The absent-row half of the case above. A poller outage or an archive
    reply that omitted an hour leaves no row at all -- not even the NULL-watts
    one rebuild_circuit_hourly writes for a circuit that was listed and never
    answered -- so neither branch that marks a band partial ever runs, and a
    circuit recorded either side of the gap summed as though nothing were
    missing."""
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=10), watts=1000)
    # Hour 11 never rolled up at all -- no row, not even a NULL one -- and
    # hour 12 resumes reporting.
    _fill_hour(repo, store, 100000, "1", day + timedelta(hours=12), watts=1000)
    intervals = [(day, day + timedelta(days=1), "off-peak")]

    result = repo.band_kwh(day, day + timedelta(days=1), intervals, tier="hourly")

    energy = {c.name: c for c in result}["Dryer"]
    assert energy.by_band["off-peak"] == pytest.approx(2.0), "only the two recorded hours' energy"
    assert energy.partial_bands == frozenset({"off-peak"})
    store.close()


def test_band_kwh_does_not_treat_a_finished_historical_hour_as_a_live_prefix(
    tmp_path: Path,
) -> None:
    """Finding 3. A query ending mid-hour is not always the current, still-
    forming moment -- a finished month whose UTC offset is not a whole number
    of hours ends mid-hour too, and a collection gap can leave that
    already-elapsed hour thin on its own. ``covered_seconds`` says only how
    much of the hour was sampled, never where, so treating it as a prefix
    here would book all thirty covered minutes before the 20-minute boundary
    instead of spreading them the way an ordinary thin hour already is.
    """
    repo, store = _repo(tmp_path)
    day = datetime(2026, 7, 1, tzinfo=UTC)
    repo.sync_circuits([Circuit(100000, "1", "Dryer", 1.0, "circuit")], day)
    hour = day + timedelta(hours=10)
    # Thirty one-minute-apart readings from the top of the hour -- the real
    # rollup's own covered_seconds for a thin hour -- but "now" is ten days
    # past it, so nothing about this gap is still forming.
    _fill_hour(repo, store, 100000, "1", hour, watts=1000, minutes=30)
    window_end = hour + timedelta(minutes=20)
    intervals = [(day, window_end, "off-peak")]

    result = repo.band_kwh(day, window_end, intervals, tier="hourly", now=hour + timedelta(days=10))

    energy = {c.name: c for c in result}["Dryer"]
    # The row holds 0.5 kWh over its thirty covered minutes. Spread evenly
    # across the hour and clipped to the twenty minutes the window reaches,
    # that is 0.5 kWh * (20/60) = 1/6 kWh -- not the 1/3 kWh span=covered
    # gives by assuming the thirty covered minutes sat inside those first
    # twenty.
    assert energy.by_band["off-peak"] == pytest.approx(1 / 6)
    store.close()


def _oracle_band_kwh(
    rows: list[tuple[int, int, int]],
    bands: list[tuple[int, int, str]],
    first: int,
    last: int,
    now_ts: int,
) -> dict[str, float]:
    """An independent reference for ``band_kwh``'s span/overlap arithmetic.

    Built from the rule this project has already settled on -- an hour's
    energy is spread evenly across the hour it was measured in, unless the
    hour genuinely has not finished happening yet, in which case the only
    seconds it could hold are the ones already recorded and they are a real
    prefix rather than a guess -- and not from ``band_kwh``'s own code. A
    formula copied from the function under test would reproduce whatever bug
    that function has; this is a second, independently written implementation
    of the same specification, so a disagreement between the two says one of
    them has drifted from it.

    ``rows`` are ``(when, watts, covered_seconds)`` for one circuit's hourly
    buckets, ``when`` aligned to the hour. ``bands`` are ``(start, end,
    name)`` in epoch seconds, expected to partition ``[first, last)`` exactly
    -- the property below relies on that so summing every band recovers the
    whole window's energy with nothing left over and nothing double-counted.
    """
    hour_seconds = 3600
    out: dict[str, float] = {}
    for when, watts, covered in rows:
        hour_end = when + hour_seconds
        live = hour_end > last and covered < hour_seconds and hour_end > now_ts
        span = covered if live else hour_seconds
        if span <= 0 or covered <= 0:
            continue
        energy = float(watts) * covered
        for band_start, band_end, name in bands:
            overlap = min(when + span, band_end, last) - max(when, band_start, first)
            if overlap <= 0:
                continue
            out[name] = out.get(name, 0.0) + energy * (overlap / span) / 3_600_000
    return out


def _random_band_kwh_case(
    rng: random.Random,
) -> tuple[list[tuple[int, int, int]], list[tuple[int, int, str]], int, int, int]:
    """One random scenario for the energy-conservation property below.

    Aimed at the axes that have produced a defect before: a window that
    starts inside an hour, ends inside an hour, or both inside one hour
    shorter than 3,600 seconds; a run of one to four stored hours, some fully
    covered and some only partly; and a band boundary falling inside an hour,
    exactly on one, or outside the window altogether. ``now_ts`` decides
    whether the final hour -- the only one ``band_kwh`` may ever treat as a
    still-forming prefix -- genuinely has not finished yet or is a fully
    elapsed hour that merely happens to be thin, which is the distinction
    finding 3 was about.

    Returns ``(rows, bands, first, last, now_ts)`` rather than a dataclass:
    nothing downstream needs to name the pieces, and a tuple keeps the caller
    and this generator agreeing on shape by construction.
    """
    hour_seconds = 3600
    h0 = hour_seconds * rng.randint(472_000, 500_000)  # an aligned hour, varied across runs
    num_hours = rng.randint(1, 4)
    first_offset = rng.randrange(0, hour_seconds)
    last_offset = rng.randrange(first_offset + 1, num_hours * hour_seconds + 1)
    first = h0 + first_offset
    last = h0 + last_offset

    rows: list[tuple[int, int, int]] = []
    for i in range(num_hours):
        when = h0 + i * hour_seconds
        watts = rng.randint(1, 4000)
        covered = hour_seconds if rng.random() < 0.5 else rng.randint(1, hour_seconds - 1)
        rows.append((when, watts, covered))

    boundary_when = h0 + ((last_offset - 1) // hour_seconds) * hour_seconds
    boundary_hour_end = boundary_when + hour_seconds
    live = rng.random() < 0.5
    now_ts = last if live else boundary_hour_end + rng.randint(0, 10_000_000)

    candidates: set[int] = set()
    if last - first > 1:
        candidates |= {rng.randrange(first + 1, last) for _ in range(6)}
    hour_grid = (h0 + k * hour_seconds for k in range(1, num_hours))
    candidates |= {edge for edge in hour_grid if first < edge < last}
    pool = sorted(candidates)
    cuts = sorted(rng.sample(pool, min(len(pool), rng.randint(0, 2))))
    edges = [first, *cuts, last]
    bands = [(edges[i], edges[i + 1], f"band{i}") for i in range(len(edges) - 1)]
    return rows, bands, first, last, now_ts


def test_band_kwh_conserves_energy_across_bands(tmp_path: Path) -> None:
    """The invariant, once and for all, rather than a fourth review of the
    same span/overlap arithmetic: for any circuit, any run of hourly rows and
    any request window, the energy ``band_kwh`` attributes across bands must
    equal what the independent oracle above -- built from the specification,
    not from this function's own formula -- says the window actually holds.

    Cases are generated rather than hand-picked, from a fixed seed printed in
    every failure message so one can be reproduced exactly rather than chased
    through a re-run that draws different numbers.

    Rows are inserted directly into ``circuit_hourly`` rather than replayed
    through ``append_readings``/``rebuild_circuit_hourly``: a few hundred
    cases spanning up to four hours apiece would be tens of thousands of
    per-minute inserts through the real rollup, and what is under test here
    is ``band_kwh``'s own arithmetic over an already-stored row -- the
    rollup's own arithmetic already has its own tests elsewhere in this file.
    """
    seed = 20260817
    rng = random.Random(seed)
    repo, store = _repo(tmp_path)
    try:
        for case_index in range(400):
            rows, bands, first, last, now_ts = _random_band_kwh_case(rng)
            name = f"circuit{case_index}"
            ids = repo.sync_circuits([Circuit(100000, str(case_index), name, 1.0, "circuit")], NOW)
            circuit_id = ids[(100000, str(case_index))]
            conn = store._conn
            with conn:
                conn.executemany(
                    "INSERT INTO circuit_hourly"
                    " (timestamp, circuit_id, watts, sample_count, covered_seconds)"
                    " VALUES (?, ?, ?, 1, ?)",
                    [(when, circuit_id, watts, covered) for when, watts, covered in rows],
                )
            intervals = [
                (
                    datetime.fromtimestamp(band_start, UTC),
                    datetime.fromtimestamp(band_end, UTC),
                    band_name,
                )
                for band_start, band_end, band_name in bands
            ]
            actual = repo.band_kwh(
                datetime.fromtimestamp(first, UTC),
                datetime.fromtimestamp(last, UTC),
                intervals,
                tier="hourly",
                circuit_ids=[circuit_id],
                now=datetime.fromtimestamp(now_ts, UTC),
            )
            energy = next(c for c in actual if c.name == name)
            expected = _oracle_band_kwh(rows, bands, first, last, now_ts)
            for band_name in {*expected, *energy.by_band}:
                exp = expected.get(band_name, 0.0)
                got = energy.by_band.get(band_name, 0.0)
                assert got == pytest.approx(exp, abs=1e-6), (
                    f"seed={seed} case={case_index} band={band_name!r} rows={rows} "
                    f"bands={bands} first={first} last={last} now_ts={now_ts} "
                    f"expected={exp} got={got}"
                )
    finally:
        store.close()


# --- the charger audit ----------------------------------------------------


def test_a_change_is_recorded_with_what_it_moved_and_why(tmp_path: Path) -> None:
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=32, to_a=16, reason="peak band opened", applied=True, source=MODULE, now=NOW
    )
    rows = repo.recent_changes()
    assert len(rows) == 1
    assert (rows[0].from_a, rows[0].to_a, rows[0].applied) == (32, 16, True)
    assert rows[0].reason == "peak band opened"
    store.close()


def test_a_refused_change_is_recorded_too(tmp_path: Path) -> None:
    # A log of successes only makes a module that never acted look exactly like
    # one that was never asked. Somebody debugging an unexpected rate needs to
    # see the decisions that went the other way.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001,
        from_a=32,
        to_a=16,
        reason="advisory: proposed, not applied",
        applied=False,
        source=MODULE,
        now=NOW,
    )
    assert repo.recent_changes()[0].applied is False
    store.close()


def test_the_last_rate_this_service_set_is_what_restore_compares_against(tmp_path: Path) -> None:
    # Restore only ever undoes its own work, so it has to know which rate was
    # its own. A refused change never reached the charger and must not count.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=None, to_a=16, reason="set", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=16,
        to_a=6,
        reason="would have",
        applied=False,
        source=MODULE,
        now=NOW + timedelta(minutes=1),
    )
    assert repo.last_applied_rate(900001) == 16
    store.close()


def test_a_charger_this_service_never_touched_has_no_last_rate(tmp_path: Path) -> None:
    repo, store = _audit(tmp_path)
    assert repo.last_applied_rate(900001) is None
    store.close()


def test_a_rate_the_owner_set_is_not_a_rate_this_service_set(tmp_path: Path) -> None:
    # The defect this column exists for. The owner moving the slider is audited
    # as applied — it did reach the charger — so a query asking only "what was
    # last applied" answered with the owner's own number, restore concluded the
    # rate was its own work, and undid it inside the override window.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=7, to_a=6, reason="set by hand", applied=True, source=OWNER, now=NOW
    )
    assert repo.last_applied_rate(900001) is None
    store.close()


def test_the_owners_change_is_still_the_newest_thing_that_happened(tmp_path: Path) -> None:
    # Filtering it out of last_applied_rate must not hide it from the history:
    # "what has this service done to my car" is answered by every line, and the
    # owner's own is often the one that explains the rest.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=7, to_a=6, reason="set by hand", applied=True, source=OWNER, now=NOW
    )
    newest = repo.last_change(900001)
    assert newest is not None
    assert (newest.reason, newest.source) == ("set by hand", OWNER)
    assert repo.recent_changes()[0].source == OWNER
    store.close()


def test_the_owner_choosing_a_rate_this_service_once_set_is_still_theirs(tmp_path: Path) -> None:
    # Filtering the owner's rows out of the query is not enough on its own. The
    # module set 6 A months ago; the owner has since chosen 6 A deliberately. A
    # query that skips their row finds the module's, sees the charger sitting at
    # exactly that rate, and concludes it may put it back — undoing a choice
    # somebody made on purpose because it happened to agree with an old one.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=32, to_a=6, reason="throttled", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=6,
        to_a=6,
        reason="set by hand",
        applied=True,
        source=OWNER,
        now=NOW + timedelta(hours=1),
    )
    assert repo.last_applied_rate(900001) is None
    store.close()


def test_stopping_the_charger_by_hand_does_not_retire_the_restore(tmp_path: Path) -> None:
    # Stopping and starting is audited, because "why is the car not charged" has
    # to have an answer — but it decides nothing about the rate, and it carries
    # no rate for that reason. Reading it as the owner claiming the rate would
    # retire restore-on-startup the first time anybody pressed stop, which is
    # the behaviour the whole control stage exists for.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=32, to_a=6, reason="throttled", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=6,
        to_a=None,
        reason="stopped charging",
        applied=True,
        source=OWNER,
        now=NOW + timedelta(minutes=1),
    )
    assert repo.last_applied_rate(900001) == 6
    store.close()


def test_an_owners_write_that_could_not_be_confirmed_still_claims_the_rate(
    tmp_path: Path,
) -> None:
    # A write is audited as applied only when the charger reads back at the new
    # rate, so a request Emporia accepted and then went quiet on records as not
    # applied while having changed the rate anyway. This module's own proposals
    # are right to be discounted that way; the owner's are not — they reached
    # for the charger, and a rate that may be theirs is not one this module can
    # show is its own.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=32, to_a=6, reason="throttled", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=6,
        to_a=20,
        reason="failed: no route",
        applied=False,
        source=OWNER,
        now=NOW + timedelta(minutes=1),
    )
    assert repo.last_applied_rate(900001) is None
    store.close()


def test_an_unapplied_proposal_of_this_modules_own_still_claims_nothing(tmp_path: Path) -> None:
    # The other side of the same asymmetry. A proposal this module never sent
    # changed nothing, so the rate it last really did set is still its own.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=32, to_a=6, reason="throttled", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=6,
        to_a=32,
        reason="advisory: proposed, not applied",
        applied=False,
        source=MODULE,
        now=NOW + timedelta(minutes=1),
    )
    assert repo.last_applied_rate(900001) == 6
    store.close()


def test_a_change_written_before_the_source_was_recorded_is_never_claimed(tmp_path: Path) -> None:
    # An audit written by an earlier build says who moved the rate no more than
    # it says why. Unknown provenance is not a showing that the rate was ours,
    # and restore puts back only what it can show is its own — so the old rows
    # are left alone rather than assumed.
    repo, store = _audit(tmp_path)
    with store._conn:
        store._conn.execute(
            "INSERT INTO charger_change (timestamp, device_gid, from_a, to_a, reason, applied)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (int(NOW.timestamp()), 900001, 32, 6, "restored on startup", 1),
        )
    assert repo.last_applied_rate(900001) is None
    assert repo.recent_changes()[0].source is None
    store.close()


def test_the_newest_changes_come_first(tmp_path: Path) -> None:
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=None, to_a=10, reason="first", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=10,
        to_a=20,
        reason="second",
        applied=True,
        source=MODULE,
        now=NOW + timedelta(minutes=5),
    )
    assert [row.reason for row in repo.recent_changes()] == ["second", "first"]
    store.close()


def test_two_decisions_in_the_same_second_are_both_kept(tmp_path: Path) -> None:
    # Stopping and starting a charger takes about a second, and an audit that
    # loses one of the two is worse than none: it shows a charger that was
    # stopped and never started, or started and never stopped.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=16, to_a=16, reason="stopped charging", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001, from_a=16, to_a=16, reason="started charging", applied=True, source=MODULE, now=NOW
    )
    assert [row.reason for row in repo.recent_changes()] == [
        "started charging",
        "stopped charging",
    ]
    store.close()


def test_the_newest_decision_is_readable_whether_or_not_it_was_applied(tmp_path: Path) -> None:
    # A different question from last_applied_rate, and asked for a different
    # reason: that one wants the last rate this service put on the charger,
    # this one wants the last thing it decided. A caller checking whether it is
    # about to repeat itself has to see the refusals too, because a proposal
    # that repeats is a proposal that was never applied.
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=None, to_a=16, reason="set", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900001,
        from_a=16,
        to_a=32,
        reason="proposed",
        applied=False,
        source=MODULE,
        now=NOW + timedelta(minutes=1),
    )
    newest = repo.last_change(900001)
    assert newest is not None
    assert (newest.to_a, newest.reason, newest.applied) == (32, "proposed", False)
    store.close()


def test_the_newest_decision_is_the_one_for_that_charger(tmp_path: Path) -> None:
    repo, store = _audit(tmp_path)
    repo.record_change(
        900001, from_a=None, to_a=16, reason="mine", applied=True, source=MODULE, now=NOW
    )
    repo.record_change(
        900002,
        from_a=None,
        to_a=24,
        reason="theirs",
        applied=True,
        source=MODULE,
        now=NOW + timedelta(minutes=1),
    )
    newest = repo.last_change(900001)
    assert newest is not None and newest.reason == "mine"
    assert repo.last_change(900003) is None
    store.close()


def test_a_decision_matches_an_earlier_one_only_when_every_part_of_it_does() -> None:
    # Pure, and deliberately so: whether two decisions are the same decision is
    # a judgement about their content, not about the table they came out of.
    earlier = ChargerChange(
        timestamp=1,
        device_gid=900001,
        from_a=6,
        to_a=32,
        reason="restored to 32 A on startup: advisory: proposed, not applied",
        applied=False,
        source=MODULE,
    )
    assert earlier.same_decision(
        from_a=6, to_a=32, reason=earlier.reason, applied=False, source=MODULE
    )
    # The timestamp is the one field that must not count. Two identical
    # proposals are one proposal made twice, and they can never share a second.
    assert ChargerChange(
        timestamp=999,
        device_gid=900001,
        from_a=6,
        to_a=32,
        reason=earlier.reason,
        applied=False,
        source=MODULE,
    ).same_decision(from_a=6, to_a=32, reason=earlier.reason, applied=False, source=MODULE)

    assert not earlier.same_decision(
        from_a=6, to_a=24, reason=earlier.reason, applied=False, source=MODULE
    )
    assert not earlier.same_decision(
        from_a=10, to_a=32, reason=earlier.reason, applied=False, source=MODULE
    )
    assert not earlier.same_decision(
        from_a=6, to_a=32, reason="set by hand", applied=False, source=MODULE
    )
    # Applied is the sharpest of them. A proposal and the same figure actually
    # reaching the charger are not the same event, and treating them as one
    # would hide the write.
    assert not earlier.same_decision(
        from_a=6, to_a=32, reason=earlier.reason, applied=True, source=MODULE
    )
    # And who decided is part of the decision. The same numbers chosen by the
    # owner and chosen by this module are two different things happening.
    assert not earlier.same_decision(
        from_a=6, to_a=32, reason=earlier.reason, applied=False, source=OWNER
    )

    # An absent rate and a rate are different decisions in both directions. A
    # charger that never said what it was at is not one sitting at 6 A, and a
    # comparison that let those match would suppress a genuinely new proposal.
    unknown = ChargerChange(
        timestamp=1,
        device_gid=900001,
        from_a=None,
        to_a=None,
        reason="r",
        applied=False,
        source=MODULE,
    )
    assert unknown.same_decision(from_a=None, to_a=None, reason="r", applied=False, source=MODULE)
    assert not unknown.same_decision(from_a=6, to_a=None, reason="r", applied=False, source=MODULE)
    assert not unknown.same_decision(from_a=None, to_a=32, reason="r", applied=False, source=MODULE)
    assert not earlier.same_decision(
        from_a=None, to_a=32, reason=earlier.reason, applied=False, source=MODULE
    )


def test_a_circuits_parent_survives_the_round_trip(tmp_path: Path) -> None:
    """What contains a circuit is stored, so a total can leave it out.

    Emporia states containment on the device and we only learn it at sync time;
    a total computed later reads the stored row. Losing it here is #219 — the
    subpanel's branches, the charger and the plugs all added on top of the main
    panel's clamps that already measure them.
    """
    repo, store = _repo(tmp_path)
    start = NOW
    repo.sync_circuits(
        [
            Circuit(100000, "5", "Dryer", 1.0, "circuit"),
            Circuit(100002, "4", "Study", 1.0, "circuit", None, 100000, "1,2,3"),
        ],
        start,
    )
    repo.append_readings([Reading(100000, "5", 1000), Reading(100002, "4", 500)], start)
    got = repo.history(start, start + timedelta(minutes=5), tier="full")
    parents = {series.name: series.parent_device_gid for series in got.series}
    assert parents == {"Dryer": None, "Study": 100000}
    store.close()
