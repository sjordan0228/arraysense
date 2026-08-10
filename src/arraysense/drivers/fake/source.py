"""source.py — a deterministic in-memory inverter, for tests and for demos.

``FakeSource`` is how everything above the wire gets tested. Its values mirror a
real midday reading from the reference installation, so a test expectation that
looks wrong probably is.

It is a driver package rather than a test helper for two reasons. Registering it
the same way a real driver is registered means the tests exercise the lookup the
service actually uses, instead of a shortcut that would keep working after the
lookup broke. And a name in the configuration that brings the whole service up
with no hardware attached is worth having on its own: the pages can be worked on
away from the one dongle, which admits a single client and is busy holding the
real collector.
"""

from __future__ import annotations

from datetime import UTC, datetime

from arraysense.drivers.base import (
    Capabilities,
    DeviceIdentity,
    EnergyReporting,
    expand_module_metrics,
)
from arraysense.models import BatteryModuleSample, Sample

# The name this driver is registered and configured under.
NAME = "fake"

# One midday moment on the reference installation: about 7.6 kW of PV with the
# battery charging. Hoisted out of ``read`` so the capability declaration can be
# derived from it rather than repeat it — a fake whose declaration disagreed
# with what it returns would be a poor test of code that trusts declarations.
_READINGS: dict[str, float] = {
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
}

# The per-module templates the fake fills, as arraysense.metrics names them
# before the registry expands each across the inverter's battery slots.
_MODULE_TEMPLATES: tuple[str, ...] = (
    "soc_pct",
    "soh_pct",
    "voltage_v",
    "current_a",
    "temperature_c",
    "cycle_count",
    "cell_max_voltage_v",
    "cell_min_voltage_v",
    "cell_max_temperature_c",
    "cell_min_temperature_c",
    "cell_max_voltage_num",
    "cell_min_voltage_num",
    "cell_max_temperature_num",
    "cell_min_temperature_num",
)

# What the fake claims to be. Split-phase and four battery slots because it
# imitates the reference installation, but energy is ESTIMATED and deliberately
# so: it returns no kWh counter at all, and a fake that claimed to count energy
# it does not produce would let a page's "counted" branch go untested while
# looking exercised.
CAPABILITIES = Capabilities(
    pv_strings=3,
    energy=EnergyReporting.ESTIMATED,
    metrics=frozenset(_READINGS) | expand_module_metrics(_MODULE_TEMPLATES),
    split_phase=True,
    per_module_battery=True,
    relays_battery=True,
    battery_module_slots=4,
)


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
        device: str = "CE00000000",
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

        ``device`` is the serial the fake claims to be. It is a made-up value
        in the shape of a real one, and the default is deliberately not a
        serial from the reference installation: this repository is public.
        """
        self.fail_on_connect = fail_on_connect
        self.fail_on_read = fail_on_read
        self.modules = modules
        self.device = device
        self.connected = False
        self.reads = 0

    @property
    def identity(self) -> DeviceIdentity:
        """Which unit this pretends to be.

        The model is named where a real driver leaves it absent, because here it
        is not a guess about hardware nobody asked: there is no hardware, and
        anything reading this should be able to tell.
        """
        return DeviceIdentity(driver=NAME, serial=self.device, model="simulated")

    @property
    def capabilities(self) -> Capabilities:
        """What the fake produces — the module-level declaration, not a copy."""
        return CAPABILITIES

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
            # Copied rather than handed out: the readings are one shared dict,
            # and a caller that mutated a sample would change every later read.
            readings=dict(_READINGS),
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
