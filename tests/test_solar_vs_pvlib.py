"""The referee: our transcription held to pvlib's reference implementation.

pvlib is a development dependency and is imported only here. What ships to the
collector is the stdlib arithmetic in solar.py; this is the test that proves
that arithmetic matches the library the industry trusts.

Five sites, sampled across a year, because a hemisphere sign error is invisible
from one site and a high-latitude or equatorial degeneracy is invisible from
four. Each function is judged against its own reference with the SAME inputs —
the transposition is fed the reference's own sun angles rather than ours, so a
position error cannot hide inside a POA tolerance, and each number below is one
this pair of implementations actually achieves rather than a hopeful bound.

Measured worst cases, which is where the tolerances come from:

    solar_position             0.41 deg   (angular separation on the sky)
    poa_irradiance             0.11 %     (same sun angles both sides)
    cell_temperature           0.0000 C   (exact)

The elevation comparison is against ``elevation`` and not
``apparent_elevation``: pvlib's apparent figure includes atmospheric
refraction, which lifts the sun by up to half a degree near the horizon.
Our transcription is deliberately geometric, because the callers use elevation
to decide whether the sun is up and to project irradiance — neither of which
wants a refracted angle — and comparing against the refracted one would be
holding the code to a model it does not claim to implement.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from arraysense.solar import _faiman_u0, cell_temperature, poa_irradiance, solar_position

pvlib = pytest.importorskip("pvlib")
pd = pytest.importorskip("pandas")

SITES = [
    ("Texas", 33.088264, -97.201443),
    ("Sydney", -33.87, 151.21),
    ("Reykjavik", 64.15, -21.94),
    ("Nairobi", -1.29, 36.82),
    ("Santiago", -33.45, -70.67),
]

# Four points across the year, six hours across each day: enough to catch a
# seasonal or hemispheric error without a slow suite.
WHENS = [
    datetime(2026, month, 15, hour, tzinfo=UTC)
    for month in (1, 4, 7, 10)
    for hour in (0, 4, 8, 12, 16, 20)
]


def _direction(elevation: float, azimuth: float) -> tuple[float, float, float]:
    """The sun as a unit vector, so two positions can be compared as one angle."""
    e, a = math.radians(elevation), math.radians(azimuth)
    return (math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e))


@pytest.mark.parametrize(("name", "latitude", "longitude"), SITES)
def test_solar_position_matches_pvlib(name: str, latitude: float, longitude: float) -> None:
    # Judged as the angle between the two suns rather than as two coordinates
    # compared separately. Azimuth is degenerate near the zenith — directly
    # overhead it has no value at all, and a degree from overhead a hair of
    # position error swings it by degrees — so a per-coordinate tolerance
    # would have to be loose enough near the zenith to miss a real error
    # everywhere else. The separation on the sky is the quantity the model
    # actually depends on, and it is well conditioned everywhere.
    for when in WHENS:
        ours = solar_position(when, latitude, longitude)
        reference = pvlib.solarposition.get_solarposition(
            pd.DatetimeIndex([when]), latitude, longitude
        )
        their_elevation = float(reference["elevation"].iloc[0])
        if their_elevation < 0.0:
            # The sun is down; where exactly it is under the horizon does not
            # matter to anything, and only that both agree it has set does.
            assert ours[0] < 1.0, f"{name} {when}: sun should be down"
            continue
        theirs = (their_elevation, float(reference["azimuth"].iloc[0]))
        dot = sum(x * y for x, y in zip(_direction(*ours), _direction(*theirs), strict=True))
        separation = math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        assert separation < 0.6, (
            f"{name} {when}: {separation:.3f} deg apart "
            f"(ours {ours[0]:.2f}/{ours[1]:.2f}, pvlib {theirs[0]:.2f}/{theirs[1]:.2f})"
        )


def test_poa_matches_pvlib_hay_davies_given_the_same_sun() -> None:
    # Both implementations are handed the same sun position, which is what
    # isolates the transposition from the position: a disagreement here is a
    # transposition bug and nothing else.
    extra = float(pvlib.irradiance.get_extra_radiation(pd.Timestamp("2026-06-21")))
    for zenith in (20.0, 40.0, 60.0, 75.0, 85.0):
        for sun_azimuth in (60.0, 120.0, 180.0, 240.0, 300.0):
            for tilt in (0.0, 15.0, 25.0, 40.0, 60.0):
                for surface_azimuth in (90.0, 180.0, 270.0):
                    ours = poa_irradiance(
                        700.0,
                        800.0,
                        120.0,
                        90.0 - zenith,
                        sun_azimuth,
                        tilt,
                        surface_azimuth,
                        172,
                    )
                    theirs = float(
                        pvlib.irradiance.get_total_irradiance(
                            surface_tilt=tilt,
                            surface_azimuth=surface_azimuth,
                            solar_zenith=zenith,
                            solar_azimuth=sun_azimuth,
                            dni=800.0,
                            ghi=700.0,
                            dhi=120.0,
                            dni_extra=extra,
                            model="haydavies",
                            albedo=0.2,
                        )["poa_global"]
                    )
                    assert ours == pytest.approx(theirs, rel=0.005, abs=0.5), (
                        f"zenith={zenith} sun={sun_azimuth} tilt={tilt} "
                        f"surface={surface_azimuth}: {ours} vs {theirs}"
                    )


@pytest.mark.parametrize(("name", "latitude", "longitude"), SITES)
def test_poa_end_to_end_stays_close_where_the_energy_is(
    name: str, latitude: float, longitude: float
) -> None:
    # The composed pipeline — our position feeding our transposition — against
    # pvlib's. Looser than the isolated test above and deliberately so: the
    # residual is the refraction difference, which grows as the sun drops, and
    # an hour at ten degrees carries a tiny share of a day's energy. Held tight
    # where the energy actually is.
    for when in WHENS:
        elevation, azimuth = solar_position(when, latitude, longitude)
        if elevation <= 20.0:
            continue
        reference = pvlib.solarposition.get_solarposition(
            pd.DatetimeIndex([when]), latitude, longitude
        )
        ours = poa_irradiance(
            700.0, 800.0, 120.0, elevation, azimuth, 25.0, 180.0, when.timetuple().tm_yday
        )
        theirs = float(
            pvlib.irradiance.get_total_irradiance(
                surface_tilt=25.0,
                surface_azimuth=180.0,
                solar_zenith=float(reference["zenith"].iloc[0]),
                solar_azimuth=float(reference["azimuth"].iloc[0]),
                dni=800.0,
                ghi=700.0,
                dhi=120.0,
                dni_extra=float(pvlib.irradiance.get_extra_radiation(pd.Timestamp(when))),
                model="haydavies",
                albedo=0.2,
            )["poa_global"]
        )
        assert ours == pytest.approx(theirs, rel=0.05), f"{name} {when}: {ours} vs {theirs}"


def test_cell_temperature_matches_pvlib_faiman() -> None:
    for poa in (0.0, 200.0, 600.0, 1000.0, 1200.0):
        for air in (-10.0, 0.0, 20.0, 35.0, 45.0):
            for wind in (0.0, 0.5, 3.0, 8.0, 15.0):
                ours = cell_temperature(poa, air, wind, "open_rack")
                theirs = float(pvlib.temperature.faiman(poa, air, wind))
                assert ours == pytest.approx(theirs, abs=0.01), f"{poa} {air} {wind}"


def test_a_declared_noct_is_still_faiman_with_shifted_coefficients() -> None:
    """The NOCT adjustment must move the coefficients, never the model.

    A declaration is read as a difference from the grammar's default and turned
    back into a Faiman pair, so the referee is pvlib's own faiman with that pair
    handed to it. What this holds is the arithmetic; that the pair is the right
    one is the definition of NOCT, which the sibling test in test_solar.py reads
    back at the conditions NOCT is stated under.
    """
    for mounting in ("open_rack", "close_roof"):
        for declared in (30.0, 45.0, 62.0, 85.0):
            u0, u1 = _faiman_u0(mounting, declared)
            for poa in (0.0, 400.0, 1000.0):
                for air in (-5.0, 25.0, 40.0):
                    for wind in (0.0, 2.0, 9.0):
                        ours = cell_temperature(poa, air, wind, mounting, noct=declared)
                        theirs = float(pvlib.temperature.faiman(poa, air, wind, u0=u0, u1=u1))
                        assert ours == pytest.approx(theirs, abs=0.01), (
                            f"{mounting} noct={declared} {poa} {air} {wind}"
                        )
