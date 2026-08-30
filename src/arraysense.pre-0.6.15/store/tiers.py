"""tiers.py — tier selection from a time range and a target pixel width.

Choosing a resolution tier is a pure function of how long a range is and how
wide the chart is: no database is involved. A chart cannot draw more points
than it has pixels, so massively oversampling is waste; returning far too few
points loses detail the display could have shown. The fit is judged as a
ratio rather than a difference — a tier giving twice the width is a better fit
than one giving a fortieth of it — and the full-cadence tier's resolution is
the polling interval, which the caller knows and the store does not.
"""

from __future__ import annotations

import math
from datetime import timedelta

from arraysense.store.schema import INVERTER_TIERS, MODULE_TIERS, Tier

# The coarse tiers' bucket periods, in seconds — the fixed periods the rollup
# writes (see store.rollup), not table names. The full tier's resolution is
# the polling interval and arrives as a parameter instead of living here.
_MINUTE_SECONDS = 60
_HOUR_SECONDS = 3600


def _resolution_seconds(tier: Tier, cadence_seconds: int) -> int:
    """Return the period one row of ``tier`` covers, in seconds.

    The full tier's resolution is the polling interval, which the caller knows
    and the store does not, so it arrives as a parameter rather than being
    recorded here. The coarse tiers are the fixed bucket periods the rollup
    writes, and this is the one place those periods are turned back into a
    number — scoring a tier means knowing how many rows it would produce.

    Raises:
        AssertionError: ``tier.name`` is not a tier this module knows a
            resolution for, so a tier added to schema without a resolution
            here fails loudly instead of being silently skipped.
    """
    if tier.name == "full":
        return cadence_seconds
    if tier.name == "minute":
        return _MINUTE_SECONDS
    if tier.name == "hourly":
        return _HOUR_SECONDS
    raise AssertionError(f"no resolution for tier {tier.name!r}")


def _fit_score(points: float, width_px: int) -> float:
    """Return how well ``points`` fits a ``width_px`` chart; smaller is better.

    ``points`` is the row count a tier would return over the range, fractional
    because it is a span divided by a bucket period. The fit is a point-to-pixel
    ratio judged multiplicatively — how many doublings it sits from one point per
    pixel — so a tier giving twice the width (one doubling) is a better fit than
    one giving a fortieth of it (about five halvings): the absolute gap is
    smaller in the second case, but the chart is proportionally emptier. Log
    distance makes over- and under-sampling symmetric, which is what pins a
    six-hour range to full cadence rather than the closer-in-ratio minute tier.
    """
    return abs(math.log2(points / width_px))


def select_tier(
    span: timedelta,
    width_px: int,
    cadence_seconds: int,
    module: bool = False,
) -> str:
    """Return the resolution tier whose point count best fits the target width.

    The choice is a fit, not a floor: pick the tier whose point count sits
    closest to the requested width, judged as a ratio rather than a
    difference. On an exact tie the coarser tier wins — it costs less to store
    and draw for the same visible outcome. Per-module data is scored against
    the module tiers, which have no minute tier, so a module chart lands on
    full cadence or hourly and never in between.

    The polling interval has to be supplied because it is the full tier's
    resolution and only the caller knows it. A width of no pixels, a range that
    ends before it starts, or a cadence of zero is a programming error rather
    than a bad request, and raises ValueError — unguarded, the last of those
    divides by zero or takes the log of a negative and surfaces as "math domain
    error" from somewhere far less obvious.
    """
    if width_px <= 0:
        raise ValueError(f"width_px must be positive, got {width_px}")
    if span <= timedelta(0):
        raise ValueError(f"span must be positive, got {span}")
    if cadence_seconds <= 0:
        # Unguarded this divides by zero, or takes the log of a negative and
        # surfaces as "math domain error" from deep inside the scoring.
        raise ValueError(f"cadence_seconds must be positive, got {cadence_seconds}")

    tiers = MODULE_TIERS if module else INVERTER_TIERS
    span_seconds = span.total_seconds()
    best: Tier | None = None
    best_score = float("inf")
    for tier in tiers:
        score = _fit_score(span_seconds / _resolution_seconds(tier, cadence_seconds), width_px)
        if best is None:
            best = tier
            best_score = score
            continue
        # On a tie prefer the coarser tier: it costs less to store and draw for
        # the same visible outcome. Compared with a tolerance rather than for
        # exact equality, because the two scores are independently computed
        # logarithms and a mathematical tie can differ by one unit in the last
        # place, which would silently pick the finer tier instead.
        tied = math.isclose(score, best_score, rel_tol=1e-9, abs_tol=1e-12)
        if (score < best_score and not tied) or (tied and tiers.index(tier) > tiers.index(best)):
            best = tier
            best_score = score
    assert best is not None
    return best.name
