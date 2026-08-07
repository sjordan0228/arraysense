# Configuration

> Solar ArraySense is not yet functional. The settings below are the ones the
> collector will read. Treat this as a reference for what will be required rather
> than working instructions.

Configuration lives in a TOML file, by default at `/etc/arraysense/config.toml`. Pass
a different path with `--config`.

```toml
dongle_host     = "192.168.1.50"
dongle_serial   = "BA12345678"
inverter_serial = "CE12345678"
poll_interval   = 11.0
database_path   = "/var/lib/arraysense/arraysense.db"
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

Lower values give finer resolution at the cost of more writes. At eleven seconds a
full day measures about 9 MB, which matters mainly if the database sits on an SD
card.

There is little to gain below about ten seconds. The dongle replies at its own
pace, so a shorter interval mostly produces reads that overlap the previous one
and get abandoned.

### `database_path`

Where the SQLite database is written. The directory must exist and be writable.

On a Raspberry Pi, prefer a USB SSD over the SD card. Continuous database writes wear
SD cards out.

## Data retention

Three resolution tiers are kept, and queries are served from the coarsest one that
still fills the requested chart width:

| Tier | Resolution | Retained |
| --- | --- | --- |
| Raw | Polling interval | 30 days |
| Minute | 1 minute | 1 year |
| Hour | 1 hour | Indefinitely |

Retention will become configurable. The defaults put a full three-tier database at
roughly 280 MB.

## Secrets

Keep the configuration file out of version control. It contains your serial numbers,
which identify your hardware.
