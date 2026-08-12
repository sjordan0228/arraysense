"""solar.py — what the sky and this array should have produced, from first principles.

A performance score is a comparison, and a comparison needs a defensible other
side. These are the published models the industry already uses — NOAA's solar
position, Hay-Davies transposition, Faiman cell temperature, the PVWatts derate
chain — transcribed rather than invented, and held to pvlib point by point in
the test suite so a transcription error cannot survive a test run.

pvlib itself is a development dependency and is imported only by those tests.
The physics here never changes: these are equations from the 1980s that will
read the same in another forty years. A numpy and pandas stack on the collector
would be a permanent upkeep for arithmetic that is a few hundred lines of
trigonometry, on a Raspberry Pi whose database already outgrew one SD card.

Pure functions throughout: no I/O, no state, no clock of their own. Everything
they need arrives as an argument, which is exactly what lets the referee hold
them to a reference implementation without a fixture in sight.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from arraysense.panels import DEFAULT_NOCT, StringSpec

# PVWatts' default derate stack — wiring, connections, mismatch, light-induced
# degradation, availability — the losses every installation has and none of
# them measures separately. It stands until the baseline fit replaces it with
# this array's own constant, which is the difference between a textbook figure
# and a measured one.
SYSTEM_DERATE = 0.86

# Resistance in ohms per 1000 feet, uncoated copper, from NEC chapter 9
# table 8's 75 C column. The warm column rather than the 20 C one because a PV
# string's conductors run in sun-heated conduit, and the cool figure would
# understate the loss by about a fifth on a summer afternoon -- the hours that
# carry the energy.
_AWG_OHMS_PER_KFT: dict[int, float] = {
    2: 0.194,
    4: 0.308,
    6: 0.491,
    8: 0.778,
    10: 1.24,
    12: 1.98,
    14: 3.14,
}

# PVWatts bundles a flat 2 % wiring allowance into SYSTEM_DERATE. A string that
# declares its actual run has that allowance divided back out before its real
# loss is subtracted, or the wire would be paid for twice -- once as a guess
# and once as a measurement.
_GENERIC_WIRING_FACTOR = 0.98

# The solar constant, for the extraterrestrial irradiance the anisotropy index
# is measured against.
_SOLAR_CONSTANT = 1367.0

# Ground reflectance. PVWatts' default for ordinary ground; a site over snow or
# water reflects far more, which is a per-string fact nobody here has measured,
# so the default stands rather than a guess dressed as a setting.
_ALBEDO = 0.2

# Faiman's coefficients per mounting. u0 is the constant heat-loss term and u1
# the wind-driven one: a close-roof panel has still air behind it and so sheds
# heat both more slowly and less sensitively to wind, which is why it runs
# hotter than an open rack in the same sun.
_FAIMAN: dict[str, tuple[float, float]] = {
    "open_rack": (25.0, 6.84),
    "ground": (25.0, 6.84),
    "close_roof": (15.0, 2.5),
}


def _day_angle(when: datetime) -> float:
    """The year as an angle, which is what the position formulae are written in."""
    moment = when.astimezone(UTC)
    day_of_year = moment.timetuple().tm_yday
    hour = moment.hour + moment.minute / 60 + moment.second / 3600
    days_in_year = 366 if _is_leap(moment.year) else 365
    return 2 * math.pi / days_in_year * (day_of_year - 1 + (hour - 12) / 24)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def solar_position(when: datetime, latitude: float, longitude: float) -> tuple[float, float]:
    """Where the sun is: elevation above the horizon, azimuth clockwise from north.

    NOAA's algorithm — the fractional year gives the equation of time and the
    declination, the true solar time gives the hour angle, and the two together
    place the sun. Azimuth is measured from north because that is what the
    array grammar stores, and because it is the convention every compass and
    every installer already uses.

    Both angles come back in degrees. Elevation is negative when the sun is
    below the horizon, which the callers use to skip an hour rather than
    modelling production from a sun that has set.
    """
    gamma = _day_angle(when)

    # Equation of time, in minutes: the difference between clock noon and the
    # sun's own noon, which swings by a quarter of an hour across the year.
    eqtime = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    moment = when.astimezone(UTC)
    minutes = moment.hour * 60 + moment.minute + moment.second / 60
    # True solar time in minutes, longitude included directly because the
    # timestamp is already UTC — there is no local zone offset to subtract.
    true_solar = (minutes + eqtime + 4 * longitude) % 1440
    hour_angle = math.radians(true_solar / 4 - 180)

    lat = math.radians(latitude)
    cos_zenith = math.sin(lat) * math.sin(declination) + math.cos(lat) * math.cos(
        declination
    ) * math.cos(hour_angle)
    cos_zenith = max(-1.0, min(1.0, cos_zenith))
    zenith = math.acos(cos_zenith)
    elevation = 90.0 - math.degrees(zenith)

    # atan2 rather than acos: the arccosine form loses the sign and puts every
    # afternoon sun in the morning sky.
    azimuth = math.degrees(
        math.atan2(
            -math.sin(hour_angle),
            math.tan(declination) * math.cos(lat) - math.sin(lat) * math.cos(hour_angle),
        )
    )
    return elevation, azimuth % 360.0


def _incidence_cosine(elevation: float, sun_azimuth: float, tilt: float, azimuth: float) -> float:
    """The cosine of the angle between the sun and the panel's normal."""
    zenith = math.radians(90.0 - elevation)
    return math.cos(zenith) * math.cos(math.radians(tilt)) + math.sin(zenith) * math.sin(
        math.radians(tilt)
    ) * math.cos(math.radians(sun_azimuth - azimuth))


def poa_irradiance(
    ghi: float,
    dni: float,
    dhi: float,
    elevation: float,
    sun_azimuth: float,
    tilt: float,
    azimuth: float,
    day_of_year: int = 172,
) -> float:
    """What a tilted panel actually receives, in W/m², from what the sky reports.

    Weather services measure irradiance on a horizontal surface; panels are not
    horizontal, and the difference is the largest single correction in the whole
    model. Hay-Davies splits the sky's diffuse light into a bright halo around
    the sun, which follows the beam, and an even background, which does not —
    the refinement over an isotropic sky that a tilted array at this latitude
    genuinely notices, without Perez's coefficient tables for a further fraction
    of a percent.

    ``day_of_year`` only sets the earth-sun distance correction, worth about
    three percent across the year; its default is the solstice, so a caller
    that does not care is wrong by less than two percent rather than by a
    season.

    Zero when the sun is down: a panel at night receives nothing, and the
    ground-reflected term would otherwise keep paying out after sunset.
    """
    if elevation <= 0.0:
        return 0.0

    cos_aoi = max(_incidence_cosine(elevation, sun_azimuth, tilt, azimuth), 0.0)
    beam = dni * cos_aoi

    # The anisotropy index: how much of the extraterrestrial BEAM survived the
    # atmosphere, which is what decides how much of the diffuse light is
    # clustered around the sun rather than spread across the sky. Measured
    # against the beam arriving normal to itself, never against its horizontal
    # projection — dividing by the projection makes the index blow past one
    # whenever the sun is low, which clamps it, erases the isotropic term
    # entirely, and models a hazy winter afternoon as producing almost nothing.
    extra_normal = _SOLAR_CONSTANT * (1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365.0))
    index = max(0.0, min(1.0, dni / extra_normal))

    sin_elevation = max(math.sin(math.radians(elevation)), 0.01)
    circumsolar = dhi * index * cos_aoi / sin_elevation
    isotropic = dhi * (1 - index) * (1 + math.cos(math.radians(tilt))) / 2
    ground = ghi * _ALBEDO * (1 - math.cos(math.radians(tilt))) / 2

    return max(beam + circumsolar + isotropic + ground, 0.0)


# The conditions NOCT is defined at, per IEC 61215: 800 W/m2 on the module,
# 20 C air, 1 m/s wind, open circuit. They are what lets a datasheet figure and
# a Faiman coefficient pair be read as statements about the same thing.
_NOCT_IRRADIANCE = 800.0
_NOCT_AIR_C = 20.0
_NOCT_WIND_MS = 1.0


def _faiman_u0(mounting: str, noct: float) -> tuple[float, float]:
    """This mounting's Faiman coefficients, adjusted for a declared NOCT.

    Faiman at NOCT conditions gives ``T_cell - T_air = 800 / (u0 + u1)``, so
    every mounting's coefficient pair already implies a NOCT: 45.1 C for the
    open rack, 65.7 C for a close roof. The open-rack figure is the grammar's
    own default to within a tenth of a degree, which is what makes the two
    reconcilable rather than merely combinable.

    The declaration is therefore read as a *difference* from the default, not as
    an absolute. A module whose datasheet says 48 C runs three degrees hotter at
    NOCT conditions than the ordinary module the mounting's pair describes,
    wherever it is mounted — so a close-roof array keeps its whole mounting
    penalty, which is the larger of the two terms and the one the owner did not
    get wrong. Reading the declaration as the absolute cell temperature would
    erase that penalty for anybody who typed their datasheet in correctly.

    A declaration equal to the default returns the mounting's pair unchanged and
    exactly, so an installation that declares nothing sees no figure move.
    """
    u0, u1 = _FAIMAN.get(mounting, _FAIMAN["open_rack"])
    rise = _NOCT_IRRADIANCE / (u0 + u1) + (noct - DEFAULT_NOCT)
    # The grammar bounds NOCT at 20-90, which keeps this comfortably positive on
    # every mounting; the floor is here so a caller reaching past the grammar
    # gets a very hot module rather than a division by zero.
    return _NOCT_IRRADIANCE / max(rise, 1.0) - u1, u1


def cell_temperature(
    poa: float,
    air_c: float,
    wind_ms: float,
    mounting: str,
    noct: float = DEFAULT_NOCT,
) -> float:
    """How hot the modules are, from the sun on them and the wind over them.

    A panel in full sun runs twenty-five to thirty-five degrees above the air
    around it, and every one of those degrees costs output through the module's
    temperature coefficient — which makes this one of the largest real losses
    in the whole waterfall, and the reason ambient temperature alone would model
    the array as far more productive than it is.

    Faiman's model, because it takes exactly the inputs a free weather service
    provides: irradiance, air temperature, wind speed. Wind arrives in m/s; the
    weather client converts it once, at the boundary, for this.

    ``noct`` shifts the mounting's coefficients to the module actually
    installed. It defaults to the grammar's own default, which returns the
    mounting's pair unchanged, so a string that declares nothing is modelled
    exactly as it was before this argument existed.
    """
    u0, u1 = _faiman_u0(mounting, noct)
    return air_c + poa / (u0 + u1 * wind_ms)


def _years_since(installed: str | None, when: datetime) -> float:
    """How old the array is in years, or zero when its birthday is unknown."""
    if installed is None:
        return 0.0
    year, month = (int(part) for part in installed.split("-"))
    months = (when.year - year) * 12 + (when.month - month)
    return max(months, 0) / 12.0


def wire_loss_watts(spec: StringSpec, watts: float) -> float:
    """What this string's DC run dissipates carrying ``watts`` to the inverter.

    I squared R over both conductors, so the run length counts twice. The
    current comes from the power and the string's own operating voltage rather
    than from a nameplate figure, because loss goes as the square of current:
    the same panels on a higher-voltage string lose proportionally less, and a
    long thin run on a low-voltage string is where the energy actually goes.

    Zero when the string has not been described -- no gauge, no length, or no
    module Vmp to derive a string voltage from. That is a real zero rather than
    an absent one: with nothing declared, the loss stays inside the generic
    derate where it has always been, and no figure moves.
    """
    if spec.wire_awg is None or spec.wire_run_ft is None or spec.vmp is None:
        return 0.0
    ohms_per_kft = _AWG_OHMS_PER_KFT.get(spec.wire_awg)
    if ohms_per_kft is None or watts <= 0.0:
        return 0.0
    volts = spec.panels * spec.vmp
    if volts <= 0.0:
        return 0.0
    resistance = 2.0 * (spec.wire_run_ft / 1000.0) * ohms_per_kft
    current = watts / volts
    return current * current * resistance


def expected_watts(spec: StringSpec, poa: float, cell_c: float, when: datetime) -> float:
    """What this string should be producing, in watts, under these conditions.

    The PVWatts chain: nameplate scaled by the light actually landing on the
    panels, derated for how hot they are, credited for whatever the rear side
    collects, aged by however long they have been outside, and finally reduced
    by the system losses nobody measures separately.

    Degradation applies only to a string that reported when it was installed.
    A rate without a date would be an assumption about an unknown age dressed
    as arithmetic, and the grammar deliberately keeps the two apart.
    """
    if poa <= 0.0:
        return 0.0
    nameplate = spec.panels * spec.watts
    heat = 1 + spec.temp_coeff * (cell_c - 25.0) / 100.0
    bifacial = 1 + spec.bifacial_pct / 100.0
    age = 1 - spec.degradation * _years_since(spec.installed, when) / 100.0
    watts = nameplate * (poa / 1000.0) * heat * bifacial * max(age, 0.0) * SYSTEM_DERATE
    if spec.wire_awg is not None and spec.wire_run_ft is not None and spec.vmp is not None:
        # Its own run replaces the guess rather than joining it.
        watts /= _GENERIC_WIRING_FACTOR
        watts -= wire_loss_watts(spec, watts)
    return max(watts, 0.0)
