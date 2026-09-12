"""charge.py — the inverter's AC-charge configuration: how it reads, what a charge may cost.

Pure: no transport, no I/O, no clock. The register map is transcribed from
``pylxpweb/constants/registers.py``, which is documented against an 18kPV
and must be confirmed against this installation's own unit before anything
writes. In the decode half, this module says what the device answered and
nothing else: a register nobody read decodes to None, never to a value that
claims the setting is off. The planning half added below is the same kind of
file — arithmetic over numbers a caller supplies, deciding how hard a charge
may run and which periods it occupies. It asks the device nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

AC_CHARGE_ENABLE_REGISTER = 21
AC_CHARGE_ENABLE_BIT = 7
AC_CHARGE_POWER_REGISTER = 66
AC_CHARGE_STOP_SOC_REGISTER = 67
AC_CHARGE_WINDOW_START_REGISTER = 68  # three pairs: 68/69, 70/71, 72/73
AC_CHARGE_WINDOW_PERIODS = 3
AC_CHARGE_TYPE_REGISTER = 120
AC_CHARGE_TYPE_SHIFT = 1
AC_CHARGE_TYPE_MASK = 0x0E
AC_CHARGE_START_VOLTAGE_REGISTER = 158
AC_CHARGE_STOP_VOLTAGE_REGISTER = 159
AC_CHARGE_START_SOC_REGISTER = 160
AC_CHARGE_WINDOW_END_SOC_REGISTER = 161
POWER_COMMAND_WATTS = 100
SOC_LIMIT_NEVER_STOP = 101
SCHEDULE_TYPES = {0: "time", 1: "soc_voltage", 2: "time_and_soc_voltage"}

# The registers a charge writes, and therefore the ones an undo has to put back.
# One list, because two would drift: a restore that wrote a different set from
# the one the start changed is a change with no undo, and a stored record that
# is missing any of these is not an undo either — it is a partial copy that
# would leave the inverter half restored. Both the driver and the stored record
# read the list from here.
CHARGE_RESTORE_ADDRESSES: tuple[int, ...] = (
    AC_CHARGE_ENABLE_REGISTER,
    AC_CHARGE_POWER_REGISTER,
    AC_CHARGE_STOP_SOC_REGISTER,
    *range(
        AC_CHARGE_WINDOW_START_REGISTER,
        AC_CHARGE_WINDOW_START_REGISTER + 2 * AC_CHARGE_WINDOW_PERIODS,
    ),
)


@dataclass(frozen=True)
class ChargeWindow:
    """One configured charging period, as its two packed registers read."""

    start_hour: int
    start_minute: int
    end_hour: int
    end_minute: int

    @property
    def is_set(self) -> bool:
        """False for the all-zero window an inverter holds when none is configured."""
        return any((self.start_hour, self.start_minute, self.end_hour, self.end_minute))


@dataclass(frozen=True)
class ChargeConfig:
    """The AC-charge configuration as the device reported it.

    A None field means the register behind it was not read, which is a
    different fact from a register that answered zero: the first says nobody
    asked, the second says the device holds nothing.
    """

    ac_charge_enabled: bool | None
    power_w: int | None
    stop_soc_pct: int | None
    windows: tuple[ChargeWindow, ...]
    schedule_type: str
    start_soc_pct: int | None
    window_end_soc_pct: int | None
    start_voltage_v: float | None
    stop_voltage_v: float | None
    quick_charge_remaining_s: int | None
    registers: dict[int, int]
    read_at: datetime


def unpack_time(value: int) -> tuple[int, int]:
    """(hour, minute) from one packed window register: hour low byte, minute high byte."""
    return value & 0xFF, (value >> 8) & 0xFF


def decode_charge_config(
    registers: Mapping[int, int],
    *,
    quick_charge_remaining_s: int | None,
    read_at: datetime,
) -> ChargeConfig:
    """Describe the charge configuration one register read answered.

    The 100 W power command and the 101 "never stop" SOC limit are the
    device's own encodings and stay as read: whether to call them "1.2 kW"
    and "never" is a caller's presentation, not a fact this decode may add.
    A half-read window schedule decodes to no windows at all, because a
    partial schedule shown as a schedule would be three windows with a hole
    in one, which is what a page cannot check against the device's display.
    """
    enable = registers.get(AC_CHARGE_ENABLE_REGISTER)
    power = registers.get(AC_CHARGE_POWER_REGISTER)
    stop = AC_CHARGE_WINDOW_START_REGISTER
    window_end = stop + 2 * AC_CHARGE_WINDOW_PERIODS
    if any(address not in registers for address in range(stop, window_end)):
        windows: tuple[ChargeWindow, ...] = ()
    else:
        windows = tuple(
            ChargeWindow(
                *unpack_time(registers[stop + 2 * index]),
                *unpack_time(registers[stop + 2 * index + 1]),
            )
            for index in range(AC_CHARGE_WINDOW_PERIODS)
        )
    charge_type = registers.get(AC_CHARGE_TYPE_REGISTER)
    schedule_type = "unknown"
    if charge_type is not None:
        schedule_type = SCHEDULE_TYPES.get(
            (charge_type & AC_CHARGE_TYPE_MASK) >> AC_CHARGE_TYPE_SHIFT, "unknown"
        )
    start_v = registers.get(AC_CHARGE_START_VOLTAGE_REGISTER)
    stop_v = registers.get(AC_CHARGE_STOP_VOLTAGE_REGISTER)
    return ChargeConfig(
        ac_charge_enabled=(None if enable is None else bool(enable >> AC_CHARGE_ENABLE_BIT & 1)),
        power_w=None if power is None else power * POWER_COMMAND_WATTS,
        stop_soc_pct=registers.get(AC_CHARGE_STOP_SOC_REGISTER),
        windows=windows,
        schedule_type=schedule_type,
        start_soc_pct=registers.get(AC_CHARGE_START_SOC_REGISTER),
        window_end_soc_pct=registers.get(AC_CHARGE_WINDOW_END_SOC_REGISTER),
        # Tenths of a volt arrive as whole-register counts, so the decode
        # divides; a "46.04 V" would be precision the register does not hold.
        start_voltage_v=None if start_v is None else round(start_v / 10, 1),
        stop_voltage_v=None if stop_v is None else round(stop_v / 10, 1),
        quick_charge_remaining_s=quick_charge_remaining_s,
        # The raw answer travels with the decode so the confirmation step can
        # compare what the device said against its own display.
        registers=dict(registers),
        read_at=read_at,
    )


# The planning numbers. Defaults, not facts about the device: the site limit
# and the house's draw arrive as arguments, and each of these can be replaced
# by a caller that has read something better than a default.
GRID_CHARGE_DEFAULT_W = 3000  # the shipped request, ~8 kW of a 12 kW site with a 5 kW house
GRID_CHARGE_MIN_W = 1000  # below this a charge is not worth starting
GRID_CHARGE_MAX_W = 12000  # the hard ceiling a request can never raise
GRID_CHARGE_MARGIN_W = 1000  # headroom kept for the house to breathe
GRID_CHARGE_TARGET_SOC_PCT = 100  # what a charge-to-full charges to
# How old the house's own load reading may be before a charge is refused rather
# than sized against it. The collector polls every few seconds, so this is
# roughly "the collector is running"; an older number describes a house nobody
# is watching, and the site limit caps the charge rather than the charge plus
# the house, so sizing against a reading that old can put more on the site than
# the site was limited to.
CHARGE_LOAD_FRESHNESS = timedelta(minutes=5)
# How long the battery has to hold the charge's target before the charge counts
# as finished. The device stops *charging* at the target, but it holds the bank
# there and serves the house from the grid while the window is open, so the
# override has to be released rather than waited out: this is the settling time
# the owner asked for, not the hours the window allowed.
CHARGE_SETTLE = timedelta(minutes=10)
# How far below the target a pack may read and still count as full. A battery
# held at the top of charge reports the target and one point under it by turns
# while it balances — measured on the reference bank over one evening, 362
# readings at 99% against 100 at 100% — so requiring the exact integer would
# leave the override in place all night over a rounding, which is the complaint
# this rule exists to answer.
CHARGE_FULL_TOLERANCE_PCT = 1


@dataclass(frozen=True)
class ChargeLimits:
    """What the installation allows, and what the house is doing right now.

    ``house_load_w`` is None when the load could not be read, which is not the
    same fact as a quiet house. Read as a zero it would hand a charge the
    whole of the site's remaining capacity on top of whatever the house was
    already drawing, so an unread load is treated as a load nobody may ignore.
    """

    site_limit_w: int
    house_load_w: int | None
    max_power_w: int = GRID_CHARGE_MAX_W
    min_power_w: int = GRID_CHARGE_MIN_W
    margin_w: int = GRID_CHARGE_MARGIN_W


@dataclass(frozen=True)
class ChargeDecision:
    """The charge power a request may have, and the words for why that number.

    ``power_w`` None means do not start a charge, which is a different command
    from a small one and needs to be told apart from it.

    ``refused`` is not an error. It carries the difference between what was
    asked and what the installation allows, so an audit line can say "asked
    10000 W, allowed 6000 W" instead of recording a number nobody requested
    with no hint of where it came from.
    """

    power_w: int | None
    reason: str
    refused: str | None = None


def decide_charge_power(requested_w: int, limits: ChargeLimits) -> ChargeDecision:
    """The charge power this installation may actually draw.

    The site limit beats any configured ceiling, and what the house is drawing
    right now plus a margin comes off it before a charge is considered. This
    installation stores a 10 kW AC-charge command against a 12 kW site limit,
    and its house routinely draws 5 kW, so a window opened on the stored value
    would have pulled about 15 kW: the clamp is what makes that impossible
    rather than merely unlikely.

    The floor is compared against the already-clamped power and never used to
    raise it. With a 1000 W floor and 500 W of headroom, lifting the charge to
    the floor would command twice what the site had left. Two settings that can
    be typed in either order will eventually disagree, and when they do the
    lower bound wins, because exceeding a limit is the failure with a breaker
    on the other end of it.
    """
    # A ceiling is something an owner or a future packet typed. The site limit
    # is what the installation was built to carry, so it wins.
    cap = min(limits.max_power_w, limits.site_limit_w)

    if requested_w <= 0:
        return ChargeDecision(None, f"asked {requested_w} W, which is not a charge to start")

    house = limits.house_load_w
    # An unread load is not a zero load. The decision still needs a number to
    # run on, so it runs on the ceiling and says so: the owner is entitled to
    # see which of the two the plan used.
    headroom = cap if house is None else limits.site_limit_w - house - limits.margin_w

    if headroom <= 0:
        if house is None:
            return ChargeDecision(None, f"the {cap} W ceiling leaves no headroom for a charge")
        return ChargeDecision(
            None,
            f"the house is already drawing {house} W of the {limits.site_limit_w} W site limit",
        )

    power = min(requested_w, cap, headroom)
    if power < limits.min_power_w:
        return ChargeDecision(
            None,
            f"a charge under the {limits.min_power_w} W floor is not worth starting",
            f"asked {requested_w} W against {headroom} W of headroom, under the "
            f"{limits.min_power_w} W floor",
        )

    if power < requested_w:
        # Whichever bound cut the request is named by its own number, so the
        # reason reads as arithmetic rather than as a verdict.
        basis = f"{power} W the site had left" if headroom < cap else f"{cap} W ceiling"
        reason = f"asked {requested_w} W, held at the {basis}"
        refused = f"asked {requested_w} W, allowed {power} W"
    else:
        reason = f"charging at the requested {requested_w} W"
        refused = None
        if house is None:
            reason = f"{reason} under the {cap} W ceiling"
    if house is None:
        reason = f"house load was not available: {reason}"
    return ChargeDecision(power, reason, refused)


def pack_time(hour: int, minute: int) -> int:
    """One packed window register: hour low byte, minute high byte.

    The exact inverse of ``unpack_time``, so a window this module builds and a
    window it decodes are the same two numbers read in opposite directions.
    """
    return (hour & 0xFF) | ((minute & 0xFF) << 8)


def windows_for(start: datetime, end: datetime) -> tuple[ChargeWindow, ...]:
    """The one or two periods that cover [start, end) in the inverter's single-day schedule.

    The device holds one day's schedule with no date in it, so a run that
    crosses midnight is written as two periods — 22:00 to 23:59, then 00:00 to
    its end — instead of one period that only works if the register pair is
    read as a range running backwards through midnight. Nobody has confirmed
    that wrap-around on this hardware, and a charge that silently never opens
    is worse than one written twice.

    A span longer than a day raises rather than being truncated: the schedule
    cannot express it, and a shortened window would under-deliver a charge the
    owner asked for while looking like it was delivered.
    """
    # Daylight-saving offsets are the reason this asks for the offset and not
    # just a tzinfo: a bare name says nothing about which day the two instants
    # fall on, and the choice between one period and two is made on the day.
    if any(instant.tzinfo is None or instant.utcoffset() is None for instant in (start, end)):
        raise ValueError("a charge window needs timezone-aware instants")
    if end <= start:
        raise ValueError(f"a charge window must end after it starts, not at {end}")
    if end - start > timedelta(days=1):
        raise ValueError("the schedule holds one day, and this window is longer than that")
    if start.date() == end.date():
        return (ChargeWindow(start.hour, start.minute, end.hour, end.minute),)
    return (
        # 23:59 is the latest end the encoding carries. The minute it leaves
        # out is the price of not relying on a midnight wrap nobody has tested;
        # it is one minute, and it is on the side of charging less.
        ChargeWindow(start.hour, start.minute, 23, 59),
        ChargeWindow(0, 0, end.hour, end.minute),
    )


class ChargeWriteRefusedError(Exception):
    """The transport answered that it did not take a charge write."""


@dataclass(frozen=True)
class GridChargeChange:
    """What a charge write found, what it applied, and when it closes."""

    saved: ChargeConfig
    applied: ChargeConfig
    until: datetime
