"""Tests for band splitting: arraysense.costs."""

from __future__ import annotations

import time as clock
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from arraysense.costs import (
    BandInterval,
    band_intervals,
    bucket_energy,
    period_energy,
    price_period,
)
from arraysense.energy import bucket_edges
from arraysense.tariff import (
    BillEstimate,
    CostResult,
    PeriodEnergy,
    Tariff,
    compute_cost,
    estimate_bill,
    merge_shortfalls,
    parse_bands,
)

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


def test_no_readings_leave_every_band_unknown_rather_than_zero() -> None:
    # Each band the day passed through is named and left at None. Naming them
    # says more than an empty mapping does — these bands happened and nobody
    # measured them — and both read as unknown to everything downstream.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, [], start, end, TZ)
    assert set(energy.grid_import_kwh) == {"On-peak", "Off-peak"}
    assert all(v is None for v in energy.grid_import_kwh.values())


def test_the_real_tariff_prices_a_summer_day_correctly() -> None:

    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    result = compute_cost(COSERV, energy)
    assert result is not None
    # 5 kWh peak at 0.210321 plus 19 kWh off-peak at 0.086709.
    expected = 5 * 0.210321 + 19 * 0.086709
    assert result.energy_cost == pytest.approx(expected, abs=0.02)


def _rows_with_battery(start: datetime, hours: int) -> list[dict[str, object]]:
    """As ``_rows``, plus the bank's own discharge counter."""
    return [
        {
            "timestamp": start + timedelta(hours=h),
            "grid_import_energy_total_kwh": 1000.0 + h,
            "load_energy_total_kwh": 2000.0 + h * 3,
            "battery_discharge_energy_total_kwh": 500.0 + h * 2,
            "grid_export_energy_total_kwh": 5.0,
        }
        for h in range(hours + 1)
    ]


def test_the_bank_is_split_by_band_so_peak_hours_can_be_valued() -> None:
    # Nobody meters the bank, so this is priced by nothing — but it is what the
    # system carried through the expensive hours, and the band rate is the only
    # honest way to say what those hours would otherwise have cost.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows_with_battery(start, 24), start, end, TZ)
    assert energy.battery_discharge_kwh is not None
    assert energy.battery_discharge_kwh["On-peak"] == pytest.approx(10.0, abs=0.01)
    assert energy.battery_discharge_kwh["Off-peak"] == pytest.approx(38.0, abs=0.01)


def test_a_period_with_no_battery_counter_reports_none_rather_than_zeroes() -> None:
    # The bank went unreported all day. Every band must say so; not one may
    # come back as 0.0 kWh, which would read as a bank that sat idle.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    assert energy.battery_discharge_kwh is not None
    assert all(v is None for v in energy.battery_discharge_kwh.values())


def test_coverage_counts_the_minutes_the_collector_was_actually_running() -> None:
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    assert energy.elapsed_minutes == pytest.approx(1440.0)
    assert energy.measured_minutes == pytest.approx(1440.0)


def test_an_outage_shows_up_as_minutes_that_were_never_measured() -> None:
    # Half a day of readings and then nothing. The month is not quiet, it is
    # unobserved, and a page that cannot tell the difference will report the
    # second as the first.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 12, 1.0), start, end, TZ)
    assert energy.elapsed_minutes == pytest.approx(1440.0)
    assert energy.measured_minutes is not None
    assert energy.measured_minutes < 1000.0


def test_bands_are_wall_clock_in_the_owners_zone_not_utc() -> None:
    # The API hands these bounds in UTC, because that is what a query string
    # with a Z on it parses to. Attaching a zone to an already-aware datetime
    # changes nothing, so the peak window was matched against the UTC clock and
    # landed five hours early — 10:00 to 15:00 local instead of 15:00 to 20:00.
    # Every figure the Costs page showed was priced against the wrong hours.
    start = datetime(2026, 8, 5, 5, 0, tzinfo=UTC)
    end = datetime(2026, 8, 6, 5, 0, tzinfo=UTC)
    peak = next(i for i in band_intervals(COSERV, start, end, TZ) if i.band == "On-peak")
    assert peak.start.astimezone(TZ).hour == 15
    assert peak.end.astimezone(TZ).hour == 20


def test_the_period_is_reported_in_the_owners_zone() -> None:
    # Downstream, estimate_bill takes the month from these bounds and
    # bands_in_effect takes the season from them. Left in UTC, a month that
    # starts at local midnight looks like it reaches into the next one.
    start = datetime(2026, 10, 1, 5, 0, tzinfo=UTC)
    end = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    energy = period_energy(COSERV, [], start, end, TZ)
    assert energy.start.astimezone(TZ).month == 10
    assert energy.end.astimezone(TZ).day == 1
    assert [b.name for b in COSERV.bands_in_effect(energy.start, energy.end)] == [
        "On-peak",
        "Off-peak",
    ]


def test_a_clock_change_day_is_measured_in_real_minutes() -> None:
    # Subtracting two datetimes that share a tzinfo ignores the zone — Python
    # treats them as naive — so a 23-hour day counted 1440 minutes and a
    # 25-hour day counted 1440 too. The coverage figure the page draws from
    # this would then say the collector missed an hour it did not miss, and
    # claim full coverage of an hour it did.
    spring_start = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    spring_end = datetime(2026, 3, 9, 5, 0, tzinfo=UTC)
    spring = period_energy(COSERV, [], spring_start, spring_end, TZ)
    assert spring.elapsed_minutes == pytest.approx(23 * 60)

    autumn_start = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)
    autumn_end = datetime(2026, 11, 2, 6, 0, tzinfo=UTC)
    autumn = period_energy(COSERV, [], autumn_start, autumn_end, TZ)
    assert autumn.elapsed_minutes == pytest.approx(25 * 60)


def test_measured_minutes_never_exceed_the_minutes_that_elapsed() -> None:
    # The page divides one by the other and shows it as a percentage. Coverage
    # above 100% is not a rounding artefact, it is a unit error.
    start = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
    end = datetime(2026, 3, 9, 5, 0, tzinfo=UTC)
    energy = period_energy(COSERV, _rows(datetime(2026, 3, 8, tzinfo=TZ), 23, 1.0), start, end, TZ)
    assert energy.measured_minutes is not None
    assert energy.elapsed_minutes is not None
    assert energy.measured_minutes <= energy.elapsed_minutes


# A tariff whose peak costs ten times off-peak, so a kilowatt-hour landing in
# the wrong band is unmistakable in the total rather than a rounding argument.
STARK = Tariff(bands=parse_bands("Peak | 1.00 | 08:00-16:00; Off | 0.10 | 00:00-24:00"))


def _counter(start: datetime, hours: tuple[int, ...]) -> list[dict[str, object]]:
    """One lifetime import counter reading at each named hour, climbing by one."""
    return [
        {"timestamp": start + timedelta(hours=h), "grid_import_energy_total_kwh": 100.0 + h}
        for h in hours
    ]


def test_a_bucket_with_nothing_in_it_does_not_shift_the_rest_into_other_bands() -> None:
    # bucket_totals drops a bucket it has nothing to report for, so pairing
    # buckets with intervals by position slides every later bucket one band to
    # the left. Reproduced: seven off-peak kilowatt-hours billed at the peak
    # rate, and a total that priced confidently while a third of the period's
    # energy had gone missing from it.
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    energy = period_energy(STARK, _counter(start, (0, 8, 17, 24)), start, end, TZ)
    assert energy.grid_import_kwh.get("Peak") in (None, 8.0)
    total = sum(v for v in energy.grid_import_kwh.values() if v is not None)
    assert total <= 24.0


def test_a_band_measured_once_and_missed_once_is_partial_and_says_so() -> None:
    # Off-peak runs before the peak window and again after it. Measuring the
    # first stretch and not the second used to null the band — #23's decision
    # is the opposite: the measured stretch stands, and the shortfall entry is
    # what stops it reading as the band's whole total. Readings stop at 16:00,
    # so how much the evening held is not knowable, and the entry says that
    # rather than inventing an amount.
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    energy = period_energy(STARK, _counter(start, (0, 8, 16)), start, end, TZ)
    assert energy.grid_import_kwh["Off"] == pytest.approx(8.0)
    assert energy.shortfall is not None
    entry = energy.shortfall["grid_import"]
    assert entry.unknowable is True
    assert entry.unattributed_kwh == pytest.approx(0.0)


def test_coverage_reports_the_part_of_the_period_actually_observed() -> None:
    # Half a day of readings under a single all-day band claimed the whole day
    # was measured, because the band never changed and so the whole day was one
    # interval. The page renders this as "the collector recorded 100% of it".
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"))
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    energy = period_energy(flat, _counter(start, tuple(range(13))), start, end, TZ)
    assert energy.measured_minutes == pytest.approx(720, abs=5)


def test_the_repeated_autumn_hour_is_priced_as_two_hours() -> None:
    # Walking wall-clock minutes visits 01:00 to 01:59 once on the day the
    # clocks go back, so the second pass through the repeated hour is never
    # priced at all and a boundary inside it lands an hour early.
    tariff = Tariff(bands=parse_bands("Night | 0.05 | 00:00-01:30; Morning | 0.20 | 01:30-08:00"))
    start = datetime(2026, 11, 1, 5, 0, tzinfo=UTC)  # 00:00 local, CDT
    end = datetime(2026, 11, 1, 14, 0, tzinfo=UTC)  # 08:00 local, CST
    intervals = band_intervals(tariff, start, end, TZ)

    def band_at(moment: datetime) -> str | None:
        for i in intervals:
            if i.start.astimezone(UTC) <= moment < i.end.astimezone(UTC):
                return i.band
        return None

    # 01:15 happens twice: once on CDT and again an hour later on CST. Both
    # are before 01:30 on the clock, so both are Night.
    assert band_at(datetime(2026, 11, 1, 6, 15, tzinfo=UTC)) == "Night"
    assert band_at(datetime(2026, 11, 1, 7, 15, tzinfo=UTC)) == "Night"
    assert band_at(datetime(2026, 11, 1, 8, 15, tzinfo=UTC)) == "Morning"


def test_the_projection_scales_by_what_was_watched_not_by_what_was_asked_for() -> None:
    # Twelve hours of readings inside a day the caller requested in full. The
    # month must be projected from twelve hours of use, not from a day the
    # collector spent half of switched off — which halves the estimate and
    # tells the owner their bill will be far smaller than it will.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"), fixed_monthly=0.0)
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    energy = period_energy(flat, _counter(start, tuple(range(13))), start, end, TZ)
    bill = estimate_bill(flat, energy)
    assert bill is not None
    # 12 kWh over 12 hours, across a 744-hour month.
    assert bill.projected_energy_cost == pytest.approx(12 * 0.10 * (744 / 12), rel=0.02)


def test_a_month_the_counters_covered_start_to_finish_is_not_projected() -> None:
    # is_projected is a different question from fraction_elapsed reaching 1.0:
    # it has to come out false by itself once the counted span genuinely
    # reaches the whole month, which is the only case the bill card's "so
    # this is what it came to rather than a projection" is actually true.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"), fixed_monthly=0.0)
    start, end = _day(2026, 7, 1), _day(2026, 8, 1)
    energy = period_energy(flat, _counter(start, tuple(range(745))), start, end, TZ)
    bill = estimate_bill(flat, energy)
    assert bill is not None
    assert bill.is_projected is False
    assert bill.projected_energy_cost == pytest.approx(744 * 0.10, rel=1e-6)


def test_collection_beginning_mid_month_still_projects_a_finished_month() -> None:
    # The exact case named in the review: fraction_elapsed reads 1.0 the
    # instant the calendar month ends, whether or not the collector was
    # running for all of it. An installation whose collection began on the
    # fifteenth still has its bill scaled up on the thirty-first from half a
    # month of readings, and the bill card must not call that "what it came
    # to rather than a projection" on the strength of the calendar alone.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"), fixed_monthly=0.0)
    start, end = _day(2026, 7, 1), _day(2026, 8, 1)
    mid = _day(2026, 7, 15)
    hours = int((end - mid).total_seconds() // 3600)
    energy = period_energy(flat, _counter(mid, tuple(range(hours + 1))), start, end, TZ)
    bill = estimate_bill(flat, energy)
    assert bill is not None
    # Fully elapsed calendar-wise even though only about half was measured —
    # exactly where the old wording read the fraction and believed it.
    assert bill.fraction_elapsed == pytest.approx(1.0)
    assert bill.is_projected is True


def test_the_last_forty_three_minutes_of_a_month_still_projects() -> None:
    # renderMonth's own 0.999 display tolerance calls a month "over" within
    # three-quarters of an hour of its end — the right laxity for a label,
    # and the wrong one for a claim about what estimate_bill actually did:
    # 43 minutes of the month are still unmeasured, and the total is still
    # scaled up from what came before them.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"), fixed_monthly=0.0)
    start = _day(2026, 7, 1)
    near_end = _day(2026, 8, 1) - timedelta(minutes=43)
    hours = int((near_end - start).total_seconds() // 3600)
    energy = period_energy(flat, _counter(start, tuple(range(hours + 1))), start, near_end, TZ)
    bill = estimate_bill(flat, energy)
    assert bill is not None
    # Within renderMonth's own tolerance for calling the month done —
    # and still a projection, because the counters do not yet reach August.
    assert bill.fraction_elapsed > 0.999
    assert bill.is_projected is True


def test_a_gap_inside_a_band_does_not_inflate_the_projection() -> None:
    # Counters keep counting while nobody is watching — that is the whole
    # reason this reads them instead of integrating power — so a delta across
    # a four-hour outage still covers those four hours. Dividing that energy by
    # the two hours the collector was awake projects a month at three times the
    # real rate. The denominator has to be the span the counters account for,
    # which is a different question from how much of it was observed.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"), fixed_monthly=0.0)
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    energy = period_energy(flat, _counter(start, (0, 1, 5, 6)), start, end, TZ)
    assert energy.grid_import_kwh["Flat"] == pytest.approx(6.0)
    # Still honest about how much of the day anybody watched.
    assert energy.measured_minutes == pytest.approx(120)
    # But the six kilowatt-hours are spread across six hours, not two.
    assert energy.counted_minutes == pytest.approx(360)
    bill = estimate_bill(flat, energy)
    assert bill is not None
    assert bill.projected_energy_cost == pytest.approx(6 * 0.10 * (744 / 6), rel=0.02)


def test_a_failed_poll_is_not_evidence_that_anything_was_measured() -> None:
    # A row with no counter on it is a poll that came back empty. Counting its
    # timestamp made four failed polls a minute apart look like three minutes
    # of collection, and coverage is the figure the page uses to decide whether
    # to warn that a total is short.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"))
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    rows: list[dict[str, object]] = [
        {"timestamp": start + timedelta(minutes=m), "grid_import_energy_total_kwh": None}
        for m in range(4)
    ]
    energy = period_energy(flat, rows, start, end, TZ)
    assert energy.measured_minutes == 0.0
    assert energy.counted_minutes == 0.0


def test_a_reading_from_before_the_period_still_bounds_it() -> None:
    # The endpoint widens its query so the first interval has an earlier
    # reading to be measured from. Discarding that row outright loses the
    # stretch between the period's start and the first reading inside it.
    flat = Tariff(bands=parse_bands("Flat | 0.10 | 00:00-24:00"))
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    rows = _counter(start - timedelta(hours=1), (0, 2, 3))  # 23:00, 01:00, 02:00
    energy = period_energy(flat, rows, start, end, TZ)
    assert energy.counted_minutes == pytest.approx(120)


def test_an_unknown_export_credit_leaves_the_bill_absent_rather_than_larger() -> None:
    # A tariff that pays for export and an export figure nobody has is a bill
    # that cannot be told, not a bill with no credit on it. Netting off nothing
    # quotes a total higher than the one that arrives.
    paying = Tariff(
        bands=parse_bands("Flat | 0.10 | 00:00-24:00"),
        fixed_monthly=10.0,
        export_per_kwh=0.05,
    )
    energy = PeriodEnergy(
        start=_day(2026, 7, 1),
        end=_day(2026, 7, 11),
        grid_import_kwh={"Flat": 100.0},
        grid_export_kwh=None,
        counted_minutes=10 * 1440,
    )
    bill = estimate_bill(paying, energy)
    assert bill is not None
    assert bill.projected_energy_cost is not None
    assert bill.estimated_total is None

    # And with the export known, it is told.
    known = estimate_bill(paying, replace(energy, grid_export_kwh=40.0))
    assert known is not None
    assert known.estimated_total is not None


def _exporting_rows(
    start: datetime, hours: int, silent: frozenset[int] = frozenset()
) -> list[dict[str, object]]:
    """Hourly counters on which the export meter climbs as well, and may go quiet.

    ``silent`` names the hours whose row carries no export counter at all. The
    inverter answers a poll without answering every energy register, so one
    counter going absent while the others keep arriving is an ordinary reading
    and not an outage — and it is the case in which the period is fully
    measured in every respect except its export.
    """
    rows: list[dict[str, object]] = []
    for hour in range(hours + 1):
        row: dict[str, object] = {
            "timestamp": start + timedelta(hours=hour),
            "grid_import_energy_total_kwh": 1000.0 + hour,
            "load_energy_total_kwh": 2000.0 + hour * 2,
        }
        if hour not in silent:
            row["grid_export_energy_total_kwh"] = 300.0 + hour
        rows.append(row)
    return rows


def test_export_measured_across_part_of_a_period_is_the_part_with_the_rest_named() -> None:
    # Nothing at all recorded between two in the afternoon and nine at night.
    # The stretches either side exported 14 and 3 kilowatt-hours and the hole
    # held 7 — both its bracketing readings exist, so the amount is exact. The
    # old rule nulled the day; #23's decision is the part plus an accounting
    # of the rest, so the page can label rather than withhold.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    full = _exporting_rows(start, 24)
    holed = full[:15] + full[21:]
    energy = period_energy(COSERV, holed, start, end, TZ)
    assert energy.grid_export_kwh == pytest.approx(17.0)
    assert energy.shortfall is not None
    assert energy.shortfall["grid_export"].unattributed_kwh == pytest.approx(7.0)

    # And a day read end to end still reports the whole of it, unflagged.
    whole = period_energy(COSERV, full, start, end, TZ)
    assert whole.grid_export_kwh == pytest.approx(24.0)
    assert whole.shortfall is not None
    assert whole.shortfall["grid_export"].short is False


def test_an_export_counter_that_goes_quiet_alone_is_flagged_alone() -> None:
    # The harder shape, because everything else about the day is measured: the
    # import counter arrives all day and only the export register goes quiet
    # through the peak window. Minute coverage reads complete — this is the
    # shape that broke the second attempt at #23 — and the per-counter entry
    # is what still catches it: import clean, export short by a known amount.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    quiet = _exporting_rows(start, 24, frozenset(range(16, 21)))
    energy = period_energy(COSERV, quiet, start, end, TZ)
    assert energy.grid_import_kwh["On-peak"] == pytest.approx(5.0)
    assert energy.grid_import_kwh["Off-peak"] == pytest.approx(19.0)
    assert energy.grid_export_kwh == pytest.approx(18.0)
    assert energy.shortfall is not None
    assert energy.shortfall["grid_import"].short is False
    assert energy.shortfall["grid_export"].unattributed_kwh == pytest.approx(6.0)


def test_a_partially_measured_export_credits_the_part_and_flags_the_figure() -> None:
    # The tariff pays for export and five peak hours of the register went
    # unread — 6 kWh, known exactly from the readings either side. The credit
    # prices what was measured and says it is short; the projection restores
    # the missing kilowatt-hours at the flat export rate, which needs no
    # assumption at all. The old behaviour nulled the credit and the whole
    # estimated bill, which is the attempt-one shape the owner rejected.
    paying = replace(COSERV, export_per_kwh=0.05)
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    quiet = _exporting_rows(start, 24, frozenset(range(16, 21)))
    energy = period_energy(paying, quiet, start, end, TZ)

    result = price_period(paying, energy)
    assert result is not None
    assert result.cost is not None
    assert result.cost_is_short is False
    assert result.export_credit == pytest.approx(18.0 * 0.05)
    assert result.export_is_short is True

    bill = estimate_bill(paying, energy)
    assert bill is not None
    # One July day scaled to the month, with the quiet stretch's 6 kWh back
    # in: 24 kWh at five cents, thirty-one days.
    assert bill.projected_export_credit == pytest.approx(24.0 * 0.05 * 31, rel=0.01)
    assert bill.estimated_total is not None
    assert bill.is_short is True


def test_the_bucket_path_reports_the_same_partial_export() -> None:
    # The History page prices its days through the other entry point, and a
    # figure that is flagged on the Costs page and unqualified on the History
    # page is the disagreement this module exists to prevent.
    start, end = _day(2026, 7, 14), _day(2026, 7, 17)
    full = _exporting_rows(start, 72)
    # Hours 39 to 44 are the second day's peak window, and nothing was recorded
    # in them. The days either side are read end to end.
    days = bucket_energy(COSERV, full[:39] + full[45:], bucket_edges(start, end, "day", TZ), TZ)
    assert days[0].grid_export_kwh == pytest.approx(24.0)
    assert days[0].shortfall is not None
    assert days[0].shortfall["grid_export"].short is False
    assert days[1].grid_export_kwh == pytest.approx(24.0 - 7.0)
    assert days[1].shortfall is not None
    assert days[1].shortfall["grid_export"].unattributed_kwh == pytest.approx(7.0)
    assert days[2].grid_export_kwh == pytest.approx(24.0)


# --- the band scan itself -----------------------------------------------------


def _asked_minute_by_minute(
    tariff: Tariff, start: datetime, end: datetime, zone: ZoneInfo
) -> list[tuple[datetime, str | None]]:
    """The band in force at every real minute, asked one minute at a time.

    Stepped through UTC so the repeated autumn hour is visited twice and the
    spring hour that never happened is not visited at all.
    """
    out: list[tuple[datetime, str | None]] = []
    instant = start.astimezone(UTC)
    finish = end.astimezone(UTC)
    while instant < finish:
        band = tariff.band_at(instant.astimezone(zone))
        out.append((instant, None if band is None else band.name))
        instant += timedelta(minutes=1)
    return out


def _read_from(intervals: list[BandInterval], moment: datetime) -> str | None:
    """The band the returned intervals put this instant in."""
    for interval in intervals:
        if interval.start.astimezone(UTC) <= moment < interval.end.astimezone(UTC):
            return interval.band
    raise AssertionError(f"{moment} falls in no interval at all")


@pytest.mark.parametrize(
    ("first", "last"),
    [
        # An ordinary summer day, a winter day with no peak, the day the season
        # turns, and both days a year the clocks move.
        (_day(2026, 7, 15), _day(2026, 7, 16)),
        (_day(2027, 1, 15), _day(2027, 1, 16)),
        (_day(2026, 10, 31), _day(2026, 11, 2)),
        (datetime(2026, 3, 8, 6, 0, tzinfo=UTC), datetime(2026, 3, 9, 6, 0, tzinfo=UTC)),
        (datetime(2026, 11, 1, 5, 0, tzinfo=UTC), datetime(2026, 11, 2, 6, 0, tzinfo=UTC)),
    ],
)
def test_the_intervals_agree_with_asking_every_minute(first: datetime, last: datetime) -> None:
    # The scan no longer walks a minute at a time — it jumps to the clock times
    # a band can turn on, which is what makes thirteen months affordable — so
    # the thing that has to keep holding is that it says the same about every
    # minute as asking the tariff directly does. Without this the speed-up
    # could skip a boundary and nothing else in the suite would notice.
    intervals = band_intervals(COSERV, first, last, TZ)
    for moment, expected in _asked_minute_by_minute(COSERV, first, last, TZ):
        assert _read_from(intervals, moment) == expected, moment


def test_no_interval_is_empty_across_a_midnight_clock_change() -> None:
    # Santiago moves its clocks at midnight, so local midnight is a time that
    # never happened and the zone resolves it to the instant before the change.
    # Comparing the period's own start against a candidate boundary as wall
    # clock — which is what Python does to two datetimes sharing a tzinfo —
    # put a boundary exactly on the start and handed the caller an interval of
    # zero length to divide by.
    santiago = ZoneInfo("America/Santiago")
    tariff = Tariff(bands=parse_bands("Late | 0.2 | 23:00-01:00; Rest | 0.1 | 01:00-23:00"))
    start = datetime(2026, 9, 6, tzinfo=santiago)
    intervals = band_intervals(tariff, start, datetime(2026, 9, 7, tzinfo=santiago), santiago)
    assert all(i.end.astimezone(UTC) > i.start.astimezone(UTC) for i in intervals)


# --- many buckets at once -----------------------------------------------------


def test_each_bucket_is_split_exactly_as_it_would_be_on_its_own() -> None:
    # The History page prices thirty days and thirteen months at once, and the
    # only reason to do that in one pass rather than thirty is speed. A pass
    # that answers something different from the single-period path would put a
    # different number on the History page than on the Costs page for the same
    # day, which is the disagreement this whole module exists to prevent.
    start, end = _day(2026, 7, 14), _day(2026, 7, 17)
    rows = _rows(start, 72, 1.0)
    edges = bucket_edges(start, end, "day", TZ)
    together = bucket_energy(COSERV, rows, edges, TZ)
    assert len(together) == 3
    for index, split in enumerate(together):
        alone = period_energy(COSERV, rows, edges[index], edges[index + 1], TZ)
        assert dict(split.grid_import_kwh) == dict(alone.grid_import_kwh), index
        assert dict(split.load_kwh or {}) == dict(alone.load_kwh or {}), index


def test_a_bucket_nobody_measured_is_absent_rather_than_free() -> None:
    # The collector was down for the middle day. Its bands have to come back
    # unknown so the cost does too — a day priced at nothing is a day the owner
    # reads as a day they used nothing.
    start, end = _day(2026, 7, 14), _day(2026, 7, 17)
    rows = _rows(start, 24, 1.0) + _rows(_day(2026, 7, 16), 24, 1.0)
    edges = bucket_edges(start, end, "day", TZ)
    middle = bucket_energy(COSERV, rows, edges, TZ)[1]
    result = compute_cost(COSERV, middle)
    assert result is not None
    assert result.cost is None


def test_the_months_priced_together_add_up_to_the_range_priced_whole() -> None:
    # Thirteen monthly buckets is far past the seventy days one costed period
    # may cover, so this is the path that has to work at that length. Summing
    # them must land on what the same energy costs when it is split once over
    # the whole span, or the History page's column of months would not agree
    # with any other view of the same electricity.
    start = _day(2026, 6, 1)
    end = _day(2026, 8, 1)
    rows = _rows(start, 24 * 61, 0.5)
    edges = bucket_edges(start, end, "month", TZ)
    by_month = bucket_energy(COSERV, rows, edges, TZ)
    assert len(by_month) == 2

    summed: dict[str, float] = {}
    for split in by_month:
        for name, kwh in split.grid_import_kwh.items():
            assert kwh is not None, name
            summed[name] = summed.get(name, 0.0) + kwh
    whole = period_energy(COSERV, rows, start, end, TZ)
    for name, kwh in whole.grid_import_kwh.items():
        assert summed[name] == pytest.approx(kwh, abs=0.2), name


def test_thirteen_months_of_buckets_do_not_take_a_minute_each() -> None:
    # Priced by walking every minute this is well over half a million steps and
    # blocks the event loop the collector polls on. The scan is proportional to
    # the days in the range instead, so this is milliseconds; the bound is
    # loose enough not to fail on a slow machine and tight enough to catch a
    # return to the minute walk, which measured 100x this.
    start, end = _day(2025, 8, 1), _day(2026, 9, 1)
    edges = bucket_edges(start, end, "month", TZ)
    began = clock.perf_counter()
    priced = bucket_energy(COSERV, _rows(start, 24, 1.0), edges, TZ)
    assert len(priced) == 13
    assert clock.perf_counter() - began < 1.0


# --- pricing a period against the bands it entered ----------------------------


def test_a_morning_is_priced_before_its_peak_window_arrives() -> None:
    # Asked at nine in the morning, the day has not reached its peak window, so
    # the peak band has no interval and no reading. That is not an unmeasured
    # band, it is one that has not happened, and treating the two alike made
    # the day's cost a dash for the first fifteen hours of every day.
    start, end = _day(2026, 7, 15), _day(2026, 7, 15, 9)
    energy = period_energy(COSERV, _rows(start, 9, 1.0), start, end, TZ)
    assert "On-peak" not in energy.grid_import_kwh
    result = price_period(COSERV, energy)
    assert result is not None
    assert result.cost is not None
    assert result.energy_cost == pytest.approx(9 * 0.086709, abs=0.01)


def test_a_peak_window_that_happened_and_went_unmeasured_prices_partial_and_flagged() -> None:
    # The distinction that has to survive, in its new form. Readings stop at
    # two in the afternoon and resume at nine, so the whole peak window
    # happened with nobody watching. The band itself stays None — nothing of
    # it was measured, and a number there would be a missing reading rendered
    # small. But the day now prices what was measured, and the flag plus the
    # counted delta are what keep the partial from reading as a cheap day.
    # (The second _rows helper restarts its counters at 1000, so the counter
    # steps backwards across the gap and even the gap's amount is unknowable.)
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    rows = _rows(start, 14, 1.0) + _rows(_day(2026, 7, 15, 21), 3, 1.0)
    energy = period_energy(COSERV, rows, start, end, TZ)
    assert energy.grid_import_kwh["On-peak"] is None
    assert energy.shortfall is not None
    assert energy.shortfall["grid_import"].short is True
    result = price_period(COSERV, energy)
    assert result is not None
    assert result.cost is not None
    assert result.cost_is_short is True
    assert result.energy_cost == pytest.approx((14.0 + 3.0) * 0.086709, abs=0.01)


def test_a_period_that_entered_no_band_at_all_is_still_not_free() -> None:
    # A tariff whose only band covers the afternoon, asked about the morning.
    # Nothing was entered, so there is nothing to narrow to — and narrowing to
    # an empty band list would price nothing, total zero and call it a bill.
    sparse = Tariff(bands=parse_bands("Peak | 0.30 | 15:00-20:00"))
    start, end = _day(2026, 7, 15), _day(2026, 7, 15, 9)
    energy = period_energy(sparse, _rows(start, 9, 1.0), start, end, TZ)
    assert dict(energy.grid_import_kwh) == {}
    result = price_period(sparse, energy)
    assert result is not None
    assert result.cost is None


def test_without_a_tariff_there_is_no_priced_period_at_all() -> None:
    # Not zero, and not an empty breakdown either.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    assert price_period(None, period_energy(COSERV, [], start, end, TZ)) is None


# --- the shortfall: issue #23's verification cases ------------------------------
#
# Consumption is identical in every case — only the recording differs — so the
# clean case's figures are the truth the others are checked against. Import
# climbs faster through the peak window so a misplaced kilowatt-hour is visible
# in the total, and load climbs faster than import so the savings side is real.

START_23 = _day(2026, 7, 1)
END_23 = _day(2026, 7, 5)


def _case_rows(
    drop_all: frozenset[tuple[int, int]] = frozenset(),
    drop_import: frozenset[tuple[int, int]] = frozenset(),
) -> list[dict[str, object]]:
    """Hourly counters from two hours before the period to its end.

    ``drop_all`` removes whole rows (a poll gap, the shape yield mode makes);
    ``drop_import`` removes only the import counter (the register going quiet
    while polls continue, the shape that broke the second attempt). Keys are
    (day, hour) local.
    """
    rows: list[dict[str, object]] = []
    when = START_23 - timedelta(hours=2)
    imported, load = 1000.0, 4000.0
    while when <= END_23:
        local = when.astimezone(TZ)
        peak = 15 <= local.hour < 20
        imported += 2.5 if peak else 0.8
        load += 4.0 if peak else 1.5
        key = (local.day, local.hour)
        if key not in drop_all:
            row: dict[str, object] = {
                "timestamp": when,
                "load_energy_total_kwh": round(load, 1),
                "grid_export_energy_total_kwh": 5.0,
            }
            if key not in drop_import:
                row["grid_import_energy_total_kwh"] = round(imported, 1)
            rows.append(row)
        when += timedelta(hours=1)
    return rows


def _priced(
    rows: list[dict[str, object]],
) -> tuple[PeriodEnergy, CostResult | None, BillEstimate | None]:
    energy = period_energy(COSERV, rows, START_23, END_23, TZ)
    return energy, price_period(COSERV, energy, fixed_charge=15.0), estimate_bill(COSERV, energy)


def test_case_one_a_clean_period_is_unchanged_and_unflagged() -> None:
    energy, cost, bill = _priced(_case_rows())
    assert energy.grid_import_kwh["Off-peak"] == pytest.approx(67.6)
    assert energy.grid_import_kwh["On-peak"] == pytest.approx(43.2)
    assert energy.shortfall is not None
    # The battery counter is deliberately absent from these rows, so its own
    # entry is honestly unknowable; the money counters are the clean claim.
    assert all(energy.shortfall[k].short is False for k in ("grid_import", "load", "grid_export"))
    assert cost is not None and bill is not None
    assert cost.energy_cost == pytest.approx(14.95, abs=0.01)
    assert cost.savings == pytest.approx(10.53, abs=0.01)
    assert cost.cost_is_short is False
    assert bill.estimated_total == pytest.approx(130.84, abs=0.05)
    assert bill.is_short is False


def test_case_two_a_gap_across_a_band_edge_is_quantified_and_flagged() -> None:
    # Five hours of polls lost on 2 July, 17:00 to 22:00, across the 20:00
    # edge. The counters bracket the hole: 7.4 kWh of import and 12.5 of load
    # crossed it, so both figures show the measured part and say what is
    # missing. Attributed plus unattributed is the clean total — nothing
    # vanishes, it just stops being silent.
    energy, cost, bill = _priced(_case_rows(drop_all=frozenset((2, h) for h in (18, 19, 20, 21))))
    assert energy.shortfall is not None
    imported = energy.shortfall["grid_import"]
    assert imported.unattributed_kwh == pytest.approx(7.4)
    assert imported.unknowable is False
    assert imported.attributed_kwh + imported.unattributed_kwh == pytest.approx(110.8)
    assert energy.shortfall["load"].unattributed_kwh == pytest.approx(12.5)
    assert cost is not None and bill is not None
    # The figures themselves are what the silent version showed — the change
    # is that they are flagged now.
    assert cost.energy_cost == pytest.approx(13.59, abs=0.01)
    assert cost.cost_is_short is True
    # Savings is where the overstatement hid in the reverted attempt, so its
    # value is stated, not just its flag: both sides lost the same gap here,
    # and the partial counterfactual minus the partial cost is 9.63 against a
    # clean 10.53.
    assert cost.savings == pytest.approx(9.63, abs=0.01)
    assert cost.savings_is_short is True
    # The projection restores the missing 7.4 kWh at the blended rate of the
    # 103.4 it priced: 13.59 x 7.75 x (110.8 / 103.4). The truth is 115.84 and
    # the silent figure was 105.31 — the residual 2.6% is the stated
    # assumption itself, since the missing energy was peak-heavier than the
    # blend it was priced at.
    assert bill.is_short is True
    assert bill.assumed_kwh == pytest.approx(7.4)
    assert bill.projected_energy_cost is not None
    assert bill.projected_energy_cost == pytest.approx(112.85, abs=0.05)
    assert bill.projected_energy_cost > 105.31
    assert bill.estimated_total == pytest.approx(127.85, abs=0.05)


def test_case_three_the_same_gap_inside_one_band_raises_nothing() -> None:
    # The identical five-hour hole, 02:00 to 07:00, wholly inside the
    # off-peak stretch. The counter delta spans it exactly, so every figure is
    # byte-identical to the clean case and no flag or label may appear — a
    # caption here was finding 2 of the reverted attempt.
    energy, cost, bill = _priced(_case_rows(drop_all=frozenset((2, h) for h in (3, 4, 5, 6))))
    clean_energy, clean_cost, clean_bill = _priced(_case_rows())
    assert energy.shortfall is not None
    assert all(energy.shortfall[k].short is False for k in ("grid_import", "load", "grid_export"))
    assert dict(energy.grid_import_kwh) == dict(clean_energy.grid_import_kwh)
    assert cost is not None and clean_cost is not None
    assert cost.cost == clean_cost.cost
    assert cost.savings == clean_cost.savings
    assert cost.cost_is_short is False
    assert bill is not None and clean_bill is not None
    assert bill.estimated_total == clean_bill.estimated_total
    assert bill.is_short is False


def test_case_three_holds_when_the_band_runs_through_midnight() -> None:
    # STARK's off band runs 16:00 to 08:00 straight through midnight, so a
    # five-hour hole across midnight crosses a calendar day and no band edge.
    # period_energy cuts at band changes only: nothing drops, nothing flags.
    start, end = _day(2026, 7, 1), _day(2026, 7, 3)
    hours = tuple(h for h in range(49) if not 22 <= h <= 26)
    energy = period_energy(STARK, _counter(start, hours), start, end, TZ)
    assert energy.shortfall is not None
    assert energy.shortfall["grid_import"].short is False
    assert sum(v for v in energy.grid_import_kwh.values() if v is not None) == pytest.approx(48.0)


def test_case_four_a_quiet_counter_is_flagged_though_the_clock_reads_covered() -> None:
    # The import register alone says nothing for twenty-one hours while polls
    # keep arriving — the exact shape that broke attempt two, whose coverage
    # read 100% while the savings overstated by 14%. The minutes still read
    # covered; the energy accounting is what catches it.
    energy, cost, bill = _priced(_case_rows(drop_import=frozenset((2, h) for h in range(3, 24))))
    assert energy.measured_minutes == pytest.approx(energy.elapsed_minutes)
    assert energy.shortfall is not None
    imported = energy.shortfall["grid_import"]
    assert imported.unattributed_kwh == pytest.approx(26.1)
    assert imported.attributed_kwh + imported.unattributed_kwh == pytest.approx(110.8)
    assert energy.shortfall["load"].short is False
    # The band the quiet stretch swallowed whole keeps its measured days —
    # each day's peak interval attributes 10.8 kWh (four peak increments and
    # the reading on the 20:00 edge closing it).
    assert energy.grid_import_kwh["On-peak"] == pytest.approx(43.2 - 10.8)
    assert cost is not None and bill is not None
    assert cost.energy_cost == pytest.approx(11.35, abs=0.01)
    assert cost.cost_is_short is True
    # Savings shows and is flagged: 14.13 against a true 10.53 — overstated,
    # which is exactly why the label exists, and the page words the direction
    # from the import-side entry.
    assert cost.savings == pytest.approx(14.13, abs=0.01)
    assert cost.savings_is_short is True
    # The projection restores the 26.1 kWh at the blended measured rate:
    # 11.35 x 7.75 x (110.8 / 84.7) = 115.06, against a truth of 115.84.
    assert bill.is_short is True
    assert bill.assumed_kwh == pytest.approx(26.1)
    assert bill.projected_energy_cost == pytest.approx(115.06, abs=0.05)
    assert bill.estimated_total == pytest.approx(130.06, abs=0.05)


def test_case_five_an_emptied_band_inside_the_tolerance_is_unknowable() -> None:
    # Codex's reproduction: a one-hour band, and the only readings around it
    # exactly MAX_EDGE_GAP apart. No pair is dropped — the tolerance attributes
    # the whole delta to the neighbouring band — so the kilowatt-hours conserve
    # while the money is wrong. The band that occurred and reported nothing
    # against an otherwise clean entry is the tell.
    spiky = Tariff(bands=parse_bands("Spike | 1.00 | 16:00-17:00; Rest | 0.10 | 17:00-16:00"))
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    rows = [
        {
            "timestamp": start + timedelta(hours=h, minutes=30 if h in (15, 17) else 0),
            "grid_import_energy_total_kwh": 100.0 + h,
            "grid_export_energy_total_kwh": 300.0 + h,
        }
        for h in range(25)
        if h not in (16,)
    ]
    energy = period_energy(spiky, rows, start, end, TZ)
    assert energy.grid_import_kwh["Spike"] is None
    assert energy.shortfall is not None
    entry = energy.shortfall["grid_import"]
    assert entry.unattributed_kwh == pytest.approx(0.0)
    assert entry.unknowable is True
    # Export rides the same readings through the same emptied interval and is
    # NOT flagged: it takes no band split, so a flat rate prices its total and
    # the total is exact — an interval-level emptiness means nothing for it.
    assert energy.grid_export_kwh == pytest.approx(24.0)
    assert energy.shortfall["grid_export"].short is False
    result = price_period(spiky, energy)
    assert result is not None
    assert result.cost is not None
    assert result.cost_is_short is True


def test_a_flat_counter_across_an_emptied_band_prices_exact_and_unflagged() -> None:
    # The import counter sits still from 15:00 to 18:00 straight across the
    # one-hour Spike band. Monotonicity proves the band used exactly nothing,
    # so the day's cost is exact — the zero-delta span is the proof, and it
    # must read as coverage, never as a shortfall.
    spiky = Tariff(bands=parse_bands("Spike | 1.00 | 16:00-17:00; Rest | 0.10 | 17:00-16:00"))
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    # No readings at 16:00 or 17:00: the flat stretch is one three-hour pair
    # crossing both of the band's edges, which is a dropped span of zero.
    rows = [
        {
            "timestamp": start + timedelta(hours=h),
            "grid_import_energy_total_kwh": 100.0 + min(h, 15) + max(h - 18, 0),
        }
        for h in range(25)
        if h not in (16, 17)
    ]
    energy = period_energy(spiky, rows, start, end, TZ)
    assert energy.grid_import_kwh["Spike"] is None
    assert energy.shortfall is not None
    entry = energy.shortfall["grid_import"]
    assert entry.unattributed_kwh == pytest.approx(0.0)
    assert entry.unknowable is False
    result = price_period(spiky, energy)
    assert result is not None
    assert result.cost_is_short is False
    assert result.energy_cost == pytest.approx(21.0 * 0.10, abs=0.01)


def test_two_outages_sharing_a_reading_stay_quantified() -> None:
    # A lone reading between two gaps splits what would be one dropped pair
    # into two spans sharing that instant. Together they bracket everything
    # between their outer readings and their energies sum exactly — asking
    # whether one span alone covers the emptied band said no, and flagged a
    # shortfall that was in fact exact.
    tariff = Tariff(
        bands=parse_bands(
            "Night | 0.05 | 00:00-05:00; Mid | 0.30 | 05:00-06:00; Day | 0.10 | 06:00-24:00"
        )
    )
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    rows = [
        {"timestamp": start + timedelta(hours=h), "grid_import_energy_total_kwh": 100.0 + h}
        for h in (0, 1, 2, *range(9, 25))
    ]
    rows.append(
        {
            "timestamp": start + timedelta(hours=5, minutes=30),
            "grid_import_energy_total_kwh": 105.5,
        }
    )
    energy = period_energy(tariff, rows, start, end, TZ)
    assert energy.shortfall is not None
    entry = energy.shortfall["grid_import"]
    assert entry.unattributed_kwh == pytest.approx(7.0)
    assert entry.unknowable is False


def test_a_counter_that_dies_and_never_resumes_is_flagged_by_its_bounds() -> None:
    # Import answers its last poll at noon on the 2nd and never again, while
    # load carries on. No pair exists past the last answer, so no span does
    # either — the counter's own reach is the only evidence, and it must be
    # enough to keep three unmeasured days from pricing as cheap ones.
    quiet = frozenset((d, h) for d in (2, 3, 4, 5) for h in range(24) if (d, h) > (2, 12))
    energy, cost, _ = _priced(_case_rows(drop_import=quiet))
    assert energy.shortfall is not None
    imported = energy.shortfall["grid_import"]
    assert imported.unknowable is True
    assert energy.shortfall["load"].short is False
    assert cost is not None
    assert cost.cost_is_short is True


def test_an_outage_across_the_period_edge_is_unknowable_not_counted() -> None:
    # A gap from 23:00 on 30 June to 03:00 on 1 July straddles the period's
    # own start. The delta across it is known but includes June's energy, so
    # counting it would overstate July's shortfall — the entry says inexact
    # instead of naming a number that is wrong.
    rows = _case_rows(drop_all=frozenset({(1, 0), (1, 1), (1, 2)}))
    energy = period_energy(COSERV, rows, START_23, END_23, TZ)
    assert energy.shortfall is not None
    imported = energy.shortfall["grid_import"]
    assert imported.unknowable is True
    assert imported.unattributed_kwh == pytest.approx(0.0)


def test_the_bucket_path_flags_the_day_whose_money_is_short() -> None:
    # Reverted finding 4: a yield session from 17:00 to 22:00 sits wholly
    # inside 2 July, so the day's energy total is exact — the counters span
    # the hole — while its cost is short by the peak hours nobody can place.
    # The day's shortfall is what carries that to a badge.
    rows = _case_rows(drop_all=frozenset((2, h) for h in (18, 19, 20, 21)))
    edges = bucket_edges(START_23, END_23, "day", TZ)
    days = bucket_energy(COSERV, rows, edges, TZ)
    assert days[1].shortfall is not None
    assert days[1].shortfall["grid_import"].unattributed_kwh == pytest.approx(7.4)
    second = price_period(COSERV, days[1])
    first = price_period(COSERV, days[0])
    assert second is not None and first is not None
    assert second.cost_is_short is True
    assert first.cost_is_short is False


# --- which bands the unplaced energy could belong to (#31) ----------------------
#
# A band row is a figure, and #23's rule is that a figure whose window was partly
# unmeasured must say so. The period already knows: `shortfall.grid_import` reports
# the unattributed kilowatt-hours. What it does not say is *which band* they might
# have fallen in, so the Costs page could mark its totals row and its cards while
# leaving "On-peak 0.0 kWh / $0.00" standing unmarked beside them.
#
# Exact per-band attribution is not achievable and this does not attempt it: a
# dropped span's energy is known in total and unlocatable within the span. What is
# achievable is naming the bands it *could* belong to, which is enough to mark a row
# honestly and claims no number.


def _rows_with_hole(start: datetime, hours: int, hole: range) -> list[dict[str, object]]:
    """Counters climbing hourly, with the grid-import counter absent for a stretch.

    The reading still arrives; only that counter is missing from it, which is how a
    real gap in one register looks beside the others.
    """
    out: list[dict[str, object]] = []
    for h in range(hours + 1):
        row: dict[str, object] = {
            "timestamp": start + timedelta(hours=h),
            "load_energy_total_kwh": 2000.0 + h * 2,
            "grid_export_energy_total_kwh": 5.0,
        }
        if h not in hole:
            row["grid_import_energy_total_kwh"] = 1000.0 + h
        out.append(row)
    return out


def test_a_band_whose_window_was_partly_unmeasured_names_itself() -> None:
    # The hole covers 14:00-21:00 local, which swallows the whole 15:00-20:00 peak
    # window and part of the evening off-peak either side of it.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows_with_hole(start, 24, range(14, 22)), start, end, TZ)
    short = (energy.shortfall or {})["grid_import"]
    assert "On-peak" in short.bands_possibly_short


def test_a_band_measured_throughout_does_not_name_itself() -> None:
    # Same hole. The Winter band is out of season in July and never occurs, so it
    # cannot be short — and naming it would put a mark on a row that is not there.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows_with_hole(start, 24, range(14, 22)), start, end, TZ)
    short = (energy.shortfall or {})["grid_import"]
    assert "Winter" not in short.bands_possibly_short


def test_a_clean_period_names_no_band_at_all() -> None:
    # Nothing missing, so nothing may be marked. A marker on a complete period is
    # the same failure as a missing marker on a partial one, in the other direction.
    #
    # Only the counters the fixture actually feeds: it carries no battery counter,
    # so battery discharge has been `unknowable` here since long before bands were
    # named — see the test below, which pins that case on its own rather than
    # letting it ride along in a loop over every key.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    for name in ("grid_import", "load", "grid_export"):
        short = (energy.shortfall or {})[name]
        assert short.short is False, f"{name} is not clean; the fixture has changed"
        assert short.bands_possibly_short == frozenset()


def test_a_counter_that_never_reported_names_every_band() -> None:
    # A counter with no readings at all reached nothing, so every band's window is
    # unmeasured for it and every one is a candidate. This is not a new verdict —
    # the missing bounds have made it `unknowable` since #23 — only the same verdict
    # carried down to the rows, and marking fewer bands than that would be the page
    # claiming a window was measured when no reading exists anywhere in the period.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(COSERV, _rows(start, 24, 1.0), start, end, TZ)
    battery = (energy.shortfall or {})["battery_discharge"]
    assert battery.unknowable is True
    assert battery.bands_possibly_short == frozenset({"On-peak", "Off-peak"})


def test_the_emptiness_signal_alone_would_miss_this() -> None:
    # The trap recorded on the issue. A band that reported 0.0 rather than None is
    # not "empty", because the band has intervals on other days that did report and
    # the split hands it a zero. A fix built only on the empty-interval check passes
    # every other test here and still leaves the defect on the page, so this pins
    # the case directly: the peak window is wholly inside the hole, and the band
    # must be named even though its split value is a number rather than absent.
    start, end = _day(2026, 7, 14), _day(2026, 7, 17)
    hole = range(38, 46)  # 14:00-21:00 local on the middle day only
    energy = period_energy(COSERV, _rows_with_hole(start, 72, hole), start, end, TZ)
    short = (energy.shortfall or {})["grid_import"]
    peak = (energy.grid_import_kwh or {}).get("On-peak")
    assert peak is not None, "the fixture no longer reproduces the case: peak is absent, not zero"
    assert "On-peak" in short.bands_possibly_short


def test_the_flag_does_not_disturb_the_existing_shortfall_fields() -> None:
    # `short` and the two kilowatt-hour figures decide the totals row and the cards,
    # which #23 settled. This adds a field beside them and must not move them.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    rows = _rows_with_hole(start, 24, range(14, 22))
    energy = period_energy(COSERV, rows, start, end, TZ)
    short = (energy.shortfall or {})["grid_import"]
    assert short.unattributed_kwh > 0.0
    assert short.short is True


def test_export_names_no_band_because_it_has_no_band_split() -> None:
    # Export is priced at one flat rate, so an interval-level verdict means
    # nothing for it — which is why the emptiness check beside this already skips
    # it, having once flagged provably exact export credits. Naming bands for it
    # would qualify a figure the spans and bounds proved exact, and would hand the
    # page a set it could only draw wrongly.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    rows: list[dict[str, object]] = []
    for h in range(25):
        row: dict[str, object] = {
            "timestamp": start + timedelta(hours=h),
            "grid_import_energy_total_kwh": 1000.0 + h,
            "load_energy_total_kwh": 2000.0 + h * 2,
        }
        # The export counter alone is missing across the peak window's edges.
        if h not in range(15, 21):
            row["grid_export_energy_total_kwh"] = 5.0 + h * 0.5
        rows.append(row)
    energy = period_energy(COSERV, rows, start, end, TZ)
    export = (energy.shortfall or {})["grid_export"]
    assert export.unattributed_kwh > 0.0, "the fixture no longer produces an export shortfall"
    assert export.bands_possibly_short == frozenset()


def test_a_counter_that_dies_mid_period_names_the_bands_it_never_reached() -> None:
    # A counter that stops and never resumes leaves no dropped span behind, because
    # a span needs a reading on both sides of the hole and there is no reading after
    # this one. Its own reach is the only evidence, and the bounds check turns that
    # into `unknowable`. But `unknowable` is a period-level verdict: it marks the
    # totals row and says nothing about which band rows to mark, so a fix that
    # collects bands only from spans leaves every row on a three-day outage clean.
    start, end = _day(2026, 7, 14), _day(2026, 7, 17)
    rows = _rows_with_hole(start, 72, range(38, 73))
    energy = period_energy(COSERV, rows, start, end, TZ)
    short = (energy.shortfall or {})["grid_import"]
    assert short.unknowable is True, "the fixture no longer reproduces a bounds-only shortfall"
    assert "On-peak" in short.bands_possibly_short
    assert "Off-peak" in short.bands_possibly_short


def test_a_present_band_measured_throughout_is_not_named() -> None:
    # Discrimination between bands that all occur, which the Winter case above
    # cannot show: Winter is out of season in July and has no intervals at all, so
    # its absence proves only that a band with no window is not named. COSERV alone
    # cannot show it either — its peak is bracketed by off-peak on both sides, so
    # any span ambiguous enough to be dropped touches both bands by construction.
    #
    # Four bands across the day, and a hole from 16:00 to 21:00 local. It spans the
    # Peak/Evening boundary, so the energy could have fallen either side and both
    # are named; Night and Day are measured end to end and must stay clean.
    tariff = Tariff(
        bands=parse_bands(
            "Night | 0.05 | 00:00-06:00; Day | 0.10 | 06:00-15:00; "
            "Peak | 0.30 | 15:00-20:00; Evening | 0.15 | 20:00-24:00"
        ),
        fixed_monthly=15.0,
    )
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    energy = period_energy(tariff, _rows_with_hole(start, 24, range(17, 21)), start, end, TZ)
    short = (energy.shortfall or {})["grid_import"]
    assert short.short is True, "the fixture no longer produces a shortfall"
    assert short.bands_possibly_short == frozenset({"Peak", "Evening"})


def test_merging_buckets_keeps_the_bands_each_one_named() -> None:
    # The History footer prices thirty-one days as one period by merging the
    # buckets' accounting. A merge that drops the band names hands that footer an
    # empty set, so a month containing an outage prices its band rows as though
    # every window had been measured — the totals stay flagged and the rows do not.
    first = _day(2026, 7, 15)
    second = _day(2026, 7, 16)
    third = _day(2026, 7, 17)
    holed = period_energy(COSERV, _rows_with_hole(first, 24, range(14, 22)), first, second, TZ)
    clean = period_energy(COSERV, _rows(second, 24, 1.0), second, third, TZ)
    assert "On-peak" in (holed.shortfall or {})["grid_import"].bands_possibly_short
    merged = merge_shortfalls([holed, clean])
    assert merged is not None
    assert "On-peak" in merged["grid_import"].bands_possibly_short
