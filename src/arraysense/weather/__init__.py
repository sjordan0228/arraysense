"""weather — site-level sources that are not an inverter.

A parallel package to drivers/: a weather service is a source the collector
layer polls, but nothing about it speaks to the wire, so it must not live under
drivers/ and must never import an inverter library. Issue #5.
"""

from __future__ import annotations

from arraysense.weather.open_meteo import fetch_current

__all__ = ["fetch_current"]
