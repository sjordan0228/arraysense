"""Tests for tariffs, cost and savings: arraysense.tariff."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from arraysense.settings import describe, lookup_setting
from arraysense.tariff import (
    EXAMPLE_ADJUSTMENTS,
    EXAMPLE_BANDS,
    SETTING_ADJUSTMENTS,
    SETTING_BANDS,
    SETTING_CURRENCY,
    SETTING_EXPORT_PER_KWH,
    SETTING_FIXED_MONTHLY,
    PeriodEnergy,
    Tariff,
    apportion_fixed,
    compute_cost,
    counter_delta,
    estimate_bill,
    load_tariff,
    parse_adjustments,
    parse_bands,
)

EAST = ZoneInfo("America/New_York")

# The reference bill: $25 a month whatever the usage, a peak rate in the
# evening and an off-peak rate the rest of the day.
REFERENCE: dict[str, object] = {
    SETTING_BANDS: "Peak | 0.34 | 16:00-21:00; Off-peak | 0.11 | 21:00-16:00",
    SETTING_FIXED_MONTHLY: 25.0,
    SETTING_CURRENCY: "$",
    SETTING_EXPORT_PER_KWH: 0.0,
}


def tariff(**overrides: object) -> Tariff:
    loaded = load_tariff(dict(REFERENCE) | overrides)
    assert loaded is not None
    return loaded


def band_name(loaded: Tariff, moment: datetime) -> str | None:
    band = loaded.band_at(moment)
    return None if band is None else band.name


def january(
    grid_import_kwh: Mapping[str, float],
    load_kwh: Mapping[str, float] | None = None,
    grid_export_kwh: float | None = None,
) -> PeriodEnergy:
    """The first fifteen days of January: fifteen days of a thirty-one day month."""
    return PeriodEnergy(
        start=datetime(2026, 1, 1, tzinfo=EAST),
        end=datetime(2026, 1, 16, tzinfo=EAST),
        grid_import_kwh=grid_import_kwh,
        load_kwh=load_kwh,
        grid_export_kwh=grid_export_kwh,
    )


# --- The tariff itself ------------------------------------------------------


def test_the_reference_tariff_loads_from_settings() -> None:
    loaded = tariff()
    assert [band.name for band in loaded.bands] == ["Peak", "Off-peak"]
    assert loaded.fixed_monthly == 25.0
    assert loaded.currency == "$"


def test_a_band_spanning_midnight_covers_both_sides_of_it() -> None:
    # Off-peak runs 21:00 to 16:00, which wraps. Both 23:30 and 02:00 are in
    # it, and a naive start <= t <= end test puts neither of them anywhere.
    loaded = tariff()
    assert band_name(loaded, datetime(2026, 1, 5, 23, 30, tzinfo=EAST)) == "Off-peak"
    assert band_name(loaded, datetime(2026, 1, 6, 2, 0, tzinfo=EAST)) == "Off-peak"
    assert band_name(loaded, datetime(2026, 1, 6, 17, 0, tzinfo=EAST)) == "Peak"


def test_a_band_boundary_belongs_to_the_band_it_starts() -> None:
    loaded = tariff()
    assert band_name(loaded, datetime(2026, 1, 6, 16, 0, tzinfo=EAST)) == "Peak"
    assert band_name(loaded, datetime(2026, 1, 6, 21, 0, tzinfo=EAST)) == "Off-peak"


def test_the_price_at_a_moment_comes_from_its_band() -> None:
    loaded = tariff()
    assert loaded.price_at(datetime(2026, 1, 6, 18, 0, tzinfo=EAST)) == 0.34
    assert loaded.price_at(datetime(2026, 1, 6, 3, 0, tzinfo=EAST)) == 0.11


def test_an_hour_no_band_covers_has_no_price_rather_than_a_free_one() -> None:
    partial = tariff(**{SETTING_BANDS: "Peak | 0.34 | 16:00-21:00"})
    dawn = datetime(2026, 1, 6, 3, 0, tzinfo=EAST)
    assert partial.band_at(dawn) is None
    assert partial.price_at(dawn) is None


def test_a_naive_moment_is_refused_rather_than_priced_as_utc() -> None:
    # Bands are wall-clock hours. A naive datetime has no zone, and reading a
    # UTC instant as a wall clock prices four in the afternoon as nine at night.
    with pytest.raises(ValueError, match="timezone-aware"):
        tariff().band_at(datetime(2026, 1, 6, 18, 0))


def test_a_third_band_is_an_addition_rather_than_a_rewrite() -> None:
    three = tariff(
        **{
            SETTING_BANDS: (
                "Peak | 0.40 | 16:00-21:00; "
                "Shoulder | 0.20 | 07:00-16:00; "
                "Off-peak | 0.09 | 21:00-07:00"
            )
        }
    )
    assert three.price_at(datetime(2026, 1, 6, 9, 0, tzinfo=EAST)) == 0.20
    assert three.price_at(datetime(2026, 1, 6, 18, 0, tzinfo=EAST)) == 0.40
    assert three.price_at(datetime(2026, 1, 6, 4, 0, tzinfo=EAST)) == 0.09


def test_a_band_may_hold_several_time_ranges() -> None:
    split = tariff(
        **{SETTING_BANDS: "Peak | 0.34 | 07:00-09:00, 17:00-20:00; Off | 0.10 | 09:00-17:00"}
    )
    assert split.price_at(datetime(2026, 1, 6, 8, 0, tzinfo=EAST)) == 0.34
    assert split.price_at(datetime(2026, 1, 6, 18, 0, tzinfo=EAST)) == 0.34
    assert split.price_at(datetime(2026, 1, 6, 12, 0, tzinfo=EAST)) == 0.10


def test_a_whole_day_band_uses_midnight_to_midnight() -> None:
    flat = tariff(**{SETTING_BANDS: "Standard | 0.25 | 00:00-24:00"})
    assert flat.price_at(datetime(2026, 1, 6, 0, 0, tzinfo=EAST)) == 0.25
    assert flat.price_at(datetime(2026, 1, 6, 23, 59, tzinfo=EAST)) == 0.25


def test_the_example_in_the_settings_help_is_one_that_parses() -> None:
    # The help text is the only documentation of the format a person types.
    # An example that would be refused on save is worse than none.
    assert parse_bands(EXAMPLE_BANDS)
    assert EXAMPLE_BANDS in str(lookup_setting(SETTING_BANDS).help)


@pytest.mark.parametrize(
    "text",
    [
        "Peak | 0.34",
        "Peak | free | 16:00-21:00",
        "Peak | -0.10 | 16:00-21:00",
        "Peak | 0.34 | 16:00",
        "Peak | 0.34 | 25:00-26:00",
        "Peak | 0.34 | 16:00-16:00",
        "| 0.34 | 16:00-21:00",
    ],
)
def test_a_band_that_cannot_be_read_is_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_bands(text)


def test_two_bands_of_the_same_name_are_refused() -> None:
    # Energy is reported per band by name. Two bands called the same thing
    # make that mapping ambiguous, and the ambiguity would show up as money.
    with pytest.raises(ValueError, match="peak"):
        parse_bands("Peak | 0.34 | 16:00-21:00; peak | 0.11 | 21:00-16:00")


# --- No tariff --------------------------------------------------------------


def test_no_tariff_configured_is_no_tariff_rather_than_a_guess() -> None:
    assert load_tariff({}) is None
    assert load_tariff({SETTING_BANDS: ""}) is None
    assert load_tariff({SETTING_BANDS: "   "}) is None


def test_a_tariff_that_will_not_parse_is_no_tariff_rather_than_a_wrong_one() -> None:
    assert load_tariff({SETTING_BANDS: "Peak, 0.34, always"}) is None


def test_without_a_tariff_there_is_no_money_at_all_not_zero() -> None:
    energy = january(
        grid_import_kwh={"Peak": 10.0, "Off-peak": 20.0},
        load_kwh={"Peak": 40.0, "Off-peak": 60.0},
    )
    assert compute_cost(None, energy) is None
    assert estimate_bill(None, energy) is None


# --- Cost -------------------------------------------------------------------


def test_grid_import_is_priced_in_the_band_it_arrived_in() -> None:
    result = compute_cost(
        tariff(**{SETTING_FIXED_MONTHLY: 0.0}),
        january(grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0}),
    )
    assert result is not None
    assert result.energy_cost == pytest.approx(100 * 0.34 + 200 * 0.11)
    assert result.currency == "$"
    assert {band.name: band.cost for band in result.bands} == {"Peak": 34.0, "Off-peak": 22.0}


def test_a_period_entirely_off_peak_is_priced_entirely_off_peak() -> None:
    overnight = PeriodEnergy(
        start=datetime(2026, 1, 5, 22, 0, tzinfo=EAST),
        end=datetime(2026, 1, 6, 5, 0, tzinfo=EAST),
        grid_import_kwh={"Peak": 0.0, "Off-peak": 8.0},
    )
    result = compute_cost(tariff(**{SETTING_FIXED_MONTHLY: 0.0}), overnight)
    assert result is not None
    assert result.energy_cost == 0.88
    assert result.cost == 0.88


def test_a_band_with_no_energy_reported_leaves_the_cost_absent_not_zero() -> None:
    # Off-peak said nothing. Nought kilowatt-hours and "we do not know" are
    # different facts, and pricing the second as the first invents a bill.
    result = compute_cost(tariff(), january(grid_import_kwh={"Peak": 100.0}))
    assert result is not None
    assert result.energy_cost is None
    assert result.cost is None
    assert result.savings is None
    # What is known is still reported.
    assert {band.name: band.kwh for band in result.bands} == {"Peak": 100.0, "Off-peak": None}
    assert result.fixed_charge == 12.1


def test_money_is_rounded_once_at_the_end_rather_than_per_band() -> None:
    # Three bands at 0.334 each round to 0.33 apiece — 0.99 if the total is
    # built from rounded parts, 1.00 if the total is rounded once.
    three = tariff(
        **{
            SETTING_BANDS: (
                "A | 0.334 | 00:00-08:00; B | 0.334 | 08:00-16:00; C | 0.334 | 16:00-24:00"
            ),
            SETTING_FIXED_MONTHLY: 0.0,
        }
    )
    result = compute_cost(three, january(grid_import_kwh={"A": 1.0, "B": 1.0, "C": 1.0}))
    assert result is not None
    assert [band.cost for band in result.bands] == [0.33, 0.33, 0.33]
    assert result.energy_cost == 1.0


# --- The fixed charge -------------------------------------------------------


def test_the_fixed_charge_is_apportioned_across_a_partial_month() -> None:
    # Fifteen days of a thirty-one day January: $25 * 15/31.
    result = compute_cost(tariff(), january(grid_import_kwh={"Peak": 0.0, "Off-peak": 0.0}))
    assert result is not None
    assert result.fixed_charge == 12.1
    assert result.cost == 12.1


def test_a_whole_month_carries_the_whole_fixed_charge() -> None:
    charge = apportion_fixed(
        25.0, datetime(2026, 1, 1, tzinfo=EAST), datetime(2026, 2, 1, tzinfo=EAST)
    )
    assert charge == pytest.approx(25.0)


def test_a_period_spanning_two_months_is_apportioned_to_each() -> None:
    # Half of January and half of February, each against its own length.
    charge = apportion_fixed(
        25.0, datetime(2026, 1, 17, tzinfo=EAST), datetime(2026, 2, 15, tzinfo=EAST)
    )
    assert charge == pytest.approx(25.0 * 15 / 31 + 25.0 * 14 / 28)


def test_the_month_daylight_saving_lengthens_is_measured_in_real_time() -> None:
    # November 1st 2026 is twenty-five hours long in New York. The month is
    # 721 hours, not 720, and the first fifteen days are 361 of them —
    # dividing by 86400 gives 12.50 where the real answer is 12.52.
    charge = apportion_fixed(
        25.0, datetime(2026, 11, 1, tzinfo=EAST), datetime(2026, 11, 16, tzinfo=EAST)
    )
    assert round(charge, 2) == 12.52


def test_the_month_daylight_saving_shortens_is_measured_in_real_time() -> None:
    charge = apportion_fixed(
        25.0, datetime(2026, 3, 1, tzinfo=EAST), datetime(2026, 3, 16, tzinfo=EAST)
    )
    assert round(charge, 2) == 12.08


# --- Savings ----------------------------------------------------------------


def test_savings_is_the_counterfactual_minus_what_the_grid_actually_cost() -> None:
    result = compute_cost(
        tariff(),
        january(
            grid_import_kwh={"Peak": 40.0, "Off-peak": 0.0},
            load_kwh={"Peak": 100.0, "Off-peak": 0.0},
        ),
    )
    assert result is not None
    assert result.no_solar_cost == 34.0
    assert result.energy_cost == pytest.approx(13.6)
    assert result.savings == pytest.approx(20.4)


def test_savings_excludes_the_fixed_charge() -> None:
    # The connection charge is payable whatever the usage. Counting it against
    # the solar makes the array look worse the more it saved.
    result = compute_cost(
        tariff(),
        january(
            grid_import_kwh={"Peak": 40.0, "Off-peak": 0.0},
            load_kwh={"Peak": 100.0, "Off-peak": 0.0},
        ),
    )
    assert result is not None
    assert result.fixed_charge == 12.1
    assert result.savings == pytest.approx(20.4)
    assert result.cost == pytest.approx(13.6 + 12.1)


def test_savings_is_absent_when_the_house_load_was_not_reported() -> None:
    result = compute_cost(tariff(), january(grid_import_kwh={"Peak": 40.0, "Off-peak": 5.0}))
    assert result is not None
    assert result.cost is not None
    assert result.no_solar_cost is None
    assert result.savings is None


def test_grid_charging_the_battery_can_show_as_a_loss_rather_than_be_hidden() -> None:
    # Importing more than the house used means the bank was charged from the
    # grid. Clamping that to zero would hide a tariff setting that costs money.
    result = compute_cost(
        tariff(),
        january(
            grid_import_kwh={"Peak": 0.0, "Off-peak": 100.0},
            load_kwh={"Peak": 0.0, "Off-peak": 40.0},
        ),
    )
    assert result is not None
    assert result.savings == pytest.approx(-6.6)


# --- Export credit ----------------------------------------------------------


def test_export_is_credited_when_the_tariff_pays_for_it() -> None:
    result = compute_cost(
        tariff(**{SETTING_EXPORT_PER_KWH: 0.08}),
        january(grid_import_kwh={"Peak": 0.0, "Off-peak": 0.0}, grid_export_kwh=50.0),
    )
    assert result is not None
    assert result.export_credit == 4.0


def test_a_tariff_with_no_export_rate_has_no_credit_rather_than_zero() -> None:
    result = compute_cost(
        tariff(), january(grid_import_kwh={"Peak": 0.0, "Off-peak": 0.0}, grid_export_kwh=50.0)
    )
    assert result is not None
    assert result.export_credit is None


def test_export_that_was_not_reported_is_absent_rather_than_zero() -> None:
    result = compute_cost(
        tariff(**{SETTING_EXPORT_PER_KWH: 0.08}),
        january(grid_import_kwh={"Peak": 0.0, "Off-peak": 0.0}),
    )
    assert result is not None
    assert result.export_credit is None


# --- The estimated bill -----------------------------------------------------


def test_the_bill_extrapolates_to_the_month_end_and_adds_the_fixed_charge_once() -> None:
    # Half of January at $2 a day: $62 of energy for the month, plus $25.
    bill = estimate_bill(tariff(), january(grid_import_kwh={"Peak": 0.0, "Off-peak": 30.0 / 0.11}))
    assert bill is not None
    assert bill.energy_cost_so_far == 30.0
    assert bill.projected_energy_cost == pytest.approx(62.0, abs=0.01)
    assert bill.fixed_charge == 25.0
    assert bill.estimated_total == pytest.approx(87.0, abs=0.01)


def test_a_mid_month_bill_says_it_is_partial() -> None:
    bill = estimate_bill(tariff(), january(grid_import_kwh={"Peak": 0.0, "Off-peak": 10.0}))
    assert bill is not None
    assert bill.is_partial
    assert bill.fraction_elapsed == pytest.approx(15 / 31)
    assert bill.month_start == datetime(2026, 1, 1, tzinfo=EAST)
    assert bill.month_end == datetime(2026, 2, 1, tzinfo=EAST)


def test_a_complete_month_is_not_extrapolated_and_is_not_partial() -> None:
    bill = estimate_bill(
        tariff(),
        PeriodEnergy(
            start=datetime(2026, 1, 1, tzinfo=EAST),
            end=datetime(2026, 2, 1, tzinfo=EAST),
            grid_import_kwh={"Peak": 0.0, "Off-peak": 100.0},
        ),
    )
    assert bill is not None
    assert not bill.is_partial
    assert bill.projected_energy_cost == pytest.approx(11.0)
    assert bill.estimated_total == pytest.approx(36.0)


def test_a_bill_with_a_band_unaccounted_for_is_absent_not_the_fixed_charge_alone() -> None:
    bill = estimate_bill(tariff(), january(grid_import_kwh={"Peak": 1.0}))
    assert bill is not None
    assert bill.energy_cost_so_far is None
    assert bill.projected_energy_cost is None
    assert bill.estimated_total is None
    assert bill.fixed_charge == 25.0


def test_a_period_of_no_duration_is_not_extrapolated() -> None:
    instant = datetime(2026, 1, 16, tzinfo=EAST)
    bill = estimate_bill(
        tariff(),
        PeriodEnergy(start=instant, end=instant, grid_import_kwh={"Peak": 0.0, "Off-peak": 0.0}),
    )
    assert bill is not None
    assert bill.projected_energy_cost is None
    assert bill.estimated_total is None


def test_the_projected_bill_is_net_of_the_export_the_tariff_pays_for() -> None:
    bill = estimate_bill(
        tariff(**{SETTING_EXPORT_PER_KWH: 0.10}),
        january(grid_import_kwh={"Peak": 0.0, "Off-peak": 100.0}, grid_export_kwh=50.0),
    )
    assert bill is not None
    assert bill.projected_export_credit == pytest.approx(5.0 * 31 / 15, abs=0.01)
    assert bill.estimated_total == pytest.approx(11.0 * 31 / 15 + 25.0 - 5.0 * 31 / 15, abs=0.01)


# --- Energy from the inverter's own counters --------------------------------


def test_a_period_is_the_lifetime_counter_at_each_end() -> None:
    # End minus start is right even if the collector was down in between,
    # which is exactly why integrating power is not used here.
    assert counter_delta(1000.0, 1050.5) == pytest.approx(50.5)
    assert counter_delta(1000.0, 1000.0) == 0.0


def test_a_counter_reset_gives_no_answer_rather_than_a_negative_one() -> None:
    # Firmware updates zero the lifetime counters. Subtracting across that
    # produces a large negative, which would read as a refund.
    assert counter_delta(1000.0, 3.2) is None


def test_a_counter_that_was_not_read_gives_no_answer() -> None:
    assert counter_delta(None, 50.0) is None
    assert counter_delta(50.0, None) is None


# --- The settings registry --------------------------------------------------


def test_every_tariff_setting_the_module_reads_is_registered() -> None:
    for key in (SETTING_BANDS, SETTING_FIXED_MONTHLY, SETTING_CURRENCY, SETTING_EXPORT_PER_KWH):
        assert lookup_setting(key).key == key


def test_an_unconfigured_install_has_no_tariff() -> None:
    defaults: dict[str, object] = {
        key: lookup_setting(key).default
        for key in (SETTING_BANDS, SETTING_FIXED_MONTHLY, SETTING_CURRENCY, SETTING_EXPORT_PER_KWH)
    }
    assert load_tariff(defaults) is None


def test_the_currency_is_whatever_was_configured() -> None:
    result = compute_cost(
        tariff(**{SETTING_CURRENCY: "£"}),
        january(grid_import_kwh={"Peak": 0.0, "Off-peak": 0.0}),
    )
    assert result is not None
    assert result.currency == "£"


# --- The period itself ------------------------------------------------------


def test_a_period_with_naive_bounds_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        PeriodEnergy(
            start=datetime(2026, 1, 1),
            end=datetime(2026, 1, 16),
            grid_import_kwh={"Peak": 1.0},
        )


def test_a_period_that_ends_before_it_starts_is_refused() -> None:
    with pytest.raises(ValueError, match="end"):
        PeriodEnergy(
            start=datetime(2026, 1, 16, tzinfo=UTC),
            end=datetime(2026, 1, 1, tzinfo=UTC),
            grid_import_kwh={"Peak": 1.0},
        )


# --- seasons ------------------------------------------------------------------


def test_a_band_can_be_limited_to_a_season() -> None:
    # Real tariffs are seasonal before they are time-of-use. The reference
    # installation is time-of-use May to October and flat November to April, so
    # a model that knows only hours prices a January evening at a summer peak.
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from arraysense.tariff import parse_bands

    tz = ZoneInfo("America/Chicago")
    (summer, winter) = parse_bands(
        "On-peak | 0.21 | 15:00-20:00 | May-Oct; Winter | 0.12 | 00:00-24:00 | Nov-Apr"
    )
    # 16:00, inside the 15:00-20:00 window, so only the season differs.
    assert summer.applies_at(datetime(2026, 7, 16, 16, 0, tzinfo=tz))
    assert not summer.applies_at(datetime(2026, 1, 16, 16, 0, tzinfo=tz))
    assert winter.applies_at(datetime(2026, 1, 16, 16, 0, tzinfo=tz))
    assert not winter.applies_at(datetime(2026, 7, 16, 16, 0, tzinfo=tz))
    # And the summer band still respects its hours within its own season.
    assert not summer.applies_at(datetime(2026, 7, 16, 9, 0, tzinfo=tz))


def test_a_season_may_wrap_the_year_end() -> None:
    # November to April is the ordinary shape of a winter season, and a naive
    # range would produce an empty set for it.
    from arraysense.tariff import parse_bands

    (band,) = parse_bands("Winter | 0.12 | 00:00-24:00 | Nov-Apr")
    assert band.months == frozenset({11, 12, 1, 2, 3, 4})


def test_a_band_with_no_season_applies_all_year() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from arraysense.tariff import parse_bands

    tz = ZoneInfo("America/Chicago")
    (band,) = parse_bands("Flat | 0.12 | 00:00-24:00")
    assert band.months is None
    assert all(band.applies_at(datetime(2026, m, 15, tzinfo=tz)) for m in range(1, 13))


def test_months_can_be_listed_as_well_as_ranged() -> None:
    from arraysense.tariff import parse_bands

    (band,) = parse_bands("Odd | 0.12 | 00:00-24:00 | Jan, Mar, December")
    assert band.months == frozenset({1, 3, 12})


def test_a_month_that_is_not_a_month_is_refused() -> None:
    # A half-understood tariff prices a bill wrongly and says nothing about it.
    import pytest

    from arraysense.tariff import parse_bands

    with pytest.raises(ValueError, match="smarch"):
        parse_bands("Bad | 0.12 | 00:00-24:00 | smarch")


def test_a_whole_day_band_is_not_written_as_an_empty_range() -> None:
    # A 24-hour band is stored with both ends at midnight, and spelling that
    # out literally gives "00:00-00:00" — which reads as no hours at all.
    (band,) = parse_bands("Flat | 0.12 | 00:00-24:00")
    assert band.hours[0].label == "all day"


def test_an_ordinary_window_keeps_its_times() -> None:
    (band,) = parse_bands("Peak | 0.21 | 15:00-20:00")
    assert band.hours[0].label == "15:00\N{EN DASH}20:00"


def test_a_seasonal_tariff_can_still_be_projected() -> None:
    # The bug this guards: the projection priced every band in the tariff
    # while the cost priced only the bands in season, so the out-of-season
    # band was permanently unmeasured, the total permanently absent, and the
    # reference installation could never once see an estimated bill.
    tariff = Tariff(
        bands=parse_bands(
            "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
            "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
            "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
        ),
        fixed_monthly=15.0,
    )
    tz = ZoneInfo("America/Chicago")
    energy = PeriodEnergy(
        start=datetime(2026, 8, 1, tzinfo=tz),
        end=datetime(2026, 8, 11, tzinfo=tz),
        grid_import_kwh={"On-peak": 10.0, "Off-peak": 100.0},
    )
    bill = estimate_bill(tariff, energy)
    assert bill is not None
    assert bill.energy_cost_so_far is not None
    assert bill.estimated_total is not None
    # Ten days of a thirty-one day month, so roughly three times the cost.
    measured = 10 * 0.210321 + 100 * 0.086709
    assert bill.projected_energy_cost == pytest.approx(measured * 3.1, rel=0.01)
    assert bill.season_changes is False


def test_a_month_that_changes_season_says_the_projection_cannot_hold() -> None:
    # October runs on the summer tariff and November on the winter one, so
    # scaling what October cost across the rest of the month is arithmetic on
    # two different tariffs. Projecting anyway is defensible; doing it without
    # saying so is not.
    tariff = Tariff(
        bands=parse_bands(
            "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
            "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
            "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
        ),
    )
    tz = ZoneInfo("America/Chicago")
    energy = PeriodEnergy(
        start=datetime(2026, 10, 1, tzinfo=tz),
        end=datetime(2026, 10, 20, tzinfo=tz),
        grid_import_kwh={"On-peak": 10.0, "Off-peak": 100.0},
    )
    october = estimate_bill(tariff, energy)
    assert october is not None
    assert october.season_changes is False

    # A period that genuinely reaches into November does straddle, and says so.
    across = PeriodEnergy(
        start=datetime(2026, 10, 25, tzinfo=tz),
        end=datetime(2026, 11, 2, tzinfo=tz),
        grid_import_kwh={"On-peak": 5.0, "Off-peak": 50.0, "Winter": 8.0},
    )
    straddling = estimate_bill(tariff, across)
    assert straddling is not None
    assert straddling.season_changes is True


def test_a_completed_month_can_be_priced_on_a_seasonal_tariff() -> None:
    # The exclusive end of a whole-month range used to pull in the next
    # month's bands, so October priced against a Winter band that had no
    # energy — and absent propagated to the whole month's cost.
    tariff = Tariff(
        bands=parse_bands(
            "On-peak | 0.210321 | 15:00-20:00 | May-Oct; "
            "Off-peak | 0.086709 | 00:00-24:00 | May-Oct; "
            "Winter | 0.123030 | 00:00-24:00 | Nov-Apr"
        ),
    )
    tz = ZoneInfo("America/Chicago")
    october = (datetime(2026, 10, 1, tzinfo=tz), datetime(2026, 11, 1, tzinfo=tz))
    assert [b.name for b in tariff.bands_in_effect(*october)] == ["On-peak", "Off-peak"]
    result = compute_cost(
        tariff,
        PeriodEnergy(
            start=october[0],
            end=october[1],
            grid_import_kwh={"On-peak": 30.0, "Off-peak": 300.0},
        ),
    )
    assert result is not None
    assert result.energy_cost == pytest.approx(30 * 0.210321 + 300 * 0.086709, abs=0.02)


# --- The monthly adjustment factors -----------------------------------------
#
# PCRF (power cost recovery) and SCRF (securitized charges recovery) are riders
# the supplier sets afresh every month and charges on every kilowatt-hour, on
# top of the band rate. PCRF is routinely negative. Because they change month to
# month, the month each pair applied to is part of the value: without it,
# re-pricing July in September prices July at September's factors and quietly
# restates a bill the owner has already paid.


def adjusted(text: str, **overrides: object) -> Tariff:
    return tariff(**({SETTING_ADJUSTMENTS: text} | overrides))


def test_a_month_of_factors_parses_including_a_negative_pcrf() -> None:
    (entry,) = parse_adjustments("2026-07 | -0.001230 | 0.004560")
    assert (entry.year, entry.month) == (2026, 7)
    assert entry.pcrf_per_kwh == -0.001230
    assert entry.scrf_per_kwh == 0.004560
    assert entry.per_kwh == pytest.approx(0.003330)


def test_the_example_in_the_adjustments_help_is_one_that_parses() -> None:
    # The help text is the format's only documentation, so an example the
    # parser would refuse is worse than no example.
    assert parse_adjustments(EXAMPLE_ADJUSTMENTS)
    assert EXAMPLE_ADJUSTMENTS in str(lookup_setting(SETTING_ADJUSTMENTS).help)


def test_a_factor_left_blank_is_unrecorded_rather_than_nought() -> None:
    # The supplier published PCRF and not SCRF. Reading the empty field as zero
    # would be a measurement nobody took.
    (entry,) = parse_adjustments("2026-07 | -0.001230 |")
    assert entry.pcrf_per_kwh == -0.001230
    assert entry.scrf_per_kwh is None
    assert entry.per_kwh is None


@pytest.mark.parametrize(
    "text",
    [
        "2026-07 | -0.001 | 0.004; 2026-07 | -0.002 | 0.004",
        "July 2026 | -0.001 | 0.004",
        "2026-13 | -0.001 | 0.004",
        "2026-07 | ever so slightly negative | 0.004",
        "2026-07 | -0.001",
    ],
)
def test_adjustments_that_cannot_be_read_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        parse_adjustments(text)


def test_the_recorded_month_is_charged_on_every_kilowatt_hour() -> None:
    result = compute_cost(
        adjusted("2026-01 | -0.002 | 0.005", **{SETTING_FIXED_MONTHLY: 0.0}),
        january(grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0}),
    )
    assert result is not None
    assert result.adjustment_status == "applied"
    assert result.pcrf_per_kwh == -0.002
    assert result.scrf_per_kwh == 0.005
    assert result.energy_cost == pytest.approx(56.0)
    # 300 kWh at the combined 0.003 rider.
    assert result.adjustment == pytest.approx(0.90)
    assert result.cost == pytest.approx(56.90)


def test_a_month_with_no_factors_recorded_cannot_be_priced_and_says_so() -> None:
    # Not zero. Zero is a measurement meaning "no adjustment this month", and
    # this project does not assert measurements nobody took.
    result = compute_cost(
        adjusted("2026-02 | -0.002 | 0.005", **{SETTING_FIXED_MONTHLY: 0.0}),
        january(grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0}),
    )
    assert result is not None
    assert result.adjustment_status == "unknown"
    assert result.adjustment is None
    assert result.pcrf_per_kwh is None
    # And the total goes with it. Spending an unknown rider as zero states a
    # bill the supplier will not send, confidently and with nothing to say a
    # figure was missing — the same error as rendering a missing reading as 0.
    assert result.cost is None
    # What was measured still comes back, so a page can show the energy and say
    # only the total is unavailable.
    assert result.energy_cost == pytest.approx(56.0)


def test_an_install_whose_supplier_has_no_riders_is_not_nagged_about_them() -> None:
    # Nothing entered at all is a different fact from a month gone unrecorded,
    # and a page that cannot tell them apart warns every row forever.
    result = compute_cost(
        tariff(**{SETTING_FIXED_MONTHLY: 0.0}),
        january(grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0}),
    )
    assert result is not None
    assert result.adjustment_status == "none"
    assert result.adjustment is None
    assert result.cost == pytest.approx(56.0)


def test_recosting_an_old_month_uses_that_months_factors_not_the_newest() -> None:
    # The whole reason the month is part of the value.
    loaded = adjusted(
        "2026-01 | -0.002 | 0.005\n2026-02 | 0.030 | 0.005",
        **{SETTING_FIXED_MONTHLY: 0.0},
    )
    january_result = compute_cost(loaded, january(grid_import_kwh={"Peak": 100.0, "Off-peak": 0.0}))
    february = compute_cost(
        loaded,
        PeriodEnergy(
            start=datetime(2026, 2, 1, tzinfo=EAST),
            end=datetime(2026, 2, 16, tzinfo=EAST),
            grid_import_kwh={"Peak": 100.0, "Off-peak": 0.0},
        ),
    )
    assert january_result is not None and february is not None
    assert january_result.adjustment == pytest.approx(0.30)
    assert february.adjustment == pytest.approx(3.50)


def test_the_rider_reaches_the_counterfactual_and_so_the_saving() -> None:
    # Without the system every kilowatt-hour of house load would have been
    # metered, and the rider is charged on metered kilowatt-hours. Leaving it
    # out of the counterfactual understates what the array saved.
    result = compute_cost(
        adjusted("2026-01 | -0.002 | 0.005", **{SETTING_FIXED_MONTHLY: 0.0}),
        january(
            grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0},
            load_kwh={"Peak": 150.0, "Off-peak": 260.0},
        ),
    )
    assert result is not None
    # 79.60 of band energy plus 410 kWh at 0.003.
    assert result.no_solar_cost == pytest.approx(80.83)
    assert result.savings == pytest.approx(80.83 - 56.90)


def test_two_months_with_different_factors_cannot_be_priced_as_one_period() -> None:
    # A PeriodEnergy carries no split of its kilowatt-hours across months, so
    # there is no honest way to charge January's rider to January's share.
    loaded = adjusted("2026-01 | -0.002 | 0.005\n2026-02 | 0.030 | 0.005")
    across = PeriodEnergy(
        start=datetime(2026, 1, 20, tzinfo=EAST),
        end=datetime(2026, 2, 10, tzinfo=EAST),
        grid_import_kwh={"Peak": 10.0, "Off-peak": 20.0},
    )
    result = compute_cost(loaded, across)
    assert result is not None
    assert result.adjustment_status == "unknown"
    assert result.adjustment is None


def test_two_months_the_supplier_left_unchanged_are_priced_as_one() -> None:
    loaded = adjusted("2026-01 | -0.002 | 0.005\n2026-02 | -0.002 | 0.005")
    across = PeriodEnergy(
        start=datetime(2026, 1, 20, tzinfo=EAST),
        end=datetime(2026, 2, 10, tzinfo=EAST),
        grid_import_kwh={"Peak": 10.0, "Off-peak": 20.0},
    )
    result = compute_cost(loaded, across)
    assert result is not None
    assert result.adjustment_status == "applied"
    assert result.adjustment == pytest.approx(30 * 0.003)


def test_adjustments_that_will_not_parse_are_unknown_rather_than_absent() -> None:
    # A stored value that no longer reads is not evidence that the supplier
    # charges no rider. The tariff still prices energy; the adjustment does not
    # silently become nothing.
    loaded = load_tariff(dict(REFERENCE) | {SETTING_ADJUSTMENTS: "kilowatts and rainbows"})
    assert loaded is not None
    assert loaded.adjustments_unreadable is True
    result = compute_cost(loaded, january(grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0}))
    assert result is not None
    assert result.adjustment_status == "unknown"


def test_the_estimated_bill_projects_the_rider_with_the_energy() -> None:
    loaded = adjusted("2026-01 | -0.002 | 0.005")
    bill = estimate_bill(loaded, january(grid_import_kwh={"Peak": 100.0, "Off-peak": 200.0}))
    assert bill is not None
    assert bill.adjustment_status == "applied"
    # 300 kWh at 0.003 over fifteen days, scaled to a thirty-one day month.
    assert bill.projected_adjustment == pytest.approx(0.90 * 31 / 15, abs=0.01)
    assert bill.estimated_total == pytest.approx(
        (bill.projected_energy_cost or 0.0) + 25.0 + (bill.projected_adjustment or 0.0), abs=0.02
    )


def test_the_adjustment_setting_needs_no_change_to_the_settings_page() -> None:
    # settings.html renders itself from describe(). A per-month table that
    # needed its own control would be a design that had outgrown the registry.
    spec = lookup_setting(SETTING_ADJUSTMENTS)
    assert spec.kind == "str"
    assert spec.multiline is True
    assert spec.check is parse_adjustments
    (entry,) = [row for row in describe() if row["key"] == SETTING_ADJUSTMENTS]
    assert entry["multiline"] is True
    assert entry["default"] == ""
