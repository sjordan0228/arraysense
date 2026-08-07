"""Tests for band splitting: arraysense.costs."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from arraysense.costs import band_intervals, period_energy
from arraysense.tariff import Tariff, parse_bands

TZ = ZoneInfo("America/Chicago")

# The reference installation's actual tariff. Time-of-use May to October,
# a single flat rate November to April.
COSERV = Tariff(
    bands=parse_bands(
        "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
        "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
        "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
    ),
    fixed_monthly=15.0,
)


def _day(y: int, m: int, d: int, h: int = 0) -> datetime:
    return datetime(y, m, d, h, tzinfo=TZ)


def test_a_summer_day_is_cut_at_the_peak_window() -> None:
    got = band_intervals(COSERV, _day(2026, 7, 15), _day(2026, 7, 16), TZ)
    assert [i.band for i in got] == ["Off-peak", "On-peak", "Off-peak"]
    assert got[1].start.hour == 15
    assert got[1].end.hour == 20


def test_a_winter_day_has_no_peak_at_all() -> None:
    # The defect this module exists to prevent: the browser applied the summer
    # peak window all year, inventing a peak/off-peak split for six months of
    # every year and pricing a January evening at 2.4x the real rate.
    got = band_intervals(COSERV, _day(2027, 1, 15), _day(2027, 1, 16), TZ)
    assert [i.band for i in got] == ["Winter"]


def test_the_season_turns_mid_period() -> None:
    # 31 October into 1 November: the pattern changes shape partway through.
    got = band_intervals(COSERV, _day(2026, 10, 31, 12), _day(2026, 11, 1, 12), TZ)
    names = [i.band for i in got]
    assert "On-peak" in names
    assert names[-1] == "Winter"


def test_a_gap_in_the_schedule_is_reported_rather_than_dropped() -> None:
    # Unpriced energy must be visible. Silently omitting it makes a bill look
    # smaller than it is.
    sparse = Tariff(bands=parse_bands("Peak | 0.30 | 15:00-20:00"), fixed_monthly=0.0)
    got = band_intervals(sparse, _day(2026, 7, 15), _day(2026, 7, 16), TZ)
    assert None in [i.band for i in got]


def test_a_period_longer_than_the_scan_limit_is_refused() -> None:
    with pytest.raises(ValueError, match="70 days"):
        band_intervals(COSERV, _day(2026, 1, 1), _day(2026, 6, 1), TZ)


def test_a_backwards_period_yields_nothing() -> None:
    assert band_intervals(COSERV, _day(2026, 7, 16), _day(2026, 7, 15), TZ) == []


def _rows(start: datetime, hours: int, per_hour: float) -> list[dict[str, object]]:
    """Lifetime counters climbing by a fixed amount each hour."""
    return [
        {
            "timestamp": start + timedelta(hours=h),
            "grid_import_energy_total_kwh": 1000.0 + h * per_hour,
            "load_energy_total_kwh": 2000.0 + h * per_hour * 2,
            "grid_export_energy_total_kwh": 5.0,
        }
        for h in range(hours + 1)
    ]


def test_energy_lands_in_the_band_that_was_in_force() -> None:
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    # Five peak hours of one kWh each, nineteen off-peak.
    assert energy.grid_import_kwh["On-peak"] == pytest.approx(5.0, abs=0.01)
    assert energy.grid_import_kwh["Off-peak"] == pytest.approx(19.0, abs=0.01)


def test_a_band_the_period_never_entered_is_absent_not_zero() -> None:
    # Absent means there is nothing to say; zero means measured and nothing
    # happened. A projection built on a band that has not occurred is a guess.
    start, end = _day(2027, 1, 15), _day(2027, 1, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    assert "Winter" in energy.grid_import_kwh
    assert "On-peak" not in energy.grid_import_kwh


def test_no_readings_produce_no_band_totals() -> None:
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, [], start, end, TZ)
    assert energy.grid_import_kwh == {}


def test_the_real_tariff_prices_a_summer_day_correctly() -> None:
    from arraysense.tariff import compute_cost

    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    result = compute_cost(COSERV, energy)
    assert result is not None
    # 5 kWh peak at 0.210321 plus 19 kWh off-peak at 0.086709.
    expected = 5 * 0.210321 + 19 * 0.086709
    assert result.energy_cost == pytest.approx(expected, abs=0.02)
