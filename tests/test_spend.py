"""test_spend.py — which circuits cost the most, and what the answer may claim.

Every rule here is the completeness rule from #23 wearing a different hat.
Ranking five circuits over a month's bill invites the reader to believe those
five are the bill, and the reference account's thirty-nine channels do not add
up to a house. So: a circuit nobody heard from is a dash and sorts last, a
circuit thin in one band is labelled in that band and not across the month, and
``mains`` is never in the list at all because it is the total rather than a part.
"""

from __future__ import annotations

import pytest

from arraysense.spend import CircuitEnergy, top_spenders
from arraysense.tariff import AdjustmentRate, parse_bands

BANDS = parse_bands("peak | 0.40 | 15:00-20:00; off-peak | 0.10 | 00:00-24:00")


def circuit(
    name: str,
    kind: str = "circuit",
    *,
    peak: float | None = None,
    off: float | None = None,
    partial: frozenset[str] = frozenset(),
) -> CircuitEnergy:
    return CircuitEnergy(
        name=name,
        kind=kind,
        by_band={"peak": peak, "off-peak": off},
        partial_bands=partial,
    )


def test_the_ranking_is_by_money_not_by_energy() -> None:
    """The whole point of the panel. A dryer that only ever runs at peak
    outranks a fridge that used three times the energy, and that is the useful
    thing to learn rather than a presentation bug to hide."""
    ranked = top_spenders([circuit("Fridge", off=30.0), circuit("Dryer", peak=10.0)], BANDS)
    assert [c.name for c in ranked] == ["Dryer", "Fridge"]
    assert ranked[0].cost == pytest.approx(4.0)
    assert ranked[1].cost == pytest.approx(3.0)


def test_both_figures_are_reported() -> None:
    """Cost is the sort; kWh is what makes the sort explicable."""
    ranked = top_spenders([circuit("Dryer", peak=10.0, off=5.0)], BANDS)
    assert ranked[0].cost == pytest.approx(4.5)
    assert ranked[0].kwh == pytest.approx(15.0)


def test_the_band_split_says_where_the_money_went() -> None:
    ranked = top_spenders([circuit("Dryer", peak=10.0, off=5.0)], BANDS)
    split = {b.band: b for b in ranked[0].bands}
    assert split["peak"].cost == pytest.approx(4.0)
    assert split["off-peak"].cost == pytest.approx(0.5)


def test_a_circuit_that_reported_nothing_is_a_dash_and_sorts_last() -> None:
    """Not a zero, and not above a circuit measured at nothing. "It used no
    energy" and "nobody heard from it" are different claims."""
    ranked = top_spenders([circuit("Dead outlet"), circuit("Quiet lamp", peak=0.0, off=0.0)], BANDS)
    assert [c.name for c in ranked] == ["Quiet lamp", "Dead outlet"]
    assert ranked[0].cost == pytest.approx(0.0)
    assert ranked[1].cost is None
    assert ranked[1].kwh is None


def test_mains_is_never_named() -> None:
    """It is the sum of the circuits beside it. Ranking it is a tautology and
    counting it doubles the house."""
    ranked = top_spenders(
        [circuit("Mains", kind="mains", peak=100.0), circuit("Dryer", peak=10.0)],
        BANDS,
    )
    assert [c.name for c in ranked] == ["Dryer"]


def test_five_is_the_default_and_it_is_alerts_own_five() -> None:
    from arraysense.alerts import DEFAULT_TOP

    many = [circuit(f"C{i}", peak=float(i)) for i in range(1, 9)]
    assert len(top_spenders(many, BANDS)) == DEFAULT_TOP


def test_a_band_thin_on_one_circuit_is_labelled_only_there() -> None:
    """The property test. A circuit recorded end to end through the off-peak
    stretch and thin in the one evening hour that costs the most must say so
    about that hour — a flag over the whole row cannot say which band is soft,
    and a flag over none of it presents a part as a whole."""
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0, off=5.0, partial=frozenset({"peak"}))], BANDS
    )
    split = {b.band: b for b in ranked[0].bands}
    assert split["peak"].partial is True
    assert split["off-peak"].partial is False
    assert ranked[0].partial is True
    assert ranked[0].cost == pytest.approx(4.5)


def test_an_unmeasured_band_does_not_poison_the_circuit_total() -> None:
    """The partial rule from #23: show the figure with a label rather than
    withhold it, and dash only when nothing at all was measured."""
    ranked = top_spenders([circuit("Dryer", peak=None, off=5.0)], BANDS)
    assert ranked[0].cost == pytest.approx(0.5)
    split = {b.band: b for b in ranked[0].bands}
    assert split["peak"].cost is None
    assert split["peak"].kwh is None


def test_a_band_the_tariff_does_not_have_is_dropped_not_raised_on() -> None:
    """A band renamed on the settings page must not take the page down until
    the next rollup catches up."""
    odd = CircuitEnergy(
        name="Dryer", kind="circuit", by_band={"shoulder": 4.0}, partial_bands=frozenset()
    )
    ranked = top_spenders([odd], BANDS)
    assert ranked[0].cost is None


def test_a_missing_band_marks_the_circuit_total_partial() -> None:
    """A total that quietly omits a whole band is exactly as partial as one
    built from thin buckets -- #23 draws no distinction between the two ways a
    sum can fall short of the period."""
    ranked = top_spenders([circuit("Dryer", peak=None, off=5.0)], BANDS)
    assert ranked[0].cost == pytest.approx(0.5)
    assert ranked[0].partial is True


def test_a_circuit_silent_everywhere_is_not_also_flagged_partial() -> None:
    """cost is already None for a circuit nobody heard from -- that is a
    stronger claim than partial, and flagging it too would put a hatch
    caption beside a row that renders no hatch at all."""
    ranked = top_spenders([circuit("Dead outlet")], BANDS)
    assert ranked[0].cost is None
    assert ranked[0].partial is False


def test_the_monthly_adjustment_rides_on_the_circuits_total_too() -> None:
    """compute_cost adds the PCRF/SCRF rider on top of the band price for the
    whole house; a circuit priced without it is wrong by the same rider, and
    the ranking can invert because the rider scales with energy rather than
    with the band's own price."""
    adjustment = AdjustmentRate(
        status="applied", per_kwh=0.01, pcrf_per_kwh=0.006, scrf_per_kwh=0.004
    )
    ranked = top_spenders([circuit("Dryer", peak=10.0, off=5.0)], BANDS, adjustment=adjustment)
    # Band price alone is 10*0.40 + 5*0.10 = 4.5; the rider adds 15 kWh * 0.01.
    assert ranked[0].cost == pytest.approx(4.65)
    assert ranked[0].kwh == pytest.approx(15.0)
    # Finding 5: the rider comes back on its own rather than only folded into
    # cost, since it is not in any BandSpend either -- $4.50 of band cost
    # beside a $4.65 total, with nothing on screen saying where the other
    # $0.15 came from, until this is read.
    assert ranked[0].rider == pytest.approx(0.15)
    band_costs = [b.cost for b in ranked[0].bands if b.cost is not None]
    assert sum(band_costs) == pytest.approx(4.5)


def test_an_unpublished_months_adjustment_poisons_the_circuits_cost() -> None:
    """The rider is not zero when nobody has published it for this month, and
    a circuit cost that silently omitted an unrecorded rider would be the
    same #23 mistake compute_cost already refuses to make for the house."""
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0, off=5.0)], BANDS, adjustment=AdjustmentRate(status="unknown")
    )
    assert ranked[0].cost is None
    assert ranked[0].kwh == pytest.approx(15.0)
    assert ranked[0].rider is None, "as unknown as the total it would have ridden on"


def test_no_configured_adjustment_reads_as_a_zero_rider_not_an_absent_one() -> None:
    """No PCRF/SCRF table at all is a known fact about the tariff -- there is
    no rider -- not the same claim as a published month whose factors have
    not arrived yet."""
    ranked = top_spenders([circuit("Dryer", peak=10.0, off=5.0)], BANDS)
    assert ranked[0].rider == 0.0
