"""charge.py — decoding the inverter's AC-charge configuration.

Pure: no transport, no I/O, no clock. The register map is transcribed from
``pylxpweb/constants/registers.py``, which is documented against an 18kPV
and must be confirmed against this installation's own unit before anything
writes. This module describes what the device answered and nothing else: a
register nobody read decodes to None, never to a value that claims the
setting is off.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

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
