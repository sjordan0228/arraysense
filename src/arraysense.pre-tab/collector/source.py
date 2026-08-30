"""source.py — the collector's view of a driver: the source interface, and the fake.

The service polls an ``InverterSource`` and never learns which inverter
answered. Both names it needs now live under ``arraysense.drivers``, where the
protocol sits beside the drivers that implement it and where a driver package
can be added without touching the collector at all.

They are re-exported here rather than the collector importing them from two
places, because this module is the collector's whole dependency on the driver
layer: one import to look at when asking what the poll loop is allowed to know
about the hardware, and one place a stricter answer would be enforced.

Nothing in this package imports the inverter library. That is the seam — only a
driver knows what speaks to the wire.
"""

from __future__ import annotations

from arraysense.drivers.base import InverterSource
from arraysense.drivers.fake import FakeSource

__all__ = ["FakeSource", "InverterSource"]
