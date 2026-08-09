"""Tests for the misrouted-reply retry in arraysense.drivers.eg4_luxpower.source.

Every exception message here is copied from pylxpweb 0.9.38's own raise sites,
not paraphrased. The retry is a string-and-type judgement about somebody else's
library, so a test built on an invented message proves only that the test and
the code agree with each other — which is exactly how the predicate came to
match one of the library's four routing failures and miss the other three.

The wire behaviour these tests are bounded against was measured, not assumed:
one ``_send_receive`` already makes three attempts and sleeps 0.5 s between
them — dongle.py:700 and :751 — and one ``read_runtime()`` is eight of those.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pylxpweb.transports.exceptions import (
    TransportError,
    TransportReadError,
    TransportResponseMismatchError,
)

from arraysense.config import Config
from arraysense.drivers.base import DeviceIdentityError
from arraysense.drivers.eg4_luxpower.source import (
    _MISROUTED,
    CAPABILITIES,
    Eg4LuxPowerSource,
)

# The four routing failures pylxpweb 0.9.38 raises, verbatim from
# transports/dongle.py — three through ``_raise_mismatch`` at line 1037 and one
# at line 956. All four are TransportResponseMismatchError and all four mean
# the same thing: this frame answers a question we did not ask.
_SERIAL_MISMATCH = (
    "[CE12345678] Response serial mismatch: expected CE12345678, got CE87654321 "
    "(expected [tcp_func=0xc2 func=0x04 register=32 count=32], "
    "received [func=0x04 register=32]) — likely a misrouted cloud response"
)
_FUNCTION_MISMATCH = (
    "[CE12345678] Response function mismatch: expected [tcp_func=0xc2 func=0x04 "
    "register=32 count=32], received [func=0x03 register=32] "
    "— likely a misrouted cloud response"
)
_REGISTER_MISMATCH = (
    "[CE12345678] Response register mismatch: expected [tcp_func=0xc2 func=0x04 "
    "register=32 count=32], received [func=0x04 register=127] "
    "— likely a misrouted cloud response"
)
_TCP_FUNCTION_MISMATCH = (
    "[CE12345678] Unexpected TCP function 0xc1 (heartbeat): expected "
    "[tcp_func=0xc2 func=0x04 register=32 count=32], received [tcp_func=0xc1] "
    "— misrouted/unsolicited frame"
)


def _runtime() -> SimpleNamespace:
    """A minimal runtime object: enough fields to prove a sample got through."""
    return SimpleNamespace(pv1_power=2253.0, output_power=2357.0)


def _wrapped(message: str) -> TransportReadError:
    """Rebuild what actually escapes ``read_runtime()``.

    The group reader catches every failure and re-raises a plain
    ``TransportReadError("Failed to read register group '<name>': ...")``
    ``from`` the original, so the specific mismatch type never reaches us —
    only its message and its ``__cause__``. Verified against pylxpweb 0.9.38 by
    driving ``read_runtime`` with a stub register reader.
    """
    try:
        raise TransportResponseMismatchError(message)
    except TransportResponseMismatchError as exc:
        wrapped = TransportReadError(f"Failed to read register group 'status_energy': {exc}")
        # Chained explicitly. Building the wrapper inside the except block does
        # not set __cause__ — only ``raise ... from`` does, which is what
        # _register_data.py:1094 actually writes. Without this the helper looked
        # right and left __cause__ None, so the chain walk it exists to exercise
        # was never reached and the test passed on the message match alone.
        wrapped.__cause__ = exc
        return wrapped


class _Crossed:
    """A transport that misanswers a fixed number of times, then succeeds."""

    def __init__(self, failures: list[Exception]) -> None:
        self.failures = list(failures)
        self.runtime_calls = 0
        self.battery_calls = 0

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def read_runtime(self) -> object:
        self.runtime_calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return _runtime()

    async def read_battery(self) -> object:
        self.battery_calls += 1
        return None

    async def read_energy(self) -> object:
        return None


def _source(
    transport: object,
    poll_interval: float = 11.0,
) -> Eg4LuxPowerSource:
    return Eg4LuxPowerSource(
        Config(
            dongle_host="127.0.0.1",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path=":memory:",
            poll_interval=poll_interval,
        ),
        transport=transport,  # type: ignore[arg-type]
    )


@pytest.mark.parametrize(
    "message",
    [_SERIAL_MISMATCH, _FUNCTION_MISMATCH, _REGISTER_MISMATCH, _TCP_FUNCTION_MISMATCH],
    ids=["serial", "function", "register", "tcp_function"],
)
async def test_every_routing_failure_the_library_raises_is_retried(message: str) -> None:
    # pylxpweb raises one exception type for all four and the messages differ
    # only in which field disagreed. Which of them a given collision produces
    # has not been measured — every misroute observed on the reference
    # installation has been the register case — so this is defence against the
    # other three rather than a claim about how often they arrive.
    transport = _Crossed([TransportResponseMismatchError(message)])
    sample = await _source(transport).read()
    assert transport.runtime_calls == 2
    assert sample.readings["pv1_power_w"] == 2253.0


@pytest.mark.parametrize(
    "message",
    [_SERIAL_MISMATCH, _FUNCTION_MISMATCH, _REGISTER_MISMATCH, _TCP_FUNCTION_MISMATCH],
    ids=["serial", "function", "register", "tcp_function"],
)
async def test_a_routing_failure_wrapped_by_the_group_reader_is_retried(message: str) -> None:
    # This is the shape that actually arrives: the group reader flattens the
    # mismatch type into a plain TransportReadError. Matching on the type alone
    # would therefore never fire in production, which is why the cause chain is
    # walked as well.
    transport = _Crossed([_wrapped(message)])
    sample = await _source(transport).read()
    assert transport.runtime_calls == 2
    assert sample.readings["pv1_power_w"] == 2253.0


async def test_a_mismatch_type_with_no_recognisable_message_is_still_retried() -> None:
    # Belt to the message's braces: if a future version reworks the wording,
    # the type still says what happened.
    transport = _Crossed([TransportResponseMismatchError("frame rejected")])
    sample = await _source(transport).read()
    assert transport.runtime_calls == 2
    assert sample.readings["pv1_power_w"] == 2253.0


async def test_a_closed_socket_is_not_retried() -> None:
    # Only a crossed reply earns an immediate second attempt. Retrying a dead
    # connection is how a poll loop becomes a busy wait against an inverter
    # that is not there.
    transport = _Crossed([TransportError("connection reset by peer")])
    with pytest.raises(ConnectionError):
        await _source(transport).read()
    assert transport.runtime_calls == 1


async def test_a_second_crossed_reply_ends_the_poll() -> None:
    # Once is the dongle being itself; twice in a row is a fault, and the
    # caller's backoff is the answer to a fault.
    transport = _Crossed([_wrapped(_REGISTER_MISMATCH), _wrapped(_SERIAL_MISMATCH)])
    with pytest.raises(ConnectionError, match="reading from inverter failed"):
        await _source(transport).read()
    assert transport.runtime_calls == 2


async def test_a_recovered_read_still_counts_as_a_misroute() -> None:
    # The rate is the diagnostic, not the individual event: one poll in thirty
    # is the dongle being itself and one in three is a fault. A retry that
    # succeeds must still be visible or the fault arrives as a mystery.
    transport = _Crossed([_wrapped(_TCP_FUNCTION_MISMATCH)])
    source = _source(transport)
    await source.read()
    assert source.misroutes == 1


def test_the_cause_chain_is_what_finds_a_reworded_wrapper() -> None:
    # The walk has to earn its place. Every other case here matches on the
    # wrapper's own text, so deleting the chain walk left the suite green and
    # the walk untested. This is the case that needs it: a wrapper that chained
    # the original but said nothing recognisable itself, which is what a library
    # reword would produce.
    try:
        raise TransportResponseMismatchError(
            "Response register mismatch: expected 32, got 127 — likely a misrouted cloud response"
        )
    except TransportResponseMismatchError as exc:
        opaque = TransportReadError("Failed to read register group 'status_energy'")
        opaque.__cause__ = exc

    assert _MISROUTED not in str(opaque), "the wrapper must not give it away on its own"
    assert Eg4LuxPowerSource._is_misrouted(opaque)


def test_implicit_context_is_not_followed() -> None:
    # A dead socket that happened to be raised while a mismatch was in flight
    # is a dead socket, and backing off is the right answer to it. Following
    # __context__ as well as __cause__ would retry it instead.
    try:
        raise TransportResponseMismatchError("likely a misrouted cloud response")
    except TransportResponseMismatchError:
        try:
            raise OSError("connection reset by peer")
        except OSError as dead:
            assert dead.__context__ is not None, "the mismatch must be in the context"
            assert Eg4LuxPowerSource._is_misrouted(dead) is False


async def test_the_energy_read_is_retried_too() -> None:
    # The energy counters go through a separate call that had no retry at all,
    # so a crossed reply there cost the counters even though the identical
    # failure on the runtime read was recovered. A failure here is not fatal —
    # energy falls back to the last good read — which is exactly why it went
    # unnoticed.
    crossed = _wrapped(_REGISTER_MISMATCH)
    calls = {"n": 0}

    async def energy() -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise crossed
        return SimpleNamespace(pv_energy_today=41.6)

    transport = _Crossed([])
    transport.read_energy = energy  # type: ignore[method-assign]
    source = _source(transport)
    sample = await source.read()

    assert calls["n"] == 2, "the crossed energy read must be tried again"
    assert sample.readings.get("pv_energy_today_kwh") == 41.6
    assert source.misroutes == 1, "and it must count, like the runtime path"


# --- transport choice tests -------------------------------------------------


def test_default_config_builds_dongle_transport() -> None:
    # The backwards-compatibility case: a config with no transport set builds
    # a dongle transport exactly as before.
    with patch("arraysense.drivers.eg4_luxpower.source.create_dongle_transport") as mock_dongle:
        mock_dongle.return_value = _Crossed([])
        source = Eg4LuxPowerSource(
            Config(
                dongle_host="192.0.2.10",
                dongle_serial="BA12345678",
                inverter_serial="CE12345678",
                database_path="/tmp/test.db",
                # transport defaults to "dongle"
            )
        )
        mock_dongle.assert_called_once()
        assert "host" in mock_dongle.call_args.kwargs
        assert mock_dongle.call_args.kwargs["host"] == "192.0.2.10"
        assert source._transport is mock_dongle.return_value


def test_modbus_serial_config_builds_serial_transport() -> None:
    # transport = "modbus_serial" with a device path builds a serial transport,
    # carrying the configured device, baud and unit id.
    with patch("arraysense.drivers.eg4_luxpower.source.create_serial_transport") as mock_serial:
        mock_serial.return_value = _Crossed([])
        source = Eg4LuxPowerSource(
            Config(
                dongle_host="192.0.2.10",
                dongle_serial="BA12345678",
                inverter_serial="CE12345678",
                database_path="/tmp/test.db",
                transport="modbus_serial",
                serial_device="/dev/ttyUSB0",
                serial_baud=38400,
                serial_unit_id=2,
            )
        )
        mock_serial.assert_called_once()
        kwargs = mock_serial.call_args.kwargs
        assert kwargs["port"] == "/dev/ttyUSB0"
        assert kwargs["serial"] == "CE12345678"
        assert kwargs["baudrate"] == 38400
        assert kwargs["unit_id"] == 2
        assert source._transport is mock_serial.return_value


def test_injected_transport_overrides_config_for_dongle() -> None:
    # An explicitly injected transport still overrides the config.
    fake_transport = _Crossed([])
    with patch("arraysense.drivers.eg4_luxpower.source.create_dongle_transport") as mock_dongle:
        source = Eg4LuxPowerSource(
            Config(
                dongle_host="192.0.2.10",
                dongle_serial="BA12345678",
                inverter_serial="CE12345678",
                database_path="/tmp/test.db",
                # transport defaults to "dongle": the factory this patches
            ),
            transport=fake_transport,
        )
        mock_dongle.assert_not_called()
        assert source._transport is fake_transport


def test_injected_transport_overrides_config_for_serial() -> None:
    # An explicitly injected transport still overrides the config for serial too.
    fake_transport = _Crossed([])
    with patch("arraysense.drivers.eg4_luxpower.source.create_serial_transport") as mock_serial:
        source = Eg4LuxPowerSource(
            Config(
                dongle_host="192.0.2.10",
                dongle_serial="BA12345678",
                inverter_serial="CE12345678",
                database_path="/tmp/test.db",
                transport="modbus_serial",
                serial_device="/dev/ttyUSB0",
            ),
            transport=fake_transport,
        )
        # Neither factory should be called
        mock_serial.assert_not_called()
        assert source._transport is fake_transport


def test_capabilities_report_the_transport_this_installation_uses() -> None:
    # A page telling somebody they are connected by dongle over a serial link
    # would be worse than telling them nothing. The registry entry can only
    # carry the family default, so the built source has to answer for itself.
    with patch("arraysense.drivers.eg4_luxpower.source.create_serial_transport") as mock_serial:
        mock_serial.return_value = _Crossed([])
        source = Eg4LuxPowerSource(
            Config(
                dongle_host="",
                dongle_serial="",
                inverter_serial="CE12345678",
                database_path="/tmp/test.db",
                transport="modbus_serial",
                serial_device="/dev/rs485",
            )
        )
    assert source.capabilities.transport == "modbus_serial"
    # Everything else still agrees with the family declaration.
    assert source.capabilities.pv_strings == CAPABILITIES.pv_strings
    assert source.capabilities.metrics == CAPABILITIES.metrics


class _SerialBus:
    """A serial transport that behaves like the exclusive port really does.

    Refuses a second connect while still open, because that is what the real
    port does: the library builds a fresh client on every connect without
    closing the last one, and on real hardware the second poll failed with
    "Could not exclusively lock port". A double that quietly accepts repeated
    connects hides exactly the bug that matters.

    It also counts serial reads and runtime reads separately, so a test can pin
    the order the two happen in — which is load-bearing, not cosmetic.
    """

    def __init__(self, answers: str, runtime_fails: bool = False) -> None:
        self._answers = answers
        self._runtime_fails = runtime_fails
        self.is_connected = False
        self.serial_reads = 0
        self.runtime_reads = 0
        self.connects = 0

    async def connect(self) -> None:
        if self.is_connected:
            raise OSError("could not exclusively lock port")
        self.connects += 1
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False

    async def read_serial_number(self) -> str:
        self.serial_reads += 1
        return self._answers

    async def read_runtime(self) -> object:
        self.runtime_reads += 1
        if self._runtime_fails:
            raise OSError("the bus has gone deaf")
        return SimpleNamespace(serial_number="CE12345678")

    async def read_battery(self) -> object:
        return None

    async def read_energy(self) -> object:
        raise OSError("no energy counters in this test")


def _serial_source(bus: _SerialBus, configured: str = "CE12345678") -> Eg4LuxPowerSource:
    with patch("arraysense.drivers.eg4_luxpower.source.create_serial_transport") as mock:
        mock.return_value = bus
        return Eg4LuxPowerSource(
            Config(
                dongle_host="",
                dongle_serial="",
                inverter_serial=configured,
                database_path="/tmp/test.db",
                transport="modbus_serial",
                serial_device="/dev/rs485",
            )
        )


async def test_a_serial_bus_answering_for_another_inverter_stops_the_collector() -> None:
    # Modbus selects a unit by address, so nothing in a reply says which machine
    # sent it. If the configured serial is not the one on the bus, every reading
    # would be filed under the wrong identity and every row would look ordinary.
    # Refusing to collect is recoverable; a history merged from two machines is
    # not, so this must escape rather than become a gap.
    source = _serial_source(_SerialBus("BB99999999"))
    with pytest.raises(DeviceIdentityError, match="refusing to file its readings"):
        await source.connect()
    assert not issubclass(DeviceIdentityError, ConnectionError), (
        "a mistyped serial never comes right on its own, so it must not be "
        "recorded as a gap and retried forever"
    )


async def test_an_incomplete_serial_answer_is_a_gap_not_an_accusation() -> None:
    # An answer of the wrong width identifies nobody — it may have been cut
    # short, or arrived whole and lost a byte the decoder drops — so it cannot
    # be an accusation that another inverter is on the bus. Width is judged
    # against the protocol's ten characters rather than against the configured
    # value, so a short setting cannot match a short answer and call it
    # agreement.
    source = _serial_source(_SerialBus("CE1234"))
    with pytest.raises(ConnectionError, match="came back incomplete"):
        await source.connect()


async def test_an_open_port_does_not_let_a_later_connect_skip_the_question() -> None:
    # connect() skips the physical open when the port is already held, which is
    # what stops it relocking an exclusive port against itself. The identity
    # question is asked outside that guard, so an unanswered one stays
    # unanswered — otherwise a check that failed once would leave a connection
    # behind that every later poll walked straight past.
    bus = _SerialBus("BB99999999")
    source = _serial_source(bus)
    for _ in range(3):
        with pytest.raises(DeviceIdentityError):
            await source.connect()
    assert bus.connects == 1, "the port was reopened while already open"
    assert bus.serial_reads == 3, "a later connect skipped the question"


async def test_the_identity_is_asked_once_and_not_again() -> None:
    # Asked once because the library forces it. Any successful register read
    # zeroes pylxpweb's consecutive-error count, and that count is the only
    # trigger for its own reconnect, so a probe anywhere in the poll loop would
    # erase the evidence of the previous poll's failure and starve the repair.
    bus = _SerialBus("CE12345678")
    source = _serial_source(bus)
    for _ in range(5):
        await source.connect()
        await source.read()
    assert bus.serial_reads == 1, "the serial was probed inside the poll loop"
    assert bus.connects == 1, "the port was reopened while already open"


async def test_a_failing_bus_is_never_probed_and_keeps_its_error_count() -> None:
    # The whole reason the question is asked at connect rather than in read: a
    # bus that has gone deaf must accumulate errors until the library's own
    # reconnect threshold repairs it. A probe in the read path reset that count
    # every poll.
    bus = _SerialBus("CE12345678", runtime_fails=True)
    source = _serial_source(bus)
    await source.connect()
    for _ in range(3):
        with pytest.raises(ConnectionError):
            await source.read()
    assert bus.runtime_reads == 3
    assert bus.serial_reads == 1, (
        "the serial was probed during the poll loop, which resets the library's "
        "error count and starves its reconnect"
    )


async def test_a_dongle_installation_asks_no_identity_question() -> None:
    # The dongle authenticates every reply with the serial already, so the extra
    # read would buy nothing and cost a round trip on the transport that can
    # least afford one.
    class _Dongle(_SerialBus):
        async def read_serial_number(self) -> str:
            raise AssertionError("the dongle path must not read the serial")

    bus = _Dongle("CE12345678")
    with patch("arraysense.drivers.eg4_luxpower.source.create_dongle_transport") as mock_dongle:
        mock_dongle.return_value = bus
        source = Eg4LuxPowerSource(
            Config(
                dongle_host="192.0.2.10",
                dongle_serial="BA12345678",
                inverter_serial="CE12345678",
                database_path="/tmp/test.db",
            )
        )
    await source.connect()
    await source.read()
