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


def last_full_charge(
    inverter_rows: Sequence[dict[str, Any]],
    module_rows: Sequence[dict[str, Any]],
    min_absorb: timedelta = MIN_ABSORB,
) -> datetime | None:
    """Return when the bank last genuinely reached full, or None if never seen.

    Both halves of the evidence are required. The bank must have been held at
    its charge reference, and every pack that reported during that period must
    have read full. Either alone gives a wrong answer: absorb voltage without
    the packs agreeing means the charge was cut short, and a pack reading 100%
    outside an absorb window is a pack whose counter has drifted high, which is
    precisely the condition being detected rather than evidence against it.

    ``module_rows`` spans the same range as ``inverter_rows``, each row
    carrying ``timestamp``, ``serial`` and ``soc_pct``. What comes back is the
    *end* of the most recent qualifying charge, since that is the instant the
    counters reset.
    """
    for start, end in reversed(full_charge_windows(inverter_rows, min_absorb)):
        within = [
            row
            for row in module_rows
            if row.get("timestamp") is not None and start <= row["timestamp"] <= end
        ]
        if packs_recalibrated(within):
            return end
        logger.debug("absorb window %s to %s rejected: %d module rows", start, end, len(within))
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
        headline = "State of charge may be drifting"
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
