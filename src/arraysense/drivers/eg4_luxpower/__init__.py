"""eg4_luxpower — the LuxPower-protocol family: 18kPV, 12kPV, FlexBOSS, 6000XP.

One package per protocol family rather than per model, because these units
share a register surface: the same 141 registers, the same 31 kWh counters.
How many PV strings each model actually has is a per-model fact carried with a
citation in MODELS — only the 18kPV's is measured; the rest inherit the family
declaration until a source exists. A model that answers differently on the
wire earns its own package; one that answers the same earns a line in MODELS.

The registry entry lives here rather than in ``source.py`` so that the module
doing the reading does not also have to know it is being registered — and so
this file stays the one-screen summary of what the family is.
"""

from __future__ import annotations

from arraysense.drivers.base import DriverEntry
from arraysense.drivers.eg4_luxpower.source import (
    CAPABILITIES,
    MODELS,
    NAME,
    Eg4LuxPowerSource,
    to_sample,
)

DRIVER = DriverEntry(
    name=NAME,
    description=(
        "EG4 and LuxPower hybrid inverters over the WiFi dongle or a "
        "USB-to-RS485 adapter (18kPV, 12kPV, FlexBOSS21, FlexBOSS18; 6000XP unverified)"
    ),
    manufacturer="EG4",
    models=MODELS,
    capabilities=CAPABILITIES,
    build=Eg4LuxPowerSource,
)

__all__ = ["CAPABILITIES", "DRIVER", "MODELS", "NAME", "Eg4LuxPowerSource", "to_sample"]
