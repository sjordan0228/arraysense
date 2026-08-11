"""test_drivers.py — the driver protocol, the capability declaration and the registry.

Two things are being pinned here. The first is that nothing above the driver
layer knows which inverter it is talking to: the collector reaches its source
through a name looked up in a registry, and the fake is looked up the same way
the real one is, so the tests exercise the path production uses rather than a
shortcut past it.

The second is that the capability declaration can describe a device this
repository does not support. It is written against the EG4 3000 EHV — one PV
string, no backup panel, and no kWh counters at all — because a declaration
designed only against the reference 18kPV would quietly assume every inverter
counts its own energy, and the whole energy model reads those counters.
"""

from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from arraysense import drivers
from arraysense.config import Config
from arraysense.drivers.base import (
    Capabilities,
    DeviceIdentity,
    DriverEntry,
    EnergyReporting,
    InverterDriver,
    InverterSource,
    ModelSpec,
    expand_module_metrics,
    find_model,
    resolve_model,
)
from arraysense.metrics import _MODULE_SLOTS, column_names
from arraysense.models import BatteryModuleSample


def _config(driver: str = "eg4_luxpower") -> Config:
    return Config(
        dongle_host="192.0.2.10",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path="/tmp/arraysense-test.db",
        driver=driver,
    )


# --- the registry -----------------------------------------------------------


def test_the_reference_family_is_registered() -> None:
    assert "eg4_luxpower" in drivers.names()


def test_an_unknown_driver_names_the_ones_that_exist() -> None:
    # The message is the whole value of failing here: somebody has mistyped a
    # name in a TOML file and needs the spelling, not a KeyError.
    with pytest.raises(ValueError, match="eg4_luxpower"):
        drivers.get("eg4_18kpv")


def test_registering_the_same_name_twice_is_refused() -> None:
    # Explicit registration is the point. Two drivers claiming one name means
    # whichever module imported last silently wins.
    entry = drivers.get("eg4_luxpower")
    with pytest.raises(ValueError, match="already registered"):
        drivers.register(entry)


def test_a_driver_is_looked_up_by_the_configured_name() -> None:
    source = drivers.create(_config())
    assert isinstance(source, InverterDriver)
    assert source.device == "CE12345678"


def test_the_fake_registers_the_same_way_a_real_driver_does() -> None:
    # Not a shortcut around the registry: the fake is an entry like any other,
    # so a test that builds one has proved the lookup works.
    from arraysense.drivers.fake import FakeSource

    source = drivers.create(_config("fake"))
    assert isinstance(source, FakeSource)
    assert isinstance(source, InverterDriver)
    assert source.device == "CE12345678"


def test_every_registered_driver_satisfies_the_protocol() -> None:
    for name in drivers.names():
        source = drivers.get(name).build(_config(name))
        assert isinstance(source, InverterSource), name
        assert isinstance(source, InverterDriver), name


def test_every_registered_driver_declares_metrics_the_registry_knows() -> None:
    # metrics.py stays the single source of truth for what a metric is. A
    # driver names a subset of it and never a spec of its own, so a name that
    # is not in the registry is a typo that would otherwise become a column
    # nothing ever writes.
    known = set(column_names())
    for name in drivers.names():
        declared = drivers.get(name).capabilities.metrics
        assert declared, name
        assert declared <= known, sorted(declared - known)


# --- the capability declaration ---------------------------------------------


def test_energy_can_be_declared_estimated_rather_than_counted() -> None:
    # The EG4 3000 EHV, which this repository does not support and is the point
    # of the exercise: one string, no backup panel, and no kWh counters at all,
    # so its energy could only ever be integrated from power. A page showing a
    # day's kWh has to be able to tell the two apart.
    ehv = Capabilities(
        pv_strings=1,
        energy=EnergyReporting.ESTIMATED,
        metrics=frozenset({"pv1_power_w", "pv_total_power_w", "battery_soc_pct"}),
    )
    assert ehv.energy is EnergyReporting.ESTIMATED
    assert ehv.backup_output is False
    assert ehv.pv_strings == 1


def test_the_reference_family_counts_its_energy() -> None:
    caps = drivers.get("eg4_luxpower").capabilities
    assert caps.energy is EnergyReporting.COUNTED
    assert "pv_energy_total_kwh" in caps.metrics


def test_a_declared_metric_the_registry_does_not_know_is_refused() -> None:
    with pytest.raises(ValueError, match="pv9_power_w"):
        Capabilities(
            pv_strings=3,
            energy=EnergyReporting.COUNTED,
            metrics=frozenset({"pv9_power_w"}),
        )


def test_a_string_metric_beyond_the_declared_count_is_refused() -> None:
    # This is the confusion issue #11 exists to prevent, caught one layer
    # earlier: a one-string inverter declaring pv3_power_w would carry a column
    # that can never be filled, and a NULL there would mean both "no third
    # string" and "the third string reported nothing".
    with pytest.raises(ValueError, match="pv3_power_w"):
        Capabilities(
            pv_strings=1,
            energy=EnergyReporting.ESTIMATED,
            metrics=frozenset({"pv1_power_w", "pv3_power_w"}),
        )


def test_a_declared_string_with_partial_metrics_is_refused() -> None:
    # The converse of the check above: declaring N strings but producing metrics
    # for only some of 1..N is an error. A page rendering from /api/capabilities
    # draws a card per declared string, so the unreported ones sit permanently
    # blank — which is what #90 was on the fake.
    with pytest.raises(ValueError, match="string\\(s\\) 2, 3"):
        Capabilities(
            pv_strings=3,
            energy=EnergyReporting.ESTIMATED,
            metrics=frozenset({"pv1_power_w"}),
        )


# --- per-module metric expansion --------------------------------------------


def test_a_module_template_expands_to_every_slot_the_registry_holds() -> None:
    # The whole reason a driver names ``soc_pct`` once: the slot count lives in
    # the registry and a registry that grows a fifth slot needs no driver edited.
    slots = {
        name
        for name in column_names()
        if name.startswith("battery_module") and name.endswith("_soc_pct")
    }
    assert slots, "the registry declares no per-module slots at all"
    assert expand_module_metrics(["soc_pct"]) == slots


def test_a_module_template_the_registry_does_not_know_is_refused() -> None:
    # A template matching nothing used to expand to nothing, and nothing is a
    # subset of the registry, so Capabilities had nothing left to object to. The
    # typo survived as far as issue #11 generating the schema from the declared
    # set, where it becomes a column never created and a reading silently
    # dropped — which is the failure this project exists to prevent.
    with pytest.raises(ValueError, match="not_a_real_template"):
        expand_module_metrics(["not_a_real_template"])


def test_the_refusal_names_the_wrong_template_and_not_the_right_one() -> None:
    # The message is the whole value: a driver author has mistyped one name in a
    # list of nineteen and needs to be told which.
    with pytest.raises(ValueError) as raised:
        expand_module_metrics(["soc_pct", "soc_percent"])
    assert "soc_percent" in str(raised.value)
    assert "soc_pct" not in str(raised.value)


def test_a_mistyped_template_is_refused_even_beside_valid_ones() -> None:
    # Not swallowed by the good names around it, which is how it stayed quiet.
    with pytest.raises(ValueError, match="cell_max_volts"):
        expand_module_metrics(["soc_pct", "cell_max_volts", "voltage_v"])


def test_declaring_no_module_templates_is_not_an_error() -> None:
    # A device that reports only a bank-level summary declares none, and that is
    # a real device rather than a mistake.
    assert expand_module_metrics([]) == frozenset()


def test_a_negative_string_count_is_refused() -> None:
    with pytest.raises(ValueError, match="pv_strings"):
        Capabilities(pv_strings=-1, energy=EnergyReporting.COUNTED, metrics=frozenset())


def test_capabilities_declare_the_battery_axis() -> None:
    # The battery used to be one boolean on the inverter. The axis needs two
    # more facts: whether this family relays BMS data at all, and how many
    # module slots the relay can carry.
    caps = Capabilities(
        pv_strings=0,
        energy=EnergyReporting.COUNTED,
        metrics=frozenset(),
        relays_battery=False,
        battery_module_slots=0,
    )
    assert caps.relays_battery is False
    assert caps.battery_module_slots == 0


def test_no_driver_declares_more_module_slots_than_the_registry_expands() -> None:
    # The registry expands battery_module1..N names at import time and cannot
    # ask the drivers (they import it). _MODULE_SLOTS is therefore a ceiling:
    # raising a driver's declaration past it means raising the ceiling and the
    # schema with it, deliberately, not silently.
    for entry in drivers.entries():
        assert entry.capabilities.battery_module_slots <= _MODULE_SLOTS, (
            f"{entry.name} declares more module slots than the registry expands"
        )


# --- identity ---------------------------------------------------------------


def test_identity_carries_the_driver_name_and_the_serial() -> None:
    source = drivers.create(_config())
    assert source.identity == DeviceIdentity(driver="eg4_luxpower", model=None, serial="CE12345678")
    # One serial, not two: the collector files rows under ``device`` and the
    # identity has to agree with it or the same inverter arrives twice.
    assert source.identity.serial == source.device


def test_the_model_is_absent_rather_than_guessed() -> None:
    # pylxpweb detects the family from a device-type holding register, and this
    # collector reads no holding registers at all. Absent is the honest answer
    # until it does; "18kPV" would be a guess that happens to be right here.
    assert drivers.create(_config()).identity.model is None


# --- connect: a bad connection is a gap, not a crash ------------------------


async def test_an_unresolvable_host_becomes_a_connectionerror_not_a_crash() -> None:
    # A dongle host the settings page accepted as a string but that resolves
    # through IDNA to an over-long DNS label ("label empty or too long") raises
    # UnicodeError from connect, not OSError. The collector's poll loop catches
    # ConnectionError and records a gap; an escaped UnicodeError would kill the
    # loop on every poll and every boot until someone SSHed in. connect must
    # translate it like any other unreachable endpoint.
    config = Config(
        dongle_host="a" * 64 + ".invalid",
        dongle_serial="BA12345678",
        inverter_serial="CE12345678",
        database_path="/tmp/arraysense-test.db",
        driver="eg4_luxpower",
    )
    source = drivers.create(config)
    with pytest.raises(ConnectionError):
        await source.connect()


# --- ModelSpec: cited capability deltas -------------------------------------


def test_a_model_delta_without_a_citation_is_refused() -> None:
    # The rule from the spec: no invented hardware facts. A delta asserts
    # something about a physical machine, so it names where the fact came
    # from or it does not exist.
    with pytest.raises(ValueError, match="citation"):
        ModelSpec(name="6000XP", pv_strings=2)


def test_a_model_with_no_deltas_needs_no_citation() -> None:
    spec = ModelSpec(name="12kPV")
    assert spec.citation == ""


def test_resolve_model_applies_deltas_and_inherits_the_rest() -> None:
    family = Capabilities(
        pv_strings=3,
        energy=EnergyReporting.COUNTED,
        metrics=frozenset({"pv1_power_w", "pv2_power_w", "pv3_power_w"}),
        relays_battery=True,
        battery_module_slots=4,
    )
    model = ModelSpec(name="18kPV", pv_strings=3, citation="measured on the reference installation")
    resolved = resolve_model(family, model)
    assert resolved.pv_strings == 3
    assert resolved.relays_battery is True  # inherited untouched


# --- Task 3: EG4 and fake families declare themselves -----------------------


def test_every_registered_driver_names_a_manufacturer_and_models() -> None:
    # The setup endpoint's manufacturer tree is built from these; an entry
    # without them renders an unanswerable question.
    for entry in drivers.entries():
        assert entry.manufacturer, f"{entry.name} names no manufacturer"
        assert entry.models, f"{entry.name} declares no models"


def test_the_18kpv_cites_its_measured_string_count() -> None:
    entry = drivers.get("eg4_luxpower")
    model = find_model(entry, "18kPV")
    assert model.pv_strings == 3
    assert "reference" in model.citation


def test_uncited_models_inherit_the_family_defaults() -> None:
    entry = drivers.get("eg4_luxpower")
    for name in ("6000XP", "12kPV"):
        model = find_model(entry, name)
        resolved = resolve_model(entry.capabilities, model)
        assert resolved.pv_strings == entry.capabilities.pv_strings


# --- what the reference driver declares -------------------------------------


def test_the_reference_driver_declares_what_its_mapping_produces() -> None:
    caps = drivers.get("eg4_luxpower").capabilities
    # Straight from the mapping tables.
    assert "pv3_power_w" in caps.metrics
    assert "grid_frequency_hz" in caps.metrics
    # Assembled outside them, and just as much a thing this driver produces.
    assert "load_power_w" in caps.metrics
    assert "battery_power_w" in caps.metrics
    assert "battery_soh_pct" in caps.metrics
    # Per-module telemetry, expanded across the four register slots.
    assert "battery_module1_soc_pct" in caps.metrics
    assert "battery_module4_cell_max_voltage_v" in caps.metrics
    assert caps.per_module_battery is True


def test_the_reference_driver_declares_no_per_module_fault_columns() -> None:
    # pylxpweb never fills them — none of its 21 battery registers carries
    # either quantity — so every poll would write a zero meaning "no fault"
    # about a pack nobody asked.
    caps = drivers.get("eg4_luxpower").capabilities
    assert "battery_module1_fault_code" not in caps.metrics
    assert "battery_module1_warning_code" not in caps.metrics


def test_the_declared_module_metrics_match_what_the_mapping_actually_fills() -> None:
    # The one place the declaration could drift from the code without anything
    # noticing. Populate every field of a module record, map it, and the fields
    # that came through are exactly the templates the driver claims.
    from arraysense.drivers.eg4_luxpower.source import _MODULE_TEMPLATES, _module_sample

    class _Record:
        serial_number = "BATT000001"
        battery_index = 0
        soc = 64.0
        soh = 99.0
        voltage = 53.78
        current = 22.8
        temperature = 21.7
        cycle_count = 450
        max_cell_voltage = 3.363
        min_cell_voltage = 3.359
        max_cell_temperature = 22.0
        min_cell_temperature = 21.4
        max_cell_num_voltage = 4
        min_cell_num_voltage = 1
        max_cell_num_temp = 2
        min_cell_num_temp = 6
        current_capacity = 180.0
        max_capacity = 280.0
        charge_current_limit = 100.0
        discharge_current_limit = 200.0
        status = 3

    sample = _module_sample(_Record())
    assert sample is not None
    filled = {
        field.name
        for field in fields(BatteryModuleSample)
        if field.name not in ("serial", "slot") and getattr(sample, field.name) is not None
    }
    assert filled == set(_MODULE_TEMPLATES)


def test_the_fake_declares_what_it_returns() -> None:
    caps = drivers.get("fake").capabilities
    assert "pv_total_power_w" in caps.metrics
    assert "battery_module1_soc_pct" in caps.metrics
    # It reports no kWh counters, so it must not claim to count energy.
    assert caps.energy is EnergyReporting.ESTIMATED


async def test_the_fake_returns_only_metrics_it_declared() -> None:
    caps = drivers.get("fake").capabilities
    sample = await drivers.create(_config("fake")).read()
    assert set(sample.readings) <= caps.metrics


async def test_the_fake_per_string_powers_sum_to_the_total() -> None:
    # The fake's per-string powers must add up to pv_total_power_w, not
    # contradict it. A fake that declares 3 strings and returns pv1/2/3_power_w
    # but has them sum to something other than pv_total_power_w would render
    # a dashboard with internally inconsistent numbers.
    from arraysense.drivers.fake.source import _READINGS

    total = _READINGS["pv_total_power_w"]
    pv1 = _READINGS["pv1_power_w"]
    pv2 = _READINGS["pv2_power_w"]
    pv3 = _READINGS["pv3_power_w"]
    assert pv1 + pv2 + pv3 == total


# --- the configuration reaches the registry ---------------------------------


def test_an_existing_installation_needs_no_edit() -> None:
    # The setting has to default to what every install is already doing, or
    # adding it breaks every config.toml in the field on the next upgrade.
    from arraysense.config import load

    path = Path("config.example.toml")
    assert load(path).driver == "eg4_luxpower"


def test_a_blank_driver_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="driver"):
        Config(
            dongle_host="h",
            dongle_serial="BA12345678",
            inverter_serial="CE12345678",
            database_path="/tmp/x.db",
            driver="  ",
        )


def test_a_mistyped_driver_stops_the_service_with_one_line(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Not a traceback out of the middle of application assembly: the person
    # reading this has mistyped a name in a file and needs the spelling.
    from arraysense.__main__ import main

    config = tmp_path / "config.toml"
    config.write_text(
        'dongle_host = "192.0.2.10"\n'
        'dongle_serial = "BA12345678"\n'
        'inverter_serial = "CE12345678"\n'
        f'database_path = "{tmp_path / "as.db"}"\n'
        'driver = "eg4_18kpv"\n'
    )
    assert main(["--config", str(config)]) == 1
    assert "eg4_luxpower" in caplog.text


# --- the seam holds ---------------------------------------------------------


def test_the_collector_no_longer_reaches_for_the_inverter_library() -> None:
    # The whole point of the move. The collector polls a source; only a driver
    # knows what library speaks to the hardware.
    collector = Path("src/arraysense/collector")
    offenders = [p.name for p in sorted(collector.glob("*.py")) if "pylxpweb" in p.read_text()]
    assert offenders == []


def test_an_entry_must_name_itself() -> None:
    with pytest.raises(ValueError, match="name"):
        DriverEntry(
            name="  ",
            description="nothing",
            capabilities=Capabilities(
                pv_strings=0, energy=EnergyReporting.ESTIMATED, metrics=frozenset()
            ),
            build=lambda _config: drivers.create(_config),
        )


def _setup_config(**overrides: object) -> Config:
    base: dict[str, object] = {
        "dongle_host": "192.0.2.1",
        "dongle_serial": "BA12345678",
        "inverter_serial": "CE12345678",
        "database_path": "/tmp/test.db",
    }
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def test_create_refuses_an_unknown_model_naming_the_known_ones() -> None:
    config = _setup_config(driver="eg4_luxpower", model="9000GT")
    with pytest.raises(ValueError, match="18kPV"):
        drivers.create(config)


def test_create_refuses_direct_battery_source_for_now() -> None:
    config = _setup_config(driver="eg4_luxpower", battery_source="direct")
    with pytest.raises(ValueError, match="not yet"):
        drivers.create(config)


def test_create_refuses_relayed_when_the_family_relays_nothing() -> None:
    # No current driver has relays_battery=False, so pin the rule with a
    # temporary entry. Reaching into the registry dict for cleanup is uglier
    # than an unregister function and better than shipping one nobody needs.
    from dataclasses import replace as dc_replace

    fake_entry = drivers.get("fake")
    entry = DriverEntry(
        name="norelay",
        description="test-only",
        capabilities=dc_replace(fake_entry.capabilities, relays_battery=False),
        build=fake_entry.build,
        manufacturer="Test",
        models=(ModelSpec(name="One"),),
    )
    drivers.register(entry)
    try:
        config = _setup_config(driver="norelay", battery_source="relayed")
        with pytest.raises(ValueError, match="does not relay"):
            drivers.create(config)
    finally:
        drivers._REGISTRY.pop("norelay", None)


def test_a_built_source_reports_the_models_cited_capabilities() -> None:
    config = _setup_config(driver="eg4_luxpower", model="18kPV", transport="dongle")
    source = drivers.create(config)
    assert source.capabilities.pv_strings == 3


def test_a_double_digit_string_is_still_a_string_metric() -> None:
    # Nothing supported has ten strings, so this pins the pattern rather than a
    # device: a single-digit match would read pv10_power_w as not-a-string,
    # which fails open — the count check would call string 10 missing while the
    # beyond-the-count check let it through.
    with pytest.raises(ValueError, match="pv10_power_w"):
        Capabilities(
            pv_strings=3,
            energy=EnergyReporting.ESTIMATED,
            metrics=frozenset({"pv1_power_w", "pv2_power_w", "pv3_power_w", "pv10_power_w"}),
        )
