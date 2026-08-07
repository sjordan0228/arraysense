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

from datetime import timedelta

import pytest

from arraysense.store.tiers import select_tier

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
