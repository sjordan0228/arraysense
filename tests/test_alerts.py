"""test_alerts.py — the house is drawing hard, and what is drawing it.

Two halves that must not be confused. Whether the alert fires is the
inverter's business: ``load_power_w`` arrives every eleven seconds and is the
same figure the dashboard's HOME card shows, so the warning is prompt and
needs no Emporia at all. What is *responsible* is Emporia's, and an
installation without it still gets the warning — it simply cannot name a
culprit.

The rest is the rule this project exists for, applied to a number that will be
read as an accusation. An absent load must never fire an alert, an absent
circuit reading must never be ranked as though it drew nothing, and a set of
circuits that explains a fraction of the load must say so rather than imply the
list is the whole story.
"""

from __future__ import annotations

from arraysense.alerts import Contributor, high_usage


def _circuits() -> tuple[Contributor, ...]:
    return (
        Contributor("Main panel", 6000, "mains"),
        Contributor("air conditioner main", 3000, "circuit"),
        Contributor("Dryer", 1200, "circuit"),
        Contributor("Family room", 200, "circuit"),
        Contributor("Spare breaker", None, "circuit"),
    )


def test_a_quiet_house_raises_nothing() -> None:
    assert high_usage(4000, 8000, _circuits()) is None


def test_crossing_the_threshold_raises_it() -> None:
    got = high_usage(9000, 8000, _circuits())
    assert got is not None
    assert got.load_w == 9000
    assert got.threshold_w == 8000


def test_sitting_exactly_on_the_threshold_raises_it() -> None:
    # "Crosses 8 kW" reads as 8 kW being enough. A boundary that fires only
    # above it makes the setting mean something a little different from what it
    # says, and nobody would ever notice which.
    assert high_usage(8000, 8000, _circuits()) is not None


def test_an_absent_load_never_raises_an_alert() -> None:
    # The cardinal rule, at the one place where breaking it would shout. A poll
    # that failed is not a house drawing nothing, and it is not a house drawing
    # everything either — it is a house nobody has heard from.
    assert high_usage(None, 8000, _circuits()) is None


def test_a_threshold_of_zero_is_off_rather_than_always_on() -> None:
    # The default. Off has to mean off, or every installation that never
    # configured this gets a permanent alarm the first time it boils a kettle.
    assert high_usage(9000, 0, _circuits()) is None


def test_the_biggest_circuits_are_named_in_order() -> None:
    got = high_usage(9000, 8000, _circuits())
    assert got is not None
    assert [c.name for c in got.contributors] == ["air conditioner main", "Dryer", "Family room"]


def test_a_monitors_own_total_is_never_named_as_a_culprit() -> None:
    # The mains channel is the sum of the circuits beside it. Named among them
    # it would always be the biggest, and it explains nothing: "what is drawing
    # 9 kW" answered with "the main panel" is a tautology.
    got = high_usage(9000, 8000, _circuits())
    assert got is not None
    assert all(c.kind != "mains" for c in got.contributors)


def test_a_circuit_that_did_not_report_is_not_ranked_as_though_it_drew_nothing() -> None:
    got = high_usage(9000, 8000, _circuits())
    assert got is not None
    assert "Spare breaker" not in [c.name for c in got.contributors]


def test_it_says_how_much_of_the_load_it_actually_explains() -> None:
    # The completeness rule, which money already paid for once on the Costs
    # page. 4.4 kW of named circuits against a 9 kW house is not "the dryer did
    # it" — and a page that cannot say which is which will imply the first.
    got = high_usage(9000, 8000, _circuits())
    assert got is not None
    assert got.accounted_w == 4400
    assert got.complete is False


def test_a_house_whose_circuits_add_up_says_so() -> None:
    circuits = (
        Contributor("air conditioner main", 6000, "circuit"),
        Contributor("Dryer", 2900, "circuit"),
    )
    got = high_usage(9000, 8000, circuits)
    assert got is not None
    assert got.complete is True, "8.9 kW of 9 kW is the whole story for practical purposes"


def test_with_no_circuits_at_all_the_alert_still_fires_and_names_nobody() -> None:
    # The module is optional, and this is what that means here: an installation
    # without Emporia is still told its house is drawing hard.
    got = high_usage(9000, 8000, ())
    assert got is not None
    assert got.contributors == ()
    assert got.accounted_w is None, "nothing measured is not zero explained"
    assert got.complete is False


# --- the edges ------------------------------------------------------------


def test_a_circuit_measured_at_exactly_zero_is_a_fact_and_is_still_named() -> None:
    # The other half of the absent-data rule, and the half that gets forgotten.
    # A circuit measured at 0 W was heard from; it belongs in the list, at the
    # bottom, where it says "not this one" — which is information.
    circuits = (
        Contributor("Main panel", 5000, "mains"),
        Contributor("heater", 0, "circuit"),
        Contributor("light", 100, "circuit"),
    )
    got = high_usage(6000, 5000, circuits)
    assert got is not None
    assert [c.name for c in got.contributors] == ["light", "heater"]


def test_naming_nobody_still_counts_everybody() -> None:
    # top only limits the list. The share of the load explained is a fact about
    # the house, not about how many rows fit on a phone.
    circuits = (
        Contributor("air conditioner main", 3000, "circuit"),
        Contributor("Dryer", 1200, "circuit"),
    )
    got = high_usage(9000, 8000, circuits, top=0)
    assert got is not None
    assert got.contributors == ()
    assert got.accounted_w == 4200


def test_circuits_drawing_the_same_are_ordered_by_name() -> None:
    # Ties have to break somewhere, and the alternative is a list that shuffles
    # itself every refresh while nothing in the house has changed.
    circuits = (
        Contributor("zebra", 1000, "circuit"),
        Contributor("alpha", 1000, "circuit"),
        Contributor("beta", 1000, "circuit"),
    )
    got = high_usage(5000, 2000, circuits)
    assert got is not None
    assert [c.name for c in got.contributors] == ["alpha", "beta", "zebra"]


def test_a_house_monitored_only_at_the_mains_explains_nothing() -> None:
    # A real shape: one Vue on the mains and no branch clamps. The warning is
    # still right and there is genuinely nobody to name, which must read as
    # "nothing measured" rather than as "nothing drawing".
    circuits = (
        Contributor("Main panel", 6000, "mains"),
        Contributor("Sub panel", 3000, "mains"),
    )
    got = high_usage(9000, 8000, circuits)
    assert got is not None
    assert got.contributors == ()
    assert got.accounted_w is None
    assert got.complete is False


def test_overlapping_clamps_can_account_for_more_than_the_house_draws() -> None:
    # A sub-panel clamped as well as the circuits inside it counts twice. The
    # figure is reported as measured rather than clipped, because clipping it
    # would hide the double count instead of showing it.
    circuits = (
        Contributor("heater", 5000, "circuit"),
        Contributor("oven", 4000, "circuit"),
    )
    got = high_usage(6000, 5000, circuits)
    assert got is not None
    assert got.accounted_w == 9000


def test_exactly_nine_tenths_of_the_load_counts_as_the_whole_story() -> None:
    got = high_usage(9000, 8000, (Contributor("heater", 8100, "circuit"),))
    assert got is not None
    assert got.complete is True


def test_a_watt_short_of_nine_tenths_does_not() -> None:
    got = high_usage(9000, 8000, (Contributor("heater", 8099, "circuit"),))
    assert got is not None
    assert got.complete is False


def test_a_negative_threshold_is_off_like_a_zero_one() -> None:
    assert high_usage(9000, -100, (Contributor("heater", 5000, "circuit"),)) is None


def test_a_negative_load_is_below_any_real_threshold() -> None:
    # load_power_w is bounded at -2000 in the registry, so a negative reading is
    # a value this function can be handed. It is a house exporting, which is the
    # opposite of the thing being warned about.
    assert high_usage(-1500, 8000, (Contributor("heater", 100, "circuit"),)) is None


def test_a_watt_below_the_threshold_stays_quiet() -> None:
    assert high_usage(7999, 8000, (Contributor("heater", 100, "circuit"),)) is None


def test_the_order_is_the_ranking_and_not_the_order_it_was_given() -> None:
    circuits = (
        Contributor("small", 100, "circuit"),
        Contributor("medium", 500, "circuit"),
        Contributor("large", 1000, "circuit"),
    )
    got = high_usage(2000, 1500, circuits)
    assert got is not None
    assert [c.name for c in got.contributors] == ["large", "medium", "small"]


def test_a_device_inside_a_counted_circuit_is_named_but_not_added_twice() -> None:
    """The EV charger sits behind a breaker the panel already clamps.

    Emporia states that containment on the device, and until it was read the
    charger's own draw was added on top of the branch measuring it — the
    instantaneous half of #219, and what made ``accounted_w`` overstate the
    house. It still earns a row: "which load" is the question this answers.
    """
    verdict = high_usage(
        load_w=10_000,
        threshold_w=5_000,
        circuits=[
            Contributor("air conditioner main", 6000, "circuit", None),
            Contributor("EVSE", 3000, "charger", 100000),
        ],
    )
    assert verdict is not None
    assert verdict.accounted_w == 6000, "the charger is already inside the clamped branch"
    assert [c.name for c in verdict.contributors] == ["air conditioner main", "EVSE"]
