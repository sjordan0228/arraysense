"""Scaffold smoke test.

Proves the package is importable and every module in the planned layout exists.
Replace nothing here — add real tests alongside it as features land.
"""

import importlib

import pytest

import arraysense

MODULES = [
    "arraysense.metrics",
    "arraysense.models",
    "arraysense.config",
    "arraysense.validate",
    "arraysense.collector.source",
    "arraysense.collector.service",
    "arraysense.drivers",
    "arraysense.drivers.base",
    "arraysense.drivers.eg4_luxpower",
    "arraysense.drivers.eg4_luxpower.source",
    "arraysense.drivers.fake",
    "arraysense.drivers.fake.source",
    "arraysense.store.base",
    "arraysense.store.schema",
    "arraysense.store.sqlite_store",
    "arraysense.store.rollup",
    "arraysense.store.tiers",
    "arraysense.api.app",
    "arraysense.api.routes",
]


def test_version_is_exposed() -> None:
    assert isinstance(arraysense.__version__, str)
    assert arraysense.__version__.count(".") >= 2


@pytest.mark.parametrize("name", MODULES)
def test_planned_module_imports(name: str) -> None:
    assert importlib.import_module(name) is not None
