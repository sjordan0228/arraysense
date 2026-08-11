"""Physics held to published values, so a pvlib-less environment still fails
loudly on a real error rather than passing for want of a referee.

The referee tests in test_solar_vs_pvlib.py are the finer instrument; these are
the ones that catch a transcription inverted end to end — a sun below the
horizon at noon, a southern-hemisphere azimuth pointing the wrong way, a tilted
panel that ignores its tilt.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from arraysense.panels import parse_strings
from arraysense.solar import (
    SYSTEM_DERATE,
    cell_temperature,
    expected_watts,
    poa_irradiance,
    solar_position,
)


def test_the_equinox_sun_passes_overhead_at_the_equator() -> None:
    # The coarsest check that the geometry is not inverted, and one no sign
    # error survives. Asked as the day's maximum rather than at clock noon:
    # the equation of time puts true solar noon up to a quarter of an hour
    # either side of twelve, so an instant reading is a few degrees short of
    # vertical even when the model is right — as the referee confirms, pvlib
    # agrees with us to a fifth of a degree at that instant.
    highest = max(
        solar_position(datetime(2026, 3, 20, hour, minute, tzinfo=UTC), 0.0, 0.0)[0]
        for hour in range(10, 14)
        for minute in (0, 15, 30, 45)
    )
    # Within two degrees of vertical, not exactly vertical: the equinox instant
    # falls later in the day than this sampling, so the declination has not
    # quite reached zero. pvlib puts the same day's maximum at 88.14 against
    # our 88.23, which is the referee agreeing that the sky, not the model, is
    # what falls short of ninety here.
    assert highest == pytest.approx(90.0, abs=2.0)


def test_the_southern_hemisphere_sun_is_in_the_north() -> None:
    # A hemisphere sign error is the classic failure and it is invisible from
    # one site: at Sydney's midday the sun bears roughly north, not south.
    _, azimuth = solar_position(datetime(2026, 6, 21, 2, 0, tzinfo=UTC), -33.87, 151.21)
    assert azimuth < 45.0 or azimuth > 315.0


def test_the_northern_hemisphere_sun_is_in_the_south() -> None:
    # The same check from the other side, so a fix for one hemisphere cannot
    # quietly break the other.
    _, azimuth = solar_position(datetime(2026, 6, 21, 18, 0, tzinfo=UTC), 33.09, -97.20)
    assert 135.0 < azimuth < 225.0


def test_the_sun_is_below_the_horizon_at_local_midnight() -> None:
    elevation, _ = solar_position(datetime(2026, 8, 11, 6, 0, tzinfo=UTC), 33.09, -97.20)
    assert elevation < 0.0


def test_poa_on_a_flat_panel_at_high_sun_is_about_ghi() -> None:
    # A horizontal surface sees the global horizontal irradiance by definition;
    # the transposition must reduce to that identity or every tilt is wrong.
    poa = poa_irradiance(
        900.0, 850.0, 100.0, elevation=80.0, sun_azimuth=180.0, tilt=0.0, azimuth=180.0
    )
    assert poa == pytest.approx(900.0, rel=0.05)


def test_a_panel_facing_away_from_the_sun_sees_only_diffuse_and_ground() -> None:
    # No beam component reaches a surface turned away from the sun, but the sky
    # and the ground still do — a zero here would be as wrong as a full reading.
    poa = poa_irradiance(
        900.0, 850.0, 100.0, elevation=20.0, sun_azimuth=90.0, tilt=60.0, azimuth=270.0
    )
    assert 0.0 < poa < 300.0


def test_a_panel_facing_the_sun_beats_one_facing_away() -> None:
    facing = poa_irradiance(900.0, 850.0, 100.0, 40.0, 180.0, 30.0, 180.0)
    away = poa_irradiance(900.0, 850.0, 100.0, 40.0, 180.0, 30.0, 0.0)
    assert facing > away * 1.5


def test_poa_is_never_negative_at_night() -> None:
    assert (
        poa_irradiance(0.0, 0.0, 0.0, elevation=-10.0, sun_azimuth=0.0, tilt=25.0, azimuth=180.0)
        == 0.0
    )


def test_full_sun_puts_panels_far_above_ambient_and_roofs_hotter_than_racks() -> None:
    # Panels commonly run 25-35 C above ambient in full sun, and a close-roof
    # mount runs hotter than an open rack because the air cannot carry the heat
    # away behind it. Both are the issue's own statements, made checkable.
    rack = cell_temperature(1000.0, 30.0, 1.0, "open_rack")
    roof = cell_temperature(1000.0, 30.0, 1.0, "close_roof")
    assert 55.0 <= rack <= 70.0
    assert roof > rack


def test_wind_cools_the_panel() -> None:
    still = cell_temperature(1000.0, 30.0, 0.5, "open_rack")
    breezy = cell_temperature(1000.0, 30.0, 6.0, "open_rack")
    assert breezy < still - 5.0


def test_cell_temperature_at_night_is_ambient() -> None:
    assert cell_temperature(0.0, 18.0, 2.0, "open_rack") == pytest.approx(18.0, abs=0.5)


def test_expected_watts_derates_for_heat_and_credits_bifacial() -> None:
    (cool,) = parse_strings("A | 1 | 10 | 400 | 25 | 180")
    when = datetime(2026, 8, 11, 18, tzinfo=UTC)
    at_stc = expected_watts(cool, poa=1000.0, cell_c=25.0, when=when)
    assert at_stc == pytest.approx(10 * 400 * SYSTEM_DERATE, rel=0.01)
    hot = expected_watts(cool, poa=1000.0, cell_c=55.0, when=when)
    assert hot < at_stc * 0.92  # -0.35 %/C over 30 C is about a tenth
    (bifacial,) = parse_strings("B | 1 | 10 | 400 | 25 | 180 | bifacial=10")
    assert expected_watts(bifacial, 1000.0, 25.0, when) == pytest.approx(at_stc * 1.10, rel=0.01)


def test_expected_watts_ages_the_array_only_when_it_knows_its_birthday() -> None:
    # Degradation without an install date is an assumption about an unknown
    # age; the grammar keeps them separate and so does the model.
    when = datetime(2026, 8, 11, 18, tzinfo=UTC)
    (undated,) = parse_strings("A | 1 | 10 | 400 | 25 | 180 | degradation=1.0")
    (dated,) = parse_strings("A | 1 | 10 | 400 | 25 | 180 | degradation=1.0 installed=2020-08")
    assert expected_watts(dated, 1000.0, 25.0, when) < expected_watts(undated, 1000.0, 25.0, when)


def test_expected_watts_scales_with_irradiance() -> None:
    (spec,) = parse_strings("A | 1 | 10 | 400 | 25 | 180")
    when = datetime(2026, 8, 11, 18, tzinfo=UTC)
    full = expected_watts(spec, 1000.0, 25.0, when)
    half = expected_watts(spec, 500.0, 25.0, when)
    assert half == pytest.approx(full / 2, rel=0.01)


def test_no_irradiance_is_no_production_not_a_negative_one() -> None:
    (spec,) = parse_strings("A | 1 | 10 | 400 | 25 | 180")
    assert expected_watts(spec, 0.0, 40.0, datetime(2026, 8, 11, 6, tzinfo=UTC)) == 0.0
