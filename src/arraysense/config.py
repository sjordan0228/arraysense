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

_REQUIRED = ("dongle_host", "dongle_serial", "inverter_serial", "database_path")


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
    """

    dongle_host: str
    dongle_serial: str
    inverter_serial: str
    database_path: str
    poll_interval: float = DEFAULT_POLL_INTERVAL
    dongle_port: int = DEFAULT_DONGLE_PORT

    def __post_init__(self) -> None:
        """Reject a configuration that cannot work.

        Checking at construction rather than at first use is the difference
        between a startup that fails naming the field and a service that comes
        up, reports itself healthy, and then fails every poll for as long as
        nobody is watching. A blank serial or a zero interval is only ever
        discovered at the wire otherwise.
        """
        for field in _REQUIRED:
            if not str(getattr(self, field)).strip():
                raise ValueError(f"{field} must be set")
        if self.poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {self.poll_interval}")
        if not 1 <= self.dongle_port <= 65535:
            raise ValueError(f"dongle_port must be a valid port, got {self.dongle_port}")


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
            "your dongle address and serial numbers."
        )
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from exc

    missing = [f for f in _REQUIRED if not str(data.get(f, "")).strip()]
    if missing:
        raise ValueError(f"{path} is missing required setting(s): {', '.join(missing)}")

    return Config(
        dongle_host=str(data["dongle_host"]),
        dongle_serial=str(data["dongle_serial"]),
        inverter_serial=str(data["inverter_serial"]),
        database_path=str(data["database_path"]),
        poll_interval=_number(data, "poll_interval", DEFAULT_POLL_INTERVAL),
        dongle_port=round(_number(data, "dongle_port", DEFAULT_DONGLE_PORT)),
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
        "# Where the database is written. Prefer an SSD over a Pi's SD card.\n"
        'database_path = "/var/lib/arraysense/arraysense.db"\n'
    )
