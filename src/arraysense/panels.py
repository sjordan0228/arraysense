"""panels.py — the per-string array grammar, parsed in exactly one place.

The efficiency model reads the owner's array from configuration, and the
settings registry stores flat text — so strings live as one multiline setting
with a line grammar, the same shape the tariff took and for the same reason:
the moment two parsers exist (one in Python, one in a page) they disagree, and
this config drives the expected-production model the performance score hangs
off. The browser composes this grammar; only this module reads it.

A default is applied and *named* — each StringSpec carries which fields fell
back, so a page can label "assumed -0.35 %/°C" instead of presenting a guess
as the owner's entry. Refusals quote the offending line, because "invalid
config" against a ten-line setting is a hunt, not an error message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MOUNTINGS: tuple[str, ...] = ("open_rack", "close_roof", "ground")

EXAMPLE_STRINGS = (
    "East | 1 | 9 | 410 | 25 | 90 | bifacial=9\n"
    "West | 2 | 9 | 410 | 25 | 270 | temp_coeff=-0.34 installed=2024-08"
)

# Defaults are grammar facts, named once. -0.35 %/°C and NOCT 45 are ordinary
# crystalline-module figures; 0.5 %/yr is the industry's usual degradation
# assumption. Every one is overridable per string, and every fallback is
# reported in ``defaulted`` so no page presents an assumption as an entry.
_FLOAT_DEFAULTS: dict[str, float] = {
    "temp_coeff": -0.35,
    "noct": 45.0,
    "bifacial": 0.0,
    "degradation": 0.5,
}
_DEFAULT_MOUNTING = "open_rack"
_DEFAULTS: dict[str, float | str] = {**_FLOAT_DEFAULTS, "mounting": _DEFAULT_MOUNTING}

_INSTALLED = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# key=value tokens; a quoted value may hold spaces. Built for the tail only.
# A quoted value may hold spaces and escaped quotes (\"); a bare value may not.
# The quoted branch accepts any run of non-quote characters and \" pairs, so a
# note that quotes something round-trips instead of being refused at the door.
_TAIL_TOKEN = re.compile(r'(\w+)=(?:"((?:[^"\\]|\\.)*)"|(\S+))')


@dataclass(frozen=True)
class StringSpec:
    """One string of the array, defaults resolved and their names kept."""

    name: str
    mppt: int
    panels: int
    watts: float
    tilt: float
    azimuth: float
    temp_coeff: float
    noct: float
    mounting: str
    bifacial_pct: float
    installed: str | None
    degradation: float
    vmp: float | None
    voc: float | None
    note: str
    defaulted: frozenset[str]


def _unescape(value: str) -> str:
    """Undo the composer's escaping of a quoted value."""
    return value.replace('\\"', '"').replace("\\\\", "\\")


def _refuse(line: str, why: str) -> ValueError:
    return ValueError(f"{why} in string line: {line.strip()!r}")


def _number(line: str, field: str, raw: str, lo: float, hi: float) -> float:
    try:
        value = float(raw)
    except ValueError:
        raise _refuse(line, f"{field} must be a number, got {raw!r}") from None
    if not lo <= value <= hi:
        raise _refuse(line, f"{field} must be between {lo:g} and {hi:g}, got {value:g}")
    return value


def _parse_line(line: str) -> StringSpec:
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 6:
        raise _refuse(
            line, "a string needs six fields (name | mppt | panels | watts | tilt | azimuth)"
        )
    name = parts[0]
    if not name:
        raise _refuse(line, "the string needs a name")
    mppt = int(_number(line, "mppt", parts[1], 1, 32))
    panels = int(_number(line, "panels", parts[2], 1, 100))
    watts = _number(line, "watts", parts[3], 50, 1000)
    tilt = _number(line, "tilt", parts[4], 0, 90)
    azimuth = _number(line, "azimuth", parts[5], 0, 360)

    tail = " | ".join(parts[6:]) if len(parts) > 6 else ""
    keys: dict[str, str] = {}
    if tail:
        for match in _TAIL_TOKEN.finditer(tail):
            key = match.group(1)
            quoted = match.group(2)
            value = _unescape(quoted) if quoted is not None else match.group(3)
            if key in keys:
                raise _refuse(line, f"{key} is given twice")
            keys[key] = value
        # What the tokens did not consume. Separators BETWEEN tokens are
        # structural — the composer writes "bifacial=9 | note=..." — so only
        # text that is not a separator counts as unreadable, and a separator
        # with nothing after it is the typo worth naming.
        leftovers = _TAIL_TOKEN.sub("", tail).strip()
        unreadable = leftovers.replace("|", "").strip()
        if unreadable:
            raise _refuse(line, f"could not read {unreadable!r}; the tail is key=value pairs")
        if leftovers and tail.rstrip().endswith("|"):
            raise _refuse(line, "the tail ends with a stray separator")

    known = {
        "temp_coeff",
        "noct",
        "mounting",
        "bifacial",
        "installed",
        "degradation",
        "vmp",
        "voc",
        "note",
    }
    unknown = set(keys) - known
    if unknown:
        # Refused loudly, never ignored: a typo that quietly became a default
        # would be a config the owner believes is set.
        raise _refuse(line, f"unknown key(s) {sorted(unknown)}; known: {sorted(known)}")

    defaulted = {k for k in _DEFAULTS if k not in keys}
    if "installed" not in keys:
        defaulted.add("installed")

    temp_coeff = (
        _number(line, "temp_coeff", keys["temp_coeff"], -1.0, 0.0)
        if "temp_coeff" in keys
        else _FLOAT_DEFAULTS["temp_coeff"]
    )
    noct = (
        _number(line, "noct", keys["noct"], 20, 90) if "noct" in keys else _FLOAT_DEFAULTS["noct"]
    )
    mounting = keys.get("mounting", _DEFAULT_MOUNTING)
    if mounting not in MOUNTINGS:
        raise _refuse(line, f"mounting must be one of {MOUNTINGS}, got {mounting!r}")
    bifacial = (
        _number(line, "bifacial", keys["bifacial"], 0, 40)
        if "bifacial" in keys
        else _FLOAT_DEFAULTS["bifacial"]
    )
    degradation = (
        _number(line, "degradation", keys["degradation"], 0, 5)
        if "degradation" in keys
        else _FLOAT_DEFAULTS["degradation"]
    )
    installed = keys.get("installed")
    if installed is not None and not _INSTALLED.match(installed):
        raise _refuse(line, f"installed must be YYYY-MM, got {installed!r}")
    vmp = _number(line, "vmp", keys["vmp"], 10, 100) if "vmp" in keys else None
    voc = _number(line, "voc", keys["voc"], 10, 120) if "voc" in keys else None

    return StringSpec(
        name=name,
        mppt=mppt,
        panels=panels,
        watts=watts,
        tilt=tilt,
        azimuth=azimuth,
        temp_coeff=temp_coeff,
        noct=noct,
        mounting=mounting,
        bifacial_pct=bifacial,
        installed=installed,
        degradation=degradation,
        vmp=vmp,
        voc=voc,
        note=keys.get("note", ""),
        defaulted=frozenset(defaulted),
    )


def parse_strings(text: str) -> tuple[StringSpec, ...]:
    """Parse the panels.strings setting: every string of the array, or nothing.

    Empty text is a valid unconfigured array, distinct from a refusal — the
    settings page saves an empty box without ceremony and the model simply has
    no array to expect production from. Comments (#) and blank lines are for
    the owner's own annotations in the raw text view.
    """
    strings: list[StringSpec] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        strings.append(_parse_line(line))
    names = [s.name for s in strings]
    for name in names:
        if names.count(name) > 1:
            raise ValueError(f"two strings are both named {name!r}; names identify strings")
    return tuple(strings)
