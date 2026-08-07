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
    PeriodEnergy,
    Tariff,
    compute_cost,
    estimate_bill,
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


def test_a_band_measured_once_and_missed_once_is_unknown_not_partial() -> None:
    # Off-peak runs before the peak window and again after it. Measuring the
    # first stretch and not the second must not leave the first standing as the
    # band's total: that is a missing reading rendered as zero, in the one
    # place where it turns into money.
    start, end = _day(2026, 7, 1), _day(2026, 7, 2)
    energy = period_energy(STARK, _counter(start, (0, 8, 16)), start, end, TZ)
    assert energy.grid_import_kwh["Off"] is None


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
    assert compute_cost(COSERV, middle) is not None
    assert compute_cost(COSERV, middle).cost is None  # type: ignore[union-attr]


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


def test_a_peak_window_that_happened_and_went_unmeasured_still_reads_unknown() -> None:
    # The distinction that has to survive. Readings stop at two in the
    # afternoon and resume at nine, so the whole peak window happened with
    # nobody watching. Its energy is unknown, and a day priced without it would
    # read as cheap — which is a missing reading rendered as zero, in the one
    # place where it becomes money.
    start, end = _day(2026, 7, 15), _day(2026, 7, 16)
    rows = _rows(start, 14, 1.0) + _rows(_day(2026, 7, 15, 21), 3, 1.0)
    energy = period_energy(COSERV, rows, start, end, TZ)
    assert energy.grid_import_kwh["On-peak"] is None
    result = price_period(COSERV, energy)
    assert result is not None
    assert result.cost is None


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
