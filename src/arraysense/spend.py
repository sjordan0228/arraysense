"""spend.py — which circuits cost the most over a period, and what that may claim.

``alerts.py`` answers "what is drawing all that" from one instant's watts. This
answers "what did the money go on" from a period's energy, and the two are
genuinely different questions: a kettle at 5 kW for a minute is the first
answer and never the second.

Nothing here knows what an Emporia is, for the reason ``alerts.py`` does not
either — the core must work on an installation that never heard of the module,
and a reader hands over energy already keyed by band. Nothing here prices a
kilowatt-hour of its own, either: every band figure goes through
``price_by_band``, the one place a missing band is kept from turning quietly
into a small number. The one thing added on top is the monthly PCRF/SCRF
rider, because it is not a band price — it is charged on the whole total, the
same way ``compute_cost`` charges it on the house's — and a circuit priced
without it is wrong by exactly that rider, which scales with energy rather
than with price and can invert the ranking on its own.

The completeness rule from #23 is the whole difficulty and it applies twice
over. A circuit thin in one band is labelled *in that band*, because a circuit
can be recorded end to end through the off-peak stretch and thin in the one
evening hour that costs the most, and a flag over the whole row cannot say
which. A circuit that reported nothing is None and sorts last — below one
measured at nothing, because "it used no energy" and "nobody heard from it" are
different claims and a ranking that puts the second above the first is telling
the reader the opposite of what happened.

The share of the house these circuits account for is deliberately not computed
here: it needs the inverter's own counter, which this module has no business
reaching for. The route measures it beside this list.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from .alerts import DEFAULT_TOP, NOT_A_CULPRIT
from .tariff import AdjustmentRate, RateBand, energy_by_band, price_by_band


@dataclass(frozen=True)
class CircuitEnergy:
    """One circuit's energy over a period, keyed by the band it was used in.

    ``by_band`` is keyed by band name as ``band_intervals`` writes it; the
    casefolding to a band key happens in ``price_by_band``'s own helper, so a
    caller never has to know the difference. A band absent from the mapping was
    never measured for this circuit and a band present with None is the same
    claim said louder. Neither of them is zero.

    ``partial_bands`` names the bands whose stored buckets did not cover their
    whole hour. It is not a doubt about the number — the number is what was
    measured — it is what lets the page label a figure rather than present a
    part as a whole.
    """

    name: str
    kind: str
    by_band: Mapping[str, float | None]
    partial_bands: frozenset[str]


@dataclass(frozen=True)
class BandSpend:
    """What one circuit spent in one band."""

    band: str
    kwh: float | None
    cost: float | None
    partial: bool


@dataclass(frozen=True)
class CircuitSpend:
    """One circuit's month: what it cost, what it used, and where.

    ``cost`` and ``kwh`` are None together for a circuit that reported in no
    band at all. ``partial`` is true when any band's figure is a labelled part
    rather than a whole, or when the circuit never reported at all in a band
    the period had in effect — a total that quietly leaves out a whole band is
    exactly as partial as one built from thin buckets, and #23 draws no
    distinction between the two ways a sum can fall short. Which band is
    thin, or absent, is in ``bands``, because that is the fact a reader needs
    and a row-level flag cannot carry.

    ``rider`` is the PCRF/SCRF addition folded into ``cost`` but not into any
    ``BandSpend.cost`` — the rider is charged on the whole total rather than
    per band, so there is no band figure to add it to. Carried separately
    rather than silently absorbed, because a page that only shows the bands
    and the total would otherwise present a total the bands do not sum to
    with nothing explaining the gap: $4.50 of segments beside a $4.65 total.
    None when ``cost`` is None too — an unpublished month's rider is exactly
    as unknown as the total it would have ridden on.

    ``grid_kwh`` and ``grid_cost`` are the part of this circuit the meter
    actually charged for — its band energy weighted by the house's own grid
    share in each band, since nothing meters a single circuit's supply. They are
    None together, and None whenever any band the circuit was priced in had no
    knowable share: a grid figure built from three bands of four is a smaller
    number than the truth with nothing on screen saying so, and a reader cannot
    tell it from a circuit that genuinely ran on solar. That absence is
    independent of ``partial`` — a circuit measured end to end can still have an
    unknowable grid share, because it is the inverter's counter that went unread
    rather than the circuit's.
    """

    name: str
    kind: str
    cost: float | None
    kwh: float | None
    bands: tuple[BandSpend, ...]
    partial: bool
    rider: float | None = None
    grid_kwh: float | None = None
    grid_cost: float | None = None
    grid_partial: bool = False


def grid_share_by_band(
    imported: Mapping[str, float | None],
    load: Mapping[str, float | None] | None,
    bands: Sequence[RateBand],
) -> dict[str, float]:
    """What fraction of each band's house load came straight off the meter.

    Nothing meters a single circuit's supply — an Emporia clamp measures current
    through a branch and has no idea whether the electrons behind it came down
    the service drop or off the roof. The house-level answer is metered, so a
    circuit's grid figure is this ratio applied to the circuit's own energy.

    Per band rather than per period, because that is the granularity at which
    the answer is exact in the money: a band has one price, so a circuit's grid
    cost in a band is its band cost times this share with no rounding path of
    its own. A month's single ratio applied to an evening circuit understates
    its grid share badly, since the evening is grid-heavy and midday is not.

    Battery discharge is not counted as grid supply, which is an accounting
    choice and the correct one: grid energy that charged the bank was already
    counted as import at the moment it was charged and billed at that hour's
    rate. Counting it again on the way out would charge the owner twice for one
    kilowatt-hour and make this column sum to more than the meter read.

    A band is absent from the result whenever its share cannot be told — either
    counter unread, or a house load of zero that nothing can be divided by.
    Absent is what the caller needs: a share of zero would be the claim that the
    circuit ran on solar, which is a different thing from nobody knowing.

    Clamped to 0..1. A share above one is legitimate — a band in which the grid
    both ran the house and charged the bank imports more than the house loaded —
    but it has nothing to say on a page that can only draw a circuit as at most
    wholly grid-fed, and printing a grid cost larger than the total beside it
    reads as a bug whatever the accounting says.

    Keyed by ``RateBand.key`` rather than by band name, because that is what the
    pricing matches against. ``energy_by_band`` does the casefolding, drops the
    Nones and warns about a name no band answers to, which is the same job here
    as it is everywhere else it is called.
    """
    metered = energy_by_band(imported, bands, "grid import") or {}
    house = energy_by_band(load, bands, "house load") or {}
    out: dict[str, float] = {}
    for key, total in house.items():
        drawn = metered.get(key)
        if drawn is None or total <= 0.0:
            continue
        out[key] = min(1.0, max(0.0, drawn / total))
    return out


def _grid_energy(
    reported: Mapping[str, float], shares: Mapping[str, float]
) -> dict[str, float] | None:
    """One circuit's band energy weighted by the grid's share of each band.

    None the moment any band the circuit was priced in has no share, and that
    is the whole point of the function existing separately. A grid figure built
    from three of four bands is a smaller number than the truth with nothing on
    screen saying so, and a reader cannot tell it from a circuit that genuinely
    ran on solar — #23's rule at the scale of one column, and #23 was reverted
    twice for exactly this shape of shortfall.
    """
    out: dict[str, float] = {}
    for key, kwh in reported.items():
        share = shares.get(key)
        if share is None:
            return None
        out[key] = kwh * share
    return out


def _grid_figures(
    present: Mapping[str, float],
    bands: Sequence[RateBand],
    grid_share: Mapping[str, float] | None,
    *,
    short: Collection[str],
    unknown_rider: bool,
    rider_per_kwh: float | None,
) -> tuple[float | None, float | None, bool]:
    """One circuit's metered energy, what it cost, and whether it is a part.

    Lifted out of ``top_spenders``'s loop rather than left inline, because the
    loop was already the most branched thing in this module and this added a
    fourth nested level to it. The rider arithmetic is duplicated from the
    total's rather than shared with it: they charge the same rate on different
    energy, and a helper taking "which energy" as an argument would be the same
    number of lines with an extra thing to get wrong.

    Priced through ``price_by_band`` for the reason everything here is — the
    rule for when a total is unknown is one rule, and a second copy of it is a
    second place for a missing band to turn quietly into a small number.
    """
    if grid_share is None or not present:
        return None, None, False
    weighted = _grid_energy(present, grid_share)
    if weighted is None:
        return None, None, False
    _, cost, kwh = price_by_band(bands, weighted, partial=True)
    # Only the bands this circuit actually spent in. A flag raised from the
    # period would label a circuit that never entered the short band, and a
    # figure carrying a caption that does not describe it is worse than an
    # unlabelled one.
    #
    # No guard here on ``kwh`` being present, deliberately. It cannot be absent
    # at this point: ``present`` is already filtered to band keys by
    # ``energy_by_band``, so every key in ``weighted`` matches a band and
    # ``price_by_band`` counts at least one — and the only route to an empty
    # ``weighted`` is an empty ``present``, which returned above. A guard that
    # cannot be false is dead code that reads as a live protection.
    label = any(key in short for key in present)
    if cost is None or unknown_rider:
        return kwh, None, label
    if rider_per_kwh is not None and kwh is not None:
        cost += kwh * rider_per_kwh
    return kwh, cost, label


def top_spenders(
    circuits: Sequence[CircuitEnergy],
    bands: Sequence[RateBand],
    *,
    adjustment: AdjustmentRate | None = None,
    grid_share: Mapping[str, float] | None = None,
    grid_short: Collection[str] | None = None,
    top: int = DEFAULT_TOP,
) -> tuple[CircuitSpend, ...]:
    """Rank circuits by what they cost, and say where each one spent it.

    ``bands`` is the set in effect over the period, not every band the tariff
    holds — the same set ``estimate_bill`` prices against. Pricing a seasonal
    tariff's whole list instead leaves an out-of-season band permanently
    unmeasured, which makes every total permanently absent.

    ``adjustment`` is the PCRF/SCRF rider ``compute_cost`` charges on top of
    the house's own band price, resolved once by the caller from
    ``Tariff.adjustment_at`` and passed in rather than recomputed here — the
    same reason ``bands`` arrives pre-selected rather than as a whole tariff.
    It rides on a circuit's total the way it rides on the house's: as one
    whole-total addition rather than a per-band one, because the bill does not
    itemise it by band either, and it is charged on energy rather than on the
    original band price — which is why the ranking can invert without it, not
    just come out a little low. A month whose factors are not published makes
    every circuit's true cost unstatable in exactly the way it makes the whole
    house's cost unstatable, so ``status == "unknown"`` poisons a circuit's
    ``cost`` the same way it poisons ``CostResult.cost`` — leaving ``kwh``
    alone, since the energy figure does not depend on a price nobody has
    entered. The rider itself comes back on ``CircuitSpend.rider`` rather than
    only inside ``cost``, because it never lands in any ``BandSpend`` either —
    a page that drew a circuit's cost breakdown from its bands alone would sum
    to less than the total shown beside it, with nothing on screen saying why.

    ``rider`` on the return value is 0.0 rather than None when no adjustment
    is configured at all: that is a fact about the tariff, known and zero,
    which is a different claim from a published month's rider being unstated.

    ``grid_share`` is ``grid_share_by_band``'s answer, resolved by the caller
    the way ``bands`` and ``adjustment`` already are — this function has no
    business reading the inverter's counters. Omitted, both grid fields come
    back None and every existing caller behaves exactly as it did, which is
    what keeps the ranking's own tests measuring the ranking.

    ``grid_short`` names the bands whose *house* window was only partly
    measured, from the inverter's own shortfall accounting. A band there still
    has a share — it is the ratio over the hours that did report — so the
    figure stands and is labelled rather than withheld, which is the owner's
    settled reading of #23: a labelled partial is fine, and a dash is right
    only when nothing was measured. Band names, matched casefolded, because
    that is how the shortfall speaks them.

    Ranked by cost rather than by energy because the page is about money, and a
    circuit that only ever runs at peak outranking one that used twice the
    energy is the useful thing this panel exists to show. Both figures come
    back, because that ranking is baffling to meet without the second.

    ``top`` defaults to ``alerts.DEFAULT_TOP`` rather than to a five of its own.
    Two constants that have to agree are one constant too many.
    """
    unknown_rider = adjustment is not None and adjustment.status == "unknown"
    short = {name.strip().casefold() for name in (grid_short or ())}
    rider_per_kwh = None if adjustment is None else adjustment.per_kwh
    ranked: list[CircuitSpend] = []
    for circuit in circuits:
        if circuit.kind in NOT_A_CULPRIT:
            continue
        reported = energy_by_band(circuit.by_band, bands, "circuit energy")
        breakdown, cost, kwh = price_by_band(bands, reported, partial=True)
        # A band this circuit never reported in, over a period the band was in
        # effect for, is money the total above cannot include — the same claim
        # a thin bucket makes about a fraction of an hour, just about a whole
        # band instead. Gated on ``cost is not None``: a circuit that reported
        # nowhere at all is already a dash rather than a number, and flagging
        # it partial too would put a hatch caption beside a row with nothing
        # to hatch.
        present = reported or {}
        missing_band = any(band.key not in present for band in bands)
        rider: float | None = None
        if cost is not None:
            if unknown_rider:
                cost = None
            elif rider_per_kwh is not None and kwh is not None:
                rider = kwh * rider_per_kwh
                cost += rider
            else:
                rider = 0.0
        # The grid's own share of this circuit, priced through the same
        # function as the total beside it rather than by arithmetic here.
        # Two ways of turning a kilowatt-hour into money is how this page
        # came to hold a second tariff parser that disagreed within a day.
        grid_kwh, grid_cost, grid_partial = _grid_figures(
            present,
            bands,
            grid_share,
            short=short,
            unknown_rider=unknown_rider,
            rider_per_kwh=rider_per_kwh,
        )
        thin = {name.strip().casefold() for name in circuit.partial_bands}
        split = tuple(
            BandSpend(
                band=band.name,
                kwh=entry.kwh,
                cost=entry.cost,
                # A band nobody measured is absent, not partial: there is no
                # part of it on screen to label.
                partial=entry.kwh is not None and band.key in thin,
            )
            for band, entry in zip(bands, breakdown, strict=True)
        )
        ranked.append(
            CircuitSpend(
                name=circuit.name,
                kind=circuit.kind,
                cost=cost,
                kwh=kwh,
                bands=split,
                partial=any(b.partial for b in split) or (cost is not None and missing_band),
                rider=rider,
                grid_kwh=grid_kwh,
                grid_cost=grid_cost,
                grid_partial=grid_partial,
            )
        )
    # A circuit nobody heard from sorts last, below one measured at nothing.
    # The same order ``CircuitRepository.history`` already puts its series in,
    # and for the same reason: ordering a silence above a fact is a claim.
    ranked.sort(key=lambda c: (c.cost is None, -(c.cost or 0.0), c.name))
    return tuple(ranked[:top])
