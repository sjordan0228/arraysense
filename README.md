# Solar ArraySense

Open-source solar and battery monitoring for EG4 and LuxPower inverters — including
**per-module battery telemetry**, read over your local network. No cloud account, no
subscription, and your data never leaves the house.

![The dashboard, showing live production, house load, battery and grid](docs/images/dashboard-now.jpg)

> **Status: 1.0, running unattended on real hardware.** The reference installation —
> an EG4 18kPV with four PowerPro WallMount modules — has collected **289,310
> full-cadence readings across 36 days**, on top of **672 days of hourly history**,
> polling over RS485 every eleven seconds. Every screenshot on this page is that
> installation, live, with hardware serials replaced by the documented placeholders.
>
> Authentication is optional and off until you set a password on the Settings
> page ([#34](https://github.com/sjordan0228/arraysense/issues/34)). When it is
> on it protects the write endpoints while the dashboard keeps reading without
> one, so a wall display never needs a login. It travels as plain HTTP on a home
> network, so it stops other devices changing things by accident or mischief,
> not anyone who can watch the traffic — keep it on your own network rather
> than exposing it to the internet.

## Supported hardware

One driver family today, the LuxPower register set that EG4 rebrands. Support is
listed by the *evidence behind it*, because a register that means one thing on a
hybrid can mean something else on an off-grid unit, and a wrong reading is worse
than a missing one.

| Model | Status |
| --- | --- |
| **EG4 18kPV** | **Measured.** The reference installation. Everything here was verified against it. |
| **EG4 12kPV** | Confirmed upstream — shares device type code 2092 with the 18kPV. |
| **EG4 FlexBOSS21** | Confirmed upstream — device type code 10284. |
| **EG4 FlexBOSS18** | Confirmed upstream — device type code 10284. |
| **EG4 6000XP** | ⚠️ Partial, and not recommended yet. An off-grid family: several registers this driver reads mean something different there, and the PV string count is unconfirmed. Readings may be *wrong* rather than missing — see [#122](https://github.com/sjordan0228/arraysense/issues/122). |

EG4 is the US rebrand of LuxPower, so LuxPower units speaking the same protocol
should work.

### How it connects — read this before buying a dongle

Two transports, and the dongle one is narrower than "WiFi dongle" suggests.

| Connection | Supported | Notes |
| --- | --- | --- |
| **Wired RS485 / Modbus** | ✅ **Recommended** | A USB-to-RS485 adapter on the inverter's 485A/485B terminals. What the reference installation runs. No firmware can take it away, and nothing competes for it. |
| **LuxPower/EG4 WiFi dongle, TCP port 8000** | ✅ The only dongle supported | The proprietary LuxPower protocol, authenticated with the dongle's serial. |
| **The same WiFi dongle on newer firmware** | ❌ | Some firmware has **no port 8000 at all**, and there is **no way to re-enable it**. A dongle that worked can stop after an update. |
| **Ethernet dongles** | ❌ | They never exposed port 8000. Not usable, at any firmware version. |
| Any other vendor's dongle | ❌ | Different protocol entirely. |

Two more things about the WiFi dongle specifically:

- It accepts **exactly one TCP client**. Anything else already polling it — the EG4
  app, Solar Assistant, a Home Assistant integration — will fight with this one and
  both will lose readings. Run only one.
- Firmware updates go through the vendor's app, which needs that same single slot,
  so the service has a yield mode to hand it back temporarily.

This is precisely why the transport is pluggable and why **RS485 is the path this
project treats as durable**. If you are choosing today, choose RS485.

Want to try it without an inverter? A simulated driver ships in the box — set
`driver = "fake"` and everything below runs against generated data.

### Your inverter not listed?

**[Open a hardware request →](https://github.com/sjordan0228/arraysense/issues/new?template=hardware-request.yml)**

Other families are absolutely intended — the transport and the driver registry were
built to make adding one a directory plus a line of registration, not a rewrite. What
gates it is evidence rather than effort: nothing ships as supported until its
registers have been confirmed against real hardware. If you have a unit and can help
test, say so in the request; that is the thing that actually unblocks a family.

## Why this exists

Existing options make you choose between a local tool with thin battery data and a
vendor cloud that has the detail but wants your data and a subscription. This reads
the detail locally.

The interesting part is that per-battery data is already sitting in the inverter, at
undocumented input registers 5002–5121. Per module you get state of charge, state of
health, cycle count, current and voltage, the highest and lowest cell voltage **with
the cell index for each**, and the same for cell temperature. That last part matters
most: cell delta — the earliest warning of a weak cell — becomes computable with no
extra hardware, down to which cell in which module.

![Per-pack temperature bands, one band per battery module](docs/images/graphs-packs.jpg)

## What it records

**175 columns per poll**: 91 inverter measurements and 21 for each of four battery
modules, plus six site readings from the weather service. Alongside the obvious power
and voltage figures that means per-string current, both heatsink temperatures,
split-phase backup output leg by leg, the charge and discharge limits the BMS is
currently imposing, and remaining capacity in amp-hours rather than only as a
percentage.

Energy comes from the inverter's own kWh counters, daily and lifetime, rather than
from integrating power. Integration agrees on a clean day and quietly undercounts
after any gap in collection; the counters keep counting through ours.

Two rules govern all of it. **A reading the inverter did not report is stored as NULL
and rendered as a gap, never as zero** — a battery block empty because the CAN link
dropped must not appear as a pack at 0%. And every metric carries plausible bounds,
so a decode error is caught on the way in rather than recorded as fact.

### Where the energy actually went

![Sankey diagram of a day's energy from solar, grid and battery through to the house](docs/images/energy-flow.jpg)

### What it should have produced, against what it did

Expected output is modelled from sun position, plane-of-array irradiance and cell
temperature — including wind, which is worth up to 12% of output on a hot still day —
then scored against what the inverter recorded, per string, so an underperformer can
be localised rather than merely suspected. Curtailment is separated out, because an
inverter protecting a full battery is not a fault.

![Performance ratio and per-string scoring for a day](docs/images/efficiency.jpg)

### What it cost, and what solar saved

Rate bands with seasons, monthly adjustment factors, an estimated bill, and the
counterfactual: what the same use would have cost from the grid alone.

![Monthly cost, estimated bill and the savings attributable to solar](docs/images/costs.jpg)

### Thirty days, and thirteen months

![Daily energy for the last thirty days with a day-by-day table](docs/images/history.jpg)

### Telling a drifting gauge apart from a failing battery

Each pack estimates its charge by counting amp-hours, and that count cannot correct
itself until the pack charges fully. Packs left to drift disagree with each other by
tens of points while sitting within a few tens of millivolts — which means they hold
the same charge and only the counters are wrong.

The dashboard says exactly that, rather than implying the batteries are diverging. It
finds full-charge events in stored history, tracks how long it has been since one, and
marks per-pack percentages as estimates once they are too stale to act on. The case
where packs genuinely disagree on *voltage* is reported separately and more loudly,
because parallel packs are forced to the same voltage and a spread there means a
cable, a lug or a busbar rather than arithmetic.

## Install

One line, on a Linux host with systemd — a Raspberry Pi is the usual target:

```bash
curl -fsSL https://raw.githubusercontent.com/sjordan0228/arraysense/main/install.py | sudo python3 -
```

That is the whole install. The script is written to be read before it is trusted:
it is **standard library only** and parses on the Python 3.8 a distribution ships,
it **fetches exactly two things** — [uv](https://docs.astral.sh/uv/)'s installer and
this repository — and it **prints everything it intends to do before doing any of
it**. It asks its questions on the terminal rather than stdin, so piping into `sudo`
cannot swallow your answers, and it is safe to re-run over a partial install.

It checks the host first and refuses with a reason rather than failing halfway:
architecture, systemd, and at least 2 GB free. Then it installs uv, has it fetch
Python 3.12, clones to `/opt/arraysense`, creates the service account, and installs
the systemd service plus the daily backup timer.

For an unattended install:

```bash
sudo python3 install.py --yes --port 8090
```

`--repo URL` installs from a fork and `--ref NAME` pins a branch, tag or commit.

### Then set it up in the browser

The installer prints the address. Open it and you get a first-run wizard rather than
a dashboard — pick the manufacturer, the model and how it is wired, and it starts
collecting. Everything is changeable afterwards on the settings page.

![The first-run setup wizard](docs/images/setup-wizard.jpg)

Full instructions are in [docs/installation.md](docs/installation.md), including the
Raspberry Pi path in [docs/raspberry-pi.md](docs/raspberry-pi.md) — which covers the
RS485 wiring, the USB adapter, and the two systemd settings a serial install needs.

### How much disk

About **5.3 MB a day** at the default eleven-second cadence. Retention is available
and **off by default** — deleting readings cannot be undone, so it stays your
decision. Turn it on and full-cadence data is kept 30 days and minute data a year,
with hourly kept for ever; nothing is deleted without a current backup and without
the coarser tiers already holding every bucket being dropped. See
[docs/configuration.md](docs/configuration.md#data-retention).

## Documentation

- [docs/installation.md](docs/installation.md) — hardware requirements and setup
- [docs/raspberry-pi.md](docs/raspberry-pi.md) — running it on a Pi: the SD card, the
  USB SSD, and reaching the inverter over RS485
- [docs/configuration.md](docs/configuration.md) — configuration and retention reference
- [docs/api.md](docs/api.md) — HTTP API reference
- [docs/troubleshooting.md](docs/troubleshooting.md) — dongle connection problems,
  missing battery data, and other things people run into
- [docs/migrating-from-solar-assistant.md](docs/migrating-from-solar-assistant.md) —
  bringing your SolarAssistant history across, so switching does not cost you it

## Development

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed automatically.

```bash
uv sync                 # install everything
uv run pytest           # tests
uv run ruff check .     # lint
uv run ruff format .    # format
uv run mypy             # type check
```

All four checks must pass before a change is complete.

### Layout

| Path | Responsibility |
| --- | --- |
| `src/arraysense/metrics.py` | metric registry — names, units, integer scaling, plausible bounds |
| `src/arraysense/models.py` | wire-independent sample model |
| `src/arraysense/calibration.py` | state-of-charge drift detection from stored history |
| `src/arraysense/solar.py` | sun position, plane-of-array irradiance, expected output |
| `src/arraysense/drivers/` | inverter transports — one directory per family |
| `src/arraysense/collector/` | poll loop, backoff, gap recording, and weather |
| `src/arraysense/store/` | tiered SQLite storage, rollups and retention |
| `src/arraysense/api/` | HTTP API |
| `src/arraysense/web/` | the dashboard |

`metrics.py` is the single source of truth; schema, validation and the API all derive
from it, so adding a metric is a one-line change there. Adding an inverter family is a
directory under `drivers/` plus one line of registration — the collector does not
change.

## Licence

[AGPL-3.0-or-later](LICENSE). Chosen deliberately: anyone may run and modify this, but
a modified version offered as a service must publish its source, which prevents this
being turned into a closed product.

## Vendored

[uPlot](https://github.com/leeoniya/uPlot) (MIT) is committed under
`src/arraysense/web/` rather than loaded from a CDN. The service runs on home networks
that may have no route to the internet, and a chart library that silently fails to
load leaves a blank panel with no clue why. Its licence ships alongside it as
`uPlot.LICENSE`.
