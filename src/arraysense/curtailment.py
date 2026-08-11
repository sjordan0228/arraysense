"""curtailment.py — telling energy the inverter refused from energy the array lost.

A performance score that counts a throttled afternoon as a loss punishes an
installation for the one thing it did right: the bank was full, the house was
quiet, and there was nowhere for the power to go. That is not a fault, and a
score that cannot say so drags every demand-limited day down and buries the
faults it exists to surface.

The rule is deliberately conservative — an hour books as curtailed only when
there was nowhere for the power to go **and** the electrical evidence agrees.
Each half alone is wrong in a way that matters:

*Gate alone* would excuse a genuinely faulty string on any full-battery
afternoon, which is precisely the fault this feature was built to find, hidden
by precisely the condition that makes it hardest to notice.

*Signature alone* would claim curtailment whenever MPPT hunting or a passing
cloud edge mimicked the shape, quietly inflating the score.

Under-claiming is therefore the safe direction and the one taken throughout:
the score runs pessimistic on some genuinely throttled hours, and never
flatters a real fault. Shortfall that fails either half is not silently
absorbed — it goes to ``unexplained``, which claims no cause and counts against
the ratio, because a growing unexplained segment is itself the diagnostic.

The thresholds below are not invented. They were read off 264,764 stored rows
from the reference installation before this module was written; the four
minutes on 8 August that pin them down are tabulated in the design spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from arraysense.panels import StringSpec

logger = logging.getLogger(__name__)

# The bank is full enough that it cannot absorb much. It reads 100 % for hours
# either side of a real throttle, which is why this is a supporting signal and
# never the trigger on its own.
_FULL_SOC_PCT = 95.0

# The BMS pinches charge current long before anything else moves, and it is the
# least ambiguous signal on the system: 400 A normally, stepping down through
# 80 and 60 to 40 as the bank fills. Measured against the window's own maximum
# rather than a constant, because 400 A is this bank's figure and not a fact
# about batteries.
_LIMIT_PINCHED_FRACTION = 0.5

# A string held off its maximum power point sits near open-circuit voltage.
# Six percent above its own operating voltage is well clear of the two or three
# percent that ordinary irradiance and temperature swings produce.
_VOLTAGE_ELEVATED_FRACTION = 1.06

# ...while its current is strangled. Read off the measured event rather than
# chosen: the throttled string carried 4.9 A against the 8.6 A it runs at
# normally, a ratio of 0.57, so a half-current rule would have missed the one
# throttle anybody has actually looked at. This sits just above it.
#
# It is loose on its own — an overcast hour halves current too — and it is meant
# to be. The elevated voltage beside it is what separates a throttle from a
# cloud: current falls under both, but only a throttle walks the voltage up
# toward open circuit. Both must hold.
_CURRENT_STRANGLED_FRACTION = 0.65

# Below this the hour is not short enough to be worth attributing to anything.
# Modelled expectation carries real error, and a five-percent gap is inside it.
_MIN_SHORTFALL_FRACTION = 0.05


@dataclass(frozen=True)
class StringBaseline:
    """What one string looks like when nothing is holding it back.

    Per string, never shared, and that is the whole point rather than a
    refinement. String 1 of the reference array runs at about 377 V normally —
    a *higher* voltage than strings 2 and 3 show while they are being
    curtailed. A threshold shared across strings would therefore mark string 1
    permanently curtailed and hide every real fault on it, which is the wiring
    difference the project notes record arriving exactly where it was predicted.
    """

    name: str
    operating_voltage_v: float
    operating_current_a: float
    samples: int


def baseline_for(name: str, readings: list[tuple[float, float]]) -> StringBaseline | None:
    """Fit one string's ordinary operating point from its own (voltage, current) pairs.

    The median of the string's better-producing hours, not the mean: a handful
    of throttled hours in the window would drag a mean upward in voltage and
    down in current, which is the exact direction that would then hide the next
    throttle. The median is unmoved by a minority behaving differently, and a
    minority is what curtailment is here — 112 rows in the whole history.

    Returns None when there is too little to fit, and the caller then attributes
    nothing rather than guessing. An unfitted string cannot be judged, and a
    guess would be a diagnosis drawn from no evidence.
    """
    producing = [(v, a) for v, a in readings if v > 0.0 and a > 0.0]
    if len(producing) < 3:
        return None
    # Judge against the string's own better hours: near-dark hours sit at odd
    # points on the curve and describe nothing about how it runs in sun.
    producing.sort(key=lambda pair: pair[1], reverse=True)
    upper = producing[: max(3, len(producing) // 2)]
    voltages = sorted(v for v, _ in upper)
    currents = sorted(a for _, a in upper)
    return StringBaseline(
        name=name,
        operating_voltage_v=voltages[len(voltages) // 2],
        operating_current_a=currents[len(currents) // 2],
        samples=len(producing),
    )


def gate_is_open(
    soc_pct: float | None,
    charge_limit_a: float | None,
    window_max_limit_a: float | None,
) -> bool:
    """Say whether there was anywhere for the power to go this hour.

    "Open" means there was not — the bank is full and the BMS has pinched the
    current it will accept. Both are required: a bank at 100 % with its limit
    still wide open is a bank that would happily take more, and a pinched limit
    on a half-empty bank is the BMS managing temperature or cell balance, which
    is a different story with a different remedy.

    Missing data closes the gate. An hour that cannot be shown to be throttled
    is not throttled, and the shortfall goes to ``unexplained`` where it is
    visible, rather than being excused by an absent reading.
    """
    if soc_pct is None or charge_limit_a is None or not window_max_limit_a:
        return False
    if soc_pct < _FULL_SOC_PCT:
        return False
    return charge_limit_a < window_max_limit_a * _LIMIT_PINCHED_FRACTION


def signature_matches(
    voltage_v: float | None,
    current_a: float | None,
    baseline: StringBaseline | None,
) -> bool:
    """Say whether this string's electrical shape is that of a throttled one.

    The MPPT walking off the maximum power point on purpose: voltage climbing
    toward open circuit while current is strangled. Both halves are required,
    against this string's own baseline and no other's.

    Missing data or an unfitted baseline means no match, for the same reason the
    gate closes on absence — the conservative direction is the one that leaves a
    real fault visible.
    """
    if baseline is None or voltage_v is None or current_a is None:
        return False
    elevated = voltage_v >= baseline.operating_voltage_v * _VOLTAGE_ELEVATED_FRACTION
    strangled = current_a <= baseline.operating_current_a * _CURRENT_STRANGLED_FRACTION
    return elevated and strangled


def curtailed_kwh_for_hour(
    expected_kwh: float,
    actual_kwh: float,
    gate_open: bool,
    signature_seen: bool,
) -> float:
    """How much of this hour's shortfall books as curtailment rather than loss.

    Zero unless both halves of the rule hold, which is the whole discipline of
    this module in one line. The caller sends whatever this does not claim to
    ``unexplained``, so nothing is lost by returning zero — a shortfall simply
    keeps its honest name.

    A shortfall inside the model's own error is not attributed at all. Claiming
    a fifty-watt-hour gap as curtailment would be reading noise as a diagnosis.
    """
    if not (gate_open and signature_seen):
        return 0.0
    shortfall = expected_kwh - actual_kwh
    if shortfall <= 0.0 or shortfall < expected_kwh * _MIN_SHORTFALL_FRACTION:
        return 0.0
    return shortfall


def window_max_limit(limits: list[float | None]) -> float | None:
    """The charge limit this bank accepts when nothing is holding it back.

    Self-calibrating on purpose. 800 A is the reference bank's figure — the
    value 90 % of its readings carry — not a property of batteries, and a
    constant here would mis-read every other installation: a smaller bank whose
    limit never exceeds 100 A would look permanently throttled.

    The ninetieth percentile rather than the maximum, because the maximum is one
    reading and this dongle is known to cross replies between requests. A single
    misrouted high value would inflate the anchor and make the bank's ordinary
    limit read as pinched, which would open the gate all day and start excusing
    real faults. A percentile needs a majority to move it. On the reference
    history the two agree exactly, which is the point: it costs nothing here and
    holds when a reading arrives from the wrong question.
    """
    seen = sorted(limit for limit in limits if limit is not None and limit > 0.0)
    if not seen:
        return None
    return seen[min(len(seen) - 1, int(len(seen) * 0.9))]


def strings_by_mppt(strings: list[StringSpec]) -> dict[int, list[StringSpec]]:
    """Group strings by the MPPT that reads them, since readings are per-MPPT."""
    grouped: dict[int, list[StringSpec]] = {}
    for spec in strings:
        grouped.setdefault(spec.mppt, []).append(spec)
    return grouped
