# Changelog

What changed, and why it mattered. Entries are written for somebody deciding
whether to upgrade, so a fix says what was wrong rather than what was touched.

Versions follow [semantic versioning](https://semver.org). Until 1.0 the schema
may change between minor versions, and any release that needs a database
migration says so at the top of its entry.

## 0.5.1 — 7 August 2026

### Fixed

- **A money figure is never again presented as whole when it is not**
  ([#23](https://github.com/sjordan0228/arraysense/issues/23)). A gap in
  collection that crossed a rate-band boundary used to price silently short —
  reproduced at cost 9% low and the savings figure following it — and a
  counter that went quiet while polls continued dashed the whole month, losing
  even the days that were measured. The lifetime counters bracket every such
  hole, so the energy that could not be placed in a band is now counted
  exactly, carried to the pages, and said out loud: the Costs cards and the
  History money cells show the measured figure with a label naming the
  kilowatt-hours it is missing, and the estimated bill both corrects its
  projection by that energy and says it did. A gap that crosses no band
  boundary costs nothing and is deliberately not labelled — the counters span
  it exactly. Dashes remain only where nothing attributable was measured, and
  a dash beside counted-but-unplaceable energy now says so. The energy
  columns are untouched: a gap inside a day never cost them anything, and a
  day an outage crossed into already wore its partial badge.

## 0.5.0 — 7 August 2026

**First beta.** The reference installation has been running on this exclusively
since SolarAssistant was switched off, with its full history imported.

### Upgrading

**This release changes the database schema and needs a migration.** Every stored
reading now carries the serial of the inverter it came from, which cannot be
added in place because SQLite will not alter a primary key.

```bash
sudo systemctl stop arraysense
sudo cp /var/lib/arraysense/arraysense.db /var/lib/arraysense/arraysense.db.backup
sudo -u arraysense arraysense --config /etc/arraysense/config.toml --migrate
sudo systemctl start arraysense
```

It is a separate command rather than something startup does by itself, because
it rewrites every table in a database that may hold years of history and the
person running it should be the one who decided to. Running it twice is
harmless. The service refuses to start on a database that still needs it rather
than rewriting a year of history on a restart nobody was watching.

Measured on the reference installation — 796,156 rows — it took 3.6 seconds, and
that is the whole downtime. The file roughly doubles in size while the old pages
sit free inside it; `VACUUM` reclaims them. Check free space against the size
SQLite reports rather than `du`, which on a compressing filesystem can read five
times smaller.

### Added

- **Every reading carries a device identity.** The schema can hold several
  inverters, rollups group by device so two units never average into one row,
  and the read endpoints take an optional `device` that defaults to the
  configured one. Nothing a single-inverter owner sees changes. EG4 18kPVs stack
  up to ten units, and the migration only gets more expensive with time.
- **The dashboard says what the system is doing** — on grid, solar and battery,
  battery discharging — judged once on the server and printed by the page, with
  the readings it turned on available on hover. The battery figure names its own
  state, and Home and Grid each show the day's total beside the live number.
- **A stale-data warning on every page** when the newest reading is more than
  fifteen minutes old. Yield mode is not stale, and an unreachable inverter is
  not a stopped collector — a loop recording gaps and retrying is a loop working.
- **Costs, History and Graphs pages**, a tariff with seasons and time-of-use
  bands, a settings page that renders itself from the registry, and an operating
  mode indicator.
- **A SolarAssistant importer** under `tools/`, so switching does not cost you
  your history.
- **A watchdog** that exits when the poll loop stops, so the supervisor restarts
  it, and a `misroutes` count on `/api/status`.
- **A reference for what the inverter library exposes** against what is
  collected, at `docs/pylxpweb-inventory.md`.

### Fixed

- **Collection no longer stops on a busy database.** Storage failures were
  outside the handler that recovers from inverter problems, so a database held
  by another writer past the busy timeout killed the poll loop while the web
  server went on serving stale pages. Backoff also overflowed after about 85
  hours of an unreachable inverter, killing the loop at the moment it had been
  working correctly throughout.
- **Crossed replies are retried on every path.** The dongle serves its vendor's
  cloud on the same socket and the answers cross. Only one of the four ways the
  library reports that was recognised, and the energy read — which carries about
  half of them — was not retried at all.
- **Money is no longer summed from rounded figures.** The History footer added up
  a column already rounded to the cent, so thirty-one shares of a $15.00
  connection charge came to $14.88. Totals are priced once, on the server.
- **Tariff bands match the owner's clock.** An aware bound was being attached to
  a zone rather than converted, which put a 15:00–20:00 peak window at
  10:00–15:00 local and mispriced every hour of every day. A seasonal tariff also
  produced no estimate at all, and a whole month could not be priced.
- **A state of health of 0 is stored as absent.** The library rewrites it to 100
  on every path, commented "assume healthy", which is the one column whose job
  is to warn about a degrading bank. Neither the fabricated 100 nor a raw 0 is a
  measurement, so both store as NULL.
- The watchdog never fired, because the dying loop cleared the flag it was
  checking on its way out.

### Known issues

- Grid export and any period with a gap in it can be priced as though whole
  ([#18](https://github.com/sjordan0228/arraysense/issues/18),
  [#19](https://github.com/sjordan0228/arraysense/issues/19)). Import is priced
  daily, so this is the one to read first.
- A month missing a day currently waives part of its connection charge. A
  connection charge is owed for being connected, not for being observed.
- The hourly rollup attributes energy counters an hour early
  ([#17](https://github.com/sjordan0228/arraysense/issues/17)). Invisible on a
  tariff with no on-peak import, and wrong regardless.
- Per-module fault and warning codes are a constant zero, and remaining capacity
  is state of charge restated
  ([#20](https://github.com/sjordan0228/arraysense/issues/20),
  [#21](https://github.com/sjordan0228/arraysense/issues/21)).
- Only EG4 and LuxPower inverters over the WiFi dongle are supported. The driver
  work that opens that up is
  [#10](https://github.com/sjordan0228/arraysense/issues/10) onward.
