# Changelog

What changed, and why it mattered. Entries are written for somebody deciding
whether to upgrade, so a fix says what was wrong rather than what was touched.

Versions follow [semantic versioning](https://semver.org). Until 1.0 the schema
may change between minor versions, and any release that needs a database
migration says so at the top of its entry.

## 0.6.4 — 8 August 2026

### Fixed

- **An unknown timezone asked of `/api/costs` is refused, not answered with a
  server error** ([#49](https://github.com/sjordan0228/arraysense/issues/49)).
  Asking for costs in a zone the tz database does not know raised out of the
  endpoint and became a 500, telling a caller who sent a bad zone that the
  service is broken and sending them to look at the wrong thing. It now answers
  400 with the zone named, which is what `/api/energy` and `/api/bands` already
  do.

  `/api/status` still falls back instead of refusing, and the difference is what
  each answer is for: the banner only says whether the screen is current, which
  is worth answering in some nearby zone rather than withholding over a browser's
  stale name, while a month's cost is cut at a midnight and one cut in the wrong
  place looks entirely normal.

  An installation that has set its own timezone was never affected and still is
  not — where a zone is configured, the caller's is not consulted at all, not
  even to reject it, so a phone carrying a stale zone cannot refuse a request the
  service can answer perfectly well.

## 0.6.3 — 8 August 2026

### Fixed

- **A tariff band whose window was partly unmeasured now says so**
  ([#31](https://github.com/sjordan0228/arraysense/issues/31)). The Costs page
  marked its totals and its cards when a counter went quiet, but left the band
  table alone — so an evening the inverter never reported could sit in the table
  as `On-peak 0.0 kWh / $0.00`, unmarked, beside a total that was flagged. Read
  plainly it said the peak hours had cost nothing, which is the most expensive
  thing on the page to get wrong.

  Each band row is now marked when some of the period's unplaced energy could
  belong to it, and the marks are per column: the house figures stay clean when
  the house counter was fine and only the import counter went quiet. What the
  mark claims is deliberately narrow. Which band the energy belongs to cannot be
  known — inside a gap the amount is exact in total and unplaceable within it —
  so the wording says the window was partly unmeasured and names no amount and
  no owner. A band measured end to end is not marked, and neither is a complete
  period.

  Three cases the first cut of this missed, all of which would have read as
  clean data. A counter that stops and never resumes leaves no gap behind to
  find — there is no later reading to close one — so an outage running to the end
  of the month marked nothing at all. The same for a counter with no readings in
  the period whatsoever. And merging days into a month dropped the band names,
  which is exactly the path the History footer takes.

  The mark on the split bars was invisible. It was drawn in the page's warning
  amber, which measures 1.0:1 against the bar it sits on in light mode; it is now
  a hatch in the page's own foreground colour, which inverts with the theme and
  measures above the 3:1 floor on every band shade in both. The segment that
  needed it most was also the one with no width to draw in — a band reading zero
  — so a marked segment now keeps a few pixels of its own.

## 0.6.2 — 8 August 2026

### Added

- **A theme button, in the header of every page**
  ([#33](https://github.com/sjordan0228/arraysense/issues/33)). It cycles between
  following the device, light, and dark, and remembers the choice for that
  browser — so a wall tablet can stay dark while a laptop follows the room it is
  in. The charts repaint in place rather than needing a reload, and the band
  shading legend turns round with them: "brighter = higher rate" on a dark page,
  "darker" on a light one.

## 0.6.1 — 8 August 2026

### Added

- **Light mode, following the system setting**
  ([#33](https://github.com/sjordan0228/arraysense/issues/33)). A machine set to a
  light appearance gets light pages. Nothing changes for anyone who has not
  asked for it — the dark theme is unchanged down to the pixel.

  The chart colours are the same in both. A pair of colours sits the same
  distance apart whatever is behind them, so the ones already validated stay
  validated; what changes is the page under them. Two things had to be reversed
  rather than restyled, because they are painted onto the chart itself rather
  than set in the stylesheet: the tariff band shading, which is a pale wash on a
  dark page and a dark one on a light page, and the zero line on the battery
  chart. The shading legend reverses with it — "brighter = higher rate" on dark,
  "darker = higher rate" on light — because naming it the wrong way round would
  point the reader at exactly the wrong hours.

## 0.6.0 — 8 August 2026

### Added

- **The battery card says how fast the bank is filling or emptying**
  ([#44](https://github.com/sjordan0228/arraysense/issues/44)). A power figure
  says how hard the bank is working, not what that means for it: +7 kW into a
  57 kWh bank is a different afternoon from +7 kW into a 14 kWh one. The card now
  reads `+5.09 kW · bank 64% · +8.5 %/hr · 53.7 V`.

  The rate is worked out by the service, from the bank's own reported capacity
  rather than a number typed into the code. An idle bank reads `0 %/hr`, which is
  a real state; a bank whose capacity was not reported shows no rate at all
  rather than a zero standing in for something nobody knew.

- **A battery icon on the card**, filled to the state of charge. The fill is the
  part that carries the meaning and the percentage is printed beside it, so the
  colour is a third telling rather than the only one — the icon reads correctly
  whether or not its colours are distinguishable. Below 20% it is red, to 50%
  amber, above that green.

- **The pack voltage**, beside the rest.

## 0.5.9 — 8 August 2026

### Changed

- **The band shading legend says what it means in plain words.** It read
  "brighter is dearer", which is compressed and not how most people say it. It now
  reads "brighter = higher rate".

## 0.5.8 — 8 August 2026

### Added

- **The Power flow chart shows which hours were expensive**
  ([#46](https://github.com/sjordan0228/arraysense/issues/46)). Peak hours are
  shaded brighter behind the lines, so a spike of grid import can be read against
  what it cost rather than only how large it was. The legend names the bands and
  says which way round the shading goes.

  The shading varies **brightness, not colour**. A tariff has as many bands as it
  likes and two colours could never say that, and every colour on these charts has
  to be checked against every other for a reader who cannot tell some of them
  apart — so a band with a colour of its own would be one nobody had checked.
  Brightness needs no such check and works for everybody.

  With no tariff configured nothing is shaded, and a stretch of time no band
  covers is left plain rather than shaded as though it had a middling price.

### Changed

- **Solar is drawn as a line rather than a filled area.** Two filled areas left no
  room to read the shading behind them, and on a sunny day the solar one covers
  most of the chart. Grid keeps its fill: when the house runs on the grid, import
  equals house load to the watt, so a grid line lies exactly under the home line
  and disappears beneath it.

## 0.5.7 — 8 August 2026

### Added

- **An endpoint that says which tariff band a stretch of time fell in**
  ([#46](https://github.com/sjordan0228/arraysense/issues/46)). `GET /api/bands`
  returns the ordered windows covering a range, each with its band, its price and
  its exact bounds. This is the groundwork for shading the Power flow chart by
  band, so grid import can be read against what it cost rather than only how much
  there was. The chart itself comes next; nothing on any page changes yet.

  The windows are worked out here rather than in the browser for the same reason
  the pricing is. When the page had its own copy of the tariff rules the two
  disagreed within a day: it rejected the seasonal format the parser accepts, so a
  real tariff priced nothing at all, and in the older format it had no notion of a
  season and charged a January evening at the summer peak rate.

  With no tariff configured the answer is no windows, rather than one window
  implying the whole day was cheap. A range longer than can be walked is refused
  with a clear message instead of a server error.

## 0.5.6 — 8 August 2026

### Changed

- **Chart tick labels can be read** ([#45](https://github.com/sjordan0228/arraysense/issues/45)).
  They were 9.5px — the smallest text anywhere on a page whose body type is 14px —
  in the dimmest ink the palette has, measuring 5.6:1 against the panel. They are
  now 12px in `--ink2`, which measures 10.7:1, with the axis gutters and the
  minimum tick spacing grown to match so nothing clips or overlaps. Applies to
  every chart on every page, since they share one axis definition.

- **Battery charge and discharge are green and red.** They were one hue at two
  lightnesses. The zero line still carries the meaning — charge above it,
  discharge below — so the colour reinforces the split rather than being the only
  thing that says which is which.

  The pair was measured, not chosen: every combination scored under simulated
  protanopia, deuteranopia and tritanopia against the panel background. Charging
  keeps `#2aa198`; discharging is `#d1495b`, which separates from it by ΔE 20.6 at
  worst — better than any pair already in the palette. A more obvious red,
  `#e0603d`, was rejected because it collapses into the solar orange under
  deuteranopia at ΔE 3.3. That is the whole reason these are measured rather than
  picked by eye, and there is now a test pinning the two values so neither drifts.

## 0.5.5 — 8 August 2026

### Fixed

- **A battery pack the inverter did not read was recorded as though it had
  been** ([#40](https://github.com/sjordan0228/arraysense/issues/40)). The
  inverter library serves every pack it has ever seen on every read, and a pack
  the firmware did not surface on a given cycle comes back with its registers
  frozen at their last real values. Nothing checked for that, so held readings
  were stored stamped with the current time — a pack last actually measured
  fifteen minutes or nine hours ago looked as fresh as one measured a second ago.

  The consequences were not cosmetic. Every safeguard downstream decides what to
  trust by looking at that timestamp, so none of them could fire: the checks that
  drop a reading too old to compare, and the ones that require packs to have been
  read at the same moment, both saw a bank where every pack was current. A held
  voltage compared against a live one could raise a **wiring fault that was not
  there**, and the charge- and voltage-spread graphs could draw a spread that was
  partly the gap between a fresh reading and a stale one.

  A held pack is now left out of the reading rather than recorded, the same way a
  pack whose BMS has gone quiet already was. **Expect the spread graphs to look
  sparser as a result**: moments where a pack was not actually read now show a
  break instead of a line drawn from old values. That is the honest picture
  replacing a misleading one, not a regression.

  Two cases deliberately keep the pack rather than dropping it, because being
  wrong in the direction of keeping data can be recovered from and being wrong in
  the direction of discarding it cannot: a library build that does not stamp the
  reading time at all, and one that stamps it without a timezone. The second logs
  a warning every poll, because it would mean the library changed underneath us.

## 0.5.4 — 8 August 2026

### Fixed

- **A bank of more than four battery modules stopped collecting entirely**
  ([#29](https://github.com/sjordan0228/arraysense/issues/29)). A fifth pack
  raised an error the poll loop did not catch, which killed the collector while
  the web server carried on serving pages — so the dashboard looked alive, the
  history simply stopped, and not even a gap row was written to show where. It
  was not only a five-pack problem: the inverter library hands out a *virtual*
  slot per battery and keeps the number reserved when a serial goes unread, so a
  four-pack bank that had one poll with an unreadable serial could produce a
  fifth slot and hit the same wall. Storage never needed the limit — a pack is a
  row keyed on its serial, not a column — so no migration is required and
  existing databases are untouched.

- **Any reading the driver could not decode killed the poll loop.** The loop
  died, nothing restarted it, and the outage left no trace — not even a gap row
  to show where the history stopped. Such a reading is now recorded as a gap
  carrying its reason and backed off from, exactly as an unreachable inverter is,
  and the status page names it as a condition of its own instead of borrowing the
  name of an unreachable inverter or a failing disk. One limitation is worth
  knowing: the reading is recognised by the `ValueError` a sample raises when it
  refuses what the driver assembled, which is broader than it ought to be, so a
  `ValueError` from an unrelated mistake in our own code is recorded the same way
  rather than surfacing. A dedicated decode error is the next step.

- **The dongle was not released when the poll loop died.** Stopping the service
  re-raised the dead loop's error before it reached the disconnect, so the
  dongle's single TCP slot stayed held until it timed out — blocking both the
  restart and the EG4 app.

- **The shipped service file could never restart a stalled collector.** It set
  `Restart=on-failure`, and the watchdog restarts a stalled loop by sending
  SIGTERM, which systemd treats as a clean exit — so the one restart the
  watchdog exists to trigger was the one that never happened. Anyone who
  installed from this repository had a watchdog that could stall silently. Now
  `Restart=always`, and the installation docs say why.

- **Plausibility checking raised an error on a fifth pack** rather than checking
  it. `validate.py` resolved a reading's bounds through its slot number, which
  only exists for four of them; it now resolves through the shared template, the
  same way storage does, so the two cannot disagree about what is plausible.

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
  credited on the packs' own evidence instead: every pack the bank is known to
  hold measured below full in the quarter hour before the absorb and at or above
  99% together during it, with the bank at its reference and the current settled.
  A transition per pack, because a charge resets every counter. What is *not*
  credited is one counter drifting to 100% on its own, a bank whose counters have
  all been pegged at 100% for weeks with no charge behind them, or three stale
  counters sitting at 100% beside a fourth that really did charge — all three are
  the drift being detected, and all three stay reported.

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
  mistake, and until now nothing anywhere said so. A band name cannot hold a
  pipe, a semicolon or a line break: those separate one band and one field from
  the next, so a name containing them would store a different set of bands than
  the one on screen. They are dropped when the name is saved, and the editor
  says so beside it.
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
