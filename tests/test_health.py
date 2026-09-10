"""test_health.py — what a finding is, and what changed about it.

Three distinctions this file refuses to let the merge blur. A condition being
seen and a rule being run are different events: missing readings let the rule
run with nothing to judge, and that is not a recovery. A finding is one record
that survives recurrence, not one row per poll. And an event records a change
of state — a repeat of what a finding already is must leave history alone.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arraysense.health import (
    Finding,
    FindingKey,
    FindingState,
    Observation,
    merge_observation,
)

T0 = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(hours=6)
DAY = timedelta(days=1)

SUMMARY = "SoC drift above threshold on BMS-0421."
EVIDENCE: dict[str, object] = {"drift_pct": 5.0}

KEY = FindingKey("battery.soc_drift", 1, "pack", "BMS-0421")
KEY_V2 = FindingKey("battery.soc_drift", 2, "pack", "BMS-0421")


def _observe(
    state: FindingState,
    at: datetime,
    key: FindingKey = KEY,
    summary: str = SUMMARY,
) -> Observation:
    return Observation(key, state, at, summary, dict(EVIDENCE))


def _stored(
    state: FindingState,
    *,
    first: datetime = T0,
    last: datetime = T0,
    evaluated: datetime = T0,
    occurrences: int = 1,
    resolved: datetime | None = None,
    key: FindingKey = KEY,
) -> Finding:
    return Finding(
        key,
        SUMMARY,
        state,
        first,
        last,
        evaluated,
        occurrences,
        resolved,
        dict(EVIDENCE),
    )


def test_a_first_active_observation_creates_a_finding_and_one_event() -> None:
    finding, event = merge_observation(None, _observe(FindingState.ACTIVE, T0))

    assert finding is not None
    assert finding.state is FindingState.ACTIVE
    assert finding.first_observed == T0
    assert finding.last_observed == T0
    assert finding.last_evaluated == T0
    assert finding.occurrences == 1
    assert finding.resolved_at is None
    assert finding.evidence == EVIDENCE

    assert event is not None
    assert event.key == KEY
    assert event.at == T0
    assert event.state is FindingState.ACTIVE
    assert event.summary == SUMMARY


def test_a_repeated_active_observation_updates_the_same_finding() -> None:
    finding = _stored(FindingState.ACTIVE, first=T0 - DAY, last=T0)

    merged, event = merge_observation(finding, _observe(FindingState.ACTIVE, LATER))

    assert merged is not None
    assert merged.occurrences == 2
    assert merged.first_observed == T0 - DAY
    assert merged.last_observed == LATER
    assert merged.resolved_at is None
    assert event is None


def test_recovery_records_when_it_resolved_and_keeps_the_history() -> None:
    finding = _stored(FindingState.ACTIVE, first=T0 - DAY, last=T0, occurrences=2)

    merged, event = merge_observation(finding, _observe(FindingState.RECOVERED, LATER))

    assert merged is not None
    assert merged.state is FindingState.RECOVERED
    assert merged.resolved_at == LATER
    assert merged.first_observed == T0 - DAY
    assert merged.last_observed == T0
    assert merged.occurrences == 2
    assert event is not None
    assert event.state is FindingState.RECOVERED


def test_a_missing_measurement_never_reads_as_recovery() -> None:
    finding = _stored(FindingState.ACTIVE, occurrences=2)

    merged, event = merge_observation(finding, _observe(FindingState.UNASSESSABLE, LATER))

    assert merged is not None
    assert merged.state is FindingState.UNASSESSABLE
    assert merged.resolved_at is None
    assert merged.occurrences == 2
    assert merged.last_observed == T0
    assert event is not None
    assert event.state is FindingState.UNASSESSABLE


def test_an_active_observation_reopens_a_recovered_finding() -> None:
    finding = _stored(
        FindingState.RECOVERED,
        first=T0 - DAY,
        last=T0 - timedelta(hours=6),
        occurrences=2,
        resolved=T0,
    )

    merged, event = merge_observation(finding, _observe(FindingState.ACTIVE, LATER))

    assert merged is not None
    assert merged.state is FindingState.ACTIVE
    assert merged.resolved_at is None
    assert merged.occurrences == 3
    assert event is not None
    assert event.state is FindingState.ACTIVE


def test_an_unassessable_evaluation_with_no_prior_finding_records_nothing() -> None:
    finding, event = merge_observation(None, _observe(FindingState.UNASSESSABLE, T0))

    assert finding is None
    assert event is None


def test_a_recovery_with_no_prior_finding_records_nothing() -> None:
    finding, event = merge_observation(None, _observe(FindingState.RECOVERED, T0))

    assert finding is None
    assert event is None


def test_merging_across_keys_raises() -> None:
    finding = _stored(FindingState.ACTIVE)
    observation = Observation(KEY_V2, FindingState.ACTIVE, LATER, SUMMARY, dict(EVIDENCE))

    with pytest.raises(ValueError) as raised:
        merge_observation(finding, observation)

    assert "rule_version=1" in str(raised.value)
    assert "rule_version=2" in str(raised.value)


def test_a_bumped_rule_version_is_a_different_identity() -> None:
    # Identity lives in the key and is decided before any merge: a store that
    # looks findings up by key never hands a version-2 observation the record
    # of version 1.
    assert FindingKey("battery.soc_drift", 1, "pack", "BMS-0421") == KEY
    assert KEY_V2 != KEY

    by_key: dict[FindingKey, Finding] = {KEY: _stored(FindingState.ACTIVE)}
    assert KEY_V2 not in by_key


def test_the_merge_does_not_mutate_or_alias_its_inputs() -> None:
    finding = _stored(FindingState.ACTIVE)
    observation = _observe(FindingState.RECOVERED, LATER)

    merged, _event = merge_observation(finding, observation)

    assert finding.state is FindingState.ACTIVE
    assert finding.resolved_at is None
    assert finding.evidence == EVIDENCE

    assert merged is not None
    merged.evidence["drift_pct"] = 99.0
    assert observation.evidence == EVIDENCE


def test_a_repeated_recovered_observation_only_advances_the_evaluation_time() -> None:
    finding = _stored(
        FindingState.RECOVERED,
        first=T0 - DAY,
        last=T0 - timedelta(hours=6),
        occurrences=2,
        resolved=T0,
    )

    merged, event = merge_observation(finding, _observe(FindingState.RECOVERED, LATER))

    assert merged is not None
    assert merged.state is FindingState.RECOVERED
    assert merged.last_evaluated == LATER
    assert merged.last_observed == T0 - timedelta(hours=6)
    assert merged.resolved_at == T0
    assert event is None


def test_an_already_unassessable_finding_keeps_its_last_observation() -> None:
    finding = _stored(FindingState.UNASSESSABLE, first=T0 - DAY, last=T0)

    merged, event = merge_observation(finding, _observe(FindingState.UNASSESSABLE, LATER))

    assert merged is not None
    assert merged.state is FindingState.UNASSESSABLE
    assert merged.last_observed == T0
    assert merged.resolved_at is None
    assert merged.last_evaluated == LATER
    assert event is None
