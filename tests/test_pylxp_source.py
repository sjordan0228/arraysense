"""Tests for the misrouted-reply retry in arraysense.collector.pylxp_source.

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

import pytest
from pylxpweb.transports.exceptions import (
    TransportError,
    TransportReadError,
    TransportResponseMismatchError,
)

from arraysense.collector.pylxp_source import _MISROUTED, PylxpSource
from arraysense.config import Config

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
) -> PylxpSource:
    return PylxpSource(
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
    assert PylxpSource._is_misrouted(opaque)


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
            assert PylxpSource._is_misrouted(dead) is False


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
