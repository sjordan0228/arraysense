"""config.py — runtime configuration, read from a TOML file.

Nothing about any particular installation is compiled in: this is published
software, and the next person's dongle is at a different address with a
different serial and a different number of battery modules. The file holds the
serial numbers that identify the hardware, so it lives outside the source tree
and outside version control.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arraysense.settings import SettingsStore

DEFAULT_PATH = Path("/etc/arraysense/config.toml")

# Eleven seconds is the cadence the reference installation is known to sustain.
# It is not an arbitrary round number: the product this replaces polls the same
# dongle in a tight loop with no timer at all, and over half an hour that loop
# measured a 11 s median and a 9-16 s spread. Reading faster than the round trip
# allows gains nothing, because the dongle answers when it answers.
DEFAULT_POLL_INTERVAL = 11.0

# The dongle's own port. Newer firmware removes it and Ethernet dongles never
# had it, which is why the transport is pluggable.
DEFAULT_DONGLE_PORT = 8000

# Which family of inverter to read. Defaults to the one every installation of
# this software has today, so nobody's existing config.toml has to be edited to
# gain a setting that would only tell it what it was already doing.
#
# Not validated here. The list of drivers lives in arraysense.drivers, which
# imports this module to build a source, so checking a name at load time would
# be a cycle. The registry raises a ValueError naming the drivers that do exist,
# and the entry point already reports a ValueError as one line rather than a
# traceback.
DEFAULT_DRIVER = "eg4_luxpower"

# Required whatever the transport: the identity rows are filed under, and
# somewhere to file them.
_REQUIRED = ("inverter_serial", "database_path")

# Required only by the transport that uses them. A serial installation has no
# dongle to name, and demanding a placeholder host from it would make the
# transport choice a fiction.
REQUIRED_BY_TRANSPORT: dict[str, tuple[str, ...]] = {
    "dongle": ("dongle_host", "dongle_serial"),
    "modbus_serial": ("serial_device",),
}


def _required_for(transport: str) -> tuple[str, ...]:
    """Return the settings an installation on ``transport`` cannot start without."""
    return _REQUIRED + REQUIRED_BY_TRANSPORT.get(transport, ())


# Valid transport types. The default preserves the existing dongle behaviour;
# modbus_serial is the alternative path for USB-to-RS485 adapters.
_VALID_TRANSPORTS = frozenset({"dongle", "modbus_serial"})

# How durably a stored reading has to land before the write is called done.
# "full" fsyncs on every commit and is what every installation had before this
# was a choice. "normal" syncs at checkpoint instead: measured on the reference
# Pi through the store's own append, 200 polls cost 207 fsyncs at full and 7 at
# normal. Neither risks corruption — SQLite stays consistent either way — but
# normal can lose the most recent readings if power is cut abruptly, roughly the
# last five minutes at that checkpoint rate.
_VALID_SYNCHRONOUS = frozenset({"full", "normal"})

# Where battery truth comes from. Empty means "derive from the driver": relayed
# when the family relays BMS data, none otherwise — which is every existing
# installation's behaviour, so nobody migrates anything. "direct" is reserved
# by the setup design and refused at driver construction until a battery
# driver exists to honour it; refusing it here would hard-code driver
# knowledge into a module the drivers import.
_VALID_BATTERY_SOURCES = frozenset({"", "relayed", "none", "direct"})


@dataclass(frozen=True)
class Config:
    """Everything the service needs to reach one inverter and store its data.

    Frozen because a running collector holding a settings object that can change
    underneath it is a class of bug nobody enjoys: a reconfiguration means a new
    Config and a restart, both of which are visible.

    Three of these fields carry advice the type cannot. The dongle's address is
    never rediscovered at runtime, so give it a static DHCP lease. The inverter
    serial has to be read off the inverter itself rather than out of another
    tool's logs — on the reference system those two disagreed, and every read
    failed on a serial mismatch until the inverter's own value was used. The
    database path should land on an SSD rather than a Raspberry Pi's SD card,
    which sustained writes wear out.

    Both serials are ten characters, and the dongle's is a credential rather
    than a label — the protocol authenticates with it, so a wrong one is
    refused rather than ignored. Find it on the dongle's label, in the
    router's DHCP list, or broadcast as its WiFi access point name.
    ``poll_interval`` is in seconds.

    ``driver`` names the inverter family, and defaults to the EG4/LuxPower one
    because that is what every installation of this software reads today. The
    names are listed by ``arraysense.drivers``; an unknown one is reported
    there rather than here, so that this module does not have to import the
    drivers that import it.

    ``synchronous`` chooses how durably a reading has to land. The default
    matches what every installation did before it was a choice; "normal" trades
    a bounded amount of recent data on an abrupt power loss for roughly thirty
    times fewer writes, which is worth having on flash storage.

    ``transport`` chooses how to reach the inverter. The default "dongle" uses
    the WiFi dongle's TCP port as before. "modbus_serial" uses a USB-to-RS485
    adapter. When "modbus_serial" is chosen, ``serial_device`` must be set.
    """

    dongle_host: str
    dongle_serial: str
    inverter_serial: str
    database_path: str
    poll_interval: float = DEFAULT_POLL_INTERVAL
    dongle_port: int = DEFAULT_DONGLE_PORT
    driver: str = DEFAULT_DRIVER
    transport: str = "dongle"
    serial_device: str = ""
    serial_baud: int = 19200
    serial_unit_id: int = 1
    synchronous: str = "full"
    model: str = ""
    battery_source: str = ""

    def __post_init__(self) -> None:
        """Reject a configuration that cannot work.

        Checking at construction rather than at first use is the difference
        between a startup that fails naming the field and a service that comes
        up, reports itself healthy, and then fails every poll for as long as
        nobody is watching. A blank serial or a zero interval is only ever
        discovered at the wire otherwise.
        """
        # Before the field check, because "transport must be one of ..." is a
        # more useful first complaint than a missing field the reader has never
        # heard of because it belongs to a transport they did not choose.
        if self.transport not in _VALID_TRANSPORTS:
            raise ValueError(
                f"transport must be one of {sorted(_VALID_TRANSPORTS)}, got {self.transport!r}"
            )
        for field in _required_for(self.transport):
            if not str(getattr(self, field)).strip():
                # Name the transport when the field is only required because of
                # it. "serial_device must be set" alone sends someone hunting
                # through a file where that setting may not even appear.
                if field in REQUIRED_BY_TRANSPORT.get(self.transport, ()):
                    raise ValueError(f"{field} must be set when transport is {self.transport!r}")
                raise ValueError(f"{field} must be set")
        if self.poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {self.poll_interval}")
        if not 1 <= self.dongle_port <= 65535:
            raise ValueError(f"dongle_port must be a valid port, got {self.dongle_port}")
        # Blank rather than absent, because absent means the default. Someone
        # who wrote ``driver = ""`` meant to name one and did not, and letting
        # it fall through to the registry would report it as "no such driver:
        # ''", which reads like a bug in the software rather than in the file.
        if not self.driver.strip():
            raise ValueError("driver must be set")
        if self.synchronous not in _VALID_SYNCHRONOUS:
            raise ValueError(
                f"synchronous must be one of {sorted(_VALID_SYNCHRONOUS)}, got {self.synchronous!r}"
            )
        if self.battery_source not in _VALID_BATTERY_SOURCES:
            raise ValueError(
                f"battery_source must be one of {sorted(s for s in _VALID_BATTERY_SOURCES if s)}"
                f" or unset, got {self.battery_source!r}"
            )
        if self.serial_baud <= 0:
            raise ValueError(f"serial_baud must be positive, got {self.serial_baud}")
        # 0 is the Modbus broadcast address: it is write-only by specification
        # and never answers a read, so a collector pointed at it would poll a
        # silent bus forever. 248 upward is reserved.
        if not 1 <= self.serial_unit_id <= 247:
            raise ValueError(f"serial_unit_id must be between 1 and 247, got {self.serial_unit_id}")


def _number(data: dict[str, object], field: str, default: float) -> float:
    """Read one numeric setting, turning a wrong type into a useful ValueError.

    TOML holds a list or a table quite happily where a number belongs, and
    handing one to ``float`` raises TypeError. That escapes the whole loading
    path, because the entry point catches FileNotFoundError and ValueError and
    nothing else — so a mistyped value would reach a first-time installer as a
    traceback rather than as the one line naming the setting.

    A boolean is rejected rather than converted. Python counts ``True`` as an
    integer, so ``poll_interval = true`` would otherwise become a one-second
    poll instead of an error.
    """
    value = data.get(field, default)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be a number, got {value!r}")
    return float(value)


def load(path: Path | str = DEFAULT_PATH) -> Config:
    """Read configuration from a TOML file.

    Everything wrong with the file is found here, before a Config exists and
    long before the first poll, and the missing setting is named in the message.
    That matters because the usual reader of this failure is someone installing
    the service on their own hardware for the first time, with no way to guess
    which setting the failure was about.

    For the same reason an absent file does not surface as a bare
    FileNotFoundError on a path in /etc — it says to copy the shipped example
    and what to fill in. Malformed TOML is re-raised as ValueError so a caller
    has one exception type covering every way the file's contents can be wrong,
    rather than needing to know that tomllib was involved.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"no configuration at {path}. Copy config.example.toml and fill in "
            "how to reach the inverter — a dongle address, or a serial device "
            'path with transport = "modbus_serial" — and the inverter serial.'
        )
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc

    missing = [
        f
        for f in _required_for(str(data.get("transport", "dongle")))
        if not str(data.get(f, "")).strip()
    ]
    if missing:
        raise ValueError(f"{path} is missing required setting(s): {', '.join(missing)}")

    return Config(
        # ``get`` rather than indexing: a serial installation legitimately has
        # no dongle settings, and the missing check above has already refused
        # anything genuinely required for the chosen transport.
        dongle_host=str(data.get("dongle_host", "")),
        dongle_serial=str(data.get("dongle_serial", "")),
        inverter_serial=str(data["inverter_serial"]),
        database_path=str(data["database_path"]),
        poll_interval=_number(data, "poll_interval", DEFAULT_POLL_INTERVAL),
        dongle_port=round(_number(data, "dongle_port", DEFAULT_DONGLE_PORT)),
        driver=str(data.get("driver", DEFAULT_DRIVER)),
        transport=str(data.get("transport", "dongle")),
        serial_device=str(data.get("serial_device", "")),
        serial_baud=round(_number(data, "serial_baud", 19200)),
        serial_unit_id=round(_number(data, "serial_unit_id", 1)),
        synchronous=str(data.get("synchronous", "full")),
        model=str(data.get("model", "")),
        battery_source=str(data.get("battery_source", "")),
    )


def effective(config: Config, settings: SettingsStore) -> Config:
    """Merge the stored settings over the file configuration.

    The settings page is the newer authority. Someone who changes a value there
    has to see it take effect rather than be silently overruled by a file they
    cannot reach from a tablet on a wall.

    Only settings actually stored take part, and a stored empty string is
    ignored. Every connection setting defaults to empty, so an untouched one
    would otherwise blank a working serial and break every poll from the next
    restart onward.
    """
    stored = settings.overrides()

    def pick(key: str, fallback: object) -> object:
        value = stored.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            return fallback
        return value

    return replace(
        config,
        dongle_host=str(pick("connection.dongle_host", config.dongle_host)),
        dongle_serial=str(pick("connection.dongle_serial", config.dongle_serial)),
        inverter_serial=str(pick("connection.inverter_serial", config.inverter_serial)),
        poll_interval=float(pick("collector.poll_interval", config.poll_interval)),  # type: ignore[arg-type]
        # Registered in settings.py — an unregistered overlay here is dead
        # code, which is exactly how the first attempt at these failed review.
        transport=str(pick("connection.transport", config.transport)),
        serial_device=str(pick("connection.serial_device", config.serial_device)),
        serial_baud=round(float(pick("connection.serial_baud", config.serial_baud))),  # type: ignore[arg-type]
        serial_unit_id=round(float(pick("connection.serial_unit_id", config.serial_unit_id))),  # type: ignore[arg-type]
        model=str(pick("connection.model", config.model)),
        battery_source=str(pick("connection.battery_source", config.battery_source)),
    )


def example_toml() -> str:
    """Return a commented example configuration.

    Shipped as ``config.example.toml`` so a new user has something to copy
    rather than a schema to infer.
    """
    return (
        "# Solar ArraySense configuration.\n"
        "# Copy to /etc/arraysense/config.toml and fill in your own values.\n"
        "# This file identifies your hardware — keep it out of version control.\n"
        "\n"
        "# Address of the inverter's WiFi dongle. Give it a static DHCP lease.\n"
        'dongle_host = "192.168.1.50"\n'
        "\n"
        "# The dongle's 10-character serial. On its label, in your router's DHCP\n"
        "# list, or broadcast as the dongle's WiFi access point name.\n"
        'dongle_serial = "BA12345678"\n'
        "\n"
        "# The inverter's 10-character serial. Read it from the inverter itself:\n"
        "# other tools have been observed reporting a different value, and a\n"
        "# mismatch makes every read fail.\n"
        'inverter_serial = "CE12345678"\n'
        "\n"
        "# Seconds between reads. The dongle answers when it answers, so asking faster\n"
        "# than the round trip takes gains nothing.\n"
        f"poll_interval = {DEFAULT_POLL_INTERVAL}\n"
        "\n"
        "# Which family of inverter to read. Leave this alone unless you know you\n"
        "# need something else: it covers the EG4 and LuxPower hybrids reached over\n"
        "# the WiFi dongle. A wrong name is reported at startup along with the list\n"
        "# of names that work.\n"
        f'driver = "{DEFAULT_DRIVER}"\n'
        "\n"
        "# Where the database is written. Prefer an SSD over a Pi's SD card.\n"
        'database_path = "/var/lib/arraysense/arraysense.db"\n'
        "\n"
        "# How durably a reading must land before the write is done. 'full'\n"
        "# fsyncs every commit and is the safe default. 'normal' syncs at\n"
        "# checkpoint instead — measured on a Raspberry Pi, 200 polls cost 207\n"
        "# fsyncs at full and 7 at normal — which matters on flash storage that\n"
        "# wears out. Neither risks corruption; 'normal' can lose the most recent\n"
        "# readings, roughly the last five minutes, if power is cut abruptly.\n"
        'synchronous = "full"\n'
        "\n"
        "# How to reach the inverter: 'dongle' (default) for the WiFi dongle's\n"
        "# TCP port, or 'modbus_serial' for a USB-to-RS485 adapter. When set to\n"
        "# 'modbus_serial', serial_device must also be set.\n"
        'transport = "dongle"\n'
        "\n"
        "# Device path for the USB-to-RS485 adapter. Only used when transport\n"
        "# is 'modbus_serial'. Nothing expands a glob here, so give a real path:\n"
        "# '/dev/ttyUSB0', or better a stable one that survives replugging, such\n"
        "# as '/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0' or a udev symlink.\n"
        'serial_device = ""\n'
        "\n"
        "# Baud rate for the serial connection. Defaults to 19200, which is the\n"
        "# standard for EG4/LuxPower inverters.\n"
        "serial_baud = 19200\n"
        "\n"
        "# Modbus unit ID for the serial connection. Defaults to 1.\n"
        "serial_unit_id = 1\n"
    )
