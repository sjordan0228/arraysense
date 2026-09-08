# Overnight battery planner — design

Issue [#242](https://github.com/sjordan0228/arraysense/issues/242). Decisions in this
document are the packet author's (user-approved 2026-09-07); coders implement them, they do
not re-derive them.

## The question

At bedtime the owner asks: "will the battery get us through to morning?" A percentage does
not answer that. This feature projects the battery from now to a chosen end time under
load scenarios the owner can compare, states its assumptions, and refuses precision the
inputs do not support.

## User decisions (2026-09-07)

1. **End time**: 07:00 next morning by default, owner-adjustable (a setting, `overnight.end_hour`, integer 0–12, default 7).
2. **"Comparable nights"**: the last 7 nights, median load curve. Simple and explainable beats clever.
3. **Placement**: a new page, linked from the dashboard navigation.
4. **Scheduled load**: generic (start, duration, power or energy), with an optional Emporia-backed circuit picker when the module is enabled.

## Inputs — verified anchors at base `b125348`

| Input | Source | Anchor / shape |
|---|---|---|
| State of charge now | `store.latest` per device | `battery_soc_pct` (one bank in the reference install) |
| Usable capacity | summed per-module | `battery_full_capacity_ah` (`models.py:59`); usable = full × (100 − min_soc)/100 |
| Reserve floor | registry setting | `battery.min_soc_pct` (`settings.py:1014`, default 10.0) — "the floor the inverter is configured to hold; usable capacity ends here" |
| Charge limit | registry setting | `battery.max_charge_a` (`settings.py:1023`) |
| Discharge limit | observed, not configured | p95 of recorded `battery_discharge_power_w` over the last 7 nights; stated as an assumption |
| Round-trip efficiency | registry setting | the "Round-trip efficiency" spec (`settings.py:~1000`) — measured reference figure, applied to discharge (energy out of the battery costs ÷efficiency reaching the loads) |
| Historical consumption | minute tier | `load_power_w` — already the honest house load: derived from reg 170 `output_power`, EPS fallback, library `load_power` deliberately avoided (`source.py:_house_load`) |
| Calibration / drift | `calibration.py` via `/api/calibration` | severity ladder (ok/warning/elevated/alert) + `last_full_charge` |
| Solar tail | forecast engine | `forecast.py expected_points` / `/api/forecast` rows for the sunrise tail; when unavailable, proceed without solar and say so |
| Emporia circuits (optional) | `modules/emporia` | `Reading` (`parse.py:275`, `watts: None` means "did not answer" and must survive as silence), circuit histories via `/api/emporia/history` |
| Zone / DST | `energy.py` | `resolve_zone` / `with_zone` — every calendar cut in the installation's zone |

## The model

Time-stepped simulation, **5-minute steps**, from now (aligned to the step grid) to the end
time, in the installation's zone. Per step, for each scenario:

```
load_t        = scenario load at local clock time t        (watts)
solar_t       = forecast/actual solar charging at t        (watts, may be 0 overnight)
discharge_t   = clamp(load_t / efficiency − solar_t / efficiency, 0, discharge_limit)
charge_t      = clamp(solar_t / efficiency − load_t / efficiency, 0, charge_limit)  if surplus
soc_{t+dt}    = soc_t − (discharge_t − charge_t) × dt / (3600 × usable_capacity_ah) × 100
```

- `soc` clamps to [min_soc, 100]. **Grid-available assumption**: when `soc` reaches
  `min_soc`, the battery holds there and the model records `import_start` — the household
  begins importing; not an outage, because the grid serves the loads.
- Steps are charged by real overlap: the walk starts on the grid at or before now, but the
  first step is weighted by the seconds it has left after now, and the last by the seconds
  left inside the end time. A plan that starts at 22:02 is charged for 22:02 onward, not
  for the whole 22:00 step (which would overstate by up to a step).
- **Grid-outage assumption**: loads beyond battery capability are dropped, and the model
  records `outage_duration` — how long the *backed-up* loads alone would have been served
  until `min_soc`.
- Backed-up load = the scenario's load unless Emporia supplies circuit kinds; unmonitored
  consumption is a stated watts allowance the owner sets, never silently zero.
- Nested circuits (a parent and its child both monitored) count once: sum leaves only, or
  parents only, by Emporia's own device tree — never both.

## Scenarios

1. **Typical overnight use** — median per-step `load_power_w` across the last 7 nights,
   aligned by local clock time. The window is the last 7 **consecutive** local nights ending
   with the most recent night that has any rows: a silent night sits inside the window and
   makes itself unusable rather than letting the median reach back past it into older
   history. A night is usable when it answers at least half of its **own** real steps —
   288 normally, 300 across a fall-back, 276 across a spring-forward, walked from the
   dates rather than assumed from a flat day — and fewer than 3 usable nights means typical
   is unavailable and the page says what is missing. A step that did not answer is neither
   filled with zeros nor read as no load: the last answered step carries forward across it,
   seeded from the whole curve read cyclically (the nearest answered clock key below the
   window's start, wrapping through the end of the curve), so a window with no answered key
   still carries the day's last answer instead of a zero. Rows are bucketed on the shared
   five-minute grid (counted once per bucket, duplicates averaged), and a night whose rows
   all read silence still names the window's most recent night: it holds its slot in the
   window, unusable but not gone.
2. **Essential loads** - Emporia circuit selection when the module is enabled (measured
   histories for those circuits, unmonitored allowance added on top); without Emporia, a
   manual constant-watts entry. The label splits the two halves of the sum: a circuit curve
   is a **measurement**, and the watts added on top at every measured step are a **stand-in
   for load nobody meters**. It is not a statement that the whole sum was invented.
3. **Typical + scheduled load** - the scheduled load (start, duration, power or energy)
   added *as a delta* on top of scenario 1. **The delta is applied by instant, not by clock
   key**: `scheduled_windows` turns a start and a duration into the real five-minute windows
   the schedule covers, and the simulation charges a step when that step's own instant falls
   inside one. A run whose clock hours sit inside a fall-back hour therefore pays for the
   seconds it actually runs and not for both passes through the repeated hour, and a run
   over a spring-forward gap steps over the hour that never happened. Each window also
   carries its exact overlap seconds (a run that starts mid-step covers only the part of
   that step it really runs through), and a step is charged `watts * overlap_s / step_s`,
   so a one-hour scheduled run costs exactly one hour of energy however the grid falls.
   **Double-count rule**:
   when Emporia history shows that circuit already drawing during the scheduled window on
   the baseline nights, the baseline's overlapping contribution is stated in the result
   ("this window already carries ~X W of this circuit on a typical night"), and the delta
   applies only beyond it. Without Emporia the planner says plainly that it cannot know
   whether the load is already in the baseline and adds the full amount with that caveat.

## Honesty rules (the acceptance criteria, made concrete)

- **Drift**: calibration severity `warning` widens the SoC band only when a drift
  magnitude comes with it. `build_plan` takes `drift_band_pct`, the largest state-of-charge
  disagreement between the packs, and uses it as the width: the curve is reported as that
  band around the estimate and the reserve range grows at both ends by the crossings that
  belong to the two ends of the band. A widened range therefore rests on something measured
  and names where it came from. A warning with no magnitude leaves the range at the spread
  between nights and says the drift is missing from it. `elevated`/`alert` return
  `estimate_unavailable` with the reason, not a number.
- **Unsupported capacity** (`full_capacity_ah` absent), an efficiency that is not a positive
  number, a reserve floor at or above full charge, and a horizon shorter than one step are
  all refusals, and `plan_status` knows them so the API slice can gate before simulating. A
  projection that refuses for one of those reasons is propagated: the summary carries that
  status and that reason, with no range and no scenarios derived from a curve that never ran.
- **No point exhaustion times**: reserve crossing is reported as a **time range** whose
  width is the p25 to the p75 of the per-night crossing times, interpolated linearly
  between them in real instants — crossings are converted to UTC before sorting and
  interpolation, because two clock readings inside a fall-back hour are not one clock hour
  apart — until slice 2's replay error distribution replaces it with something earned. Nights
  that never reach the floor inside the horizon are censored rather than dropped: with more
  than a quarter of them the range is reported open at the later end, and with all of them
  there is no range at all.

- **Probability language is forbidden** until the replay evidence supports it; the words
  are "range", "assumption", "estimate".
- **Freshness**: SoC staleness and forecast age are surfaced per the existing staleness
  verdicts, and stale inputs narrow what the page claims.

## Replay validation (slice 2 — the self-test)

For each eligible past night: run the projection from 22:00 local using only information
available at that moment — the typical curve built from *prior* nights only, and the
recorded solar as the forecast stand-in for the sunrise tail (the replay method says so,
because archived forecasts do not exist). Compare projected vs actual: runtime error and
reserve-crossing error per night, reported as a distribution. Those distributions are what
let later slices call a range "earned".

## API (slice 3)

`GET /api/overnight?end=<ISO>&scenarios=typical,essential,scheduled` — read-only,
unauthenticated (the #34 decision). Returns per scenario: the trajectory, reserve-crossing
range, import-start range, outage duration (when the outage assumption is requested),
assumption list, freshness and drift annotations, and guidance states. No hardware changes
of any kind; the planner is advisory and touches nothing the charger guard owns.

## Page (slice 4)

New page ("Overnight", dashboard nav): the projected SoC curve with the reserve line,
scenario comparison (the delta each scenario makes), assumptions rendered as text,
freshness and drift states, setup-guidance states. Node seam tests for the rendering
decisions.

## Slices

1. **Core engine** — `overnight.py`: typical curve, the three scenario builders, the
   simulation, reserve-crossing/import-start summary, calibration input. Pytest red-first.
2. **Replay + uncertainty** — the backtest harness, error distributions, DST tests.
3. **API + outage assumption** — the endpoint and the grid-outage/backed-up-loads model.
4. **Page** — the frontend slice.

Each lands on `dev` through the pipeline (flashnext → session grading → codex → kat), with
a status comment on #242. The issue closes only when the whole feature reaches production.
