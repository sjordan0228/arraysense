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
| WiFi dongle, TCP port 8000 | Primary target | Requires the dongle serial and inverter serial |
| Wired RS485 | Planned | Dongle port pins 14 (B) and 15 (A) |

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

On a Raspberry Pi, point `database_path` at a USB SSD rather than the SD card.
Continuous writes wear cards out, and at the default poll interval this writes about
9 MB a day.

### Serving on port 80

The default is 8080 because ports below 1024 are privileged and this service does not
run as root. To use port 80, add `--port 80` to `ExecStart` and uncomment
`AmbientCapabilities=CAP_NET_BIND_SERVICE` in the unit. That grants the one binding
and nothing else — running the whole service as root to get a shorter URL is the
wrong trade.

## Next

Configuration options are described in [configuration.md](configuration.md).
Problems are covered in [troubleshooting.md](troubleshooting.md).
