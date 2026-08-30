# The guided tour (sub-project 3 of 4)

Designed 2026-08-12, the third of four sub-projects agreed with the owner ahead
of announcing the project publicly. The first is
`docs/superpowers/specs/2026-08-11-installer-and-docs-design.md`; its closing
section named this one in one paragraph:

> The guided tour — a dismissible first-visit walkthrough, state-aware enough
> to skip what an installation does not have, with dismissal persisted as an
> unregistered settings key.

The owner's own framing, from the conversation that produced that list: after
the wizard, there should be a walkthrough of the pages, dismissible at any
time. This document works out what that means against the code as it stands
today, not as it is about to become — the last section names exactly where
this depends on sub-project 2, the wizard and settings expansion, which has
not been designed yet.

## What this builds

A first-time visitor who has just finished the setup wizard gets walked
through the pages that now have something to show them — and only those. A
returning visitor who dismissed the tour last week does not see it again
unless they ask for it. Every step is a fact the page already renders, pointed
at rather than restated.

## What it does not build

Not a product-tour library, not a generic "spotlight any element" framework
for later features to reuse, not an in-app help system, not anything that
touches the wizard itself. Four pages get tour content in this pass — Now,
Energy flow (both inside `index.html`), Graphs, and Costs — for reasons the
"what it covers" section gives. History, Efficiency and Settings get the
mechanism (they can be added to a step list) but not authored steps in this
pass; that is scoping, not a limitation of the design.

## The single most important finding: navigation is two different things

The five pages named in the brief are not five equal siblings. `index.html`
holds three of the eight `NAV` entries — Now, Energy flow, Inverter — as
**hash-routed views inside one document**. `wireTabs()` (index.html:1641)
swaps which `<div>` is hidden and rewrites `location.hash`; nothing reloads.
Graphs, History, Costs, Efficiency and Settings are **five separate HTML
documents**, reached by an ordinary `<a href="/graphs">` in the `NAV` array
(`common.js:732`) that the browser navigates to in full.

This decides the entire shape of a cross-page tour. Anything held only in a
JavaScript variable — the current step index, "the tour is running" — is
destroyed the instant the visitor's next step is on a different page, because
that page is a fresh document with a fresh `<script>` execution. A tour that
spans Now, Graphs and Costs has no choice but to persist its position
somewhere that survives a full navigation: `localStorage`, read again by the
next document's own boot sequence. There is no in-memory alternative; the
architecture forecloses it. Inside `index.html` itself, by contrast, three
tour steps can run back-to-back with no reload, driving `wireTabs()`'s own
switcher to change view.

The practical shape this forces: the tour is not one continuously-running
script. It is a short sequence, resumed from a stored position at the top of
every page's `boot()`, that either has a next step on *this* page (show it)
or does not (stay quiet, having recorded nothing).

## Whether "an unregistered settings key" is actually possible: it is not

I read `settings.py` and the write paths in `api/routes.py` looking
specifically for a way to store a value under a key nobody registered. There
isn't one.

- `SettingsStore.get`, `.set`, `.set_many`, `.clear` and `.update` all begin by
  calling `lookup_setting(key)` (settings.py:770), which raises `KeyError` for
  anything not in the `SETTINGS` tuple. There is no bypass.
- `PUT /api/settings` (routes.py:774) constructs a `SettingsStore` and calls
  `.update()`, which validates every key through the same registry before
  writing anything. A client cannot smuggle an unregistered key through the
  HTTP layer either.
- The `settings` table itself (`store/schema.py`, `SETTINGS_TABLE`) is a plain
  key/value table with no foreign key or check constraint — SQLite would
  accept any string as a key. The restriction is entirely a Python-level
  invariant, enforced identically everywhere a write can happen, and it is
  there on purpose: `describe()`'s docstring is explicit that a page renders
  itself from the registry, and a value with no `SettingSpec` is a value no
  page — settings.html included — can ever display, describe, or let anyone
  clear.

So the phrase in the sub-project list, taken literally, describes something
the codebase refuses to do, on every path that could do it. Two ways to
reconcile it:

1. **Register it properly.** Add a `SettingSpec` — `tour.dismissed`, boolean,
   default `false` — the same one-line change CLAUDE.md asks for any new
   setting. This makes dismissal **installation-wide**: `settings.py`'s own
   docstring is explicit that everything in the registry is "one value for the
   whole installation rather than per browser." One dismissal, from any
   device, would hide the tour from the tablet on the wall and every phone
   that has ever opened the dashboard, permanently, until someone visits
   `/settings` and clears a key that — being deliberately excluded from
   `describe()`'s intended audience of *things a person tunes* — would need a
   decision about whether it even appears there.

2. **Follow the pattern the codebase already uses for exactly this kind of
   fact.** Seven `localStorage` keys already exist, all prefixed `as.`, none
   of them registered in `settings.py`, all handling per-browser interface
   state: `as.tempUnit`, `as.refreshSecs`, `as.tab`, `as.detailOpen`,
   `as.histSpan`, `as.theme`, and — closest of all in shape to this problem —
   `as.calDismissed` (index.html:715), which remembers a *dismissed advisory*
   for 14 days per browser, distinguishing severity so a worse warning
   overrides an old dismissal. `settings.py`'s own docstring gives the reason
   these stay out of the registry: "a temperature unit stored in local storage
   means the tablet and the phone disagree about what 39 means" — stated as
   the *deliberate* behaviour for a display preference, not a bug. A tour
   dismissal is the same kind of fact: whether *this browser* has seen the
   walkthrough, not a property of the installation.

**Decision: localStorage, following the `as.calDismissed` precedent, not a
registered setting.** The word "unregistered" in the original brief reads, in
light of what the code actually enforces, as "not put through the ceremony of
registering a `SettingSpec`" — and a `localStorage` key already satisfies that
without needing anything invented to make it true. The alternative — a
registered, installation-wide flag — is available if the owner actually wants
one dismissal to speak for the whole house; it is a two-line change if so
(one `SettingSpec`, one exclusion from whatever list `settings.html` renders
as tunable). I have not built it, because nothing I read argues for
installation-wide over per-browser, and per-browser is what every comparable
piece of interface state in this codebase already does. **This is the one
design decision in this document I'd flag hardest for the owner to confirm or
overrule** — it is a guess about intent dressed as a technical finding.

Key name: `as.tourStep`, holding either nothing (never started or fully
finished — see below for why those two collapse into one state) or a step
identifier the tour resumes from. Not a boolean: "dismissed" and "finished"
need to be the same terminal state or a completed tour looks identical to a
skipped one and re-offers itself, which the owner's "dismissible at any time"
does not ask for and a returning visitor would find irritating.

## State-aware: the specific conditions, each tied to what answers it

"State-aware" cannot be a design aspiration; it has to be a list a step
either passes or does not. Reading `/api/capabilities`, `/api/status`,
`/api/setup` and `/api/settings`, here is what a step can condition on and
where the answer comes from:

| Condition | Source | What it gates |
| --- | --- | --- |
| How many PV strings exist | `capabilities.devices[0].pv_strings` | A step describing "String 3" on a one-string installation must not run. `pv_strings` is `null` when the source is bare (declares nothing) and a number otherwise — `null` must not be read as zero strings, following the same rule the existing `capStrings` function in common.js already applies (see below). |
| Whether the bank reports per-module data | `capabilities.devices[0].per_module_battery` | Gates a step pointing at the four module cards on Now — a battery relayed only in aggregate, or absent, has none. |
| Whether energy is metered or estimated | `capabilities.devices[0].energy` | `"counted"` vs `"estimated"` vs `null`. A step that says "these are the inverter's own lifetime counters" is a false claim on a family without `EnergyReporting` (the EG4 3000 EHV, per CLAUDE.md) and must say something true instead, or skip. |
| Whether EPS/backup output exists | `capabilities.devices[0].backup_output` | Gates any step over the Legs panel — `index.html:1399` already gates the same panel on this exact field (`caps.backup_output !== false`), so the tour's rule is the rendering rule, read from the same place. |
| Whether the collector has ever written a row | `status.staleness.any_rows` (nested under `/api/status`) | The sharpest signal for "this is a genuinely fresh install with nothing on the charts yet" — see the fresh-install section below. |
| Whether the collector is connected right now | `status.connected`, `status.staleness.verdict` | A step over the Now page's live cards should not claim "this updates every few seconds" while the verdict reads `stopped` or `inverter`-faulted; it can still run, worded to match what is actually on screen (a dash), never promising liveness the page cannot currently back up. |
| Whether a tariff is entered | `settings.values["tariff.bands"]` from `/api/settings`, non-empty after trimming | Gates every Costs step. Costs already renders this exact branch itself (`costs.html:128`, the `#notariff` panel, driven by the same emptiness) — the tour reads the identical source rather than asking `/api/costs` and inferring absence from missing money fields, which would be a second, weaker implementation of the same check. |
| Whether an array is described at all | `settings.values["panels.strings"]`, non-empty | Gates Efficiency's steps in a future pass — `efficiency.html`'s own `#noconfig` panel (line 105) is driven by exactly this. |
| Whether a location is set | `settings.values["site.latitude"]` / `["site.longitude"]`, non-null | Gates any step mentioning the forecast panel or weather, which `collector/weather.py` only populates once a location exists. |
| First-run vs already-configured | `/api/setup`'s `first_run` | Not a tour gate directly — the wizard already owns this branch (`index.html:2023`) — but it is *when* the tour is first offered: the moment `first_run` flips from true to false is the one point in an installation's life that is unambiguously "just finished setup," which is the trigger the next section builds the offer around. |

Two of these deserve a specific note because getting them backwards is exactly
the failure mode CLAUDE.md spends a full section warning about.

**`pv_strings: null` is not zero strings — it is an unknown number of
strings**, and a step gated on it must fail closed (skip) rather than fail
open (show, describing a string that is not there) or fail by assuming the
maximum. This already has a tested precedent to copy rather than reinvent:
`common.js` carries `capStrings` and `capHasMetric` between the `// >>> caps-
logic` / `// <<< caps-logic` markers (common.js:405–487), and
`tests/test_dashboard_caps_js.py` runs that exact slice under `node` against
three fixture shapes — a full three-string 18kPV, a reduced one-string
machine, and a bare source declaring nothing at all — checking specifically
that "unknown" and "zero" render differently. A step-gating function for the
tour should sit in the same file, follow the same shape, and ideally call
these two functions rather than re-deriving the same branch a third time.

**A tariff that fails to parse is not the same as no tariff entered.**
`costs.html`'s `renderAssumptions` (line 883) already distinguishes
`hasTariff` from `unreadable` — a tariff that exists but the service could not
read right now gets a different sentence than one that was never typed. The
tour only needs the first distinction (typed vs not), because it is deciding
whether to run a Costs step at all, not whether to render money — but it
should read `tariff.bands` from `/api/settings` rather than `/api/costs`,
because `/api/settings` answers "was anything entered" directly, while
`/api/costs`'s absence of money fields conflates "nothing entered" with "the
month has not started yet" and "the parser is currently unhappy." Asking the
narrower question at its own source avoids inheriting ambiguity that belongs
to a different endpoint.

## Where the tour's content lives, and why duplication is not accepted here

CLAUDE.md's clearest instruction on this whole project is "nothing is
computed in two places," and a tour is, by nature, a second description of
things the page already shows. The Costs page's own history — a second
tariff parser that disagreed with the first within a day — is the concrete
cost of getting this wrong, and it happened with numbers; a tour getting it
wrong is quieter but the same shape of failure, and worse to catch, because a
sentence that has quietly gone stale produces no error, just a walkthrough
that is lying about a card it is pointing directly at.

The decision: **a tour step never states a number, a threshold, or a fact
already computed elsewhere. It only names and points.** "This card shows how
much your solar strings are producing right now" is safe, because it is true
regardless of what the number is, what the metric bounds are, or which
strings exist. "You have three PV strings" is not — that is `pv_strings`
restated in English, and the day the array grows a fourth string the tour
sentence is wrong while the card beside it is right. Every step's copy was
checked against this rule while writing the step list below: any step that
could not be phrased without restating a number or a capability value was
either dropped or rewritten as a pointer only.

The gating conditions themselves (the table above) are not an exception to
this rule — they are the rule applied to *whether a step runs at all* rather
than to its wording, and they are read from the same endpoints the pages
already read, not duplicated as separate thresholds. There is nothing in this
design analogous to metric bounds or tariff bands that would need a second
source of truth; the tour's only "data" is prose, and prose that never quotes
a number cannot drift from one.

This also settles where the step definitions live: as a static list in
`common.js` (or a new `tour.js`, loaded the same way — see the deployment
note below on why a new file needs a decision, not an assumption), each entry
naming a `page`, a DOM selector to anchor to, copy, and the gate condition
from the table above. Nothing server-side renders or serves this list; there
is no metric-registry-style single source to point at because there is no
underlying data to keep it in sync with — only the *decision of whether to
show it*, which is kept in sync by reading the same capability and settings
endpoints the pages themselves already read for the same purpose.

## Fresh install, no data yet: the default case, not the edge case

The tour's very first opportunity to run is the moment right after the
wizard hands off to the dashboard — `applyAndWatch` (index.html:1989) posts
the new configuration, waits for the restart, and calls
`location.assign('/')`. At that instant `status.staleness.any_rows` is
`false`: the collector has just started and has not completed a single poll.
Every chart on Now is either freshly built with nothing to plot or still
showing its `chartMessage(..., 'no data in this range')` placeholder
(`index.html:1487`, `:1535`, `:1665`; the identical pattern recurs in
`graphs.html:571`). Costs, if a tariff happens to already be set, shows the
`renderEnergy` fallback text: "Nothing has been recorded this month yet."

This is not a state the tour has to detect and route around specially — it
falls out of the anchor-only design above for free. A step that says "this
card shows your battery's state of charge" and points at the SOC panel is
exactly as true whether the panel holds a chart or a "no data in this range"
placeholder, because the step never asserts what the panel currently
contains. The one place this needs an explicit rule rather than an accident:
**the tour must anchor to a DOM element that exists in the page's static
markup**, not one only JavaScript injects after a successful fetch. I checked
this holds for every page in scope — `#cards`, `#notariff`, `#noconfig`,
`#power`, `#soc`, `#battpower` and the four module card slots are all present
in the served HTML with `hidden` toggled by script, never created by script —
so a tour step can safely point at any of them before a single API response
has come back. This is also why the tour does not need to wait for
`loadLive()`, `loadHistory()` or any other page fetch to resolve before
starting: it operates against the skeleton, which is complete at parse time.

The one condition that should suppress the tour outright rather than run it
against an empty page: `status.running === false` or a `staleness.verdict` of
`not_running` — not "no data yet," which is normal and expected seconds after
setup, but "the collector never started," which is a fault the tour would
otherwise walk someone through a broken installation as if it were working.
That is the stale-banner's job to say loudly elsewhere on the page; the tour
should simply decline to add commentary on top of it.

## Does the tour interrupt, or offer itself — and what happens on return

**It offers itself once, unforced, and never interrupts.** A full-screen
takeover immediately after the wizard would be the third consecutive blocking
screen a new owner has sat through (the installer's own confirmation prompt,
then the wizard, then this), and CLAUDE.md's account of the wizard replacing
the whole dashboard because "there is no data to show and nowhere else to go"
is a justified interruption for exactly that reason — no data exists yet, so
there is nothing to protect the visitor from missing. The tour has the
opposite problem: the dashboard *is* usable the moment it renders, even
mostly empty, and forcing a walkthrough in front of it teaches "wait for
permission before touching anything," which is not this project's tone
anywhere else.

Concretely: the first time `boot()` on `index.html` observes `first_run` just
flipped to `false` — reachable without a persistent flag, by noticing
`as.tourStep` has never been set at all (distinct from "set and finished," see
below) — it shows a small, dismissible banner in the same visual register as
the calibration advisory (`cal` class family, index.html:95–120): a strip
under the nav, not a modal over the page, offering "See what's on this page"
with a close control that behaves exactly like `dismissCal`. Declining
records completion the same as finishing does; there is no difference between
"I don't want this" and "I've seen it" in the stored state, because the tour
never needs to distinguish them — both mean "don't offer again unsolicited."

**A visitor who accepts and stops at step 2 of, say, 6:** `as.tourStep` holds
the identifier of step 3, not a boolean. The banner or highlight for step 2
had its own dismiss control (again, the `calhide` pattern), and dismissing
mid-tour is worded to make clear it is not "finish the tour," it is "stop for
now" — because the owner's requirement is dismissible *at any time*, which
this codebase's own vocabulary for "not now, maybe later" is `as.calDismissed`
paired with a fixed cool-down, not a permanent close. **The tour does not
reuse the calibration advisory's 14-day cool-down model**, though, because the
two are answering different questions: a drift advisory recurs because the
underlying condition (an uncalibrated bank) is still true two weeks later and
worth mentioning again; a tour a visitor has already partly seen has no
underlying condition to re-trigger on. A mid-tour dismissal is final — it
writes the *last completed step*, and nothing brings the banner back
uninvited afterward. The way back is the "Show me around" button (placement:
alongside the theme toggle in the header, since that is the only other piece
of chrome that already exists on every page purely for the visitor's own
preference) — **shown only where it works**. A control that is sometimes
inert is worse than one that is absent, so the button stays off when the
browser refuses localStorage, while the collector is suppressed, on a first
run that the setup wizard owns, and when no tour step passes its gate. After
the tour is finished or dismissed the button stays: pressing it restarts from
step 1 regardless of where the stored step points — a deliberate restart,
chosen on purpose, is allowed to be the whole tour again rather than a
resume, since someone who went looking for it wants the tour, not the
fragment of it they had not yet seen. (Revised 2026-08, #203: an earlier
revision made the button permanent; that is the same inert-control failure
this spec refuses elsewhere.)

**A visitor who comes back next week having never touched the offer at all**
(closed the tab mid-wizard-handoff, or simply ignored the banner): the banner
stays offered, because nothing was dismissed. It disappears only on an
explicit close or on reaching the tour's last step. This is the same rule
`as.calDismissed` follows for a *new*, higher-severity condition arriving
after an old one was dismissed — an un-acted-on offer keeps asking, because
silence is not an answer CLAUDE.md's own memory notes are emphatic about
elsewhere ("never assume silence is consent").

## What it covers, in this pass, and why not more

Four pages, six to eight steps, chosen by what is both stable today and
independent of sub-project 2:

1. **Now** (index.html, `#now`): the live cards, the mode line, the module
   grid (gated on `per_module_battery`), the Legs panel (gated on
   `backup_output`). This is the page the wizard hands off to, so it is
   necessarily first.
2. **Energy flow** (index.html, `#flow`): the Sankey, reached by driving
   `wireTabs()`'s own `show('flow')` — the tour does not reimplement tab
   switching, it calls the page's own function, the same discipline as
   reading `capStrings` rather than re-deriving it.
3. **Graphs**: the small multiples, and the per-string detail gated on
   `pv_strings`.
4. **Costs**: gated as a whole on a tariff being entered — a visitor with no
   tariff sees the same `#notariff` panel the page already shows regardless
   of the tour, so a Costs tour step for that visitor is either skipped
   entirely or, better, points at the `#notariff` panel's own "Enter a tariff
   in settings" link rather than inventing new copy that duplicates it.

**History and Efficiency are left for a later pass**, not because they lack
anything to show — Efficiency's waterfall and trend, in particular, are
exactly the kind of thing worth a step — but because both took real design
work of their own recently (`docs/superpowers/specs` shows the daily-trend
and waterfall work landed within the last few days of this repository's
history) and are more likely to have their DOM anchors move again soon than
the four pages chosen. Adding their steps later is additive, not a rework of
this design.

**Settings is deliberately excluded from tour steps entirely in this pass**,
for a reason worth stating plainly rather than leaving implicit: I read
`settings.html`'s renderer (`render()`, line 331) looking for a stable anchor
per group — an id on the Connection section, the Tariff section, and so on —
and there isn't one. Every group is an anonymous `<section class="p grp">`
built from `GROUPS` at render time; the only addressable unit is a single
field's `data-for="<key>"` wrapper. A tour step pointing at "the Connection
group" today would have to select on heading text or DOM order, either of
which breaks the moment a group is reordered or retitled — and the Connection
group specifically is exactly the part sub-project 2 is about to replace
wholesale with the PV-string wizard and postcode-based location entry named
in the installer spec's closing list. Writing a tour step against markup that
is about to be rewritten is work guaranteed to be redone; the honest answer
is that Settings tour content is **blocked on sub-project 2's shape**, not
merely deferred by preference.

## Explicit dependency on sub-project 2

Flagged as asked: two places in this design would need revisiting once the
wizard/settings expansion lands, and the ordering should put that project
first if both are queued close together.

- **Settings steps cannot be written until the expanded settings page has
  stable per-section anchors**, as the paragraph above explains. If
  sub-project 2 adds stable ids to the groups it touches (it does not have to
  add them to groups it leaves alone), the tour can adopt them directly.
- **A step describing "type your panel strings as one line per string"**
  would be actively wrong the day the PV-string wizard sub-project 2 proposes
  replaces that text grammar with a guided picker. No such step exists in
  this pass's list for exactly that reason — Efficiency, which is where that
  description would have lived, is already deferred above.

Nothing else in this document assumes anything about sub-project 2's outcome;
the four pages in scope here are untouched by it.

## Mechanism, concretely

- **Storage:** `localStorage['as.tourStep']`, a string step identifier or
  absent. Read once per page load, in each page's existing `boot()`,
  immediately after `drawNav(current)` — the point every page already reaches
  after its own routing decision, and before its data fetches begin, matching
  the "operates against the skeleton" rule above.
- **Step definitions:** a static array in `common.js`, one entry per step:
  `{ id, page, selector, title, body, gate }`, where `gate` is a pure function
  of `(capabilities, status, settings)` — the three payloads every page
  already fetches for its own purposes (`loadCaps()` on index.html;
  equivalent fetches exist or are trivially added on the others). Following
  the `caps-logic` precedent, this list and its gate functions sit between a
  new pair of markers — `// >>> tour-logic` / `// <<< tour-logic` — so the
  same `node`-under-`subprocess` harness `test_dashboard_caps_js.py` already
  uses can extract and test it without a browser, a new dependency, or a DOM.
- **Rendering:** a small popover anchored to the target selector — CSS
  reusing the `--tip` / `--tip-shadow` tokens already defined for uPlot's
  hover readout, so it inherits validated light/dark contrast rather than
  inventing new colours (the palette must not change by eye, per CLAUDE.md,
  and this sidesteps the question by not introducing a new colour at all —
  the tour uses ink and panel tokens throughout, never `--pv`/`--load`/etc.,
  since it has no series to represent). Advance/back/dismiss controls in the
  same visual language as `.calhide`.
- **Cross-page continuation:** each page's list of steps that apply to *it*
  is filtered by `page` and by `gate` at boot; if the stored `as.tourStep`
  names a step that belongs to this page, it opens directly to that step
  rather than starting over. If the named step's gate no longer passes (rare
  — a string removed between visits, say), the tour advances silently to the
  next passing step on any page, rather than getting stuck pointing at
  something no longer there.
- **New file or addition to `common.js`:** an open question, not a decision.
  `common.js` is already 2,163 lines; the `caps-logic`/`setup-logic`/`readout-
  value` markers show extractable-slice testing does not require the code to
  live in a small file. I lean toward a new `tour.js`, loaded the same way
  `common.js` is (a plain `<script src="/tour.js">` tag, vendored nowhere,
  added to every in-scope page's `<head>`), because a tour is conceptually
  separable from the palette/formatter/nav grab-bag `common.js` already is,
  and because CLAUDE.md's "Whether a restart is needed" section notes that
  HTML/CSS/JS changes take effect on reload with no service restart — a
  second static file changes nothing about that. I did not build this, so
  the decision is left open rather than assumed silently.

## Verification

- **Step-gating logic** — the pure functions deciding whether a step's
  condition passes — tested the way `capStrings`/`capHasMetric` already are:
  extracted between markers, run under `node -e`, against fixture payloads
  covering a full three-string installation, a one-string one, a bare
  undeclared source, an installation with no tariff, one with no array
  described, and one with `any_rows: false`. Skipped where `node` is absent,
  matching the existing tests' own skip condition, so this adds no new CI
  prerequisite.
- **Persistence across a real page navigation** cannot be exercised by that
  harness — it has no DOM and no browser session. This needs the same
  approach the project's own "Verify UI in a browser" note already prescribes
  for JS work generally: drive an actual browser, start the tour on Now,
  advance it, navigate to Graphs by clicking the nav link exactly as a real
  visitor would, and confirm the tour resumes at the right step rather than
  restarting or vanishing. This is not proposed as a new automated suite —
  the codebase has no browser-automation dependency today and this design
  adds none — it is a manual (or agent-driven, via the existing
  `claude-in-chrome` tooling) check to run once the implementation exists,
  the same way the installer's own spec proposes bench-LXC verification for
  things a unit test cannot see.
- **The fresh-install path specifically** — `any_rows: false`, every chart
  showing its placeholder — verified by actually looking at it: install onto
  a clean instance (or a fake-driver `arraysense` with an emptied database),
  run the wizard, and confirm the tour's first offer reads sensibly pointed
  at empty panels rather than producing a step that visibly contradicts what
  is on screen.
- **The dismissal decision itself** (localStorage vs. a registered setting)
  is the one place in this document a fact could not be established from the
  code — the code only tells me what is *possible*, not what the owner meant.
  That is a question for the owner, not something to resolve by picking the
  option I could build.

## Open question for the owner

Is tour dismissal meant to be per-browser (what this document proposes,
following the `as.calDismissed` precedent) or installation-wide (a registered
setting, meaning one dismissal from any device silences it everywhere)? The
original brief's wording — "an unregistered settings key" — cannot be either
of those literally, since the settings system refuses unregistered keys
outright; I have picked the reading that matches every comparable piece of
interface state already in the codebase, but it is a guess about intent and
should be confirmed before implementation starts.

---

## Owner's decision, 12 August 2026

**Dismissal is per-browser, in `localStorage`, not an installation-wide
setting.** This follows the seven `as.*` keys the pages already use for
interface state — `as.calDismissed`, `as.tab`, `as.detailOpen` — rather than
registering a setting.

The reason is what the tour is for. A tour is shown to a *person*, and an
installation is read by several: one household member dismissing it on the
kitchen tablet must not silence it on somebody else's phone. Registering it as a
setting would make one click authoritative for every device, which is the
opposite of what a first-visit walkthrough is for.

It also resolves the contradiction this spec found in its brief. "An unregistered
settings key" is not possible: every write path through `SettingsStore` calls
`lookup_setting`, which raises `KeyError` for anything outside the `SETTINGS`
tuple, and `PUT /api/settings` enforces the same. `localStorage` is what the
codebase actually means by unregistered state, and it already has the precedent.

The cost is accepted: a new browser sees the tour again. That is arguably right,
since a new browser is usually a new reader.
