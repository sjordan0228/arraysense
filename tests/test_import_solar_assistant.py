"""Tests for the SolarAssistant migration: tools/import_solar_assistant.py.

This runs once per installation, against data nobody can re-export if it goes
wrong, so the cases below are the ones that were actually got wrong while it was
being written. Each of them looked like a working import at the time.
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from arraysense.energy import energy_totals
from arraysense.metrics import lookup
from arraysense.models import Sample
from arraysense.store.schema import INVERTER_TIERS
from arraysense.store.sqlite_store import SqliteStore
from import_solar_assistant import (
    HOURLY_ENERGY,
    SOURCES,
    Buckets,
    _encode,
    _fahrenheit,
    parse,
    read_anchor,
    scan,
    synthesise_counters,
    write_tier,
)

TZ = ZoneInfo("America/Chicago")
HOUR = 3600


def _write(path: Path, lines: list[str]) -> Path:
    """Write a gzipped line-protocol file, as InfluxDB's exporter produces."""
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write("# INFLUXDB EXPORT\n# DDL\n")
        fh.write("CREATE DATABASE solar_assistant WITH NAME autogen\n")
        fh.write("# DML\n")
        fh.write("\n".join(lines) + "\n")
    return path


def test_an_integer_field_is_not_silently_dropped(tmp_path: Path) -> None:
    # Line protocol types an integer by suffixing it, so power arrives as
    # `combined=4917i`. Handing that to float() raises, and a parser that skips
    # what it cannot read loses every power series and the state of charge while
    # still importing the voltages — which look fine, so the import appears to
    # have worked.
    path = _write(
        tmp_path / "e.lp.gz",
        [
            "PV\\ power combined=4917i 1785986264000000000",
            "Battery\\ voltage inverter_0=51.9 1785986264000000000",
            "Battery\\ state\\ of\\ charge combined=7i 1785986264000000000",
        ],
    )
    got = list(parse(path))
    assert ("PV power", "combined", 4917.0, 1785986264) in got
    assert ("Battery state of charge", "combined", 7.0, 1785986264) in got
    assert ("Battery voltage", "inverter_0", 51.9, 1785986264) in got


def test_the_ddl_lines_carry_no_timestamp_and_are_skipped(tmp_path: Path) -> None:
    # They do not start with a comment marker either, so they have to be
    # recognised by the shape of the line rather than by its first character.
    path = _write(tmp_path / "e.lp.gz", ["PV\\ power combined=1i 1785986264000000000"])
    assert len(list(parse(path))) == 1


def test_a_measurement_name_keeps_its_spaces(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "e.lp.gz",
        ["Grid\\ power\\ in\\ hourly combined=12.5 1785986264000000000"],
    )
    ((measurement, _, _, _),) = parse(path)
    assert measurement == "Grid power in hourly"


def test_the_inverter_temperature_is_fahrenheit() -> None:
    # Verified against this project's own read of the same inverter at the same
    # moment: radiators at 68.0 and 71.0 °C while SolarAssistant recorded 157.7.
    assert _fahrenheit(157.7) == pytest.approx(69.8, abs=0.2)
    # Left raw it is an inverter at 158 °C, which the registry rejects.
    assert not lookup("radiator1_temperature_c").within_bounds(157.7)
    assert lookup("radiator1_temperature_c").within_bounds(_fahrenheit(157.7))


def test_solar_assistants_inverter_temperature_is_the_heatsink() -> None:
    # It lands in radiator1_temperature_c, not inverter_temperature_c. The two
    # sensors sit about thirteen degrees apart — 157.7 °F is 69.9 °C, against
    # radiators at 68.0 and 71.0 and an internal sensor at 59.0 at the same
    # instant — so importing one into the other's column would put a step at
    # the cutover seam and present it as the inverter warming up.
    target = {s.metric for s in SOURCES if s.measurement == "Inverter temperature"}
    assert target == {"radiator1_temperature_c"}
    assert "inverter_temperature_c" not in {s.metric for s in SOURCES}


def test_the_battery_temperature_is_not_imported() -> None:
    # SolarAssistant's is an ambient sensor reading 21.9 °C while the BMS
    # reported its hottest cell at 39.0. battery_temperature_c is fed by the BMS
    # from cutover onwards, so importing the other sensor into the same column
    # would put a seventeen degree step in the chart at the seam.
    assert not any(s.measurement == "Battery temperature" for s in SOURCES)


def _buckets(energy: dict[int, float], metric: str) -> Buckets:
    return Buckets(
        origin_hour=0,
        hours=0,
        origin_minute=0,
        minutes=0,
        hourly={},
        minutely={},
        energy={metric: {h: (kwh, h * HOUR) for h, kwh in energy.items()}},
    )


def test_counters_land_on_the_anchor_exactly() -> None:
    # The reconstruction has to join the inverter's real counters without a
    # step, because every figure downstream is a difference and a step reads as
    # one enormous hour.
    hours = {h: 1.0 for h in range(100, 110)}
    counters = synthesise_counters(
        _buckets(hours, "pv_energy_total_kwh"), {"pv_energy_total_kwh": 500.0}, 110
    )
    series = counters["pv_energy_total_kwh"]
    # The value at each hour is the counter as it stood at the *start* of it, so
    # the ten hours of one kWh each run from 490 up to the anchor at 500.
    assert series[100] == pytest.approx(490.0)
    assert series[109] == pytest.approx(499.0)


def test_a_counter_never_decreases() -> None:
    hours = {h: float(h % 7) for h in range(200, 400)}
    counters = synthesise_counters(
        _buckets(hours, "load_energy_total_kwh"), {"load_energy_total_kwh": 9000.0}, 400
    )
    series = counters["load_energy_total_kwh"]
    ordered = [series[h] for h in sorted(series)]
    assert all(b >= a for a, b in pairwise(ordered))


def test_the_counter_at_an_hour_is_its_start_not_its_end() -> None:
    # An hour late, every reading shifts into its neighbour and a day totals
    # hours one to twenty-three, dropping the first. It hid for a while because
    # the hour it drops is midnight, when solar is zero: solar matched to a
    # fiftieth of a percent while grid import came out fifteen percent short.
    hours = {10: 2.0, 11: 3.0, 12: 5.0}
    counters = synthesise_counters(
        _buckets(hours, "grid_import_energy_total_kwh"), {"grid_import_energy_total_kwh": 100.0}, 12
    )
    series = counters["grid_import_energy_total_kwh"]
    # Hour 12 consumed 5, so it began at 95 and the anchor of 100 is its end.
    assert series[12] == pytest.approx(95.0)
    assert series[11] == pytest.approx(92.0)
    assert series[10] == pytest.approx(90.0)
    # And the difference across the whole span is what was actually measured.
    assert series[12] - series[10] == pytest.approx(5.0)


def test_an_implausible_reading_stores_as_absent_rather_than_clamped() -> None:
    # A value the registry calls impossible is not a reading. Clamping it into
    # range would launder a decode fault into a plausible number.
    assert _encode("battery_soc_pct", 50.0) == 50
    assert _encode("battery_soc_pct", 240.0) is None
    assert _encode("battery_soc_pct", None) is None


def test_a_day_read_back_matches_what_the_source_recorded(tmp_path: Path) -> None:
    # The whole point of the migration, end to end: hourly means in, the same
    # day's energy out through the code the pages actually call.
    db = tmp_path / "s.db"
    store = SqliteStore(str(db))
    anchor_at = datetime(2026, 3, 2, 6, 0, tzinfo=UTC)
    store.append(
        Sample(
            timestamp=anchor_at,
            readings={metric: 1000.0 for metric in HOURLY_ENERGY.values()},
            battery_modules=(),
        )
    )

    # One kilowatt-hour every hour of 1 March, local time, and one hour into the
    # 2nd. The extra hour is not padding: a day's last interval runs from 23:00
    # to the next midnight, so without a reading on the far side of that
    # boundary the day totals twenty-three hours. Real collection is continuous
    # and always supplies it; a fixture that stops at midnight does not.
    day = datetime(2026, 3, 1, tzinfo=TZ)
    lines = []
    for hour in range(25):
        at = int(day.timestamp()) + hour * HOUR
        lines.append(f"PV\\ power\\ hourly combined=1000 {at}000000000")
        lines.append(f"PV\\ power combined=500i {at}000000000")
    export = _write(tmp_path / "e.lp.gz", lines)

    anchor, anchor_hour = read_anchor(db)
    last = anchor_hour * HOUR
    buckets = scan([export], last - 86400 * 30, last, last - 86400 * 30, last - 86400 * 30)
    counters = synthesise_counters(buckets, anchor, anchor_hour)
    conn = sqlite3.connect(db)
    tiers = {t.name: t.table for t in INVERTER_TIERS}
    write_tier(
        conn,
        tiers["hourly"],
        buckets.origin_hour,
        buckets.hours,
        HOUR,
        buckets.hourly,
        counters,
        HOUR,
    )
    conn.close()

    got = energy_totals(store, day, day.replace(day=2), period="day", zone=TZ)
    assert got, "the day should read back"
    assert got[0].totals["solar_kwh"] == pytest.approx(24.0, abs=0.2)


def test_the_import_refuses_to_run_without_a_live_reading_to_anchor_to(tmp_path: Path) -> None:
    # Without one there is nothing to join to, and a counter starting at zero
    # would put the entire lifetime of the system into the hour of cutover.
    db = tmp_path / "empty.db"
    SqliteStore(str(db))
    with pytest.raises(SystemExit, match="anchor"):
        read_anchor(db)
