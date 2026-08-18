"""Tests for tier selection: arraysense.store.tiers.

Choosing a resolution tier is a pure function of how long a range is and how
wide the chart is. No database is involved.

The rule these tests pin down: return the tier whose point count sits closest to
the requested width, judged as a ratio rather than a difference. A chart cannot
draw more points than it has pixels, so massively oversampling is waste; but
returning far too few points loses detail the display could have shown. The
measured case that motivates the whole function is a month at a normal width,
where the minute tier is 43,200 points for a chart around a thousand pixels
wide — 107 ms of work to draw something indistinguishable from the hourly
tier's 720 points at roughly 2 ms.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arraysense.store.tiers import select_tier, select_tier_for_range

# The full-cadence tier's resolution is the polling interval, which the caller
# knows and the store does not.
CADENCE = 10


def test_a_few_hours_uses_full_cadence() -> None:
    # Six hours at 10s is 2,160 points; the minute tier would give 360. The
    # finer tier is closer to a 1,000px width and costs nothing at this size.
    assert select_tier(timedelta(hours=6), width_px=1000, cadence_seconds=CADENCE) == "full"


def test_a_day_uses_the_minute_tier() -> None:
    # 8,640 raw points against 1,440 minute points for a 1,000px chart. Minute
    # is the better fit; hourly's 24 points would throw away the day's shape.
    assert select_tier(timedelta(days=1), width_px=1000, cadence_seconds=CADENCE) == "minute"


def test_a_month_uses_hourly_not_minute() -> None:
    # The case the function exists for. Minute is 43,200 points for a 1,000px
    # chart — 43 points per pixel, none of them visible.
    assert select_tier(timedelta(days=30), width_px=1000, cadence_seconds=CADENCE) == "hourly"


def test_a_year_uses_hourly() -> None:
    assert select_tier(timedelta(days=365), width_px=1000, cadence_seconds=CADENCE) == "hourly"


def test_a_narrow_display_coarsens() -> None:
    # The same span on a smaller chart needs fewer points.
    wide = select_tier(timedelta(days=2), width_px=4000, cadence_seconds=CADENCE)
    narrow = select_tier(timedelta(days=2), width_px=200, cadence_seconds=CADENCE)
    order = ("full", "minute", "hourly")
    assert order.index(narrow) > order.index(wide), (wide, narrow)


def test_a_wide_display_justifies_a_finer_tier() -> None:
    # Same span, more pixels: never coarser than the narrow case.
    narrow = select_tier(timedelta(hours=12), width_px=400, cadence_seconds=CADENCE)
    wide = select_tier(timedelta(hours=12), width_px=4000, cadence_seconds=CADENCE)
    order = ("full", "minute", "hourly")
    assert order.index(wide) <= order.index(narrow), (narrow, wide)


def test_module_data_never_selects_the_minute_tier() -> None:
    # Module readings have only full-cadence and hourly tiers; state of charge
    # and cycle counts move too slowly to justify a minute tier.
    for days in (1, 7, 30, 365):
        tier = select_tier(
            timedelta(days=days), width_px=1000, cadence_seconds=CADENCE, module=True
        )
        assert tier in ("full", "hourly"), (days, tier)


def test_module_day_uses_full_cadence() -> None:
    # Without this, an implementation returning "hourly" for every module
    # request would pass the whole module suite. A day of module data is 8,640
    # raw points against hourly's 24; raw is by far the closer fit.
    assert (
        select_tier(timedelta(days=1), width_px=1000, cadence_seconds=CADENCE, module=True)
        == "full"
    )


def test_module_month_still_uses_hourly() -> None:
    assert (
        select_tier(timedelta(days=30), width_px=1000, cadence_seconds=CADENCE, module=True)
        == "hourly"
    )


def test_a_very_short_range_uses_full_cadence() -> None:
    assert select_tier(timedelta(minutes=5), width_px=1000, cadence_seconds=CADENCE) == "full"


def test_cadence_affects_the_choice() -> None:
    # A one-second poller has ten times the raw points of a ten-second one over
    # the same span, which is enough to move the choice a whole tier. Asserted
    # as explicit results rather than an ordering, because an implementation
    # that ignored cadence and always assumed ten seconds would satisfy an
    # inequality.
    assert select_tier(timedelta(hours=6), width_px=1000, cadence_seconds=1) == "minute"
    assert select_tier(timedelta(hours=6), width_px=1000, cadence_seconds=10) == "full"


def test_returns_a_real_tier_name() -> None:
    from arraysense.store.schema import INVERTER_TIERS, MODULE_TIERS

    inverter_names = {t.name for t in INVERTER_TIERS}
    module_names = {t.name for t in MODULE_TIERS}
    for days in (1, 30, 365):
        span = timedelta(days=days)
        assert select_tier(span, width_px=1000, cadence_seconds=CADENCE) in inverter_names
        assert (
            select_tier(span, width_px=1000, cadence_seconds=CADENCE, module=True) in module_names
        )


def test_zero_or_negative_width_is_rejected() -> None:
    # A caller asking for a chart with no pixels is a programming error.
    for bad in (0, -1):
        with pytest.raises(ValueError):
            select_tier(timedelta(days=1), width_px=bad, cadence_seconds=CADENCE)


def test_non_positive_span_is_rejected() -> None:
    for bad in (timedelta(0), timedelta(seconds=-60)):
        with pytest.raises(ValueError):
            select_tier(bad, width_px=1000, cadence_seconds=CADENCE)


def test_non_positive_cadence_is_rejected() -> None:
    # Unguarded, zero divides and a negative reaches log2, surfacing as a
    # confusing "math domain error" from deep inside the scoring.
    for bad in (0, -10):
        with pytest.raises(ValueError, match="cadence"):
            select_tier(timedelta(days=1), width_px=1000, cadence_seconds=bad)


def test_a_mathematical_tie_prefers_the_coarser_tier() -> None:
    # These arguments make the raw and hourly tiers exactly equidistant in
    # ratio terms. Compared with exact equality the two independently computed
    # logarithms differ by about one unit in the last place, and the finer tier
    # wins by accident.
    assert (
        select_tier(
            timedelta(seconds=360_360),
            width_px=1001,
            cadence_seconds=36,
            module=True,
        )
        == "hourly"
    )


# --- what a tier can actually answer ------------------------------------------
#
# Fit alone is blind to whether the tier it picks holds the range at all. The
# raw tier keeps thirty days and the importer only ever filled it that far back,
# so a short window from last year scores as "full" and comes back empty while
# the minute tier holds every minute of it. A window straddling that floor is
# worse: it comes back half-length with nothing saying the other half exists.
#
# The coverage a caller passes is what each tier really holds, read from the
# store. With none passed the choice is the pure fit it always was.

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_RAW_FLOOR = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
_MINUTE_FLOOR = datetime(2025, 8, 7, 12, 0, tzinfo=UTC)
_HOURLY_FLOOR = datetime(2024, 10, 10, 18, 0, tzinfo=UTC)

# The reference installation's own tier floors, from a copy of its database.
_REAL_COVERAGE: dict[str, tuple[datetime, datetime] | None] = {
    "full": (_RAW_FLOOR, _NOW),
    "minute": (_MINUTE_FLOOR, _NOW),
    "hourly": (_HOURLY_FLOOR, _NOW),
}


def test_a_short_window_older_than_raw_reads_the_tier_that_holds_it() -> None:
    # Six hours, sixty days ago. Fit says "full" at any sensible width; the raw
    # tier's floor is a month back, so "full" is an empty chart.
    start = _NOW - timedelta(days=60)
    choice = select_tier_for_range(
        start,
        start + timedelta(hours=6),
        width_px=4000,
        cadence_seconds=CADENCE,
        coverage=_REAL_COVERAGE,
    )
    assert choice.name == "minute"
    assert choice.bounded is True


def test_a_window_straddling_the_raw_floor_is_not_answered_half_length() -> None:
    # Three hours either side of the floor. The raw tier answers the later half
    # and says nothing about the earlier one, which is the truncation that looks
    # exactly like a chart of a quiet morning.
    choice = select_tier_for_range(
        _RAW_FLOOR - timedelta(hours=3),
        _RAW_FLOOR + timedelta(hours=3),
        width_px=4000,
        cadence_seconds=CADENCE,
        coverage=_REAL_COVERAGE,
    )
    assert choice.name == "minute"
    assert choice.bounded is True


def test_bounded_says_the_stored_bounds_enclose_the_range() -> None:
    # The flag is deliberately not named "complete": it is judged from a tier's
    # first and last rows, so a collector outage inside the range is invisible
    # to it. The name has to say what it measures — bounds-enclosure, not a
    # hole-free answer — so a caller cannot present a partial series as whole
    # on its strength alone.
    start = _NOW - timedelta(hours=6)
    coverage: dict[str, tuple[datetime, datetime] | None] = {
        "full": (start, _NOW),
        "minute": None,
        "hourly": None,
    }
    choice = select_tier_for_range(
        start, _NOW, width_px=4000, cadence_seconds=CADENCE, coverage=coverage
    )
    assert choice.bounded is True
    assert choice.available == (start, _NOW)


def test_a_recent_window_is_chosen_on_fit_exactly_as_before() -> None:
    # The common case must not move: every tier holds the last day, so the
    # choice is the fit it always was.
    start = _NOW - timedelta(days=1)
    with_coverage = select_tier_for_range(
        start, _NOW, width_px=1000, cadence_seconds=CADENCE, coverage=_REAL_COVERAGE
    )
    assert with_coverage.name == select_tier(
        timedelta(days=1), width_px=1000, cadence_seconds=CADENCE
    )
    assert with_coverage.bounded is True


def test_a_lagging_coarse_tier_does_not_read_as_covering_the_present() -> None:
    # The hourly tier's last bucket is the hour just gone, so a range ending now
    # is covered. One that stopped being rebuilt three days ago is not, and the
    # tier holding those three days should answer instead.
    coverage = dict(_REAL_COVERAGE)
    coverage["hourly"] = (_HOURLY_FLOOR, _NOW - timedelta(days=3))
    choice = select_tier_for_range(
        _NOW - timedelta(days=2),
        _NOW,
        width_px=200,
        cadence_seconds=CADENCE,
        coverage=coverage,
    )
    assert choice.name == "minute"


def test_a_range_no_tier_holds_says_so_rather_than_looking_empty() -> None:
    # Before the database begins. Something has to be returned — the caller has
    # a query to run — but the answer carries the fact that it is not the range
    # that was asked for.
    start = _HOURLY_FLOOR - timedelta(days=40)
    choice = select_tier_for_range(
        start,
        start + timedelta(days=1),
        width_px=1000,
        cadence_seconds=CADENCE,
        coverage=_REAL_COVERAGE,
    )
    assert choice.bounded is False
    assert choice.available is None


def test_a_partly_held_range_reports_the_part_that_is_held() -> None:
    # A day ending an hour after the last hourly bucket: bounded is False and
    # the part that exists is named, so a page can qualify what it draws instead
    # of presenting three-quarters of a day as a whole one.
    coverage: dict[str, tuple[datetime, datetime] | None] = {
        "full": None,
        "minute": None,
        "hourly": (_HOURLY_FLOOR, _NOW),
    }
    choice = select_tier_for_range(
        _NOW - timedelta(hours=12),
        _NOW + timedelta(hours=12),
        width_px=1000,
        cadence_seconds=CADENCE,
        coverage=coverage,
    )
    assert choice.name == "hourly"
    assert choice.bounded is False
    assert choice.available == (_NOW - timedelta(hours=12), _NOW)


def test_an_empty_tier_is_never_preferred_to_one_holding_the_range() -> None:
    # A fresh database whose rollups have not run: raw holds the day, the coarse
    # tiers hold nothing at all.
    coverage: dict[str, tuple[datetime, datetime] | None] = {
        "full": (_NOW - timedelta(days=2), _NOW),
        "minute": None,
        "hourly": None,
    }
    choice = select_tier_for_range(
        _NOW - timedelta(days=1),
        _NOW,
        width_px=1000,
        cadence_seconds=CADENCE,
        coverage=coverage,
    )
    assert choice.name == "full"
    assert choice.bounded is True


def test_module_ranges_are_scored_against_the_module_tiers() -> None:
    coverage: dict[str, tuple[datetime, datetime] | None] = {
        "full": (_NOW - timedelta(days=30), _NOW),
        "hourly": (_NOW - timedelta(days=400), _NOW),
    }
    choice = select_tier_for_range(
        _NOW - timedelta(days=200),
        _NOW - timedelta(days=199),
        width_px=1000,
        cadence_seconds=CADENCE,
        coverage=coverage,
        module=True,
    )
    assert choice.name == "hourly"
    assert choice.bounded is True


def test_circuit_tiers_pick_raw_for_a_day_and_hourly_for_a_week() -> None:
    # 24 h of 60 s readings is 1,440 points into a 1,200 px chart — a good fit.
    # 7 d of the same is 10,080, which is eight doublings past the width, while
    # the hourly tier gives 168. The fit rule is what decides; this pins the
    # two ranges the Graphs page actually offers on either side of the flip.
    day = select_tier(timedelta(hours=24), width_px=1200, cadence_seconds=60, circuit=True)
    week = select_tier(timedelta(days=7), width_px=1200, cadence_seconds=60, circuit=True)
    assert day == "full"
    assert week == "hourly"


def test_circuit_tiers_have_no_minute_tier() -> None:
    # The circuit tables are raw and hourly only. A selection that returned
    # "minute" would name a table that does not exist and fail at query time
    # rather than here, which is the wrong place to find out.
    for hours in (1, 6, 24, 24 * 7, 24 * 30):
        chosen = select_tier(
            timedelta(hours=hours), width_px=1200, cadence_seconds=60, circuit=True
        )
        assert chosen in {"full", "hourly"}


def test_circuit_and_module_are_not_both_askable() -> None:
    # Two tier families requested at once is a programming error, not a
    # preference to resolve quietly. Resolving it would silently score one
    # family's ranges against the other's tables.
    with pytest.raises(ValueError, match="module and circuit"):
        select_tier(
            timedelta(hours=6), width_px=1200, cadence_seconds=60, module=True, circuit=True
        )
