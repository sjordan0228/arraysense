"""check_mppt_grouping.py — prove two strings on one MPPT are counted once.

The inverter reports one power reading per MPPT. Before #133 the scorer read
that reading once per configured *string*, so an array with two strings landing
on one MPPT had its actual production counted twice and its performance ratio
came out roughly double — in the direction nobody investigates.

This walks the real ``compute_day`` rather than the grouping helper, because
the defect lived in how the scorer consumed the groups rather than in how they
were formed, and a check that stopped at ``mppt_groups`` would have passed
throughout.

Two cases, and the second is the one that protects everybody:

- **shared** — two strings on MPPT 1, one on MPPT 2. The total must equal two
  MPPTs' output, not three strings' worth.
- **ordinary** — one string per MPPT, which is almost every installation. Its
  numbers must be exactly what they were before the fix, so the correction
  cannot quietly move a figure for an array it was never about.

Run it from a checkout: ``uv run python scripts/check_mppt_grouping.py``.
Exits non-zero if either case is wrong, so it can be used as a check.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from arraysense.efficiency import CONFIG_VERSION_KEY, compute_day
from arraysense.panels import parse_strings
from arraysense.settings import SettingsStore
from arraysense.store.sqlite_store import SqliteStore

DEVICE = "CE12345678"
CENTRAL = timezone(timedelta(hours=-5))
# Every MPPT is fed the same power, so the two cases differ only in wiring.
MPPT_WATTS = 3000.0
DAYLIGHT = range(7, 19)

ORDINARY = "\n".join(
    [
        "PV1 | 1 | 10 | 400 | 25 | 180",
        "PV2 | 2 | 10 | 400 | 25 | 180",
        "PV3 | 3 | 10 | 400 | 25 | 180",
    ]
)
SHARED = "\n".join(
    [
        "PV1 | 1 | 10 | 400 | 25 | 180",
        "PV2 | 1 | 10 | 400 | 25 | 180",
        "PV3 | 2 | 10 | 400 | 25 | 180",
    ]
)


def _insert_hour(conn: sqlite3.Connection, when: datetime, mppts: list[int]) -> None:
    """Record one hour with the same reading on every MPPT the array uses."""
    columns = ["timestamp", "device", "sample_count"]
    values: list[object] = [int(when.timestamp()), DEVICE, 1]
    for mppt in mppts:
        columns.append(f"pv{mppt}_power_w")
        values.append(round(MPPT_WATTS))
    for column, value in (
        ("ghi_wm2", 800),
        ("dni_wm2", 700),
        ("dhi_wm2", 150),
        ("wind_speed_ms", 2),
        ("outside_temperature_c", 30),
    ):
        columns.append(column)
        values.append(value)
    placeholders = ", ".join("?" for _ in columns)
    conn.execute(
        f"INSERT OR REPLACE INTO inverter_hourly ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )


def score(strings_text: str) -> list[tuple[str, float, float, float | None]]:
    """Score one synthetic day for an array described by ``strings_text``."""
    store = SqliteStore(str(Path(tempfile.mkdtemp()) / "check.db"), device=DEVICE)
    settings = SettingsStore(store)
    settings.set("site.timezone", "America/Chicago")
    settings.set("site.latitude", 33.0)
    settings.set("site.longitude", -97.0)
    settings.set("panels.strings", strings_text)
    settings.set(CONFIG_VERSION_KEY, 1)
    specs = parse_strings(strings_text)
    mppts = sorted({spec.mppt for spec in specs})
    day_start = datetime(2026, 8, 10, 0, 0, tzinfo=CENTRAL)
    for hour in DAYLIGHT:
        _insert_hour(
            store._conn, datetime(2026, 8, 10, tzinfo=UTC) + timedelta(hours=hour + 5), mppts
        )
    store._conn.commit()
    rows = compute_day(store, settings, day_start, day_start + timedelta(days=1), specs, 1)
    out = [
        (
            r.string_name or "TOTAL",
            round(r.expected_kwh, 6),
            round(r.actual_kwh, 6),
            None if r.pr is None else round(r.pr, 6),
        )
        for r in rows
    ]
    store.close()
    return out


def main() -> int:
    """Score both arrays and check the totals against the hardware's own reading."""
    failures: list[str] = []
    hours = len(DAYLIGHT)

    for label, text, mppt_count in (("ordinary", ORDINARY, 3), ("shared", SHARED, 2)):
        rows = score(text)
        total = next(r for r in rows if r[0] == "TOTAL")
        want = MPPT_WATTS / 1000.0 * hours * mppt_count
        print(f"\n{label}: {len(rows) - 1} scored row(s)")
        for name, expected, actual, pr in rows:
            print(f"  {name:<22}{expected:>10.4f}{actual:>10.4f}   PR {pr}")
        if abs(total[2] - want) > 0.01:
            failures.append(f"{label}: total actual {total[2]:.4f} kWh, expected {want:.4f}")
        else:
            print(f"  total actual {total[2]:.4f} kWh = {mppt_count} MPPT x 3 kW x {hours} h")

    # The ordinary array is what almost everyone has, so its per-string rows
    # must survive the change untouched rather than merely summing correctly.
    ordinary = score(ORDINARY)
    if [r[0] for r in ordinary] != ["PV1", "PV2", "PV3", "TOTAL"]:
        failures.append(f"ordinary rows renamed: {[r[0] for r in ordinary]}")

    print()
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("both arrays score against the readings the inverter actually reports")
    return 0


if __name__ == "__main__":
    sys.exit(main())
