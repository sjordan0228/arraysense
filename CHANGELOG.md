# Changelog

What changed, and why it mattered. Entries are written for somebody deciding
whether to upgrade, so a fix says what was wrong rather than what was touched.

Versions follow [semantic versioning](https://semver.org). Until 1.0 the schema
may change between minor versions, and any release that needs a database
migration says so at the top of its entry.

## 0.9.0 — 12 August 2026

The setup wizard learns what an installation is, a first-visit tour explains the
pages, the backup becomes something you configure rather than something fixed,
and the settings page stops being one long scroll. Behind that, an audit of the
data path — the collector, the store, the API and the modelled figures — and the
defects it found.

### Added

- **The wizard asks where the installation is.** One optional postcode, resolved
  through Open-Meteo's keyless geocoder, shown back as the town it found so it
  can be checked before it is accepted. Coordinates remain for the countries the
  geocoder does not cover, and nothing calls them more accurate: moving 4.6 km
  changes modelled plane-of-array irradiance by 0.006 %, against a forecast grid
  of about 11 km. Location is the one thing the service never reconstructs for
  itself afterwards.
- **The inverter's conversion figures are carried as cited fact.** CEC 96.9 %,
  PV to grid 97.5 %, battery to grid 94 %, PV to battery 99.9 %, idle around
  70 W and 18 W, from the EG4 18kPV spec sheet version 1.4.3, labelled a
  manufacturer's claim rather than a measurement. They are shown, not used: both
  sides of the performance comparison are DC, so putting a conversion figure in
  it would lift every installation's ratio by about three per cent for no
  physical reason.
- **A panel catalogue**, each entry cited, with a warning when a generic module
  is chosen — a generic is a guess, and it should not be indistinguishable from
  a measured panel.
- **A guided tour of the pages**, offered by a dismissible banner rather than
  interrupting, and dismissed per browser: a tour is shown to a person, and one
  household member silencing it on the kitchen tablet should not silence it on
  somebody else's phone. It skips what an installation does not have, and never
  restates a number, so it cannot drift from the cards it describes.
- **`arraysense restore`**, replacing a printed shell recipe that could destroy
  the database it was restoring. It unpacks beside the live database, proves the
  result is a database with rows in it, and only then stops the service, clears
  the write-ahead log and moves the file into place.
- **The backup is configured, not fixed.** Destination, retention, schedule and
  whether it runs at all are settings. The timer fires every fifteen minutes and
  the command decides whether a backup is due, so systemd keeps reliable wakeups
  and catch-up after downtime while the installation owns the time. A
  destination is checked before it is accepted, and the three ways it can fail —
  missing, not writable, outside the unit's writable set — are told apart.
- **Tabs on the settings page**, in the order an installation is configured. A
  prefix no tab claims still appears, so a setting added to the registry
  tomorrow cannot vanish behind a layout.
- **Troubleshooting for the backup**, including the trap that a hand-run backup
  succeeds while the scheduled one fails, because they run as different users
  under different sandboxes.

### Fixed

- **A week or a month was scored from its first day.** The hourly rows were
  truncated at 24 offsets. On the reference installation the worst hour of the
  week of 3 August moves from 1.708 to 6.322 kWh.
- **A total no longer hides how much of its period it covers.** It carries the
  days scored against the days expected, and says which incompleteness it is.
- **A string the inverter never reported is absent, not zero.** It no longer
  produced a row claiming expected 0.0, actual 0.0 and a specific yield of 0.0.
  Days stored under the old logic present a zero as a measurement, so the
  efficiency config version moves and history is scored again.
- **The peak-scaled forecast is gone rather than labelled.** It over-called by
  43 to 63 per cent on twelve consecutive days, and for an installation with no
  array described it was not a first-week measure but the permanent answer.
  Below five scored days there is now no forecast.
- **The sky poller could stop without saying so.** One failed interval read
  ended the loop for good; nothing watched it, and the only symptom was that
  recorded conditions stopped.
- **A tier that could not answer a range said so**, rather than returning
  nothing or a silently truncated answer.
- **A date typed off the end of the calendar** answered 500 on several
  endpoints. It is a bad request, on all of them.
- **"Calibrated from" was printed whenever any daily row existed**, whether or
  not anything had been fitted.
- **The efficiency backfill shared the collector's transaction.** A backfill
  failing part-way rolled back a poll that had succeeded, taking its readings,
  its battery rows and its device registration with it. It has its own
  connection now, as the rollup already had.
- Also: a sub-second poll interval is a configuration error rather than a 500;
  the hour in progress is no longer extrapolated to a whole one; the declared
  NOCT is read by the cell-temperature model; and `peak()` answers absent for a
  column the database does not have.

### Known

Declared tier retention is still not enforced and the database grows about
5 MB a day (#135). Two strings on one MPPT are still double-counted (#133).
There is still no authentication (#34), so this should not be exposed to the
internet.

## 0.8.1 — 12 August 2026

### Fixed

- **Two writers shared one row and destroyed each other's readings.** The raw
  tier is keyed by timestamp and device at one-second resolution, and it has two
  writers: the inverter poll loop, and the weather poller on its own
  fifteen-minute clock. Nothing coordinates the two clocks, and the write
  replaced every column of the row, so whichever landed second erased the first.

  Replayed over a month of the reference installation's real history — 255,798
  rows — a weather tick landed on a second an inverter poll already owned
  **294 times in 3,116 ticks**, and every one of those polls lost all 91 of its
  readings while its own battery modules kept theirs at that same instant. Worse,
  a tick landing on a recorded outage cleared the reason: **all 62 gap rows in
  that month were erased** by the replay. An outage smoothed into a straight
  segment is an outage nobody ever notices.

  Each writer now updates only its own columns, told apart by the metric
  registry's own classification of what is the site's and what is the
  inverter's. Replayed again, the same 294 collisions produce rows carrying both
  writers' data — which no row in the database had ever held — and no losses.

- **The archive backfill destroyed the weather it had just written.** The
  archive answers one hour in two pieces, the means over the hour just gone and
  the readings taken at the label, and a day's request therefore also writes the
  previous day's last hour. Backfilling a range wrote each shared hour twice and
  kept only the second half. A site reading now writes the columns it carries
  and leaves the rest of that instant alone.

- **A failed poll could overwrite the reading before it.** The gap was stamped
  before the connection was even attempted, while a successful reading is
  stamped when the read completes — and since the cadence is the interval or the
  read time, whichever is longer, a failure filed under that older stamp landed
  on the second the previous poll's reading was filed under. The stamp now comes
  from the moment the failure was seen, and a recorded gap is refused outright on
  any row that holds a reading, which also covers a clock stepped backwards.

- **The daily energy counters were cut at UTC midnight rather than the owner's.**
  The inverter resets them at its local midnight. Between the two midnights —
  five hours on the reference installation, and the five that hold the evening
  peak — the cache believed it was still yesterday and carried the old day's
  totals into the new one, where the daily metrics roll up with max and that
  stale high-water mark then stood for the rest of the day. The day is now cut in
  the installation's own zone, which also makes the 23- and 25-hour days come out
  right.

## 0.8.0 — 12 August 2026

An installer, a command to manage the installation afterwards, a daily backup,
and documentation that describes what the software does rather than what it was
once going to do. This is the release the project is announced on.

### Added

- **`install.py`, a one-line bootstrap.** It refuses a host it cannot install
  on one reason at a time before touching anything, resolves the port — 80 when
  it is free, otherwise asking, with 8080 as the default — prints everything it
  intends to do, and installs only after that is confirmed. It writes no
  configuration file, because the config's absence is what runs the setup
  wizard. `--yes`, `--port`, `--repo` and `--ref` cover unattended installs and
  installing a fork or a pinned ref.
- **`arraysense`, the command left behind.** `status`, `upgrade`, `logs`,
  `restart`, `backup`, `restore`, `uninstall` and `version`. It acts; the web
  page only reports, because the service binds `0.0.0.0` with no authentication
  and an update button on that surface would be remote code execution on a home
  network.
- **`arraysense upgrade` rolls back a release that will not run.** It shows the
  incoming commits and the incoming changelog entry, confirms, applies, and
  restores the previous commit if the service does not come back. Rolling the
  code back does not require rolling the database back, because migrations only
  ever add columns.
- **A daily backup onto a different disk.** SQLite's online backup API, so the
  collector keeps writing; the copy is verified with `PRAGMA quick_check`
  before it is trusted, compressed, and renamed into place atomically; older
  backups are removed only after a new one exists. Measured on the reference
  database: 264 MB live, about 21 MB compressed, roughly 7.2 GB a year written
  to the card. The uncompressed working copy is written beside the database
  rather than on the backup disk, which is what keeps that figure where it is.
- **`arraysense restore`**, which replaces a shell recipe that could destroy the
  database it was restoring. It unpacks beside the live database, proves the
  result is a real database with rows in it, and only then stops the service,
  clears the write-ahead log, moves the file into place and waits for the
  collector to return.
- **`docs/raspberry-pi.md`**, carrying what the reference installation actually
  cost: SD-card wear and why the database belongs on a USB SSD, mounting by
  UUID, the `ReadWritePaths` carve-out without which every write fails on a
  read-only database, the USB enclosure quirk, and the RS485 udev rule and
  `dialout` membership.

### Fixed

- **A fresh install reported failure.** Setup mode serves only `/api/setup`, so
  the health check polling `/api/status` read a healthy new installation as
  dead and told every first-time user the service had not come up. Waiting for
  setup is now a state of its own rather than a failure.
- **`arraysense upgrade` could never succeed.** The installer cloned with
  `--depth 1`, so the fast-forward failed with "refusing to merge unrelated
  histories" on every machine it created. The clone keeps its history, and an
  upgrade repairs an installation that was made shallow.
- **An unreachable inverter was read as a failed upgrade**, so a working
  release was rolled back whenever the inverter was quiet — and the healthy
  rollback was then reported as "the service is down".
- **`arraysense status` called a database it could not read "empty".** The file
  is owned by the service user; the size was right and the range said absent.
  It now distinguishes empty from unreadable.
- **The rollback claimed success it did not have**, printing "rolled back"
  without checking that the checkout worked.
- **A purge could recurse into a directory.** The database and its sidecars are
  regular files; a configured path that names anything else is refused.
- **The backup could not write beside the database**, because the unit declared
  no state directory of its own — and reported the failure as "another backup
  is running" when nothing was.
- **Messages that named a cause nobody established**, throughout: a fabricated
  "0.0 MB", "already up to date" when the comparison never ran, "restarted and
  collecting" over a dead collector, a stale lock reported as a live one.
- **The installer could not report a failed `uv` download**, returned success
  for a service that would not survive a reboot, silently ignored `--port=8080`
  and any mistyped flag, and claimed a `.local` address on hosts that do not
  answer mDNS. Its docstring claimed it downloads no further scripts while
  piping `uv`'s installer, which was the stated mitigation for running it as
  root.
- **`/api/capabilities` reported no model** while returning everything derived
  from it, so a page had every consequence of "this is a FlexBOSS21" and not the
  fact itself.
- **Documented retention is not enforced.** Nothing prunes any tier and the
  database grows about 5 MB a day; the pages say so now, and the published
  "roughly 280 MB" sizing figure is gone. Tracked in #135.
- Three published pages said the software did not work. They now carry the
  measured record instead: 668 days of hourly history, 34.5 days of continuous
  collection covering 99.76 % of that window.

## 0.7.3 — 11 August 2026

### Fixed

- **The Inverter tab drew a row for every reading the reference machine has,
  whatever machine you actually own**
  ([#12](https://github.com/sjordan0228/arraysense/issues/12)). Cards already
  disappeared when a device declared nothing they could show — a machine with no
  backup panel has no Legs card rather than an all-dash one — but inside the
  cards that remained, every row was drawn regardless.

  Measured on a device declaring 19 metrics against the reference machine's 91:
  **28 dashes across six of the seven cards**, for registers that hardware never
  reads. "Cell high", "Charge reference", "Discharge cutoff" and the rest were
  all present and all empty. The same device now shows four cards and no dashes
  at all.

  A permanent dash is worse than a missing row, which is the whole reason this
  matters: it teaches you to read past dashes, and the next one is a sensor that
  has actually failed.

  Two kinds of row needed more than a yes or no. A per-pack reading is declared
  in a different list from an inverter reading, so asking the wrong list would
  have answered no on every machine. And a row naming two readings — "H1 · H2",
  "Health / cycles" — cannot simply be kept or dropped: an inverter with one
  heatsink used to draw a real number beside a dash, which reads as a broken
  sensor rather than as a machine built differently. Those rows now name only
  the halves that exist, so one heatsink reads "H1". A row that is a difference
  rather than a pair — cell spread, temperature spread — still requires both
  ends, because a spread measured from one end is not a spread.

  Nothing changes on an inverter that declares everything. The row set was
  captured from the reference installation before the change and compared after:
  identical, down to the separators.

## 0.7.2 — 11 August 2026

### Fixed

- **The History page's 30-day view no longer takes seven seconds**
  ([#87](https://github.com/sjordan0228/arraysense/issues/87)). It read the
  minute tier to answer a question about days — roughly a quarter of a million
  rows to produce thirty numbers. A day's kWh telescopes, being the counter at
  the end minus the counter at the start, so a coarser tier moves at most one
  bucket-edge's worth of energy between neighbours and never loses any. Both the
  daily and monthly views now read the hourly tier. Measured on the reference
  installation, a Raspberry Pi 4: about seven seconds before, 0.06 seconds now.

  The fallback order was revisited at the same time. The coarse tiers are rollup
  destinations, so a database whose rollup has not yet run has empty ones, and
  answering "no energy" out of an empty tier while the readings sit in the raw
  table would be the worst of both.

- **Light mode is readable where colours had been written into rules rather
  than into the theme.** The hover readout took its text from a token and its
  background from a fixed near-black, so under light mode it drew dark text on a
  dark box. Several neighbours hardcoded white tints that simply vanish on a
  light surface: the range and navigation buttons and their selected states, the
  chart bar and icon buttons, the selection box and crosshair on every chart,
  and the efficiency page's date navigation.

  The readout and the crosshair now carry their own tokens, because a readout
  sits over a chart rather than on a panel and has to stay legible against
  whatever is behind it. Dark mode is unchanged except at the tint call sites,
  where each took the nearest existing step — a shift of one or two hundredths
  of alpha — and at the settings select, which had been a hardcoded dark well
  and now matches the selects on the settings page.

- **The fake driver reports the strings it declares**
  ([#90](https://github.com/sjordan0228/arraysense/issues/90)). It advertised
  three PV strings and then produced no per-string metrics, so anything written
  against it was written against a shape no real inverter has.

### Added

- **The efficiency chart's hover says what the ratio was**, not only the
  expected and actual figures with the division left to the reader. The row is
  labelled Actual / Expected rather than Performance, because the page's
  headline ratio divides by expected minus curtailed and the chart carries only
  the two raw series — a row implying the headline figure would be wrong on any
  day something was curtailed.

  It states a ratio only where the figures above it can support one. The hours
  either side of sunrise and sunset arrive with expected production rounded to
  four decimals, and dividing there once reported 150000%, or 100% directly
  beneath two rows both reading 0.00 kWh. Below that floor, and for a negative
  measurement, the row shows the same dash it shows for a reading nobody took.

## 0.7.1 — 11 August 2026

### Changed

- **The forecast panel draws one prediction, not two**
  ([#96](https://github.com/sjordan0228/arraysense/issues/96)). It carried a
  frozen morning baseline behind a live revision, and a figure saying how far
  ahead or behind the day was running against the frozen one. Two prediction
  curves on one chart read as clutter rather than as insight. There is now a
  single curve, called Prediction, re-made on the weather poller's own clock,
  and the gap between it and the solid actual line says what the tracking figure
  said without a number whose reference was nowhere on screen.

  The ahead-or-behind figure went with the baseline rather than being pointed at
  the live curve. Measured against the live curve it would have looked like it
  worked and meant almost nothing: the prediction for an hour already past is
  re-made from a forecast that has since seen that hour, so it converges on what
  happened and would read near zero all day.

  The header now says how often the prediction is re-made, served by the
  endpoint rather than written into the page, because that interval is a setting
  an owner may move between five minutes and a day.

  Every revision is still recorded. Nothing on screen reads the older rows now,
  but they are the only account of how a day's expectation moved, and
  reinstating the baseline is a query rather than a migration.

## 0.7.0 — 11 August 2026

### Added

- **The array and the battery bank are now things you describe, and everything
  else reads that description** ([#97](https://github.com/sjordan0228/arraysense/issues/97)).
  One multiline setting holds a line per string — name, MPPT input, panel count,
  watts each, tilt and azimuth, then optional `key=value` fields for the rest —
  parsed by exactly one parser, which is also the one that refuses a bad line at
  the settings page. Every default that gets applied is also named, so no page
  presents an assumption as something you typed, and an unknown key is refused
  loudly rather than quietly becoming a default you believe is set. The battery
  group records chemistry, module count, capacity and round-trip efficiency.
  `GET /api/panels` serves the parsed result so nothing else has to know the
  grammar. Without this there is no array to model, and the rest of this release
  depends on it.

- **The Efficiency tab shows what the array actually made against what it should
  have made, and why not every watt was counted** ([#96](https://github.com/sjordan0228/arraysense/issues/96)).
  A headline performance ratio with a tolerance band, a budget bar that reconciles
  expected to actual through named causes, and a per-string breakdown that locates
  an underperformer. The waterfall treats refused energy differently from lost
  energy: curtailment sits past the gap as an outlined segment, so a full-battery
  afternoon does not read as a faulty array. A day is drawn hour by hour; a week
  or a month, day by day — the longer periods used to carry a headline figure with
  no chart under it, which reads as something broken rather than as a period with
  coarser detail. `GET /api/efficiency` serves all of it, so the page computes
  nothing itself.

  A string is judged curtailed only when the battery had nowhere to put the power
  **and** its own voltage says so — each string against its own operating point,
  never a threshold shared across the array. On the reference installation string
  one runs at about 377 V where the others sit near 310, so a shared threshold
  would mark it permanently curtailed and hide every real fault on it.

- **Every day of history is scored once and kept**, rather than remodelled each
  time a page asks for it ([#96](https://github.com/sjordan0228/arraysense/issues/96)).
  The maintenance clock rescores today and yesterday and fills the rest of history
  a bounded slice at a time — at most sixty days a pass, newest first, off the
  event loop so nothing stalls an open page. On the reference installation's 671
  days that converged in thirteen passes. Once a day is scored, it is a durable
  fact the trend and the forecast both read rather than a figure that happens to
  be recomputed the same way twice.

- **Past weather can be recovered, so a system with history can be scored against
  the weather it actually had** ([#96](https://github.com/sjordan0228/arraysense/issues/96)).
  `POST /api/efficiency/backfill` with a date range reads Open-Meteo's ERA5
  archive a day at a time into the ordinary hourly rows. Owner-triggered rather
  than implicit, because a year is a few hundred requests and no page load should
  start that; resumable, because rows are keyed by timestamp, so a re-run rewrites
  the same hours rather than duplicating them and a failure reports the last day
  that landed. Without it a new installation's performance trend starts from the
  day you set it up rather than the day the array did.

- **The dashboard forecasts the day's production, and draws the plan hardening
  into fact** ([#5](https://github.com/sjordan0228/arraysense/issues/5)). The
  morning's expectation is drawn as a hatched band, the day's measured output over
  it, and the panel says how far ahead or behind the day is running. A prediction
  is stored apart from every measurement and never in a metric column.

  It is calibrated by what this array has demonstrated, not by its best moment.
  The forecast runs the same model the Efficiency tab runs, over predicted
  conditions, and scales it by the median performance ratio of the scored days in
  the last 28 — which means an array described wrongly still forecasts correctly,
  because the model is then wrong by some factor and the measured ratio wrong by
  its reciprocal. Below five scored days it falls back to scaling by the observed
  peak, and says so in the log.

- **Per-string wiring is modelled, and the efficiency engine subtracts the
  resistive loss the wire actually incurs** ([#96](https://github.com/sjordan0228/arraysense/issues/96)).
  A string can declare the gauge and run length it is wired with, and the model
  subtracts that string's own ohmic loss instead of PVWatts' flat 2% allowance.
  New `solar.py` holds the physics: NOAA solar position, Hay-Davies transposition,
  Faiman cell temperature, and the PVWatts derate chain. `pvlib` remains a
  development dependency only, used to hold the transcription to a reference
  implementation at five sites across a year; the runtime dependency list is
  unchanged.

- **The weather is recorded and plotted, on its own slow clock**
  ([#5](https://github.com/sjordan0228/arraysense/issues/5)). Four site-level
  metrics — global horizontal irradiance, direct normal irradiance, diffuse
  horizontal irradiance, and wind speed — join outside temperature and cloud cover.
  The Graphs page draws the sky beside the solar bands; the dashboard shows the
  current conditions. Radiation is read against the hour it actually describes:
  Open-Meteo labels an irradiance hour by the hour it ends, and this project's
  buckets are labelled by the hour they begin, so every hour is now scored against
  its own sky rather than the next hour's.

- **The settings page can edit the wire fields** the efficiency engine now models
  ([#96](https://github.com/sjordan0228/arraysense/issues/96)).

- **The dashboard and Graphs page render what the inverter actually is**, not the
  reference machine's shape ([#12](https://github.com/sjordan0228/arraysense/issues/12)).
  Cards and charts read from `/api/capabilities`: one PV string on a one-string
  machine, none where none are declared, three on the reference. The Legs card
  gates on a declared backup panel, the BMS card on the battery its state-of-charge
  witnesses. A machine with no backup panel no longer shows an all-dash Legs card
  that reads as a fault.

- **The connection kind is rendered from capabilities**, not hard-coded as "Dongle"
  ([#72](https://github.com/sjordan0228/arraysense/issues/72)). The dashboard
  fetches the transport once at boot and names the connection from a single map
  used by both the label over the release controls and the yield copy — dongle
  yields a TCP slot, serial yields the port, and the two spots cannot disagree.

- **A systemd unit for a serial installation** ([#71](https://github.com/sjordan0228/arraysense/issues/71)).
  `PrivateDevices=false` so the USB-to-RS485 adapter is visible,
  `SupplementaryGroups=dialout` so the dedicated user can open it. The rest of the
  sandbox is intact. A dongle installation uses neither and may re-harden
  `PrivateDevices` with a drop-in.

### Fixed

- **The Efficiency chart escaped its container, and the page did not load
  uPlot's stylesheet** ([#96](https://github.com/sjordan0228/arraysense/issues/96)).

- **The day-by-day chart printed every date twice** on its x axis
  ([#96](https://github.com/sjordan0228/arraysense/issues/96)). uPlot chose a
  twelve-hour tick increment over the six days a week spans, so two ticks landed
  inside each calendar day and both printed the same date.

### Changed

- **The documentation explains the USB SSD carve-out** for a database on external
  storage ([#89](https://github.com/sjordan0228/arraysense/issues/89)). A
  `ReadWritePaths` directive is needed because `ProtectSystem=strict` in the
  service file blocks writes outside the declared paths.

**Upgrading.** This release adds `efficiency_day` and four site-level metrics
(irradiance components and wind) to the schema. The store lays down missing
columns on open; no manual migration is needed.

## 0.6.14 — 10 August 2026

### Added

- **The machine can now describe itself, which is what a setup wizard renders
  from.** Drivers declare a manufacturer and their models, and every model
  fact carries a citation — the 18kPV's three PV strings cite the reference
  installation; the 6000XP and 12kPV inherit the family declaration until a
  source exists, because a conservative default is honest and an invented
  spec is not. Configuration gains `model` and `battery_source` (`relayed`,
  the reality of every current installation, or `none`), the settings overlay
  carries the connection group, and `/api/setup` serves the manufacturer
  tree, each transport's required fields, the serial adapters the machine can
  actually see, and the current values with secrets redacted.

  Two endpoints act: **detect** stops the collector, reads the inverter's
  serial off the candidate connection, and starts the collector again on the
  way out whatever happened — it writes nothing, and on the dongle it needs
  the inverter serial you typed, because that protocol authenticates with it.
  **apply** validates the whole merged result with the registry's own boot
  rules — an overlay it accepts is one the next boot accepts — discards any
  masked value the form echoed back rather than storing dots over a real
  serial, writes every setting in a single transaction or none at all, and
  restarts the collector. Switching the driver family is part of it.

  A brand-new installation — no config file at all — now serves **first-run
  setup** instead of exiting with an error: pages and the setup endpoints,
  no store and no collector, because there is no inverter serial to open a
  store under until the wizard supplies one — typed, or read off a serial bus
  by detection. The wizard's first apply writes the only config file software
  will ever write, validated by the same loader that will read it at boot.

  A **first-run wizard** renders all of it: pick who made the inverter, which
  model, and how it is wired, read the serial off the wire with Detect or type
  it, and one button writes the config and restarts into a live dashboard —
  the page watches the restart come back rather than guessing at a delay, and a
  refused apply keeps its reason on the form. The **settings page's connection
  group is the same renderer**, one component in two shells so the wizard and
  the settings form cannot drift, prefilled with the current values redacted and
  saved through the same validated, restart-on-apply path. Detect on an
  unchanged connection uses the configured secrets rather than the dots shown
  for them. An existing installation changes nothing: every new field defaults
  to exactly today's behaviour, and an already-configured box shows no wizard.

### Changed

- **The battery topologies have names.** The wizard's Battery choice read
  "Through the inverter" and "No battery data"; it now reads "Closed loop
  (through the inverter)" and "None", with the reserved direct mode shown beside
  them as a disabled "Open loop — coming soon" so it reads as planned rather than
  missing. The server's choices are unchanged and still refuse it.
- **The timezone is chosen from a list** rather than typed — a menu over the tz
  database, so it cannot be mistyped. The empty default still means "follow the
  machine's own zone", which every existing installation has stored. The orphaned
  `check_timezone` validator went with the change, since a choice field validates
  by membership before any callback runs.
- **The settings page says less.** Group introductions that re-explained the
  controls beside them are trimmed or dropped, "This installation" becomes
  "General", and Collection now states what each transport wants — 11 seconds or
  more over the dongle, ten over RS485. Numeric help no longer reprints the
  bounds the number box already enforces.

## 0.6.13 — 9 August 2026

### Changed

- The History page's footer says "days in America/Chicago time" instead of
  "days as reckoned in America/Chicago". The zone still matters — a total
  labelled August 5 depends entirely on where midnight was put — it just no
  longer sounds like a treaty.

## 0.6.12 — 9 August 2026

### Fixed

- **The dashboard can no longer freeze the service it is watching**
  ([#63](https://github.com/sjordan0228/arraysense/issues/63)). The
  once-a-minute stall this project chased through the rollup pass for three
  releases was never the rollup pass. The dashboard reloads its history on a
  sixty-second timer and fetches the calibration advisory alongside — and that
  endpoint, like every tier-scanning read, ran synchronous SQLite on the event
  loop. Measured on the reference Pi it held the loop for 1.6 to 3.2 seconds,
  and for that whole time every other response waited: status polls, pages,
  everything, which is exactly the both-at-the-same-instant freeze the issue
  documents. The evidence trail — including why the rollup timer cannot even
  fire at the observed sixty-second spacing — is on the issue.

  The tier-scanning endpoints (`live`, `calibration`, `costs`, `history`,
  `battery/history`, `energy`, `bands`) now run in the server's threadpool,
  each on its own short-lived database connection (a memory-backed store — the
  test configuration — cannot be reopened, so it serves reads from its one
  connection as before). Both halves are measured:
  under WAL a reader on its own connection saw zero interference from writers
  on either reference filesystem, and opening a configured connection costs
  0.07 ms. Endpoints answering from memory or single rows stay on the loop.

- **A deployment's durability choice now reaches every connection**. 0.6.10's
  `synchronous = "normal"` was applied only to the primary connection, so the
  maintenance pass — the bulk of the writing, once a minute — kept fsyncing at
  FULL from the SQLite build's own default. Proved by execution on the
  production Pi: primary NORMAL, maintenance FULL. Every connection the store
  opens now carries the configured value.

## 0.6.11 — 9 August 2026

### Fixed

- **A bug in the battery mapper can no longer be filed as an inverter gap**
  ([#66](https://github.com/sjordan0228/arraysense/issues/66)). The wrap that
  converts a model's refusal into `SampleBuildError` enclosed the whole
  constructor expression, so a `ValueError` raised while *evaluating* an
  argument — a defect in this driver's own reading helpers, demonstrated with a
  NaN cycle count reaching `round()` — was converted too, and would have been
  recorded as an inverter outage and retried forever. The arguments are now
  evaluated before the wrap, so it covers construction alone, and a mapper bug
  surfaces loudly the way #42 established one layer up.

  No real reply reaches either path today — the guards drop malformed records
  earlier — so this closes a latent hole rather than a live one. Nothing about
  an installation's data changes.

## 0.6.10 — 9 August 2026

### Added

- **An installation can choose how durably a reading has to land.**
  `synchronous = "normal"` in the configuration syncs at checkpoint rather than
  on every commit. The default is `"full"`, which is what every installation did
  before this was a choice, so nothing changes for anyone who does not ask.

  This exists for flash storage. Measured on a Raspberry Pi through the store's
  own `append`, 200 polls cost **207 fsyncs at full and 7 at normal** — about
  thirtyfold fewer, or roughly 2.8 million fewer flash program cycles a year at
  an eleven-second cadence. On the same hardware the write itself went from
  7.5 ms to 0.6 ms.

  What it trades is bounded loss, not integrity: SQLite stays consistent either
  way, and an abrupt power cut discards the readings written since the last
  checkpoint — roughly the last five minutes at that rate. `"off"` is rejected
  rather than offered, because that one risks corruption instead of loss and is
  a different bargain altogether.

  A reading written under `normal` reads back identically; the setting changes
  when the write is durable, never what it contains.

## 0.6.9 — 9 August 2026

### Added

- **An installation can choose how it reaches the inverter**
  ([#41](https://github.com/sjordan0228/arraysense/issues/41)). Until now the
  driver dialled the WiFi dongle and nothing else. Newer dongle firmware closes
  the local TCP port that depends on, and the Ethernet dongle never had it, so
  somebody buying an inverter today could be unable to run this at all.

  `transport = "modbus_serial"` with a `serial_device` now reaches the inverter
  over a USB-to-RS485 adapter instead. An installation that sets nothing keeps
  the dongle and behaves exactly as before, and a serial one is no longer asked
  for a dongle address it does not have.

  Measured on the reference inverter: a full poll of 90 readings takes about
  3.6 seconds against the dongle's 12 to 17, and the serial link runs alongside
  the dongle without either disturbing the other. The transport is
  latency-limited rather than transfer-limited — a Modbus transaction costs
  about 250 ms whether it carries one register or thirty-two — so the way to
  make it faster is fewer, larger reads, not a higher baud rate.

  This adds `pyserial` to the runtime dependencies, the first addition in a long
  while, because the library's serial transport cannot open a port without it.

### Fixed

- **A serial installation is checked against the inverter it claims.** Readings
  are filed under the serial in the configuration, and the dongle made that safe
  by refusing any reply whose serial did not match — a typo produced no data
  rather than misfiled data. Modbus offers nothing equivalent: a request selects
  a unit by address, and whichever inverter holds that address answers.

  A serial installation now reads the inverter's own serial before it takes a
  single reading, and refuses to collect if it disagrees. That refusal stops the
  service rather than being recorded as a gap, because a mistyped serial cannot
  come right on its own and every poll it survived would add another row to
  another machine's history.

  Checked once, at startup. Doing it per poll would have been better and is not
  possible: any successful register read resets the counter the library uses to
  decide a dead bus needs reconnecting, so a repeated check would have quietly
  disabled its own recovery. What that leaves uncovered — rewiring the bus to a
  different inverter without restarting — is written down beside the check
  rather than left to be discovered.

## 0.6.8 — 9 August 2026

### Fixed

- **A bug in our own decoding was being filed as an inverter outage**
  ([#42](https://github.com/sjordan0228/arraysense/issues/42)). The collector
  recognised "the driver could not turn this reply into a sample" by catching
  `ValueError`. That is what a sample raises when it refuses malformed data — and
  also what `int("")`, `float(None)`, an unpack of the wrong length and a failed
  date parse raise. Any of those, anywhere in a driver's read, was recorded as a
  gap and retried with backoff forever, looking exactly like an inverter that had
  stopped answering.

  Refusals now raise a `SampleBuildError` the driver puts on them, and only that
  is caught. A bare `ValueError` reaches the poll loop, is logged with its
  traceback, and stops the collector so the watchdog and systemd can see it —
  which is what should have happened all along.

  Two statements softened in 0.5.4 because the code could not support them have
  been settled rather than left hedged. One was removed as still untrue: a
  refused reply is deterministic for *that* reply, but a later reply may be fine,
  so the fault is not permanent and the page no longer implies it is.

  Nothing about an installation's data changes and no migration is needed.

## 0.6.7 — 9 August 2026

### Fixed

- **A page request could commit the collector's half-written reading.** Every
  request that needed the installation's timezone — which includes every call to
  `/api/status` — built a settings reader, and building one executed
  `CREATE TABLE IF NOT EXISTS settings` and then committed. That commit landed on
  the connection the collector shares with the web server, and on a SQLite
  connection a commit is not scoped to whoever issued it: it ends whatever
  transaction is open. A reading being written at that moment would be committed
  half-formed by a page doing nothing but asking what time zone to draw in.

  The settings table is now created once at startup with the rest of the schema,
  and a settings reader only reads. It is the same hazard fixed in 0.6.5 for the
  rollup pass — one connection, two threads, one transaction between them — found
  on the request path this time.

  Nothing about an installation's data changes and no migration is needed;
  existing settings are left exactly as they are.

### Changed

- **The durability of a stored reading no longer depends on how SQLite was
  built.** Raw samples are the one thing here that cannot be reconstructed, and
  their `synchronous = FULL` guarantee was inherited from SQLite's compile-time
  default rather than asked for. Both machines this runs on default to FULL
  today, so nothing was actually at risk — but a package built with
  `SQLITE_DEFAULT_SYNCHRONOUS=1` would have quietly weakened it with no test able
  to notice. It is now set explicitly, and the test forces a connection to the
  weaker setting first to prove the setting is doing the work.

- **A maintenance connection stops re-declaring WAL journalling.** WAL is
  persistent state in the database file, established once when the store opens.
  Asking for it again every sixty seconds set state that was already set, and
  took locks to do it.

## 0.6.6 — 9 August 2026

### Changed

- **Rollup maintenance rebuilds three hours of the past instead of forty-eight**
  ([#60](https://github.com/sjordan0228/arraysense/issues/60)). 0.6.5 moved the
  once-a-minute pass off the event loop, which stopped it freezing open pages but
  left it doing the same work: re-deriving two days of hourly buckets every sixty
  seconds. The window was wall-clock, so the rows inside it were set entirely by
  how often the inverter is polled — halve the interval and the work doubles,
  permanently. On the reference box that is 10,842 raw rows a pass today, and it
  is the reason a direct RS485 transport could not simply be dropped in: at a
  two-second cadence the same window holds around eight times as many rows and
  the pass no longer fits between the polls it runs beside.

  Nothing needed those forty-eight hours. The constant's own comment argued for
  "a few hours" while the value was sixteen times that, and the gap was never
  explained. Every writer that can dirty an hourly bucket was checked instead of
  assumed: readings and gaps are stamped when the poll happens, a held battery
  block is dropped rather than back-dated, the store's upsert can only replace a
  row from the instant it was stamped, and both the SolarAssistant importer and
  the out-of-bounds scrub write every tier they touch directly rather than
  leaving a later rebuild to notice. None of them reaches back beyond the hour in
  progress.

  Existing data is untouched and no migration is needed. One edge is worth
  knowing: a pass reads raw at the moment it runs, so if the service dies less
  than a minute after one and then stays down longer than three hours, that
  single hour's average is built from marginally fewer rows than it holds. The
  bucket's sample count records it, the raw readings are still there, and the
  minute tier has had the same bound since it was written.

## 0.6.5 — 8 August 2026

### Fixed

- **The once-a-minute rollup no longer freezes every open page**
  ([#30](https://github.com/sjordan0228/arraysense/issues/30)). Maintenance
  rebuilt the coarse tiers with synchronous SQLite inside an `async def`, on the
  event loop the HTTP API runs on. Measured on the reference box, `/api/status`
  answered in 4 ms at the median and then stalled for **1141 ms**, twice in a
  150-second window — once per pass, landing on every open page, every chart
  request and the collector's own read at the same instant.

  The pass now runs in a thread, on a second database connection it opens and
  closes itself. The second connection is the part that matters and is not
  obvious: on a SQLite connection `with conn:` is transaction state rather than
  a lock, so two threads entering it on one connection share a single commit and
  a single rollback. The collector's per-sample commit would have landed a
  half-built tier, and a failed rollup would have rolled back a reading that had
  stored successfully — losing a reading being the failure this project exists
  to prevent.

  A pass still delays *this collector's* next poll, and that is the intended
  trade rather than an oversight: a slow rebuild costs the collector its lock
  instead of costing every open page its response. What the pass costs has not
  changed; where it is paid has.

  Nothing about an installation's data changes, and no migration is needed.

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
  name of an unreachable inverter or a failing disk.

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
