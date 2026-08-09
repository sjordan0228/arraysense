"""eg4_luxpower — the LuxPower-protocol family: 18kPV, 6000XP, 12kPV.

One package per protocol family rather than per model, because these units
share a register surface: the same 141 registers, the same three PV strings,
the same 31 kWh counters. A model that answers differently earns its own
package; a model that answers the same earns a line in this docstring.

The registry entry lives here rather than in ``source.py`` so that the module
doing the reading does not also have to know it is being registered — and so
this file stays the one-screen summary of what the family is.
"""

from __future__ import annotations

from arraysense.drivers.base import DriverEntry
from arraysense.drivers.eg4_luxpower.source import (
    CAPABILITIES,
    NAME,
    Eg4LuxPowerSource,
    to_sample,
)

DRIVER = DriverEntry(
    name=NAME,
    description=(
        "EG4 and LuxPower hybrid inverters over the WiFi dongle or a "
        "USB-to-RS485 adapter (18kPV, 6000XP, 12kPV)"
    ),
    capabilities=CAPABILITIES,
    build=Eg4LuxPowerSource,
)

__all__ = ["CAPABILITIES", "DRIVER", "NAME", "Eg4LuxPowerSource", "to_sample"]
