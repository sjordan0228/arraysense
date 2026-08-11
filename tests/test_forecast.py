"""test_forecast.py — the forecast scales by what the array has demonstrated.

The engine's own arithmetic is held to pvlib elsewhere; what matters here is the
scaling decision — which basis is chosen, what the median does to an outlier,
and that a fresh install falls back rather than inventing a ratio.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arraysense.efficiency import EfficiencyRow
from arraysense.forecast import (
    MIN_SCORED_DAYS,
    SkyHour,
    expected_points,
    fallback_points,
    modelled_watts,
    trailing_pr,
)
from arraysense.panels import parse_strings

# The reference array, three strings on one roof.
STRINGS = parse_strings(
    "PV1 | 1 | 12 | 370 | 27 | 180 | vmp=34.5 temp_coeff=-0.36\n"
    "PV2 | 2 | 12 | 400 | 5 | 180 | vmp=31.01 temp_coeff=-0.35\n"
    "PV3 | 3 | 12 | 400 | 5 | 180 | vmp=31.01 temp_coeff=-0.35"
)
LAT, LON = 33.088264, -97.201443


def _day(n: int, pr: float | None, *, partial: bool = False, name: str = "") -> EfficiencyRow:
    """One stored total row, n days ago, carrying ``pr``."""
    return EfficiencyRow(
        day=datetime(2026, 8, 10, tzinfo=UTC) - timedelta(days=n),
        string_name=name,
        expected_kwh=80.0,
        actual_kwh=80.0 * (pr or 0.0),
        curtailed_kwh=0.0,
        unexplained_kwh=0.0,
        modelled_hours=14,
        partial=partial,
        pr=pr,
        config_version=1,
    )


def _noon() -> SkyHour:
    """A bright hour at the reference site: clear-sky August, light breeze."""
    return SkyHour(
        when=datetime(2026, 8, 10, 18, tzinfo=UTC),
        ghi=900.0,
        dni=850.0,
        dhi=110.0,
        air_c=35.0,
        wind_ms=3.0,
    )


def test_too_few_scored_days_yields_no_ratio() -> None:
    """Four days is not a demonstrated ratio, however consistent they look."""
    rows = [_day(n, 0.9) for n in range(MIN_SCORED_DAYS - 1)]
    assert trailing_pr(rows) is None


def test_enough_scored_days_yields_their_median() -> None:
    rows = [_day(n, pr) for n, pr in enumerate([0.80, 0.85, 0.90, 0.95, 1.00])]
    assert trailing_pr(rows) == pytest.approx(0.90)


def test_one_overcast_day_does_not_drag_the_ratio() -> None:
    """Why the median and not the mean, in one case.

    Four ordinary days and one the array spent under cloud. The median names an
    ordinary day; the mean names a day that did not happen and would under-call
    every forecast for the next four weeks.
    """
    ratios = [0.90, 0.91, 0.92, 0.93, 0.10]
    rows = [_day(n, pr) for n, pr in enumerate(ratios)]
    assert trailing_pr(rows) == pytest.approx(0.91)
    assert sum(ratios) / len(ratios) == pytest.approx(0.752)


def test_partial_days_are_not_counted() -> None:
    """A day the collector watched half of has a ratio fitted to whichever half."""
    rows = [_day(n, 0.9) for n in range(4)] + [_day(9, 0.2, partial=True)]
    assert trailing_pr(rows) is None


def test_per_string_rows_are_not_counted() -> None:
    """The forecast is for the array, so only the total rows speak for it."""
    rows = [_day(n, 0.9, name="PV1") for n in range(10)]
    assert trailing_pr(rows) is None


def test_a_day_with_no_ratio_is_not_counted_as_zero() -> None:
    """A day that could not be scored is absent, never a zero dragging the median."""
    rows = [_day(n, 0.9) for n in range(4)] + [_day(9, None)]
    assert trailing_pr(rows) is None


def test_the_sun_below_the_horizon_produces_nothing() -> None:
    midnight = SkyHour(
        when=datetime(2026, 8, 10, 6, tzinfo=UTC),
        ghi=0.0,
        dni=0.0,
        dhi=0.0,
        air_c=24.0,
        wind_ms=2.0,
    )
    assert modelled_watts(STRINGS, LAT, LON, midnight) == 0.0


def test_a_bright_hour_models_a_plausible_output() -> None:
    """Not a golden number — a bound. 14.4 kWp of panels in near-full sun."""
    watts = modelled_watts(STRINGS, LAT, LON, _noon())
    assert 8_000 < watts < 15_000


def test_the_ratio_scales_the_whole_curve() -> None:
    sky = [_noon()]
    full = expected_points(STRINGS, LAT, LON, sky, 1.0)
    half = expected_points(STRINGS, LAT, LON, sky, 0.5)
    assert half[0][1] == pytest.approx(full[0][1] / 2)
    assert half[0][0] == sky[0].when


def test_hotter_air_lowers_the_expectation() -> None:
    """The temperature coefficient is negative, so this must fall, not rise."""
    cool = _noon()
    hot = SkyHour(
        when=cool.when,
        ghi=cool.ghi,
        dni=cool.dni,
        dhi=cool.dhi,
        air_c=cool.air_c + 15.0,
        wind_ms=cool.wind_ms,
    )
    assert modelled_watts(STRINGS, LAT, LON, hot) < modelled_watts(STRINGS, LAT, LON, cool)


def test_wind_raises_the_expectation() -> None:
    """Still air runs the cells hotter, which costs output. See CLAUDE.md."""
    calm = SkyHour(
        when=_noon().when,
        ghi=900.0,
        dni=850.0,
        dhi=110.0,
        air_c=35.0,
        wind_ms=0.0,
    )
    breezy = SkyHour(
        when=calm.when,
        ghi=calm.ghi,
        dni=calm.dni,
        dhi=calm.dhi,
        air_c=calm.air_c,
        wind_ms=6.0,
    )
    assert modelled_watts(STRINGS, LAT, LON, breezy) > modelled_watts(STRINGS, LAT, LON, calm)


def test_the_fallback_is_the_old_peak_scaled_curve() -> None:
    """Unchanged behaviour for an installation with nothing to demonstrate yet."""
    sky = [_noon()]
    points = fallback_points(sky, observed_peak_pv=9_500.0)
    assert points[0][1] == pytest.approx(900.0 * 9_500.0 / 950.0)


def test_the_fallback_never_goes_negative() -> None:
    dark = SkyHour(
        when=_noon().when,
        ghi=0.0,
        dni=0.0,
        dhi=0.0,
        air_c=20.0,
        wind_ms=1.0,
    )
    assert fallback_points([dark], observed_peak_pv=9_500.0)[0][1] == 0.0
