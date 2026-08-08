"""Scaffold smoke test.

Proves the package is importable and every module in the planned layout exists.
Replace nothing here — add real tests alongside it as features land.
"""

import importlib
from pathlib import Path

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


def test_the_unit_file_and_the_docs_agree_on_the_restart_policy() -> None:
    # A watchdog that kills the process by SIGTERM only causes a restart if
    # systemd treats that as a failure. The shipped unit says on-failure while
    # docs/installation.md and __main__.py both assert always, so an install made
    # from this repository would stop dead where the reference box restarts. The
    # two have to say the same thing, whichever way it is settled.
    unit = (Path(__file__).resolve().parents[1] / "packaging" / "arraysense.service").read_text()
    docs = (Path(__file__).resolve().parents[1] / "docs" / "installation.md").read_text()
    policy = [
        line.split("=", 1)[1].strip()
        for line in unit.splitlines()
        if line.strip().startswith("Restart=")
    ]
    assert policy, "the unit file states no Restart policy"
    for stated in policy:
        assert f"Restart={stated}" in docs, (
            f"the unit ships Restart={stated} and the installation docs do not say so"
        )


def test_the_measured_battery_colours_are_pinned_in_common_js() -> None:
    # Charge green and discharge red were chosen by measuring every pair under
    # simulated protanopia, deuteranopia and tritanopia against the panel
    # surface, not by eye. Nudging either value by eye is exactly the failure
    # this project cares about, so the declarations the palette ships are pinned
    # here rather than trusted to a comment.
    #
    # Searching the whole file is not enough: both values also appear in the
    # INK_FALLBACK map, so deleting the real CSS declaration while leaving the
    # fallback behind would still pass and the charts would silently change.
    # The :root block is what the browser actually reads, so that is what is
    # pinned, and the fallback is checked separately for agreeing with it.
    common = (
        Path(__file__).resolve().parents[1] / "src" / "arraysense" / "web" / "common.js"
    ).read_text()
    root = common.split(":root{", 1)[-1].split("}", 1)[0]
    assert "--batt:#2aa198" in root, "the shipped :root palette no longer declares --batt"
    assert "--batt-dis:#d1495b" in root, "the shipped :root palette no longer declares --batt-dis"
    fallback = common.split("INK_FALLBACK", 1)[-1].split("};", 1)[0]
    assert "'--batt':'#2aa198'" in fallback and "'--batt-dis':'#d1495b'" in fallback, (
        "INK_FALLBACK has drifted from the :root palette it stands in for"
    )
