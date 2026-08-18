"""parse.py — turn Emporia's replies into things this service can store.

Kept apart from the client so the shape of Emporia's JSON is understood in one
place, and so every one of these decisions can be tested against a captured
reply without a network or an account.

Two of them are load-bearing. A channel's ``channelMultiplier`` is 2.0 on a 240 V
circuit because one clamp measures one leg, and dropping it halves the dryer,
the oven and both air conditioners — precisely the loads this feature exists to
identify. And the ``"1,2,3"`` channel is the monitor's own mains total rather
than a circuit; counted as a circuit it would double every sum drawn from these
rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# The channel a Vue reports for its own mains, rather than one of its clamps.
MAINS_CHANNEL = "1,2,3"

# Model prefixes, so a device is typed by what it is rather than by where it sat
# in the reply. Anything unrecognised is a monitor, which is the safe default:
# it gets stored and shown, it simply gains no special treatment.
_MODEL_KINDS = (("VVDN", "charger"), ("SSO", "outlet"), ("VUE", "monitor"))


@dataclass(frozen=True)
class Circuit:
    """One thing Emporia can report power for.

    ``kind`` separates a measured circuit from a monitor's own mains total, an
    outlet and a charger, because a page summing circuits must not add the total
    to its own parts.
    """

    device_gid: int
    channel_num: str
    name: str
    multiplier: float
    kind: str
    # The category the owner chose in Emporia's app — 1 for an air conditioner,
    # 19 for septic on the reference account. None where nobody has set one,
    # which is a clamp that has not been configured; zero would be a category,
    # and category zero is a claim nobody made. Emporia publishes no dictionary
    # for these (the API answers "apiMethod 'getChannelTypes' is invalid"), so
    # what each number means is learned from the account that uses it.
    type_gid: int | None = None
    # What this circuit sits inside, as Emporia's own device record states it.
    # The containment is published as two flat fields on the device rather than
    # as JSON nesting, and without it every device reads as a peer of the house
    # — so a subpanel's branches, a charger and a smart plug are all added on
    # top of the main panel's clamps that already measure them, and the sum
    # reaches 131% of the house (#212, #219). None means a device nothing else
    # contains, which is the only kind that may be added to a total.
    parent_device_gid: int | None = None
    parent_channel_num: str | None = None

    @property
    def identity(self) -> tuple[int, str]:
        """What this circuit *is*, independent of what it is currently called.

        The name is the owner's and they will change it. Keying on it would
        orphan history the first time somebody corrected a typo — the same
        reasoning that keys battery modules by serial rather than slot.
        """
        return (self.device_gid, self.channel_num)


def _kind_for(model: str, channel_num: str) -> str:
    if channel_num == MAINS_CHANNEL:
        for prefix, kind in _MODEL_KINDS:
            if model.startswith(prefix) and kind != "monitor":
                return kind
        return "mains"
    return "circuit"


def _multiplier_for(raw: object, device_gid: int, channel_num: str) -> float:
    """How this channel's reading is scaled, and a complaint when that is unclear.

    A channel that names no multiplier is a 120 V circuit measured whole, so 1.0
    is the fact rather than a stand-in for a missing one. A channel that names a
    multiplier this cannot read is different: Emporia sends a number today, and
    if that ever became a string then falling back to 1.0 would halve a 240 V
    circuit with nothing on the page to show for it. The value still falls back,
    because a poller that stops on one odd channel serves nobody — but it says
    so, so the halved dryer is diagnosable rather than mysterious.
    """
    if raw is None:
        return 1.0
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    logger.warning(
        "emporia channel %s/%s has an unreadable channelMultiplier %r; using 1.0, "
        "which understates a 240 V circuit",
        device_gid,
        channel_num,
        raw,
    )
    return 1.0


def _device_name(node: dict[str, object]) -> str | None:
    """What the owner called this device in Emporia's app, if they called it anything.

    It sits in ``locationProperties`` rather than on the channel, which is why
    it went unused: an outlet, a charger and a monitor all report themselves on
    the mains channel and none of them name it, so the whole device read as
    "Device 100002 ch 1,2,3" while Emporia had held "EVSE" all along.
    """
    properties = node.get("locationProperties")
    if not isinstance(properties, dict):
        return None
    name = properties.get("deviceName")
    return name.strip() if isinstance(name, str) and name.strip() else None


def _label_for(
    name: object, device_gid: int, channel_num: str, device_name: str | None = None
) -> str:
    """What to call a channel: its own name, then its device's, then an identifier.

    ``device_name`` is offered only for a device's own mains channel, and the
    restriction is the point rather than an optimisation. A name describes the
    device, so lending it to every unnamed clamp on a monitor renders four
    separate circuits as four rows all reading "Subpanel Vue" — which is worse
    than the identifier it replaced, since the identifier at least tells them
    apart. An unnamed clamp is a clamp nobody has named, and saying so is the
    honest answer.
    """
    if isinstance(name, str) and name.strip():
        return name.strip()
    if device_name:
        return device_name
    return f"Device {device_gid} ch {channel_num}"


def _parent_of(
    node: dict[str, object], inherited: tuple[int | None, str | None]
) -> tuple[int | None, str | None]:
    """What contains this device: its own declaration, or its ancestor's.

    A monitor states its parent once, on the device record. Its channel bank
    arrives as a nested record that repeats the gid and declares nothing, so
    read on its own terms a subpanel's sixteen branches look like the house's
    own — which is the larger half of the double count.
    """
    gid = node.get("parentDeviceGid")
    if isinstance(gid, int) and not isinstance(gid, bool):
        channel = node.get("parentChannelNum")
        return gid, channel if isinstance(channel, str) else None
    return inherited


def _walk(
    node: object,
    out: list[Circuit],
    inherited: tuple[int | None, str | None] = (None, None),
) -> None:
    if not isinstance(node, dict):
        return
    parent_gid, parent_channel = _parent_of(node, inherited)
    gid = node.get("deviceGid")
    model = str(node.get("model", ""))
    if isinstance(gid, int):
        channels = node.get("channels")
        named = _device_name(node)
        if isinstance(channels, list):
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                number = channel.get("channelNum")
                if not isinstance(number, str):
                    continue
                multiplier = _multiplier_for(channel.get("channelMultiplier"), gid, number)
                raw_type = channel.get("channelTypeGid")
                out.append(
                    Circuit(
                        device_gid=gid,
                        channel_num=number,
                        name=_label_for(
                            channel.get("name"),
                            gid,
                            number,
                            named if number == MAINS_CHANNEL else None,
                        ),
                        multiplier=multiplier,
                        kind=_kind_for(model, number),
                        type_gid=(
                            raw_type
                            if isinstance(raw_type, int) and not isinstance(raw_type, bool)
                            else None
                        ),
                        parent_device_gid=parent_gid,
                        parent_channel_num=parent_channel,
                    )
                )
    nested = node.get("devices")
    if isinstance(nested, list):
        for child in nested:
            _walk(child, out, (parent_gid, parent_channel))


def circuits_from_devices(payload: object) -> list[Circuit]:
    """Every circuit in a ``/customers/devices`` reply, nested devices included.

    A malformed reply yields nothing rather than raising: this runs unattended on
    a timer, and a shape change at Emporia's end must degrade to "no circuits
    found" rather than stopping the poller.
    """
    out: list[Circuit] = []
    if not isinstance(payload, dict):
        return out
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return out
    for device in devices:
        _walk(device, out)
    return out


def device_gids(payload: object) -> tuple[int, ...]:
    """Every device identifier in a ``/customers/devices`` reply.

    The usage endpoint will not answer without them. Asked without a
    ``deviceGids`` parameter it returns HTTP 400 and says so outright —
    "Could not get attribute 'deviceGids' from input" — measured against the
    real API on 16 August 2026. So this is not a filter or an optimisation: a
    request that names no devices reads nothing at all.

    Nested devices count. A charger sits inside its monitor in that reply, and
    leaving it out means its own draw is never asked for.

    Deduplicated, and in the order the reply gave them: the same gid can appear
    as a parent and again inside its own nested list, and asking twice makes
    the query longer to no effect.
    """
    found: list[int] = []

    def walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        gid = node.get("deviceGid")
        if isinstance(gid, int) and not isinstance(gid, bool) and gid not in found:
            found.append(gid)
        nested = node.get("devices")
        if isinstance(nested, list):
            for child in nested:
                walk(child)

    if not isinstance(payload, dict):
        return ()
    devices = payload.get("devices")
    if not isinstance(devices, list):
        return ()
    for device in devices:
        walk(device)
    return tuple(found)


# Emporia's scale names, and how many of each fit in an hour. Usage arrives as
# kilowatt-hours accumulated in the bucket, so watts is the energy divided by the
# bucket's share of an hour — a factor of sixty between these two, which is the
# easiest mistake in this file to make and the hardest to see on a chart.
SCALE_SECOND = "1S"
SCALE_MINUTE = "1MIN"
_BUCKETS_PER_HOUR = {SCALE_SECOND: 3600.0, SCALE_MINUTE: 60.0}


@dataclass(frozen=True)
class Reading:
    """One circuit's power at one moment, or the fact that it did not answer.

    ``watts`` is None when Emporia reported null — an offline outlet does this —
    and that must survive all the way to storage. A zero here would be a claim
    that the circuit drew nothing, which is a different statement from silence.
    """

    device_gid: int
    channel_num: str
    watts: int | None

    @property
    def identity(self) -> tuple[int, str]:
        """Matches ``Circuit.identity``, so a reading finds its circuit."""
        return (self.device_gid, self.channel_num)


def _watts_from(usage: object, per_hour: float) -> int | None:
    """Watts from a bucket of kilowatt-hours, or None when there was no number.

    ``bool`` is excluded deliberately: it is an ``int`` in Python, so an
    unguarded numeric check turns a flag into 60 kW on the minute scale. A field
    that is not a number is an absent reading, which is the one thing this
    project refuses to render as a value.
    """
    if isinstance(usage, bool) or not isinstance(usage, int | float):
        return None
    return round(float(usage) * per_hour * 1000.0)


def _walk_usage(nodes: object, per_hour: float, out: list[Reading]) -> None:
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, dict):
            continue
        gid = node.get("deviceGid")
        number = node.get("channelNum")
        if isinstance(gid, int) and isinstance(number, str):
            out.append(
                Reading(
                    device_gid=gid,
                    channel_num=number,
                    watts=_watts_from(node.get("usage"), per_hour),
                )
            )
        nested = node.get("nestedDevices")
        if isinstance(nested, list):
            for child in nested:
                if isinstance(child, dict):
                    _walk_usage(child.get("channelUsages"), per_hour, out)


def readings_from_usage(payload: object, scale: str) -> list[Reading]:
    """Every reading in a ``getDeviceListUsages`` reply, converted to watts.

    ``scale`` is the scale that was *asked for*, not one read back out of the
    reply: the conversion has to match the request, and trusting the echo would
    silently rescale everything if Emporia ever answered a different bucket.
    """
    out: list[Reading] = []
    per_hour = _BUCKETS_PER_HOUR.get(scale)
    if per_hour is None:
        logger.warning("unknown Emporia scale %r; no readings taken", scale)
        return out
    if not isinstance(payload, dict):
        return out
    usages = payload.get("deviceListUsages")
    if not isinstance(usages, dict):
        return out
    devices = usages.get("devices")
    if not isinstance(devices, list):
        return out
    for device in devices:
        if isinstance(device, dict):
            _walk_usage(device.get("channelUsages"), per_hour, out)
    return out


# Emporia's own automation for a charger, and the exact words to tell an owner
# to look for. Four switches, not the one the design anticipated: the first
# lives on the charger record and the other three on the load record beside it.
# Measured on the reference account, where **two were on and the anticipated one
# was off** — a check written to the design would have reported all clear while
# two other controllers were driving the same charger.
#
# Ordered charger-record-first, then as the load record lists them, so the
# warning reads the same way twice running.
_CHARGER_CONFLICTS = (("loadManagementEnabled", "load management"),)

# Whether a car is attached, and the only field that answers it. Established by
# plugging one in on 16 August 2026: `status` reads "Standby" both with nothing
# connected and with a connected car paused, and `chargerOn` was true with no
# car attached at all — it is the switch, not a statement about delivery. The
# icon separates them, and an icon nobody has seen leaves the question open
# rather than guessing, because a confident wrong answer here tells somebody
# the cable is out while it is in their hand.
_PLUGGED_ICONS = {"CarConnected": True, "CarNotConnected": False}
_LOAD_CONFLICTS = (
    ("schedulesEnabled", "schedules"),
    ("peakDemandEnabled", "peak demand"),
    ("excessGenerationEnabled", "excess generation"),
)


@dataclass(frozen=True)
class ChargerState:
    """An EV charger as this module needs to see it.

    Deliberately without ``breakerPIN``. The write path needs it and re-reads
    the raw record for that one purpose; everything else — the page, the audit,
    the logs — passes this object around, and a secret cannot leak from a field
    that does not exist.

    ``conflicts`` names the Emporia controllers that are switched on for this
    charger. It is a warning and never a refusal: it is the owner's charger and
    their account, and a module that downed tools over a setting it disliked
    would be less useful than one that says plainly what is about to fight it.
    """

    device_gid: int
    rate_a: int | None
    max_rate_a: int | None
    on: bool | None
    status: str
    message: str
    conflicts: tuple[str, ...]
    # Whether a car is attached. None where the charger reported an icon this
    # does not know, which is a state Emporia may add at any time.
    plugged_in: bool | None
    connected: bool | None
    offline_since: str | None
    fault: str | None


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)


@dataclass(frozen=True)
class DeviceConnection:
    """Whether one device is still answering Emporia, and since when it stopped.

    The distinction the circuits page cannot draw without it. A circuit reading
    of NULL is rendered as a dash whether the clamp measured nothing or the
    whole device died in April, and those are different answers to "why is this
    blank". Emporia knows which it is; this is the only field that says so.

    ``connected`` is None where the device did not report one, which is the
    ordinary rule here: a device nobody spoke for has not been declared healthy.
    """

    connected: bool | None
    offline_since: str | None


def connections_from_status(payload: object) -> dict[int, DeviceConnection]:
    """Every device's connection state in a ``/customers/devices/status`` reply.

    Keyed by ``deviceGid``, so a circuit finds its device's state without the
    caller walking a list per row.

    An unreadable reply yields an empty mapping rather than raising. This runs
    on a timer and the mapping only decorates a page: a shape change at
    Emporia's end must cost the offline marks, never the readings beside them.
    """
    out: dict[int, DeviceConnection] = {}
    if not isinstance(payload, dict):
        return out
    entries = payload.get("devicesConnected")
    if not isinstance(entries, list):
        return out
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        gid = _int_or_none(entry.get("deviceGid"))
        if gid is None:
            continue
        state = entry.get("connected")
        since = entry.get("offlineSince")
        out[gid] = DeviceConnection(
            connected=state if isinstance(state, bool) else None,
            offline_since=since if isinstance(since, str) else None,
        )
    return out


def charger_from_status(payload: object) -> ChargerState | None:
    """The first EV charger in a ``/customers/devices/status`` reply, or None.

    None for an account with no charger, and for a reply this cannot read.
    Neither is an error: most installations have no charger at all, and this
    runs on a timer where a shape change at Emporia's end must degrade to "no
    charger" rather than stop the poller.
    """
    if not isinstance(payload, dict):
        return None
    chargers = payload.get("evChargers")
    if not isinstance(chargers, list) or not chargers:
        return None
    record = chargers[0]
    if not isinstance(record, dict):
        return None
    gid = _int_or_none(record.get("deviceGid"))
    if gid is None:
        return None

    conflicts = [name for key, name in _CHARGER_CONFLICTS if record.get(key) is True]
    load_gid = record.get("loadGid")
    loads = payload.get("loads")
    if isinstance(loads, list):
        for load in loads:
            if isinstance(load, dict) and load.get("loadGid") == load_gid:
                conflicts.extend(name for key, name in _LOAD_CONFLICTS if load.get(key) is True)

    # The same reader the circuits page uses. Two walks of one list would be one
    # fact parsed twice, and the charger tab and the circuits page disagreeing
    # about whether a device is up is exactly the drift that costs.
    connection = connections_from_status(payload).get(gid, DeviceConnection(None, None))

    on = record.get("chargerOn")
    fault = record.get("faultText")
    return ChargerState(
        device_gid=gid,
        rate_a=_int_or_none(record.get("chargingRate")),
        max_rate_a=_int_or_none(record.get("maxChargingRate")),
        on=on if isinstance(on, bool) else None,
        status=str(record.get("status") or ""),
        message=str(record.get("message") or ""),
        conflicts=tuple(conflicts),
        plugged_in=_PLUGGED_ICONS.get(str(record.get("icon") or "")),
        connected=connection.connected,
        offline_since=connection.offline_since,
        fault=fault if isinstance(fault, str) else None,
    )
