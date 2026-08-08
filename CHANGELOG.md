# Changelog

What changed, and why it mattered. Entries are written for somebody deciding
whether to upgrade, so a fix says what was wrong rather than what was touched.

Versions follow [semantic versioning](https://semver.org). Until 1.0 the schema
may change between minor versions, and any release that needs a database
migration says so at the top of its entry.

## 0.5.3 — 8 August 2026

### Fixed

- **A real full charge went unnoticed**
  ([#36](https://github.com/sjordan0228/arraysense/issues/36)). The bank was
  charged to full overnight on 8 August, all four packs recalibrated, and the
  spread between their counters fell from 24 points to 3 — and the dashboard
  still said "State of charge maybe drifting", still marked every pack
  percentage as an estimate, and still advised charging to 100%. A full charge
  was only credited to a bank that held at its charge reference for twenty
  minutes, which this hardware never does: it crosses absorb, finishes and
  tapers to zero in about three minutes, so the sixty-day search came back
  empty with the charge sitting in the history. A charge that short is now
  credited on the packs' own
  evidence instead — every pack the bank is known to hold arriving at full
  within a couple of minutes of one another, with the bank at its reference and
  the current settled. What is *not* credited is a single counter drifting to
  100% on its own, or a bank whose four counters have all been pegged at 100%
  for weeks with no charge behind them; both are the drift being detected, and
  both stay reported.

## 0.5.2 — 8 August 2026

### Added

- **The installation has its own timezone**
  ([#7](https://github.com/sjordan0228/arraysense/issues/7)). Every page used to
  send the *browser's* zone, so a phone that had travelled got a different
  answer for the same day — and a bill drawn against the wrong midnight looks
  entirely normal rather than obviously wrong. Rate bands are wall-clock hours
  in the owner's zone, so this is the one setting a tariff cannot be read
  without. The zone now decides, then the request, then the machine; an
  unresolvable name is refused where it is typed rather than discovered later
  by an endpoint that has to guess what to do about it. The pages ask the
  service which calendar to build rather than assuming their own — half a fix
  here is worse than none, because a page still cutting months on the
  browser's clock while the service prices on the installation's loses the
  connection charge and can show one month's bill under another month's
  heading. Leaving it empty follows the host exactly as before, so nothing
  moves on upgrade.
- **A friendly editor for the rate bands.** They were a box of pipe-delimited
  text — a rate, a clock range and a season per line — which is a reasonable
  thing to ask of somebody who wrote it and an unreasonable one for anybody
  else. Each band now has a rate box, month toggles and time selectors. What
  is *stored* is the same text as before and the service is still the only
  thing that judges it: a browser with its own opinion about tariff grammar is
  how a January evening once got priced at a summer peak rate. A band the
  editor cannot represent keeps its own text box rather than being dropped,
  and bands nobody touched are saved back exactly as they were written. A band
  whose hours are entirely claimed by a band above it is now pointed out —
  bands are first-match-wins, so one that prices nothing is almost always a
  mistake, and until now nothing anywhere said so.
- **Where the installation is**, as latitude and longitude, with a control that
  fills them from the device you are holding. Groundwork for weather
  ([#5](https://github.com/sjordan0228/arraysense/issues/5)). Empty means not
  recorded and is kept distinct from zero, which is a real place in the Gulf of
  Guinea.
- **A contact address**, recorded for alerts that do not exist yet, and masked
  on read like the serials. Nothing is sent to it. Sending needs a mail server
  and a password, and there is nowhere honest to put a password until
  [#34](https://github.com/sjordan0228/arraysense/issues/34) settles
  authentication.
- **Common currencies are suggested** rather than typed blind
  ([#6](https://github.com/sjordan0228/arraysense/issues/6)) — offered, never
  enforced, so an unusual currency stays possible and one already configured is
  never replaced.
- **An About panel** naming the running version and what the connected driver
  declares it can measure.
- **Numbers say what they are measured in.** Seconds, currency per month,
  currency per kWh, decimal degrees. The export credit is money for each
  exported kWh rather than an amount of energy, and now says so — a box marked
  only "kWh" invites a rate to be typed as a quantity.

## 0.5.1 — 8 August 2026

Closes every open item in 0.5.0's known issues except the rollup migration,
which is a decision about existing rows rather than a defect in the code.

### Added

- **Drivers declare what they produce, and the API can say what a device
  supports** ([#11](https://github.com/sjordan0228/arraysense/issues/11)).
  Every metric used to become a column on every installation, which is right
  only for as long as every installation is an 18kPV: on a machine with one PV
  string a column of NULLs would mean both "this inverter has no third string"
  and "this inverter's third string reported nothing", and those are not the
  same claim. Each driver now names the subset of the registry it produces,
  new databases are built from that declaration, and `GET /api/capabilities`
  reports per device what it can measure. `metrics.py` remains the single
  source of truth for what a metric is called, how it scales and what values
  are plausible, and adding an inverter metric is still a one-line change
  there. **An existing database is untouched**: reopening one under a
  declaration adds and removes nothing, and every reading already stored stays
  readable.
- **The Graphs page draws how far the packs disagree, in charge and in
  voltage** ([#26](https://github.com/sjordan0228/arraysense/issues/26)). Two
  bands under Battery: the spread in state of charge across the packs, which
  is fuel gauges drifting apart and collapses when one completes a full
  charge, and the spread in terminal voltage, which should sit near zero
  because parallel packs are physically forced to share it. Read side by side
  they are the difference between a bank whose gauges are lying and a bank
  with resistance somewhere it should not be — the diagnosis the calibration
  card could previously only state for the current instant. A spread is drawn
  only where every known pack reported: taken across whichever packs happened
  to answer it would be a distance that never existed.

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
