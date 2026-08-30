"""validate.py — catch a decode error on the way in, before it becomes history.

A reading outside its metric's plausible bounds is evidence of a decode bug — a
misread register, a wrong scale, a shifted word — and that evidence is worth
more than a tidy chart. The product this replaces recorded a battery power of
25,583 W as fact, roughly double what the hardware can deliver, because nothing
between the register and the database ever asked whether the number was
possible. So this module reports and never edits: the offending value stays in
the sample exactly as received, and the caller stores the value and the flag
together.

The store repeats the same check as rows land, because a bounds check that can
be skipped is not a check. This module is the storage-independent form of that
logic — no database, no imports from ``arraysense.store`` — for the collector,
the API and anything else that needs to ask "is this believable?" without
writing a row.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

from arraysense.metrics import MetricSpec, lookup
from arraysense.models import BatteryModuleSample, Sample

# Everything on a module sample that is a reading. ``serial`` and ``slot`` are
# identity and position, not measurements. Deriving the list from the model
# rather than listing it here means a new module reading is checked the day it
# is added, instead of quietly going unvalidated.
_MODULE_IDENTITY_FIELDS: frozenset[str] = frozenset({"serial", "slot"})

_MODULE_READING_FIELDS: tuple[str, ...] = tuple(
    field.name for field in fields(BatteryModuleSample) if field.name not in _MODULE_IDENTITY_FIELDS
)


@dataclass(frozen=True)
class BoundsFailure:
    """One reading that fell outside its metric's plausible bounds.

    Carries the spec it was checked against rather than copies of the bounds,
    so the numbers in the reason can never drift from the registry that
    produced them.

    ``serial`` identifies the battery module a per-module reading came from,
    and is None for an inverter-level reading. Modules are identified by serial
    and never by slot: the inverter rotates modules through a fixed set of
    register slots, which a bank may outnumber. The slot survives only inside
    ``metric``, as a label saying where the value was read from — the bounds it
    was checked against come from the shared per-module template, so a pack
    beyond the slots the registry names is still checked.
    """

    spec: MetricSpec
    value: float
    serial: str | None = None

    @property
    def metric(self) -> str:
        """Name of the metric that failed, e.g. ``battery_module3_soc_pct``.

        A registry name for an inverter reading and for a pack in a slot the
        registry expands. For a pack beyond those the slot number is composed
        into the same shape, so the name says which pack the value came from even
        where no such column exists — the bounds behind it come from the shared
        per-module template either way.
        """
        return self.spec.name

    @property
    def reason(self) -> str:
        """Explain the failure in one line an operator can act on.

        Written for a log line or a flag on the dashboard. A message that says
        only "invalid" gives whoever reads it nothing to work with, so this
        names the value, the range it missed and the unit both were measured
        in — enough to tell a wrong scale from a genuinely odd reading.
        """
        where = f" on module {self.serial}" if self.serial is not None else ""
        return (
            f"{self.spec.name}{where} read {self.value} {self.spec.unit}, "
            f"outside the plausible range {self.spec.lower} to {self.spec.upper} "
            f"{self.spec.unit}"
        )


@dataclass(frozen=True)
class ValidationResult:
    """Every implausible reading found in one sample.

    An empty result means every reported reading was believable — not that the
    sample was full. Absent readings are not failures; a poll that reached
    nothing at all is trivially valid.
    """

    failures: tuple[BoundsFailure, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether every reported reading was within bounds.

        True of an empty sample as much as a clean one. A poll that reached
        nothing has nothing implausible in it, so callers must not read this as
        "the sample is complete".
        """
        return not self.failures

    @property
    def inverter_failures(self) -> tuple[BoundsFailure, ...]:
        """Failures on inverter-level readings, in the order they were checked.

        Kept apart from the module failures because the two mean different
        things: an inverter reading that misses its bounds points at the decode
        of a shared register, while a module reading points at one pack.
        """
        return tuple(failure for failure in self.failures if failure.serial is None)

    @property
    def module_failures(self) -> tuple[BoundsFailure, ...]:
        """Failures on per-module readings, in the order they were checked.

        Every one of these carries the serial of the pack it came from, so a
        repeat offender can be traced to one module across polls even though
        the inverter keeps rotating modules through its register slots.
        """
        return tuple(failure for failure in self.failures if failure.serial is not None)


def _check(
    spec: MetricSpec, value: float | None, serial: str | None = None
) -> BoundsFailure | None:
    """Check one real-world reading against one registry spec.

    This is the single place the absent-versus-implausible distinction is made,
    and getting it wrong here would poison everything downstream. A value of
    None is a gap — the inverter reported nothing — which is neither a failure
    nor a zero, so it produces no BoundsFailure. Only a number that arrived and
    missed its bounds does.

    Passing ``serial`` attributes the failure to that battery module; leaving it
    None marks the reading as inverter-level.
    """
    if value is None:
        return None
    if spec.within_bounds(value):
        return None
    return BoundsFailure(spec=spec, value=value, serial=serial)


def _validate_module(module: BatteryModuleSample) -> list[BoundsFailure]:
    """Check one battery module's readings against the per-module specs.

    Bounds come from the template rather than from the slot. The registry
    expands one template across slots and every slot's spec is identical to it
    apart from the name, so slot 1 answers for all of them — and a bank holding
    more packs than the registry names columns for still validates, where
    resolving by slot number raised ``KeyError`` on the fifth pack. This matches
    what the store does when it writes the same readings; the two must not
    resolve scale by different rules.

    The slot survives in the reported name, which is a label rather than a
    lookup, so a failure still says which slot the value was read from. The
    failure is attributed to the serial, since the inverter rotates modules
    through its slots and only the serial identifies a pack from one poll to the
    next. Readings are checked in model field order, so the same module always
    produces the same report.

    Raises:
        KeyError: the module carries a reading with no matching per-module metric
            in the registry, which means the model and the registry have
            diverged.
    """
    failures: list[BoundsFailure] = []
    for name in _MODULE_READING_FIELDS:
        value: float | None = getattr(module, name)
        template = lookup(f"battery_module1_{name}")
        spec = replace(template, name=f"battery_module{module.slot}_{name}")
        failure = _check(spec, value, module.serial)
        if failure is not None:
            failures.append(failure)
    return failures


def validate_sample(sample: Sample) -> ValidationResult:
    """Report every reading in ``sample`` that falls outside its metric's bounds.

    The sample is not modified: an implausible value is evidence of a decode
    bug, so it is flagged, never clamped and never dropped. Callers store the
    value and the flag together.

    Inverter readings are checked in the order the sample carries them,
    followed by each module's readings in model field order, so the report is
    deterministic for a given sample. A failed poll carries no readings and so
    comes back empty — a gap is not an implausible measurement.

    Raises:
        KeyError: a reading names a metric that is not in the registry. That is
            a caller bug — a typo, or a per-module metric passed as an inverter
            reading — and surfacing it beats skipping the value as "nothing to
            check", which is how an unchecked reading gets recorded as fact.
    """
    failures: list[BoundsFailure] = []
    for name, value in sample.readings.items():
        failure = _check(lookup(name), value)
        if failure is not None:
            failures.append(failure)
    for module in sample.battery_modules:
        failures.extend(_validate_module(module))
    return ValidationResult(failures=tuple(failures))
