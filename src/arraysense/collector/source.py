"""source.py — where samples come from: the source interface and an in-memory fake.

The service polls an ``InverterSource``. Keeping that an interface means the
transport can change — the dongle's TCP port is being removed in newer
firmware, and wired RS485 is the durable path — without the service, the store
or the API noticing.

``FakeSource`` is how everything above the wire gets tested. Its values mirror
a real midday reading from the reference installation, so a test expectation
that looks wrong probably is.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from arraysense.models import BatteryModuleSample, Sample


@runtime_checkable
class InverterSource(Protocol):
    """Something that yields samples from an inverter.

    The transport is behind an interface because it is not going to stay the
    same. Today the way in is the WiFi dongle's TCP port 8000; newer firmware
    removes that port, Ethernet dongles never had it, and wired RS485 is the
    path that outlives both. Anything that reached for the dongle directly would
    have to be rewritten when that happens, so nothing above this line does.

    The dongle's other constraint sets the failure contract. It accepts exactly
    one TCP client, so being evicted mid-read by the vendor's app or a second
    copy of this service is ordinary, not exceptional. An implementation reports
    that — and an unreachable inverter, and a timeout — as ConnectionError or
    another OSError, which the collector catches to record a gap and back off.
    Inventing a different exception type for it turns a routine eviction into a
    dead poll loop.
    """

    async def connect(self) -> None:
        """Establish the connection, claiming the dongle's single client slot."""
        ...

    async def disconnect(self) -> None:
        """Release the connection and its single client slot.

        Called on shutdown and every time yield mode hands the dongle back, so
        the owner can run a firmware update from the vendor's app without
        stopping the service.
        """
        ...

    async def read(self) -> Sample:
        """Read one sample of inverter and battery state.

        One call has to produce one coherent moment. The sample carries its own
        timestamp — the service does not stamp it, and stores whatever the
        source put there as a single row — so an implementation that stitches
        together registers read minutes apart is recording a moment that never
        existed, under a time that belongs to only part of it.

        The timestamp must be timezone-aware. A naive one is a valid datetime
        and would pass unnoticed here, then be read as local time on the way to
        epoch seconds.
        """
        ...


class FakeSource:
    """Deterministic in-memory source, for testing without hardware.

    Values mirror a real midday reading from the reference installation: about
    7.6 kW of PV with the battery charging, four modules in good balance. Cell
    deltas are a few millivolts, which is what a healthy pack looks like.
    """

    def __init__(
        self,
        fail_on_connect: Exception | None = None,
        fail_on_read: Exception | None = None,
        modules: int = 4,
    ) -> None:
        """Configure the fake.

        The two failure hooks are how the paths that matter most get tested at
        all. Losing the dongle is the normal case this service exists to survive,
        and a test cannot unplug real hardware, so the fake has to be able to
        fail on demand — at connect and at read independently, because a slot
        already taken and a dongle that goes away mid-read are different moments
        that both have to come out as a recorded gap.

        ``modules=0`` reproduces a bank running without closed-loop CAN, where
        the inverter reports no per-module data whatsoever. That is the case
        that must come out as absent rather than as four packs at 0%.
        """
        self.fail_on_connect = fail_on_connect
        self.fail_on_read = fail_on_read
        self.modules = modules
        self.connected = False
        self.reads = 0

    async def connect(self) -> None:
        """Mark connected, or raise the configured failure."""
        if self.fail_on_connect is not None:
            raise self.fail_on_connect
        self.connected = True

    async def disconnect(self) -> None:
        """Mark disconnected."""
        self.connected = False

    async def read(self) -> Sample:
        """Return a plausible sample, or raise the configured failure."""
        if self.fail_on_read is not None:
            raise self.fail_on_read
        self.reads += 1
        return Sample(
            timestamp=datetime.now(tz=UTC),
            readings={
                "pv_total_power_w": 7614.0,
                "load_power_w": 2810.0,
                "grid_power_w": 0.0,
                "grid_voltage_v": 244.1,
                "grid_frequency_hz": 60.01,
                "battery_power_w": 5087.0,
                "battery_voltage_v": 53.7,
                "battery_current_a": 91.5,
                "battery_soc_pct": 64.0,
                "battery_soh_pct": 100.0,
            },
            battery_modules=tuple(
                BatteryModuleSample(
                    serial=f"Battery_ID_{slot:02d}",
                    slot=slot,
                    soc_pct=float(60 + slot),
                    soh_pct=100.0,
                    voltage_v=53.78,
                    current_a=22.8,
                    temperature_c=21.7,
                    cycle_count=450 + slot,
                    cell_max_voltage_v=3.363,
                    cell_min_voltage_v=3.359,
                    cell_max_temperature_c=22.0,
                    cell_min_temperature_c=21.4,
                    cell_max_voltage_num=4,
                    cell_min_voltage_num=1,
                    cell_max_temperature_num=2,
                    cell_min_temperature_num=6,
                )
                for slot in range(1, self.modules + 1)
            ),
        )
