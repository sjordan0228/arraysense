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

from arraysense.spend import CircuitEnergy, grid_share_by_band, top_spenders
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


def test_the_grid_share_is_taken_per_band_not_per_period() -> None:
    """Peak is grid-heavy and off-peak is not, and one ratio over both would
    understate an evening circuit and overstate a midday one. Different
    numerators and different denominators, so an implementation that averaged
    the two bands together cannot pass this."""
    shares = grid_share_by_band(
        {"peak": 8.0, "off-peak": 2.0}, {"peak": 10.0, "off-peak": 20.0}, BANDS
    )
    assert shares == {"peak": pytest.approx(0.8), "off-peak": pytest.approx(0.1)}


def test_a_share_above_one_is_clamped() -> None:
    """The grid can import more than the house loads in a band — it also charged
    the bank. A grid cost larger than the total beside it reads as a bug
    whatever the accounting says."""
    shares = grid_share_by_band({"peak": 14.0}, {"peak": 10.0}, BANDS)
    assert shares["peak"] == pytest.approx(1.0)


def test_a_band_the_house_did_not_load_yields_no_share() -> None:
    """Dividing by it is undefined, and calling it zero would claim the circuit
    ran on solar through a band the house did not run at all."""
    assert grid_share_by_band({"peak": 3.0}, {"peak": 0.0}, BANDS) == {}


def test_an_unread_counter_yields_no_share_on_either_side() -> None:
    """Unknown, not zero — on the import side and on the load side alike."""
    assert grid_share_by_band({"peak": None}, {"peak": 10.0}, BANDS) == {}
    assert grid_share_by_band({"peak": 8.0}, {"peak": None}, BANDS) == {}
    assert grid_share_by_band({"peak": 8.0}, None, BANDS) == {}


def test_shares_are_keyed_the_way_energy_is() -> None:
    """PeriodEnergy speaks band names; the pricing speaks casefolded keys. A
    share map keyed the wrong way silently matches nothing, which nulls every
    grid figure on the page rather than raising."""
    shares = grid_share_by_band({"Peak ": 8.0}, {" PEAK": 10.0}, BANDS)
    assert shares == {"peak": pytest.approx(0.8)}


SHARES = {"peak": 0.8, "off-peak": 0.1}


def test_the_grid_figure_weights_each_band_by_its_own_share() -> None:
    """Two circuits with the same total energy and opposite band splits must
    come out with different grid figures, or the weighting is happening on the
    total rather than per band and the test cannot tell the difference.

    Dryer: 10 kWh peak, 0 off. grid kWh = 10 * 0.8 = 8.0, cost = 8.0 * 0.40.
    Fridge: 0 peak, 10 kWh off. grid kWh = 10 * 0.1 = 1.0, cost = 1.0 * 0.10.
    """
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0, off=0.0), circuit("Fridge", peak=0.0, off=10.0)],
        BANDS,
        grid_share=SHARES,
    )
    by_name = {c.name: c for c in ranked}
    assert by_name["Dryer"].grid_kwh == pytest.approx(8.0)
    assert by_name["Dryer"].grid_cost == pytest.approx(3.20)
    assert by_name["Fridge"].grid_kwh == pytest.approx(1.0)
    assert by_name["Fridge"].grid_cost == pytest.approx(0.10)


def test_the_grid_cost_never_exceeds_the_cost_beside_it() -> None:
    """The invariant the whole column rests on. A reader who sees the right-hand
    figure larger than the left-hand one has been told something impossible."""
    ranked = top_spenders([circuit("Dryer", peak=10.0, off=5.0)], BANDS, grid_share=SHARES)
    assert ranked[0].grid_cost is not None
    assert ranked[0].cost is not None
    assert ranked[0].grid_cost <= ranked[0].cost


def test_a_band_with_no_share_nulls_the_grid_figures_and_only_those() -> None:
    """Two independent absences. A circuit measured end to end can still have an
    unknowable grid share, because it is the inverter's counter that went unread
    and not the circuit's — so cost stands and the grid column dashes."""
    ranked = top_spenders([circuit("Dryer", peak=10.0, off=5.0)], BANDS, grid_share={"peak": 0.8})
    assert ranked[0].grid_kwh is None
    assert ranked[0].grid_cost is None
    assert ranked[0].cost == pytest.approx(4.5)
    assert ranked[0].kwh == pytest.approx(15.0)


def test_a_band_the_circuit_never_entered_does_not_need_a_share() -> None:
    """Only the bands actually priced have to be shareable. A circuit that ran
    solely off-peak is fully answerable from the off-peak share alone, and
    withholding its figure over a peak share nobody needed would dash a row that
    is perfectly knowable."""
    ranked = top_spenders([circuit("Fridge", off=10.0)], BANDS, grid_share={"off-peak": 0.1})
    assert ranked[0].grid_kwh == pytest.approx(1.0)
    assert ranked[0].grid_cost == pytest.approx(0.10)


def test_the_rider_rides_on_the_grid_figure_too() -> None:
    """Charged per kilowatt-hour, so it lands on the grid kWh in proportion.
    A grid cost carrying the whole month's rider would be the total's rider on a
    fraction of the total's energy.

    10 kWh peak at 0.40 = 4.00, plus 10 * 0.01 rider = 4.10.
    Grid: 8.0 kWh at 0.40 = 3.20, plus 8.0 * 0.01 rider = 3.28.
    """
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0)],
        BANDS,
        adjustment=AdjustmentRate(status="applied", per_kwh=0.01),
        grid_share=SHARES,
    )
    assert ranked[0].cost == pytest.approx(4.10)
    assert ranked[0].grid_cost == pytest.approx(3.28)


def test_an_unpublished_months_rider_poisons_the_grid_cost_too() -> None:
    """A month whose factors nobody has entered makes the grid figure exactly as
    unstatable as the total, and for the same reason. The energy survives —
    it does not depend on a price."""
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0)],
        BANDS,
        adjustment=AdjustmentRate(status="unknown"),
        grid_share=SHARES,
    )
    assert ranked[0].cost is None
    assert ranked[0].grid_cost is None
    assert ranked[0].grid_kwh == pytest.approx(8.0)


def test_without_a_share_map_the_grid_fields_are_absent_not_zero() -> None:
    """Every existing caller passes no share. They must keep working, and they
    must not start claiming every circuit ran wholly on solar."""
    ranked = top_spenders([circuit("Dryer", peak=10.0)], BANDS)
    assert ranked[0].grid_kwh is None
    assert ranked[0].grid_cost is None


def test_a_circuit_that_reported_nothing_has_no_grid_figure_either() -> None:
    """Nothing to weight. A dash on the left is a dash on the right."""
    ranked = top_spenders([circuit("Dead outlet")], BANDS, grid_share=SHARES)
    assert ranked[0].grid_kwh is None
    assert ranked[0].grid_cost is None


def test_a_partly_unread_house_band_labels_the_grid_figure_rather_than_hiding_it() -> None:
    """The house's counters can go unread across *part* of a band. The band
    still reports — as a sum over the hours that were measured — so the share
    is a ratio over those hours applied to the circuit's whole band energy.
    That is an extrapolation, and #23's rule is that it may be shown as long as
    it is labelled. The owner settled the corollary: a dash is right only when
    nothing was measured.

    Peak is short and off-peak is not, and the circuit spends in both, so a
    flag raised from the period rather than from the bands the circuit actually
    entered would be indistinguishable here from one raised correctly. The
    second circuit is what tells them apart.
    """
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0, off=5.0), circuit("Fridge", off=5.0)],
        BANDS,
        grid_share=SHARES,
        grid_short={"peak"},
    )
    by_name = {c.name: c for c in ranked}
    assert by_name["Dryer"].grid_partial is True
    assert by_name["Dryer"].grid_cost == pytest.approx(3.25), (
        "the figure was withheld rather than labelled"
    )
    assert by_name["Fridge"].grid_partial is False, (
        "a circuit that never entered the short band was labelled from it anyway"
    )


def test_the_house_shortfall_flag_is_matched_casefolded() -> None:
    """``bands_possibly_short`` speaks band names; the circuit's energy is
    keyed by casefolded key. Matched raw, the flag never fires and every
    extrapolated figure ships unlabelled — silently, which is the worst way
    for this particular check to fail."""
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0)], BANDS, grid_share=SHARES, grid_short={" Peak "}
    )
    assert ranked[0].grid_partial is True


def test_no_house_shortfall_leaves_the_grid_figure_unlabelled() -> None:
    """Both sides of the branch. A fully measured month must not dot every
    figure in the column — a mark on everything marks nothing."""
    ranked = top_spenders([circuit("Dryer", peak=10.0)], BANDS, grid_share=SHARES)
    assert ranked[0].grid_partial is False


def test_a_circuit_with_no_grid_figure_is_not_labelled_partial_either() -> None:
    """There is nothing on screen to label. A dot beside a dash points at
    nothing and sends the reader to a caption that does not describe it.

    The route this actually guards is the unknown-share early return, not a
    check on the energy: once a figure exists at all its energy is necessarily
    known, so there is no third state where a dot could appear beside a dash.
    Said plainly because the first version of this test claimed to cover a
    guard that turned out to be unreachable, and passed via this path instead.
    """
    ranked = top_spenders(
        [circuit("Dryer", peak=10.0, off=5.0)],
        BANDS,
        grid_share={"peak": 0.8},
        grid_short={"peak"},
    )
    assert ranked[0].grid_kwh is None
    assert ranked[0].grid_partial is False
