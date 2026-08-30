"""alerts.py — the house is drawing hard, and what is drawing it.

Two questions with two different sources, deliberately kept apart.

*Whether* to warn comes from the inverter. ``load_power_w`` is register 170 with
the EPS fallback — the same figure the dashboard's HOME card shows — and it
arrives every eleven seconds. Emporia's circuits arrive every sixty. Deciding
from the fast one is what makes the warning prompt, and it is what lets an
installation with no Emporia account be warned at all.

*What is responsible* comes from Emporia, and only from Emporia. Without the
module the alert still fires; it simply names nobody. That is the whole reason
the module is optional even for the feature it was asked for.

The number this produces will be read as an accusation, so the completeness rule
applies with its full weight. A set of circuits that accounts for half the load
must not be presented as the explanation — the Costs page has already paid for
that mistake once, where minutes watched was mistaken for energy accounted for.
``accounted_w`` and ``complete`` exist so a page can tell the difference, and
``accounted_w`` is None rather than zero when nothing was measured at all.

Nothing here reads a setting, a store, or a clock: it is given the load, the
threshold and the circuits, and it answers. That is what lets the tests state
every rule above as a fact rather than as a fixture.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

# What the named circuits must cover before the list is called the explanation
# rather than a part of it. Not a tuned figure: it is the point past which the
# remainder is small enough to be the standing draw of a house — fridges, idle
# electronics, the circuits nobody clamped — rather than a load big enough to be
# the actual answer to "what is drawing all that".
COMPLETE_FRACTION = 0.9

# How many circuits are worth naming. A ranked list answers "what is drawing all
# that", and the tail of it is noise: the eleventh circuit at 4 W explains
# nothing and pushes the one that matters off a phone screen.
DEFAULT_TOP = 5

# Circuit kinds that are totals rather than parts. A monitor's mains channel is
# the sum of the circuits beside it, so naming it is a tautology — and adding it
# to ``accounted_w`` would double every house.
#
# Public because the same set answers the same question elsewhere: the circuit
# history endpoint ranks parts against the house and would double it in exactly
# the same way. One definition rather than two, which is the whole difference
# between a rule and a coincidence.
NOT_A_CULPRIT = frozenset({"mains"})


def counts_toward_total(kind: str, parent_device_gid: int | None) -> bool:
    """Whether this circuit may be added to a total of the circuits.

    Two ways to be inside something already counted, and both have been paid
    for. A monitor's mains channel is the sum of the circuits beside it, so
    adding it doubles the house. And a device Emporia says sits inside another
    — a subpanel's branches, a charger on a clamped breaker, a plug in a
    measured socket — is energy the counted circuits already carry: on the
    reference account it took the circuit sum to 131% of the house, and a part
    cannot exceed the whole (#212, #219).

    Both are still shown. This governs what may be summed, never what may be
    named — "which load" is the question a ranking exists to answer, and a
    charger that genuinely drew 3 kW deserves its row.
    """
    return kind not in NOT_A_CULPRIT and parent_device_gid is None


@dataclass(frozen=True)
class Contributor:
    """One circuit that might be responsible, as this module needs it.

    Deliberately not ``CircuitLatest``. Nothing in the core may import from
    ``modules/`` — the alert has to work on an installation that has never heard
    of Emporia — so the route maps one to the other and this file stays ignorant
    of where circuits come from.
    """

    name: str
    watts: int | None
    kind: str
    # What contains this circuit, as Emporia states it. None for a device
    # nothing else contains — the only kind that may be added to a total.
    parent_device_gid: int | None = None


@dataclass(frozen=True)
class HighUsage:
    """A house drawing more than the owner asked to be told about.

    ``accounted_w`` is the sum of the named circuits, or None when no circuit
    reported at all. The two are different states and a page must be able to
    tell them apart: one is "these are the loads", the other is "nobody is
    watching the circuits".
    """

    load_w: int
    threshold_w: int
    contributors: tuple[Contributor, ...]
    accounted_w: int | None

    @property
    def complete(self) -> bool:
        """Whether the named circuits explain the draw, or merely part of it.

        False when nothing was measured, because an empty list explains nothing
        — and False is what keeps a page from writing "caused by" over a
        sentence that has no subject.
        """
        if self.accounted_w is None:
            return False
        return self.accounted_w >= self.load_w * COMPLETE_FRACTION


def high_usage(
    load_w: int | None,
    threshold_w: int,
    circuits: Sequence[Contributor],
    *,
    top: int = DEFAULT_TOP,
) -> HighUsage | None:
    """Whether to warn, and which circuits to name if so.

    None when the threshold is off, when the house is below it, and — the one
    that matters — when the load is absent. A poll that failed is not a house
    drawing nothing and not a house drawing everything; it is a house nobody has
    heard from, and firing on it would teach the owner to ignore the warning.
    """
    if threshold_w <= 0 or load_w is None or load_w < threshold_w:
        return None

    measured = [
        circuit
        for circuit in circuits
        if circuit.watts is not None and circuit.kind not in NOT_A_CULPRIT
    ]
    summable = [
        circuit
        for circuit in measured
        if counts_toward_total(circuit.kind, circuit.parent_device_gid)
    ]
    accounted = sum(int(circuit.watts or 0) for circuit in summable) if summable else None
    ranked = sorted(measured, key=lambda circuit: (-(circuit.watts or 0), circuit.name))
    return HighUsage(
        load_w=load_w,
        threshold_w=threshold_w,
        contributors=tuple(ranked[:top]),
        accounted_w=accounted,
    )
