"""test_emporia_parse.py — turning Emporia's device list into circuits we store.

Two of these are load-bearing rather than tidy. The ``channelMultiplier`` is 2.0
on a 240 V circuit because one clamp measures one leg, so losing it halves the
dryer and both air conditioners — the very loads this feature exists to name.
And ``"1,2,3"`` is a monitor's own mains total rather than a circuit; counted as
a circuit it doubles every sum drawn from these rows.

The fixture is shaped exactly like a real reply with the account's identifiers
replaced, and carries no ``breakerPIN``.
"""

from __future__ import annotations

import logging

import pytest

from arraysense.modules.emporia.parse import (
    SCALE_MINUTE,
    SCALE_SECOND,
    Circuit,
    circuits_from_devices,
    device_gids,
    readings_from_usage,
)

DEVICES = {
    "devices": [
        {
            "deviceGid": 100000,
            "model": "VUE002",
            "channels": [
                {"channelNum": "1", "name": "Garage plugs", "channelMultiplier": 1.0},
                {"channelNum": "5", "name": "Dryer", "channelMultiplier": 2.0},
                {"channelNum": "7", "name": None, "channelMultiplier": 1.0},
                {"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0},
            ],
            "devices": [
                {
                    "deviceGid": 100001,
                    "model": "VVDN01",
                    "channels": [{"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0}],
                }
            ],
        }
    ]
}


# Shaped like the real reply's branch record, which carries a channelTypeGid
# per channel — 1 for the air conditioners, 19 for septic, absent on a clamp
# nobody has set up. Taken from the reference account on 16 August 2026.
TYPED = {
    "devices": [
        {
            "deviceGid": 100000,
            "model": "WAT001",
            "channels": [
                {
                    "channelNum": "8",
                    "name": "air conditioner main",
                    "channelMultiplier": 2.0,
                    "channelTypeGid": 1,
                },
                {
                    "channelNum": "16",
                    "name": "septic",
                    "channelMultiplier": 1.0,
                    "channelTypeGid": 19,
                },
                {
                    "channelNum": "7",
                    "name": None,
                    "channelMultiplier": 1.0,
                    "channelTypeGid": None,
                },
            ],
        }
    ]
}


def test_named_channels_keep_the_owners_names() -> None:
    got = {c.channel_num: c for c in circuits_from_devices(DEVICES) if c.device_gid == 100000}
    assert got["1"].name == "Garage plugs"
    assert got["5"].name == "Dryer"


def test_a_240_volt_circuit_keeps_its_multiplier() -> None:
    # One clamp measures one leg. Losing the multiplier halves the dryer, the
    # oven and both air conditioners — exactly the loads this feature is for.
    got = {c.channel_num: c for c in circuits_from_devices(DEVICES)}
    assert got["5"].multiplier == 2.0
    assert got["1"].multiplier == 1.0


def test_an_unnamed_channel_gets_a_stable_label_rather_than_being_dropped() -> None:
    # It can still be the thing that spiked, so it must be storable and
    # nameable. The label has to be stable or it becomes a new circuit daily.
    got = {(c.device_gid, c.channel_num): c for c in circuits_from_devices(DEVICES)}
    first = got[(100000, "7")].name
    second = {(c.device_gid, c.channel_num): c for c in circuits_from_devices(DEVICES)}[
        (100000, "7")
    ].name
    assert first == second
    assert "100000" in first and "7" in first


def test_the_whole_device_channel_is_marked_as_a_total_not_a_circuit() -> None:
    # "1,2,3" is the device's own mains reading. Counted as a circuit it would
    # double every total drawn from these rows.
    got = {(c.device_gid, c.channel_num): c for c in circuits_from_devices(DEVICES)}
    assert got[(100000, "1,2,3")].kind == "mains"
    assert got[(100000, "1")].kind == "circuit"


def test_a_nested_device_is_found_and_typed_by_its_model() -> None:
    got = {c.device_gid: c for c in circuits_from_devices(DEVICES)}
    assert 100001 in got, "a charger nested under a monitor must not be missed"
    assert got[100001].kind == "charger"


def test_an_empty_or_broken_payload_yields_nothing_rather_than_raising() -> None:
    assert circuits_from_devices({}) == []
    assert circuits_from_devices({"devices": []}) == []
    assert circuits_from_devices("not a dict") == []


def test_a_circuit_is_hashable_and_compares_by_identity() -> None:
    left = Circuit(100000, "1", "Garage plugs", 1.0, "circuit")
    right = Circuit(100000, "1", "Renamed later", 1.0, "circuit")
    assert left.identity == right.identity, "identity is the gid and channel, never the name"


# The rest are the malformed shapes. This parser runs unattended on a timer
# against a service with no published contract, so every one of these has to
# degrade to "fewer circuits" rather than to a poller that stopped.


def test_a_non_dict_channel_entry_is_skipped() -> None:
    payload = {
        "devices": [
            {
                "deviceGid": 100002,
                "model": "VUE002",
                "channels": [
                    {"channelNum": "1", "name": "Valid", "channelMultiplier": 1.0},
                    "not a dict",
                    ["also", "not", "a", "dict"],
                    None,
                ],
            }
        ]
    }
    circuits = circuits_from_devices(payload)
    assert len(circuits) == 1
    assert circuits[0].channel_num == "1"


def test_a_channel_number_that_is_not_a_string_is_skipped() -> None:
    payload = {
        "devices": [
            {
                "deviceGid": 100003,
                "model": "VUE002",
                "channels": [
                    {"channelNum": 1, "name": "Numeric", "channelMultiplier": 1.0},
                    {"channelNum": True, "name": "Boolean", "channelMultiplier": 1.0},
                    {"channelNum": [1], "name": "List", "channelMultiplier": 1.0},
                    {"channelNum": "valid", "name": "Valid", "channelMultiplier": 1.0},
                ],
            }
        ]
    }
    circuits = circuits_from_devices(payload)
    assert len(circuits) == 1
    assert circuits[0].channel_num == "valid"


def test_a_missing_multiplier_is_the_neutral_one_rather_than_an_absence() -> None:
    # 1.0 is not a fabricated reading. A multiplier is how a measurement is
    # scaled, and a channel that names none is a 120 V circuit measured whole —
    # so the neutral factor is the fact, not a stand-in for a missing one.
    payload = {
        "devices": [
            {
                "deviceGid": 100004,
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "No multiplier"}],
            }
        ]
    }
    circuits = circuits_from_devices(payload)
    assert len(circuits) == 1
    assert circuits[0].multiplier == 1.0


def test_a_multiplier_that_is_not_a_number_falls_back_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Emporia sends a float today. If that ever became "2.5" the fallback would
    # halve a 240 V circuit silently, which is why the parser complains rather
    # than only substituting. The log line is the point as much as the value:
    # without it the dryer reads low forever with nothing to explain it.
    payload = {
        "devices": [
            {
                "deviceGid": 100005,
                "model": "VUE002",
                "channels": [
                    {"channelNum": "1", "name": "String mult", "channelMultiplier": "2.5"}
                ],
            }
        ]
    }
    with caplog.at_level(logging.WARNING, logger="arraysense.modules.emporia.parse"):
        circuits = circuits_from_devices(payload)
    assert len(circuits) == 1
    assert circuits[0].multiplier == 1.0
    assert "unreadable channelMultiplier" in caplog.text
    assert "100005" in caplog.text


def test_a_device_with_no_usable_gid_contributes_no_circuits() -> None:
    payload = {
        "devices": [
            {
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "Orphan channel"}],
            }
        ]
    }
    assert circuits_from_devices(payload) == []


def test_a_gid_that_is_not_an_integer_contributes_no_circuits() -> None:
    payload = {
        "devices": [
            {
                "deviceGid": "100006",
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "Invalid GID"}],
            }
        ]
    }
    assert circuits_from_devices(payload) == []


def test_devices_nested_three_deep_are_all_found() -> None:
    payload = {
        "devices": [
            {
                "deviceGid": 100007,
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "Parent ch 1"}],
                "devices": [
                    {
                        "deviceGid": 100008,
                        "model": "SSO01",
                        "channels": [{"channelNum": "1", "name": "Child ch 1"}],
                        "devices": [
                            {
                                "deviceGid": 100009,
                                "model": "VVDN02",
                                "channels": [{"channelNum": "1", "name": "Grandchild ch 1"}],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    circuits = circuits_from_devices(payload)
    assert {c.device_gid for c in circuits} == {100007, 100008, 100009}


def test_a_name_that_is_only_whitespace_gets_the_stable_label() -> None:
    payload = {
        "devices": [
            {
                "deviceGid": 100010,
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "   ", "channelMultiplier": 1.0}],
            }
        ]
    }
    circuits = circuits_from_devices(payload)
    assert circuits[0].name == "Device 100010 ch 1"


# A device whose mains channel carries no name of its own, which is every
# outlet, charger and monitor on the reference account. The name the owner gave
# it in Emporia's app is in ``locationProperties``, one level up from the
# channel — taken from the real reply on 17 August 2026, where six of the
# thirty-nine circuits were reading as "Device 100002 ch 1,2,3" while Emporia
# held "EVSE", "Dishwasher", "Washer" and three more for them all along.
NAMED_DEVICES = {
    "devices": [
        {
            "deviceGid": 100020,
            "model": "VUE003",
            "locationProperties": {"deviceName": "Subpanel Vue"},
            "channels": [
                {"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0},
                {"channelNum": "1", "name": "Entry", "channelMultiplier": 1.0},
                {"channelNum": "7", "name": None, "channelMultiplier": 1.0},
                {"channelNum": "8", "name": None, "channelMultiplier": 1.0},
            ],
            "devices": [
                {
                    "deviceGid": 100021,
                    "model": "VVDN01",
                    "locationProperties": {"deviceName": "EVSE"},
                    "channels": [{"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0}],
                }
            ],
        }
    ]
}


def test_a_device_names_its_own_mains_channel() -> None:
    # The charger read as "Device 100021 ch 1,2,3" on every page while Emporia
    # had a name for it the whole time.
    got = {(c.device_gid, c.channel_num): c for c in circuits_from_devices(NAMED_DEVICES)}
    assert got[(100021, "1,2,3")].name == "EVSE"
    assert got[(100020, "1,2,3")].name == "Subpanel Vue"


def test_an_unnamed_clamp_does_not_take_the_devices_name() -> None:
    # The half of this that is easy to get wrong. A device's name belongs to the
    # device, so lending it to every unnamed clamp would render four separate
    # circuits as four rows all called "Subpanel Vue" — worse than the stable
    # label, because the stable label at least tells them apart.
    got = {(c.device_gid, c.channel_num): c for c in circuits_from_devices(NAMED_DEVICES)}
    assert got[(100020, "7")].name == "Device 100020 ch 7"
    assert got[(100020, "8")].name == "Device 100020 ch 8"


def test_a_channels_own_name_outranks_the_devices() -> None:
    got = {(c.device_gid, c.channel_num): c for c in circuits_from_devices(NAMED_DEVICES)}
    assert got[(100020, "1")].name == "Entry"


def test_a_device_with_no_name_of_its_own_keeps_the_stable_label() -> None:
    payload = {
        "devices": [
            {
                "deviceGid": 100022,
                "model": "SSO001",
                "locationProperties": {"deviceName": "   "},
                "channels": [{"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0}],
            }
        ]
    }
    assert circuits_from_devices(payload)[0].name == "Device 100022 ch 1,2,3"


def test_an_outlet_on_the_mains_channel_is_an_outlet_not_a_mains_total() -> None:
    # A smart outlet reports itself on the same channel a monitor uses for its
    # mains. Typed as mains it would be excluded from the circuit list, and the
    # owner would lose the one device they can actually switch.
    payload = {
        "devices": [
            {
                "deviceGid": 100011,
                "model": "SSO01",
                "channels": [
                    {"channelNum": "1,2,3", "name": "SSO mains", "channelMultiplier": 1.0}
                ],
            }
        ]
    }
    circuits = circuits_from_devices(payload)
    assert circuits[0].kind == "outlet"


def test_the_same_channel_on_two_monitors_stays_two_circuits() -> None:
    # Two Vues both have a channel 1. Keyed on the channel alone they would
    # collapse into one circuit and one of the two houses' worth of history
    # would be written over the other.
    payload = {
        "devices": [
            {
                "deviceGid": 100012,
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "Device A ch 1"}],
            },
            {
                "deviceGid": 100013,
                "model": "VUE002",
                "channels": [{"channelNum": "1", "name": "Device B ch 1"}],
            },
        ]
    }
    circuits = circuits_from_devices(payload)
    by_gid = {c.device_gid: c for c in circuits}
    assert len(circuits) == 2
    assert by_gid[100012].name == "Device A ch 1"
    assert by_gid[100013].name == "Device B ch 1"


# --- Usage, and the factor of sixty --------------------------------------


def test_usage_becomes_watts_for_the_scale_it_was_asked_for() -> None:
    # Emporia returns kWh accumulated in the bucket, so watts depends on the
    # bucket's length. Getting this wrong is a factor of sixty.
    payload = {
        "deviceListUsages": {
            "scale": "1MIN",
            "devices": [
                {
                    "deviceGid": 100000,
                    "channelUsages": [
                        {
                            "deviceGid": 100000,
                            "channelNum": "5",
                            "name": "Dryer",
                            "usage": 0.05,
                            "nestedDevices": [],
                        }
                    ],
                }
            ],
        }
    }
    minute = readings_from_usage(payload, SCALE_MINUTE)
    assert minute[0].watts == 3000  # 0.05 kWh in a minute = 3 kW

    second = readings_from_usage(payload, SCALE_SECOND)
    assert second[0].watts == 180000  # 0.05 kWh in a second = 180 kW


def test_a_null_usage_stays_absent_and_never_becomes_zero() -> None:
    # The rule this project exists for. An outlet that has been offline since
    # April reports null, and a zero would draw it as "using no power" — which
    # is a claim, not a silence.
    payload = {
        "deviceListUsages": {
            "devices": [
                {
                    "deviceGid": 100000,
                    "channelUsages": [
                        {
                            "deviceGid": 100000,
                            "channelNum": "1",
                            "usage": None,
                            "nestedDevices": [],
                        }
                    ],
                }
            ]
        }
    }
    got = readings_from_usage(payload, SCALE_MINUTE)
    assert len(got) == 1
    assert got[0].watts is None


def test_nested_devices_are_reached() -> None:
    payload = {
        "deviceListUsages": {
            "devices": [
                {
                    "deviceGid": 100000,
                    "channelUsages": [
                        {
                            "deviceGid": 100000,
                            "channelNum": "1,2,3",
                            "usage": 0.1,
                            "nestedDevices": [
                                {
                                    "deviceGid": 100001,
                                    "channelUsages": [
                                        {
                                            "deviceGid": 100001,
                                            "channelNum": "1,2,3",
                                            "usage": 0.02,
                                            "nestedDevices": [],
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
    }
    got = {(r.device_gid, r.channel_num): r for r in readings_from_usage(payload, SCALE_MINUTE)}
    assert (100001, "1,2,3") in got, "a charger nested under the mains must not be missed"
    assert got[(100001, "1,2,3")].watts == 1200


def test_a_broken_usage_payload_yields_nothing() -> None:
    assert readings_from_usage({}, SCALE_MINUTE) == []
    assert readings_from_usage("nope", SCALE_MINUTE) == []


def test_an_unknown_scale_takes_no_readings_rather_than_guessing_one() -> None:
    # A scale nobody has measured has no bucket length, and guessing at one
    # rescales every circuit on the page by whatever the guess was wrong by.
    payload = {
        "deviceListUsages": {
            "devices": [
                {
                    "deviceGid": 100000,
                    "channelUsages": [
                        {"deviceGid": 100000, "channelNum": "1", "usage": 0.05},
                    ],
                }
            ]
        }
    }
    assert readings_from_usage(payload, "1H") == []


def test_a_boolean_usage_is_absent_rather_than_a_kilowatt() -> None:
    # True is an int in Python, so an unguarded number check turns a flag into
    # 60 kW. Absent is the honest reading for a field that is not a number.
    payload = {
        "deviceListUsages": {
            "devices": [
                {
                    "deviceGid": 100000,
                    "channelUsages": [
                        {"deviceGid": 100000, "channelNum": "1", "usage": True},
                    ],
                }
            ]
        }
    }
    got = readings_from_usage(payload, SCALE_MINUTE)
    assert len(got) == 1
    assert got[0].watts is None


def test_every_device_identifier_is_collected_including_nested_ones() -> None:
    # The usage endpoint refuses a request that does not name the devices it is
    # being asked about: "Could not get attribute 'deviceGids' from input",
    # HTTP 400, measured against the real API. So this list is not a
    # convenience — without it the module reads nothing at all.
    got = device_gids(DEVICES)
    assert got == (100000, 100001), "the charger nested under the monitor must be asked for too"


def test_device_identifiers_are_deduplicated_and_keep_their_order() -> None:
    # The same gid can appear as a parent and again inside a nested list. Asked
    # for twice it makes the query longer for nothing.
    payload = {
        "devices": [
            {"deviceGid": 7, "channels": [], "devices": [{"deviceGid": 7, "channels": []}]},
            {"deviceGid": 3, "channels": []},
        ]
    }
    assert device_gids(payload) == (7, 3)


def test_a_broken_device_list_names_no_devices_rather_than_raising() -> None:
    assert device_gids({}) == ()
    assert device_gids("nope") == ()


def test_a_channel_keeps_the_category_the_owner_gave_it() -> None:
    # channelTypeGid is what the owner picked in Emporia's app, and it is the
    # only thing in the reply that says what a circuit *is* rather than what it
    # is called. Kept so the page can mark a row with an icon; dropped, the
    # page could only ever show a name.
    got = {c.channel_num: c for c in circuits_from_devices(TYPED) if c.device_gid == 100000}
    assert got["8"].type_gid == 1
    assert got["16"].type_gid == 19


def test_a_channel_with_no_category_has_none_rather_than_a_stand_in() -> None:
    # Four clamps on the reference account are unnamed and untyped. A zero here
    # would be a category, and category zero is a claim nobody made.
    got = {c.channel_num: c for c in circuits_from_devices(TYPED)}
    assert got["7"].type_gid is None


# Shaped like the reference account's real reply, measured 17 August 2026. The
# nesting Emporia publishes is not the JSON tree: every device is a top-level
# entry and the containment is stated in ``parentDeviceGid`` and
# ``parentChannelNum``. The only true nesting is a monitor's own channel bank,
# which repeats the parent's gid and declares no parent of its own.
PARENTED = {
    "devices": [
        {
            "deviceGid": 100000,
            "model": "VUE002",
            "parentDeviceGid": None,
            "parentChannelNum": None,
            "channels": [{"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0}],
            "devices": [
                {
                    "deviceGid": 100000,
                    "model": "WAT001",
                    "channels": [{"channelNum": "5", "name": "Dryer", "channelMultiplier": 2.0}],
                }
            ],
        },
        {
            "deviceGid": 100002,
            "model": "VUE003",
            "parentDeviceGid": 100000,
            "parentChannelNum": "1,2,3",
            "channels": [{"channelNum": "1,2,3", "name": None, "channelMultiplier": 1.0}],
            "devices": [
                {
                    "deviceGid": 100002,
                    "model": "WAT001",
                    "channels": [{"channelNum": "4", "name": "Study", "channelMultiplier": 1.0}],
                }
            ],
        },
    ]
}


def _by_name(circuits: list[Circuit], name: str) -> Circuit:
    return next(circuit for circuit in circuits if circuit.name == name)


def test_a_device_records_the_parent_emporia_declares_for_it() -> None:
    circuits = circuits_from_devices(PARENTED)
    subpanel_mains = next(
        circuit
        for circuit in circuits
        if circuit.device_gid == 100002 and circuit.channel_num == "1,2,3"
    )
    assert subpanel_mains.parent_device_gid == 100000
    assert subpanel_mains.parent_channel_num == "1,2,3"


def test_a_top_level_device_has_no_parent() -> None:
    circuits = circuits_from_devices(PARENTED)
    assert _by_name(circuits, "Dryer").parent_device_gid is None


def test_a_nested_channel_bank_inherits_its_devices_parent() -> None:
    """The 16 branches of a subpanel Vue are inside whatever that Vue is inside.

    Emporia states the containment once, on the monitor, and its channel bank
    arrives as a nested record repeating the gid and declaring nothing. Reading
    the bank on its own terms makes a subpanel's branches look like the house's
    own, which is the 327 kWh half of #219.
    """
    circuits = circuits_from_devices(PARENTED)
    study = _by_name(circuits, "Study")
    assert study.parent_device_gid == 100000
    assert study.parent_channel_num == "1,2,3"
