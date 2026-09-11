"""test_charge.py — the inverter's AC-charge configuration and the charge plan.

No hardware and no serial port: the decode is driven with literal register
maps, and the driver method with an injected fake transport. That is the
point of the driver tests — a read path that also wrote registers would
undo every rule about confirming the register map against this installation's
own inverter before anything ships that changes it. The planning tests at the
end are the same kind of thing: arithmetic over literal numbers, with no clock
and no device, because the number they produce is the one a later packet
writes into the inverter.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pylxpweb.transports.exceptions import TransportError

from arraysense.charge import (
    GRID_CHARGE_DEFAULT_W,
    ChargeConfig,
    ChargeLimits,
    ChargeWindow,
    ChargeWriteRefusedError,
    decide_charge_power,
    decode_charge_config,
    pack_time,
    unpack_time,
    windows_for,
)
from arraysense.config import Config
from arraysense.drivers.eg4_luxpower.source import Eg4LuxPowerSource

READ_AT = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _packed(hour: int, minute: int) -> int:
    """One window register: hour in the low byte, minute in the high one."""
    return (hour & 0xFF) | ((minute & 0xFF) << 8)


def _decode(registers: dict[int, int], quick: int | None = None) -> ChargeConfig:
    return decode_charge_config(registers, quick_charge_remaining_s=quick, read_at=READ_AT)


# A fully configured unit: every register the read asks for, answered.
_FULL: dict[int, int] = {
    21: 0x0080,  # bit 7: AC charge enabled
    66: 12,  # 12 x 100 W
    67: 95,  # stop charging at 95 %
    68: _packed(2, 30),
    69: _packed(4, 0),
    70: 0,
    71: 0,
    72: _packed(13, 15),
    73: _packed(14, 45),
    120: 4,  # Time + SOC/Volt
    158: 460,  # 46.0 V
    159: 520,  # 52.0 V
    160: 20,
    161: 90,
}


def test_a_configured_ac_charge_decodes_from_its_registers() -> None:
    config = _decode(_FULL, quick=600)
    assert config.ac_charge_enabled is True
    assert config.power_w == 1200
    assert config.stop_soc_pct == 95
    assert config.windows == (
        ChargeWindow(2, 30, 4, 0),
        ChargeWindow(0, 0, 0, 0),
        ChargeWindow(13, 15, 14, 45),
    )
    assert config.windows[1].is_set is False
    assert config.schedule_type == "time_and_soc_voltage"
    assert config.start_voltage_v == pytest.approx(46.0)
    assert config.stop_voltage_v == pytest.approx(52.0)
    assert config.start_soc_pct == 20
    assert config.window_end_soc_pct == 90
    assert config.quick_charge_remaining_s == 600
    # The raw answer comes back with the decode, so the confirmation step can
    # compare what the device said against its own display.
    assert config.registers == _FULL


def test_the_power_command_is_in_hundred_watt_units() -> None:
    assert _decode({66: 1}).power_w == 100
    assert _decode({66: 150}).power_w == 15000


def test_a_stop_soc_of_101_is_kept_rather_than_translated() -> None:
    # 101 is the device's own "never stop" command, not a 101 percent
    # target. Whether to label it "never" is the caller's decision.
    assert _decode({67: 101}).stop_soc_pct == 101


def test_a_window_decodes_hour_and_minute_from_the_packed_pair() -> None:
    assert unpack_time(0x0A08) == (8, 10)


def test_an_all_zero_window_reads_as_not_set() -> None:
    assert ChargeWindow(0, 0, 0, 0).is_set is False
    assert ChargeWindow(8, 10, 9, 30).is_set is True


def test_an_unnamed_schedule_type_decodes_as_unknown() -> None:
    # 7 in the field is a mode the register map does not name; the docs say
    # the firmware may support modes the EG4 web UI never exposes.
    assert _decode({120: 0x0E}).schedule_type == "unknown"
    assert _decode({}).schedule_type == "unknown"


def test_a_register_that_was_not_read_decodes_as_unknown_not_as_off() -> None:
    # A missing register must not claim the feature is off or unconfigured;
    # only a read that answered says anything about what the device holds.
    config = _decode({})
    assert config == ChargeConfig(
        ac_charge_enabled=None,
        power_w=None,
        stop_soc_pct=None,
        windows=(),
        schedule_type="unknown",
        start_soc_pct=None,
        window_end_soc_pct=None,
        start_voltage_v=None,
        stop_voltage_v=None,
        quick_charge_remaining_s=None,
        registers={},
        read_at=READ_AT,
    )
    assert config.ac_charge_enabled is None
    assert config.registers == {}
    assert config.windows == ()


def test_a_half_read_schedule_does_not_decode_as_a_schedule() -> None:
    # Registers 68-72 arrived and 73 did not. Three pairs are one schedule;
    # a partial one would read as a schedule with a hole in it.
    config = _decode(
        {
            68: _packed(2, 30),
            69: _packed(4, 0),
            70: 0,
            71: 0,
            72: _packed(13, 15),
        }
    )
    assert config.windows == ()


class _ChargeTransport:
    """A stand-in transport that records register reads and refuses writes.

    write_parameters raises rather than quietly succeeding, and records
    anyway: this packet's guarantee is that nothing writes, and a silent
    write mid-read is exactly what that guarantee forbids. It answers only
    registers it was given, so a range the driver did not ask about stays
    empty. It carries no read_quick_charge_remaining_seconds, which is the
    shape of a transport that cannot answer that question.
    """

    def __init__(self, registers: dict[int, int]) -> None:
        self.registers = registers
        self.reads: list[tuple[int, int]] = []
        self.writes: list[dict[int, int]] = []

    async def read_parameters(self, start_address: int, count: int) -> dict[int, int]:
        self.reads.append((start_address, count))
        return {
            addr: self.registers[addr]
            for addr in range(start_address, start_address + count)
            if addr in self.registers
        }

    async def write_parameters(self, parameters: dict[int, int]) -> bool:
        self.writes.append(parameters)
        raise AssertionError("charge configuration is read here, never written")


def _driver(transport: object) -> Eg4LuxPowerSource:
    # The same injected-transport seam tests/test_eg4_luxpower_source.py
    # uses: a driver with no hardware behind it.
    return Eg4LuxPowerSource(
        Config(
            dongle_host="127.0.0.1",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path=":memory:",
            poll_interval=11.0,
        ),
        transport=transport,  # type: ignore[arg-type]
    )


async def test_the_driver_reads_only_the_registers_it_needs() -> None:
    transport = _ChargeTransport(_FULL)
    config = await _driver(transport).read_charge_config()
    # The exact question asked, not just an answer received: five blocks
    # that together cover registers 21, 66-67, 68-73, 120 and 158-161.
    assert transport.reads == [(21, 1), (66, 2), (68, 6), (120, 1), (158, 4)]
    assert transport.writes == []
    assert config.ac_charge_enabled is True
    assert config.quick_charge_remaining_s is None


async def test_a_transport_that_cannot_report_quick_charge_still_answers() -> None:
    # The fake has no read_quick_charge_remaining_seconds at all. A
    # transport that cannot answer one question must not fail the whole
    # read over it.
    transport = _ChargeTransport(_FULL)
    config = await _driver(transport).read_charge_config()
    assert config.quick_charge_remaining_s is None
    assert config.ac_charge_enabled is True
    assert config.power_w == 1200
    assert len(config.windows) == 3
    assert config.schedule_type == "time_and_soc_voltage"


# The planning half: how hard a charge may run and when its window opens. The
# numbers are literals a caller would hand over, and none of them come from a
# device. A stored 10 kW command beside a 5 kW house on a 12 kW site is what
# these rules exist to make impossible.


def test_pack_time_is_the_inverse_of_unpack_time() -> None:
    for hour, minute in ((0, 0), (2, 30), (8, 10), (13, 15), (23, 59)):
        assert unpack_time(pack_time(hour, minute)) == (hour, minute)
    # Midnight is the all-zero register an unset window holds, and 0x0A08 is
    # the 8:10 the decode above reads back.
    assert pack_time(0, 0) == 0
    assert pack_time(8, 10) == 0x0A08


def test_a_window_inside_one_day_is_one_period() -> None:
    start = datetime(2026, 8, 6, 2, 30, tzinfo=UTC)
    end = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
    assert windows_for(start, end) == (ChargeWindow(2, 30, 4, 0),)


def test_a_window_crossing_midnight_is_two_periods() -> None:
    start = datetime(2026, 8, 6, 22, 0, tzinfo=UTC)
    end = datetime(2026, 8, 7, 6, 0, tzinfo=UTC)
    # The schedule holds one day with no date in it, so a run over midnight is
    # written as two periods instead of relying on a wrap-around that nobody
    # has confirmed on this hardware.
    assert windows_for(start, end) == (
        ChargeWindow(22, 0, 23, 59),
        ChargeWindow(0, 0, 6, 0),
    )


def test_a_window_longer_than_a_day_is_refused() -> None:
    start = datetime(2026, 8, 6, 22, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        windows_for(start, start + timedelta(hours=32))


def test_a_reversed_or_empty_window_is_refused() -> None:
    late = datetime(2026, 8, 6, 6, 0, tzinfo=UTC)
    early = datetime(2026, 8, 6, 4, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        windows_for(late, early)
    with pytest.raises(ValueError):
        windows_for(late, late)


def test_a_window_is_refused_without_timezones() -> None:
    # A naive instant has no day of its own to compare, which is how
    # windows_for chooses between one period and two, so it raises rather than
    # guessing a UTC day and silently picking the wrong half of it. The second
    # call is the mixed pair: comparing the two would be a TypeError, and the
    # missing timezone is the fact worth reporting.
    naive_start = datetime(2026, 8, 6, 2, 30)
    with pytest.raises(ValueError):
        windows_for(naive_start, datetime(2026, 8, 6, 4, 0))
    with pytest.raises(ValueError):
        windows_for(naive_start, datetime(2026, 8, 6, 4, 0, tzinfo=UTC))


def test_the_default_request_is_three_kilowatts() -> None:
    assert GRID_CHARGE_DEFAULT_W == 3000
    decision = decide_charge_power(GRID_CHARGE_DEFAULT_W, ChargeLimits(12000, 0))
    assert decision.power_w == 3000
    assert decision.refused is None


def test_the_site_limit_caps_the_configured_ceiling() -> None:
    # A 12000 W ceiling typed over an 8000 W site limit is an 8000 W ceiling:
    # the site limit is the number with a breaker on the other end of it.
    limits = ChargeLimits(site_limit_w=8000, house_load_w=0, margin_w=0)
    decision = decide_charge_power(12000, limits)
    assert decision.power_w == 8000
    assert "12000" in decision.reason
    assert "8000" in decision.reason
    # With the 1000 W margin back the same quiet house leaves 7000 W. The
    # margin comes off the site limit, never off the request.
    assert decide_charge_power(12000, ChargeLimits(8000, 0)).power_w == 7000


def test_the_house_load_and_the_margin_are_subtracted() -> None:
    limits = ChargeLimits(site_limit_w=12000, house_load_w=5000, margin_w=1000)
    decision = decide_charge_power(10000, limits)
    # 12000 - 5000 - 1000 leaves 6000 W of headroom, and 10000 W was asked.
    assert decision.power_w == 6000
    assert "10000" in decision.reason
    assert "6000" in decision.reason


def test_a_house_already_at_the_limit_refuses_rather_than_trickling() -> None:
    limits = ChargeLimits(site_limit_w=12000, house_load_w=11500, margin_w=1000)
    decision = decide_charge_power(10000, limits)
    assert decision.power_w is None
    assert "11500" in decision.reason
    assert "12000" in decision.reason


def test_headroom_below_the_floor_refuses() -> None:
    # 500 W of headroom is above zero and under the 1000 W floor. The floor
    # refuses the charge; it never lifts it until it looks worthwhile, which
    # would command more than the site had left.
    limits = ChargeLimits(site_limit_w=12000, house_load_w=10500, margin_w=1000)
    decision = decide_charge_power(10000, limits)
    assert decision.power_w is None
    assert decision.refused is not None
    assert "500" in decision.refused
    assert "1000" in decision.refused
    assert "1000" in decision.reason


def test_an_unreadable_house_load_falls_back_to_the_ceiling_and_says_so() -> None:
    # An unread load is not a zero load: the decision runs on the ceiling and
    # names it, so the owner can tell which number produced this charge.
    unread = decide_charge_power(10000, ChargeLimits(site_limit_w=12000, house_load_w=None))
    assert unread.power_w == 10000
    assert "not available" in unread.reason
    # The ceiling is still a ceiling. Over an 8000 W site the same request is
    # held at 8000, not lifted to make the charge worth starting.
    tight_limits = ChargeLimits(site_limit_w=8000, house_load_w=None)
    tight = decide_charge_power(10000, tight_limits)
    assert tight.power_w == 8000
    assert tight.power_w is None or tight.power_w <= tight_limits.site_limit_w
    assert "not available" in tight.reason


def test_nothing_is_asked_for_nothing_is_refused_rather_than_started() -> None:
    limits = ChargeLimits(site_limit_w=12000, house_load_w=0)
    assert decide_charge_power(0, limits).power_w is None
    assert decide_charge_power(-500, limits).power_w is None


# The write path. These tests drive the two write methods through a fake that
# applies what it is given, so the read-back the driver performs sees the
# device's new state. The read-only fake above stays as it was: a start or a
# restore that skipped its read, or wrote a register it could not put back, is
# the exact failure the two methods exist to prevent.


_SEED: dict[int, int] = {
    21: 0x0002,  # bit 7 clear: AC charge disabled, one other bit set
    66: 12,  # 12 x 100 W
    67: 95,
    68: _packed(2, 30),
    69: _packed(4, 0),
    70: 0,
    71: 0,
    72: _packed(13, 15),
    73: _packed(14, 45),
}

_NOON = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class _WriteTransport:
    """A transport that applies a write to its own register map.

    read_parameters answers from the map; write_parameters commits the given
    values into it and reports whether it took them. That is the seam the write
    path needs: the driver reads back after it writes, and only a map that
    changes under a write can be read back. It carries no
    read_quick_charge_remaining_seconds, the shape of a transport that cannot
    answer that question.
    """

    def __init__(self, registers: dict[int, int]) -> None:
        self.registers = dict(registers)
        self.reads: list[tuple[int, int]] = []
        self.writes: list[dict[int, int]] = []
        self.accepts_writes = True
        self.read_error: Exception | None = None

    async def read_parameters(self, start_address: int, count: int) -> dict[int, int]:
        self.reads.append((start_address, count))
        if self.read_error is not None:
            raise self.read_error
        return {
            addr: self.registers[addr]
            for addr in range(start_address, start_address + count)
            if addr in self.registers
        }

    async def write_parameters(self, parameters: dict[int, int]) -> bool:
        self.writes.append(dict(parameters))
        if not self.accepts_writes:
            return False
        self.registers.update(parameters)
        return True


def _write_setup(
    registers: dict[int, int] | None = None,
) -> tuple[Eg4LuxPowerSource, _WriteTransport]:
    transport = _WriteTransport(_SEED if registers is None else registers)
    return _driver(transport), transport


def _written(transport: _WriteTransport, address: int) -> int:
    """The value one register was written with, out of the per-register writes.

    Every write the driver makes carries a single address, because this
    inverter answers a single-register write and ignores a batched one — so a
    test asking what went to register 66 has to look through the writes rather
    than index one dictionary that held them all.
    """
    for chunk in transport.writes:
        if address in chunk:
            return chunk[address]
    raise AssertionError(f"register {address} was never written: {transport.writes}")


async def test_a_start_saves_what_the_inverter_held_before_it_wrote() -> None:
    driver, _ = _write_setup()
    change = await driver.start_grid_charge(power_w=3050, duration_min=90, now=_NOON)
    # saved is the state read before anything was written: the original twelve
    # units of power and the schedule the device held, untouched.
    assert change.saved.registers == _SEED
    assert change.saved.power_w == 1200
    # applied is the read-back after the write, so it carries the new thirty-
    # unit command the write applied, not the 3050 W that was asked for.
    assert change.applied.registers[66] == 30
    assert change.applied.power_w == 3000
    assert change.until == _NOON + timedelta(minutes=90)


async def test_every_register_is_written_on_its_own() -> None:
    # One register per write, because this inverter answers a single-register
    # write and ignores a batched one: on 2026-09-10 the write to register 21
    # came back echoed and the eight-register write covering 66-73 went
    # unanswered after three retries, which is what made the first live attempt
    # fail with the inverter untouched.
    driver, transport = _write_setup()
    await driver.start_grid_charge(power_w=3050, duration_min=90, now=_NOON)
    assert len(transport.writes) == 9
    assert all(len(chunk) == 1 for chunk in transport.writes)
    assert {addr for chunk in transport.writes for addr in chunk} == {
        21,
        66,
        67,
        68,
        69,
        70,
        71,
        72,
        73,
    }


async def test_the_enable_bit_is_added_without_disturbing_the_other_bits() -> None:
    seed = {**_SEED, 21: 0x0055}  # bit 7 clear, five other bits set
    driver, transport = _write_setup(seed)
    await driver.start_grid_charge(power_w=3000, duration_min=60, now=_NOON)
    written = _written(transport, 21)
    assert written == 0x0055 | 0x0080
    assert written & 0x007F == 0x0055  # bit 7 is the only one that changed


async def test_an_inverter_already_enabled_keeps_its_enable_register_verbatim() -> None:
    seed = {**_SEED, 21: 0x67D5}  # this installation's real register 21
    driver, transport = _write_setup(seed)
    await driver.start_grid_charge(power_w=3000, duration_min=60, now=_NOON)
    assert _written(transport, 21) == 0x67D5


async def test_a_window_crossing_midnight_uses_two_periods_and_clears_the_third() -> None:
    driver, transport = _write_setup()
    start = datetime(2026, 8, 6, 22, 0, tzinfo=UTC)
    await driver.start_grid_charge(power_w=3000, duration_min=480, now=start)
    # 22:00 to 23:59, then 00:00 to the end; the unused third pair is cleared.
    assert _written(transport, 68) == _packed(22, 0)
    assert _written(transport, 69) == _packed(23, 59)
    assert _written(transport, 70) == _packed(0, 0)
    assert _written(transport, 71) == _packed(6, 0)
    assert _written(transport, 72) == 0
    assert _written(transport, 73) == 0


async def test_the_window_carries_the_clock_of_the_zone_it_was_given() -> None:
    # The window registers hold clock times, not instants, so the zone of the
    # ``now`` handed in is what reaches the device: 22:40 on the installation's
    # own clock has to pack as 22:40. The first live attempt handed this method
    # the same moment in UTC and the inverter was told 03:40, which is a charge
    # window at the wrong hour rather than a long one.
    driver, transport = _write_setup()
    start = datetime(2026, 9, 10, 22, 40, tzinfo=ZoneInfo("America/Chicago"))
    await driver.start_grid_charge(power_w=3000, duration_min=600, now=start)
    assert start.astimezone(UTC).hour == 3  # the same moment on the UTC clock
    assert _written(transport, 68) == _packed(22, 40)
    assert _written(transport, 69) == _packed(23, 59)
    assert _written(transport, 70) == _packed(0, 0)
    assert _written(transport, 71) == _packed(8, 40)


async def test_the_power_rounds_down_to_the_hundred_watt_unit() -> None:
    driver, transport = _write_setup()
    await driver.start_grid_charge(power_w=3050, duration_min=60, now=_NOON)
    assert _written(transport, 66) == 30  # not 31: never stronger than decided


async def test_a_power_that_cannot_be_expressed_is_refused() -> None:
    for watts in (50, 20000):
        driver, transport = _write_setup()
        with pytest.raises(ValueError):
            await driver.start_grid_charge(power_w=watts, duration_min=60, now=_NOON)
        assert transport.writes == []


async def test_a_start_that_cannot_read_the_configuration_writes_nothing() -> None:
    transport = _WriteTransport(_SEED)
    transport.read_error = TransportError("link down")
    driver = _driver(transport)
    with pytest.raises(TransportError):
        await driver.start_grid_charge(power_w=3000, duration_min=60, now=_NOON)
    assert transport.writes == []


async def test_a_start_refuses_when_the_configuration_it_must_restore_was_not_fully_read() -> None:
    seed = {k: v for k, v in _SEED.items() if k != 66}  # register 66 not read
    driver, transport = _write_setup(seed)
    with pytest.raises(ValueError):
        await driver.start_grid_charge(power_w=3000, duration_min=60, now=_NOON)
    assert transport.writes == []


async def test_a_restore_puts_the_saved_registers_back_verbatim() -> None:
    driver, transport = _write_setup()
    original = dict(transport.registers)
    change = await driver.start_grid_charge(power_w=3050, duration_min=60, now=_NOON)
    assert transport.registers != original  # the charge changed the map
    await driver.restore_grid_charge(change.saved)
    assert transport.registers == original


async def test_a_restore_writes_one_register_per_call_too() -> None:
    # The stop path meets the same hardware as the start: a restore sent as one
    # nine-register write is ignored by this inverter, which would leave a
    # charge running with nothing that can end it.
    driver, transport = _write_setup()
    change = await driver.start_grid_charge(power_w=3050, duration_min=60, now=_NOON)
    transport.writes.clear()
    await driver.restore_grid_charge(change.saved)
    assert len(transport.writes) == 9
    assert all(len(chunk) == 1 for chunk in transport.writes)
    assert _written(transport, 66) == 12  # the saved power, not the charge's


async def test_a_restore_without_the_registers_it_needs_is_refused() -> None:
    seed = {k: v for k, v in _SEED.items() if k != 72}
    saved = _decode(seed)
    driver, transport = _write_setup(seed)
    with pytest.raises(ValueError):
        await driver.restore_grid_charge(saved)
    assert transport.writes == []


async def test_the_applied_configuration_is_what_the_device_says_not_what_was_asked() -> None:
    driver, _ = _write_setup()
    change = await driver.start_grid_charge(power_w=3050, duration_min=60, now=_NOON)
    # The read-back reflects the map the fake applied the write to: charge
    # enabled, the new thirty-unit power, and the written window in place.
    assert change.applied.ac_charge_enabled is True
    assert change.applied.power_w == 3000
    assert change.applied.registers[66] == 30
    assert len(change.applied.windows) == 3
    assert change.applied.windows[0] == ChargeWindow(12, 0, 13, 0)


async def test_a_write_the_device_refuses_is_reported() -> None:
    driver, transport = _write_setup()
    transport.accepts_writes = False
    with pytest.raises(ChargeWriteRefusedError):
        await driver.start_grid_charge(power_w=3000, duration_min=60, now=_NOON)
