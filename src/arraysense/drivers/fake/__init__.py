"""fake — a simulated inverter, registered exactly like a real one.

There is no hardware behind this and that is the point twice over. The tests get
a source that can be made to fail on demand, which no real dongle can; and the
lookup they go through is the lookup the service goes through, so a registry
that stopped working would fail a test rather than only a deployment.
"""

from __future__ import annotations

from arraysense.config import Config
from arraysense.drivers.base import DriverEntry
from arraysense.drivers.fake.source import CAPABILITIES, NAME, FakeSource


def build(config: Config) -> FakeSource:
    """Build a fake reading under the configured serial.

    Only the serial is taken from the configuration. The dongle address and port
    describe a way in that this driver does not use, which is exactly why the
    whole Config is handed to a driver rather than a fixed set of connection
    fields: each one takes what it needs and ignores the rest.
    """
    return FakeSource(device=config.inverter_serial)


DRIVER = DriverEntry(
    name=NAME,
    description="A simulated inverter with no hardware behind it, for tests and demos",
    capabilities=CAPABILITIES,
    build=build,
)

__all__ = ["CAPABILITIES", "DRIVER", "NAME", "FakeSource", "build"]
