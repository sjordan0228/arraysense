"""base.py — what a device driver is: the source protocol, identity, capabilities.

Everything above this line polls an ``InverterSource`` and never learns which
inverter answered. That was already true of the collector; what was missing was
any way for a driver to say what it *is* and what it can produce, so the pages
and the schema had no choice but to assume the reference 18kPV.

Two things a driver declares beyond the reads themselves.

Identity says which physical unit this is, so two inverters polled into one
database keep their rows apart, and so a page can name the thing it is drawing.

Capabilities say what the hardware can do at all — which is not the same
question as whether a particular reading arrived. Absent data is not zero, and
absent *capability* is not absent data: an inverter with one PV string reports
nothing for a third string because there is no third string, and a NULL that
could mean either is a NULL nobody can read.

The declaration is deliberately shaped against a device this repository does
not support. The EG4 3000 EHV has one PV string, no backup panel, and no kWh
counters of any kind — its energy could only be integrated from power. This
project's energy model reads the inverter's own lifetime counters and never
integrates, so "counted" versus "estimated" has to be sayable or a day's kWh on
such a device would be presented with the same authority as a metered one.
Designed only against the 18kPV, every one of those distinctions would have
been assumed away.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from arraysense.metrics import column_names

if TYPE_CHECKING:
    from arraysense.config import Config
    from arraysense.models import Sample


class DeviceIdentityError(Exception):
    """The inverter that answered is not the one this installation configured.

    Readings are filed under the configured serial, so if the wire reaches a
    different unit than the settings claim, perfectly valid readings land in
    another inverter's history — and nothing downstream can tell, because every
    row looks exactly as it should. Refusing to collect is recoverable; a
    history silently merged from two machines is not.

    Deliberately outside every error tuple the collector catches. This is a
    misconfiguration that cannot heal itself, so it must stop the loop where a
    watchdog and an operator can see it, rather than be filed as a gap and
    retried politely forever.
    """


class SampleBuildError(Exception):
    """The inverter answered and the reply could not be made into a sample.

    ValueError is raised both by our own decoding mistakes and by a sample
    constructor refusing malformed data; the driver wraps the latter so the
    collector can record a driver-initiated gap rather than killing the loop.
    This exception marks that boundary: a SampleBuildError is a rejected reply,
    not a transport failure, and is treated as a gap with backoff.
    """


# A metric belonging to one numbered PV string, as in ``pv2_voltage_v``. The
# number is what makes a declaration checkable against the string count.
#
# Two or more digits are matched deliberately. Nothing supported has ten
# strings — the LuxPower family tops out at six MPPTs — but a single-digit
# pattern fails silently rather than loudly if one ever does: ``pv10_power_w``
# would read as not-a-string-metric, so the count check would call string 10
# missing, the beyond-the-count check would not refuse it, and the model
# resolver would carry it through unchanged. A leading zero is still refused,
# because ``pv0_`` names no string.
_STRING_METRIC = re.compile(r"^pv([1-9]\d*)_")

# A registry column belonging to one battery slot, capturing the template it was
# expanded from.
_MODULE_COLUMN = re.compile(r"^battery_module\d+_(.+)$")


def _known_module_templates() -> frozenset[str]:
    """Every per-module template the registry expands, read back off the columns.

    The registry's own template list is private; ``column_names()`` is the
    public surface, and the templates are recoverable from it because every
    expanded name still carries the one it came from. Deriving them rather than
    keeping a copy here is what lets ``expand_module_metrics`` refuse a typo
    without owning a second list that could disagree with the registry.
    """
    return frozenset(
        match.group(1) for name in column_names() if (match := _MODULE_COLUMN.match(name))
    )


def expand_module_metrics(templates: Iterable[str]) -> frozenset[str]:
    """Turn per-module metric templates into the registry names for every slot.

    A driver declares ``soc_pct`` once; the registry holds
    ``battery_module1_soc_pct`` through to however many slots the inverter
    exposes. Doing the expansion by reading the registry rather than by
    composing names from a slot count means no driver carries a copy of that
    count, and a registry that grows a fifth slot needs no driver edited.

    A template nothing in the registry matches raises here, naming it. It has to
    be caught at this point and not by Capabilities: everything this returns was
    read out of ``column_names()`` in the first place, so a name that does not
    exist expands to nothing at all and leaves the registry check with nothing
    to object to. Silence there is not a driver declaring less — it is a
    mistyped template that becomes a column the schema never creates and a
    reading dropped on the way to the store, which is the one failure this
    project exists to prevent. Loud at import, where a driver's capabilities are
    built, is the only place it costs nothing.

    An empty set of templates is not a mistake and does not raise: a device that
    reports one bank-level summary and no per-module telemetry declares none.
    """
    wanted = set(templates)
    unknown = sorted(wanted - _known_module_templates())
    if unknown:
        raise ValueError(f"no such per-module metric(s) in the registry: {', '.join(unknown)}")
    return frozenset(
        name
        for name in column_names()
        if (match := _MODULE_COLUMN.match(name)) and match.group(1) in wanted
    )


class EnergyReporting(StrEnum):
    """Where a driver's kWh figures come from.

    Not a detail of implementation. Every energy figure this project shows is
    read from a counter the inverter maintains itself — a revenue-grade-ish
    number that survives our downtime, because the inverter kept counting while
    we were not watching. A device without those counters can still be given a
    kWh figure, by integrating power over the samples that were collected, but
    that figure is a different kind of thing: it is missing whatever happened
    during a gap, and it inherits the error of the poll cadence.

    Presenting both as "today: 41.2 kWh" with nothing to tell them apart is how
    an estimate gets treated as a meter reading. So the driver says which it is
    and the page can say so too.
    """

    COUNTED = "counted"
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class Capabilities:
    """What a device can do, and which metrics it therefore produces.

    ``metrics`` is a set of names from ``arraysense.metrics`` and never a set of
    specs. Name, unit, scale and plausible bounds are physics: they do not vary
    by device, they live in the registry, and a driver that carried its own copy
    would drift from the validation the first time a bound moved. Adding a
    metric stays a one-line change there.

    The flags beside it answer questions the metric set cannot. A driver that
    produces no ``eps_*`` reading might have no backup panel, or might have one
    that is idle; a driver that produces ``pv1_power_w`` and nothing further
    might have one string or three with two disconnected. Only the device knows,
    so only the driver can say.

    ``energy`` is the flag with teeth: see ``EnergyReporting``. Note that it
    says nothing about whether energy metrics are present — an estimating driver
    still produces them, having integrated them — only about where they came
    from.
    """

    pv_strings: int
    energy: EnergyReporting
    metrics: frozenset[str]
    # A backup or EPS panel: loads the inverter keeps alive with the grid down.
    # The 18kPV has one and the reference installation runs its whole house
    # behind it; the 3000 EHV has none at all.
    backup_output: bool = False
    generator_input: bool = False
    # Split-phase is the North American 120/240 V service the reference system
    # runs on, and the reason grid power is worth storing per leg: a house
    # importing on one leg while exporting on the other nets to nearly zero.
    split_phase: bool = False
    three_phase: bool = False
    parallel_capable: bool = False
    # Whether the device reports each battery module separately, or only a
    # bank-level summary. Per-pack drift is invisible in the summary.
    per_module_battery: bool = False
    # Whether this family relays BMS data through its own connection at all.
    # per_module_battery says how deep the relay goes; this says whether it
    # exists. A family that relays nothing can only offer battery_source
    # "none", and the setup endpoint derives its choices from exactly this.
    relays_battery: bool = False
    # How many battery module slots the relay can carry. The registry expands
    # per-module metric names to a fixed ceiling because it cannot import the
    # drivers that import it; this is the per-family truth pages should render
    # from. Must not exceed the registry's expansion — a test enforces it.
    battery_module_slots: int = 0
    # How the driver reaches the inverter: "dongle" for the WiFi dongle's TCP
    # port, "modbus_serial" for a USB-to-RS485 adapter. This is a constant for
    # a given installation. The registry entry carries the family default; a
    # built source reports what its own configuration actually chose, because a
    # page saying "connected by dongle" over a serial link would be worse than
    # saying nothing at all.
    transport: str = "dongle"
    # Manufacturer's conversion figures for the model, carried as a cited
    # ConversionSpec. None at family level because a family has no single
    # answer; ``resolve_model`` applies the model's. These are facts with a
    # provenance, never editable settings, and nothing consumes them
    # arithmetically.
    conversion: ConversionSpec | None = None

    def __post_init__(self) -> None:
        """Reject a declaration that contradicts itself or the metric registry.

        Checked at construction because a driver's capabilities are built once
        at import: a typo found here names the metric at startup, and the same
        typo found later is a column quietly missing from every row ever
        written.

        The string check is the one worth the code. Declaring ``pv3_power_w``
        on a one-string inverter creates a column that can never be filled, and
        a NULL in it then means both "there is no third string" and "the third
        string reported nothing" — which is the distinction this whole layer
        exists to keep.
        """
        if self.pv_strings < 0:
            raise ValueError(f"pv_strings cannot be negative, got {self.pv_strings}")
        unknown = sorted(self.metrics - set(column_names()))
        if unknown:
            raise ValueError(f"no such metric(s) in the registry: {', '.join(unknown)}")
        beyond = sorted(
            name
            for name in self.metrics
            if (match := _STRING_METRIC.match(name)) and int(match.group(1)) > self.pv_strings
        )
        if beyond:
            raise ValueError(
                f"declares {self.pv_strings} PV string(s) but produces {', '.join(beyond)}"
            )
        # And the converse: a string declared but never reported. The check
        # above refuses metrics past the count; this one refuses a count the
        # metrics do not reach. A driver that cannot read a string individually
        # declares the strings it can read, not the strings the array has —
        # otherwise the page draws a card per declared string and leaves the
        # unreported ones permanently blank, which is what #90 was.
        if self.pv_strings > 0:
            present = {
                int(match.group(1))
                for name in self.metrics
                if (match := _STRING_METRIC.match(name))
            }
            if missing := [n for n in range(1, self.pv_strings + 1) if n not in present]:
                raise ValueError(
                    f"declares {self.pv_strings} PV string(s) but produces no metrics for "
                    f"string(s) {', '.join(str(m) for m in missing)}"
                )

        # There is deliberately no check tying ``generator_input`` to the
        # presence of a generator metric. The flag answers "does this machine
        # have a generator input", which is a fact about the hardware; whether
        # this project can read that input is a separate question with a
        # separate answer. The 6000XP is exactly the case that separates them —
        # it has a GEN terminal, documented on EG4's own spec sheet, and none of
        # the registers behind it can be trusted (issue #122). Forbidding that
        # combination would force a model to deny hardware it demonstrably has
        # in order to admit a reading it cannot take.


@dataclass(frozen=True)
class DeviceIdentity:
    """Which physical unit a source is reading, and what speaks to it.

    ``serial`` is the identity every stored reading is filed under, so it has to
    be the same string the source reports as ``device``; two spellings of one
    inverter is two inverters as far as the store is concerned.

    ``model`` is the model the installation is configured as, ``None`` when no
    model was configured. It is not a detected value: no driver reads the
    device-type holding register today, so this reports what was declared rather
    than what was measured.
    """

    driver: str
    serial: str
    model: str | None = None


@runtime_checkable
class InverterSource(Protocol):
    """Something that yields samples from an inverter.

    The transport is behind an interface because it is not going to stay the
    same. Today the way in is the WiFi dongle's TCP port 8000; newer firmware
    removes that port, Ethernet dongles never had it, and wired RS485 is the
    path that outlives both. Anything that reached for the dongle directly would
    have to be rewritten when that happens, so nothing above this line does.

    The dongle's other constraint sets the failure contract. It accepts exactly
    one TCP client, so being evicted mid-read by the vendor's app or a second
    copy of this service is ordinary, not exceptional. An implementation reports
    that — and an unreachable inverter, and a timeout — as ConnectionError or
    another OSError, which the collector catches to record a gap and back off.
    Inventing a different exception type for it turns a routine eviction into a
    dead poll loop.
    """

    @property
    def device(self) -> str:
        """The serial of the inverter this source reads.

        Identity belongs here rather than on the sample or on the service,
        because this is the only object that knows which physical unit it is
        talking to. Everything above stamps what it stores with whatever this
        says, so two sources polling two inverters into one store keep their
        readings apart without either of them coordinating.

        How the configured value earns that trust depends on the transport,
        and the difference matters. The dongle authenticates with it: the
        inverter serial on every reply is compared against the configured one
        and a reply carrying a different one is refused, so a wrong serial
        fails the read rather than misfiling data.

        Modbus offers nothing equivalent. A request selects a unit by its
        address alone, and whichever inverter answers that address answers,
        whatever its serial. A driver on that transport has to read the serial
        off the wire and check it — ``read_serial_number()`` on the transport
        does exactly that, from input registers 115 to 119 — or the same wrong
        setting quietly files one machine's readings under another's name.
        """
        ...

    async def connect(self) -> None:
        """Establish the connection, claiming whatever single slot it needs.

        Every local transport this covers admits one client at a time — the
        dongle has a single TCP slot and a serial port is opened exclusively —
        so an implementation may be claiming either.
        """
        ...

    async def disconnect(self) -> None:
        """Release the connection and its single client slot.

        Called on shutdown and every time yield mode hands the dongle back, so
        the owner can run a firmware update from the vendor's app without
        stopping the service.
        """
        ...

    async def read(self) -> Sample:
        """Read one sample of inverter and battery state.

        One call has to produce one coherent moment. The sample carries its own
        timestamp — the service does not stamp it, and stores whatever the
        source put there as a single row — so an implementation that stitches
        together registers read minutes apart is recording a moment that never
        existed, under a time that belongs to only part of it.

        The timestamp must be timezone-aware. A protocol cannot enforce that,
        but ``Sample`` refuses a naive one at construction, so an
        implementation that builds one fails where it built it rather than
        having its reading read as local time on the way to epoch seconds.
        """
        ...


@runtime_checkable
class InverterDriver(InverterSource, Protocol):
    """A source that can also say what it is and what it can do.

    The collector needs only ``InverterSource`` and asks for no more, which is
    why that stayed a protocol of its own: the poll loop should not be able to
    branch on the model. Everything that has to describe the device rather than
    poll it — a capabilities endpoint, a schema built from what is actually
    producible, a page deciding whether it is showing a meter reading or an
    estimate — asks for this instead.
    """

    @property
    def identity(self) -> DeviceIdentity:
        """Which unit this is and which driver is reading it."""
        ...

    @property
    def capabilities(self) -> Capabilities:
        """What this device can do, and the metrics it therefore produces."""
        ...


@dataclass(frozen=True)
class ConversionSpec:
    """Manufacturer-stated conversion figures for one inverter model.

    All six come off one table in one document, so they are carried as a group
    with one citation rather than as six loose fields each repeating the same
    source. ``approximate`` names the fields the document itself hedges (the
    "~70 W" and "~18 W" idle figures), kept rather than laundered into
    precision the datasheet does not offer.

    Nothing consumes these arithmetically — they are provenance-carrying facts
    about the machine, rendered as a manufacturer's claim alongside the model
    name and version, never edited by the owner. See the spec for why they
    must not enter solar.expected_watts or the performance ratio.
    """

    cec_pct: float
    max_pv_to_grid_pct: float
    max_battery_to_grid_pct: float
    max_pv_to_battery_pct: float
    idle_normal_w: float
    idle_standby_w: float
    approximate: tuple[str, ...] = ()
    citation: str = ""

    def __post_init__(self) -> None:
        """Refuse a conversion spec that cites nothing.

        These are a manufacturer's claim about a product line rather than a
        measurement, and a number whose provenance nobody can check is a
        number presented as fact when it is not.
        """
        if not self.citation.strip():
            raise ValueError(
                "a ConversionSpec must name the document it was read from; "
                "these are manufacturer claims, not measurements"
            )


@dataclass(frozen=True)
class UnreadableMetric:
    """One metric a model cannot read, even though the family maps the register.

    ``ModelSpec`` can already adjust how many strings or battery slots a model
    has. It has no way to say *this register does not mean what the family says
    it means on this model* — which is how an off-grid machine silently
    inherited the hybrid family's whole metric set, including a "generator
    power" column that is actually a seconds counter. That is the gap this
    closes.

    The metric name is validated against the registry at construction, exactly
    as ``Capabilities`` already refuses an unknown name: a typo here would
    declare a gap nothing produces, and a gap the driver never acts on is a
    wrong reading back on the chart.

    ``cloud_available`` says whether the vendor's cloud carries a correct figure
    for this metric, so a page can tell "gone from the cloud too" from "waiting
    on the cloud path". Nothing in this phase reads it; phase 2 will.
    """

    metric: str
    reason: str
    citation: str
    cloud_available: bool = False

    def __post_init__(self) -> None:
        """Refuse a gap that names no registry metric, or gives no reason.

        All three of these fail at import, where a driver's declaration is
        built: a typo here is a gap the driver never acts on, and a gap with no
        reason or citation is a claim a future reader cannot check.
        """
        if self.metric not in column_names():
            raise ValueError(f"no such metric in the registry: {self.metric!r}")
        if not self.reason.strip():
            raise ValueError(
                f"unreadable metric {self.metric!r} needs a reason written for a person"
            )
        if not self.citation.strip():
            raise ValueError(
                f"unreadable metric {self.metric!r} needs a citation; "
                "naming a register unreadable asserts a fact about the hardware"
            )


@dataclass(frozen=True)
class ModelSpec:
    """One model within a driver family, and how it differs from the family.

    Every delta field left None inherits the family declaration. A delta that
    is set asserts a fact about a physical machine, so it must say where the
    fact came from: a citation naming a measurement or a vendor document.
    Without that rule the models table fills with plausible inventions, and a
    page rendering pv_strings=2 for a 6000XP nobody ever checked is exactly
    the kind of confident wrongness this project exists to refuse.
    """

    name: str
    pv_strings: int | None = None
    battery_module_slots: int | None = None
    # Whether this model has a generator input at all. None inherits the family
    # declaration. Setting False asserts the machine has none — an off-grid
    # model whose generator registers are not what the family says — and is a
    # hardware fact requiring a citation like any other.
    generator_input: bool | None = None
    # The metrics this model cannot read even though the family maps their
    # registers. Each entry carries its own reason and citation; see
    # UnreadableMetric. A model that declares none reads everything the family
    # does, which is every existing model today.
    unreadable: tuple[UnreadableMetric, ...] = ()
    # Manufacturer's conversion figures, where the model's spec sheet carries
    # them. None for models whose sheets have not been fetched and read — the
    # same rule their pv_strings already follow. These are facts with a
    # provenance, never editable settings, and nothing consumes them
    # arithmetically.
    conversion: ConversionSpec | None = None
    citation: str = ""
    # What is known to be wrong or unproven about this model, in a sentence a
    # page can show. A model reaches the wizard because the family's protocol
    # covers it, which is not the same as the family's *readings* having been
    # proven to mean the same thing on it — the EG4 off-grid machines share
    # every register address with the hybrids and disagree about what several
    # of them hold. Offering such a model silently is the failure this field
    # exists to prevent; offering it labelled is a decision the owner can make.
    caveat: str = ""
    # Which family of machine this is, in the owner's words rather than the
    # library's — "hybrid", "off-grid". Declared rather than inferred, because
    # the page uses it to tell two models apart whose names differ by a
    # keystroke and whose treatment does not: the 12kPV is a hybrid and the
    # 12000XP is off-grid. Deriving it from something merely correlated — that
    # a model happens to declare an unreadable metric, say — would relabel a
    # machine the moment that correlation broke, which is the same class of
    # mistake as reading a register that holds something else. Empty where a
    # family has only one kind and the distinction would be noise.
    family: str = ""

    def __post_init__(self) -> None:
        """Refuse a delta that cites nothing.

        Derived from the dataclass fields rather than listed by hand, so a new
        delta field added without touching this line is still caught — which
        is what would have happened to ``conversion`` if the old hand-written
        pair had been left in place. ``conversion`` is excluded from the delta
        check because it carries its own citation enforced by
        ``ConversionSpec.__post_init__``, and that citation is the spec sheet
        rather than the model's own measurement citation.
        """
        # ``unreadable`` is excluded like ``conversion``: each entry carries its
        # own citation, enforced by UnreadableMetric.__post_init__, so a model
        # that declares gaps need not repeat every source in its own citation.
        #
        # ``family`` is excluded because it is not a delta at all: it changes
        # no capability, ``resolve_model`` never reads it, and it exists so a
        # page can tell an owner which kind of machine they are choosing. It
        # also defaults to "" rather than None, so without this it would count
        # as a delta on every model and demand a citation from all of them.
        _non_delta = frozenset({"name", "citation", "caveat", "conversion", "unreadable", "family"})
        delta_fields = [
            f.name
            for f in __import__("dataclasses").fields(self.__class__)
            if f.name not in _non_delta and getattr(self, f.name) is not None
        ]
        if delta_fields and not self.citation.strip():
            raise ValueError(
                f"model {self.name!r} asserts a hardware fact without a citation; "
                "name the measurement or document it came from"
            )


def resolve_model(family: Capabilities, model: ModelSpec) -> Capabilities:
    """Return the family capabilities with one model's cited deltas applied.

    The same shape as the transport resolution on a built source: the registry
    entry carries the family default, and an installation that names a model
    answers with this. Fields the model does not cite stay exactly the family's.
    """
    resolved = family
    if model.pv_strings is not None:
        # The metrics move with the count, in one replace rather than two: a
        # model with fewer strings than its family would otherwise be built
        # holding metrics for strings it does not have, which Capabilities
        # refuses — and refuses on an intermediate value the caller never asked
        # for. Which per-string readings exist is a property of the family's
        # driver; how many strings there are is the model's fact.
        resolved = replace(
            resolved,
            pv_strings=model.pv_strings,
            metrics=_metrics_for_strings(family.metrics, model.pv_strings),
        )
    if model.battery_module_slots is not None:
        resolved = replace(resolved, battery_module_slots=model.battery_module_slots)
    if model.conversion is not None:
        resolved = replace(resolved, conversion=model.conversion)
    # generator_input before the unreadable subtraction, so the intermediate
    # value is never a declaration that claims a generator input it no longer
    # produces — Capabilities refuses exactly that.
    if model.generator_input is not None:
        resolved = replace(resolved, generator_input=model.generator_input)
    if model.unreadable:
        names = frozenset(g.metric for g in model.unreadable)
        resolved = replace(resolved, metrics=resolved.metrics - names)
    return resolved


def _metrics_for_strings(metrics: frozenset[str], count: int) -> frozenset[str]:
    """Re-cut the per-string metrics to cover exactly strings 1..count.

    The per-string readings a driver takes are the same whatever the count —
    power, voltage, current and the rest — so the set is rebuilt by applying
    those same readings to the strings this model actually has. Everything that
    is not per-string passes through untouched.
    """
    suffixes = {name[match.end() :] for name in metrics if (match := _STRING_METRIC.match(name))}
    rest = {name for name in metrics if not _STRING_METRIC.match(name)}
    return frozenset(rest | {f"pv{n}_{suffix}" for n in range(1, count + 1) for suffix in suffixes})


def find_model(entry: DriverEntry, name: str) -> ModelSpec:
    """Return the named model from an entry, or refuse naming what does exist."""
    for model in entry.models:
        if model.name == name:
            return model
    known = ", ".join(m.name for m in entry.models) or "none"
    raise ValueError(f"driver {entry.name!r} has no model {name!r}; models: {known}")


@dataclass(frozen=True)
class DriverEntry:
    """One registered driver: its name, what it covers, and how to build it.

    Capabilities sit on the entry as well as on the built source so that a
    caller can ask what a driver supports without dialling an inverter — the
    dongle admits one client, so constructing a source to read a leaflet would
    be a real cost. A driver that computed them twice could disagree with
    itself, so what a source reports is the entry's declaration and not a second
    opinion about the hardware.

    Two resolutions are necessarily exceptions, both per installation rather
    than per family: ``transport`` — the same inverter is reached by a dongle
    at one installation and a serial adapter at the next — and the model's
    cited capability deltas. A built source answers with what its own
    configuration chose; the entry can only carry the family defaults. Those
    are the fields on which the two may differ, and they differ about the
    installation rather than about the device.

    ``build`` takes the whole Config rather than a bag of connection settings.
    Which of them a driver needs is the driver's business: the dongle host and
    port mean nothing to an RS485 device, and a future one will want fields
    neither of the current two reads.
    """

    name: str
    description: str
    capabilities: Capabilities
    build: Callable[[Config], InverterDriver] = field(compare=False)
    # Who makes the family, as a person would name it in a dropdown. The fake
    # driver says "Simulated" — running the wizard with no hardware is a
    # supported case, so it is a manufacturer as far as the picker cares.
    manufacturer: str = ""
    # The models this family covers, each carrying only cited deltas.
    models: tuple[ModelSpec, ...] = ()

    def __post_init__(self) -> None:
        """Refuse an entry with no usable name, since the name is the lookup key."""
        if not self.name.strip():
            raise ValueError("a driver entry needs a name")
