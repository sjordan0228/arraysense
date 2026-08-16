"""test_emporia_charger.py — reading the charger, and who else is driving it.

The record shapes here are the reference account's, captured on 16 August 2026.
The shapes are real; every identifier in them is invented, in the same spirit as
the CE12345678 the rest of this repository uses — this is a public repository and
a device id is an account identifier. The breaker PIN is never a real one either,
and is written so that nobody could mistake it for one.

The conflict rules are the point of the file. Emporia ships its own automation
for a charger and there are **four** switches for it, not the one the design
anticipated: load management on the charger record, and schedules, peak demand
and excess generation on the load record beside it. Two of them were on when
this was first looked at, while the one the design checked was off — so a
conflict check written to that design would have reported all clear while two
other controllers were driving the same charger.
"""

from __future__ import annotations

from arraysense.modules.emporia.parse import (
    DeviceConnection,
    charger_from_status,
    connections_from_status,
)

STATUS: dict[str, list[dict[str, object]]] = {
    "evChargers": [
        {
            "deviceGid": 900001,
            "loadGid": 900002,
            "message": "Ready",
            "status": "Standby",
            "icon": "CarNotConnected",
            "iconLabel": "Ready",
            "chargerOn": True,
            "chargingRate": 6,
            "maxChargingRate": 48,
            "loadManagementEnabled": False,
            "faultText": None,
        }
    ],
    "loads": [
        {
            "loadGid": 900002,
            "schedulesEnabled": True,
            "peakDemandEnabled": True,
            "excessGenerationEnabled": False,
            "energyManagementOverridden": False,
        }
    ],
    "devicesConnected": [{"deviceGid": 900001, "connected": True, "offlineSince": None}],
}


def test_the_charger_is_read_with_its_rate_and_its_limit() -> None:
    got = charger_from_status(STATUS)
    assert got is not None
    assert got.device_gid == 900001
    assert got.rate_a == 6
    assert got.max_rate_a == 48
    assert got.on is True
    assert got.status == "Standby"


def test_every_emporia_controller_that_is_on_is_named() -> None:
    # Two on, and neither is the one the design expected to find.
    got = charger_from_status(STATUS)
    assert got is not None
    assert got.conflicts == ("schedules", "peak demand")


def test_a_charger_with_nobody_else_driving_it_reports_no_conflict() -> None:
    quiet = {
        "evChargers": [dict(STATUS["evChargers"][0])],
        "loads": [
            {
                "loadGid": 900002,
                "schedulesEnabled": False,
                "peakDemandEnabled": False,
                "excessGenerationEnabled": False,
            }
        ],
    }
    got = charger_from_status(quiet)
    assert got is not None
    assert got.conflicts == ()


def test_emporias_own_solar_charging_is_the_one_that_matters_most() -> None:
    # It is the same job this module exists to do. Two controllers both moving
    # the rate at the sun would fight every time a cloud passed.
    payload = {
        "evChargers": [dict(STATUS["evChargers"][0])],
        "loads": [{"loadGid": 900002, "excessGenerationEnabled": True}],
    }
    got = charger_from_status(payload)
    assert got is not None
    assert "excess generation" in got.conflicts


def test_load_management_on_the_charger_record_is_a_conflict_too() -> None:
    charger = dict(STATUS["evChargers"][0])
    charger["loadManagementEnabled"] = True
    got = charger_from_status({"evChargers": [charger], "loads": []})
    assert got is not None
    assert "load management" in got.conflicts


def test_an_offline_charger_says_so_rather_than_reading_as_idle() -> None:
    payload = {
        "evChargers": [dict(STATUS["evChargers"][0])],
        "loads": [],
        "devicesConnected": [
            {"deviceGid": 900001, "connected": False, "offlineSince": "since Apr 2, 2026, 5:06 PM"}
        ],
    }
    got = charger_from_status(payload)
    assert got is not None
    assert got.connected is False
    assert got.offline_since == "since Apr 2, 2026, 5:06 PM"


def test_an_account_with_no_charger_reads_as_absent_rather_than_failing() -> None:
    assert charger_from_status({"evChargers": []}) is None
    assert charger_from_status({}) is None
    assert charger_from_status("nope") is None


def test_the_breaker_pin_is_never_carried_into_the_parsed_state() -> None:
    # It is a secret that must never be logged, echoed by a route, or written to
    # a fixture. The surest way to keep it out of all three is for the object
    # everything else passes around not to have it.
    charger = dict(STATUS["evChargers"][0])
    charger["breakerPIN"] = "NOT-A-REAL-PIN"
    got = charger_from_status({"evChargers": [charger], "loads": []})
    assert got is not None
    assert "NOT-A-REAL-PIN" not in repr(got)
    assert not hasattr(got, "breaker_pin")


# --- is a car actually plugged in? ----------------------------------------
#
# Established by plugging one in and watching, on 16 August 2026. The obvious
# field does not answer it: `status` reads "Standby" both when nothing is
# connected and when a connected car is paused. `chargerOn` does not either —
# it was true with no car attached at all, because it is the switch rather than
# a statement about delivery. The icon is the one field that separates them.


def _charger(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "deviceGid": 900001,
        "chargerOn": True,
        "chargingRate": 6,
        "maxChargingRate": 48,
        "status": "Standby",
        "message": "Ready",
        "icon": "CarNotConnected",
    }
    record.update(overrides)
    return record


def test_no_car_attached_reads_as_unplugged() -> None:
    got = charger_from_status({"evChargers": [_charger()]})
    assert got is not None
    assert got.plugged_in is False


def test_a_charging_car_reads_as_plugged_in() -> None:
    got = charger_from_status(
        {"evChargers": [_charger(icon="CarConnected", status="Charging", message="Charging")]}
    )
    assert got is not None
    assert got.plugged_in is True


def test_a_paused_car_is_still_plugged_in() -> None:
    # The case that makes `status` useless here: Standby, switched off, and a
    # car very much attached. Reported as unplugged it would tell somebody the
    # cable was out when it was in their hand.
    got = charger_from_status(
        {
            "evChargers": [
                _charger(
                    icon="CarConnected",
                    status="Standby",
                    message="Connected to EV",
                    chargerOn=False,
                )
            ]
        }
    )
    assert got is not None
    assert got.plugged_in is True
    assert got.on is False


def test_an_icon_nobody_has_seen_leaves_the_question_open() -> None:
    # Emporia can add a state, and guessing one way or the other would put a
    # confident wrong answer on the page. Unknown is a fact; a guess is not.
    got = charger_from_status({"evChargers": [_charger(icon="SomethingNew")]})
    assert got is not None
    assert got.plugged_in is None
    missing = charger_from_status({"evChargers": [_charger(icon=None)]})
    assert missing is not None
    assert missing.plugged_in is None


# --- the shapes a reply can arrive in --------------------------------------


def test_a_first_entry_that_is_not_a_record_reads_as_no_charger() -> None:
    got = charger_from_status({"evChargers": ["not-a-dict", {"deviceGid": 900001}]})
    assert got is None


def test_another_loads_settings_are_not_read_as_this_chargers() -> None:
    # The load list holds every load on the account. Matching on the wrong one
    # would put somebody else's schedule in this charger's conflict warning.
    got = charger_from_status(
        {
            "evChargers": [{"deviceGid": 900001, "loadGid": 900002}],
            "loads": [{"loadGid": 900003, "schedulesEnabled": True}],
        }
    )
    assert got is not None
    assert got.conflicts == ()


def test_a_charger_that_reported_no_rate_has_none_rather_than_zero() -> None:
    got = charger_from_status(
        {"evChargers": [{"deviceGid": 900001, "status": "Standby", "icon": "CarNotConnected"}]}
    )
    assert got is not None
    assert got.rate_a is None
    assert got.max_rate_a is None


def test_a_switch_that_is_not_a_boolean_is_unknown_rather_than_off() -> None:
    got = charger_from_status(
        {"evChargers": [{"deviceGid": 900001, "chargerOn": "yes", "icon": "CarConnected"}]}
    )
    assert got is not None
    assert got.on is None


def test_the_first_charger_is_the_one_read() -> None:
    # A limitation worth stating rather than leaving to be discovered: an
    # account with two chargers gets the first, and the second is invisible.
    got = charger_from_status(
        {
            "evChargers": [
                {"deviceGid": 900001, "chargingRate": 6},
                {"deviceGid": 900005, "chargingRate": 16},
            ]
        }
    )
    assert got is not None
    assert got.device_gid == 900001


def test_another_devices_connection_is_not_read_as_this_ones() -> None:
    got = charger_from_status(
        {
            "evChargers": [{"deviceGid": 900001, "icon": "CarConnected"}],
            "devicesConnected": [{"deviceGid": 900002, "connected": True}],
        }
    )
    assert got is not None
    assert got.connected is None, "unknown, not assumed online"


def test_conflicts_from_both_records_are_listed_in_a_stable_order() -> None:
    got = charger_from_status(
        {
            "evChargers": [{"deviceGid": 900001, "loadGid": 900002, "loadManagementEnabled": True}],
            "loads": [{"loadGid": 900002, "schedulesEnabled": True}],
        }
    )
    assert got is not None
    assert got.conflicts == ("load management", "schedules")


# --- which devices are still answering -------------------------------------
#
# The same `devicesConnected` list the charger reads its own state out of, but
# asked about every device rather than one. It is the difference between a
# circuit that drew nothing and a circuit nobody has heard from since April,
# which the page could otherwise only render as the same dash. Two of the
# reference account's outlets have been offline since 2 April and 8 August 2026.


def test_every_device_that_reported_its_connection_is_read() -> None:
    got = connections_from_status(
        {
            "devicesConnected": [
                {"deviceGid": 100000, "connected": True, "offlineSince": None},
                {
                    "deviceGid": 100001,
                    "connected": False,
                    "offlineSince": "since Apr 2, 2026, 5:06 PM",
                },
            ]
        }
    )
    assert got[100000].connected is True
    assert got[100000].offline_since is None
    assert got[100001].connected is False
    assert got[100001].offline_since == "since Apr 2, 2026, 5:06 PM"


def test_a_device_that_did_not_say_is_unknown_rather_than_online() -> None:
    # The same rule as every other absence here. "Nobody said" and "it is up"
    # are different statements, and a page that draws the first as the second
    # is the reason this project exists.
    got = connections_from_status({"devicesConnected": [{"deviceGid": 100000}]})
    assert got[100000].connected is None
    assert got[100000].offline_since is None


def test_a_reply_with_no_connections_reads_as_nothing_known() -> None:
    # A shape change at Emporia's end must cost the circuits page its offline
    # marks, never its readings.
    assert connections_from_status({}) == {}
    assert connections_from_status({"devicesConnected": "nope"}) == {}
    assert connections_from_status("nope") == {}
    assert connections_from_status({"devicesConnected": [None, 7, {"connected": True}]}) == {}


def test_a_connection_that_is_not_a_boolean_is_unknown_rather_than_up() -> None:
    # The same trap as everywhere else in this file: 1 is truthy and is not
    # True, and reading it as True would put "online" on a page on the strength
    # of a field Emporia never set.
    got = connections_from_status(
        {
            "devicesConnected": [
                {"deviceGid": 100000, "connected": 1},
                {"deviceGid": 100001, "connected": "true"},
            ]
        }
    )
    assert got[100000].connected is None
    assert got[100001].connected is None


def test_an_offline_date_that_is_not_text_is_dropped_rather_than_rendered() -> None:
    got = connections_from_status({"devicesConnected": [{"deviceGid": 100000, "offlineSince": 17}]})
    assert got[100000].offline_since is None


def test_a_device_identifier_that_is_not_a_number_names_no_device() -> None:
    # A bool is an int in Python, so an unguarded check would file a device
    # under gid 1 and hand its state to whatever real device holds that number.
    got = connections_from_status(
        {
            "devicesConnected": [
                {"deviceGid": True, "connected": False},
                {"deviceGid": "100000", "connected": False},
                {"deviceGid": 100000, "connected": True},
            ]
        }
    )
    assert got == {100000: DeviceConnection(connected=True, offline_since=None)}


def test_a_device_listed_twice_is_read_as_its_latest_entry() -> None:
    got = connections_from_status(
        {
            "devicesConnected": [
                {"deviceGid": 100000, "connected": False, "offlineSince": "since Apr 2, 2026"},
                {"deviceGid": 100000, "connected": True},
            ]
        }
    )
    assert got[100000] == DeviceConnection(connected=True, offline_since=None)


def test_the_charger_reads_its_own_connection_from_the_same_list() -> None:
    # One reader for one field. The charger tab and the circuits page disagreeing
    # about whether a device is up would be the same fact computed twice.
    payload = {
        "evChargers": [dict(STATUS["evChargers"][0])],
        "devicesConnected": [
            {"deviceGid": 900001, "connected": False, "offlineSince": "since Aug 8, 2026, 9:12 AM"}
        ],
    }
    charger = charger_from_status(payload)
    connections = connections_from_status(payload)
    assert charger is not None
    assert (charger.connected, charger.offline_since) == (
        connections[900001].connected,
        connections[900001].offline_since,
    )
