# Installation

> **Status.** Measuring a real installation since October 2024. Against the
> reference database on 11 August 2026: 668 days of hourly history, and 34.5 days
> of continuous raw collection carrying 272,789 inverter readings — 99.76 % of
> that window covered. One inverter model has been measured; three more are
> supported on upstream evidence. There is no authentication yet, so run it on a
> network you trust and do not expose it to the internet.

## What you need

### Inverter

Developed and measured against an **EG4 18kPV** with four EG4 PowerPro WallMount
modules. EG4 is the US rebrand of LuxPower, and the 12kPV, FlexBOSS21 and FlexBOSS18
share the same register surface, so the wizard offers them on upstream evidence
rather than on a measured unit. The 6000XP is offered with a warning instead of a
promise: it belongs to the off-grid family, where several registers this driver
reads mean something different, so its readings may be wrong rather than missing.

Per-module battery data needs the batteries in **closed-loop CAN** with the
inverter; out of closed loop the inverter reports only a bank-level summary, and
the per-module fields stay empty. The inverter exposes four battery slots and
rotates modules through them, so a module is identified by serial number, never by
position.

### Connection

| Connection | Notes |
| --- | --- |
| WiFi dongle, TCP port 8000 | The wizard asks for the dongle's address and serial |
| Wired RS485 | A USB-to-RS485 adapter to the 485A/485B terminals; the wizard asks for the device path |

The dongle accepts **exactly one TCP client**. A second client is closed
immediately, and two clients repeatedly evicting each other produce CRC errors and
missing data on both — so nothing else may poll the inverter while this runs,
including the EG4 app, Solar Assistant and Home Assistant integrations. Newer
dongle firmware removes port 8000 and Ethernet dongles never had it, which is why
wired RS485 exists as a transport at all.

### Host

Any Linux host on `aarch64` or `x86-64` with systemd and git — a Raspberry Pi, an
LXC container, a small VM. The installer needs root, needs the machine to run
Python 3.8 or newer (uv installs the service's own Python 3.12), and refuses a host
with under 2 GB free, because uv fetches a full Python and the database grows
continuously.

Memory and CPU are not the constraint; sustained writes are. On a Raspberry Pi, put
the database on a USB SSD rather than the SD card — a card carrying a continuous
write load wears out. Everything Pi-specific is in
[raspberry-pi.md](raspberry-pi.md).

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/sjordan0228/arraysense/main/install.py | sudo python3 -
```

That is the whole install. The script is written to be piped into root: it is
self-contained (apart from uv's installer it fetches nothing but the repository it
clones), prints everything it is about to do before it does any of it, and reads
its answers from the terminal rather than stdin, so the pipe cannot swallow them.
It is safe to re-run over a partial one. Once `/opt/arraysense` exists, a run is
refused rather than half-installed over, and the message points at
`arraysense upgrade`.

For an install nobody is watching:

```bash
sudo python3 install.py --yes --port 8090
```

`--yes` answers the confirmation, `--port N` chooses the port, `--repo URL`
installs from a fork, and `--ref NAME` pins a branch, tag or commit instead of the
project's default branch. A bare run defaults to the ordinary choices.

## What it will ask

Two things, and only when it needs them:

- **The port.** If 80 is free it is used, and the service is granted
  `CAP_NET_BIND_SERVICE` so an unprivileged process can bind it. If 80 is taken —
  usually by a web server the owner cares about — you are asked, with 8080 as the
  default. It is asked rather than silently moved because the address ends up
  printed for you to open.
- **Confirmation.** The plan is shown first — the `arraysense` system user, the
  clone into `/opt/arraysense`, the config and data directories, the systemd
  service, the port, and the `arraysense` command installed on the PATH. Nothing
  is touched until you say yes.

It writes **no configuration file**, and that absence is the point: an existing
config would skip the setup wizard, so the installer deliberately leaves none
behind.

## Where to go next: the wizard

The installer prints two addresses — `http://<hostname>.local:<port>` and
`http://<ip>:<port>` — because mDNS does not work on every network. Open either in
a browser. With no config file present, the service is running in setup mode, and
the page is a first-run wizard rather than a dashboard.

The wizard asks which manufacturer and model you own, which connection (dongle or
RS485) and its details, whether the batteries are in closed loop, and the
inverter's ten-character serial. A **Detect** button probes the connection and
reads back the serial the inverter answers with — confirming the dongle one you
typed, or discovering it on the serial bus. Read the serial off the inverter
itself rather than out of another tool's logs: other tools have been observed
reporting a different value, and a mismatch makes every read fail.

On apply, the wizard writes `/etc/arraysense/config.toml` (mode 0600, because it
identifies your hardware) and restarts the service into the dashboard. The page
then opens the live dashboard once the collector is up. `arraysense status` shows
the same thing from the terminal.

The service has no authentication yet. It binds every interface so the dashboard is
reachable from other machines on the LAN, and anything on that network can also
write its settings — so keep it on a network you trust and do not expose it to the
internet. A reverse proxy in front of it is the answer if you need it further away.

## Managing it

The installer leaves an `arraysense` command on the PATH:

| Command | What it does |
| --- | --- |
| `arraysense status` | the installed version; whether the collector is running, connected and fresh; which driver is in use and what it declares, including whether energy is counted or estimated; the database's size and date range |
| `arraysense logs` | the service's journal; flags pass through to `journalctl`, so `-f` follows |
| `arraysense restart` | restarts the service and waits — up to 90 seconds — until the collector is answering, rather than trusting that `systemctl start` succeeded |
| `arraysense version` | the version and the commit it is running |
| `arraysense upgrade` | covered below |
| `arraysense uninstall` | covered below |

The database is owned by the service user, so the date range needs
`sudo arraysense status`; run as an ordinary user the size is still shown but
the range reports that it could not be read.

The service also looks after itself. The shipped unit sets `Restart=always`, and
deliberately not `on-failure`: the watchdog below ends a stalled loop with
SIGTERM, which systemd would otherwise count as a clean exit and never restart
over. systemd restarts a process that exits; the watchdog restarts the whole
process if the poll loop produces neither a reading nor an error for twenty
minutes — the failure systemd cannot see, because the process is alive and
serving pages while collecting nothing. A stalled loop is ended with SIGTERM, so
the dongle's single TCP slot is released on the way down. An inverter that is
simply not answering does not trigger the watchdog: those polls fail, which is
the loop working, and restarting over that would throw away its backoff.

## Upgrading

`arraysense upgrade` fetches, shows, and applies. It lists the commits between what
you run and the project's `main`, and the incoming changelog entry — the place a
release announces a schema change — before anything is applied. It confirms,
fast-forwards the install, reinstalls dependencies, restarts, and waits for a live
collector. If the collector does not come back within 90 seconds, it rolls back to
the previous commit, reinstalls and restarts that, and says plainly what is now
running. It refuses an install that has been edited by hand, naming the modified
files, because an upgrade would silently overwrite them.

It never touches `/etc/arraysense/config.toml` and never touches the database.
That limit is stated rather than implied: the rollback rescues a broken upgrade,
not a broken database, and a release that ever needs a destructive migration will
come with its own instructions.

## Uninstalling

`arraysense uninstall` removes the service, the code under `/opt/arraysense`, and
the `arraysense` command. The database and `/etc/arraysense` are kept, so a
reinstall resumes where collection stopped. `--purge` deletes the database as
well, and it is confirmed twice before anything is destroyed — a database no
reinstall can bring back is exactly what a single accidental enter should not
throw away.

## The hand-editing fallback

The wizard is the supported path, and the whole setup can also be configured by
hand. `config.example.toml` is the reference for that: it documents every setting.
Copying it into `/etc/arraysense/config.toml` is what *skips* the wizard, so do it
only when you mean to configure by hand. Copy it, fill in the inverter serial and
the connection's own details — the dongle address and serial, or the serial
device path with `transport = "modbus_serial"` — keep it mode 600, and run the
service under the `arraysense` user. The unit in
`packaging/arraysense.service` shows the sandbox and the paths; the details of a
hand setup — the user, the clone, `uv sync`, the drop-ins a serial device or an
SSD needs, serving on port 80 — are spelled out in
[raspberry-pi.md](raspberry-pi.md).

## Next

[configuration.md](configuration.md) documents the settings,
[troubleshooting.md](troubleshooting.md) covers the problems people hit, and
[raspberry-pi.md](raspberry-pi.md) carries the Pi-specific detail.
