"""forecast.py — what this array should make tomorrow, from the sky and its own record.

A forecast is the same question the efficiency engine already answers, asked one
day earlier and against a sky nobody has measured yet. So it runs the same chain
— sun position, transposition, cell temperature, the PVWatts derate — over
predicted conditions instead of recorded ones, and then scales the answer by the
ratio this array has actually been delivering.

The scale is what the previous model got wrong, and it is worth being precise
about how. It set ``K = observed_peak_pv / 950 W/m²`` and multiplied the
radiation forecast by it. One instant of cloud-edge enhancement on 17 July 2026
put 14,983 W through the inverter — the array's highest reading ever recorded —
and for the next thirty days every hour of every forecast was scaled as though
the array reached that. It called 118.7 kWh for 10 August, a day that modelled at
84.2 and produced 77.5, and its peak hour predicted 15,141 W as an *hourly mean*
against an instantaneous record of 14,983. A peak is the wrong statistic to
calibrate with: it is the one reading in a month guaranteed not to be typical.

The trailing median performance ratio is the right one, and not only because it
is robust. It closes a loop the peak never could: PR is measured as actual over
modelled, so if the array is described wrongly — a nameplate typed low, a tilt
guessed, a string miscounted — the model is wrong by some factor and the PR is
wrong by its reciprocal, and their product is still what this array makes. The
forecast is therefore right about the roof even when the configuration is not.

Two things it deliberately does not do. It does not predict curtailment: PR is
measured with curtailed energy removed from both sides, so this says what the
array would make with somewhere for the power to go, and a day the bank fills
early will fall short of it without the model being wrong. And it does not
invent a figure it cannot support — below ``MIN_SCORED_DAYS`` scored days it
falls back to the old peak-scaled curve and says so through ``basis``, because a
median of two days is not a demonstrated ratio.

Pure functions throughout: no I/O, no clock, no store. The poller fetches, reads
the stored days, and hands both in.
"""

from __future__ import annotations

import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from arraysense.efficiency import EfficiencyRow
from arraysense.panels import StringSpec
from arraysense.solar import cell_temperature, expected_watts, poa_irradiance, solar_position

logger = logging.getLogger(__name__)

# How far back the delivered ratio is measured. Four weeks is long enough that
# one overcast week cannot dominate the median and short enough to follow a real
# change — soiling building up over a dry month, or a string that has just been
# cleaned. It is also the window the Efficiency page's own trend draws.
PR_WINDOW_DAYS = 28

# The fewest scored days that make a median mean anything. Below this the
# forecast falls back rather than presenting a ratio fitted to a long weekend as
# though it described the array.
MIN_SCORED_DAYS = 5

# The fallback's clear-sky reference peak, carried over unchanged from the model
# this replaces so a fresh install behaves exactly as it did before. It is only
# ever reached in the first few days of an installation's life.
CLEAR_SKY_PEAK_RADIATION = 950.0  # W/m²


@dataclass(frozen=True)
class SkyHour:
    """One hour of predicted conditions, stamped by the hour it BEGINS.

    Every field the model needs and nothing it does not. Wind is in metres per
    second and radiation is attributed to the hour it covers rather than the one
    it is labelled with; both conversions happen once, in the weather client, so
    no consumer can inherit the wrong unit or the wrong hour.
    """

    when: datetime
    ghi: float
    dni: float
    dhi: float
    air_c: float
    wind_ms: float


def trailing_pr(rows: Sequence[EfficiencyRow], min_days: int = MIN_SCORED_DAYS) -> float | None:
    """The array's demonstrated performance ratio, or None if too few days say.

    Reads only the total rows — ``string_name`` of ``""`` — because the forecast
    is for the array, and only complete days, because a day the collector watched
    half of has a ratio fitted to whichever half that was.

    The median rather than the mean, and the reference site says why: across the
    28 days to 11 August 2026 the daily ratios ran from 0.607 to 1.085 around a
    median of 0.915. A mean is dragged by the overcast tail toward a figure no
    ordinary day resembles; the median names the middle day, which is what
    "what this array usually delivers" means.
    """
    ratios = [r.pr for r in rows if r.string_name == "" and not r.partial and r.pr is not None]
    if len(ratios) < min_days:
        return None
    return statistics.median(ratios)


def modelled_watts(
    strings: Sequence[StringSpec],
    latitude: float,
    longitude: float,
    hour: SkyHour,
) -> float:
    """What the array should produce in this hour, before any measured scaling.

    The same chain ``efficiency.compute_hours`` runs, over predicted conditions
    rather than recorded ones. Deriving expected production twice would be the
    Costs page's tariff mistake against a harder sum, so the physics stays in
    ``solar.py`` and both callers walk it in the same order.

    Zero when the sun is down. A panel at night produces nothing, and the
    ground-reflected term would otherwise keep paying out after sunset.
    """
    elevation, sun_azimuth = solar_position(hour.when, latitude, longitude)
    if elevation <= 0.0:
        return 0.0
    day_of_year = hour.when.timetuple().tm_yday
    total = 0.0
    for spec in strings:
        poa = poa_irradiance(
            hour.ghi,
            hour.dni,
            hour.dhi,
            elevation,
            sun_azimuth,
            spec.tilt,
            spec.azimuth,
            day_of_year,
        )
        cell_c = cell_temperature(poa, hour.air_c, hour.wind_ms, spec.mounting)
        total += expected_watts(spec, poa, cell_c, hour.when)
    return total


def expected_points(
    strings: Sequence[StringSpec],
    latitude: float,
    longitude: float,
    sky: Sequence[SkyHour],
    pr: float,
) -> list[tuple[datetime, float]]:
    """The modelled curve scaled by what the array has been delivering.

    One point per predicted hour, in watts, floored at zero. The caller stores
    them in the forecast table, never a metric column: a prediction is not a
    measurement, and the two must never share a home.
    """
    return [
        (hour.when, max(modelled_watts(strings, latitude, longitude, hour) * pr, 0.0))
        for hour in sky
    ]


def fallback_points(
    sky: Sequence[SkyHour], observed_peak_pv: float
) -> list[tuple[datetime, float]]:
    """The old peak-scaled curve, for an array with too little history to judge.

    Kept exactly as it was rather than improved, because its whole job is to be
    the behaviour a fresh install has always had until five days have been
    scored. It over-calls, and it is labelled as an estimate for that reason.
    """
    k = observed_peak_pv / CLEAR_SKY_PEAK_RADIATION
    return [(hour.when, max(hour.ghi * k, 0.0)) for hour in sky]
