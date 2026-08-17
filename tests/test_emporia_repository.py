"""test_emporia_repository.py — storing circuits and their readings.

The two properties worth guarding are identity and absence. A circuit is
``(device_gid, channel_num)``, so renaming it in Emporia's app moves a label
rather than starting a new circuit and stranding a year of readings. And an
absent reading stays absent all the way to the page — an outlet that has been
offline since April must not be drawn as one using no power.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from arraysense.modules.emporia.parse import Circuit, Reading
from arraysense.modules.emporia.repository import (
    MODULE,
    OWNER,
    ChargerAudit,
    ChargerChange,
    CircuitRepository,
)
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
