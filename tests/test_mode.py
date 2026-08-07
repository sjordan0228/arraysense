"""Tests for naming the system's state: arraysense.mode.

The cases below are the ones the reference installation actually produces, taken
from real readings rather than invented, plus the absences that must not be
allowed to look like a state.
"""

from __future__ import annotations

from arraysense.mode import Battery, Mode, assess, battery_state


def test_bypass_is_not_the_same_as_importing_hard() -> None:
    # The distinction the whole module exists for, and it is invisible in the
    # magnitudes: a house drawing 2.6 kW off the grid looks identical whether
    # the inverter is working or has stepped aside. Only the silent EPS port
    # separates them. Twenty-six percent of the last twenty-two months was this
    # state, and nothing on the dashboard said so.
    bypass = assess(
        {
            "load_power_w": 2596.0,
            "eps_power_w": 0.0,
            "grid_power_w": 2150.0,
            "pv_total_power_w": 624.0,
            "battery_power_w": 0.0,
        }
    )
    assert bypass.mode is Mode.ON_GRID
    assert bypass.battery is Battery.IDLE

    inverting = assess(
        {
            "load_power_w": 2596.0,
            "eps_power_w": 2596.0,
            "grid_power_w": 2150.0,
            "pv_total_power_w": 0.0,
            "battery_power_w": 0.0,
        }
    )
    assert inverting.mode is Mode.IMPORTING


def test_the_array_covering_the_house_and_charging_the_bank() -> None:
    # A real reading: 3856 W of sun, 1822 W of house, 1957 W into the bank.
    got = assess(
        {
            "load_power_w": 1822.0,
            "eps_power_w": 1866.0,
            "grid_power_w": 0.0,
            "pv_total_power_w": 3856.0,
            "battery_power_w": 1957.0,
        }
    )
    assert got.mode is Mode.SOLAR_AND_BATTERY
    assert got.battery is Battery.CHARGING


def test_the_bank_carrying_the_house_after_dark() -> None:
    # 02:00 local, no sun, the bank supplying 7.4 kW. This is the state that
    # keeps the on-peak bill at zero.
    got = assess(
        {
            "load_power_w": 7121.0,
            "eps_power_w": 7121.0,
            "grid_power_w": 0.0,
            "pv_total_power_w": 0.0,
            "battery_power_w": -7435.0,
        }
    )
    assert got.mode is Mode.BATTERY
    assert got.battery is Battery.DISCHARGING


def test_sun_and_bank_sharing_the_load_at_dusk() -> None:
    got = assess(
        {
            "load_power_w": 6743.0,
            "eps_power_w": 6743.0,
            "grid_power_w": 0.0,
            "pv_total_power_w": 4413.0,
            "battery_power_w": -2544.0,
        }
    )
    assert got.mode is Mode.SOLAR_AND_BATTERY
    assert got.battery is Battery.DISCHARGING


def test_a_failed_poll_names_no_state_at_all() -> None:
    # A poll that did not complete leaves a row with an error and no readings.
    # Naming a mode from that asserts something nobody measured, which is the
    # same fault as rendering a missing reading as zero.
    got = assess({})
    assert got.mode is Mode.UNKNOWN
    assert got.battery is Battery.UNKNOWN
    assert not got.known


def test_a_trickle_is_idle_rather_than_charging() -> None:
    # The bank and the inverter draw tens of watts simply being switched on. A
    # label that changes at one watt spends the day flickering between
    # "charging" and "idle" while nothing happens.
    assert battery_state({"battery_power_w": 20.0}) is Battery.IDLE
    assert battery_state({"battery_power_w": -20.0}) is Battery.IDLE
    assert battery_state({"battery_power_w": 200.0}) is Battery.CHARGING
    assert battery_state({"battery_power_w": -200.0}) is Battery.DISCHARGING


def test_an_absent_battery_reading_is_unknown_not_idle() -> None:
    # Zero flow and no reading are different claims. The bank being quiet is a
    # measurement; the BMS having gone silent is not.
    assert battery_state({}) is Battery.UNKNOWN
    assert battery_state({"battery_power_w": None}) is Battery.UNKNOWN
    assert battery_state({"battery_power_w": 0.0}) is Battery.IDLE


def test_a_boolean_is_not_a_power_reading() -> None:
    # Booleans are integers in Python, so a flag landing in a numeric column
    # would otherwise read as one watt.
    assert battery_state({"battery_power_w": True}) is Battery.UNKNOWN


def test_the_reason_names_the_readings_it_turned_on() -> None:
    # A mode is an interpretation, and one a reader cannot check is one they
    # have to take on trust.
    got = assess(
        {
            "load_power_w": 2596.0,
            "eps_power_w": 0.0,
            "grid_power_w": 2150.0,
            "pv_total_power_w": 624.0,
            "battery_power_w": 0.0,
        }
    )
    assert "2596" in got.why and "0 W" in got.why
