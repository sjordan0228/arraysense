"""Tests for the grid-charge override record: what it stores and what it refuses.

The record exists so a charge can be undone after the process that started
it is gone, so these tests center on reading back what was written — through
the settings store, across a simulated restart, and what happens when a
stored record cannot be read. No hardware, no serial port, no driver: the
record is written straight into the same settings store the service uses.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arraysense.charge import decode_charge_config
from arraysense.charge_override import (
    ChargeOverride,
    clear_override,
    decode_override,
    encode_override,
    load_override,
    override_is_active,
    save_override,
)
from arraysense.settings import CHARGE_OVERRIDE_KEY, SettingsStore, lookup_setting
from arraysense.store.sqlite_store import SqliteStore
from conftest import TEST_DEVICE

# The registers one read answered: charge enable with bit 7 set (26581), a
# 3 kW command at 100 W per unit, the charge's stop setting, both window pairs
# of a two-period schedule with the third pair clear, and a start voltage.
# Deliberately more than the nine a restore writes — a record keeps the whole
# read, not a reconstruction — and deliberately all nine, because a record that
# cannot be written back whole is damage rather than an undo.
REGISTERS = {
    21: 26581,
    66: 30,
    67: 100,
    68: 1310,
    69: 1410,
    70: 0,
    71: 0,
    72: 0,
    73: 0,
    120: 1,
    158: 460,
}
READ_AT = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
UNTIL = datetime(2026, 9, 10, 18, 20, tzinfo=UTC)


def _store(tmp_path: Path) -> SqliteStore:
    return SqliteStore(str(tmp_path / "charge-override.db"), device=TEST_DEVICE)


def _override() -> ChargeOverride:
    saved = decode_charge_config(REGISTERS, quick_charge_remaining_s=None, read_at=READ_AT)
    return ChargeOverride(saved=saved, until=UNTIL, requested_w=3000)


def test_a_saved_override_round_trips_through_the_settings_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    override = _override()
    save_override(SettingsStore(store), override)
    assert load_override(SettingsStore(store)) == override
    store.close()


def test_the_record_survives_a_restart(tmp_path: Path) -> None:
    # The whole reason the record lives in the database rather than in
    # memory: a service that died mid-charge must still be able to put the
    # inverter back the way it was.
    path = str(tmp_path / "restart.db")
    store = SqliteStore(path, device=TEST_DEVICE)
    save_override(SettingsStore(store), _override())
    store.close()

    reopened = SqliteStore(path, device=TEST_DEVICE)
    loaded = load_override(SettingsStore(reopened))
    assert loaded is not None
    assert loaded.saved.registers == REGISTERS
    assert loaded.saved.read_at == READ_AT
    assert loaded.until == UNTIL
    assert loaded.requested_w == 3000
    reopened.close()


def test_an_empty_cell_means_no_override(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = SettingsStore(store)
    assert load_override(settings) is None
    save_override(settings, _override())
    clear_override(settings)
    assert load_override(settings) is None
    store.close()


def test_a_malformed_record_is_reported_rather_than_read_as_no_override(
    tmp_path: Path,
) -> None:
    # Read as absence, a caller would start a second charge and overwrite
    # the only record of how to undo the first.
    store = _store(tmp_path)
    settings = SettingsStore(store)
    settings.set(CHARGE_OVERRIDE_KEY, "{")
    with pytest.raises(ValueError):
        load_override(settings)
    store.close()


def test_a_timestamp_without_a_timezone_is_reported() -> None:
    # A window with no zone cannot be placed on a clock, so the record is
    # refused rather than read against whichever zone the reader happens to
    # assume. Whether a charge is still running would then be decided by luck.
    for field in ("read_at", "until"):
        payload: dict[str, object] = {
            "registers": {str(address): value for address, value in REGISTERS.items()},
            "read_at": READ_AT.isoformat(),
            "until": UNTIL.isoformat(),
            "requested_w": 3000,
        }
        payload[field] = str(payload[field]).replace("+00:00", "")
        with pytest.raises(ValueError):
            decode_override(json.dumps(payload))


def test_a_record_missing_its_registers_is_reported() -> None:
    text = json.dumps(
        {"read_at": READ_AT.isoformat(), "until": UNTIL.isoformat(), "requested_w": 3000}
    )
    with pytest.raises(ValueError):
        decode_override(text)


def test_a_record_missing_one_of_the_registers_a_restore_writes_is_reported() -> None:
    # The record is an undo, so one that cannot be written back whole is damage
    # rather than a usable record. Read as usable, it would put a stop on the
    # page that the driver must refuse — and the stop would answer 500 over a
    # control that cannot work.
    partial = {address: value for address, value in REGISTERS.items() if address != 72}
    payload = {
        "registers": {str(address): value for address, value in partial.items()},
        "read_at": READ_AT.isoformat(),
        "until": UNTIL.isoformat(),
        "requested_w": 3000,
    }
    with pytest.raises(ValueError) as damaged:
        decode_override(json.dumps(payload))
    assert "72" in str(damaged.value)


def test_the_payload_is_json_with_string_register_keys() -> None:
    override = _override()
    text = encode_override(override)
    assert decode_override(text) == override
    payload = json.loads(text)
    assert set(payload) == {"registers", "read_at", "until", "requested_w", "target_soc_pct"}
    assert all(isinstance(key, str) for key in payload["registers"])


def test_the_decoded_configuration_carries_the_raw_registers_a_restore_needs() -> None:
    decoded = decode_override(encode_override(_override()))
    assert decoded is not None
    assert decoded.saved.registers == REGISTERS
    assert decoded.saved.read_at == READ_AT


def test_the_record_carries_the_target_it_is_charging_to() -> None:
    """The sweep that ends a finished charge has to know what finished means, and
    it reads the answer out of the record rather than asking the device every
    minute."""
    decoded = decode_override(encode_override(_override()))
    assert decoded is not None
    assert decoded.target_soc_pct == 100


def test_a_record_from_before_the_target_existed_reads_as_the_shipped_target() -> None:
    """Every charge written before that field was stored was charging to the
    shipped target, so a record without it is not a charge with no target."""
    override = _override()
    payload = json.loads(encode_override(override))
    del payload["target_soc_pct"]
    decoded = decode_override(json.dumps(payload))
    assert decoded is not None
    assert decoded.target_soc_pct == 100
    # And a target the device would not accept is damage, like the rest of the
    # record: 0 is not a stop setting, and neither is 102.
    for refused in (0, 102, "100"):
        payload["target_soc_pct"] = refused
        with pytest.raises(ValueError):
            decode_override(json.dumps(payload))


def test_an_override_is_active_until_its_window_closes() -> None:
    override = _override()
    assert override_is_active(override, UNTIL - timedelta(minutes=1))
    assert not override_is_active(override, UNTIL)
    assert not override_is_active(override, UNTIL + timedelta(seconds=1))
    assert not override_is_active(None, UNTIL)


def test_saving_twice_replaces_the_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    settings = SettingsStore(store)
    second = ChargeOverride(
        saved=decode_charge_config(
            {**REGISTERS, 21: 0, 66: 12},
            quick_charge_remaining_s=None,
            read_at=READ_AT,
        ),
        until=UNTIL + timedelta(minutes=10),
        requested_w=1200,
    )
    save_override(settings, _override())
    save_override(settings, second)
    assert load_override(settings) == second
    store.close()


def test_the_key_is_registered_and_holds_structured_text() -> None:
    spec = lookup_setting(CHARGE_OVERRIDE_KEY)
    assert spec.kind == "str"
    text = encode_override(_override())
    assert len(text) <= spec.max_length
