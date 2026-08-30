"""setup.py — everything the setup wizard renders, computed in one place.

The wizard and the settings section are one renderer in two shells, and this
module is the single source both draw from: the manufacturer tree with its
cited model deltas, which fields each transport requires, which battery
sources each family supports, the serial ports the machine can see, and the
current configuration with secrets redacted. A page computing any of this a
second way is the drift the settings page's own rules exist to forbid.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from arraysense import drivers
from arraysense.config import REQUIRED_BY_TRANSPORT, Config
from arraysense.drivers.base import resolve_model


def list_serial_ports(dev_root: Path | str = "/dev") -> list[dict[str, str]]:
    """Return the serial adapters the machine can see, stable names first.

    Reads /dev/serial/by-id, whose entries survive replugging because they
    encode the adapter's identity rather than its enumeration order. Each
    entry carries the stable path and the /dev node it currently points at.
    A machine with no adapters — or no such directory at all — answers with
    an empty list, because "nothing detected" is an answer, not an error.
    """
    by_id = Path(dev_root) / "serial" / "by-id"
    if not by_id.is_dir():
        return []
    ports: list[dict[str, str]] = []
    for entry in sorted(by_id.iterdir()):
        ports.append({"stable": str(entry), "target": os.path.realpath(entry)})
    return ports


def _redact(value: str) -> str:
    """Mask exactly as the settings API masks, so an echo is recognisable.

    One masker, not two: the apply endpoint discards masked echoes by the
    settings module's own rule, and a second masking format here would slip
    straight past that check and overwrite a real serial with dots.
    """
    from arraysense.settings import _mask

    return _mask(value)


def describe_setup(config: Config, dev_root: Path | str = "/dev") -> dict[str, Any]:
    """The one payload both setup shells render from.

    Everything here is registry metadata, a directory listing and the loaded
    configuration — nothing scans a tier, which is why the route serving it
    may stay on the event loop. The connection values are redacted on the way
    out because the page in front of them has no authentication (#34) and a
    serial number is an installation secret.
    """
    by_maker: dict[str, list[dict[str, Any]]] = {}
    battery_sources: dict[str, list[str]] = {}
    for entry in drivers.entries():
        models = []
        for model in entry.models:
            resolved = resolve_model(entry.capabilities, model)
            cited = [
                name
                for name, delta in (
                    ("pv_strings", model.pv_strings),
                    ("battery_module_slots", model.battery_module_slots),
                )
                if delta is not None
            ]
            models.append(
                {
                    "name": model.name,
                    "citation": model.citation,
                    # Which of the values below the citation actually covers.
                    # The rest are inherited family defaults, and a page must
                    # be able to label the difference — a cited fact and a
                    # conservative default are different claims.
                    "cited_fields": cited,
                    "pv_strings": resolved.pv_strings,
                    "battery_module_slots": resolved.battery_module_slots,
                }
            )
        by_maker.setdefault(entry.manufacturer, []).append(
            {"driver": entry.name, "description": entry.description, "models": models}
        )
        choices = ["relayed", "none"] if entry.capabilities.relays_battery else ["none"]
        battery_sources[entry.name] = choices

    manufacturers = [
        {
            "name": maker,
            "families": families,
            "models": [m for family in families for m in family["models"]],
        }
        for maker, families in sorted(by_maker.items())
    ]
    return {
        "manufacturers": manufacturers,
        "transports": {name: list(fields) for name, fields in REQUIRED_BY_TRANSPORT.items()},
        "battery_sources": battery_sources,
        "ports": list_serial_ports(dev_root),
        "current": {
            "driver": config.driver,
            "model": config.model,
            "transport": config.transport,
            "battery_source": config.battery_source,
            "serial_device": config.serial_device,
            "serial_baud": config.serial_baud,
            "serial_unit_id": config.serial_unit_id,
            "dongle_host": _redact(config.dongle_host),
            "dongle_serial": _redact(config.dongle_serial),
            "inverter_serial": _redact(config.inverter_serial),
        },
    }


def render_config(values: dict[str, object]) -> str:
    """Render the first and only software-written config.toml.

    First run has no file, so there is nothing to brick; every run after it
    edits the settings overlay and never this. Only keys with non-empty
    values are written — an empty dongle_host line would read as a setting
    somebody chose rather than a transport nobody uses.
    """
    lines = ["# Written by first-run setup. Later changes go through the settings page."]
    for key in (
        "driver",
        "model",
        "transport",
        "serial_device",
        "serial_baud",
        "serial_unit_id",
        "dongle_host",
        "dongle_port",
        "dongle_serial",
        "inverter_serial",
        "battery_source",
        "database_path",
        "poll_interval",
    ):
        value = values.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            lines.append(f'{key} = "{value}"')
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"
