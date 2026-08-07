# Migrating from SolarAssistant

SolarAssistant keeps every reading it has ever taken. Switching monitors should
not cost you that, so `tools/import_solar_assistant.py` reads its database
export and fills ArraySense's history from it — twenty-two months, in the case
this was built against, at the same eleven-second cadence SolarAssistant
recorded.

Read this page before starting. The import is designed to be safe to re-run, but
the export it reads cannot be recreated once SolarAssistant is gone.

## What comes across, and what does not

Twenty-one measurements map straight onto ArraySense metrics: all three solar
strings' power, voltage and current, the array total, house load, essential
load, grid power, voltage and frequency, the AC output voltage, battery power,
voltage, current and state of charge, the inverter temperature, and generator
power.

Energy comes across too, though not directly. **SolarAssistant records no
lifetime kWh counters** — it stores instantaneous power and an hourly mean, and
nothing else. ArraySense computes every energy figure as the difference between
two counter readings, because that stays correct across a gap in collection in a
way integrating power does not. So the import reconstructs the counters from
SolarAssistant's hourly means, anchored to the first real reading your collector
takes, and every difference across the join is exact. See "How energy is
reconstructed" below.

Three things are deliberately left behind:

**The battery temperature.** SolarAssistant's is an ambient sensor, not the
cells. On the reference hardware it read 21.9 °C at a moment the BMS reported
its hottest cell at 39.0. ArraySense fills `battery_temperature_c` from the
BMS's own hottest-cell figure, so importing a different sensor into the same
column would put a seventeen-degree step in the chart at the changeover and
present it as a temperature change.

**Weather and forecast.** Outside temperature, cloud cover and predicted PV are
real series, but they come from a weather service rather than the inverter, and
ArraySense has nowhere to put them yet. They are named in the script's `DROPPED`
table so that adding them later is a small change rather than a rediscovery.

**Per-module battery data.** SolarAssistant never had any — it records the stack
as one aggregate. ArraySense keys modules by serial and will start recording
them the moment the collector runs, so per-module history begins at the
changeover and there is nothing to import.

## Before you start

You need SSH access to the machine running SolarAssistant, and enough space on
it for the export. The reference export was 596 MB compressed for 141 million
points across twenty-two months; budget roughly 30 MB per month.

Check how far back your data goes, and confirm nothing has already been aged
out:

```bash
ssh solar-assistant@<host> 'influx -database solar_assistant -execute "SHOW RETENTION POLICIES"'
```

A duration of `0s` means infinite, and everything SolarAssistant ever recorded
is still there.

## 1. Export the history

```bash
ssh solar-assistant@<host> 'sudo influx_inspect export \
  -database solar_assistant \
  -datadir /var/lib/influxdb/data -waldir /var/lib/influxdb/wal \
  -compress -out /tmp/sa-history.lp.gz'
scp solar-assistant@<host>:/tmp/sa-history.lp.gz ./
```

`sudo` is not optional: the data directory is not readable by the
`solar-assistant` user, and without it the export fails with a permission error
after appearing to start normally.

### Check the export is complete

Worth doing, and it takes a minute. Count the points InfluxDB holds for a few
series and compare them against the export:

```bash
ssh solar-assistant@<host> 'influx -database solar_assistant \
  -execute "SELECT count(combined) FROM \"PV power\""'

gzcat sa-history.lp.gz | grep -c '^PV\\ power combined='
```

They should agree. The hourly series are the exception and will show *more*
points in the export than InfluxDB reports — SolarAssistant rewrites the
current hour as it fills and the raw file keeps every version, where a query
shows only the last. The import deduplicates them, last write wins.

## 2. Stop SolarAssistant

The dongle accepts exactly one TCP client, so nothing else can read the inverter
while SolarAssistant holds it.

```bash
ssh solar-assistant@<host> 'sudo systemctl stop influx-bridge'
```

Stop `influx-bridge`, not InfluxDB itself. The bridge is the part that talks to
the inverter; the database has to stay up for the final export in step 3.

## 3. Take the final slice

SolarAssistant kept recording between your first export and the moment you
stopped it. Export that gap too, overlapping by a few minutes so nothing falls
between the two files:

```bash
ssh solar-assistant@<host> 'sudo influx_inspect export \
  -database solar_assistant \
  -datadir /var/lib/influxdb/data -waldir /var/lib/influxdb/wal \
  -start <the last timestamp in the first export, minus five minutes> \
  -compress -out /tmp/sa-delta.lp.gz'
```

The overlap is harmless. Every write is keyed by instant, so importing the same
point twice stores it once.

## 4. Start the collector

Configure ArraySense with your dongle's address and serials and start it. It
needs to record at least one sample before the import can run, because that
sample is what the reconstructed counters are anchored to.

## 5. Import

```bash
uv run python tools/import_solar_assistant.py \
  sa-history.lp.gz sa-delta.lp.gz --db /var/lib/arraysense/arraysense.db
```

Pass both files; they are read in order and may overlap. The import prints what
it anchored to, how many points it read, and how many rows it wrote to each
tier.

It fills the tiers to their own retention: full cadence for the last thirty
days, per minute for the last year, hourly for everything older. `--raw-days`
and `--minute-days` override those if your retention differs.

## How energy is reconstructed

This is the one place the import derives a number rather than copying one, so it
is worth understanding.

Each of SolarAssistant's hourly buckets is a mean power in watts. Divided by a
thousand it is that hour's energy in kWh, and that figure is a real measurement
— SolarAssistant computed it from the samples it took.

What does not exist is the running total. So the import walks *backwards* from
the counter values your inverter reports at the moment of changeover,
subtracting each hour's energy as it goes. The last reconstructed hour therefore
lands exactly on the first real reading, and any difference taken across the
join is correct.

Two consequences worth stating plainly:

- The absolute counter values before the changeover are a reconstruction. Only
  the differences are measurements — and differences are all ArraySense ever
  computes, so nothing on any page rests on the absolute figures.
- The reconstructed energy inherits SolarAssistant's own collection gaps. An
  hour it missed is an hour missing from its hourly mean, and no amount of
  arithmetic here can recover it. This is exactly the weakness that motivated
  reading the inverter's counters instead, and it stops applying the moment your
  collector takes over.

## Checking it worked

Pick a day well inside the imported range and compare what ArraySense reports
against what SolarAssistant recorded for the same day:

```bash
# SolarAssistant's own figure
ssh solar-assistant@<host> 'influx -database solar_assistant -execute \
  "SELECT sum(combined)/1000 FROM \"PV power hourly\" \
   WHERE time >= '"'"'2026-08-06T05:00:00Z'"'"' AND time < '"'"'2026-08-07T05:00:00Z'"'"'"'

# ArraySense's
curl -s 'http://<arraysense>/api/energy?start=2026-08-06T05:00:00Z&end=2026-08-07T05:00:00Z&period=day&tz=America/Chicago'
```

On the reference installation these agree to within a tenth of a percent, which
is the rounding: energy is stored to 0.1 kWh.

Use your own timezone in both, and note that the boundaries are local midnight
expressed in UTC — the offset differs by half the year.

## If something looks wrong

**A whole class of readings is missing.** Almost certainly a parsing problem
rather than a mapping one. InfluxDB's line protocol types an integer by
suffixing it — `combined=4917i` — and every power series and the state of charge
are integers while the voltages and currents are floats. A parser that skips
what it cannot read therefore drops exactly the important half and imports the
rest, which looks like a working import.

**A temperature is absurd.** SolarAssistant stores the inverter temperature in
Fahrenheit. The import converts it; anything reading 158 °C has skipped that.

**Daily figures are a little low but monthly ones are right.** Check where the
reconstructed counters are stamped. A counter written at the end of its hour
rather than the start shifts every reading into its neighbour, and a day then
totals hours one to twenty-three. It hides well, because the hour it drops is
midnight — solar matches almost exactly while grid import comes out short.

**Nothing appears before a year ago.** Daily views read the minute tier, which
is only kept for a year. Older ranges are answered from the hourly tier
instead; if that is empty, the import did not reach that far back. Check
`--max-years`.
