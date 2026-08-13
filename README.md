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
> There is no authentication yet
> ([#34](https://github.com/sjordan0228/arraysense/issues/34)), so keep it on your own
> network rather than exposing it to the internet.

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
should work. Both a **WiFi dongle** and **wired RS485/Modbus** are supported; the
reference installation runs RS485, which is the durable path.

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

## Getting started

Setup is a form in the browser — pick the manufacturer, the model and how it is
wired, and it starts collecting. Everything is changeable afterwards on the settings
page.

![The first-run setup wizard](docs/images/setup-wizard.jpg)

Full instructions are in [docs/installation.md](docs/installation.md), including the
Raspberry Pi path in [docs/raspberry-pi.md](docs/raspberry-pi.md).

Two constraints worth knowing before you plan a deployment:

- The WiFi dongle accepts **exactly one TCP client**. Anything else already polling it
  — a vendor app, another monitoring tool — will fight with this one. Run only one.
- **Port 8000 is being removed** in newer dongle firmware and does not exist on
  Ethernet dongles. That is why the transport is pluggable, and why RS485 is the
  path this project treats as durable.

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
