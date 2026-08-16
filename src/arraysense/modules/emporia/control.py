"""control.py — what may be commanded to a charger, and what may not.

The fact this module exists for: **a charge rate persists for ever once set.**
It does not time out, revert, or expire, and nothing at Emporia's end will ever
put it back. So a service that throttles a car for a peak window and then
restarts, loses power, or loses its connection leaves that car at 6 A all night,
and nobody finds out until morning.

Everything here is a decision, not an action. Nothing in this file opens a
socket or touches a store, which is what lets the safety rules be stated as
facts in a test rather than assembled out of fixtures — and the rules are the
only part of this stage that has to be right the first time.

Three of them, and none is a setting:

* a commanded rate is clamped to the owner's floor and ceiling, and to what the
  charger says it can actually do;
* authority decides whether the module may act or only propose, and proposing is
  the default;
* a manual override wins, because somebody standing at the car knows something
  this service does not.

How much autonomy the module has is the owner's setting. The safety net is not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Say what it would do; never write. The default, and the only setting that can
# be safe before anybody has watched this behave against a real car.
ADVISORY = "advisory"
# May set a rate between the floor and the ceiling. May not stop a charge: a
# mode that throttles must not be able to end one the owner needed.
LIMITED = "limited"
# May also stop and start the charger.
FULL = "full"

AUTHORITIES = (ADVISORY, LIMITED, FULL)


@dataclass(frozen=True)
class Limits:
    """The amps this module will never cross, whoever asks.

    ``hardware_max_a`` is the charger's own ``maxChargingRate``, and is None
    when it did not say. A charger that has not reported a maximum has not
    reported a small one, so an unknown maximum imposes nothing — the owner's
    ceiling is then the only limit there is.
    """

    floor_a: int
    ceiling_a: int
    hardware_max_a: int | None = None


@dataclass(frozen=True)
class Decision:
    """What to do about a request, and whether this module may do it itself.

    ``rate_a`` is None for "stop the charger", which is a different power from
    setting a rate and needs a different authority.

    ``refused`` is not an error. It carries the difference between what was
    asked for and what the limits allow, so an audit line can say "asked 40 A,
    set 32 A, ceiling" rather than recording a number nobody requested with no
    hint of where it came from.
    """

    rate_a: int | None
    apply: bool
    reason: str
    refused: str | None = None


def clamp_rate(requested_a: int, limits: Limits) -> tuple[int, str | None]:
    """The most current this request may have, and why it was cut if it was.

    The ceiling is the harder of the two limits and **nothing crosses it, the
    floor included**. That is not a tidiness: with a floor of 32 A and a charger
    admitting 20 A, lifting a small request "to the floor" commanded 32 A —
    more current than the hardware had said it could take. Two settings that can
    be typed in either order, and a hardware maximum that is not a setting at
    all, will eventually disagree; when they do, the lower bound wins, because
    exceeding a limit is the failure with a breaker on the other end of it.
    """
    ceiling = limits.ceiling_a
    if limits.hardware_max_a is not None:
        ceiling = min(ceiling, limits.hardware_max_a)
    floor = min(limits.floor_a, ceiling)
    if requested_a > ceiling:
        return ceiling, f"asked {requested_a} A, held at the {ceiling} A ceiling"
    if requested_a < floor:
        return floor, f"asked {requested_a} A, lifted to the {floor} A floor"
    return requested_a, None


def decide(
    requested_a: int | None,
    *,
    authority: str,
    limits: Limits,
    now: datetime,
    override_until: datetime | None = None,
) -> Decision:
    """Whether this module may command ``requested_a``, and what it would be.

    A decision is always returned, even when nothing may be applied: the page
    shows what the module *would* do under advisory authority, and that is the
    whole of what advisory means.
    """
    if requested_a is None:
        # Stopping the charger. Clamping does not apply; authority does.
        stop = Decision(None, authority == FULL, "stop the charger")
        return stop if authority == FULL else Decision(None, False, "stopping needs full authority")

    rate, refused = clamp_rate(requested_a, limits)
    if override_until is not None and override_until > now:
        return Decision(rate, False, f"override holds until {override_until.isoformat()}", refused)
    # An allowlist, not a denylist. This read `if authority == ADVISORY` and
    # applied everything else, so a typo in the setting — or a database written
    # by a newer build that knows an authority this one does not — was granted
    # permission to set a charge rate. Permission to write to somebody's car has
    # to fail closed, so the two levels that may act are named and the rest
    # propose.
    if authority not in (LIMITED, FULL):
        return Decision(rate, False, "advisory: proposed, not applied", refused)
    return Decision(rate, True, f"set {rate} A", refused)


def restore_target(
    *,
    charger_rate_a: int | None,
    last_set_a: int | None,
    default_a: int,
    holding: bool,
) -> int | None:
    """The rate to put the charger back to on startup, or None to leave it.

    The single most important behaviour in this stage. A service that died
    mid-throttle leaves a car at the floor for as long as nobody notices, and
    noticing means walking out to the car.

    It restores only what it can show is its own: the charger has to be sitting
    at exactly the rate this module last set. A rate somebody moved by hand is
    theirs — undoing it would be this service overruling the person standing in
    front of the charger, which is the opposite of the rule above. And a service
    that has never set anything has nothing to put back, so a fresh install
    walks in and changes nothing at all.
    """
    if holding or last_set_a is None or charger_rate_a is None:
        return None
    if charger_rate_a != last_set_a:
        return None
    if charger_rate_a == default_a:
        return None
    return default_a
