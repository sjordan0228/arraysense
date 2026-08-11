"""The rule that keeps a throttled afternoon out of the loss column, and keeps
a broken string out of the excused one.

The numbers staged here are the reference installation's own, read from stored
production rows: strings 2 and 3 running near 310 V at 8.6 A, string 1 at 377 V
at 47 A because it is wired with more panels in series, and the throttle itself
at 372.8 V / 4.9 A with the BMS charge limit pinched from its ordinary 800 A.

The 800 A figure is measured, not quoted: it is the value 90 % of the stored
readings carry. An earlier note calling the usual limit 400 A was wrong — 400 A
appears in 1.2 % of readings and is itself a mildly pinched state.
"""

from __future__ import annotations

import pytest

from arraysense.curtailment import (
    StringBaseline,
    baseline_for,
    curtailed_kwh_for_hour,
    gate_is_open,
    signature_matches,
    window_max_limit,
)

# Strings 2 and 3 as they run normally.
_NORMAL = StringBaseline(
    name="South", operating_voltage_v=310.0, operating_current_a=8.6, samples=40
)
# String 1, wired differently: a higher voltage in normal operation than the
# others show while they are being curtailed.
_STRING_ONE = StringBaseline(
    name="East", operating_voltage_v=377.0, operating_current_a=47.0, samples=40
)


class TestTheGate:
    """Was there anywhere for the power to go?"""

    def test_a_full_bank_with_a_pinched_limit_opens_the_gate(self) -> None:
        assert gate_is_open(soc_pct=100.0, charge_limit_a=40.0, window_max_limit_a=400.0)

    def test_a_full_bank_that_still_accepts_current_does_not(self) -> None:
        # 100 % reads for hours either side of a real throttle. On its own it
        # says nothing, which is why the limit is the signal that matters.
        assert not gate_is_open(soc_pct=100.0, charge_limit_a=400.0, window_max_limit_a=400.0)

    def test_a_pinched_limit_on_a_half_empty_bank_does_not(self) -> None:
        # The BMS managing temperature or cell balance is a different event
        # with a different remedy, and must not be read as a full bank.
        assert not gate_is_open(soc_pct=54.0, charge_limit_a=40.0, window_max_limit_a=400.0)

    @pytest.mark.parametrize(
        ("soc", "limit", "window"),
        [(None, 40.0, 400.0), (100.0, None, 400.0), (100.0, 40.0, None)],
    )
    def test_a_missing_reading_closes_the_gate(
        self, soc: float | None, limit: float | None, window: float | None
    ) -> None:
        # Absence must never excuse a shortfall. An hour that cannot be shown
        # to be throttled keeps its shortfall in plain sight as unexplained.
        assert not gate_is_open(soc_pct=soc, charge_limit_a=limit, window_max_limit_a=window)

    def test_the_widest_limit_in_the_window_anchors_pinched(self) -> None:
        # Self-calibrating: the reference bank's ordinary limit is 800 A, which
        # is its own figure and not a fact about batteries. A bank whose limit
        # never exceeds 100 A would read as permanently throttled against any
        # constant borrowed from another installation.
        assert window_max_limit([400.0, 80.0, 40.0, None]) == 400.0
        assert window_max_limit([None, None]) is None
        assert window_max_limit([]) is None

    def test_a_small_bank_is_judged_against_its_own_widest_limit(self) -> None:
        small = window_max_limit([100.0, 90.0, 20.0])
        assert gate_is_open(soc_pct=99.0, charge_limit_a=20.0, window_max_limit_a=small)
        assert not gate_is_open(soc_pct=99.0, charge_limit_a=90.0, window_max_limit_a=small)


class TestTheSignature:
    """Does the electrical shape agree?"""

    def test_held_near_open_circuit_with_current_strangled_matches(self) -> None:
        # The measured event: 372.8 V at 4.9 A against a 310 V / 8.6 A baseline.
        assert signature_matches(372.8, _NORMAL)

    def test_a_string_at_its_normal_operating_point_does_not(self) -> None:
        assert not signature_matches(310.0, _NORMAL)

    def test_low_current_at_normal_voltage_is_not_curtailment(self) -> None:
        # Cloud, shading, or a fault. The voltage would climb if the MPPT were
        # walking off the power point on purpose; it has not, so this is a
        # shortfall that must keep its own name.
        assert not signature_matches(308.0, _NORMAL)

    def test_a_high_voltage_hour_that_lost_nothing_books_nothing(self) -> None:
        """A cold bright morning lifts voltage without anything being throttled.

        The signature is voltage alone, so this hour matches it — and books
        nothing regardless, because a string producing what the sun allowed has
        no shortfall to attribute. That is the division of labour: voltage says
        the MPPT may have stepped away, and the shortfall says whether anything
        was actually given up.
        """
        assert signature_matches(340.0, _NORMAL)
        assert curtailed_kwh_for_hour(3.0, 3.0, gate_open=True, signature_seen=True) == 0.0

    def test_string_one_is_judged_against_its_own_baseline(self) -> None:
        """The finding that changes the implementation rather than confirming it.

        String 1 runs at 377 V normally — higher than strings 2 and 3 read
        while curtailed. Judged against a shared threshold it would be marked
        permanently curtailed, and every real fault on it would be excused as
        the inverter protecting the battery.
        """
        # Its ordinary operating point, which a shared threshold would condemn:
        assert not signature_matches(377.0, _STRING_ONE)
        # And it is still detectable when genuinely throttled, on its own terms:
        assert signature_matches(410.0, _STRING_ONE)

    def test_an_unfitted_baseline_matches_nothing(self) -> None:
        assert not signature_matches(372.8, None)


class TestTheBaselineFit:
    def test_a_minority_of_throttled_hours_does_not_move_the_baseline(self) -> None:
        """Otherwise the fit hides the next throttle.

        Throttled hours sit at high voltage and low current. Let them drag the
        baseline and it moves in exactly the direction that stops the following
        throttle from clearing the threshold.
        """
        normal = [(310.0, 8.6)] * 9
        throttled = [(372.8, 4.9)] * 3
        fit = baseline_for("South", normal + throttled)
        assert fit is not None
        assert fit.operating_voltage_v == pytest.approx(310.0)
        assert signature_matches(372.8, fit), "the fit must still catch the throttle"

    def test_too_little_data_fits_nothing_rather_than_guessing(self) -> None:
        # A guessed baseline is a diagnosis drawn from no evidence.
        assert baseline_for("South", [(310.0, 8.6)]) is None
        assert baseline_for("South", []) is None

    def test_dark_hours_are_excluded_from_the_fit(self) -> None:
        readings = [(310.0, 8.6)] * 6 + [(0.0, 0.0)] * 10
        fit = baseline_for("South", readings)
        assert fit is not None
        assert fit.operating_current_a == pytest.approx(8.6)


class TestBooking:
    """Both halves, or it is not curtailment."""

    def test_both_halves_book_the_shortfall_as_curtailed(self) -> None:
        assert curtailed_kwh_for_hour(4.0, 1.0, gate_open=True, signature_seen=True) == 3.0

    def test_the_gate_alone_excuses_nothing(self) -> None:
        """The failure this rule exists to prevent.

        A genuinely faulty string on a full-battery afternoon: the gate is
        open, so a gate-only rule would book the whole shortfall as the
        inverter protecting the battery and the fault would never surface —
        hidden by the very condition that makes it hardest to notice.
        """
        assert curtailed_kwh_for_hour(4.0, 1.0, gate_open=True, signature_seen=False) == 0.0

    def test_the_signature_alone_claims_nothing(self) -> None:
        # MPPT hunting mimics the shape; without somewhere for the power to
        # have gone, there is no reason to think it was refused.
        assert curtailed_kwh_for_hour(4.0, 1.0, gate_open=False, signature_seen=True) == 0.0

    def test_a_shortfall_inside_the_models_own_error_is_not_attributed(self) -> None:
        # The floor is five percent of expected, and these pin it from both
        # sides rather than merely sitting under it: a gap the wrong side of
        # the line must book, or the constant could drift with nothing failing.
        assert curtailed_kwh_for_hour(4.0, 3.84, gate_open=True, signature_seen=True) == 0.0
        assert curtailed_kwh_for_hour(4.0, 3.76, gate_open=True, signature_seen=True) > 0.0

    def test_producing_more_than_expected_books_nothing(self) -> None:
        assert curtailed_kwh_for_hour(3.0, 3.4, gate_open=True, signature_seen=True) == 0.0


def test_one_misrouted_reading_cannot_inflate_the_anchor() -> None:
    """The dongle crosses replies, and the anchor must survive one that lands here.

    A single spurious high limit taken as the bank's normal would make its real
    limit read as pinched, opening the gate all day and excusing exactly the
    faults this rule exists to expose.
    """
    honest = [100.0] * 20
    assert window_max_limit([*honest, 5000.0]) == 100.0
    assert (
        gate_is_open(
            soc_pct=99.0,
            charge_limit_a=100.0,
            window_max_limit_a=window_max_limit([*honest, 5000.0]),
        )
        is False
    )
