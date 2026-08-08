"""Tests for state-of-charge drift detection: arraysense.calibration.

Timestamps step one minute apart because that is the tier the API actually
reads. Detection depends on elapsed time between samples, so a helper that
invented a convenient ten-minute cadence would exercise a code path production
never takes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from arraysense.calibration import (
    CORROBORATING_ABSORB,
    ESTIMATE_AFTER_DAYS,
    INFO_AFTER_DAYS,
    PACK_RESET_LAG,
    WARNING_AFTER_DAYS,
    assess,
    bank_recalibrated_at,
    full_charge_windows,
    last_full_charge,
    packs_recalibrated,
)

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
ABSORB_V = 55.9
RESTING_V = 52.0


def _inverter(
    start: datetime,
    volts: list[float],
    current: float = 5.0,
    ref: float = 56.0,
) -> list[dict[str, Any]]:
    """One row per minute, as the minute rollup tier serves them."""
    return [
        {
            "timestamp": start + timedelta(minutes=i),
            "battery_voltage_v": v,
            "battery_current_a": current,
            "bms_charge_voltage_ref_v": ref,
            "error": None,
        }
        for i, v in enumerate(volts)
    ]


def _charge(minutes: int = 21, **kwargs: Any) -> list[float]:
    """A resting bank, a spell at absorb voltage, then resting again."""
    return [RESTING_V, *([kwargs.pop("volts", ABSORB_V)] * minutes), RESTING_V]


def _modules(when: datetime, socs: dict[str, float], volts: float = 53.8) -> list[dict[str, Any]]:
    return [
        {"timestamp": when, "serial": s, "soc_pct": soc, "voltage_v": volts}
        for s, soc in socs.items()
    ]


# --- finding the absorb windows ---------------------------------------------


def test_a_sustained_absorb_period_is_a_full_charge_window() -> None:
    (window,) = full_charge_windows(_inverter(NOW, _charge()))
    assert window[0] == NOW + timedelta(minutes=1)
    assert window[1] == NOW + timedelta(minutes=21)


def test_a_brief_voltage_spike_is_not_a_full_charge() -> None:
    # A couple of minutes at absorb voltage is a surge or a decode blip, not a
    # charge that ran long enough for any BMS to reset its counter.
    assert full_charge_windows(_inverter(NOW, _charge(minutes=3))) == []


def test_two_distant_blips_do_not_splice_into_one_window() -> None:
    # The defect this check exists for. Rollup tiers are built with an
    # "error IS NULL" filter and carry no error column, so downtime shows up as
    # rows that are simply missing. Judging continuity by list adjacency turned
    # two isolated minutes hours apart into a two-hour absorb, declared a full
    # charge that never happened, and silenced the warning for a month.
    rows = [
        *_inverter(NOW, [ABSORB_V]),
        *_inverter(NOW + timedelta(hours=2), [ABSORB_V]),
    ]
    assert full_charge_windows(rows) == []


def test_a_gap_mid_charge_splits_the_run_rather_than_bridging_it() -> None:
    # Twenty minutes of absorb, an hour we have no readings for, then twenty
    # more. Neither half reaches the threshold on its own and the hole must not
    # be assumed to have been more of the same.
    rows = [
        *_inverter(NOW, [ABSORB_V] * 15),
        *_inverter(NOW + timedelta(hours=1), [ABSORB_V] * 15),
    ]
    assert full_charge_windows(rows) == []


def test_a_charge_still_pushing_current_has_not_reached_the_top() -> None:
    # Charge current can hold the bank above its reference well short of full,
    # so a run whose last sample is still at 90 A is a charge in progress.
    assert full_charge_windows(_inverter(NOW, _charge(), current=90.0)) == []
    assert len(full_charge_windows(_inverter(NOW, _charge(), current=4.0))) == 1


def test_the_threshold_follows_the_bms_charge_reference() -> None:
    # A bank configured to a lower charge voltage still reaches its own full.
    # Hard-coding 56 V would mean such a bank never registered a full charge.
    volts = [50.0, *([54.2] * 21), 50.0]
    assert full_charge_windows(_inverter(NOW, volts, ref=56.0)) == []
    assert len(full_charge_windows(_inverter(NOW, volts, ref=54.4))) == 1


def test_a_run_still_open_at_the_end_of_the_data_still_counts() -> None:
    # The bank may be absorbing right now. Requiring a return to rest before
    # crediting the charge would delay the reset by hours.
    (window,) = full_charge_windows(_inverter(NOW, [ABSORB_V] * 21))
    assert window[1] == NOW + timedelta(minutes=20)


def test_rows_without_a_voltage_do_not_extend_a_window() -> None:
    rows = _inverter(NOW, _charge())
    for row in rows[5:9]:
        row["battery_voltage_v"] = None
    assert full_charge_windows(rows) == []


def test_no_history_at_all_yields_no_windows() -> None:
    assert full_charge_windows([]) == []


# --- deciding whether the packs actually recalibrated -----------------------


def test_a_full_charge_needs_every_pack_to_reach_full() -> None:
    inverter = _inverter(NOW, _charge())
    absorb = NOW + timedelta(minutes=10)
    short = _modules(absorb, {"A": 100.0, "B": 100.0, "C": 100.0, "D": 91.0})
    assert last_full_charge(inverter, short) is None
    complete = _modules(absorb, {"A": 100.0, "B": 100.0, "C": 99.0, "D": 100.0})
    assert last_full_charge(inverter, complete) == NOW + timedelta(minutes=21)


def test_ninety_nine_percent_counts_as_recalibrated() -> None:
    # The BMS reports whole percent to an accuracy of five, so demanding
    # exactly 100 would strand a pack that never quite says it.
    assert packs_recalibrated(_modules(NOW, {"A": 99.0}))
    assert not packs_recalibrated(_modules(NOW, {"A": 98.0}))


def test_a_pack_silent_through_the_window_blocks_the_verdict() -> None:
    # Silence is not consent. A CAN dropout on one pack during a charge would
    # otherwise restart the drift clock for the whole bank, and that pack —
    # whose counter genuinely never reset — is then reported as calibrated for
    # as long as it keeps drifting.
    reported = _modules(NOW, {"A": 100.0, "B": 100.0, "C": 100.0})
    assert packs_recalibrated(reported)
    assert not packs_recalibrated(reported, expected=["A", "B", "C", "D"])


def test_a_pack_reporting_no_number_is_not_counted_as_full() -> None:
    rows = [*_modules(NOW, {"A": 100.0}), {"timestamp": NOW, "serial": "B", "soc_pct": None}]
    assert not packs_recalibrated(rows, expected=["A", "B"])


def test_no_packs_reporting_is_not_a_full_charge() -> None:
    assert not packs_recalibrated([])


def test_a_pack_reading_full_outside_an_absorb_window_does_not_count() -> None:
    # A pack drifted high reads 100% long before the bank is full. Requiring
    # the bank to be at its charge reference is what rejects that.
    inverter = _inverter(NOW, [RESTING_V] * 30)
    assert last_full_charge(inverter, _modules(NOW, {"A": 100.0, "B": 100.0})) is None


# --- the severity ladder -----------------------------------------------------


def _at(days: float, **kwargs: Any) -> Any:
    return assess(
        now=NOW,
        last_full=NOW - timedelta(days=days),
        searched_days=60.0,
        modules=kwargs.pop("modules", _modules(NOW, {"A": 61.0, "B": 62.0})),
        **kwargs,
    )


def test_the_ladder_climbs_at_its_stated_boundaries() -> None:
    # Each threshold is pinned on both sides. Without this the constants can be
    # moved, or a whole band removed, with every test still passing.
    assert _at(INFO_AFTER_DAYS - 0.1).severity == "none"
    assert _at(INFO_AFTER_DAYS).severity == "info"
    assert _at(WARNING_AFTER_DAYS - 0.1).severity == "info"
    assert _at(WARNING_AFTER_DAYS).severity == "warning"
    assert _at(ESTIMATE_AFTER_DAYS - 0.1).severity == "warning"
    assert _at(ESTIMATE_AFTER_DAYS).severity == "elevated"


def test_only_the_elevated_band_relabels_the_readings_as_estimates() -> None:
    assert not _at(3).soc_is_estimate
    assert not _at(INFO_AFTER_DAYS).soc_is_estimate
    assert not _at(WARNING_AFTER_DAYS).soc_is_estimate
    assert _at(ESTIMATE_AFTER_DAYS).soc_is_estimate


def test_a_lone_pack_is_still_relabelled_once_the_readings_are_stale() -> None:
    # soc_is_estimate must not depend on there being a spread to measure. A
    # single-pack bank, or one with three packs off the CAN bus, has readings
    # every bit as stale as a four-pack bank.
    status = _at(ESTIMATE_AFTER_DAYS + 1, modules=_modules(NOW, {"A": 61.0}))
    assert status.soc_spread_pct is None
    assert status.soc_is_estimate


def test_no_full_charge_in_the_searched_history_is_reported_as_at_least() -> None:
    # Never seen is not the same as never happened. The status says how far
    # back we looked rather than inventing a date.
    status = assess(now=NOW, last_full=None, searched_days=60.0, modules=_modules(NOW, {"A": 61.0}))
    assert status.severity == "elevated"
    assert status.days_since is None
    assert "60" in status.detail


# --- telling a drifting gauge apart from a real fault -----------------------


def test_matched_voltages_say_the_gauges_drifted_not_the_packs() -> None:
    # The claim this module exists to make. Four packs within 30 mV hold the
    # same charge whatever their counters say, and the message has to say so —
    # a warning that alleges a battery problem would be false.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 57.0, "voltage_v": 53.78},
        {"timestamp": NOW, "serial": "B", "soc_pct": 76.0, "voltage_v": 53.76},
    ]
    status = _at(31, modules=modules)
    assert status.soc_spread_pct == 19.0
    assert status.voltage_spread_mv == 20.0
    assert not status.wiring_suspect
    assert "counters" in status.detail
    assert "same charge" in status.detail


def test_packs_that_agree_on_charge_get_no_counters_disagree_claim() -> None:
    # The sentence above is a diagnosis, not decoration. It must not appear
    # when the packs agree on percentage, because then there is nothing to
    # explain away.
    status = _at(31, modules=_modules(NOW, {"A": 61.0, "B": 62.0}))
    assert "counters" not in status.detail


def test_a_wide_voltage_spread_is_a_different_and_louder_problem() -> None:
    # Packs in parallel are forced to the same voltage. A quarter of a volt
    # between them is a lug, a busbar or a failing pack — not a drifting
    # counter, and not something charging will fix.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 60.0, "voltage_v": 53.80},
        {"timestamp": NOW, "serial": "B", "soc_pct": 60.0, "voltage_v": 53.55},
    ]
    status = _at(2, modules=modules)
    assert status.voltage_spread_mv == 250.0
    assert status.wiring_suspect
    assert status.severity == "alert"
    assert "charge" not in status.detail.lower().replace("charging will not fix", "")


def test_a_wiring_fault_does_not_erase_the_drift_verdict() -> None:
    # A bank with both a bad lug and four months of drift is the one case where
    # the per-pack numbers are least worth trusting. An earlier version
    # returned early from the wiring branch and rendered them as measurements.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 40.0, "voltage_v": 53.80},
        {"timestamp": NOW, "serial": "B", "soc_pct": 70.0, "voltage_v": 53.50},
    ]
    status = _at(120, modules=modules)
    assert status.wiring_suspect
    assert status.soc_is_estimate
    assert status.days_since == 120.0


def test_a_pack_that_stopped_reporting_does_not_fire_the_wiring_alarm() -> None:
    # The latest-per-module query has no time bound, so a pack that fell off
    # the CAN bus keeps returning its final reading forever. Comparing that
    # against three live packs sends the owner after a fault that does not
    # exist, while the real problem — a dropped link — goes unnamed.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 60.0, "voltage_v": 53.80},
        {"timestamp": NOW, "serial": "B", "soc_pct": 61.0, "voltage_v": 53.79},
        {"timestamp": NOW, "serial": "C", "soc_pct": 60.0, "voltage_v": 53.81},
        {"timestamp": NOW - timedelta(days=7), "serial": "D", "soc_pct": 95.0, "voltage_v": 51.20},
    ]
    status = _at(2, modules=modules)
    assert not status.wiring_suspect
    assert status.voltage_spread_mv == 20.0
    assert status.soc_spread_pct == 1.0


def test_a_silent_pack_is_unknown_not_zero() -> None:
    # An absent module must never be folded into the spread as 0%, which would
    # manufacture a 60-point drift out of a CAN dropout.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 61.0, "voltage_v": 53.78},
        {"timestamp": NOW, "serial": "B", "soc_pct": None, "voltage_v": None},
    ]
    status = _at(2, modules=modules)
    assert status.soc_spread_pct is None
    assert status.voltage_spread_mv is None
    assert not status.wiring_suspect


def test_a_single_pack_bank_has_no_spread_to_report() -> None:
    status = _at(2, modules=_modules(NOW, {"A": 61.0}))
    assert status.soc_spread_pct is None
    assert not status.wiring_suspect


# --- findings from an independent review -------------------------------------


def test_five_minute_holes_in_the_minute_tier_do_not_bridge() -> None:
    # The tier this reads has one row per minute. Rows at 0, 5, 10, 15 and 20
    # minutes were being credited as one unbroken twenty-minute absorb, when
    # nothing at all is known about the four minutes between each of them.
    rows = [
        {
            "timestamp": NOW + timedelta(minutes=5 * i),
            "battery_voltage_v": ABSORB_V,
            "bms_charge_voltage_ref_v": 56.0,
            "error": None,
        }
        for i in range(5)
    ]
    assert full_charge_windows(rows) == []


def test_ordinary_jitter_still_counts_as_one_run() -> None:
    # The tolerance has to absorb a late write without bridging a real hole.
    rows = _inverter(NOW, _charge())
    rows[10]["timestamp"] += timedelta(seconds=40)
    assert len(full_charge_windows(rows)) == 1


def test_a_pack_read_minutes_ago_is_not_compared_against_live_ones() -> None:
    # A spread asks whether the packs disagree at one instant. A fourteen-
    # minute-old 53.50 V beside a live 53.80 V is 300 mV of elapsed time, and
    # it was raising the wiring alarm over a pack that had simply gone quiet.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 60.0, "voltage_v": 53.80},
        {"timestamp": NOW, "serial": "B", "soc_pct": 61.0, "voltage_v": 53.79},
        {
            "timestamp": NOW - timedelta(minutes=14),
            "serial": "C",
            "soc_pct": 58.0,
            "voltage_v": 53.50,
        },
    ]
    status = _at(2, modules=modules)
    assert not status.wiring_suspect
    assert status.voltage_spread_mv == 10.0


def test_packs_all_equally_old_are_still_compared_with_each_other() -> None:
    # The skew is measured against the newest reading, not the clock, so a bank
    # that went quiet together is still diagnosable from its last moment.
    old = NOW - timedelta(minutes=10)
    modules = [
        {"timestamp": old, "serial": "A", "soc_pct": 60.0, "voltage_v": 53.80},
        {"timestamp": old, "serial": "B", "soc_pct": 60.0, "voltage_v": 53.50},
    ]
    status = _at(2, modules=modules)
    assert status.voltage_spread_mv == 300.0
    assert status.wiring_suspect


def test_a_wiring_alert_keeps_the_drift_verdict_beside_it() -> None:
    # Both can be true at once, and an earlier version let the alert erase the
    # ladder entirely — leaving no sign the counters were stale as well.
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 40.0, "voltage_v": 53.80},
        {"timestamp": NOW, "serial": "B", "soc_pct": 70.0, "voltage_v": 53.50},
    ]
    status = _at(120, modules=modules)
    assert status.severity == "alert"
    assert status.drift_severity == "elevated"
    assert "stale as well" in status.detail


def test_a_wiring_alert_on_a_freshly_charged_bank_says_nothing_about_drift() -> None:
    modules = [
        {"timestamp": NOW, "serial": "A", "soc_pct": 60.0, "voltage_v": 53.80},
        {"timestamp": NOW, "serial": "B", "soc_pct": 60.0, "voltage_v": 53.50},
    ]
    status = _at(2, modules=modules)
    assert status.drift_severity == "none"
    assert "stale as well" not in status.detail


# --- a charge this hardware finishes in three minutes ------------------------
#
# The absorb below is the reference installation's charge of 8 August 2026, read
# off the minute tier the API scans: 55.6, 55.7, 55.7 and 55.5 V at 111.2, 63.9,
# 12.2 and -1.6 A, three minutes against a twenty-minute rule. Three packs
# snapped to 100% two minutes into that and the fourth five minutes later, three
# minutes after the bank's own voltage had fallen back below the reference. The
# spread across the four fell from 24 points to nothing. The climb before the
# absorb is shortened; everything else is as recorded.
#
# ``DRIFTED`` is what the four read through the quarter hour before the absorb,
# measured: 70-75, 77-80, 72-76 and 96-99. The fourth pack's dip below 99 is
# load-bearing rather than incidental — it is what proves that pack's counter
# also reset, and a version of these tests that pinned it at a flat 99 was
# passing a bank three quarters of whose counters were stale.

BANK = ("P1", "P2", "P3", "P4")
DRIFTED = {"P1": 75.0, "P2": 80.0, "P3": 76.0, "P4": 96.0}
THREE_OF_FOUR = {"P1": 100.0, "P2": 100.0, "P3": 77.0, "P4": 100.0}
RESET = dict.fromkeys(BANK, 100.0)


def _packs_over(start: datetime, minutes: int, socs: dict[str, float]) -> list[dict[str, Any]]:
    """Every pack read at the same instant, once a minute, holding these states."""
    rows: list[dict[str, Any]] = []
    for i in range(minutes):
        rows.extend(_modules(start + timedelta(minutes=i), socs))
    return rows


def _short_charge(
    start: datetime = NOW, amps: tuple[float, ...] = (111.2, 63.9, 12.2, -1.6)
) -> list[dict[str, Any]]:
    """A resting bank, the measured three-minute absorb with its taper, then rest."""
    rows = _inverter(start, [RESTING_V] * 15 + [55.6, 55.7, 55.7, 55.5] + [54.9] * 20)
    for row, current in zip(rows[15:19], amps, strict=True):
        row["battery_current_a"] = current
    return rows


def _reference_packs(start: datetime = NOW, last_pack_at: int = 21) -> list[dict[str, Any]]:
    """Three packs full inside the absorb, the fourth some minutes after it ends."""
    return [
        *_packs_over(start, 17, DRIFTED),
        *_packs_over(start + timedelta(minutes=17), last_pack_at - 17, THREE_OF_FOUR),
        *_packs_over(start + timedelta(minutes=last_pack_at), 10, RESET),
    ]


def test_a_three_minute_absorb_with_every_pack_full_is_a_charge() -> None:
    # The defect. This hardware crosses absorb, finishes and tapers off in about
    # three minutes, so the twenty-minute hold never happens and sixty days of
    # daily full charges came back as "no full charge found".
    assert full_charge_windows(_short_charge()) == []
    when = last_full_charge(_short_charge(), _reference_packs())
    assert when == NOW + timedelta(minutes=21)


def test_the_last_pack_may_snap_its_counter_after_the_bank_leaves_absorb() -> None:
    # Measured: the slowest pack crossed 99% three minutes after the inverter's
    # own terminal voltage had fallen back below the reference. Judging only the
    # rows inside the voltage window would miss the pack that completes the set.
    inside = _reference_packs(last_pack_at=18)
    assert last_full_charge(_short_charge(), inside) == NOW + timedelta(minutes=18)
    late = _reference_packs(last_pack_at=30)
    assert last_full_charge(_short_charge(), late) == NOW + timedelta(minutes=30)


def test_a_pack_that_snaps_long_after_the_absorb_belongs_to_another_event() -> None:
    # Beyond the lag there is nothing tying the reset to this charge, and a
    # counter that arrives at 100% by itself an hour later is the drift being
    # detected rather than evidence against it.
    beyond = int((timedelta(minutes=18) + PACK_RESET_LAG).total_seconds() // 60) + 2
    assert last_full_charge(_short_charge(), _reference_packs(last_pack_at=beyond)) is None


def test_one_pack_drifting_to_full_alone_is_not_a_short_charge() -> None:
    # The hole this must not reopen. One counter at 100% while the rest sit at
    # 70 is drift, which is the condition being detected.
    lagging = dict.fromkeys(BANK, 70.0)
    drifted_high = {**lagging, "P4": 100.0}
    packs = [
        *_packs_over(NOW, 15, lagging),
        *_packs_over(NOW + timedelta(minutes=15), 20, drifted_high),
    ]
    assert last_full_charge(_short_charge(), packs) is None


def test_packs_already_all_reading_full_prove_nothing_about_a_short_absorb() -> None:
    # The other shape of the same hole, and the reason a standing 100% is not
    # accepted. Uncounted standby draw makes every counter read high, so a bank
    # left alone long enough has all four pegged at 100% while it sits at half
    # charge. Only packs seen *arriving* at full are a reset.
    assert last_full_charge(_short_charge(), _packs_over(NOW, 35, RESET)) is None


def test_a_pack_below_full_a_month_ago_does_not_vouch_for_this_charge() -> None:
    # The lookback is bounded at both ends. A pack measured below full in July
    # says nothing about whether August's absorb was a transition, and an open
    # lookback would let those rows vouch for a bank pegged at 100% for weeks.
    packs = [
        *_packs_over(NOW - timedelta(days=30), 15, DRIFTED),
        *_packs_over(NOW, 35, RESET),
    ]
    assert last_full_charge(_short_charge(), packs) is None


def test_packs_reaching_full_at_different_times_are_not_one_reset() -> None:
    # Four counters cannot independently arrive at 100% within a poll of each
    # other; four peaks scattered across half an hour is exactly what drift
    # looks like. peaks-anywhere would credit this, which is why the short door
    # asks for the packs to be full at one instant.
    rest = dict.fromkeys(BANK, 70.0)
    packs = [*_packs_over(NOW, 15, rest)]
    for i, serial in enumerate(BANK):
        packs.extend(_packs_over(NOW + timedelta(minutes=15 + 4 * i), 1, {**rest, serial: 100.0}))
    assert packs_recalibrated(packs, expected=BANK)
    assert last_full_charge(_short_charge(), packs) is None


def test_a_single_minute_above_the_reference_is_not_corroboration() -> None:
    # The dongle crosses replies, so one row above the reference can be another
    # register's value wearing this one's name. Two consecutive rows is the
    # cheapest thing that cannot be a single misrouted sample, and the measured
    # charge held for three.
    rows = _inverter(NOW, [RESTING_V] * 15 + [55.7] + [54.9] * 20)
    rows[15]["battery_current_a"] = -1.6
    assert last_full_charge(rows, _reference_packs()) is None


def test_a_short_absorb_still_pushing_current_is_not_a_charge() -> None:
    # The taper is what separates a bank sitting full from one being held above
    # its reference by charge current, and the short door leans on it harder
    # than the long one because it has no duration to fall back on.
    pushing = _short_charge(amps=(111.2, 98.4, 90.1, 88.7))
    assert last_full_charge(pushing, _reference_packs()) is None


def test_a_pack_silent_through_a_short_charge_blocks_the_reset() -> None:
    # Silence is not consent here either. A CAN dropout on one pack must not
    # restart the drift clock on behalf of a counter nobody read.
    three = {k: v for k, v in RESET.items() if k != "P3"}
    packs = [
        *_packs_over(NOW, 15, {k: v for k, v in DRIFTED.items() if k != "P3"}),
        *_packs_over(NOW + timedelta(minutes=17), 14, three),
    ]
    assert bank_recalibrated_at(packs, NOW + timedelta(minutes=15)) is not None
    assert bank_recalibrated_at(packs, NOW + timedelta(minutes=15), expected=BANK) is None


# --- what a transition has to be a transition of -----------------------------


def test_three_counters_pegged_and_one_charging_is_not_a_bank_reset() -> None:
    # Reproduced against the reference database through the endpoint before this
    # test existed. Three counters drifted high and pegged at 100% while the
    # fourth genuinely charged: every pack reads at or above 99 by the end, and
    # one of them was previously below, so a rule asking only for *some* pack to
    # have transitioned called the bank calibrated. Three quarters of those
    # percentages were stale and the dashboard would have drawn all four as
    # measurements. A charge resets every counter, so every counter has to show
    # the transition.
    pegged = {"P1": 100.0, "P2": 100.0, "P3": 100.0}
    packs = [
        *_packs_over(NOW, 15, {**pegged, "P4": 96.0}),
        *_packs_over(NOW + timedelta(minutes=15), 16, {**pegged, "P4": 100.0}),
    ]
    assert last_full_charge(_short_charge(), packs) is None


def test_a_pack_that_never_dipped_below_full_blocks_the_reset() -> None:
    # The same defect one pack at a time, and the reason DRIFTED reads 96 rather
    # than 99 for the fourth pack. A counter sitting at exactly 99 through the
    # quarter hour before the absorb has not been seen to reset, so the bank has
    # not been seen to reset.
    stuck = {**DRIFTED, "P4": 99.0}
    packs = [
        *_packs_over(NOW, 17, stuck),
        *_packs_over(NOW + timedelta(minutes=17), 14, RESET),
    ]
    assert last_full_charge(_short_charge(), packs) is None


def test_a_second_absorb_cannot_borrow_the_first_charge_transition() -> None:
    # Two absorb touches ten minutes apart. The packs transition during the
    # first and read 100 throughout the second, so the second's lookback reaches
    # back past the first and finds a below-full row belonging to that charge.
    # The answer must be the first charge's reset, not a later window credited
    # on evidence that was never about it.
    volts = [RESTING_V] * 15 + [55.6, 55.7, 55.7, 55.5] + [54.9] * 9
    volts += [55.6, 55.7, 55.7, 55.5] + [54.9] * 14
    rows = _inverter(NOW, volts)
    for offset in (15, 28):
        for row, current in zip(rows[offset : offset + 4], (111.2, 63.9, 12.2, -1.6), strict=True):
            row["battery_current_a"] = current
    assert len(full_charge_windows(rows, CORROBORATING_ABSORB)) == 2
    packs = [
        *_packs_over(NOW, 17, DRIFTED),
        *_packs_over(NOW + timedelta(minutes=17), 25, RESET),
    ]
    assert last_full_charge(rows, packs) == NOW + timedelta(minutes=17)


def test_a_pack_full_before_the_window_opened_has_not_been_seen_to_arrive() -> None:
    # A pack's qualifying reading has to fall inside the charge, not in the
    # minutes before it. Otherwise the whole transition can happen before the
    # bank ever reaches its charge reference, and the absorb becomes decoration
    # rather than corroboration.
    packs = [
        *_packs_over(NOW, 14, DRIFTED),
        *_packs_over(NOW + timedelta(minutes=14), 1, RESET),
        *_modules(NOW + timedelta(minutes=16), {"P1": 100.0}),
    ]
    assert last_full_charge(_short_charge(), packs) is None


def test_a_reading_whose_clock_cannot_be_compared_is_dropped_not_raised() -> None:
    # This is reached from an endpoint, so a TypeError here is a 500. Every store
    # read hands back aware timestamps, but a caller assembling rows by hand can
    # mix them, and a row that cannot be placed in time is unusable rather than
    # fatal — the same treatment a row with no state of charge already gets.
    naive = {"timestamp": datetime(2026, 8, 6, 12, 5), "serial": "P1", "soc_pct": 100.0}
    packs = [
        *_packs_over(NOW, 17, DRIFTED),
        naive,
        *_packs_over(NOW + timedelta(minutes=17), 14, RESET),
    ]
    assert last_full_charge(_short_charge(), packs) == NOW + timedelta(minutes=17)
    assert bank_recalibrated_at([naive], NOW) is None
