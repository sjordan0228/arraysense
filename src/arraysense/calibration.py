"""calibration.py — tell a drifting fuel gauge apart from a failing battery.

Every pack estimates its state of charge by counting amp-hours in and out. The
count has no way to correct itself, so a current too small for the BMS to see —
the inverter's own standby draw, split between the packs — accumulates as error
until something resets it. The only thing that resets it is the pack reaching
full, at which point its counter snaps to 100%.

On the reference bank the four packs read 57, 60, 62 and 76 percent while
sitting within 30 mV of each other. Wired in parallel they are forced to the
same voltage, and at the same voltage packs of the same size hold the same
charge, so those numbers cannot all be true. The batteries are equal; the
counters are not.

That distinction is the whole reason this module exists. A warning that says
"your batteries are drifting apart" would be a false claim, and users learn
quickly to ignore a channel that cries wolf. What it says instead is that the
readings have gone stale, and it separates out the one case that really is a
hardware fault: packs in parallel that do *not* agree on voltage, which means
a cable, a lug or a busbar rather than arithmetic.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

logger = logging.getLogger(__name__)

Severity = Literal["none", "info", "warning", "elevated", "alert"]

# How far below the BMS's own charge reference still counts as absorbing. The
# bank holds slightly under its target while current tapers, so an exact match
# would almost never be seen.
FULL_CHARGE_MARGIN_V = 0.5

# Used only when the inverter did not report a charge reference. It is the
# reference the EG4 PowerPro packs state, not a universal constant.
DEFAULT_CHARGE_REFERENCE_V = 56.0

# A pack has to hold at the reference for this long before any BMS treats the
# charge as complete. Shorter than this and a voltage surge would read as a
# full charge and silence the warning for a month.
MIN_ABSORB = timedelta(minutes=20)

# How far apart two samples may be and still count as the same unbroken absorb.
# This is load-bearing, not a tidiness measure. A run cannot be inferred from
# adjacency in a list, because the rows this reads are rollup rows: the coarse
# tiers are built with an ``error IS NULL`` filter and never carry the error
# column at all, so collection downtime is invisible there — it shows up as
# rows that are simply missing. Without an elapsed-time check, two one-minute
# voltage blips hours apart merge into one long "absorb" and a bank that has
# drifted for two months reports itself calibrated.
#
# Two minutes, not five. The tier this reads has one row per minute, so five
# minutes of tolerance silently bridges four missing rows — rows at 0, 5, 10,
# 15 and 20 minutes were being credited as one unbroken twenty-minute absorb
# when nothing at all is known about the four minutes between each of them.
# Two minutes still absorbs ordinary jitter and a single late write.
MAX_SAMPLE_GAP = timedelta(minutes=2)

# Charge current at the end of a window, above which the bank was still being
# pushed rather than sitting full. Voltage alone can be held high by charge
# current at well under full, so the taper is what distinguishes the two. Only
# applied when the current was actually reported.
FULL_CHARGE_TAPER_A = 25.0

# How stale a module's last reading may be before it stops counting as present.
# At an eleven-second poll this is some eighty missed reads, which is a dropped
# CAN link rather than jitter. Without it a pack that fell off the bus keeps
# contributing its final voltage forever.
PACK_REPORT_MAX_AGE = timedelta(minutes=15)

# How far apart two packs' readings may be and still be compared with each
# other. Much tighter than the presence window above, and for a different
# reason: presence asks "is this pack alive", while a voltage spread asks "do
# these packs disagree *at the same instant*". A fourteen-minute-old 53.50 V
# against a live 53.80 V is a 300 mV spread that never existed — and it would
# raise the wiring alarm, sending the owner after a lug over a pack that simply
# stopped answering. Two minutes is a handful of polls.
PACK_COMPARE_MAX_SKEW = timedelta(minutes=2)

# What a pack has to report for its counter to be considered reset. Not 100:
# the BMS reports whole percent to an accuracy of five, and a pack that settles
# at 99 would otherwise be treated as never having recalibrated.
PACK_RECALIBRATED_SOC = 99.0

# The hold required of a bank whose packs are doing the arguing. The reference
# installation crosses absorb, finishes and tapers to zero in about three
# minutes — sixteen raw samples above the bar at an eleven-second poll, four
# rows of the minute tier — so ``MIN_ABSORB`` cannot be met there at all, and
# the sixty-day search came back empty with the charge sitting in the history.
#
# One minute is two consecutive rows of the tier this scans, so no single row
# can carry a window. That is the whole of what this rules out, and it is worth
# being exact about: two crossed replies in adjacent minutes would still make
# one. What makes it unlikely rather than impossible is that a minute row is
# ROUND(AVG()) over the raw samples in that minute — three to five of them on
# this installation — so one bad decode moves the mean by a fraction of its own
# error. Duration is not what makes this safe; see ``bank_recalibrated_at``.
CORROBORATING_ABSORB = timedelta(minutes=1)

# How far either side of an absorb the packs are read, and it is used for two
# different things. *After*: how long a pack may still snap its counter and count
# as part of the same charge — on 8 August 2026 three packs reset two minutes
# into the absorb and the fourth three minutes after the inverter's terminal
# voltage had fallen back below the reference, so a search confined to the
# voltage window misses the pack that completes the set. *Before*: how far back
# the below-full evidence may be read, where the binding measurement is that the
# slowest pack's last reading under 99% came 2 min 19 s before the absorb opened.
# Fifteen minutes covers both with room for the round-robin slot rotation, while
# staying far short of the hours between one charge and the next — and it is
# capped at the previous absorb so one charge cannot vouch for the next.
PACK_RESET_LAG = timedelta(minutes=15)

# Days since the last full charge, and what each means. Seven is early enough
# to be useful and quiet enough to ignore. Fourteen is roughly where this
# hardware's drift reaches the ten points EG4's own manual stops calling
# normal. Thirty is where the number on screen is no longer good enough to make
# a decision from, so it stops being presented as a measurement.
INFO_AFTER_DAYS = 7.0
WARNING_AFTER_DAYS = 14.0
ESTIMATE_AFTER_DAYS = 30.0

# Terminal voltage spread that stops being arithmetic and starts being
# hardware. Parallel packs are physically forced to the same voltage; a quarter
# of a volt between them is resistance somewhere it should not be.
VOLTAGE_SPREAD_ALARM_MV = 200.0


@dataclass(frozen=True)
class CalibrationStatus:
    """What the dashboard needs to say about the trustworthiness of state of charge.

    ``soc_is_estimate`` is the field that changes how numbers are drawn rather
    than merely what a badge says: once set, per-pack percentages are labelled
    as estimates, because by then they are not good enough to act on.

    ``wiring_suspect`` is deliberately separate from the drift ladder. It
    describes a different failure with a different cause and a different fix,
    and folding the two into one message is the difference between a diagnosis
    and a nag.
    """

    severity: Severity
    # What the drift ladder said, independently of any wiring fault. A wiring
    # alert takes over the headline because it is the more urgent thing and
    # needs a different action — but it must not erase the fact that the
    # counters are also stale, which an earlier version did.
    drift_severity: Severity
    days_since: float | None
    last_full_charge: datetime | None
    searched_days: float
    soc_is_estimate: bool
    wiring_suspect: bool
    soc_spread_pct: float | None
    voltage_spread_mv: float | None
    headline: str
    detail: str


def _charge_reference(row: dict[str, Any]) -> float:
    """Return the absorb voltage this row should be judged against.

    Taken from the BMS's own stated charge reference where it reported one. A
    bank configured to a lower charge voltage still reaches its own full, and a
    hard-coded 56 V would mean such a bank never registered a full charge at
    all.

    """
    ref = row.get("bms_charge_voltage_ref_v")
    if not isinstance(ref, int | float):
        ref = DEFAULT_CHARGE_REFERENCE_V
    return float(ref) - FULL_CHARGE_MARGIN_V


def full_charge_windows(
    rows: Sequence[dict[str, Any]],
    min_absorb: timedelta = MIN_ABSORB,
    max_gap: timedelta = MAX_SAMPLE_GAP,
    taper_a: float = FULL_CHARGE_TAPER_A,
) -> list[tuple[datetime, datetime]]:
    """Find every period the bank spent held at its charge reference and settled there.

    Continuity is decided by elapsed time, never by adjacency in the list. That
    distinction is the whole safety of this function. The rows it is given come
    from a rollup tier, and the coarse tiers are built with an ``error IS NULL``
    filter and carry no error column, so a collector restart, a network blip or
    a spell of yield mode leaves no marker at all — the rows are simply absent.
    Trusting adjacency would splice two brief voltage excursions hours apart
    into one long absorb, declare a full charge that never happened, and
    silence the drift warning for a month.

    A run also has to end settled. Charge current can hold the bank above its
    reference at well under full, so a window whose final sample is still
    pushing more than ``taper_a`` is a charge in progress rather than a bank
    that reached the top. Current is only judged when the inverter reported it.

    ``rows`` are inverter history in ascending time order, each carrying
    ``timestamp`` and ``battery_voltage_v``, and optionally
    ``bms_charge_voltage_ref_v``, ``battery_current_a`` and ``error``. The
    windows come back oldest first.
    """
    windows: list[tuple[datetime, datetime]] = []
    start: datetime | None = None
    last: datetime | None = None
    last_current: float | None = None

    def close() -> None:
        """Bank the run in progress if it was long enough and ended settled."""
        if start is None or last is None or last - start < min_absorb:
            return
        if last_current is not None and abs(last_current) > taper_a:
            logger.debug("absorb run ending %s discarded: still at %.1f A", last, last_current)
            return
        windows.append((start, last))

    for row in rows:
        volts = row.get("battery_voltage_v")
        when = row.get("timestamp")
        absorbing = (
            row.get("error") is None
            and isinstance(when, datetime)
            and isinstance(volts, int | float)
            and float(volts) >= _charge_reference(row)
        )
        if not absorbing or not isinstance(when, datetime):
            close()
            start = last = last_current = None
            continue
        if last is not None and when - last > max_gap:
            # Time passed that we have no readings for. What the bank did in
            # the hole is unknown, so the run ends here and a new one begins.
            #
            # This is the only thing that ends a run for silence now. The store
            # stopped returning rows carrying none of the asked-for metrics —
            # the weather poller writes into the same tier and its rows have no
            # battery columns — so a minute the bank said nothing arrives as a
            # hole in time rather than as a row of nulls that ended the run
            # outright. A silence shorter than max_gap is therefore bridged
            # where it used to split. That is the rule this function already
            # states, applied consistently, but the direction is worth naming:
            # it errs toward believing a charge completed across a short
            # silence, where before it erred toward discarding a real one.
            close()
            start = None
        if start is None:
            start = when
        last = when
        current = row.get("battery_current_a")
        last_current = float(current) if isinstance(current, int | float) else None

    close()
    return windows


def packs_recalibrated(
    module_rows: Sequence[dict[str, Any]], expected: Sequence[str] | None = None
) -> bool:
    """Whether every pack in the bank reached full and reset its counter.

    Each pack is judged on the highest state of charge it reported, because the
    reset is an instant rather than a state — a pack that touched 100% and then
    began discharging has still recalibrated.

    Silence never counts as consent. Empty input is False, and when ``expected``
    names the packs the bank is known to have, a pack that said nothing during
    the window fails the check as surely as one that reported 80%. Both
    directions matter: a CAN dropout on a single pack during a charge would
    otherwise restart the drift clock for the whole bank, and the pack whose
    counter genuinely never reset is precisely the one then reported as
    calibrated for as long as it keeps drifting.

    ``module_rows`` covers one absorb window, each row carrying ``serial`` and
    ``soc_pct``. ``expected`` names the serials the bank is known to contain;
    when given, every one of them must have reported full.
    """
    peak: dict[str, float] = {}
    for row in module_rows:
        soc = row.get("soc_pct")
        if not isinstance(soc, int | float):
            continue
        serial = str(row["serial"])
        peak[serial] = max(peak.get(serial, float(soc)), float(soc))
    if not peak:
        return False
    if expected is not None and not set(expected).issubset(peak):
        logger.debug("packs silent through window: %s", sorted(set(expected) - set(peak)))
        return False
    return all(soc >= PACK_RECALIBRATED_SOC for soc in peak.values())


def _placeable(when: object, anchor: datetime) -> bool:
    """Whether a row's timestamp can be compared against this search's own clock.

    Mixing an aware timestamp with a naive one raises rather than sorting, and
    this module is reached from an HTTP endpoint where that is a 500 rather than
    a wrong answer. Every store read hands back aware timestamps, so this only
    fires on rows a caller assembled by hand — and a reading that cannot be
    placed in time is unusable, which is the treatment a reading with no state
    of charge already gets.
    """
    return isinstance(when, datetime) and (when.tzinfo is None) == (anchor.tzinfo is None)


def bank_recalibrated_at(
    module_rows: Sequence[dict[str, Any]],
    since: datetime,
    expected: Sequence[str] | None = None,
    skew: timedelta = PACK_COMPARE_MAX_SKEW,
) -> datetime | None:
    """Return the instant every pack in the bank was first seen *arriving* at full together.

    This asks a different question from the twenty-minute hold rather than a
    weaker one. One counter reading 100% proves nothing — that is drift, the
    condition being detected — so the evidence demanded here is a transition,
    per pack, and all of them at once:

    * every pack in the roster **measured below** ``PACK_RECALIBRATED_SOC``
      somewhere in the rows before ``since``, and
    * every pack in the roster **at or above** it at one instant at or after
      ``since``, each reading no more than ``skew`` old at that instant.

    Both halves are per pack, and an earlier version of this function asked only
    that *some* pack had been below. That was not enough, and the endpoint was
    shown to credit a bank whose three drifted counters sat pegged at 100% while
    the fourth genuinely charged: the roster all read full, one of them had
    previously been below, and three quarters of the percentages the dashboard
    then drew as measurements were stale. A charge resets every counter, so
    every counter has to show it.

    Simultaneity is the other half of why drift cannot fake this. Drift is slow
    — the ladder below is calibrated on this hardware taking a fortnight to open
    ten points, which is under a point a day — so it cannot carry a bank across
    the bar inside a couple of minutes. It is also why the packs must be full at
    one *instant* rather than have peaked somewhere in a range the way
    ``packs_recalibrated`` does; peaks scattered across half an hour are the
    drift signature itself.

    Requiring the *spread* to collapse by some number of points would have
    matched the reference installation and then failed on the next one, because
    a bank charged nightly has no spread left to collapse. Per-pack transitions
    say the same thing without a threshold nobody can set.

    What this cannot distinguish is a transition a charge did not cause: a pack
    swapped for another carrying the same serial, a firmware update that resets
    a counter, or a pack that dropped off the bus below full and came back at
    100. Each needs the bank to be at its charge reference with the current
    settled at the same moment to be credited at all, which is what bounds them
    — but none is ruled out, and the first two would be indistinguishable from a
    charge in this data.

    ``module_rows`` covers the charge and the minutes either side of it, each
    row carrying ``timestamp``, ``serial`` and ``soc_pct``. Rows before ``since``
    are read *only* for the below-full half; a pack's qualifying reading has to
    fall inside the charge, or the whole transition could happen before the bank
    ever reached its reference and the absorb would be decoration rather than
    corroboration.

    ``expected`` names the serials the bank is known to hold, and a pack that
    said nothing then fails as surely as one that read 80%. Omit it and the
    roster is inferred from these rows, which means a pack silent through the
    whole range is not merely unproven but invisible — the endpoint passes the
    serials from the store for that reason.
    """
    stamped = sorted(
        (
            row
            for row in module_rows
            if _placeable(row.get("timestamp"), since)
            and isinstance(row.get("soc_pct"), int | float)
        ),
        key=lambda row: row["timestamp"],
    )
    roster = set(expected) if expected is not None else {str(row["serial"]) for row in stamped}
    if not roster:
        return None

    below: set[str] = set()
    latest: dict[str, tuple[datetime, float]] = {}
    for row in stamped:
        when: datetime = row["timestamp"]
        serial = str(row["serial"])
        soc = float(row["soc_pct"])
        if when < since:
            if serial in roster and soc < PACK_RECALIBRATED_SOC:
                below.add(serial)
            continue
        latest[serial] = (when, soc)
        if not roster.issubset(below) or not roster.issubset(latest):
            continue
        if all(
            when - latest[s][0] <= skew and latest[s][1] >= PACK_RECALIBRATED_SOC for s in roster
        ):
            return when
    logger.debug(
        "no bank-wide reset after %s: %d of %d packs seen below full first, %d reporting after",
        since,
        len(below),
        len(roster),
        len(latest),
    )
    return None


def charge_completed_at(
    start: datetime,
    end: datetime,
    module_rows: Sequence[dict[str, Any]],
    expected: Sequence[str] | None = None,
    min_absorb: timedelta = MIN_ABSORB,
    lag: timedelta = PACK_RESET_LAG,
    after: datetime | None = None,
) -> datetime | None:
    """Decide whether one absorb window was a completed charge, and when it completed.

    Two doors, and a bank only has to pass one. The first is the original: the
    bank held at its charge reference for ``min_absorb`` and every pack peaked
    at full inside that window. The second asks nothing of duration and
    everything of the packs — see ``bank_recalibrated_at`` — and exists because
    the reference hardware finishes a charge in three minutes and would
    otherwise never register one.

    Both can pass at once, and the long door runs first so that its answer wins
    when they do. That is not a safety property, it is a choice about which
    instant to report: a bank already reading full when it charges again has no
    transition for the short door to find, and the long door credits it without
    needing one.

    ``after`` is the end of the previous candidate window, and the lookback
    never reaches back past it. Two absorb touches closer together than ``lag``
    would otherwise let the second borrow the first charge's below-full rows and
    be credited as the more recent completed charge, on evidence that was never
    about it.

    ``module_rows`` spans ``start - lag`` to ``end + lag``; this slices out the
    window itself for the first door. Neither door returns the reset itself,
    which nothing observes: the long door returns the end of the absorb, and the
    short door the first instant at which the whole bank was seen to have
    arrived. Each is the tightest bound the data supports, and both are late
    rather than early.
    """
    within = [
        row
        for row in module_rows
        if _placeable(row.get("timestamp"), start) and start <= row["timestamp"] <= end
    ]
    if end - start >= min_absorb and packs_recalibrated(within, expected=expected):
        return end
    # Bounded at both ends. A pack measured below full a month ago says nothing
    # about whether this charge was a transition, and an open lookback would let
    # ancient rows vouch for a bank that has been pegged at 100% for weeks.
    lookback_from = start - lag
    if after is not None and _placeable(after, start):
        lookback_from = max(lookback_from, after)
    nearby = [
        row
        for row in module_rows
        if _placeable(row.get("timestamp"), start)
        and lookback_from <= row["timestamp"] <= end + lag
    ]
    return bank_recalibrated_at(nearby, start, expected=expected)


def last_full_charge(
    inverter_rows: Sequence[dict[str, Any]],
    module_rows: Sequence[dict[str, Any]],
    min_absorb: timedelta = MIN_ABSORB,
) -> datetime | None:
    """Return when the bank last genuinely reached full, or None if never seen.

    The inverter's own terminals and the packs' own counters are both required,
    and neither is sufficient. Absorb voltage without the packs agreeing is a
    charge that was cut short; packs reading 100% with no absorb behind them are
    counters that have drifted high, which is the condition being detected
    rather than evidence against it. What the two halves are allowed to trade
    off is duration: a bank that holds at its reference for twenty minutes needs
    only every pack to have peaked full in that time, while a bank that finishes
    in three needs the packs seen arriving at full together.

    ``module_rows`` wants to reach ``PACK_RESET_LAG`` beyond ``inverter_rows`` at
    both ends rather than merely match it, because the short door reads the
    quarter hour before a charge for its below-full evidence and the quarter hour
    after it for the last pack to snap. A charge sitting against either edge of
    the data can therefore go uncredited for want of the margin — which is the
    safe direction, and the reason the endpoint widens its own module reads.

    ``min_absorb`` is the hold the *long* door asks for and nothing else; the
    short door has its own, which is only long enough that no single row can
    carry a window. What comes back is the best bound on the reset in the most
    recent qualifying charge, never earlier than the reset itself.
    """
    windows = full_charge_windows(inverter_rows, CORROBORATING_ABSORB)
    for index in reversed(range(len(windows))):
        start, end = windows[index]
        when = charge_completed_at(
            start,
            end,
            module_rows,
            min_absorb=min_absorb,
            after=windows[index - 1][1] if index else None,
        )
        if when is not None:
            return when
        logger.debug("absorb window %s to %s credited no reset", start, end)
    return None


def _current(
    modules: Sequence[dict[str, Any]], now: datetime, max_age: timedelta
) -> list[dict[str, Any]]:
    """Keep only the packs that have reported recently enough to be compared.

    The rows arrive from a "latest per module" query, which has no time bound
    at all — a pack that fell off the CAN bus a week ago still returns its final
    reading, forever. Comparing that against three live packs is not a
    comparison of anything, and it fires the loudest alarm in the module over a
    voltage nobody measured today.

    A row with no timestamp is kept. Callers that assemble rows by hand rather
    than from the store have nothing to be stale against.

    """
    fresh = []
    for row in modules:
        when = row.get("timestamp")
        if isinstance(when, datetime) and now - when > max_age:
            logger.debug("pack %s last reported %s; excluded as stale", row.get("serial"), when)
            continue
        fresh.append(row)
    return fresh


def _simultaneous(
    modules: Sequence[dict[str, Any]], skew: timedelta = PACK_COMPARE_MAX_SKEW
) -> list[dict[str, Any]]:
    """Keep only the packs read at close enough to the same moment to compare.

    A spread asks whether the packs disagree *at one instant*. Packs read
    minutes apart have not been compared at all, and the difference between
    them is elapsed time rather than a measurement — a fourteen-minute-old
    53.50 V beside a live 53.80 V looks like 300 mV of resistance and would
    raise the wiring alarm over a pack that simply stopped answering.

    Anchored to the newest reading rather than to the clock, so a bank whose
    readings are all equally old is still compared with itself.
    """
    stamped = [r for r in modules if isinstance(r.get("timestamp"), datetime)]
    if not stamped:
        return list(modules)
    newest = max(r["timestamp"] for r in stamped)
    kept = []
    for row in modules:
        when = row.get("timestamp")
        if isinstance(when, datetime) and newest - when > skew:
            logger.debug(
                "pack %s read %s before the newest; not comparable",
                row.get("serial"),
                newest - when,
            )
            continue
        kept.append(row)
    return kept


def _spreads(
    modules: Sequence[dict[str, Any]],
) -> tuple[float | None, float | None]:
    """Return the state-of-charge spread in points and the voltage spread in millivolts.

    Both are None unless at least two packs reported the value. A pack silent
    because its CAN link dropped is unknown, not zero, and folding an absent
    pack in as 0% would manufacture a sixty-point drift out of a dropout.

    Returns the state-of-charge spread first and the voltage spread second —
    an order the tuple type cannot express, and one worth getting right since
    both are floats and confusing them swaps points for millivolts.
    """
    socs = [float(m["soc_pct"]) for m in modules if isinstance(m.get("soc_pct"), int | float)]
    volts = [float(m["voltage_v"]) for m in modules if isinstance(m.get("voltage_v"), int | float)]
    soc_spread = round(max(socs) - min(socs), 1) if len(socs) > 1 else None
    volt_spread = round((max(volts) - min(volts)) * 1000, 1) if len(volts) > 1 else None
    return soc_spread, volt_spread


def assess(
    now: datetime,
    last_full: datetime | None,
    searched_days: float,
    modules: Sequence[dict[str, Any]],
) -> CalibrationStatus:
    """Judge how far the state-of-charge readings have drifted, and say so in words.

    A wide terminal-voltage spread takes over the message, because parallel
    packs are forced to the same voltage and packs that disagree on it have a
    hardware problem no amount of charging will fix. It does not take over the
    verdict. The drift ladder still runs underneath, so a bank with both a bad
    lug and four months of drift still marks its per-pack percentages as
    estimates — that being the one situation where they are least worth
    trusting, and the one where an early version of this function called them
    measurements.

    Args:
        now: the current time, timezone-aware.
        last_full: when the bank last reached full, or None if no full charge
            was found in the history that was searched.
        searched_days: how far back the search looked. Reported rather than
            assumed, so "not seen" is never presented as "never happened".
        modules: the latest row per battery module. Packs that have not
            reported recently are dropped before anything is compared.
    """
    reporting = _current(modules, now, PACK_REPORT_MAX_AGE)
    # Present is not the same as comparable: a spread between readings taken
    # minutes apart is elapsed time wearing the shape of a measurement.
    soc_spread, volt_spread = _spreads(_simultaneous(reporting))
    wiring = volt_spread is not None and volt_spread >= VOLTAGE_SPREAD_ALARM_MV
    days = (now - last_full).total_seconds() / 86400 if last_full is not None else None

    if days is None:
        severity: Severity = "elevated"
    elif days >= ESTIMATE_AFTER_DAYS:
        severity = "elevated"
    elif days >= WARNING_AFTER_DAYS:
        severity = "warning"
    elif days >= INFO_AFTER_DAYS:
        severity = "info"
    else:
        severity = "none"

    estimate = severity == "elevated"

    if wiring and volt_spread is not None:
        drift_note = ""
        if severity != "none":
            drift_note = (
                f" Separately, it has been {days:.0f} days since the bank last reached full,"
                " so the per-pack percentages are stale as well."
                if days is not None
                else " Separately, no full charge was found in the searched history,"
                " so the per-pack percentages are stale as well."
            )
        return CalibrationStatus(
            severity="alert",
            drift_severity=severity,
            days_since=days,
            last_full_charge=last_full,
            searched_days=searched_days,
            soc_is_estimate=estimate,
            wiring_suspect=True,
            soc_spread_pct=soc_spread,
            voltage_spread_mv=volt_spread,
            headline="Battery packs disagree on voltage",
            detail=(
                f"{volt_spread:.0f} mV between packs. Parallel packs are forced to the same "
                "voltage, so this is resistance in a cable, a lug or a busbar — or a failing "
                "pack. Charging will not fix it; check the connections." + drift_note
            ),
        )

    same_charge = (
        soc_spread is not None
        and volt_spread is not None
        and soc_spread > 5.0
        and volt_spread < 50.0
    )

    if days is None:
        headline = "State of charge maybe drifting"
        detail = (
            f"No full charge found in the last {searched_days:.0f} days of history. "
            "Charging the bank to 100% resets each pack's counter."
        )
    elif severity == "none":
        headline = "State of charge is calibrated"
        detail = f"Bank last reached full {days:.0f} days ago."
    else:
        headline = "State of charge estimates are drifting"
        detail = (
            f"{days:.0f} days since the bank last reached full. Each pack estimates charge by "
            "counting amp-hours and cannot correct itself until it charges fully. "
            "Charge to 100% and let it finish — the slow part above 95% is the part that counts."
        )

    if same_charge:
        detail += (
            f" The packs differ by {soc_spread:.0f} points but only {volt_spread:.0f} mV, "
            "so they hold the same charge and it is the counters that disagree."
        )

    return CalibrationStatus(
        severity=severity,
        drift_severity=severity,
        days_since=days,
        last_full_charge=last_full,
        searched_days=searched_days,
        soc_is_estimate=estimate,
        wiring_suspect=False,
        soc_spread_pct=soc_spread,
        voltage_spread_mv=volt_spread,
        headline=headline,
        detail=detail,
    )
