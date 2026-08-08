# What pylxpweb exposes, and what we collect

Adding a metric is a one-line change in `metrics.py`. Knowing that the metric exists
at all has always been the expensive part, and until now that knowledge lived
nowhere — so "are we missing anything useful?" was answered by whoever last happened
to notice a field. Twice that produced a wrong answer. A search for an uptime
register concluded it might not exist, when it is input register 69 and we were
already storing it as `inverter_run_time_s`. A diff by name similarity reported 92
unmapped registers, which is inflated: `charge_power` and `discharge_power` fold into
one signed `battery_power_w`, and `radiator_temperature_1` is `radiator1_temperature_c`.

This document is the checked list that replaces both guesses. Every number in it came
from running code against the installed library and against the adapter, never from a
register name that looked like it meant something.

**It is a snapshot of pylxpweb 0.9.38 and it will go stale on the next upgrade.**
Registers get added, and the adapter's tables get edited. Before trusting a count,
re-run it:

```bash
uv run python -c "
from pylxpweb import registers as R
for n in ('INVERTER_INPUT_REGISTERS','INVERTER_HOLDING_REGISTERS','GRIDBOSS_REGISTERS','BATTERY_REGISTERS'):
    print(n, len(getattr(R, n)))
"
# 0.9.38: 143, 162, 92, 21
```

## How the mapping was established

Not by matching names. The join runs in two hops, both of them read out of source
rather than inferred:

1. **Register to library field.** `pylxpweb/transports/_field_mappings.py` holds
   `RUNTIME_FIELD` (109 entries) and `ENERGY_FIELD` (34), each mapping a register's
   `canonical_name` to the data-class field it lands on — or to an explicit `None`,
   which means only that no table-driven assignment happens, not that the value is
   lost. Twenty-seven of the 109 `RUNTIME_FIELD` entries are `None` and they are four
   different things. Seven are caught by name in a branch inside
   `from_modbus_registers`, `data.py:628-661`: the packed SOC/SOH word, the packed
   parallel config, the four fault and warning codes, and the BMS permission bitmap.
   Three — 72, 73, 74 — are read and discarded because the library derives PV current
   from power and voltage instead. Two — 80 and 107 — are read only by the
   `BatteryBankData` constructor and appear on no runtime field at all. The remaining
   fifteen are genuinely read and thrown away.
2. **Library field to our metric.** The tuple constants in
   `src/arraysense/drivers/eg4_luxpower/source.py` — `_RUNTIME_METRICS`,
   `_RUNTIME_BATTERY_METRICS`, `_RUNTIME_FLAGS`, `_RUNTIME_SIGNED_PAIRS`,
   `_BANK_METRICS`, `_BANK_FLAGS`, `_ENERGY_METRICS` — plus three paths hand-coded in
   `to_sample` that appear in no tuple: `_house_load()` reading `output_power`
   with `eps_power` as fallback, and the two signed battery-power pairs.

The adapter is the only authority on what a library field becomes on our side. Where
a claim below could have been guessed from a name, it was instead checked against
`dataclasses.fields()` of the class the attribute is actually read from — which is
how the dead mapping in our own adapter turned up.

## The short answer

| Collection | Entries | Collected | What it is |
|---|---:|---:|---|
| `INVERTER_INPUT_REGISTERS` | 143 | **85** | Telemetry. The surface this document is mostly about. |
| `INVERTER_HOLDING_REGISTERS` | 162 | **0** | Setpoints, not measurements. Nothing here belongs in the metric registry. |
| `GRIDBOSS_REGISTERS` | 92 | **0** | A separate physical MID/GridBOSS unit. Not the inverter. |
| `BATTERY_REGISTERS` | 21 | **17** | Per-module block, addressed by offset. Reached through `BatteryData`. |

Of the 143 input registers, 85 reach a metric and 58 do not. The 58 are not one
category of thing, and treating them as one backlog is the mistake this document
exists to prevent:

| Why it is not collected | Count | Can we fix it? |
|---|---:|---|
| **Available** — real data-class field, already on the wire | 11 | Yes. One line in `metrics.py`, one in an adapter tuple. |
| **Dropped by the library** — explicit `None`, lands on no data class | 15 | Not from `metrics.py`. Needs a raw register read or an upstream change. |
| **Not applicable** — this model does not have it | 14 | No. |
| **Deliberately excluded** — reads garbage on this hardware | 9 | No, and do not try. See the next section. |
| **Off the wire** — has a field, but our read path never requests the group | 6 | Not without a second read or an upstream change. |
| **Derived** — read and discarded; the field is synthesised from others | 3 | We already store the value; it is just not a measurement. |

Eleven of the 143 are a one-line change away, and they are listed under
[Worth collecting](#worth-collecting). Everything else in the 58 has a reason.

## Three stored values that are not measurements

Read this before using any of the columns below as evidence for anything. The library
synthesises three of the numbers we persist and hands them over indistinguishable from
readings, so a check that compares two of our columns can be comparing one number with
itself, or comparing a reading against a constant. All three are filed upstream as
issue #20.

**A state of health of 0 is rewritten to 100, on all three construction paths.**
`data.py:635-636` on the runtime path — `if soh == 0:` / `soh = 100  # Default to 100%
if not reported`; `data.py:1292` on the per-module path — `soh=soh if soh > 0 else
100`; and `data.py:1707` on the bank path — `actual_soh = 100  # 0 is invalid, assume
healthy`. Zero is what a silent BMS reports, so the rewrite fires in exactly the case
where nothing is known, and `battery_soh_pct` has no value left that means "no
reading". Our `_bms_is_answering` gate holds the bank and per-module copies back, but
it does not cover the third: `_collect(runtime, _RUNTIME_BATTERY_METRICS, ...)` runs
unconditionally in `to_sample`, so whatever register 5 decodes to reaches the
store either way. Whether that is a fabricated 100 during a CAN dropout depends on what
register 5 reads while the BMS is silent, which is **not established** — no capture in
this repository covers that state.

**Per-module `fault_code` and `warning_code` are a dataclass default.** `BatteryData`
declares both at `data.py:1031-1032` as `int = 0`, and
`BatteryData.from_modbus_registers` — `data.py:1181`, constructing at `data.py:1286` —
passes neither argument. It cannot: no entry in `BATTERY_REGISTERS` carries either
quantity. Our `battery_moduleN_fault_code` and `..._warning_code` columns are therefore
a constant zero arriving from upstream, which is the absent-data-rendered-as-zero
failure this project exists to avoid, committed one layer below us.

**`current_capacity` is state of charge restated in amp-hours.** `data.py:1672`
computes the bank's as `round(max_capacity * battery_soc / 100)` and `data.py:1281`
computes each module's as `max_capacity * soc / 100`. Our
`battery_remaining_capacity_ah`, at bank level and at module level alike, is an
arithmetic transform of the SOC sitting in the next column. It cannot disagree with it
and cannot corroborate it; calibration work that treats the pair as two independent
observations is reading one number twice.

## Deliberately excluded — do not re-add

These have real data-class fields, arrive on every poll, and would map in one line.
They are left out on purpose, and each has cost someone time already.

| Registers | Field | Why not |
|---|---|---|
| 13, 14 | `grid_voltage_s`, `grid_voltage_t` | No third phase to measure on split-phase hardware. Our adapter records 6.4 V and 1545.9 V from real reads. |
| 21, 22 | `eps_voltage_s`, `eps_voltage_t` | Same. |
| 67 | `battery_temperature` | Returned 11880 against the reference 18kPV — an undecoded register, not a temperature. We use `bms_max_cell_temperature` (103) instead. |
| 109–112 | `temperature_t2`…`t5` | The library's own descriptions end `(reserved).` |

The upstream source corroborates the first two rows, which is worth recording so the
next person does not have to re-derive it. `devices/inverters/_features.py:200`
carries the comment `# R/S/T registers (17-19, 20-22) contain garbage on US
split-phase installations`, and `InverterRuntimeData.is_corrupt` (`data.py:339`)
deliberately range-checks only L1 and L2, saying so at `data.py:400-402`: "R/S/T
registers contain garbage on US split-phase inverters (_features.py:174) and cannot be
validated without phase awareness." That quote's own back-reference is stale — the
comment it points at is at `_features.py:200` in 0.9.38, not 174 — which is a small
argument for grepping citations rather than trusting them, this document's included.
The library will not even validate these registers; putting one on a chart would give a
plausible-looking number with no measurement behind it.

The R-phase register is *not* in this group and we do collect it. Register 12
`grid_voltage_r` is described as "Grid R-phase voltage (L1-L2 on split-phase)" — the
line-to-line ~240 V figure an owner expects to see, which is why the adapter uses it
for `grid_voltage_v`.

Register 67 has since acquired a sentinel handler upstream that does **not** rescue
it here. `BATTERY_TEMPERATURE_SENTINEL_C = 127.0` normalises an int8 placeholder to
`None` for inverters with no local battery sensor, and its comment says only the
exact sentinel is treated that way "so is_corrupt() still catches genuine register
corruption". Our 11880 is not 127, so it passes through untouched. The same register
reaches `BatteryBankData.temperature`, and we do not map that either.

Two more exclusions are structural rather than per-register, and both encode the
project's rule that absent data is not zero:

- **Booleans go in the flag tables, never the measurement tables.** `_reading`
  rejects `bool` outright, because `True` quietly becoming `1.0` in a column of watts
  is a bug while `bms_allow_charge` becoming 1 is the intent.
- **A signed pair with a missing half is dropped, not zero-filled.** `grid_power_w`,
  `grid_power_l1_w`, `grid_power_l2_w` and `battery_power_w` can be absent. Treating
  a missing import figure as zero export would invent a reading.

And the BMS gate: `_bms_is_answering` drops the whole bank block, and each module
block, unless a cell voltage or a capacity is above zero. A BMS whose CAN link is
down leaves those registers zero-filled, so the bank arrives with `soc=0`, `soh=100`,
zero capacities and zero cell voltages — a complete set of numbers, all of them false.
The `soh=100` half is fabricated by the library on every path it could arrive by, as
[the section above](#three-stored-values-that-are-not-measurements) sets out. Removing
the gate would reintroduce exactly the failure this project was built to avoid.

## Inverter input registers — all 143

Sorted by the library's own `category`, then by address. The `Our metric / status`
column answers "do we already collect this": a metric name means yes, anything else
says why not.

- **`excluded`** is in the list above. Do not re-add it.
- **`available`** has a real field on `InverterRuntimeData` or `BatteryBankData`, is
  already on the wire, and is one line away.
- **`dropped`** carries an explicit `None` in `RUNTIME_FIELD` and lands on no data
  class. The library reads the register and discards the value, under a comment that
  reads: "Diagnostic / raw registers intentionally NOT surfaced on
  InverterRuntimeData. Explicit None keeps them acknowledged by the completeness
  contract test (a future register added without a key here fails CI) while
  preserving the existing 'read but drop' behaviour." No change on our side reaches
  these.
- **`off-wire`** has a data-class field but is never requested: it lives in the
  `extended_data` group, which `read_runtime()` asks for and `read_energy()` does not,
  and the fields concerned are all on `InverterEnergyData`. Mapping one would collect
  nothing, forever.
- **`n/a`** is a register this hardware does not have — LXP three-phase, or PV strings
  four to six on a three-MPPT unit.
- **`derived`** means the register is read and its value thrown away because the
  library computes the field from something else. See the note under the runtime table.

`÷` is the scale divisor. `±` in the Bits column marks a signed register. A `+` or `-`
after a metric name means the register is one half of a signed pair. Descriptions are
the library's `description` field, copied verbatim; none of the 143 is empty.

Two registers are restricted by model, both `frozenset({'LXP'})`: 190 and 191. The
other 141 carry all three families, so `registers_for_model('EG4_HYBRID')` and
`registers_for_model('EG4_OFFGRID')` return the same 141 registers. There is no
register the 6000XP/12000XP has and the 18kPV does not — which matters for #14 and is
covered below.

### `status` — 2 registers, 1 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 0 | `device_status` | — | 1 | 16 | `device_status` | Operating mode code (see working modes table). |
| 77 | `ac_input_type` | — | 1 | 16 | **available** | AC input type bitfield. Bit0: 0=Grid, 1=Generator. |

### `runtime` — 62 registers, 36 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 1 | `pv1_voltage` | V | 10 | 16 | `pv1_voltage_v` | PV string 1 voltage. |
| 2 | `pv2_voltage` | V | 10 | 16 | `pv2_voltage_v` | PV string 2 voltage. |
| 3 | `pv3_voltage` | V | 10 | 16 | `pv3_voltage_v` | PV string 3 voltage. |
| 4 | `battery_voltage` | V | 10 | 16 | `battery_voltage_v` | Battery pack voltage. |
| 7 | `pv1_power` | W | 1 | 16 | `pv1_power_w` | PV string 1 power. |
| 8 | `pv2_power` | W | 1 | 16 | `pv2_power_w` | PV string 2 power. |
| 9 | `pv3_power` | W | 1 | 16 | `pv3_power_w` | PV string 3 power. |
| 10 | `charge_power` | W | 1 | 16 | `battery_power_w +` | Battery charging power (power flowing into battery). |
| 11 | `discharge_power` | W | 1 | 16 | `battery_power_w -` | Battery discharging power (power flowing out of battery). |
| 12 | `grid_voltage_r` | V | 10 | 16 | `grid_voltage_v` | Grid R-phase voltage (L1-L2 on split-phase). |
| 13 | `grid_voltage_s` | V | 10 | 16 | **excluded** | Grid S-phase voltage (L2-L3 on split-phase). |
| 14 | `grid_voltage_t` | V | 10 | 16 | **excluded** | Grid T-phase voltage (L3-L1 on split-phase). |
| 15 | `grid_frequency` | Hz | 100 | 16 | `grid_frequency_hz` | Grid/mains frequency. |
| 16 | `inverter_power` | W | 1 | 16 | `inverter_power_w` | Inverter output power (Pinv). On-grid inverting power. |
| 17 | `rectifier_power` | W | 1 | 16 | `rectifier_power_w` | AC charging rectifier power (Prec). Grid-to-battery power. |
| 18 | `inverter_rms_current_r` | A | 100 | 16 | `inverter_current_a` | Inverter RMS current output, R/L1 phase. |
| 19 | `power_factor` | — | 1000 | 16 | `power_factor` | Power factor. x in (0,1000] => x/1000; x in (1000,2000) => (1000-x)/1000. |
| 20 | `eps_voltage_r` | V | 10 | 16 | `eps_voltage_v` | EPS R-phase output voltage. |
| 21 | `eps_voltage_s` | V | 10 | 16 | **excluded** | EPS S-phase output voltage. |
| 22 | `eps_voltage_t` | V | 10 | 16 | **excluded** | EPS T-phase output voltage. |
| 23 | `eps_frequency` | Hz | 100 | 16 | `eps_frequency_hz` | EPS/off-grid output frequency. |
| 24 | `eps_power` | W | 1 | 16 | `eps_power_w`, `load_power_w fallback` | EPS/off-grid inverter output power. |
| 25 | `eps_apparent_power` | VA | 1 | 16 | `eps_apparent_power_va` | EPS/off-grid apparent power. |
| 26 | `power_to_grid` | W | 1 | 16 | `grid_power_w -` | Power exported to grid (Ptogrid). |
| 27 | `power_to_user` | W | 1 | 16 | `grid_power_w +` | Power imported from grid (Ptouser). |
| 38 | `bus_voltage_1` | V | 10 | 16 | `bus_voltage_1_v` | DC bus 1 voltage. |
| 39 | `bus_voltage_2` | V | 10 | 16 | `bus_voltage_2_v` | DC bus 2 voltage. |
| 69 | `running_time` | s | 1 | 32 | `inverter_run_time_s` | Total running time in seconds. |
| 72 | `pv1_current` | A | 100 | 16 | derived | PV string 1 current. |
| 73 | `pv2_current` | A | 100 | 16 | derived | PV string 2 current. |
| 74 | `pv3_current` | A | 100 | 16 | derived | PV string 3 current. |
| 75 | `battery_current_inv` | A | 100 | 16 | dropped | Battery current (inverter-measured). |
| 127 | `eps_l1_voltage` | V | 10 | 16 | `eps_l1_voltage_v` | EPS L1-N voltage (~120V leg). 3-phase: S-phase gen voltage. |
| 128 | `eps_l2_voltage` | V | 10 | 16 | `eps_l2_voltage_v` | EPS L2-N voltage (~120V leg). 3-phase: T-phase gen voltage. |
| 129 | `eps_l1_power` | W | 1 | 16 | `eps_l1_power_w` | EPS L1N active power. 3-phase: S-phase off-grid active. |
| 130 | `eps_l2_power` | W | 1 | 16 | `eps_l2_power_w` | EPS L2N active power. 3-phase: T-phase off-grid active. |
| 131 | `eps_l1_apparent_power` | VA | 1 | 16 | `eps_l1_apparent_power_va` | EPS L1N apparent power. |
| 132 | `eps_l2_apparent_power` | VA | 1 | 16 | `eps_l2_apparent_power_va` | EPS L2N apparent power. |
| 153 | `ac_couple_power` | W | 1 | 16 | `ac_couple_power_w` | AC coupled power. On EG4_OFFGRID (12000XP/6000XP), this is the only source of AC couple power. On EG4_HYBRID, this tracks close to register 123 (genPower). Cloud API field: acCouplePower. |
| 170 | `output_power` | W | 1 | 16 ± | `load_power_w` | Total output power (Pload, on-grid load output). |
| 190 | `inverter_rms_current_s` | A | 100 | 16 | n/a (LXP) | Inverter RMS current, S/L2 phase (LXP three-phase only). |
| 191 | `inverter_rms_current_t` | A | 100 | 16 | n/a (LXP) | Inverter RMS current, T/L3 phase (LXP three-phase only). |
| 193 | `grid_l1_voltage` | V | 10 | 16 | **available** | Grid L1-N voltage (~120V, US split-phase). |
| 194 | `grid_l2_voltage` | V | 10 | 16 | **available** | Grid L2-N voltage (~120V, US split-phase). |
| 195 | `generator_l1_voltage` | V | 10 | 16 | **available** | Generator L1 voltage (US split-phase). |
| 196 | `generator_l2_voltage` | V | 10 | 16 | **available** | Generator L2 voltage (US split-phase). |
| 197 | `inverter_power_l1` | W | 1 | 16 | **available** | Inverter power L1 (US split-phase per-leg). |
| 198 | `inverter_power_l2` | W | 1 | 16 | **available** | Inverter power L2 (US split-phase per-leg). |
| 199 | `rectifier_power_l1` | W | 1 | 16 | **available** | Rectifier power L1 (US split-phase per-leg). |
| 200 | `rectifier_power_l2` | W | 1 | 16 | **available** | Rectifier power L2 (US split-phase per-leg). |
| 201 | `grid_export_power_l1` | W | 1 | 16 | `grid_power_l1_w -` | Grid export power L1 (US split-phase per-leg). |
| 202 | `grid_export_power_l2` | W | 1 | 16 | `grid_power_l2_w -` | Grid export power L2 (US split-phase per-leg). |
| 203 | `grid_import_power_l1` | W | 1 | 16 | `grid_power_l1_w +` | Grid import power L1 (US split-phase per-leg). |
| 204 | `grid_import_power_l2` | W | 1 | 16 | `grid_power_l2_w +` | Grid import power L2 (US split-phase per-leg). |
| 210 | `quick_charge_remaining_seconds` | s | 1 | 16 | dropped | Quick charge remaining time in seconds. |
| 217 | `pv4_voltage` | V | 10 | 16 | n/a (3 strings) | PV4 voltage (V23 extended). |
| 218 | `pv5_voltage` | V | 10 | 16 | n/a (3 strings) | PV5 voltage (V23 extended). |
| 219 | `pv6_voltage` | V | 10 | 16 | n/a (3 strings) | PV6 voltage (V23 extended). |
| 220 | `pv4_power` | W | 1 | 16 | n/a (3 strings) | PV4 power (V23 extended). |
| 221 | `pv5_power` | W | 1 | 16 | n/a (3 strings) | PV5 power (V23 extended). |
| 222 | `pv6_power` | W | 1 | 16 | n/a (3 strings) | PV6 power (V23 extended). |
| 232 | `smart_load_power` | W | 1 | 16 | dropped | Smart load output power. |

### `bms` — 27 registers, 15 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 5 | `soc_soh_packed` | — | 1 | 16 | `battery_soc_pct`, `battery_soh_pct` | Packed: low byte = SOC (%), high byte = SOH (%). |
| 80 | `bms_battery_type` | — | 1 | 16 | **available** (bank) | Battery type/brand and communication type (0=CAN, 1=RS485). |
| 81 | `bms_charge_current_limit` | A | 10 | 16 | `bms_charge_current_limit_a` | BMS max charging current (empirical: 0.1A scale, doc says 0.01A). |
| 82 | `bms_discharge_current_limit` | A | 10 | 16 | `bms_discharge_current_limit_a` | BMS max discharging current. |
| 83 | `bms_charge_voltage_ref` | V | 10 | 16 | `bms_charge_voltage_ref_v` | BMS recommended charging voltage. |
| 84 | `bms_discharge_cutoff` | V | 10 | 16 | `bms_discharge_cutoff_v` | BMS recommended discharge cutoff voltage. |
| 85 | `bms_status_0` | — | 1 | 16 | dropped | BMS status register 0. |
| 86 | `bms_status_1` | — | 1 | 16 | dropped | BMS status register 1. |
| 87 | `bms_status_2` | — | 1 | 16 | dropped | BMS status register 2. |
| 88 | `bms_status_3` | — | 1 | 16 | dropped | BMS status register 3. |
| 89 | `bms_status_4` | — | 1 | 16 | dropped | BMS status register 4. |
| 90 | `bms_status_5` | — | 1 | 16 | dropped | BMS status register 5. |
| 91 | `bms_status_6` | — | 1 | 16 | dropped | BMS status register 6. |
| 92 | `bms_status_7` | — | 1 | 16 | dropped | BMS status register 7. |
| 93 | `bms_status_8` | — | 1 | 16 | dropped | BMS status register 8. |
| 94 | `bms_status_9` | — | 1 | 16 | dropped | BMS status register 9. |
| 95 | `battery_status_inv` | — | 1 | 16 | `bms_allow_charge`, `bms_allow_discharge`, `bms_force_charge` | Inverter-aggregated lithium battery status / BMS permission bitmap. Legacy enum 0=Idle, 2=StandBy, 3=Active; also a bitmap (issue #232): 0x01=allow charge, 0x02=allow discharge, 0x20=force-charge request. Decoded into bms_allow_charge/bms_allow_discharge/bms_force_charge (see decode_bms_permissions); cloud equivalents bmsCharge/bmsDischarge/bmsForceCharge. |
| 96 | `battery_parallel_count` | — | 1 | 16 | `battery_module_count` | Number of batteries in parallel. |
| 97 | `battery_capacity_ah` | Ah | 1 | 16 | `battery_full_capacity_ah` | Battery capacity. |
| 98 | `battery_current_bms` | A | 10 | 16 ± | `battery_current_a` | Battery current from BMS (signed, 0.1A resolution). |
| 101 | `bms_max_cell_voltage` | V | 1000 | 16 | `battery_max_cell_voltage_v` | Maximum cell voltage (millivolts). |
| 102 | `bms_min_cell_voltage` | V | 1000 | 16 | `battery_min_cell_voltage_v` | Minimum cell voltage (millivolts). |
| 103 | `bms_max_cell_temperature` | °C | 10 | 16 ± | `battery_temperature_c` | Maximum cell temperature (signed, 0.1°C). |
| 104 | `bms_min_cell_temperature` | °C | 10 | 16 ± | `battery_min_cell_temperature_c` | Minimum cell temperature (signed, 0.1°C). |
| 105 | `bms_fw_update_state` | — | 1 | 16 | dropped | Bits 0-2: BMS FW update (1=upgrading, 2=ok, 3=fail). Bit 4: Gen dry contact. |
| 106 | `bms_cycle_count` | — | 1 | 16 | `battery_cycle_count` | Charge/discharge cycle count. |
| 107 | `battery_voltage_inv_sample` | V | 10 | 16 | `battery_voltage_inv_sample_v` †bank only | Inverter-sampled battery voltage. |

### `temperature` — 10 registers, 4 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 64 | `internal_temperature` | °C | 1 | 16 ± | `inverter_temperature_c` | Internal/ring temperature. |
| 65 | `radiator_temperature_1` | °C | 1 | 16 | `radiator1_temperature_c` | Radiator temperature 1. |
| 66 | `radiator_temperature_2` | °C | 1 | 16 | `radiator2_temperature_c` | Radiator temperature 2. |
| 67 | `battery_temperature` | °C | 1 | 16 | **excluded** | Battery temperature. |
| 68 | `battery_control_temperature` | °C | 1 | 16 | dropped | Battery control temperature. |
| 108 | `temperature_t1` | °C | 10 | 16 | `board_temperature_c` | Temperature sensor T1 (BT/board temp on 12K models). |
| 109 | `temperature_t2` | °C | 10 | 16 | **excluded** | Temperature sensor T2 (reserved). |
| 110 | `temperature_t3` | °C | 10 | 16 | **excluded** | Temperature sensor T3 (reserved). |
| 111 | `temperature_t4` | °C | 10 | 16 | **excluded** | Temperature sensor T4 (reserved). |
| 112 | `temperature_t5` | °C | 10 | 16 | **excluded** | Temperature sensor T5 (reserved). |

### `energy_daily` — 17 registers, 11 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 28 | `pv1_energy_today` | kWh | 10 | 16 | `pv1_energy_today_kwh` | PV1 generation today (Epv1_day). |
| 29 | `pv2_energy_today` | kWh | 10 | 16 | `pv2_energy_today_kwh` | PV2 generation today (Epv2_day). |
| 30 | `pv3_energy_today` | kWh | 10 | 16 | `pv3_energy_today_kwh` | PV3 generation today (Epv3_day). |
| 31 | `inverter_energy_today` | kWh | 10 | 16 | `inverter_energy_today_kwh` | On-grid inverter output energy today (Einv_day). |
| 32 | `ac_charge_energy_today` | kWh | 10 | 16 | `ac_charge_energy_today_kwh` | AC charging rectifier energy today (Erec_day). |
| 33 | `charge_energy_today` | kWh | 10 | 16 | `battery_charge_energy_today_kwh` | Battery charge energy today (Echg_day). |
| 34 | `discharge_energy_today` | kWh | 10 | 16 | `battery_discharge_energy_today_kwh` | Battery discharge energy today (Edischg_day). |
| 35 | `eps_energy_today` | kWh | 10 | 16 | `eps_energy_today_kwh` | Off-grid output energy today (Eeps_day). |
| 36 | `grid_export_energy_today` | kWh | 10 | 16 | `grid_export_energy_today_kwh` | Export to grid energy today (Etogrid_day). |
| 37 | `grid_import_energy_today` | kWh | 10 | 16 | `grid_import_energy_today_kwh` | Import from grid energy today (Etouser_day). |
| 124 | `generator_energy_today` | kWh | 10 | 16 | off-wire | Generator energy today (Egen_day). EG4_OFFGRID caveat: the withdrawn eg4_web_monitor PR #220 claimed regs 124-126 hold AC-couple energy in raw Wh (DIV_1000) on the 12000XP, but the reporter's own earlier sweep (#196) read I124 == holding 179 exactly (0x0800, the AC-couple enable bit) and successive captures moved by exact powers of two — bit-field behavior, not energy accrual. Semantics on the SNA platform are UNVERIFIED; do not surface as a sensor for that family without a fresh capture. |
| 133 | `eps_l1_energy_today` | kWh | 10 | 16 | off-wire | EPS L1 energy today (daily counter). |
| 134 | `eps_l2_energy_today` | kWh | 10 | 16 | off-wire | EPS L2 energy today (daily counter). |
| 171 | `load_energy_today` | kWh | 10 | 16 | `load_energy_today_kwh` | Load energy today (Eload_day). |
| 223 | `epv4_day` | kWh | 10 | 16 | n/a (3 strings) | PV4 daily energy yield. |
| 226 | `epv5_day` | kWh | 10 | 16 | n/a (3 strings) | PV5 daily energy yield. |
| 229 | `epv6_day` | kWh | 10 | 16 | n/a (3 strings) | PV6 daily energy yield. |

### `energy_lifetime` — 17 registers, 11 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 40 | `pv1_energy_total` | kWh | 10 | 32 | `pv1_energy_total_kwh` | PV1 cumulative generation (Epv1_all). |
| 42 | `pv2_energy_total` | kWh | 10 | 32 | `pv2_energy_total_kwh` | PV2 cumulative generation (Epv2_all). |
| 44 | `pv3_energy_total` | kWh | 10 | 32 | `pv3_energy_total_kwh` | PV3 cumulative generation (Epv3_all). |
| 46 | `inverter_energy_total` | kWh | 10 | 32 | `inverter_energy_total_kwh` | Cumulative inverter output energy (Einv_all). |
| 48 | `ac_charge_energy_total` | kWh | 10 | 32 | `ac_charge_energy_total_kwh` | Cumulative AC charging rectified energy (Erec_all). |
| 50 | `charge_energy_total` | kWh | 10 | 32 | `battery_charge_energy_total_kwh` | Cumulative battery charge energy (Echg_all). |
| 52 | `discharge_energy_total` | kWh | 10 | 32 | `battery_discharge_energy_total_kwh` | Cumulative battery discharge energy (Edischg_all). |
| 54 | `eps_energy_total` | kWh | 10 | 32 | `eps_energy_total_kwh` | Cumulative off-grid output energy (Eeps_all). |
| 56 | `grid_export_energy_total` | kWh | 10 | 32 | `grid_export_energy_total_kwh` | Cumulative export to grid energy (Etogrid_all). |
| 58 | `grid_import_energy_total` | kWh | 10 | 32 | `grid_import_energy_total_kwh` | Cumulative import from grid energy (Etouser_all). |
| 125 | `generator_energy_total` | kWh | 10 | 32 | off-wire | Generator cumulative energy (Egen_all, 32-bit). EG4_OFFGRID caveat: unverified on the SNA platform — see the register-124 note (PR #220 adjudication). |
| 135 | `eps_l1_energy_total` | kWh | 10 | 32 | off-wire | EPS L1 cumulative energy (32-bit). |
| 137 | `eps_l2_energy_total` | kWh | 10 | 32 | off-wire | EPS L2 cumulative energy (32-bit). |
| 172 | `load_energy_total` | kWh | 10 | 32 | `load_energy_total_kwh` | Load cumulative energy (Eload_all, 32-bit). |
| 224 | `epv4_all` | kWh | 10 | 32 | n/a (3 strings) | PV4 cumulative energy yield (32-bit, low word). |
| 227 | `epv5_all` | kWh | 10 | 32 | n/a (3 strings) | PV5 cumulative energy yield (32-bit, low word). |
| 230 | `epv6_all` | kWh | 10 | 32 | n/a (3 strings) | PV6 cumulative energy yield (32-bit, low word). |

### `fault` — 4 registers, 4 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 60 | `fault_code` | — | 1 | 32 | `inverter_fault_code` | Inverter fault code (32-bit bitfield). |
| 62 | `warning_code` | — | 1 | 32 | `inverter_warning_code` | Inverter warning code (32-bit bitfield). |
| 99 | `bms_fault_code` | — | 1 | 16 | `inverter_fault_code` | BMS fault code. |
| 100 | `bms_warning_code` | — | 1 | 16 | `inverter_warning_code` | BMS warning code. |

### `generator` — 3 registers, 3 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 121 | `generator_voltage` | V | 10 | 16 | `generator_voltage_v` | Generator voltage. |
| 122 | `generator_frequency` | Hz | 100 | 16 | `generator_frequency_hz` | Generator frequency. |
| 123 | `generator_power` | W | 1 | 16 | `generator_power_w` | Generator power. |

### `parallel` — 1 register, 0 collected

| Addr | canonical_name | Unit | ÷ | Bits | Our metric / status | Description (library, verbatim) |
|---:|---|---|---:|---|---|---|
| 113 | `parallel_config` | — | 1 | 16 | **available** | Packed: bits 0-1 master/slave, bits 2-3 phase, bits 8-15 parallel num. |

### Notes on the tables above

**Registers 72–74 do not carry PV current.** They are read and discarded, and the
library synthesises the field from power and voltage. `data.py:687-689`, verbatim: "The
firmware exposes no PV-current register (regs 72-74 read 0 while producing), so
compute it from the power/voltage already parsed above." Our `pv1_current_a` through
`pv3_current_a` are therefore `P/V`, not measurements. This bears on the observation
that string 1 is wired differently from the other two — 372.9 V at 6.04 A against about
310 V at 8.6 A. The current half of that is derived from the same power and voltage
registers, so it carries no independent information about the string, and any heuristic
built on the current alone is really reading the power.

**Register 107 reaches us only through the bank object.** Marked † in the table.
`RUNTIME_FIELD["battery_voltage_inv_sample"]` is `None`, so the runtime path drops it;
`BatteryBankData` reads register 107 explicitly and `_BANK_METRICS` collects it. The
identically-spelled entry in `_RUNTIME_BATTERY_METRICS` names a field that does not
exist on `InverterRuntimeData`, so `getattr(source, attribute, None)` returns `None`
on every poll and that line contributes nothing. Harmless, but it means
`battery_voltage_inv_sample_v` is collected only while the BMS is answering — the
"they survive a CAN dropout" property that table's docstring claims does not hold for
this one entry.

**`inverter_fault_code` is not purely the inverter's.** Registers 99 and 100 are the
BMS's own fault and warning codes, and `_merge_status_code` folds them into registers
60 and 62 before the adapter sees anything. The separate `battery_fault_code` and
`battery_warning_code` columns come from the bank path and do keep the BMS view apart,
which is what the adapter's comment about "a pack complaining while the inverter is
content" describes — but the inverter column is a composite.

**Register 69 is `inverter_on_time`, not `running_time`.** The canonical name is
`running_time`; the field it lands on is `inverter_on_time`; our metric is
`inverter_run_time_s`. Three different spellings of one quantity, and the gap between
the first two is exactly what made an earlier search conclude the register did not
exist.

**And the library disagrees with itself about register 69's unit.** The register
definition carries `unit='s'` and the description "Total running time in seconds."; the
field it lands on is declared `inverter_on_time: int | None = None  # hours (total on
time)` at `data.py:298`. Nothing reconciles them — `scale` is `ScaleFactor.NONE` and
`inverter_on_time` is in `_RUNTIME_INT_FIELDS`, so the raw register word is assigned
unchanged. Our metric is `inverter_run_time_s`, which follows the register rather than
the field.

**The register is right and the field comment is wrong.** Measured on the reference
installation rather than reasoned about: two readings taken 45 seconds of wall clock
apart were 62,024,140 and 62,024,190, an advance of 50. A counter in hours cannot move
fifty units in a minute. At 62,024,190 seconds the inverter has been running 717.9 days,
which puts commissioning around 20 August 2024 and independently corroborates the "over
the last 22 months" figure arrived at from a completely different source. So
`inverter_run_time_s` is correctly named and is usable as an absolute duration, not only
as a reset detector. Anyone reading `# hours (total on time)` at `data.py:298` should
disregard it.

**The 32-bit registers.** Twenty of the 143, all unsigned: the ten lifetime counters
at 40–58, plus 60, 62, 69, 125, 135, 137, 172, 224, 227, 230. All 17
`energy_lifetime` entries are 32-bit; outside that category only 60, 62 and 69 are.
The second word of a 32-bit register is never defined as its own register.

**The two packed registers.** Register 5 `soc_soh_packed` (`low=SOC,high=SOH`) unpacks
to `battery_soc` and `battery_soh`; register 113 `parallel_config`
(`b0-1=role,b2-3=phase,b8-15=parallel_num`) unpacks to `parallel_master_slave`,
`parallel_phase` and `parallel_number`. Register 95 `battery_status_inv` is also a
bitmap and is decoded into three booleans, but its `packed` field is `None`.

## What the library adds that no register carries

Four of the values we store are computed by the library rather than read. Each one
looks like a measurement in the store and is not, which matters whenever two of them
are compared.

| Our metric | How it is actually produced |
|---|---|
| `pv1_current_a`, `pv2_current_a`, `pv3_current_a` | `P/V` per string. Registers 72–74 read zero while producing. |
| `pv_total_power_w` | `_sum_optional` over `pv1_power`…`pv6_power`, so it is count-agnostic. |
| `pv_energy_today_kwh`, `pv_energy_total_kwh` | The same sum over the per-string energy counters. |
| `battery_remaining_capacity_ah` | `round(max_capacity * battery_soc / 100)` at `data.py:1672`, and `max_capacity * soc / 100` per module at `data.py:1281`. SOC restated in amp-hours; see [Three stored values that are not measurements](#three-stored-values-that-are-not-measurements). |

`load_power` is the counter-example and is correctly absent. `data.py:698-703` sets it
from register 27 `power_to_user` — power imported from the grid. That settles from the
library's own source why `load_power` reads zero all day on a system running off solar
and battery: it was never house load. It also confirms `_house_load()` is right to read
register 170 `output_power`, with register 24 `eps_power` as its fallback.

## The data classes: fields the adapter does not name

The register tables answer the question from the wire end. This answers it from the
library end, and catches fields that no register table mentions.

| Class | Fields | Named by the adapter | Not named |
|---|---:|---:|---:|
| `InverterRuntimeData` | 110 | 67 | 43 |
| `InverterEnergyData` | 37 | 24 | 13 |
| `BatteryBankData` | 31 | 24 | 7 |
| `BatteryData` (per module) | 35 | 23 | 12 |

"Named by the adapter" follows the rule set out under [How the mapping was
established](#how-the-mapping-was-established): the seven tuple constants *plus* the
three paths hand-coded in `to_sample`. Counting the tuples alone gives 64 for
`InverterRuntimeData` and 22 for `BatteryBankData`, so the rule has to be stated or the
rows cannot be reproduced. The totals are raw `dataclasses.fields()` counts, so they
include bookkeeping fields that are not candidates for anything — but not uniformly,
and the differences are worth knowing before subtracting: `InverterRuntimeData` and
`BatteryBankData` carry `timestamp` and both `_raw_soc` / `_raw_soh` corruption
canaries; `InverterEnergyData` carries `timestamp` and neither canary;
`BatteryData` carries both canaries and no `timestamp` at all. The bank additionally
carries `batteries`, the list we walk into per-module samples rather than read as a
value.

Most of the rest is already accounted for above — PV4–6, the S/T phases, the reserved
temperatures, the split-phase per-leg block. Four groups are not:

**Seven runtime fields are declared and never written.** `inverter_current_r`, `_s`,
`_t`, `grid_current_r`, `_s`, `_t` and `inverter_apparent_power` appear in no register
table and in no `RUNTIME_FIELD` value. They are also assigned nowhere: grepping the
whole installed package for each name returns exactly one hit, its own declaration on
`InverterRuntimeData` — `data.py:183-185`, `196-198` and `240`. No constructor sets
them, cloud or Modbus, so they are permanently `None` however the library is reached.
Not collectable, and not a gap.

**`BatteryBankData.status` is a string, not a code.** `data.py:1641-1648` derives it
from charge and discharge power into "Discharging", "Charging" or "Idle", which makes
it redundant with the sign of `battery_power_w`, and `_reading` would reject a string
anyway. The field's own declaration at `data.py:1376` advertises a fourth value,
"StandBy", that the Modbus constructor never produces.

**Two per-module limits are real, populated and not collected.**
`BatteryData.charge_voltage_ref` and `discharge_voltage_cutoff` come from battery
register offsets 2 and 5, arrive over Modbus, and are the per-pack counterparts of the
bank-level limits we already store. One line each in `_module_sample` and
`metrics.py`. Worth having for pack-divergence work.

**Per-module `fault_code` and `warning_code` are a constant zero.** Set out in full
[above](#three-stored-values-that-are-not-measurements). The per-cell arrays are the
same story in a gentler form: `cell_count`, `cell_voltages` and `cell_temperatures` are
never populated on the dongle path, so only the max/min values and their cell numbers
exist, and we already take all four.

## Holding registers — 162 entries, 99 addresses, none collected

These are setpoints, not telemetry: charge and discharge current limits, SOC cutoffs,
AC-charge windows, tariff schedules, peak-shaving thresholds. They belong to a control
feature and not to the metric registry, which is why zero of them are mapped and why
that is the right number. Confirmed two ways — no holding read appears anywhere under
`src`, `tools` or `tests`, and the intersection of all 175 registry metric names with
the 162 holding canonical names is empty.

The dataclass is not the input one. `HoldingRegisterDefinition` carries `address`,
`canonical_name`, `api_param_key`, `ha_entity_key`, `bit_position`, `bit_width`,
`scale`, `signed`, `unit`, `min_value`, `max_value`, `writable`, `category`, `models`,
`description`. There is no `cloud_api_field`, no `ha_sensor_key`, no `packed`.

And 162 entries are not 162 registers: 69 of them are single-bit entries sharing six
addresses, so the tuple covers 99 distinct Modbus addresses. All 162 are 16-bit, all
162 carry all three model families, and three are read-only — 9 `com_protocol_version`,
10 `controller_version`, 19 `device_type_code`. Treat `writable=True` as a dataclass
default rather than an assertion: only those three rows set the field explicitly.

Descriptions are omitted from the tables below rather than paraphrased; several run to
a paragraph of provenance. Read them from the library when a control feature needs
one. `DongleTransport` exposes both `read_parameters(start, count)` and
`read_named_parameters(start, count)`, so the surface is reachable when that day comes.

`HoldingCategory` has nine members: `system`, `function`, `grid`, `power`, `battery`,
`schedule`, `generator`, `reactive`, `output`. Its field docstring calls it "Logical
grouping for UI organisation and read scheduling."

### `system` — 7

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 9 | `com_protocol_version` | `HOLD_COM_VERSION` | — | 1 | — | **no** |
| 10 | `controller_version` | `HOLD_CONTROLLER_VERSION` | — | 1 | — | **no** |
| 15 | `modbus_address` | `HOLD_COM_ADDR` | — | 1 | 1–247 | yes |
| 16 | `language` | `HOLD_LANGUAGE` | — | 1 | 0–1 | yes |
| 19 | `device_type_code` | `HOLD_DEVICE_TYPE_CODE` | — | 1 | — | **no** |
| 112 | `system_type` | `HOLD_SYSTEM_TYPE` | — | 1 | 0–3 | yes |
| 190 | `hold_p2` | `HOLD_P2` | — | 1 | — | yes |

### `output` — 6

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 20 | `pv_input_mode` | `HOLD_PV_INPUT_MODE` | — | 1 | 0–7 | yes |
| 22 | `pv_start_voltage` | `HOLD_START_PV_VOLT` | V | 10 | 90.0–500.0 | yes |
| 90 | `output_voltage_select` | `HOLD_INVERTER_OUTPUT_VOLTAGE` | — | 1 | 0–3 | yes |
| 91 | `output_frequency_select` | `HOLD_INVERTER_OUTPUT_FREQUENCY` | — | 1 | 0–1 | yes |
| 145 | `output_priority` | `HOLD_OUTPUT_PRIORITY` | — | 1 | 0–2 | yes |
| 146 | `line_mode` | `HOLD_LINE_MODE` | — | 1 | 0–2 | yes |

### `grid` — 12

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 23 | `grid_connection_wait_time` | `HOLD_CONNECT_TIME` | s | 1 | 30–600 | yes |
| 24 | `grid_reconnection_wait_time` | `HOLD_RECONNECT_TIME` | s | 1 | 0–900 | yes |
| 25 | `grid_voltage_connection_low` | `HOLD_GRID_VOLT_CONN_LOW` | V | 10 | — | yes |
| 27 | `grid_frequency_connection_low` | `HOLD_GRID_FREQ_CONN_LOW` | Hz | 100 | — | yes |
| 28 | `grid_frequency_connection_high` | `HOLD_GRID_FREQ_CONN_HIGH` | Hz | 100 | — | yes |
| 176 | `max_grid_input_power` | `HOLD_MAX_GRID_INPUT_POWER` | W | 1 | — | yes |
| 206 | `grid_peak_shaving_power` | `_12K_HOLD_GRID_PEAK_SHAVING_POWER` | kW | 1 | 0–25.5 | yes |
| 207 | `grid_peak_shaving_soc` | `_12K_HOLD_GRID_PEAK_SHAVING_SOC` | % | 1 | 0–100 | yes |
| 208 | `grid_peak_shaving_volt` | `_12K_HOLD_GRID_PEAK_SHAVING_VOLT` | V | 10 | — | yes |
| 218 | `grid_peak_shaving_soc_2` | `_12K_HOLD_GRID_PEAK_SHAVING_SOC_2` | % | 1 | 0–100 | yes |
| 219 | `grid_peak_shaving_volt_2` | `_12K_HOLD_GRID_PEAK_SHAVING_VOLT_2` | V | 10 | — | yes |
| 232 | `grid_peak_shaving_power_2` | `_12K_HOLD_GRID_PEAK_SHAVING_POWER_2` | kW | 1 | 0–25.5 | yes |

### `power` — 8

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 64 | `charge_power_percent` | `HOLD_CHG_POWER_PERCENT_CMD` | % | 1 | 0–100 | yes |
| 65 | `discharge_power_percent` | `HOLD_DISCHG_POWER_PERCENT_CMD` | % | 1 | 0–100 | yes |
| 66 | `ac_charge_power` | `HOLD_AC_CHARGE_POWER_CMD` | W | 1 | 0–15000 | yes |
| 67 | `ac_charge_soc_limit` | `HOLD_AC_CHARGE_SOC_LIMIT` | % | 1 | 0–101 | yes |
| 103 | `max_backflow_power_percent` | `HOLD_FEED_IN_GRID_POWER_PERCENT` | W | 1 | 0–25500 | yes |
| 116 | `ptouser_start_discharge` | `HOLD_PTOUSER_START_DISCHARGE` | W | 1 | 50–10000 | yes |
| 118 | `voltage_start_derating` | `HOLD_VOLTAGE_START_DERATING` | V | 10 | — | yes |
| 119 | `power_offset_wct` | `HOLD_POWER_OFFSET_WCT` | W | 1 | -1000 to 1000 | yes |

### `battery` — 28

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 99 | `charge_voltage_ref` | `HOLD_LEAD_ACID_CHARGE_VOLTAGE_REF` | V | 10 | 50.0–59.0 | yes |
| 100 | `discharge_cutoff_voltage` | `HOLD_LEAD_ACID_DISCHARGE_CUT_OFF_VOLT` | V | 10 | 40.0–50.0 | yes |
| 101 | `charge_current_limit` | `HOLD_LEAD_ACID_CHARGE_RATE` | A | 1 | 0–140 | yes |
| 102 | `discharge_current_limit` | `HOLD_LEAD_ACID_DISCHARGE_RATE` | A | 1 | 0–140 | yes |
| 105 | `ongrid_discharge_cutoff_soc` | `HOLD_DISCHG_CUT_OFF_SOC_EOD` | % | 1 | 10–90 | yes |
| 125 | `offgrid_discharge_cutoff_soc` | `HOLD_SOC_LOW_LIMIT_EPS_DISCHG` | % | 1 | 0–100 | yes |
| 144 | `float_charge_voltage` | `HOLD_FLOAT_CHARGE_VOLTAGE` | V | 10 | 50.0–56.0 | yes |
| 147 | `battery_capacity` | `HOLD_BATTERY_CAPACITY` | Ah | 1 | 0–10000 | yes |
| 148 | `battery_nominal_voltage` | `HOLD_BATTERY_NOMINAL_VOLTAGE` | V | 10 | 40.0–59.0 | yes |
| 149 | `equalization_voltage` | `HOLD_EQUALIZATION_VOLTAGE` | V | 10 | 50.0–59.0 | yes |
| 150 | `equalization_interval` | `HOLD_EQUALIZATION_PERIOD` | days | 1 | 0–365 | yes |
| 151 | `equalization_time` | `HOLD_EQUALIZATION_TIME` | h | 1 | 0–24 | yes |
| 158 | `ac_charge_start_voltage` | `HOLD_AC_CHARGE_START_BATTERY_VOLTAGE` | V | 10 | 38.4–52.0 | yes |
| 159 | `ac_charge_end_voltage` | `HOLD_AC_CHARGE_END_BATTERY_VOLTAGE` | V | 10 | 48.0–59.0 | yes |
| 160 | `ac_charge_start_soc` | `HOLD_AC_CHARGE_START_BATTERY_SOC` | % | 1 | 0–90 | yes |
| 161 | `ac_charge_end_soc` | `HOLD_AC_CHARGE_END_BATTERY_SOC` | % | 1 | 20–100 | yes |
| 162 | `battery_low_voltage` | `HOLD_BATTERY_LOW_VOLTAGE` | V | 10 | 40.0–50.0 | yes |
| 163 | `battery_low_back_voltage` | `HOLD_BATTERY_LOW_BACK_VOLTAGE` | V | 10 | 42.0–52.0 | yes |
| 164 | `battery_low_soc` | `HOLD_BATTERY_LOW_SOC` | % | 1 | 0–90 | yes |
| 165 | `battery_low_back_soc` | `HOLD_BATTERY_LOW_BACK_SOC` | % | 1 | 20–100 | yes |
| 166 | `battery_low_to_utility_voltage` | `HOLD_BATTERY_LOW_TO_UTILITY_VOLTAGE` | V | 10 | 44.4–51.4 | yes |
| 167 | `battery_low_to_utility_soc` | `HOLD_BATTERY_LOW_TO_UTILITY_SOC` | % | 1 | 0–100 | yes |
| 168 | `ac_charge_battery_current` | `HOLD_AC_CHARGE_BATTERY_CURRENT` | A | 1 | 0–140 | yes |
| 169 | `ongrid_eod_voltage` | `HOLD_ON_GRID_EOD_VOLTAGE` | V | 10 | 40.0–56.0 | yes |
| 202 | `stop_discharge_voltage` | `_12K_HOLD_STOP_DISCHG_VOLT` | V | 1 | 40–56 | yes |
| 227 | `system_charge_soc_limit` | `HOLD_SYSTEM_CHARGE_SOC_LIMIT` | % | 1 | 0–101 | yes |
| 228 | `system_charge_volt_limit` | `HOLD_SYSTEM_CHARGE_VOLT_LIMIT` | V | 10 | 48.0–60.0 | yes |
| 234 | `quick_charge_minute` | `SNA_HOLD_QUICK_CHARGE_MINUTE` | min | 1 | 0–1440 | yes |

### `schedule` — 22

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 68 | `ac_charge_start_hour_1` | `HOLD_AC_CHARGE_START_HOUR_1` | — | 1 | 0–23 | yes |
| 69 | `ac_charge_start_minute_1` | `HOLD_AC_CHARGE_START_MINUTE_1` | — | 1 | 0–59 | yes |
| 70 | `ac_charge_end_hour_1` | `HOLD_AC_CHARGE_END_HOUR_1` | — | 1 | 0–23 | yes |
| 71 | `ac_charge_end_minute_1` | `HOLD_AC_CHARGE_END_MINUTE_1` | — | 1 | 0–59 | yes |
| 72 | `ac_charge_enable_period_1` | `HOLD_AC_CHARGE_ENABLE_1` | — | 1 | 0–1 | yes |
| 73 | `ac_charge_enable_period_2` | `HOLD_AC_CHARGE_ENABLE_2` | — | 1 | 0–1 | yes |
| 74 | `forced_charge_power_command` | `HOLD_FORCED_CHG_POWER_CMD` | W | 1 | 0–15000 | yes |
| 75 | `forced_charge_soc_limit` | `HOLD_FORCED_CHG_SOC_LIMIT` | % | 1 | 0–100 | yes |
| 76 | `forced_charge_time_0_start` | `HOLD_FORCED_CHARGE_TIME_0_START` | — | 1 | — | yes |
| 77 | `forced_charge_time_0_end` | `HOLD_FORCED_CHARGE_TIME_0_END` | — | 1 | — | yes |
| 78 | `forced_charge_time_1_start` | `HOLD_FORCED_CHARGE_TIME_1_START` | — | 1 | — | yes |
| 79 | `forced_charge_time_1_end` | `HOLD_FORCED_CHARGE_TIME_1_END` | — | 1 | — | yes |
| 80 | `forced_charge_time_2_start` | `HOLD_FORCED_CHARGE_TIME_2_START` | — | 1 | — | yes |
| 81 | `forced_charge_time_2_end` | `HOLD_FORCED_CHARGE_TIME_2_END` | — | 1 | — | yes |
| 82 | `forced_discharge_power_command` | `HOLD_FORCED_DISCHG_POWER_CMD` | W | 1 | 0–25500 | yes |
| 83 | `forced_discharge_soc_limit` | `HOLD_FORCED_DISCHG_SOC_LIMIT` | % | 1 | 0–100 | yes |
| 84 | `forced_discharge_time_0_start` | `HOLD_FORCED_DISCHARGE_TIME_0_START` | — | 1 | — | yes |
| 85 | `forced_discharge_time_0_end` | `HOLD_FORCED_DISCHARGE_TIME_0_END` | — | 1 | — | yes |
| 86 | `forced_discharge_time_1_start` | `HOLD_FORCED_DISCHARGE_TIME_1_START` | — | 1 | — | yes |
| 87 | `forced_discharge_time_1_end` | `HOLD_FORCED_DISCHARGE_TIME_1_END` | — | 1 | — | yes |
| 88 | `forced_discharge_time_2_start` | `HOLD_FORCED_DISCHARGE_TIME_2_START` | — | 1 | — | yes |
| 89 | `forced_discharge_time_2_end` | `HOLD_FORCED_DISCHARGE_TIME_2_END` | — | 1 | — | yes |

### `generator` — 6

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 177 | `generator_rated_power` | `HOLD_GEN_RATED_POWER` | W | 1 | — | yes |
| 194 | `gen_charge_start_voltage` | `HOLD_GEN_CHARGE_START_VOLTAGE` | V | 10 | 38.4–52.0 | yes |
| 195 | `gen_charge_end_voltage` | `HOLD_GEN_CHARGE_END_VOLTAGE` | V | 10 | 48.0–59.0 | yes |
| 196 | `gen_charge_start_soc` | `HOLD_GEN_CHARGE_START_SOC` | % | 1 | 0–90 | yes |
| 197 | `gen_charge_end_soc` | `HOLD_GEN_CHARGE_END_SOC` | % | 1 | 20–100 | yes |
| 198 | `max_gen_charge_battery_current` | `HOLD_MAX_GEN_CHARGE_BATTERY_CURRENT` | A | 1 | 0–60 | yes |

### `reactive` — 4

| Addr | canonical_name | api_param_key | Unit | ÷ | Range | Writable |
|---:|---|---|---|---:|---|---|
| 59 | `reactive_power_mode` | `HOLD_Q_MODE` | — | 1 | 0–4 | yes |
| 60 | `reactive_power_pv_mode` | `HOLD_Q_PV_MODE` | — | 1 | 0–4 | yes |
| 61 | `reactive_power_setting` | `HOLD_Q_POWER` | % | 1 | -100 to 100 | yes |
| 62 | `reactive_power_pv_setting` | `HOLD_Q_PV_POWER` | % | 1 | -100 to 100 | yes |

### `function` — 69 single-bit entries across 6 addresses

**21** — 0:`eps_enable`, 1:`overload_derate_enable`, 2:`drms_enable`, 3:`lvrt_enable`, 4:`anti_island_enable`, 5:`neutral_detect_enable`, 6:`grid_on_power_soft_start`, 7:`ac_charge_enable`, 8:`seamless_switching_enable`, 9:`power_on`, 10:`forced_discharge_enable`, 11:`forced_charge_enable`, 12:`isolation_detect_enable`, 13:`gfci_enable`, 14:`dci_enable`, 15:`feed_in_grid_enable`

**26** — 0:`lsp_whole_bypass_1_enable`, 1:`lsp_whole_bypass_2_enable`, 2:`lsp_whole_bypass_3_enable`, 3:`lsp_whole_battery_first_1_enable`, 4:`lsp_whole_battery_first_2_enable`, 5:`lsp_whole_battery_first_3_enable`, 6:`lsp_whole_self_consumption_1_enable`, 7:`lsp_whole_self_consumption_2_enable`, 8:`lsp_whole_self_consumption_3_enable`, 9:`lsp_battery_volt_or_soc`

**110** — 0:`pv_grid_off_enable`, 1:`run_without_grid`, 2:`micro_grid_enable`, 3:`battery_shared`, 4:`charge_last`, 5:`take_load_together`, 6:`buzzer_enable`, 7:`go_to_offgrid`, 8:`green_mode_enable`, 9:`battery_eco_enable`, 10:`working_mode`, 11:`pvct_sample_type`, 12:`pvct_sample_ratio`, 13:`ct_sample_ratio`

**120** — 0:`half_hour_ac_charge_start_enable`, 1:`sna_battery_discharge_control`, 2:`phase_independent_compensate_enable`, 3:`ac_charge_type`, 4:`discharge_control_type`, 5:`ongrid_eod_type`, 6:`generator_charge_type`

**179** — 0:`ac_ct_direction`, 1:`pv_ct_direction`, 2:`afci_alarm_clear`, 3:`battery_wakeup_enable`, 4:`volt_watt_enable`, 5:`trip_time_unit`, 6:`active_power_cmd_enable`, 7:`grid_peak_shaving_enable`, 8:`gen_peak_shaving_enable`, 9:`battery_charge_control`, 10:`battery_discharge_control`, 11:`ac_coupling_enable`, 12:`pv_arc_enable`, 13:`smart_load_enable`, 14:`rsd_disable`, 15:`ongrid_always_on`

**233** — 0:`quick_charge_start_enable`, 1:`battery_backup_enable`, 2:`maintenance_enable`, 3:`weekly_schedule_enable`, 10:`over_freq_fast_stop`, 12:`sporadic_charge_enable`


## GridBOSS registers — 92, none collected, and probably not applicable

`GRIDBOSS_REGISTERS` covers addresses 1–130 on a separate physical MID/GridBOSS unit,
not on the inverter. It is reached by `transport.read_midbox_runtime()` — five input
reads and one holding read, over the groups `(0,40) (40,28) (68,40) (108,12) (128,4)` —
and only on a device whose type code passes `is_midbox_device`. Whether the reference
installation has such a unit is not established; `transport.read_device_type()` would
settle it. Units are kWh 48, A 16, W 16, V 9, Hz 3.

**`voltage` — 9** — 1 `grid_voltage`, 2 `ups_voltage`, 3 `gen_voltage`, 4 `grid_l1_voltage`, 5 `grid_l2_voltage`, 6 `ups_l1_voltage`, 7 `ups_l2_voltage`, 8 `gen_l1_voltage`, 9 `gen_l2_voltage`

**`current` — 16** — 10 `grid_l1_current`, 11 `grid_l2_current`, 12 `load_l1_current`, 13 `load_l2_current`, 14 `gen_l1_current`, 15 `gen_l2_current`, 16 `ups_l1_current`, 17 `ups_l2_current`, 18 `smart_port1_l1_current`, 19 `smart_port1_l2_current`, 20 `smart_port2_l1_current`, 21 `smart_port2_l2_current`, 22 `smart_port3_l1_current`, 23 `smart_port3_l2_current`, 24 `smart_port4_l1_current`, 25 `smart_port4_l2_current`

**`power` — 8** — 26 `grid_l1_power`, 27 `grid_l2_power`, 28 `load_l1_power`, 29 `load_l2_power`, 30 `gen_l1_power`, 31 `gen_l2_power`, 32 `ups_l1_power`, 33 `ups_l2_power`

**`smart_load` — 8** — 34 `smart_load1_l1_power`, 35 `smart_load1_l2_power`, 36 `smart_load2_l1_power`, 37 `smart_load2_l2_power`, 38 `smart_load3_l1_power`, 39 `smart_load3_l2_power`, 40 `smart_load4_l1_power`, 41 `smart_load4_l2_power`

**`frequency` — 3** — 128 `phase_lock_frequency`, 129 `grid_frequency`, 130 `gen_frequency`

**`energy_daily` — 24** — 42 `load_energy_today_l1`, 43 `load_energy_today_l2`, 44 `ups_energy_today_l1`, 45 `ups_energy_today_l2`, 46 `grid_export_today_l1`, 47 `grid_export_today_l2`, 48 `grid_import_today_l1`, 49 `grid_import_today_l2`, 52–59 `smart_load1..4_energy_today_l1/l2`, 60–67 `ac_couple1..4_energy_today_l1/l2`

**`energy_lifetime` — 24** — 68 `load_energy_total_l1`, 70 `load_energy_total_l2`, 72 `ups_energy_total_l1`, 74 `ups_energy_total_l2`, 76 `grid_export_total_l1`, 78 `grid_export_total_l2`, 80 `grid_import_total_l1`, 82 `grid_import_total_l2`, 88–102 `smart_load1..4_energy_total_l1/l2`, 104–118 `ac_couple1..4_energy_total_l1/l2`

## Per-module battery registers — 21 entries at 18 offsets

`BATTERY_REGISTERS` is addressed by **offset** within a module's block, not by absolute
address, and three offsets carry two entries each because they are packed. Seventeen of
the 21 reach a per-module metric; the serial number is the module's identity rather
than a metric; and two of the remaining three are worth taking.

| Offset | canonical_name | Unit | ÷ | Our metric | Description (verbatim) |
|---:|---|---|---:|---|---|
| 0 | `battery_status_header` | — | 1 | `status_code` | Status header. Upper byte 0xC0 = BMS connected; lower byte = total batteries in the parallel group (e.g. 0xC003 = 3 batteries, 0xC004 = 4 batteries). Any non-zero value means slot is occupied. |
| 1 | `battery_full_capacity` | Ah | 1 | `full_capacity_ah` | Full (rated) capacity in amp-hours. |
| 2 | `battery_charge_voltage_ref` | V | 10 | **available** | BMS recommended charge voltage reference. |
| 3 | `battery_charge_current_limit` | A | 10 | `charge_current_limit_a` | BMS maximum charge current limit (0.1A units, matches input reg 81). |
| 4 | `battery_discharge_current_limit` | A | 10 | `discharge_current_limit_a` | BMS maximum discharge current limit (0.1A units, matches input reg 82). |
| 5 | `battery_discharge_voltage_cutoff` | V | 10 | **available** | BMS discharge cutoff voltage. |
| 6 | `battery_voltage` | V | 100 | `voltage_v` | Battery module total voltage. |
| 7 | `battery_current` | A ± | 10 | `current_a` | Battery module current. Positive = charging, negative = discharging. |
| 8 | `battery_soc` | % | 1 | `soc_pct` | State of charge (low byte of packed SOC/SOH register). |
| 8 | `battery_soh` | % | 1 | `soh_pct` | State of health (high byte of packed SOC/SOH register). |
| 9 | `battery_cycle_count` | — | 1 | `cycle_count` | Charge/discharge cycle count. |
| 10 | `battery_max_cell_temp` | °C ± | 10 | `cell_max_temperature_c`, `temperature_c` | Maximum cell temperature across all cells in this module. |
| 11 | `battery_min_cell_temp` | °C ± | 10 | `cell_min_temperature_c` | Minimum cell temperature across all cells in this module. |
| 12 | `battery_max_cell_voltage` | V | 1000 | `cell_max_voltage_v` | Maximum individual cell voltage (raw value in millivolts). |
| 13 | `battery_min_cell_voltage` | V | 1000 | `cell_min_voltage_v` | Minimum individual cell voltage (raw value in millivolts). |
| 14 | `battery_max_cell_num_temp` | — | 1 | `cell_max_temperature_num` | Cell number with highest temperature (low byte). |
| 14 | `battery_min_cell_num_temp` | — | 1 | `cell_min_temperature_num` | Cell number with lowest temperature (high byte). |
| 15 | `battery_max_cell_num_voltage` | — | 1 | `cell_max_voltage_num` | Cell number with highest voltage (low byte). |
| 15 | `battery_min_cell_num_voltage` | — | 1 | `cell_min_voltage_num` | Cell number with lowest voltage (high byte). |
| 16 | `battery_firmware_version` | — | 1 | — (string) | Firmware version. Packed: high byte = major, low byte = minor. |
| 17 | `battery_serial_number` | — | 1 | identity | Serial number. 8 regs (offsets 17-24), 2 ASCII chars each. |

Note what is **not** here: `fault_code` and `warning_code` have no battery register at
all, which is why the library never sets them and our per-module columns hold a
constant zero. Nor is there a per-cell array; only the extremes and their cell numbers
exist on the dongle path.

Empty slots are dropped by the adapter on the serial number, because the library
defaults a module's fields to zero rather than `None` — an unpopulated slot arrives as
a full set of zeroes with an empty serial, and a module is identified by serial and
never by slot index.

## Worth collecting

Eleven registers are a genuinely one-line change: a real data-class field, already on
the wire, no name collision in `metrics.py`. The cost argument matters here. Dumping
`INPUT_REGISTER_GROUPS` and the group list `read_runtime()` passes shows it requests
*every* group — `power_energy` (0,32), `status_energy` (32,32), `temperatures` (64,16),
`bms_data` (80,33), `extended_data` (113,41), `eps_split_phase` (140,3), `output_power`
(170,4), `split_phase_grid` (193,12). So all eleven are already arriving on a dongle
that admits one client, and collecting them costs zero extra round trips.

**Split-phase per-leg detail — 8 registers (193–200).** Per-leg grid and generator
voltage, and per-leg inverter and rectifier power. These complete the picture we
already half-have as `grid_power_l1_w` and `grid_power_l2_w`, on a US split-phase
service where a leg imbalance is invisible in the combined figure. The most useful
addition available.

**Register 113 `parallel_config` — three metrics from one register.** Already unpacked
by the library into `parallel_master_slave`, `parallel_phase` and `parallel_number`.
Bears directly on #9; see below.

**Register 77 `ac_input_type`** — "AC input type bitfield. Bit0: 0=Grid, 1=Generator."
Nothing we store currently says whether the AC input is grid or generator.

**Register 80 `bms_battery_type`**, reachable only through the bank object, and the
direct input to #13. See below.

Two per-module additions are outside the input-register set and equally cheap:
`BatteryData.charge_voltage_ref` and `discharge_voltage_cutoff`, battery offsets 2 and
5, one line each in `_module_sample`.

Three things that look like cheap additions and are not, so they do not appear in that
count:

- **Generator and per-leg EPS energy (124, 125, 133, 134, 135, 137).** All six have
  real `InverterEnergyData` fields, which is exactly why they look free. They are not:
  `read_energy()` requests only `power_energy`, `status_energy`, `output_power` and
  `bms_data`, never `extended_data`, so the registers are absent from the dict and
  `read_scaled` returns `None`. Mapping them would collect nothing forever. Worth the
  upstream effort only if the `eps_energy_today` discrepancy is ever chased — 58.1 kWh
  against a `load_energy_today` of 95.3 on the same day, which is explained by the share
  of house energy that flows during grid bypass while the EPS port sits idle — since
  133/134 and 135/137 split that same quantity by leg.
- **PV4–6 (217–222, 223, 224, 226, 227, 229, 230).** `_read_pv4_6_registers` returns
  `{}` unless `pv_string_count >= 4`, and `DEVICE_TYPE_CODE_PV_STRING_COUNT` reads
  `DEVICE_TYPE_CODE_PV_SERIES: 3,  # 18kPV, 12kPV (live-confirmed)`. Never requested,
  never parsed.
- **Registers 210 and 232.** `quick_charge_remaining_seconds` needs a dedicated
  `transport.read_quick_charge_remaining_seconds()` call — an extra round trip on the
  one-client socket — and `smart_load_power` is outside every register group, so it is
  not on the wire at all.

## Bearing on the open issues

**#9, parallel stack — one register, already decoded, three lines of work.** Register
113 carries `packed='b0-1=role,b2-3=phase,b8-15=parallel_num'`, and `data.py:640-643`
calls `unpack_parallel_config` to split it across three real `InverterRuntimeData`
fields. The bit layout is documented in that helper's docstring, not on the fields —
`transports/_canonical_reader.py:177-179`, verbatim: "Bits 0-1: master_slave (0=no
parallel, 1=master, 2=slave, 3=3-phase master) / Bits 2-3: phase (0=R, 1=S, 2=T) /
Bits 8-15: parallel_number (unit ID)". The fields themselves carry only trailing
comments, `data.py:304-306`. Register 113 is the first address of the `extended_data`
group `read_runtime()` already requests, so it costs nothing extra.
`transport.read_parallel_config()` also exists but is a second read of a register we
already have.

`parallel_config` is the only member of the `parallel` category, so #9 will find
nothing else in the input registers. Two pieces it may still want are elsewhere:
holding register 112 `system_type` ("0=Single, 1=Master, 2=Slave, 3=3-Phase Master"),
and the transport's `read_serial_number()`, `read_firmware_version()` and
`read_device_type()` for device identity. One caveat on counting: `data.py:1677-1678`
warns that "battery_parallel_count under-reports on parallel systems, #170/#258" and
derives the slot count from the populated register map instead of trusting register 96.
Our `battery_module_count` comes from register 96 on the runtime path and from
`bank.battery_count` on the bank path. Which of the two is right is **not established**:
the reference installation has four modules and both paths report four, so the case the
library warns about does not arise here and cannot be told apart. A parallel system is
what would settle it.

**#13, protocol detection — there is a register, and `detect_protocol` answers a
different question.** Register 80 `bms_battery_type` is described as "Battery
type/brand and communication type (0=CAN, 1=RS485)" — precisely the discriminator.
It is `None` in `RUNTIME_FIELD` and lands on no runtime field, so the obvious route is
closed, but `BatteryBankData.bms_battery_type` exists and is populated from register 80,
which is inside the `bms_data` group both `read_runtime` and `read_battery` already
cover. One line in `_BANK_METRICS`, observable only while the BMS answers.

`pylxpweb.battery_protocols` exports `BatteryProtocol`, `BatteryRegister`,
`BatteryRegisterBlock`, `EG4MasterProtocol`, `EG4SlaveProtocol`, `detect_protocol`,
`decode_ascii` and `signed_int16`; nothing in our tree calls any of them. Read
`detect_protocol`'s docstring before designing around it: "Checks registers 0-18: if
mostly zeros, it's a master battery. If 3+ registers are non-zero, it's a slave
battery." That is a master/slave *pack* discriminator, not a CAN/RS485 *transport* one.
The two must not be conflated.

**#14, the 6000XP/12000XP surface — it is not in the input registers.** Exactly two of
the 143 are model-restricted and both are LXP-only. There is no EG4_OFFGRID-only
register, and the two EG4 families resolve to an identical 141. Whatever separates the
6000XP/12000XP lives in three other places:

- `GRIDBOSS_REGISTERS`, above — but that is a separate physical unit.
- The feature tables in `devices/inverters/_features.py`. `FAMILY_DEFAULT_FEATURES`
  differs between EG4_HYBRID and EG4_OFFGRID on exactly six flags:
  `discharge_recovery_hysteresis` (False vs True), `quick_charge_minute` (False vs
  True), `parallel_support` (True vs False), `volt_watt_curve` (True vs False),
  `grid_peak_shaving` (True vs False), `drms_support` (True vs False). That is the real
  axis.
- The holding table, where `SNA_HOLD_QUICK_CHARGE_MINUTE` and the `_12K_HOLD_*`
  peak-shaving registers are SNA-specific.

And one thing #14 will have to settle upstream rather than here:
`DEVICE_TYPE_CODE_PV_STRING_COUNT` carries `# TODO(confirm): DEVICE_TYPE_CODE_SNA
pv_string_count (EG4 12000XP/6000XP).` and falls back to 3. Whether those models expose
more than three strings is unresolved in the library, not just in our adapter.

**#15, temperature — the premise is wrong on two of its three registers.** The issue
names 64, 68 and 108 as unmapped. Two of them are already collected: register 64
`internal_temperature` is our `inverter_temperature_c`, and register 108
`temperature_t1` is our `board_temperature_c`. Only register 68
`battery_control_temperature` is genuinely unmapped, and adding it to `metrics.py`
would do nothing — it is one of the fifteen the library reads and discards before any
data class, so reaching it needs a raw register read or an upstream change.

That leaves very little temperature to find. We already take 64, 65, 66, 103, 104 and
108, plus the four per-module cell extremes. The remaining candidates are 67 (the
broken 11880 register), 68 (unreachable), and 109–112 (reserved). #15 should be
re-scoped on that basis.

## Not established

Each of these is a real gap rather than a formality, and each has a specific thing that
would close it.

**Whether any of these registers returns a plausible value on the reference 18kPV.**
Everything above is the library's static definition tables joined to our static adapter
tables. No hardware was touched while writing it, and the repository holds no captured
register snapshot. Settled by one `read_runtime()` + `read_battery()` + `read_energy()`
capture written to a fixture, then re-running this join with an observed-value column.

**Whether registers 109–112 read a flat zero, and whether the S/T registers read 6.4 V
and 1545.9 V.** The "documented as reserved" and "garbage on split-phase" halves are
both confirmed in library source. The specific numbers are claims in our own adapter
comment that this pass did not verify. The same capture settles them, and the reserved
marking is sufficient reason to leave the registers alone either way.

**What `bank.bms_battery_type` reads on this hardware** — 0 for CAN or 1 for RS485. One
live poll settles it, and it is the direct input to #13.

**Whether the reference installation includes a GridBOSS/MID unit at all.**
`transport.read_device_type()` settles it. Without one, the 92 GridBOSS registers are
inapplicable rather than merely uncollected.

**Whether the per-module `fault_code` and `warning_code` zeros are also zero in the
live store.** The code path is unambiguous — `BatteryData.from_modbus_registers` never
passes either argument — but confirming against stored data would make the bug report
airtight.

**What register 5 reads while the BMS is silent.** It decides whether the ungated
runtime copy of `battery_soh_pct` writes a fabricated 100 through a CAN dropout, or
nothing at all. One poll taken with the BMS link down settles it, and until then the
`_bms_is_answering` gate should not be assumed to cover that column.

**Whether register 69 counts seconds or hours.** The register definition and the field
it lands on disagree, and nothing in the library reconciles them — see the note under
the runtime table. One reading held against the inverter's known age settles it, and
until it is settled `inverter_run_time_s` is a reset detector rather than a duration.

**Whether the eleven available registers pass validation as well as mapping.** They
were diffed against the adapter and against `metrics.py`'s names, which found no
collision; nobody has checked what plausible bounds each should carry in the registry.
