"""tiers.py — tier selection from a time range and a target pixel width.

Choosing a resolution tier by fit is a pure function of how long a range is and
how wide the chart is: no database is involved. A chart cannot draw more points
than it has pixels, so massively oversampling is waste; returning far too few
points loses detail the display could have shown. The fit is judged as a
ratio rather than a difference — a tier giving twice the width is a better fit
than one giving a fortieth of it — and the full-cadence tier's resolution is
the polling interval, which the caller knows and the store does not.

Fit alone is not enough, and what it misses costs the reader the data. Tiers
have floors: the raw tier keeps thirty days, and the SolarAssistant importer
only ever filled it that far back, so a six-hour window from two months ago
scores as full cadence and comes back **empty** while the minute tier holds
every minute of it. A window straddling that floor is worse than empty — it
comes back half its length, drawn as though the missing half were a quiet
morning. ``select_tier_for_range`` is the same fit asked of the tiers that can
actually answer, and it reports whether the tier it chose holds the whole
range, so a caller can say so rather than presenting a part as the whole.

The two measurements it makes are deliberately different questions:

- *Which tier* is judged against what the best-placed tier could answer. A
  tier must not be marked down for an hour nobody has recorded yet — during a
  collector outage every tier is equally short, and a rule that scored raw
  down for it would quietly promote a six-hour chart to hourly buckets.
- *Whether the answer is whole* is judged against the range that was asked
  for, because that is the only question a reader cares about.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

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
    return _best_fit(tiers, span.total_seconds(), width_px, cadence_seconds).name


def _best_fit(
    tiers: Sequence[Tier],
    span_seconds: float,
    width_px: int,
    cadence_seconds: int,
) -> Tier:
    """Return the tier of ``tiers`` whose point count best fits the width.

    Split out so the coverage-aware selection can ask the same question of a
    narrowed list — the tiers that can actually answer the range — rather than
    holding a second copy of the scoring rule that would drift from this one.
    ``tiers`` must stay in the schema's fine-to-coarse order, because the tie
    is broken by position.
    """
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
        if (score < best_score and not tied) or (
            tied and list(tiers).index(tier) > list(tiers).index(best)
        ):
            best = tier
            best_score = score
    assert best is not None
    return best


@dataclass(frozen=True)
class TierChoice:
    """A chosen tier and how much of the asked-for range it can actually answer.

    ``complete`` is False when the tier holds materially less than was asked
    for, and ``available`` then names the part it does hold — None when it
    holds none of it. A caller drawing a partial answer has to say so: a
    half-length series looks exactly like a quiet half-day, which is the shape
    the project's cardinal rule exists to forbid.
    """

    name: str
    complete: bool
    available: tuple[datetime, datetime] | None


def _overlap(
    start: datetime, end: datetime, window: tuple[datetime, datetime] | None
) -> tuple[datetime, datetime] | None:
    """The part of ``[start, end)`` that ``window`` contains, or None if none."""
    if window is None:
        return None
    first = max(start, window[0])
    last = min(end, window[1])
    return (first, last) if last > first else None


def _seconds(window: tuple[datetime, datetime] | None) -> float:
    """How long ``window`` is, in seconds; nothing at all is zero seconds.

    Both bounds are aware instants from the store, so the subtraction goes
    through the UTC clock rather than a wall clock — the trap this project
    has already paid for twice.
    """
    if window is None:
        return 0.0
    return (window[1] - window[0]).total_seconds()


def select_tier_for_range(
    start: datetime,
    end: datetime,
    width_px: int,
    cadence_seconds: int,
    coverage: Mapping[str, tuple[datetime, datetime] | None],
    module: bool = False,
) -> TierChoice:
    """Pick a tier for a real range, from the tiers that can answer it.

    ``coverage`` maps a tier name to the first and last instant that tier
    holds, or None where it holds nothing — ``SqliteStore.tier_spans``. It is
    required rather than optional on purpose: a selection made without it is
    the one this function exists to replace, and an optional argument is an
    invitation to keep making it.

    Two failures this prevents, both measured against the reference
    installation's own database. A six-hour window sixty days back scores as
    full cadence and returns nothing, because the raw tier's floor is a month
    old, while the minute tier holds all of it. A window straddling that floor
    returns half its length with nothing to say the other half exists — the
    silent truncation, which is worse, because an empty chart at least looks
    wrong.

    What decides *which* tier is how much of the range a tier misses compared
    with the best-placed tier, not compared with the range itself. Nothing has
    recorded the current minute yet, and during a collector outage every tier
    is short by the same hours; scoring tiers against the range would mark them
    all down together and hand a six-hour chart to the hourly tier for the
    duration of the outage. Each tier is allowed a shortfall of two of its own
    buckets before it stops counting as able to answer, which is the edge
    between "this bucket is still open" and "this tier does not hold that".

    What decides whether the answer is *whole* is the range that was asked for,
    since that is the only question the reader has. When no tier can answer at
    all the best fit is still returned — the caller has a query to run either
    way — with ``complete`` False and ``available`` None.
    """
    if end <= start:
        raise ValueError(f"end must be after start, got {start} to {end}")
    span = end - start
    tiers = MODULE_TIERS if module else INVERTER_TIERS
    # Validation and the plain fit both live in select_tier, so this cannot
    # diverge from the rule the width and cadence are checked against.
    fallback = select_tier(span, width_px, cadence_seconds, module=module)

    held = {tier.name: _overlap(start, end, coverage.get(tier.name)) for tier in tiers}
    reachable = max((_seconds(window) for window in held.values()), default=0.0)
    candidates = [
        tier
        for tier in tiers
        if reachable - _seconds(held[tier.name]) <= 2 * _resolution_seconds(tier, cadence_seconds)
    ]
    chosen = (
        _best_fit(candidates, span.total_seconds(), width_px, cadence_seconds)
        if candidates
        else next(tier for tier in tiers if tier.name == fallback)
    )

    window = held[chosen.name]
    short = span.total_seconds() - _seconds(window)
    complete = short <= 2 * _resolution_seconds(chosen, cadence_seconds)
    return TierChoice(
        name=chosen.name,
        complete=complete,
        # A complete answer covers the range that was asked for; the stored
        # bounds are a shade inside it only because the newest bucket is still
        # being filled, and reporting that as a shortfall would qualify every
        # live chart on the dashboard.
        available=(start, end) if complete else window,
    )
