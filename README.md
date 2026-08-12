# Solar ArraySense

Open-source solar and battery monitoring for EG4 and LuxPower inverters — including
**per-module battery telemetry**, read over your local network.

> **Status: running unattended.** Polling, storage, the HTTP API and the dashboard
> run against real hardware: the reference installation has collected 272,789
> inverter readings over 34.5 continuous days — 99.76 % of that window covered —
> on top of 668 days of hourly history. The measured record is one model, the EG4
> 18kPV; the 12kPV, FlexBOSS21 and FlexBOSS18 are supported on upstream evidence
> rather than measurement. There is no authentication yet
> ([#34](https://github.com/sjordan0228/arraysense/issues/34)), so keep the
> service on your own network rather than exposing it to the internet.

## Why

Existing options make you choose between a local tool with thin battery data and a
vendor cloud that has the detail but wants your data and a subscription. This reads
the detail locally.

The interesting part is that per-battery data is already sitting in the inverter, at
undocumented input registers 5002–5121. Per module you can get state of charge, state
of health, cycle count, current and voltage, the highest and lowest cell voltage
**with the cell index for each**, and the same for cell temperature. That last part
matters most: it means cell delta — the earliest warning of a weak cell — is
computable without any extra hardware, down to which cell in which module.

## What it records

175 columns per poll: 91 inverter measurements and 21 for each of four battery
modules. Alongside the obvious power and voltage figures that means per-string
current, both heatsink temperatures, split-phase backup output leg by leg, the
charge and discharge limits the BMS is currently imposing, and remaining capacity in
amp-hours rather than only as a percentage.

Energy comes from the inverter's own kWh counters, daily and lifetime, rather than
from integrating power. Integration agrees on a clean day and quietly undercounts
after any gap in collection; the counters keep counting through ours.

Two rules govern all of it. A reading the inverter did not report is stored as NULL
and rendered as a gap, never as zero — a battery block empty because the CAN link
dropped must not appear as a pack at 0%. And every metric carries plausible bounds,
so a decode error is caught on the way in rather than recorded as fact.

### Telling a drifting gauge apart from a failing battery

Each pack estimates its charge by counting amp-hours, and that count cannot correct
itself until the pack charges fully. Packs left to drift disagree with each other by
tens of points while sitting within a few tens of millivolts — which means they hold
the same charge and only the counters are wrong.

The dashboard says exactly that, rather than implying the batteries are diverging. It
finds full-charge events in stored history, tracks how long it has been since one,
and marks per-pack percentages as estimates once they are too stale to act on. The
case where the packs genuinely disagree on *voltage* is reported separately and more
loudly, because parallel packs are forced to the same voltage and a spread there
means a cable, a lug or a busbar rather than arithmetic.

## Hardware

Developed against an EG4 18kPV with four EG4 PowerPro WallMount modules, over the
LuxPower WiFi dongle. EG4 is the US rebrand of LuxPower, so LuxPower units speaking
the same dongle protocol should work.

**No additional hardware is required.** An optional RS485 tap on the battery
daisy-chain adds individual cell voltages and balancing state later, but nothing in
the core depends on it.

Two constraints worth knowing before you plan a deployment:

- The WiFi dongle accepts **exactly one TCP client**. Anything else already polling it
  — a vendor app, another monitoring tool — will fight with this one. Run only one.
- **Port 8000 is being removed** in newer dongle firmware and does not exist on
  Ethernet dongles. The transport is pluggable for that reason, with wired RS485 as
  the durable path.

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
| `src/arraysense/collector/` | inverter transports and the polling service |
| `src/arraysense/store/` | tiered SQLite storage and rollups |
| `src/arraysense/api/` | HTTP API |
| `src/arraysense/web/` | the single-page dashboard |

`metrics.py` is the single source of truth; schema, validation and the API all derive
from it, so adding a metric is a one-line change there.

## Documentation

- [docs/installation.md](docs/installation.md) — hardware requirements and setup
- [docs/raspberry-pi.md](docs/raspberry-pi.md) — running it on a Pi: the SD card,
  the USB SSD, and reaching the inverter over RS485
- [docs/configuration.md](docs/configuration.md) — configuration reference
- [docs/api.md](docs/api.md) — HTTP API reference
- [docs/troubleshooting.md](docs/troubleshooting.md) — dongle connection problems,
  missing battery data, and other things people run into
- [docs/migrating-from-solar-assistant.md](docs/migrating-from-solar-assistant.md) —
  bringing your SolarAssistant history across, so switching does not cost you it

## Licence

[AGPL-3.0-or-later](LICENSE). Chosen deliberately: anyone may run and modify this,
but a modified version offered as a service must publish its source, which prevents
this being turned into a closed product.

## Vendored

[uPlot](https://github.com/leeoniya/uPlot) (MIT) is committed under
`src/arraysense/web/` rather than loaded from a CDN. The service runs on home
networks that may have no route to the internet, and a chart library that
silently fails to load leaves a blank panel with no clue why. Its licence ships
alongside it as `uPlot.LICENSE`.
