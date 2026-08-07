"""mode.py — say in words what the system is doing right now.

The dashboard reports four instantaneous powers and leaves the reader to infer
the state from their signs. That inference is not obvious even to the owner:
"grid 2.2 kW, solar 0.6 kW, battery 0 W" is the inverter having given up and
passed the house through to the grid, and nothing on the page said so.

The states here are distinguished by what the *inverter* is doing, not by which
number is largest, because the interesting distinction is invisible in the
magnitudes. When the bank is flat and there is no sun the inverter stops
inverting altogether and a bypass relay feeds the loads directly from the grid.
Its EPS port then carries nothing while the house still draws kilowatts — which
is exactly the shape of ``eps_power_w`` reading zero against a live
``load_power_w``. On the reference installation that was 4,172 hours, 26% of
twenty-two months, and it is also why ``eps_energy_total_kwh`` reads a quarter
low against house load and is kept out of the energy model.

Every rule below needs a threshold, and every threshold is a decision about
what counts as nothing. They are set from what the hardware actually does
rather than from round numbers that look tidy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

# Below this, a flow is the system idling rather than doing something. The bank
# and the inverter both draw tens of watts just being switched on, so a label
# that changes at one watt spends the day flickering between "charging" and
# "idle" while nothing happens. Fifty watts is under half a percent of this
# bank's peak and comfortably above its own housekeeping.
IDLE_W = 50.0

# How little the EPS port may carry, against a live house, before the inverter
# is judged to have handed the loads to the grid. Not zero exactly: the reading
# is an integer watt and the port carries a little even in bypass on some
# firmware, so a hard equality would miss the state it exists to name.
BYPASS_W = 30.0


class Battery(Enum):
    """What the bank is doing, in the words the dashboard prints."""

    CHARGING = "Battery charging"
    DISCHARGING = "Battery discharging"
    IDLE = "Battery idle"
    UNKNOWN = "Battery unknown"


class Mode(Enum):
    """What the system as a whole is doing.

    ``UNKNOWN`` is a real member and not a failure to categorise. A poll that
    did not complete leaves no readings, and a page that names a mode from
    absent data is asserting something nobody measured — the same rule that
    keeps a missing reading from rendering as zero.
    """

    ON_GRID = "On grid"
    SOLAR = "Solar"
    SOLAR_AND_BATTERY = "Solar and battery"
    BATTERY = "Battery discharging"
    IMPORTING = "Importing"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class Status:
    """The system's state in words, with the reason it was judged that way.

    ``why`` exists because a mode is an interpretation, and an interpretation a
    reader cannot check is one they have to take on trust. It names the
    readings the decision turned on.
    """

    mode: Mode
    battery: Battery
    why: str

    @property
    def known(self) -> bool:
        """Whether anything was actually measured."""
        return self.mode is not Mode.UNKNOWN


def _reading(live: Mapping[str, object], name: str) -> float | None:
    """One metric from a live row, or None if it is absent or not a number."""
    value = live.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def battery_state(live: Mapping[str, object]) -> Battery:
    """Whether the bank is charging, discharging, or sitting still.

    Positive is charging throughout this project — ``battery_power_w`` is
    charge minus discharge — and the same convention holds in the history
    imported from SolarAssistant, which was checked against a night of readings
    rather than assumed.
    """
    power = _reading(live, "battery_power_w")
    if power is None:
        return Battery.UNKNOWN
    if power > IDLE_W:
        return Battery.CHARGING
    if power < -IDLE_W:
        return Battery.DISCHARGING
    return Battery.IDLE


def assess(live: Mapping[str, object]) -> Status:
    """Name what the system is doing, from one live reading.

    The order of the tests matters. Bypass is checked first because it is the
    one state the magnitudes hide: on grid with the inverter idle looks exactly
    like importing hard, and only the silent EPS port tells them apart.
    """
    load = _reading(live, "load_power_w")
    eps = _reading(live, "eps_power_w")
    grid = _reading(live, "grid_power_w")
    solar = _reading(live, "pv_total_power_w")
    battery = battery_state(live)

    # Every mode below names what is powering the house, so every one of them
    # is a claim about the house. Made without a house reading it is the same
    # error as rendering a missing value as zero — an assertion nobody
    # measured. The battery is measured directly and travels separately, so a
    # device that reports no load still lights the battery card.
    if load is None:
        return Status(Mode.UNKNOWN, battery, "the house's draw was not reported")

    if solar is None and grid is None:
        return Status(Mode.UNKNOWN, battery, "nothing was reported")

    # The inverter has handed the loads to the grid: its own output port is
    # carrying nothing while the house is plainly drawing.
    #
    # The house is compared against the port rather than against the same
    # threshold that decided the port was silent. Two readings of the same
    # magnitude are not evidence of a flow between them, and using one constant
    # for both meant a 40 W house beside a 0 W port satisfied the test and was
    # announced as running on the grid. The reference system never draws under
    # 740 W so it could not happen there; a cabin or a single-circuit install
    # is exactly what the driver work invites.
    if eps is not None and eps < BYPASS_W and load - eps > IDLE_W:
        return Status(
            Mode.ON_GRID,
            battery,
            f"the inverter's output is carrying {eps:.0f} W while the house draws {load:.0f} W, "
            "so the grid is feeding the house directly",
        )

    if battery is Battery.DISCHARGING:
        if solar is not None and solar > IDLE_W:
            return Status(Mode.SOLAR_AND_BATTERY, battery, "the array and the bank share the house")
        return Status(Mode.BATTERY, battery, "the bank is carrying the house")

    if solar is not None and solar > IDLE_W:
        if battery is Battery.CHARGING:
            return Status(
                Mode.SOLAR_AND_BATTERY, battery, "the array covers the house and charges the bank"
            )
        return Status(Mode.SOLAR, battery, "the array covers the house")

    if grid is not None and grid > IDLE_W:
        return Status(Mode.IMPORTING, battery, "the house is drawing from the grid")

    return Status(Mode.UNKNOWN, battery, "no flow large enough to name a state")
