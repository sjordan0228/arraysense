"""test_emporia_control.py — what may be commanded, and what may not.

Nothing here talks to a charger. It decides, and the deciding is the whole of
the safety net, because the fact this stage is built around is that **a charge
rate persists for ever once set**. Nothing at Emporia's end will ever put it
back. So a service that throttles a car for a peak window and then restarts,
loses power, or loses its connection leaves that car at 6 A all night and
nobody finds out until morning.

Three rules follow, and they are not settings. A commanded rate is clamped to
the owner's floor and ceiling and to what the hardware admits. Authority says
whether the module may act at all or only propose. And a manual override wins,
because the owner standing at the car knows something the service does not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arraysense.modules.emporia.control import (
    ADVISORY,
    APP,
    FULL,
    LIMITED,
    Limits,
    clamp_rate,
    decide,
    restore_target,
)

NOW = datetime(2026, 8, 16, 20, 0, tzinfo=UTC)
LIMITS = Limits(floor_a=6, ceiling_a=32, hardware_max_a=48)


def test_a_rate_inside_the_limits_is_commanded_as_asked() -> None:
    got = decide(20, authority=LIMITED, limits=LIMITS, now=NOW)
    assert got.rate_a == 20
    assert got.apply is True
    assert got.refused is None


def test_a_rate_above_the_ceiling_is_clamped_and_says_so() -> None:
    # Clamped rather than refused: the caller asked for more current than the
    # owner allows, and the useful answer is the most it may have, with a note
    # that it was not what was asked for.
    got = decide(40, authority=LIMITED, limits=LIMITS, now=NOW)
    assert got.rate_a == 32
    assert got.refused is not None
    assert "ceiling" in got.refused


def test_a_rate_below_the_floor_is_lifted_to_it() -> None:
    got = decide(3, authority=LIMITED, limits=LIMITS, now=NOW)
    assert got.rate_a == 6
    assert got.refused is not None
    assert "floor" in got.refused


def test_the_hardware_maximum_beats_a_ceiling_set_too_high() -> None:
    # The owner may type 60 into a box. The charger will not do 60, and a
    # command it cannot honour is a command whose effect nobody can predict.
    limits = Limits(floor_a=6, ceiling_a=60, hardware_max_a=48)
    got = decide(60, authority=LIMITED, limits=limits, now=NOW)
    assert got.rate_a == 48


def test_an_unknown_hardware_maximum_does_not_invent_one() -> None:
    # A charger that did not report its maximum has not said it is small. The
    # owner's ceiling is then the only limit there is.
    limits = Limits(floor_a=6, ceiling_a=32, hardware_max_a=None)
    got = decide(40, authority=LIMITED, limits=limits, now=NOW)
    assert got.rate_a == 32


def test_advisory_authority_proposes_and_never_writes() -> None:
    # The default. It says what it would do and leaves the doing to the owner,
    # which is the only setting that can be safe before anybody has watched
    # this thing behave.
    got = decide(20, authority=ADVISORY, limits=LIMITS, now=NOW)
    assert got.rate_a == 20, "it still says what it would have done"
    assert got.apply is False


def test_a_manual_override_stops_the_module_writing() -> None:
    # Somebody is standing at the car. Whatever they set wins until it lapses.
    got = decide(
        20, authority=FULL, limits=LIMITS, now=NOW, override_until=NOW + timedelta(hours=1)
    )
    assert got.apply is False
    assert got.reason.startswith("override")


def test_a_lapsed_override_stops_winning() -> None:
    got = decide(
        20, authority=FULL, limits=LIMITS, now=NOW, override_until=NOW - timedelta(minutes=1)
    )
    assert got.apply is True


def test_stopping_the_charger_needs_the_authority_to_stop_it() -> None:
    # Setting a rate and switching the thing off are different powers. A mode
    # that may throttle must not be able to end a charge the owner needed.
    limited = decide(None, authority=LIMITED, limits=LIMITS, now=NOW)
    assert limited.apply is False
    full = decide(None, authority=FULL, limits=LIMITS, now=NOW)
    assert full.apply is True
    assert full.rate_a is None


# --- restore on startup ---------------------------------------------------
#
# The single most important behaviour in this stage. If the service finds the
# charger at a rate it set, and has no current reason to hold it there, it puts
# it back — because the alternative is a car left at the floor overnight by a
# service that died mid-throttle.


def test_a_rate_this_service_set_is_restored_when_there_is_no_reason_to_hold() -> None:
    assert restore_target(charger_rate_a=6, last_set_a=6, default_a=32, holding=False) == 32


def test_a_rate_this_service_set_is_left_alone_while_it_still_has_a_reason() -> None:
    assert restore_target(charger_rate_a=6, last_set_a=6, default_a=32, holding=True) is None


def test_a_rate_somebody_else_set_is_never_touched() -> None:
    # The owner moved the slider to 10 A. That is theirs, not ours to undo,
    # even though we happen to have set 6 A at some point in the past.
    assert restore_target(charger_rate_a=10, last_set_a=6, default_a=32, holding=False) is None


def test_a_service_that_has_never_set_anything_restores_nothing() -> None:
    # A fresh install must not walk in and change a charge rate it has no
    # history with. Nothing was ours, so nothing is ours to put back.
    assert restore_target(charger_rate_a=6, last_set_a=None, default_a=32, holding=False) is None


def test_a_rate_already_at_the_default_needs_no_restoring() -> None:
    assert restore_target(charger_rate_a=32, last_set_a=32, default_a=32, holding=False) is None


# --- the edges, and two rules that were silently open -----------------------


def test_the_floor_can_never_push_a_rate_above_what_the_charger_admits() -> None:
    # Found by generating cases rather than by reading. With a floor of 32 A and
    # a charger admitting 20 A, lifting a small request "to the floor" commanded
    # 32 A — more current than the hardware said it could take. The ceiling is
    # the harder of the two limits and nothing may cross it, the floor included.
    limits = Limits(floor_a=32, ceiling_a=60, hardware_max_a=20)
    rate, _ = clamp_rate(10, limits)
    assert rate == 20


def test_a_floor_set_above_the_ceiling_still_never_exceeds_the_ceiling() -> None:
    # A misconfiguration nothing prevents: two independent settings that can be
    # typed in the wrong order. Whatever else happens, the ceiling holds.
    limits = Limits(floor_a=32, ceiling_a=6)
    rate, _ = clamp_rate(10, limits)
    assert rate == 6


def test_an_authority_nobody_recognises_may_not_write() -> None:
    # This failed open. A typo in the setting, or a database written by a newer
    # build that knows an authority this one does not, granted the module
    # permission to set a charge rate. Permission is an allowlist now: the two
    # levels that may act are named, and everything else proposes.
    got = decide(20, authority="typo-or-newer-build", limits=LIMITS, now=NOW)
    assert got.apply is False
    assert got.rate_a == 20, "it still says what it would have done"


def test_the_floor_and_the_ceiling_themselves_pass_through_unrefused() -> None:
    assert clamp_rate(6, LIMITS) == (6, None)
    assert clamp_rate(32, LIMITS) == (32, None)


def test_an_override_that_has_reached_its_end_has_lapsed() -> None:
    # The boundary has to fall somewhere and this is the reading that matches
    # the words: "holds until 20:00" is over at 20:00.
    got = decide(20, authority=FULL, limits=LIMITS, now=NOW, override_until=NOW)
    assert got.apply is True


def test_zero_and_negative_requests_are_lifted_to_the_floor() -> None:
    # Neither is a way to stop the charger. Stopping is requested with None and
    # needs its own authority; a zero here is a caller with a bad number.
    assert decide(0, authority=LIMITED, limits=LIMITS, now=NOW).rate_a == 6
    assert decide(-5, authority=LIMITED, limits=LIMITS, now=NOW).rate_a == 6


def test_a_charger_that_did_not_report_its_rate_is_not_restored() -> None:
    assert restore_target(charger_rate_a=None, last_set_a=6, default_a=32, holding=False) is None


def test_an_override_outranks_advisory_in_the_reason_it_gives() -> None:
    # Both would refuse to write. The owner standing at the car is the more
    # useful thing to say, so it is the one said.
    got = decide(
        20, authority=ADVISORY, limits=LIMITS, now=NOW, override_until=NOW + timedelta(hours=1)
    )
    assert got.apply is False
    assert "override" in got.reason


def test_what_was_refused_is_reported_even_when_nothing_is_applied() -> None:
    # Advisory authority still has to say "you asked for 40 and the ceiling is
    # 32", or the proposal it shows is a number with no explanation.
    got = decide(40, authority=ADVISORY, limits=LIMITS, now=NOW)
    assert got.apply is False
    assert "ceiling" in (got.refused or "")


# --- who is driving ---------------------------------------------------------
#
# Emporia ships four of its own controllers for a charger. Rather than only
# warning that they are on, the owner says outright which side has the wheel —
# and while it is Emporia's, nothing here writes at all, whatever authority
# says. Two services taking turns at one slider is worse than either alone.


def test_while_the_app_manages_the_charger_nothing_here_writes() -> None:
    got = decide(20, authority=APP, limits=LIMITS, now=NOW)
    assert got.apply is False
    assert "Emporia" in got.reason
    assert got.rate_a == 20, "it still says what it would have done"


def test_the_app_managing_it_also_stops_a_stop() -> None:
    # The heavier power needs the same gate. Full authority plus "the app has
    # this" must not add up to switching somebody's charger off.
    got = decide(None, authority=APP, limits=LIMITS, now=NOW)
    assert got.apply is False


def test_when_this_service_manages_it_authority_decides_as_before() -> None:
    assert decide(20, authority=LIMITED, limits=LIMITS, now=NOW).apply is True
    assert decide(20, authority=ADVISORY, limits=LIMITS, now=NOW).apply is False


def test_an_unrecognised_owner_of_the_charger_leaves_it_alone() -> None:
    # Fails closed, like authority. A value from a newer build, or a typo, must
    # not be read as permission to drive somebody's car charger.
    got = decide(20, authority="something-else", limits=LIMITS, now=NOW)
    assert got.apply is False


def test_the_app_managing_it_is_the_answer_even_under_an_override() -> None:
    # Both are true at once, and this is the one worth saying. Reporting the
    # override would imply that this service takes over when the override
    # lapses, and it does not — the charger is not ours at all.
    got = decide(
        20,
        authority=APP,
        limits=LIMITS,
        now=NOW,
        override_until=NOW + timedelta(hours=1),
    )
    assert got.apply is False
    assert "Emporia" in got.reason


def test_a_restore_target_outside_the_limits_is_clamped_before_it_is_sent() -> None:
    # restore_target answers "where should this go back to" and knows nothing
    # about limits, so on its own it can name a rate the module may not set.
    # Everything that acts on it goes through decide() first, and this is the
    # test that keeps it that way.
    target = restore_target(charger_rate_a=6, last_set_a=6, default_a=10, holding=False)
    assert target == 10, "the raw answer is unclamped by design"
    limits = Limits(floor_a=32, ceiling_a=48)
    got = decide(target, authority=LIMITED, limits=limits, now=NOW)
    assert got.rate_a == 32, "and the limits still hold on the way out"
    assert got.refused is not None
