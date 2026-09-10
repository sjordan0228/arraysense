"""health.py — what an observation turns a finding into.

Two questions this module keeps apart. *What is the condition and did it
change* is answered here, from the previous record and a new observation, with
no store and no clock. *When did it run, and what was seen* is answered by the
caller, who alone knows when a rule executed.

A finding is one record that outlives the poll that created it: a condition
that recurs every eleven seconds updates counters rather than appending
thousands of events, and a gap in the readings neither resolves it nor
forgives it. A state the record already holds produces no event, which is
what keeps an event log the size of the changes rather than the size of the
polls.

Nothing here reads a setting, a store, or a clock: it is given the finding,
the observation, and their timestamps, and it answers what the finding
becomes. That is what lets the tests state every transition as a fact rather
than as a fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum


class FindingState(StrEnum):
    """What the last evaluation of a condition concluded.

    Active means the condition held. Recovered means a later evaluation
    found it resolved. Unassessable means the readings were not there to
    judge, which is neither a finding nor a resolution.
    """

    ACTIVE = "active"
    RECOVERED = "recovered"
    UNASSESSABLE = "unassessable"


# What kinds of subject a finding can be about. Documented, not validated: a
# rule owns its claim about what it measures, so a scope nobody recognises is
# the rule's problem to justify, not this module's to reject.
SCOPES: tuple[str, ...] = ("bank", "pack", "mppt_group", "string", "inverter")


@dataclass(frozen=True)
class FindingKey:
    """Stable identity of one condition.

    A bumped ``rule_version`` is a different identity, not a newer edit of
    the same one: when a rule's meaning changes, what it observes under the
    new meaning must not inherit the old record.
    """

    rule_id: str
    rule_version: int
    scope: str
    subject: str


@dataclass(frozen=True)
class Observation:
    """What a rule saw when it ran, judged or not.

    ``evidence`` must be JSON-serializable: plain dicts, lists, str, int,
    float, bool, None and datetime strings. ``summary`` is one plain sentence
    in observation language — what was seen and where — and never diagnoses
    a cause.
    """

    key: FindingKey
    state: FindingState
    observed_at: datetime
    summary: str
    evidence: dict[str, object]


@dataclass(frozen=True)
class Finding:
    """The one record of one condition, current across polls.

    ``occurrences`` counts evaluations that saw the condition active,
    including the first. ``last_observed`` is when the condition was last
    seen; ``last_evaluated`` is when the rule last ran for this key, whether
    or not it could judge — they diverge exactly when readings are missing.
    ``resolved_at`` is set only by a recovery, never by an unassessable
    evaluation.
    """

    key: FindingKey
    summary: str
    state: FindingState
    first_observed: datetime
    last_observed: datetime
    last_evaluated: datetime
    occurrences: int
    resolved_at: datetime | None
    evidence: dict[str, object]


@dataclass(frozen=True)
class FindingEvent:
    """A change of state worth showing, not a trace of every poll.

    One event per transition: a long-running condition updates one row's
    counters, and only a real change of state writes a line here.
    """

    key: FindingKey
    at: datetime
    state: FindingState
    summary: str
    evidence: dict[str, object]


def merge_observation(
    finding: Finding | None, observation: Observation
) -> tuple[Finding | None, FindingEvent | None]:
    """Merge one observation into the finding it belongs to.

    Returns the new state of the record and, when that state changed, the
    event marking it. An event exists when a finding is created or changes
    state, never for a repeat of what the record already says.

    Args:
        finding: the record this key had before, or None if there was none.
        observation: what the rule saw, and when it saw it.

    Raises:
        ValueError: if the observation's key differs from the finding's.
            The store looks findings up by key, so a merge across keys would
            silently rewrite another condition's history.
    """
    if finding is not None and finding.key != observation.key:
        # Loud rather than correct-by-luck: the two records are unrelated
        # conditions, and naming both keys is the only message that lets the
        # caller find the wrong lookup.
        raise ValueError(f"observation for {observation.key} cannot merge into {finding.key}")

    now = observation.observed_at
    state = observation.state
    # A copy, not an alias. A caller that keeps its evidence dict must not be
    # able to rewrite stored history through the returned record.
    evidence = dict(observation.evidence)

    def event() -> FindingEvent:
        return FindingEvent(
            key=observation.key,
            at=now,
            state=state,
            summary=observation.summary,
            evidence=evidence,
        )

    if finding is None:
        if state is not FindingState.ACTIVE:
            # A recovery with nothing to recover is not news, and "could not
            # judge" is not itself a finding. Recording either would let the
            # absence of readings outnumber the readings.
            return None, None
        created = Finding(
            key=observation.key,
            summary=observation.summary,
            state=FindingState.ACTIVE,
            first_observed=now,
            last_observed=now,
            last_evaluated=now,
            occurrences=1,
            resolved_at=None,
            evidence=evidence,
        )
        return created, event()

    if state is FindingState.ACTIVE:
        # The condition holds again. It changes the record's counters, and it
        # is only news when the record said something else first: a finding
        # re-opening out of recovery or missing data gets an event, a repeat
        # of an already-active condition does not.
        reopen = finding.state is not FindingState.ACTIVE
        merged = replace(
            finding,
            state=FindingState.ACTIVE,
            last_observed=now,
            last_evaluated=now,
            occurrences=finding.occurrences + 1,
            resolved_at=None,
            summary=observation.summary,
            evidence=evidence,
        )
        return merged, (event() if reopen else None)

    if state is FindingState.RECOVERED:
        if finding.state is FindingState.RECOVERED:
            # Already recovered. The rule ran, there was nothing to resolve,
            # and the only fact worth keeping is the time of that run.
            return replace(finding, last_evaluated=now), None
        # A recovery resolves the record — even one last left as unassessable,
        # where the condition could not be tracked and now demonstrably is
        # gone. last_observed stays where the condition was last seen: this is
        # when it stopped, not when it was spotted.
        merged = replace(
            finding,
            state=FindingState.RECOVERED,
            resolved_at=now,
            last_evaluated=now,
            summary=observation.summary,
            evidence=evidence,
        )
        return merged, event()

    # Unassessable: missing readings mean the rule ran without judging, and
    # that is not a resolution. resolved_at is set only by a recovery, so a
    # gap in the data neither closes a finding nor lets one be quietly
    # forgotten — the record goes on showing the condition as unresolved.
    if finding.state is FindingState.UNASSESSABLE:
        # Already unassessable, and still no evidence either way.
        return replace(finding, last_evaluated=now), None
    merged = replace(
        finding,
        state=FindingState.UNASSESSABLE,
        last_evaluated=now,
        summary=observation.summary,
        evidence=evidence,
    )
    return merged, event()
