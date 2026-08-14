# Configuration

> These are working instructions. The collector reads this file at startup, and
> the installer deliberately writes no configuration — the setup wizard writes the
> first one on the dashboard's first visit. Once the service is running, most of
> these settings are edited from the settings page rather than in the file.

Configuration lives in a TOML file, by default at `/etc/arraysense/config.toml`. Pass
a different path with `--config`.

```toml
dongle_host     = "192.168.1.50"
dongle_serial   = "BA12345678"
dongle_port     = 8000
inverter_serial = "CE12345678"
poll_interval   = 11.0
driver          = "eg4_luxpower"
model           = ""
battery_source  = ""
database_path   = "/var/lib/arraysense/arraysense.db"
synchronous     = "full"
transport       = "dongle"
serial_device   = ""
serial_baud     = 19200
serial_unit_id  = 1
```

## Settings

### `dongle_host`

IP address or hostname of the inverter's WiFi dongle. Give the dongle a static DHCP
lease so this does not change.

### `dongle_serial`

The dongle's ten-character serial number, used to authenticate to it. Read it from
the dongle's label, your router's DHCP client list, or the WiFi access point name it
broadcasts.

### `inverter_serial`

The inverter's ten-character serial number, on the unit's label and in its LCD menu.

### `poll_interval`

Seconds between reads. Defaults to `11.0`.

Lower values give finer resolution at the cost of more writes. At eleven seconds the
database grows about 5.3 MB per day across all tiers (measured on the reference
installation: 1.6 MB/day inverter raw, 3.5 MB/day module raw, 0.25 MB/day inverter
minute). Halving the interval roughly doubles that.

There is little to gain below about ten seconds. The dongle replies at its own
pace, so a shorter interval mostly produces reads that overlap the previous one
and get abandoned.

### `driver`

Which family of inverter to read. Defaults to `eg4_luxpower`, which covers the EG4
and LuxPower inverters reached over the WiFi dongle — the 18kPV, the 12kPV, the
FlexBOSS models and the off-grid 6000XP. There is no reason to set this today; it
exists so that a second family can be added as a directory rather than as an edit
to the collector.

An unrecognised name stops the service at startup with the list of names that work.

### Model support

The EG4/LuxPower family covers the hybrids — 18kPV, 12kPV, FlexBOSS21 and
FlexBOSS18 — and the off-grid 6000XP. The hybrids are read in full: all three PV
strings, the backup panel, the battery bank and every kWh counter. An off-grid
machine differs in three ways, all declared by the driver and shown on the setup
page when you choose it:

- **Two PV strings, not three.** The 6000XP has 2 MPPTs with 1 input each (EG4
  spec sheet). The third-string columns are never created.
- **No generator block.** Register 123, which the register map calls "generator
  power", is a seconds counter on off-grid — proven by firmware disassembly, not
  inferred — and registers 124–126 are ARM status words, not energy. The
  generator power, voltage and frequency readings are therefore not offered at
  all rather than risk storing a wrong value. This is more conservative than
  upstream `pylxpweb`, which removed only the power and energy sensors; it is a
  judgement rather than a finding, and a reading will be added back if one is
  ever confirmed.
- **The smart-load split is cloud-only.** The GEN terminal can be repurposed as a
  smart load, but the itemised `smartLoadPower`/`epsLoadPower` figures have no
  local Modbus register. Your house load *total* is read locally; only the split
  is not.

**Set `model` if you own one of these.** All of the above follows from the
configured model, so an off-grid installation that leaves `model` unset is read
as though it were a hybrid — which puts the seconds counter back on the chart as
generator watts. The first-run wizard always sets it, so this only affects a
`config.toml` written by hand. The wizard cannot detect which off-grid model you
have either: device type code 54 covers the 6000XP and the 12000XP alike, so
Detect deliberately reports nothing rather than guess, and you pick from the
list.

Sources: [`joyfulhouse/eg4_web_monitor` issue #544](https://github.com/joyfulhouse/eg4_web_monitor/issues/544)
(the register 123 disassembly), [issue #222](https://github.com/joyfulhouse/eg4_web_monitor/issues/222)
(the smart-load split), and the
[EG4 6000XP spec sheet](https://eg4electronics.com/wp-content/uploads/2024/04/EG4-6000XP-Inverter-Spec-Sheet.pdf).

The WiFi dongle's TCP port 8000 works on these models. Modbus TCP on port 502 is
closed on them, so do not spend an evening trying to reach the inverter directly
over Modbus TCP; use the dongle, or a USB-to-RS485 adapter with
`transport = "modbus_serial"`.

### `model`

Which inverter model within the driver family. Defaults to `""`.

When blank the driver tries to identify the model from the inverter's own reply. Set
it explicitly to skip that detection, or to pin a model the driver does not yet
recognise. The wizard sets this from whatever model you choose.

### `battery_source`

Where battery-module data comes from. Defaults to `""`.

The empty default derives the source from the driver: `relayed` when the inverter
family carries BMS data, `none` otherwise — which is every existing installation's
behaviour. `direct` is reserved for a future battery driver and is refused at
construction until one exists.

### `synchronous`

How durably a reading has to land before the write is called done. Defaults to
`"full"`.

`full` fsyncs every commit. `normal` syncs at checkpoint instead — measured on a
Raspberry Pi, 200 polls cost 207 fsyncs at full and 7 at normal. Neither risks
corruption; `normal` can lose the most recent readings, roughly the last five
minutes, if power is cut abruptly.

### `dongle_port`

The dongle's TCP port. Defaults to `8000`.

Newer dongle firmware removes port 8000 and Ethernet dongles never had it. When the
port is gone, switch to wired RS485 by setting `transport = "modbus_serial"` and
`serial_device`.

### `database_path`

Where the SQLite database is written. The directory must exist and be writable.

On a Raspberry Pi, prefer a USB SSD over the SD card. Continuous database writes wear
SD cards out.

### `transport`

How to reach the inverter. Defaults to `"dongle"`.

`dongle` uses the WiFi dongle's TCP port. `modbus_serial` uses a USB-to-RS485
adapter wired to the inverter's 485A/485B terminals. When set to `modbus_serial`,
`serial_device` must also be set.

### `serial_device`

Device path for the USB-to-RS485 adapter. Only used when `transport` is
`"modbus_serial"`. Defaults to `""`.

Nothing expands a glob here, so give a real path: `/dev/ttyUSB0`, or better a stable
name that survives replugging, such as `/dev/serial/by-id/…` or a udev symlink. The
reference installation pins its adapter to `/dev/rs485` with a udev rule.

### `serial_baud`

Baud rate for the serial connection. Defaults to `19200`, which is the standard for
EG4/LuxPower inverters.

### `serial_unit_id`

Modbus unit ID for the serial connection. Defaults to `1`. Must be between 1 and
247, because 0 is the broadcast address and never answers a read.

## Authentication

Authentication is optional and off until a password is set. It is not a
configuration-file setting and it is not in the settings registry: the password
is stored in the database under its own key, and the Access section on the
Settings page's General tab is where it is set, changed and cleared. There is no
default password and no way for an upgrade to lock an installation out — with no
hash stored, the service behaves exactly as it did before.

When a password is set, the write endpoints — settings, setup, yield and resume,
and the efficiency backfill — ask for a session cookie, while every read stays
open. The dashboard only reads, so it never logs in and a wall display needs
nothing on its screen. The password and the session cookie cross the network in
plain HTTP, so the protection is against other devices on the LAN changing
things by accident or mischief, not against anyone who can watch the traffic.

Sessions live in the service's memory only, so a restart ends every session.
That is accepted: the dashboard holds none, and nothing about a session is
written to disk where a backup could carry it.

Forgetting the password is not a lockout. On the machine itself, run:

```bash
arraysense --clear-password
```

It removes the stored hash and lets every write through again, and it reports
which of the two it did — cleared, or already off.

## Data retention

Three resolution tiers are kept, and queries are served from the coarsest one that
still fills the requested chart width:

| Tier | Resolution | Intended retention |
| --- | --- | --- |
| Raw | Polling interval | 30 days |
| Minute | 1 minute | 1 year |
| Hour | 1 hour | Indefinitely |

`retention.enabled` is off by default. When enabled, `retention.raw_days` keeps raw
inverter and module data for 30 days, and `retention.minute_days` keeps inverter
minute data for 365 days; both periods are adjustable in the settings page. Nothing
is deleted unless an `arraysense-*.db.gz` backup is at least as new as the applicable
cutoff, and a source bucket stays until every required coarser tier holds it. Raw
inverter rows therefore require both minute and hourly coverage, raw module rows
require hourly coverage for the same module, and minute rows require hourly coverage.
Hourly data is kept indefinitely.

To inspect one guarded pass without changing the database, run:

```bash
arraysense --config /etc/arraysense/config.toml --prune-dry-run
```

The report lists each table's effective cutoff, rows that would be removed, oldest
row, and any reason the pass stopped. After checking that report, run the same pass
for real with `arraysense --config /etc/arraysense/config.toml --prune`. Both commands
apply the backup and coverage gates; `--prune` deletes only the bounded batches it
reports.

SQLite reuses pages freed by retention, so the database stops growing at its retained
size but does not shrink. It deliberately does not run `VACUUM`: that operation needs
about twice the database size in free space and can hold the write lock for minutes on
a Raspberry Pi. A failed or stale backup stops retention safely and leaves the database
growing until the backup is repaired.

## Secrets

Keep the configuration file out of version control. It contains your serial numbers,
which identify your hardware.
