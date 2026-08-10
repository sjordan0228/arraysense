"""drivers — which inverter families this build knows how to read, by name.

Adding support for an inverter should mean adding a directory. Before this
existed it meant editing the collector: there was one adapter, nothing chose it,
and it was named after the library it wrapped rather than the hardware it spoke
to. Now the collector polls a source it looked up by name, and the name comes
from the configuration.

Registration is explicit and lives at the bottom of this file. Nothing scans the
directory, for the same reason ``api/app.py`` names its pages one at a time: a
file that lands in the tree — half-finished, copied in to read, left behind by a
merge — must not become live code because it happens to be sitting there. The
cost is one line per driver, paid by the person adding it, in the file that
lists what this build supports.

Out-of-tree drivers are not built for. Drivers arrive as pull requests, and
nothing here makes an external one impossible; an entry point mechanism can be
added the day somebody actually wants one, and would be guesswork today.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from arraysense.drivers.base import (
    Capabilities,
    DeviceIdentity,
    DriverEntry,
    EnergyReporting,
    InverterDriver,
    InverterSource,
    find_model,
)

if TYPE_CHECKING:
    from arraysense.config import Config

__all__ = [
    "Capabilities",
    "DeviceIdentity",
    "DriverEntry",
    "EnergyReporting",
    "InverterDriver",
    "InverterSource",
    "create",
    "entries",
    "get",
    "names",
    "register",
]

_REGISTRY: dict[str, DriverEntry] = {}


def register(entry: DriverEntry) -> None:
    """Add a driver to the registry, refusing a name already taken.

    A collision is refused rather than resolved. Two drivers under one name
    means whichever module was imported last wins, silently, and the symptom is
    an installation reading its inverter through somebody else's mapping.
    """
    if entry.name in _REGISTRY:
        raise ValueError(f"driver {entry.name!r} is already registered")
    _REGISTRY[entry.name] = entry


def names() -> tuple[str, ...]:
    """Every registered driver name, in alphabetical order."""
    return tuple(sorted(_REGISTRY))


def entries() -> tuple[DriverEntry, ...]:
    """Every registered driver, in alphabetical order by name.

    For anything listing what this build supports — a settings page offering the
    choice, a capabilities endpoint — without constructing a source to ask. The
    dongle admits one client, so building one to read a description would be a
    real cost paid for nothing.
    """
    return tuple(_REGISTRY[name] for name in names())


def get(name: str) -> DriverEntry:
    """Look up one driver by name.

    A miss is a ValueError listing what does exist, and both halves of that
    matter. ValueError because the entry point already catches it alongside the
    configuration errors and logs the message as one line instead of a
    traceback; the listing because the reader has mistyped a name in a TOML
    file and needs the spelling, which no amount of KeyError will give them.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        known = ", ".join(names()) or "none"
        raise ValueError(f"no such driver: {name!r}. Available drivers: {known}") from None


def create(config: Config) -> InverterDriver:
    """Build the source for the driver this configuration names.

    The one call the entry point makes, and the reason nothing above the
    driver layer imports a driver: which family is being read is a string in a
    file, resolved here. Model and battery source are validated here too,
    beside the driver lookup, because the registry is the only layer that
    knows what each family offers — config cannot import it, and a page must
    never learn these rules a second way.
    """
    entry = get(config.driver)
    if config.model:
        find_model(entry, config.model)
    if config.battery_source == "direct":
        raise ValueError(
            "battery_source 'direct' is not yet available; the battery axis "
            "design reserves it for a dedicated battery driver"
        )
    if config.battery_source == "relayed" and not entry.capabilities.relays_battery:
        raise ValueError(
            f"driver {entry.name!r} does not relay battery data; battery_source must be 'none'"
        )
    return entry.build(config)


# Explicit registration. One line per driver, and a driver that is not on this
# list is not part of this build no matter what is in the tree.
def _register_builtin_drivers() -> None:
    """Register the drivers shipped with this build.

    Imported inside the function rather than at the top of the module so that
    the import and the registration it exists for sit together: a driver added
    to one list and not the other is in the tree and not in the build, and the
    two lines are a screen apart rather than a file apart.

    It also keeps the package importable by a driver that reaches back for
    ``register`` or ``get``. Neither of the current two does — both import
    ``arraysense.drivers.base`` directly, which resolves at the top of this
    file perfectly well — but one that did would be importing a package whose
    body had not yet defined either name.
    """
    from arraysense.drivers import eg4_luxpower, fake

    register(eg4_luxpower.DRIVER)
    register(fake.DRIVER)


_register_builtin_drivers()
