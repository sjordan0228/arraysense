# Backfilling Emporia circuit history

Your Vue has been measuring the house since the day you installed it. ArraySense
has only been recording circuits since the day you connected the Emporia module
to it, so the Circuits tab offers thirty days and thirteen months and starts with
hours.

Everything in between is still in Emporia's cloud. This brings it across, once.

```bash
uv run python tools/backfill_emporia_history.py \
    --database /var/lib/arraysense/arraysense.db \
    --tokens /var/lib/arraysense/emporia-tokens.json \
    --days 30 --dry-run
```

Drop `--dry-run` to write. The dry run fetches everything and reports what it
would store without touching the database, and running it first is worth the
minute it costs.

Both paths are the ones your installation actually uses. `--database` is the
`database_path` from your `config.toml`; the token file sits beside it. On a
Raspberry Pi installation with the database on a USB SSD, run it as the account
that owns the files:

```bash
sudo -u arraysense /opt/arraysense/.venv/bin/python \
    tools/backfill_emporia_history.py \
    --database /mnt/ssd/arraysense/arraysense.db \
    --tokens /mnt/ssd/arraysense/emporia-tokens.json --days 30
```

It is safe to run against a live installation. Every write is an insert that
yields to whatever is already there, so the collector can keep polling
throughout, and running it twice writes nothing the second time.

## How far back it can reach

`--days 30` is the default and roughly the practical maximum for one pass:
Emporia serves about a month of hourly buckets per request and answers HTTP 400
above that. A longer `--days` is cut into month-sized requests automatically, so
`--days 365` works — it simply makes twelve times as many calls, one per circuit
per month. Whether Emporia still holds hourly detail that far back depends on
your account; the tool reports what it actually received per circuit, and a
circuit added last month returns fewer hours than one installed last year.

## What it writes, and what it will not touch

It fills the hourly tier only, one row per circuit per hour, and **it never
overwrites an hour ArraySense measured itself**. Your own rollup is built from
readings taken on your machine; Emporia's figure is taken on trust. Where both
exist, yours stands.

An hour Emporia has no figure for gets no row at all, rather than a row saying
the circuit drew nothing. A dead outlet and an idle one are different facts and
the Circuits tab draws them differently.

The hour currently in progress is skipped. Emporia will answer for it, but its
figure covers only the minutes so far — stored as a whole hour it would
understate the circuit permanently, since nothing later overwrites it.

## Telling a fetched hour from a watched one

A backfilled hour records one reading covering the full 3,600 seconds, because
that is what an Emporia hourly bucket is: the device's own aggregate of its own
second-by-second record, not a sample of it. A polled hour records however many
readings your `emporia.interval` produced — sixty at the default, covering
around 3,540 seconds of the hour.

That has a visible consequence worth knowing about. The Circuits tab labels a
figure "partial" when any hour behind it was not covered end to end, so hours
ArraySense polled itself carry the label and backfilled hours do not. This is
not backwards: sampling a circuit once a minute genuinely covers less of the
hour than the device's own continuous total does. It does mean the label reads
as a statement about how the hour was measured rather than about how good it is.

## Verifying it landed

The two figures were checked against each other on the reference installation
before this shipped: thirteen hours the collector had already rolled up were
compared with the same hours fetched from the cloud, across all thirty-nine
circuits. 315 of 390 pairs agreed within 2%, and every disagreement was an hour
the collector had only partly watched — the hour it started mid-way, and the
hour still running.

After a run, load the Circuits tab and pick 30d. The header says which tier the
chart is drawn from and how many points it holds.
