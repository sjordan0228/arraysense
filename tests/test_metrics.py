"""Tests for the metric registry: arraysense.metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arraysense.api.app import create_app
from arraysense.collector.service import CollectorService
from arraysense.config import Config
from arraysense.drivers.base import Capabilities, EnergyReporting
from arraysense.drivers.fake.source import FakeSource
from arraysense.metrics import (
    ALL_METRICS,
    BATTERY_MODULE_METRICS,
    INVERTER_METRICS,
    MetricSpec,
    column_names,
    lookup,
)
from arraysense.models import Sample
from arraysense.store.sqlite_store import SqliteStore
from arraysense.validate import validate_sample
from conftest import TEST_DEVICE


def test_every_spec_is_well_formed() -> None:
    for spec in ALL_METRICS:
        assert spec.name.isidentifier()
        assert spec.scale > 0
        assert spec.lower < spec.upper


def test_column_names_are_unique_across_whole_registry() -> None:
    # A duplicate column would silently corrupt the wide-row schema.
    names = column_names()
    assert len(names) == len(set(names))


def test_inverter_and_module_registries_are_distinct() -> None:
    inverter_names = {spec.name for spec in INVERTER_METRICS}
    module_names = {spec.name for spec in BATTERY_MODULE_METRICS}
    assert inverter_names.isdisjoint(module_names)


def test_encode_decode_round_trip() -> None:
    spec = lookup("battery_voltage_v")  # scale 1000
    assert spec.decode(spec.encode(48.5)) == 48.5


def test_encode_rounds_rather_than_truncates() -> None:
    spec = lookup("battery_temperature_c")  # scale 10
    # 23.66 °C is 236.6 tenths: round to 237, never truncate to 236.
    assert spec.encode(23.66) == 237


def test_plausible_reading_passes_bounds() -> None:
    assert lookup("battery_power_w").within_bounds(5000.0)


def test_implausible_reading_fails_bounds() -> None:
    # The recorded 25,583 W battery-power glitch, about double what an 18kPV
    # can deliver, must be rejected.
    assert not lookup("battery_power_w").within_bounds(25583.0)


def test_signed_metrics_accept_negative_values() -> None:
    # Asserts the sign survives a round trip rather than a particular encoded
    # integer, so the test stays honest if a metric's scale changes.
    for name in ("battery_power_w", "grid_power_w", "battery_current_a"):
        spec = lookup(name)
        assert spec.lower < 0
        assert spec.within_bounds(-50.0)
        assert spec.encode(-50.0) < 0
        assert spec.decode(spec.encode(-50.0)) == pytest.approx(-50.0)


def test_lookup_unknown_metric_raises() -> None:
    with pytest.raises(KeyError):
        lookup("no_such_metric")


def test_grid_outage_readings_are_plausible() -> None:
    # During a power cut the inverter measures 0 V and 0 Hz. Those are real
    # readings of a real event, not decode errors, and must be storable.
    assert lookup("grid_voltage_v").within_bounds(0.0)
    assert lookup("grid_frequency_hz").within_bounds(0.0)


def test_load_power_accepts_small_negative_readings() -> None:
    # 65 negative load readings appear in 22 months of reference data.
    assert lookup("load_power_w").within_bounds(-300.0)


def test_battery_current_keeps_tenths() -> None:
    # The BMS reports tenths of an amp (pylxpweb: 'empirical: 0.1A scale,
    # doc says 0.01A'). Storing whole amps would discard that.
    spec = lookup("battery_current_a")
    assert spec.encode(-263.6) == -2636
    assert spec.decode(spec.encode(-263.6)) == pytest.approx(-263.6)


def test_battery_module_metrics_expand_across_slots() -> None:
    # Assert the expansion is exact across slots 1-4 rather than a fixed
    # count, so adding a module metric does not break this test.
    per_slot = len(BATTERY_MODULE_METRICS) // 4
    assert len(BATTERY_MODULE_METRICS) == per_slot * 4
    for slot in (1, 2, 3, 4):
        assert (
            sum(1 for s in BATTERY_MODULE_METRICS if s.name.startswith(f"battery_module{slot}_"))
            == per_slot
        )
    assert lookup("battery_module3_soc_pct").unit == "%"
    assert lookup("battery_module1_cycle_count").unit == "cycles"


def test_aggregation_is_not_mean_where_a_mean_would_be_meaningless() -> None:
    # Averaging a monotonic counter, an extreme, or a cell *number* produces a
    # value the hardware never reported.
    assert lookup("battery_module1_cycle_count").aggregation == "max"
    assert lookup("battery_module1_cell_max_voltage_v").aggregation == "max"
    assert lookup("battery_module1_cell_min_voltage_v").aggregation == "min"
    assert lookup("battery_module1_cell_max_voltage_num").aggregation == "last"


def test_ordinary_measurements_average() -> None:
    for name in ("pv_total_power_w", "battery_soc_pct", "grid_voltage_v"):
        assert lookup(name).aggregation == "mean"


def test_module_expansion_preserves_aggregation() -> None:
    # The template's policy must survive expansion across slots, or slot 2's
    # cycle counter would quietly start averaging.
    for slot in (1, 2, 3, 4):
        assert lookup(f"battery_module{slot}_cycle_count").aggregation == "max"
        assert lookup(f"battery_module{slot}_cell_min_voltage_v").aggregation == "min"


def test_adding_an_inverter_metric_to_the_registry_is_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The registry's core promise, held under driver declarations.

    Scoped deliberately to an *inverter* metric, because that is where the
    one-line rule holds whole. A per-module metric is one line in the registry
    plus its field on BatteryModuleSample in models.py — the sample is a fixed
    dataclass and the store reads module values off its fields, so a template
    with no field gets its column and then dies with AttributeError at the
    first write. That pairing predates driver declarations and is noted at
    the registry's module template list.

    The three monkeypatches below are what one line added to INVERTER_METRICS
    produces at import: the tuple itself, and the ALL_METRICS and _BY_NAME
    views the module derives from it. Nothing else is patched, so the paths
    exercised here — schema generation, the store's open and write, bounds
    validation, and the capabilities endpoint — must all be reading the
    registry at call time rather than holding import-time copies. It proves no
    more than that: a module this test never drives can still hold a copy
    (routes.py's live-view name list is one), and only the paths a metric
    actually flows through on its way to a column, a check and a declaration
    are what the one-line rule is about. A driver that declares the new name
    gets its column, its bounds check, and its line on the capabilities
    endpoint with no other file edited.
    """
    import arraysense.metrics as metrics_module

    spec = MetricSpec("coolant_temperature_c", "\N{DEGREE SIGN}C", 10, -40.0, 150.0)
    inverter = (*metrics_module.INVERTER_METRICS, spec)
    everything = (*inverter, *metrics_module.BATTERY_MODULE_METRICS)
    monkeypatch.setattr(metrics_module, "INVERTER_METRICS", inverter)
    monkeypatch.setattr(metrics_module, "ALL_METRICS", everything)
    monkeypatch.setattr(metrics_module, "_BY_NAME", {s.name: s for s in everything})

    # Registration accepts a declaration naming it: the registry is what the
    # driver's declared set is validated against.
    declared = Capabilities(
        pv_strings=0,
        energy=EnergyReporting.ESTIMATED,
        metrics=frozenset({"pv_total_power_w", "coolant_temperature_c"}),
    )

    # The schema follows: a store opened for that declaration has the column,
    # stores the reading at the registry's scale, and hands it back.
    when = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    store = SqliteStore(str(tmp_path / "flow.db"), device=TEST_DEVICE, metrics=declared.metrics)
    store.append(Sample(timestamp=when, readings={"coolant_temperature_c": 21.7}))
    row = store.latest(["coolant_temperature_c"])
    assert row is not None
    assert row["coolant_temperature_c"] == 21.7

    # Validation follows: the bounds live on the same registry line.
    verdict = validate_sample(Sample(timestamp=when, readings={"coolant_temperature_c": 400.0}))
    assert [failure.metric for failure in verdict.failures] == ["coolant_temperature_c"]

    # The API follows: a device declaring the metric reports it.
    class _Declaring(FakeSource):
        @property
        def capabilities(self) -> Capabilities:
            return declared

    config = Config(
        dongle_host="h",
        dongle_serial="s",
        inverter_serial="i",
        database_path=str(tmp_path / "flow.db"),
        poll_interval=10.0,
    )
    service = CollectorService(source=_Declaring(), store=store, interval=3600)
    app = create_app(store=store, service=service, config=config)
    with TestClient(app) as client:
        body = client.get("/api/capabilities").json()
    store.close()
    assert "coolant_temperature_c" in body["devices"][0]["metrics"]
