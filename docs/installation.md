# Installation

> Solar ArraySense is not yet functional. This page describes the intended
> installation and the hardware requirements, which are settled. Steps marked
> *Not yet available* will work once the collector is implemented.

## Hardware requirements

### Inverter

Developed against an **EG4 18kPV**. EG4 inverters are manufactured by LuxPower and
rebranded for the US market, so LuxPower models using the same dongle protocol are
expected to work. Other models in the family (12kPV, 6000XP, FlexBOSS, LXP series)
are untested.

You need one of the following connections to the inverter:

| Connection | Status | Notes |
| --- | --- | --- |
| WiFi dongle, TCP port 8000 | Supported | Requires the dongle serial and inverter serial |
| Wired RS485 | Supported | A USB-to-RS485 adapter to the 485A/485B terminals; set `transport = "modbus_serial"` |

Newer dongle firmware removes port 8000, and Ethernet dongles never had it. If your
dongle has been updated recently, the WiFi path may not be available to you.

### Batteries

Per-module battery data requires batteries connected to the inverter in **closed-loop
CAN**. Developed against **EG4 PowerPro WallMount** modules. If the batteries are not
in closed loop, the inverter reports only stack aggregates and per-module fields will
be empty.

The inverter exposes four battery register slots. Installations with more than four
modules rotate them through those slots, so each module is identified by serial
number rather than position.

### Host

Runs on ARM64 or x86-64: a Raspberry Pi, an LXC container, or any Linux host.

Memory and CPU requirements are modest. The measured write load is roughly 52 MB per
day at a ten-second polling interval. On a Raspberry Pi, put the database on a USB
SSD rather than the SD card — a card carrying a continuous database workload will
wear out, and the existing installation this project replaces wrote about 400 GB per
year to one.

## Before you install

### Only one client may talk to the dongle

The WiFi dongle accepts **exactly one TCP connection**. A second client is closed
immediately, and two clients repeatedly evicting each other produce CRC errors and
missing data on both.

Before starting Solar ArraySense, stop anything else polling the dongle. That
includes the EG4 monitoring app, Solar Assistant, Home Assistant integrations, and
any scripts of your own.

### Collect two serial numbers

The dongle protocol authenticates with both.

**Dongle serial** — ten characters, usually beginning `BA`, `BJ`, `BG`, `BE` or `DJ`.
Found on the dongle's label, in your router's DHCP client list, or broadcast as the
dongle's WiFi access point name.

**Inverter serial** — ten characters, on the inverter's label and in its LCD menu.

### Find the dongle's address

Check your router's DHCP client list for the dongle. Assigning it a static lease is
worth doing, since the address ends up in your configuration.

## Install

Docker will be the supported path, because the project requires Python 3.12 and
several common distributions still ship 3.11. A single image will cover ARM64 and
x86-64. **That image does not exist yet.** Until it does, run from source — which
works on both a Raspberry Pi and an LXC container.

### From source

[uv](https://docs.astral.sh/uv/) installs Python 3.12 itself, so the distribution's
Python version does not matter.

```bash
sudo useradd --system --home /opt/arraysense --shell /usr/sbin/nologin arraysense
sudo git clone https://github.com/sjordan0228/arraysense /opt/arraysense
cd /opt/arraysense && sudo -u arraysense uv sync

sudo install -d -o arraysense -g arraysense /etc/arraysense /var/lib/arraysense
sudo cp config.example.toml /etc/arraysense/config.toml
sudo chmod 600 /etc/arraysense/config.toml
```

Edit `/etc/arraysense/config.toml` with the two serial numbers and the dongle
address you collected above. It is mode 600 because it identifies your hardware.

Check it starts before making it a service:

```bash
sudo -u arraysense /opt/arraysense/.venv/bin/python -m arraysense \
    --config /etc/arraysense/config.toml
```

The dashboard is then at `http://<host>:8080`. A serial-number mismatch shows up
immediately as every read failing — see
[troubleshooting.md](troubleshooting.md).

### As a service

```bash
sudo cp packaging/arraysense.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now arraysense
systemctl status arraysense
```

The shipped unit runs as an unprivileged `arraysense` user with the filesystem
locked down, and allows thirty seconds to stop. That stop timeout is not padding:
the dongle accepts exactly one TCP client, and the service gives that slot up during
shutdown. A process killed before it finishes leaves the dongle occupied until it
times the connection out by itself, which blocks both the next start and the vendor's
app.

It runs a serial installation out of the box as well as a dongle one. A serial
adapter lives in `/dev` and is owned by the `dialout` group (that name on the
Debian-family systems this targets; a few distributions call it `uucp`), so the
unit sets `PrivateDevices=false` to leave the device node visible and gives the
service user `dialout` membership to open it — without either, every serial poll fails
with `could not open port ... No such file or directory` while the adapter
plainly exists, an error that points nowhere near the sandbox. A dongle-only
installation uses neither and may set `PrivateDevices=true` back with a drop-in.

On a Raspberry Pi, point `database_path` at a USB SSD rather than the SD card.
Continuous writes wear cards out, and at the default poll interval this writes about
9 MB a day.

### Serving on port 80

The default is 8080 because ports below 1024 are privileged and this service does not
run as root. To use port 80, add `--port 80` to `ExecStart` and uncomment
`AmbientCapabilities=CAP_NET_BIND_SERVICE` in the unit. That grants the one binding
and nothing else — running the whole service as root to get a shorter URL is the
wrong trade.

## Keeping it running

Two layers, because they catch different failures and neither catches both.

**systemd restarts a process that exits.** `Restart=always` with `RestartSec=10`
is in the shipped unit and covers a crash, an out-of-memory kill, or a
deliberate stop. It is `always` and not `on-failure` for a specific reason: the
watchdog below restarts a stalled loop by sending SIGTERM, and systemd treats a
SIGTERM as a clean exit by default — `on-failure` would never restart after
exactly the restart the watchdog exists to trigger.

**The service restarts itself if it stops collecting.** This is the failure
systemd cannot see: the process alive and serving pages perfectly while the poll
loop has died or hung. Every chart keeps drawing, quietly growing staler, and
nothing anywhere reports a problem. It happens two ways — a poll task that
raised and was never awaited, so its exception went to the asyncio log and the
web server carried on; or a read that never returns, which produces the same
silence with no exception at all.

A watchdog task checks every thirty seconds whether the loop has produced
*either* a reading or an error recently. If it has produced neither for twenty
minutes it logs the fact and sends the process SIGTERM, which runs the normal
shutdown — releasing the dongle's single TCP slot — and lets systemd bring
everything back.

What it deliberately does **not** trigger on is an inverter that is simply not
answering. Those polls fail, and a failure is the loop working correctly: it
records the gap and backs off. Restarting over that would throw away the backoff
and thrash for as long as the inverter was away. The distinction is between a
loop that is failing and a loop that is not running.

Twenty minutes is the most data a stall can cost. It is set well above the
five-minute maximum backoff so that a healthy but struggling service can never
reach it.

### Deploying without interrupting collection

The pages are read from disk on every request, so **changing HTML, CSS or
JavaScript needs no restart at all** — copy the files into place and reload the
browser. Only a change to the Python requires restarting the service.

When a release adds or changes a dependency — as `pyserial` did in 0.6.9 for
serial installations — copying `src/` is not enough. The deploy must also copy
`pyproject.toml` and `uv.lock` and run `uv sync`, or the new package is missing
at runtime and the service fails to start.

That distinction is worth respecting, because a restart is not free. The dongle
takes time to start serving a new client after the previous one drops, measured
at around 55 seconds on the reference hardware before the first sample comes
back. Restarting to deploy a stylesheet costs a minute of readings for nothing.

If you need Python deploys that never interrupt collection, the collector and
the web server can be split into two units sharing the database — nothing in
`api/` is allowed to touch the inverter, and SQLite in WAL mode handles two
processes. The one thing that needs care is the yield endpoint, which reaches
into the running collector to hand the dongle over for a firmware update; across
processes that needs a control channel rather than a method call.

### Upgrading a database written before readings had a device

Every stored reading now records which inverter produced it, so that a parallel
stack does not need one database per unit. A database created before that
change is keyed on time alone and cannot be opened by the current service: it
says so and stops, rather than starting and failing on the first write.

Stop the service, take a copy of the database, and run the migration:

```bash
sudo systemctl stop arraysense
sudo cp /var/lib/arraysense/arraysense.db /var/lib/arraysense/arraysense.db.bak
sudo -u arraysense /opt/arraysense/.venv/bin/python -m arraysense \
    --config /etc/arraysense/config.toml --migrate
sudo systemctl start arraysense
```

It stamps every existing row with `inverter_serial` from your config, prints how
many rows it moved per table, and is safe to run twice — a database that has
already been migrated is left alone. Everything happens in one transaction, so a
power cut in the middle leaves the database exactly as it was and the migration
can simply be run again.

Two things to plan for. It is quick — a reference installation of 796,156 rows
took 3.6 seconds, and that is the whole downtime. But it rewrites every table,
and the file roughly doubles in size, because the space the old tables used is
left free inside it rather than returned to the disk. On that same installation
134 MB became 277 MB. The space is reused by later writes, and
`sqlite3 arraysense.db VACUUM` reclaims it now if you would rather have it back.

Check the free space against the size SQLite sees, not against `du`. On a
compressing filesystem such as ZFS or Btrfs the two differ a lot — the 134 MB
database above shows as 27 MB to `du`, which would make the headroom look five
times better than it is.

## Next

Configuration options are described in [configuration.md](configuration.md).
Problems are covered in [troubleshooting.md](troubleshooting.md).
