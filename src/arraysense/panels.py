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
# The gauges a PV string is plausibly run in. Listed here because the grammar
# refuses anything else, and refusing is the point: a gauge nobody has a
# resistance for cannot become a silent zero loss.
WIRE_GAUGES = frozenset({2, 4, 6, 8, 10, 12, 14})

# Every optional key the tail accepts, named once. The settings registry quotes
# this in the help the page renders, and a test holds the two together: a key
# added here and forgotten there is a setting the owner cannot discover.
KNOWN_STRING_KEYS: tuple[str, ...] = (
    "temp_coeff",
    "noct",
    "mounting",
    "bifacial",
    "installed",
    "degradation",
    "vmp",
    "voc",
    "wire_awg",
    "wire_run_ft",
    "note",
    "panel",
)
_DEFAULT_MOUNTING = "open_rack"
_DEFAULTS: dict[str, float | str] = {**_FLOAT_DEFAULTS, "mounting": _DEFAULT_MOUNTING}

# --- Panel catalogue -----------------------------------------------------------
# Each entry supplies the electrical figures a datasheet states, drawn from a
# real manufacturer document fetched and read in full. A shipped entry is never
# edited in place — a corrected figure ships as a new entry name, because
# efficiency.config_version is bumped by writes under the panels. prefix and a
# value changed inside the code rather than inside the setting would alter what
# every existing installation is expected to produce while leaving every stored
# day agreeing with a version that no longer describes it.
#
# Adding a third format means transcribing a third datasheet first — see
# docs/superpowers/issues/panel-specs.md for the precedent.


@dataclass(frozen=True)
class PanelCatalogueEntry:
    """One generic panel format, its electrical figures, and where they came from.

    Wattage is never supplied — it is the one figure an owner reliably knows,
    printed on the module and on the invoice.
    """

    name: str
    description: str
    vmp: float
    voc: float
    temp_coeff: float
    noct: float
    degradation: float
    # Which document these figures were transcribed from, and when.
    citation: str


PANEL_CATALOGUE: tuple[PanelCatalogueEntry, ...] = (
    PanelCatalogueEntry(
        name="108cell_perc",
        description="108-cell mono PERC (typical)",
        vmp=31.01,
        voc=37.07,
        temp_coeff=-0.35,
        noct=45.0,
        degradation=0.45,
        citation="Runergy HY-DH108P8B-400 datasheet (DH108P8B-Global-Ver3.0), "
        "400 W bin; fetched and read 2026-08-10",
    ),
    PanelCatalogueEntry(
        name="120halfcell_perc",
        description="120-half-cell mono PERC bifacial (typical)",
        vmp=34.5,
        voc=41.4,
        temp_coeff=-0.36,
        noct=45.0,
        degradation=0.5,
        citation="Aptos DNA-120-BF26-370W datasheet (Ref_083124), "
        "370 W bin; fetched and read 2026-08-10",
    ),
)

_CATALOGUE_BY_NAME: dict[str, PanelCatalogueEntry] = {e.name: e for e in PANEL_CATALOGUE}

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
    wire_awg: int | None
    wire_run_ft: float | None
    note: str
    # Which catalogue entry was chosen, or None when the owner typed every
    # value by hand. Stored as a name rather than a table of values so a
    # generic choice is distinguishable from the owner's own entry — the
    # warning requested in the spec.
    panel: str | None
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

    unknown = set(keys) - set(KNOWN_STRING_KEYS)
    if unknown:
        # Refused loudly, never ignored: a typo that quietly became a default
        # would be a config the owner believes is set.
        raise _refuse(line, f"unknown key(s) {sorted(unknown)}; known: {sorted(KNOWN_STRING_KEYS)}")

    # --- resolve panel= before the per-field overrides -------------------------
    # A catalogue entry supplies representative electrical figures from a real
    # datasheet. Each one is applied only when the owner has not already set
    # that key in the tail — an explicit key beats a catalogue value — and each
    # applied field is named in ``defaulted`` so the page can label it as
    # assumed rather than as the owner's own entry.
    panel_name: str | None = None
    owner_keys = set(keys)  # snapshot before catalogue injection
    if "panel" in keys:
        panel_name = keys["panel"]
        entry = _CATALOGUE_BY_NAME.get(panel_name)
        if entry is None:
            raise _refuse(
                line,
                f"unknown panel entry {panel_name!r}; known: {sorted(_CATALOGUE_BY_NAME)}",
            )
        for field_name, field_value in (
            ("vmp", entry.vmp),
            ("voc", entry.voc),
            ("temp_coeff", entry.temp_coeff),
            ("noct", entry.noct),
            ("degradation", entry.degradation),
        ):
            if field_name not in keys:
                keys[field_name] = str(field_value)

    defaulted: set[str] = set()
    for k in _DEFAULTS:
        if k not in keys:
            defaulted.add(k)
    if panel_name is not None:
        entry = _CATALOGUE_BY_NAME[panel_name]
        for field_name in ("vmp", "voc", "temp_coeff", "noct", "degradation"):
            if field_name not in owner_keys:
                defaulted.add(field_name)
    if "installed" not in keys:
        defaulted.add("installed")
    wire_awg: int | None = None
    if "wire_awg" in keys:
        gauge = int(_number(line, "wire_awg", keys["wire_awg"], 0, 20))
        if gauge not in WIRE_GAUGES:
            raise _refuse(line, f"wire_awg must be one of {sorted(WIRE_GAUGES)}, got {gauge}")
        wire_awg = gauge
    wire_run_ft = (
        _number(line, "wire_run_ft", keys["wire_run_ft"], 0, 2000)
        if "wire_run_ft" in keys
        else None
    )
    # Both or neither: a gauge with no length, or a length with no gauge, is
    # half a measurement and cannot produce a resistance. Refused at the door
    # rather than silently ignored, because a wire loss the owner believes is
    # modelled and is not would flatter every figure downstream.
    if (wire_awg is None) != (wire_run_ft is None):
        raise _refuse(line, "wire_awg and wire_run_ft must be given together")

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
        wire_awg=wire_awg,
        wire_run_ft=wire_run_ft,
        note=keys.get("note", ""),
        panel=panel_name,
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
