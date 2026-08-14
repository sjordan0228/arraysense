# HTTP API

Everything the dashboard shows is served from this API, and nothing in it talks to
the inverter. The collector owns the one connection the dongle allows, so the API
reads only from the database — which means you can query it as hard as you like
without affecting collection.

The service listens on port 8080 by default. Authentication is optional and off
until a password is set; when it is on it protects the write endpoints only, and
the password travels as plain HTTP, so it stops other devices on the network
changing things by accident or mischief rather than anyone who can watch the
traffic. Run it on a network you trust, or put a reverse proxy in front of it.

Interactive documentation generated from the code is at `/docs`.

## `GET /api/status`

Whether the collector is running, connected, and keeping up.

```json
{
  "version": "0.1.0",
  "running": true,
  "connected": true,
  "yielding": false,
  "yield_until": null,
  "last_success": "2026-08-06T20:44:56+00:00",
  "last_failure": null,
  "last_error": null,
  "consecutive_failures": 0,
  "total_samples": 7854,
  "total_failures": 3,
  "started_at": "2026-08-06T00:00:00+00:00",
  "timezone": "America/Chicago"
}
```

`consecutive_failures` is the field to alert on. A single failure is ordinary — the
dongle drops connections — but a climbing count means the retry backoff is not
recovering.

`timezone` is the zone this service would cut a calendar answer on, by the
precedence below: the setting, then the optional `tz` query parameter, then the
machine's own zone. It is here so a caller can ask *before* asking anything else —
"which calendar are you going to answer on" has to be settled before a request for
"this month" can be written, and a caller that decides for itself has made a second
copy of the precedence rule. An unresolvable `tz` is stepped past here rather than
refused, so this endpoint always answers.

## `GET /api/live`

The most recent inverter reading and every battery module's latest, as real-world
values.

```json
{
  "inverter": {
    "timestamp": "2026-08-06T20:44:56+00:00",
    "pv_total_power_w": 7614.0,
    "battery_soc_pct": 64.0,
    "radiator1_temperature_c": 68.0,
    "error": null
  },
  "modules": [
    {"timestamp": "...", "serial": "Battery_ID_01", "soc_pct": 60.0, "cycle_count": 459.0}
  ]
}
```

Every registered metric is included rather than a chosen subset — it is one row from
one table either way. A metric the inverter did not report is `null`, never `0`.

`error` on the inverter object is non-null when that poll failed to reach the
inverter. Such a row is a recorded gap and should be drawn as a break in a chart, not
smoothed over.

## `GET /api/capabilities`

What each device is and which metrics it produces, from the driver's own
declaration. Nothing is read from the inverter to answer this.

```json
{
  "devices": [
    {
      "device": "CE12345678",
      "driver": "eg4_luxpower",
      "model": null,
      "pv_strings": 3,
      "energy": "counted",
      "backup_output": true,
      "generator_input": true,
      "split_phase": true,
      "three_phase": false,
      "parallel_capable": true,
      "per_module_battery": true,
      "metrics": ["pv_total_power_w", "pv1_power_w", "..."],
      "battery_module_metrics": ["soc_pct", "voltage_v", "..."]
    }
  ]
}
```

`metrics` names what this device can report at inverter level, and
`battery_module_metrics` the bare per-module names it produces, both in the
registry's order. `/api/battery/history` takes names in this bare form — it accepts
any registry template, but only the ones listed here can ever hold data for this
device. A metric absent from these lists is one the hardware cannot produce —
different from a metric listed here that reads `null` on `/api/live`, which the
device can produce and did not report. Render from these lists rather than
hard-coding names, and a one-string inverter stops showing empty charts for strings
it does not have.

`energy` is `counted` when every kWh figure is a counter the inverter keeps itself,
and `estimated` when it could only be integrated from power samples. An estimate
misses whatever happened during a collection gap; the two must not be presented with
the same authority.

`model` is `null` when it has not been established — nothing reads the model register
today — rather than filled with an assumption.

The reply has three states, because an unknown declaration is not an absent device.
A driver that describes itself gets the full entry above. A source that names its
device but declares nothing gets an entry with its serial and `null` for `driver`,
`model`, every flag, `metrics` and `battery_module_metrics` — null meaning "not
established", where an empty list would claim a device known to produce nothing.
Only when no device is configured at all is `devices` empty.

## `GET /api/history`

Inverter metrics over a time range.

| Parameter | Meaning |
| --- | --- |
| `start`, `end` | ISO 8601 timestamps |
| `metrics` | comma-separated metric names |
| `width` | the chart's width in pixels, default 1000 |

```json
{
  "tier": "hourly",
  "count": 144,
  "points": [
    {"timestamp": "2026-08-06T20:00:00+00:00", "pv_total_power_w": 7614.0, "error": null}
  ]
}
```

An unknown metric name is a `400` naming the offending entry, rather than an empty
result that looks like "no data".

`width` chooses the resolution. A month at one-minute resolution is 43,200 points for
a chart perhaps a thousand pixels wide; asking for the width lets the server serve the
hourly tier instead, which measured at 2 ms against 107 ms and looked identical.
`tier` reports which resolution you actually got, so a caller is never guessing.

Rows from a rollup tier also carry `sample_count`, the number of full-cadence readings
behind that point. A point built from three samples where sixty were expected is
mostly a gap, and worth drawing differently.

## `GET /api/battery/history`

The same, per battery module. Takes bare module metric names — `soc_pct`,
`cell_max_voltage_v`, `remaining_capacity_ah` — and an optional `serial` to restrict
to one pack.

Modules are keyed by serial number, never by slot. The inverter rotates modules
through four register slots when a bank has more than four, so a slot index is
positional metadata and not an identity.

## `GET /api/calibration`

How far the per-pack state-of-charge estimates have drifted from reality.

```json
{
  "severity": "elevated",
  "days_since": 29.4,
  "last_full_charge": "2026-07-08T18:22:00+00:00",
  "searched_days": 60.0,
  "soc_is_estimate": true,
  "wiring_suspect": false,
  "soc_spread_pct": 19.0,
  "voltage_spread_mv": 30.0,
  "headline": "State of charge estimates are drifting",
  "detail": "29 days since the bank last reached full. ..."
}
```

Each pack estimates its charge by counting amp-hours and cannot correct itself until
it charges fully, so the useful question is not what the packs say but how long it has
been since anything forced them to agree with reality.

A full charge needs evidence from the inverter's terminals **and** from the packs'
counters, because either alone gives a wrong answer: absorb voltage without the packs
agreeing is a charge that was cut short, and a pack reading 100% with no absorb behind
it is a counter that has drifted high, which is the very condition being detected.
What the two halves trade off is *duration*. A bank that holds at its BMS charge
reference for twenty minutes needs only every pack to have peaked full within that
window. A bank that finishes faster — the reference installation crosses absorb and
tapers to zero in about three minutes — has to show the packs *arriving* at full
together, which means a transition per pack: **every** pack the bank is known to hold
measured below 99% in the quarter hour before the absorb, and every one of them at or
above 99% at a single instant during it, with the current settled.

Both halves are per pack because a charge resets every counter. Asking only that some
pack had been below full is not enough: three counters drifted high and pegged at 100%
beside a fourth that genuinely charged would satisfy it, and three quarters of the
percentages on screen would then be stale ones drawn as measurements. Counters cannot
independently drift across the bar inside a couple of minutes, a bank whose counters
have all been pegged at 100% for weeks shows no transition at all, and the lookback
stops at the previous absorb so one charge cannot vouch for the next.

Neither door reports the reset itself, which nothing observes. The long door gives the
end of the absorb and the short one the instant the whole bank was first seen to have
arrived; both are late rather than early.

| `severity` | Meaning |
| --- | --- |
| `none` | a full charge within the last week |
| `info` | 7 days |
| `warning` | 14 days — roughly where drift exceeds the spread the vendor calls normal |
| `elevated` | 30 days, or no full charge found at all |
| `alert` | the packs disagree on **voltage**, which is a different problem entirely |

`soc_is_estimate` is the field worth acting on: once set, per-pack percentages are no
longer good enough to make decisions from and the dashboard marks them accordingly.

`alert` deserves separate handling. Packs wired in parallel are physically forced to
the same voltage, so a spread there is resistance in a cable, a lug or a busbar — or a
failing pack. Charging will not fix it, and reporting it as a calibration reminder
would send you after the wrong thing.

`days_since` and `last_full_charge` are `null` when no full charge was found in the
searched history. That is not the same as "never happened", which is why
`searched_days` says how far back the search looked.

## `GET /api/settings` and `PUT /api/settings`

Read and change the settings that live in the database rather than in the
configuration file.

```json
{
  "fields": [
    {
      "key": "display.temperature_unit",
      "kind": "choice",
      "label": "Temperature unit",
      "help": "Applies to every temperature on the page, on every device.",
      "choices": ["F", "C"],
      "lower": null, "upper": null,
      "secret": false, "default": "F",
      "max_length": 128, "multiline": false,
      "unit": "", "suggestions": [], "optional": false
    }
  ],
  "values": { "display.temperature_unit": "F", "connection.dongle_serial": "BA••••••60" }
}
```

The field descriptions travel with the values so a page renders its controls from
this rather than hard-coding labels, bounds and choices. Hard-coded copies drift
from the validation the moment either changes, and the drift surfaces as a control
offering a value the server then refuses.

Every field carries every key, empty where there is nothing to say, so a page reads
them unconditionally rather than branching on whether the server mentioned them.

- `unit` is what the number means — `"seconds"`, `"currency per kWh"` — rendered
  beside the control. The money settings name themselves as rates, because a box
  labelled `kWh` invites a rate to be typed as a quantity of energy.
- `suggestions` are offered and never enforced: a datalist, not a choice list. The
  currency has them because a closed list would make an unusual currency
  unrepresentable, and a value already typed must never be replaced by a suggested
  one. Use `choices` for what the server genuinely refuses anything else for.
- `optional` says the setting can hold *nothing*, distinct from any number it could
  hold. `site.latitude` is the case that forces it: `0.0` is a real place in the
  Gulf of Guinea, so an unset coordinate cannot be represented as zero. An optional
  setting reads back as `null`, and is cleared by sending `null` or `""`. Sending
  `0` sets it to the equator, which is a different statement.

## Where the installation is

`site.timezone` is an IANA name and decides where midnight falls and which
wall-clock hours a rate band covers. It is resolved against the tz database when it
is saved, so an unparseable zone is a `400` at the box it was typed in rather than
a history page that later cannot answer.

Zone precedence for `/api/energy` and `/api/costs` is **the setting, then the
request's `tz`, then the machine's own zone**. The installation is in one place
while the person looking at it may not be, and a bill drawn against a travelling
phone's midnight looks entirely normal while being wrong. Empty is the default, so
an install that has set nothing keeps following the browser exactly as before. Both
endpoints return the zone they actually used as `timezone` — on every path,
including the one where there is no tariff to price anything with — and so does
`/api/status`, which is where a caller asks the question first. Nothing is cached
anywhere, so a change takes effect on the next request.

That precedence is why the calendar bound of a range should be sent **naive**:
`start=2026-08-01T00:00:00` is read as midnight in whichever zone the service
resolved, which is the midnight the answer is cut at. An instant computed by the
caller — `2026-08-01T05:00:00Z`, midnight where the caller happens to be — is not
that midnight once the two zones differ, and asking about "this month" from the
wrong one loses the whole monthly connection charge, which falls due on the first
and is never apportioned. An instant is still the right thing for the *end* of a
range: it carries no calendar question and it is what says how much of the period
in progress has actually happened.

`PUT` takes a flat object of key to value and returns what changed:

```json
{ "changed": ["display.temperature_unit"], "restart_required": false, "values": { ... } }
```

Validation is all-or-nothing. A settings form posts every field together, and half a
form landing would leave the installation in a state nobody chose — so an invalid
value is a `400` naming the setting, and nothing is written. An unknown key is also
a `400` rather than a silently created setting nothing reads.

`restart_required` is true when a changed setting is not a display one. Display
settings apply on the next page refresh; the connection and poll settings are read
when the collector starts.

**Identifying values come back masked**, as `BA••••••60`. This is a read endpoint,
and reads stay open even when a password is set — a wall display never logs in — so
it answers anything that can reach the port. The
serials are not passwords — the dongle broadcasts its own as a WiFi network name —
but handing the full set to any device on the network is not a decision worth making
by accident. Masking rather than blanking keeps the page usable: you can confirm
which serial is configured without it being readable by someone who did not already
know it.

A masked value posted back unchanged is discarded rather than stored, so saving the
form without editing that field does not write bullets over the real serial.

### What stays in the file

`database_path` and the bind address remain command-line arguments. The service
cannot read settings out of a database before it knows where the database is, and
nothing sensitive is in that pair.

## Authentication

Optional, and off until a password is set. With none set, every write endpoint
behaves exactly as it does without this section. With one, the six write
endpoints — `PUT /api/settings`, `POST /api/setup/apply`, `POST /api/setup/detect`,
`POST /api/yield`, `POST /api/resume` and `POST /api/efficiency/backfill` — answer
`401` with `{"detail": "authentication required"}` unless the request carries a
session cookie. Reads are never protected, so the dashboard keeps working without
a login.

The password and the session cookie cross the network in plain HTTP, so the
protection is against other devices on the same network changing settings or
stopping collection by accident or mischief, not against anyone who can watch the
traffic. Sessions live in the service's memory, so a restart ends every session —
no loss for a display that only reads — and nothing about a session is written to
disk.

### `GET /api/auth`

Whether authentication is on, and whether this client holds a session. Open, so
a page can decide what to render.

```json
{ "required": false, "authenticated": false }
```

### `POST /api/auth/login`

Start a session in exchange for the password.

```bash
curl -X POST localhost:8080/api/auth/login -H 'Content-Type: application/json' -d '{"password": "…"}'
```

On success the reply sets an `HttpOnly`, `SameSite=Strict` session cookie and
returns `{"ok": true}`. A wrong password is `401`; after five wrong attempts
from one address, further attempts — including one with the right password —
are refused with `429` for a minute. The count is not a rolling window: it
accumulates until it reaches five, whenever those failures happened. Nothing is
counted before a password has been set, so a stranger cannot spend the
attempts the owner is going to need.

### `POST /api/auth/logout`

End this session and clear its cookie. Always `200`, even when nothing was
logged in.

### `POST /api/auth/password`

Set, change or clear the password.

```bash
curl -X POST localhost:8080/api/auth/password -H 'Content-Type: application/json' -d '{"new_password": "…"}'
```

Setting the first password needs no credential — there is none yet. Changing or
clearing an existing password requires the current one, verified, whatever the
session state. An empty `new_password` clears the password, which turns
authentication off and revokes every session. A new password shorter than 8
characters is a `400`. A wrong `current_password` is a `401`, and this endpoint
shares the login throttle, so five wrong guesses here also block login from that
address for a minute.

## `POST /api/yield` and `POST /api/resume`

Hand the dongle over and take it back.

```bash
curl -X POST localhost:8080/api/yield -H 'Content-Type: application/json' -d '{"seconds": 600}'
```

The dongle accepts exactly one TCP client, so the vendor's app cannot connect while
this service holds the socket. Yielding disconnects for a set period and reconnects
afterwards, which is how you run a firmware update without stopping the service.
`seconds` is capped at an hour; `POST /api/resume` reconnects early.

Collection stops for the duration. The gap is recorded rather than papered over, so
the chart shows a break where the data genuinely does not exist.

## `GET /api/efficiency`

How well the solar array is performing against what it should produce.

| Parameter | Meaning |
| --- | --- |
| `period` | One of `day`, `week`, `month`. Defaults to `day`. |
| `start` | The first day, as `YYYY-MM-DD`. Required. |
| `tz` | An IANA timezone name such as `America/Chicago`. Optional; the installation's configured zone is used when omitted. |

```json
{
  "configured": true,
  "period": "day",
  "start": "2026-08-06T00:00:00+00:00",
  "end": "2026-08-07T00:00:00+00:00",
  "now": "2026-08-06T21:30:00+00:00",
  "summary": {
    "expected_kwh": 12.4,
    "actual_kwh": 10.1,
    "curtailed_kwh": 1.8,
    "unexplained_kwh": 0.5,
    "unmodelled_gain_kwh": 0.0,
    "pr": 0.87,
    "specific_yield": 0.81,
    "tolerance_pct": 5.0,
    "partial": false
  },
  "waterfall": [
    {"name": "expected", "kwh": 12.4, "penalised": false},
    {"name": "unexplained", "kwh": -0.5, "penalised": true},
    {"name": "curtailed", "kwh": 1.8, "penalised": false},
    {"name": "unmodelled_gain", "kwh": 0.0, "penalised": false},
    {"name": "actual", "kwh": 10.1, "penalised": false}
  ],
  "strings": [
    {
      "id": 1,
      "summary": {
        "expected_kwh": 4.2,
        "actual_kwh": 3.1,
        "curtailed_kwh": 0.6,
        "unexplained_kwh": 0.5,
        "unmodelled_gain_kwh": 0.0,
        "pr": 0.83,
        "specific_yield": 0.74,
        "tolerance_pct": 5.0,
        "partial": false
      }
    }
  ],
  "hours": [
    {"hour": 0, "expected_power_w": 0.0, "actual_power_w": 0.0, "unexplained_w": 0.0},
    {"hour": 6, "expected_power_w": 1200.0, "actual_power_w": 980.0, "unexplained_w": 220.0}
  ],
  "worst_hour": 14,
  "baseline": {
    "start": "2026-07-01T00:00:00+00:00",
    "end": "2026-08-01T00:00:00+00:00"
  }
}
```

`configured` is `false` when no solar array has been described in settings. Every
other field is then `null` or an empty list, never zero.

`period`, `start`, `end` and `now` define the window being reported and the moment
the calculation ran.

`summary` contains the core figures:

- `expected_kwh` is what the array should produce under the measured sky conditions.
- `actual_kwh` is what the inverter actually delivered.
- `curtailed_kwh` is energy the inverter refused because the battery was full and
  the house was not drawing. It is NOT a loss and NOT a fault, so it does not count
  against the performance ratio — `penalised: false` on that segment in the waterfall.
- `unexplained_kwh` is a genuine shortfall with no cause attributed to it. It does
  count against the performance ratio.
- `unmodelled_gain_kwh` is production above the model that cannot be explained by
  known factors such as reflection or tracking.
- `pr` is the performance ratio: actual production divided by expected production,
  with curtailed energy removed from both sides. It is `null` when there is nothing
  to divide by.
- `specific_yield` is the actual energy per kilowatt of installed capacity.
- `tolerance_pct` is the acceptable deviation band around the expected value.
- `partial` is `true` when the day was not fully observed and its figures cover only
  part of it.

`waterfall` is an ordered list of segments showing how the expected value becomes
the actual one. Each segment has `name`, `kwh` and `penalised`. The names are
`expected`, `unexplained`, `curtailed`, `unmodelled_gain` and `actual`. A segment
with `penalised: true` counts against the performance ratio; `penalised: false`
means it does not.

`strings` provides the same summary shape for each configured string, so an
underperforming string can be located.

`hours` is an hour-by-hour breakdown for `period=day` only. It is `null` for `week`
and `month`. Each entry has `hour`, `expected_power_w`, `actual_power_w` and
`unexplained_w`.

`worst_hour` is the hour carrying the largest unexplained shortfall, or `null` when
there is no shortfall to report.

`baseline` is the window the model's fit came from, with `start` and `end` as ISO
timestamps.
