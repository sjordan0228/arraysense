// common.js — the parts of the front end every page is built out of.
//
// The dashboard grew from one page into five, and what they share is exactly
// what must not be allowed to diverge: the colour-blind-validated palette, the
// rule that an absent reading is a dash and never a zero, and the chart factory
// that keeps several canvases reading the same instant. A second copy of any of
// those is a copy that drifts, and the drift arrives as two pages disagreeing
// about what the same number means.
//
// Loaded as a classic script — not a module — immediately before each page's
// own script, so everything declared here is already in scope for it. Two
// things run on load: the stylesheet below, and the stale-data watch, which
// puts itself on every page precisely because no page has to remember to ask
// for it.

// ---------------------------------------------------------------------------
// The shared look. Injected rather than served as a second file so a page needs
// one tag and one request. It is appended while <head> is still being parsed,
// which puts it ahead of the page's own <style> in the cascade — so a page
// specialises by simply writing a rule, with no !important anywhere.
// ---------------------------------------------------------------------------

const BASE_CSS = `
  :root {
    /* Validated against protanopia, deuteranopia and tritanopia across every
       pair, not just adjacent ones — the previous grid violet and home blue sat
       ΔE 1.9 apart under protan, which is indistinguishable, and 9.8 apart even
       with full colour vision. These four are worst-pair ΔE 9.0 under CVD and
       16.1 with normal vision, against this panel's own surface.
       Charge is green and discharge red — the pair that shares a chart separates
       at ΔE 20.6 protan / 27.3 deutan / 70.7 tritan, chosen by measurement and
       not by eye. Position still carries the sign: the fill sits above or below
       the zero line whatever the hue does, so the colour is reinforcement. */
    --pv:#cf7b26; --load:#4678cc; --batt:#2aa198; --grid:#b0486e;
    --batt-dis:#d1495b;
    --ink:#fff; --ink2:#c8cbd9; --ink3:#8d92a8;
    --grid-line:rgba(255,255,255,.08);
    --panel:rgba(9,11,24,.55); --panel-b:rgba(255,255,255,.14);
    --good:#0ca30c; --warn:#fab219; --bad:#d03b3b;
    /* Canvas cannot read a stylesheet, so anything drawn to one has to be told
       its colour. These three exist for that: the chart code reads them at draw
       time the same way it reads a series hue, and they are the only thing that
       has to change for a canvas to follow the theme.
       --wash-rgb is a bare triplet rather than a colour because the band shading
       composes it with a different opacity per band. --theme is read as a word,
       so nothing has to infer the theme by inspecting a colour. */
    --theme:dark;
    --zero-rule:rgba(255,255,255,.28);
    --wash-rgb:255,255,255;
    /* Tints laid over a panel — track backgrounds, input fills, pressed states.
       They are the surface's own colour at low opacity, so on a light panel a
       white one is invisible and they have to invert with the theme. */
    --tint:rgba(255,255,255,.07); --tint-2:rgba(255,255,255,.12);
    --tint-3:rgba(255,255,255,.19);
    /* The page itself. Left as a literal, a light theme put light panels and
       dark text on a dark page: the headings sat on their own background and
       vanished. The panels are translucent over this, so it is the one colour
       everything else is read against. */
    --page:linear-gradient(168deg,#101a33 0%,#1b2547 34%,#3d2f56 62%,#7d4a3e 85%,#c07b3e 100%);
    --glow:radial-gradient(circle,rgba(255,198,120,.34),transparent 66%);
  }
  @media (prefers-color-scheme: light) {
    :root {
      /* Light theme tokens. The chart hues separate identically on light or dark
         — a pair's distance does not depend on the background — but their contrast
         against the surface changes. These values are the dark theme's until
         measured against a light panel like #f7f8fb. */
      --ink:#1a1a1a; --ink2:#333333; --ink3:#555555;
      --grid-line:rgba(0,0,0,.12);
      --panel:rgba(255,255,255,.85); --panel-b:rgba(0,0,0,.15);
      /* Light-theme chart hues need measuring against the light surface. For now,
         reuse the dark values — the relationships between them are already validated. */
      --pv:#cf7b26; --load:#4678cc; --batt:#2aa198; --grid:#b0486e;
      --batt-dis:#d1495b;
      --good:#0ca30c; --warn:#fab219; --bad:#d03b3b;
      /* The three canvas tokens, inverted. A white wash on a light panel is
         invisible, and a white zero rule with it — these are the whole reason
         the canvas needs telling rather than inheriting. */
      --theme:light;
      --zero-rule:rgba(0,0,0,.34);
      --wash-rgb:0,0,0;
      --tint:rgba(0,0,0,.05); --tint-2:rgba(0,0,0,.10);
      --tint-3:rgba(0,0,0,.16);
      /* The same walk through the same hues, lightened: dawn rather than dusk.
         Keeping the shape means the page still reads as this installation's
         rather than as a generic light theme. */
      --page:linear-gradient(168deg,#eef1f8 0%,#e7ebf5 34%,#efe8f3 62%,#f8ece3 85%,#fdf4e7 100%);
      --glow:radial-gradient(circle,rgba(255,186,96,.20),transparent 66%);
    }
  }
  * { box-sizing:border-box; }
  body {
    margin:0; min-height:100vh; color:var(--ink);
    font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
    background:var(--page);
    background-attachment:fixed;
  }
  .sunglow{position:fixed;width:420px;height:420px;border-radius:50%;right:-140px;top:-160px;
    background:var(--glow);filter:blur(16px);pointer-events:none}
  main{position:relative;max-width:1180px;margin:0 auto;padding:22px 20px 40px}
  header{display:flex;align-items:baseline;justify-content:space-between;margin-bottom:18px;flex-wrap:wrap;gap:8px}
  h1{margin:0;font-size:17px;font-weight:600;letter-spacing:-.01em}
  .conn{font-size:11.5px;color:var(--ink3)}
  .dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:0}
  .sq{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;vertical-align:1px}
  .p{background:var(--panel);border:1px solid var(--panel-b);border-radius:13px;padding:14px 16px;
     backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
  .lbl{font-size:9.5px;letter-spacing:.17em;text-transform:uppercase;color:var(--ink3)}
  section{margin-bottom:12px}
  .chead{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:10px;flex-wrap:wrap;gap:8px}
  .chead h2{margin:0;font-size:14px;font-weight:600;letter-spacing:-.01em}
  .legend{display:flex;gap:14px;font-size:11px;color:var(--ink2);flex-wrap:wrap}
  .rng{display:flex;gap:6px}
  .rng button{background:rgba(255,255,255,.07);border:1px solid var(--panel-b);color:var(--ink2);
    border-radius:7px;padding:3px 10px;font-size:11px;cursor:pointer;font-family:inherit}
  .rng button[aria-pressed="true"]{background:rgba(255,255,255,.2);color:var(--ink)}
  svg{display:block;width:100%;height:auto;overflow:visible}
  /* Navigation. Five views of one installation, so the marker is a state of the
     nav rather than a heading each page repeats — landing anywhere, the lit
     entry is the answer to "where am I". Drawn as links, not buttons: four of
     the five are separate documents and the two that are not still deserve a
     URL somebody can bookmark. */
  .nav{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}
  .nav a{background:rgba(255,255,255,.06);border:1px solid var(--panel-b);
    color:var(--ink3);border-radius:9px;padding:7px 15px;font:inherit;font-size:12.5px;
    line-height:1.45;cursor:pointer;text-decoration:none;transition:background .12s,color .12s}
  .nav a:hover{color:var(--ink2)}
  .nav a[aria-current="page"]{background:rgba(255,255,255,.17);color:var(--ink);font-weight:500}
  .nav a:focus-visible{outline:2px solid var(--load);outline-offset:2px}
  .view[hidden]{display:none}
  /* The stale-data banner. It sits directly under the nav on every page — the
     slot the dashboard's calibration advisory already uses — so a warning
     appearing never pushes the navigation down the screen, and it is shaped
     like that advisory on purpose: a strip with a coloured rule. A second
     visual language for "something is wrong" is one the reader has to learn
     twice. The rule's colour is never what carries the meaning; the mark and
     the headline say which condition this is in a glyph and in words. */
  .stale{display:flex;gap:13px;align-items:flex-start;margin-bottom:12px;
    border-left:3px solid var(--ink3)}
  .stale[hidden]{display:none}
  .stale h2{margin:0;font-size:14px;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
  .stale p{margin:5px 0 0;font-size:12px;line-height:1.5;color:var(--ink2);max-width:82ch}
  .stale .stalebody{min-width:0}
  .stale .stalemark{flex:0 0 auto;font-size:15px;line-height:1.5;color:var(--ink3)}
  /* The inverter's own words, quieter than the sentence that introduces them
     and allowed to break anywhere: "OSError: [Errno 113] No route to host" has
     nowhere to wrap on a phone. */
  .stale .stalewhy{margin-top:7px;font-size:10.5px;color:var(--ink3);
    font-family:ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
  .stale .stalewhy[hidden]{display:none}
  /* A deliberate yield is the quiet tone: the default rule, and a headline a
     shade down from the two that report a fault. */
  .stale.tone-note h2{color:var(--ink2)}
  .stale.tone-warn{border-left-color:var(--warn)}
  .stale.tone-warn .stalemark{color:var(--warn)}
  .stale.tone-bad{border-left-width:5px;border-left-color:var(--bad);
    background:linear-gradient(96deg,rgba(208,59,59,.17),rgba(208,59,59,.02) 46%,var(--panel))}
  .stale.tone-bad .stalemark{color:var(--bad)}
  /* Charts. uPlot draws to a canvas and ships a light-mode stylesheet; the
     vendored copy is left pristine so a newer release can be dropped straight
     in, which means every dark-page override belongs here instead. */
  .chart{position:relative;touch-action:pan-y}
  .chart .uplot{width:100%;font-family:inherit}
  /* The unit sits where the topmost tick label would run off the axis. It is
     HTML rather than a rotated canvas label so it reads horizontally. */
  .chart .unit{position:absolute;left:0;top:0;width:40px;text-align:right;
    font-size:9.5px;color:var(--ink3);pointer-events:none;z-index:2}
  .chart .nodata{color:var(--ink3);font-size:11.5px;padding:30px 0 30px 48px}
  /* Drag out a window to zoom into it. The vendored rule paints the selection
     near-black, which on this background is no feedback at all. */
  .u-select{background:rgba(255,255,255,.13);border-radius:3px}
  .u-hz .u-cursor-x{border-right:1px solid rgba(255,255,255,.34)}
  /* Hover readout. HTML rather than something painted on the canvas so it can
     wrap, use the page's own type, and sit above everything. */
  .tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .09s;
    background:rgba(6,8,18,.94);border:1px solid var(--panel-b);border-radius:9px;
    padding:8px 10px;font-size:11.5px;line-height:1.5;white-space:nowrap;z-index:5;
    box-shadow:0 6px 22px rgba(0,0,0,.45)}
  .tip.on{opacity:1}
  .tip .when{color:var(--ink3);font-size:10px;letter-spacing:.03em;margin-bottom:4px}
  .tip .row{display:flex;justify-content:space-between;gap:14px;
    font-variant-numeric:tabular-nums}
  .tip .row u{text-decoration:none;color:var(--ink2)}
  .tip .row b{font-weight:500;color:var(--ink)}
  .chartbar{display:flex;align-items:center;gap:11px;margin-bottom:9px;min-height:22px}
  .chartbar .note{font-size:11.5px;color:var(--warn)}
  .chartbar .zoomhint{font-size:10.5px;color:var(--ink3);margin-left:auto}
  .chartbar button{background:rgba(255,255,255,.09);border:1px solid var(--panel-b);
    color:var(--ink2);border-radius:7px;padding:3px 11px;font:inherit;font-size:11px;cursor:pointer}
  .chartbar button:hover{background:rgba(255,255,255,.17);color:var(--ink)}
  .chartbar button[hidden]{display:none}
  .kv{display:flex;justify-content:space-between;font-size:10.5px;color:var(--ink2);margin-top:3px}
  .kv u{text-decoration:none;color:var(--ink3)}
  .kv b{font-weight:500;font-variant-numeric:tabular-nums}
  .warn{color:var(--warn);font-weight:600}
  .muted{color:var(--ink3)}
  table{width:100%;border-collapse:collapse;font-size:12px}
  th,td{text-align:right;padding:5px 8px;border-bottom:1px solid var(--grid-line)}
  th:first-child,td:first-child{text-align:left}
  th{color:var(--ink3);font-weight:600;font-size:10px;letter-spacing:.1em;text-transform:uppercase}
  .iconbtn{background:rgba(255,255,255,.07);border:1px solid var(--panel-b);color:var(--ink2);
    border-radius:8px;padding:4px 10px;font-size:13px;cursor:pointer;font-family:inherit;line-height:1.4}
  .iconbtn:hover{background:rgba(255,255,255,.14);color:var(--ink)}
  .iconbtn.wide{font-size:11px;flex:1}
  details{margin-top:10px}
  summary{cursor:pointer;font-size:11px;color:var(--ink3)}
`;
document.head.appendChild(
  Object.assign(document.createElement('style'), { textContent: BASE_CSS }));

// ---------------------------------------------------------------------------
// Reading the page, and writing to it safely.
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"]/g,
  (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// A reading is a number or it is absent. Anything else — null, undefined, a
// string, a NaN — is unknown, and unknown must never fall through to zero.
const numOrNull = (v) => typeof v === 'number' && isFinite(v) ? v : null;

// ---------------------------------------------------------------------------
// Numbers on screen. Every one of these answers an absent reading with the
// dash, which is the single rule this whole project exists to enforce: a
// missing value and a value of nothing must never be drawn the same way.
// ---------------------------------------------------------------------------

const DASH = '—';
// Fixed digit counts, so a column of readings does not change width as the
// values move. Grouping separators come from the browser's own locale.
const gnum = (v, d) => v.toLocaleString(undefined, {minimumFractionDigits:d, maximumFractionDigits:d});
const kw = (w) => w === null || w === undefined ? DASH : (Math.abs(w) >= 1000 ? (w/1000).toFixed(2)+' kW' : Math.round(w)+' W');
const wStr = (v) => v === null ? DASH : gnum(Math.round(v), 0) + ' W';
const uStr = (v, d, unit) => v === null ? DASH : gnum(v, d) + (unit ? ' ' + unit : '');
// A quantity shown to a tenth only when it has one, so a 200 A limit is not
// dressed up as 200.0 and a 7.5 A one is not rounded away to 8.
const qty = (v, unit) => v === null ? DASH
  : gnum(v, Number.isInteger(v) ? 0 : 1) + (unit ? ' ' + unit : '');

// The currency is a setting on the service, never a hard-coded dollar sign:
// this is published software and most of the planet does not pay in dollars.
// Shared rather than kept on the page that first needed it, because Costs and
// History now both draw money and a figure that reads "$8.08" on one page and
// "8.08" on the other is the same drift in miniature that put two tariff
// parsers in this project.
function money(v, cur) {
  if (v === null || v === undefined || !isFinite(v)) return DASH;
  const sym = String(cur || '$');
  // "USD 12.34" but "$12.34" — a code needs the space and a symbol does not.
  const gap = /[A-Za-z0-9]$/.test(sym) ? ' ' : '';
  const digits = Math.abs(v).toLocaleString(undefined,
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (v < 0 ? '−' : '') + sym + gap + digits;
}

const kvRow = (label, value, cls) =>
  `<div class="kv"><u>${label}</u><b${cls ? ` class="${cls}"` : ''}>${value}</b></div>`;

// ---------------------------------------------------------------------------
// Settings the browser holds. These are display preferences rather than
// installation settings — the inverter's own configuration lives behind
// /api/settings — so they belong to the device looking at the page and not to
// the collector. A wall tablet showing Fahrenheit does not make the laptop
// beside it change.
// ---------------------------------------------------------------------------

// Readings are stored and served in Celsius because that is what the hardware
// reports; converting only on display means changing this never rewrites data.
const settings = {
  tempUnit: localStorage.getItem('as.tempUnit') || 'F',
  refreshSecs: Number(localStorage.getItem('as.refreshSecs') || 5),
};

function saveSetting(name, value) {
  settings[name] = value;
  localStorage.setItem('as.' + name, String(value));
}

// The service holds the same two display settings, and they are the
// installation's defaults rather than a competing source of truth: a device
// that has never chosen for itself follows them, and one that has chosen keeps
// its choice. Without this the settings page would write a temperature unit
// into the database that no other page ever read, which is the same shape of
// defect as a rollup builder nothing calls.
//
// Awaited before a page's first render. It fails quietly — a service that will
// not answer leaves this browser on its own defaults, and every page has
// bigger problems to report by then.
async function syncDisplayDefaults() {
  let values;
  try {
    const response = await fetch('/api/settings');
    if (!response.ok) return;
    values = (await response.json()).values || {};
  } catch (err) {
    return;
  }
  if (localStorage.getItem('as.tempUnit') === null) {
    const unit = values['display.temperature_unit'];
    if (unit === 'F' || unit === 'C') settings.tempUnit = unit;
  }
  if (localStorage.getItem('as.refreshSecs') === null) {
    const secs = Number(values['display.refresh_seconds']);
    if (Number.isFinite(secs) && secs > 0) settings.refreshSecs = secs;
  }
}

const tempOf = (c) => c === null || c === undefined ? null
  : (settings.tempUnit === 'F' ? c * 9/5 + 32 : c);
const tempStr = (c) => {
  const v = tempOf(c);
  return v === null ? DASH : `${v.toFixed(1)} °${settings.tempUnit}`;
};

// A temperature difference is not a temperature. Three degrees Celsius apart
// is 5.4 °F apart, not 37.4 — putting a delta through tempOf() is the version
// of this bug that looks right until someone switches units.
const tempDeltaStr = (dc) => dc === null ? DASH
  : `${(settings.tempUnit === 'F' ? dc * 9/5 : dc).toFixed(1)} °${settings.tempUnit}`;

// ---------------------------------------------------------------------------
// The calendar the service answers on.
//
// A day and a month are wall-clock questions, and which wall clock is not the
// browser's to decide: the installation's own zone beats it, because the
// inverter is in one place and the person looking at it may not be. So a page
// that builds "this month" or "the last thirty days" out of its own getMonth()
// is asking about a calendar the reply was not cut on. Both pages did, and both
// were wrong in a way that reads as normal: the Costs page asked from the
// browser's midnight on the first, which is not the site's, so the connection
// charge that falls due whole was apportioned instead — $3.44 of $15.00 on an
// install five hours west of the reader — and the History page keyed the
// server's buckets by a date it had built for itself, so a real day's bucket
// fell outside the row set while its energy stayed in the footer's total.
//
// One copy of the question, here, for the same reason there is one tariff
// parser: the two pages asking it separately is two answers waiting to differ.
// ---------------------------------------------------------------------------

// What this browser's clock is set to, or '' when it will not say. Still sent
// with every request, because where the installation has stated no zone — the
// default, and every install that has not been told otherwise — this is the
// zone the service answers on.
function browserZone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || '';
  } catch (err) {
    return '';
  }
}

// What this browser asks the service to consider, as a query string. Sent on
// every status poll as well, so the answer below is the answer this browser
// would actually get.
const zoneQuery = () => {
  const mine = browserZone();
  return mine ? `?tz=${encodeURIComponent(mine)}` : '';
};

// The zone /api/energy and /api/costs will cut this browser's requests in.
// Asked of the service rather than worked out here from the setting: which of
// the setting, this browser and the machine wins is the service's rule, and a
// copy of it in the browser is a copy that can drift.
//
// Held once resolved, and refreshed by the stale watch's own poll rather than
// by a request of its own — see checkStale. A wall tablet left open for a week
// would otherwise keep building calendars in the zone the setting held when it
// was opened, while the service answered in the new one, which is this defect
// all over again with a longer fuse.
let zonePromise = null;
function siteZone() {
  if (zonePromise === null) zonePromise = askSiteZone();
  return zonePromise;
}

// What a status reply says the calendar is. Ignored when it says nothing: a
// service too old to carry the field must leave the browser's own zone in
// place rather than blank it.
function noteZone(status) {
  const zone = status && status.timezone;
  if (typeof zone === 'string' && zone) zonePromise = Promise.resolve(zone);
}

async function askSiteZone() {
  const mine = browserZone();
  try {
    const response = await fetch('/api/status' + zoneQuery());
    if (response.ok) {
      const zone = (await response.json()).timezone;
      if (typeof zone === 'string' && zone) return zone;
    }
  } catch (err) {
    // Falls through to this browser's own zone, below.
  }
  // A service that will not answer. The browser's zone is what every page drew
  // on before this existed and is still what the service falls back to, so the
  // page shows a calendar rather than nothing at all.
  return mine;
}

// Today's date and time on a named zone's calendar, as plain numbers. Intl is
// the only thing in a browser that knows another zone's clock; everything past
// this point works in whole calendar fields, which is what a bucket boundary
// is. An empty or unusable zone name means this browser's own calendar, which
// is the answer when the service has stated none either.
function civilNow(zone) {
  const now = new Date();
  const own = () => ({
    year: now.getFullYear(), month: now.getMonth() + 1, day: now.getDate(),
    hour: now.getHours(), minute: now.getMinutes(), second: now.getSeconds(),
  });
  if (!zone) return own();
  const parts = {};
  try {
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: zone, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    for (const p of fmt.formatToParts(now)) parts[p.type] = Number(p.value);
  } catch (err) {
    return own();
  }
  return {
    year: parts.year, month: parts.month, day: parts.day,
    // Some engines render midnight as hour 24 rather than 0 under hour12:false,
    // and a day that begins at hour 24 is a day whose fraction elapsed is 100%.
    hour: parts.hour % 24, minute: parts.minute, second: parts.second,
  };
}

// A Date standing for one date on the site's calendar. It is a position and a
// label, never an instant: pages draw rows and chart columns against it and
// tell the API the date in words. Built at this browser's own midnight so that
// toLocaleDateString names the date it stands for — the true instant of the
// site's midnight would be labelled with the browser's date for it, which for a
// site five hours west is the day before. Month and day are allowed to run off
// the end of their ranges, which is how the Date constructor is asked what the
// calendar does next.
const civilDay = (year, month, day) => new Date(year, month - 1, day);

const pad2 = (n) => String(n).padStart(2, '0');

// One date in the words the API reads it in. A naive timestamp is read by the
// service as local time in whichever zone it resolved (see energy.with_zone),
// so this asks about the site's own midnight without the page ever having to
// work out which instant that is.
const naiveStamp = (d) =>
  `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T00:00:00`;

// A zone name as prose. The tz database writes them with underscores and a
// reader does not.
const zoneWords = (zone) => String(zone || '').replace(/_/g, ' ');

// ---------------------------------------------------------------------------
// Navigation. One list in one place: a page that names its own siblings is a
// page that goes stale the moment a sixth view is added, and a nav that
// disagrees between pages reads as a broken site rather than a stale constant.
//
// Now and Energy flow are two views of one document, so they are hashes rather
// than paths — switching between them must not throw away the live poll or the
// zoom on three charts, and a hash still gives each one a URL to bookmark.
// ---------------------------------------------------------------------------

const NAV = [
  { key:'now',     label:'Now',         href:'/#now' },
  { key:'flow',    label:'Energy flow', href:'/#flow' },
  { key:'inverter', label:'Inverter',  href:'/#inverter' },
  { key:'graphs',  label:'Graphs',      href:'/graphs' },
  { key:'history', label:'History',     href:'/history' },
  { key:'costs',   label:'Costs',       href:'/costs' },
  { key:'settings', label:'Settings',   href:'/settings' },
];

// Called again whenever the current view changes, not only at boot: on the
// dashboard the marker moves between two entries without the document
// reloading, and a marker left behind is worse than none.
function drawNav(current) {
  const el = $('nav');
  if (!el) return;
  el.innerHTML = NAV.map((n) =>
    `<a href="${n.href}"${n.key === current ? ' aria-current="page"' : ''}>${esc(n.label)}</a>`
  ).join('');
}

// ---------------------------------------------------------------------------
// The stale-data warning.
//
// Every page draws whatever the store last had in it and says nothing about how
// old that is, so a collector that has stopped leaves the charts their shape
// and the cards their numbers: a frozen dashboard is indistinguishable from a
// quiet afternoon. It is the lie this project refuses everywhere else — a
// missing reading is a dash and never a zero — told about the whole screen at
// once.
//
// It belongs here rather than on the dashboard because Costs and History price
// and total the same readings, and a stale bill is worth knowing about too.
// Nothing on any page opts in: the banner finds its own place under the nav
// this file already draws, and a warning a page has to remember to ask for is a
// warning the sixth page forgets.
//
// The verdict is /api/status's and only the wording is here. It used to be
// reached in this file, from a threshold copied out of the poll loop and a
// failure field no success ever clears, and the copy had already drifted from
// the original: the service calls a dead poll task stopped the instant it dies,
// while this file was telling the reader to wait twenty minutes for a restart.
// The service also knows two things a browser cannot — whether the read or the
// database write is what failed, and how old the newest stored reading is,
// which is the only clock that survives a restart.
// ---------------------------------------------------------------------------

// How often the banner asks. Staleness is measured in quarter-hours, so this
// governs how quickly the warning *clears* far more than how quickly it shows.
const STALE_POLL_MS = 30 * 1000;

// A timestamp from the status payload as epoch milliseconds, or null. An
// absent or unparseable one is unknown and must not become an epoch of 1970,
// which would read as fifty-six years stale.
//
// Named for the pair it belongs to rather than the obvious `stamp`, because
// three of the five pages already declare a top-level function by that name —
// and a `const` here against a `function` there is not shadowing but a
// SyntaxError that stops the whole page script from running.
const msOrNull = (iso) => {
  const t = Date.parse(iso || '');
  return Number.isFinite(t) ? t : null;
};

// How long it has been, in words. "Stale" answers the wrong question: the
// useful one is always how far behind the screen is, and an hour behind and a
// week behind call for different reactions.
function elapsedWords(ms) {
  const plural = (n, unit) => `${n} ${unit}${n === 1 ? '' : 's'}`;
  const mins = Math.max(0, Math.round(ms / 60000));
  if (mins < 60) return plural(mins, 'minute');
  const hours = Math.floor(mins / 60);
  if (hours < 48) {
    const rest = mins % 60;
    return plural(hours, 'hour') + (rest ? ' ' + plural(rest, 'minute') : '');
  }
  return plural(Math.floor(hours / 24), 'day')
    + (hours % 24 ? ' ' + plural(hours % 24, 'hour') : '');
}

// A time of day for something that happened today, and the full date once it is
// old enough that "14:32" would name the wrong day. Either way it is the
// reader's own clock, because "when did this stop" is a question about their
// afternoon and not about UTC.
const clockWords = (t, age) => age !== null && age < 18 * 3600000
  ? new Date(t).toLocaleTimeString() : new Date(t).toLocaleString();

// One mark per tone, borrowed from the calibration ladder on the dashboard: a
// dot advises, a triangle alerts, and a pause bar says the silence was asked
// for. Three shapes for three tones, so the reader who cannot tell the amber
// rule from the red one still has the distinction.
const STALE_MARKS = { note: '⏸', warn: '●', bad: '⚠' };

// How far behind the screen is, in a sentence. The endpoint reports the age of
// the newest *reading*, so a stretch of recorded gaps leaves no age to quote
// rather than a reassuring number — and a service that has read nothing at all
// gets a sentence that says so instead of one about the last reading.
function staleBehind(s) {
  const at = msOrNull(s.reading_at);
  const age = Number(s.age_seconds);
  if (at !== null && Number.isFinite(age)) {
    const ms = age * 1000;
    return `Last reading ${clockWords(at, ms)}, ${elapsedWords(ms)} ago.`;
  }
  const searched = Number(s.searched_seconds);
  return Number.isFinite(searched)
    ? `No reading in the last ${elapsedWords(searched * 1000)}.`
    : 'Nothing has been read at all.';
}

// How long the loop has marked nothing, when that is long enough to be worth a
// number. A poll task that died a moment ago is stalled by a few seconds, and
// "silent for 0 minutes" reads as a bug in the page rather than a fault in the
// service.
function staleSilence(s) {
  const seconds = Number(s.stalled_seconds);
  return Number.isFinite(seconds) && seconds >= 60
    ? ` and has marked nothing for ${elapsedWords(seconds * 1000)}`
    : '';
}

// The wording for each verdict /api/status can reach. Nothing here decides
// anything: every branch the service used to be second-guessed on — whether the
// loop is running, whether the inverter or the database is at fault, whether a
// silence was asked for — arrives already settled, and this turns it into two
// sentences and a tone.
const STALE_WORDS = {
  // Silence somebody asked for. The dongle takes one TCP client at a time, so
  // releasing it for the vendor's app is deliberate and has an end time on it,
  // and a warning here would train the reader to wave away the real ones.
  yielding: (s, status, now) => {
    const until = msOrNull(status.yield_until);
    const resumes = until !== null && until > now
      ? `polling resumes at ${new Date(until).toLocaleTimeString()}`
      : 'polling resumes when the yield ends';
    return {
      tone: 'note',
      headline: 'Paused — the dongle was handed over on purpose',
      detail: `${staleBehind(s)} It takes one connection at a time and was released so `
        + `another app could use it, so nothing is wrong here: ${resumes}.`,
    };
  },
  stopped: (s) => ({
    tone: 'bad',
    headline: 'The collector has stopped',
    detail: `${staleBehind(s)} The poll loop is not running${staleSilence(s)}, so nothing on `
      + 'this page will change until it comes back.',
  }),
  not_running: (s) => ({
    tone: 'bad',
    headline: 'The collector is not polling',
    detail: `${staleBehind(s)} The service reports that it is not collecting at all, so `
      + 'nothing on this page will change until it starts again.',
  }),
  // The loop working, not the loop failing: it records each gap, backs off and
  // keeps trying, which is why this is amber and not red.
  inverter: (s, status) => {
    const tries = Number(status.consecutive_failures);
    return {
      tone: 'warn',
      headline: 'The inverter is not responding',
      detail: `${staleBehind(s)} The collector is still trying and is recording the gap`
        + (Number.isFinite(tries) && tries > 0 ? `; ${tries} polls in a row have failed` : '')
        + '.',
    };
  },
  // Told apart by the service rather than guessed at here. The two faults call
  // for opposite reactions, and this one used to be reported as the inverter —
  // sending the reader after the dongle, the WiFi and the breaker while the
  // disk was the problem.
  storage: (s) => ({
    tone: 'warn',
    headline: 'Readings are not being saved',
    detail: `${staleBehind(s)} The inverter is answering — it is the database that is `
      + 'refusing the writes, so the disk is the place to look, not the dongle.',
  }),
  // Neither of the other two: the inverter answered and this build could not
  // turn the reply into a reading. Red rather than amber because the fault is in
  // our own decoding — an outage and a busy disk both clear on their own, and
  // this one may not. It is not claimed to be permanent: one malformed reply
  // followed by a good one clears it, so the count is what says how bad it is.
  driver: (s, status) => {
    const tries = Number(status.consecutive_failures);
    return {
      tone: 'bad',
      headline: 'Readings cannot be decoded',
      detail: `${staleBehind(s)} The inverter is answering, but this build could not turn its `
        + 'reply into a reading'
        + (Number.isFinite(tries) && tries > 0 ? `; ${tries} polls in a row have failed` : '')
        + '. The recorded reason names what was refused.',
    };
  },
  silent: (s) => ({
    tone: 'warn',
    headline: 'No new readings',
    detail: `${staleBehind(s)} The collector reports no failure either, so it is too early `
      + 'to say why.',
  }),
};

// What to say, given /api/status and the moment to measure against — or null
// when there is nothing to say. Separated from the drawing so each case can be
// read as a case, and asserted on directly.
function staleState(status, now) {
  const unreachable = (detail) => ({
    tone: 'bad', mark: STALE_MARKS.bad,
    headline: 'The service is not answering', detail, why: '',
  });
  if (!status || typeof status !== 'object') {
    return unreachable('Nothing on this page can be trusted as current: /api/status could '
      + 'not be reached, so there is no way to say how old these readings are.');
  }
  const s = status.staleness;
  if (!s || typeof s !== 'object') {
    return unreachable('Nothing on this page can be trusted as current: /api/status '
      + 'answered without saying how old these readings are.');
  }
  if (!s.stale) return null;
  // An unrecognised verdict is a newer service than this page, and the one
  // thing that must not happen then is silence: the payload has already said
  // the screen is out of date, so say that much rather than borrow the wording
  // of a case this might not be.
  const words = STALE_WORDS[s.verdict];
  const state = words ? words(s, status, now) : {
    tone: 'warn',
    headline: 'No new readings',
    detail: `${staleBehind(s)} The service names a condition this page is too old to `
      + 'describe.',
  };
  return {
    tone: state.tone,
    mark: STALE_MARKS[state.tone],
    headline: state.headline,
    detail: state.detail,
    // Usually names the fault outright — a refused connection, no route to the
    // dongle, a database held by another writer — which is most of the
    // diagnosis. Sent only where the verdict names a failure, so an error left
    // over from an outage that has since cleared is not quoted under a new one.
    why: s.reason ? String(s.reason) : '',
  };
}

// The banner's own element and its parts, built once. Kept and rewritten in
// place rather than rebuilt, so the polite live region announces a change of
// cause instead of re-reading itself every time the minute ticks over.
let stalePart = null;

function staleElement() {
  if (stalePart) return stalePart;
  const nav = $('nav');
  const host = nav ? nav.parentNode : (document.querySelector('main') || document.body);
  if (!host) return null;
  const box = document.createElement('div');
  box.className = 'p stale';
  box.id = 'staleBanner';
  box.hidden = true;
  box.setAttribute('role', 'status');
  box.setAttribute('aria-live', 'polite');
  const mark = box.appendChild(document.createElement('div'));
  mark.className = 'stalemark';
  // Decorative: the glyph repeats what the headline says, and a screen reader
  // announcing "warning sign" before the sentence adds nothing.
  mark.setAttribute('aria-hidden', 'true');
  const body = box.appendChild(document.createElement('div'));
  body.className = 'stalebody';
  const head = body.appendChild(document.createElement('h2'));
  // The two lines that change as the minutes pass are left out of the live
  // region, so the cause is announced once rather than the age every half
  // minute for as long as the outage lasts.
  const detail = body.appendChild(document.createElement('p'));
  detail.setAttribute('aria-live', 'off');
  const why = body.appendChild(document.createElement('div'));
  why.className = 'stalewhy';
  why.setAttribute('aria-live', 'off');
  why.hidden = true;
  if (nav) host.insertBefore(box, nav.nextSibling);
  else host.insertBefore(box, host.firstChild);
  stalePart = { box, mark, head, detail, why };
  return stalePart;
}

function showStale(state) {
  const parts = staleElement();
  if (!parts) return;
  if (!state) { parts.box.hidden = true; return; }
  // Written as text and never as markup. The reason comes from somewhere down
  // in the transport or the storage stack and has no business being parsed as
  // HTML, and text also means there is no esc() call here to forget. Only on a
  // change, so an unchanged line is not re-announced to a screen reader.
  const set = (el, text) => { if (el.textContent !== text) el.textContent = text; };
  parts.box.className = `p stale tone-${state.tone}`;
  set(parts.mark, state.mark);
  set(parts.head, state.headline);
  set(parts.detail, state.detail);
  set(parts.why, state.why || '');
  parts.why.hidden = !state.why;
  parts.box.hidden = false;
  // No dismiss control, deliberately. Unlike the drift advisory this describes
  // a condition that is either true this minute or it is not, and it clears
  // itself the moment a reading arrives — there is nothing to snooze.
}

// One request, and the service's own clock along with it. A timestamp the
// service wrote compared against the browser's idea of now compares two clocks,
// and the wall tablet this is read on may have no working NTP; the Date header
// is the service answering "what time is it there", so the age is measured
// against the clock that produced the reading.
// How many checks in a row must fail before the page says the service is not
// answering. One is not evidence: a laptop waking from sleep, a WiFi roam or a
// restart between polls all drop a single request, and at a thirty-second
// cadence the loudest banner on the page would flash for every one of them.
// Requiring a second consecutive failure puts thirty seconds between the blip
// and the banner, which is a poll's worth of confirmation rather than none.
const STALE_TOLERATED_MISSES = 1;
let staleMisses = 0;

async function checkStale() {
  let status = null;
  let now = Date.now();
  try {
    const response = await fetch('/api/status' + zoneQuery());
    if (response.ok) {
      const served = Date.parse(response.headers.get('date') || '');
      if (Number.isFinite(served)) now = served;
      status = await response.json();
      // This poll runs every thirty seconds on every open page, so the zone
      // the pages build their calendars in follows a setting changed elsewhere
      // within one poll — without a request of its own.
      noteZone(status);
    }
  } catch (err) {
    // A status endpoint that cannot be reached is itself one of the cases, and
    // staleState says so; nothing is swallowed by landing here.
    status = null;
  }
  if (status === null) {
    staleMisses += 1;
    // Hold the previous verdict rather than replacing it with a louder one.
    // If the collector really has stopped, the banner already up is the more
    // accurate of the two and this only delays the escalation by a poll.
    if (staleMisses <= STALE_TOLERATED_MISSES) return;
  } else {
    staleMisses = 0;
  }
  showStale(staleState(status, now));
}

function startStaleWatch() {
  checkStale();
  setInterval(checkStale, STALE_POLL_MS);
  // A backgrounded tab has its timers throttled, so what a reader sees on
  // coming back can be minutes out of date — including the banner whose whole
  // job is to say that.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkStale();
  });
}

// common.js is loaded from <head>, so on most pages there is no <nav> to hang
// the banner under yet.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', startStaleWatch);
} else {
  startStaleWatch();
}

// ---------------------------------------------------------------------------
// Time ranges. Shared so the same four spans mean the same four things on every
// page that plots against time.
// ---------------------------------------------------------------------------

const RANGES = [
  { key:'6h',  label:'6h',  seconds:6*3600 },
  { key:'24h', label:'24h', seconds:24*3600 },
  { key:'7d',  label:'7d',  seconds:7*86400 },
  { key:'30d', label:'30d', seconds:30*86400 },
];

// The caller owns which range is current and what to do when it changes, and
// redraws to move the pressed state. Holding that here would mean holding it
// once for pages that have two independent range pickers.
function drawRanges(el, current, onPick) {
  if (!el) return;
  el.innerHTML = RANGES.map((r) =>
    `<button data-k="${r.key}" aria-pressed="${r.key === current.key}">${esc(r.label)}</button>`
  ).join('');
  el.querySelectorAll('button').forEach((b) => {
    b.onclick = () => onPick(RANGES.find((r) => r.key === b.dataset.k));
  });
}

// ---------------------------------------------------------------------------
// Charts. uPlot rather than hand-written SVG, for the two things that version
// could not do: drag out a window to zoom into it, and read the same instant
// off every chart at once.
// ---------------------------------------------------------------------------

// One sync group across a page's charts, so a cursor on any of them puts the
// crosshair at the same instant on the others. Zoom rides the same channel:
// a drag-select on one window zooms them all and a double-click resets them
// together, which is the only way the comparison stays honest.
const SYNC_KEY = 'arraysense';

// uPlot paints on a canvas and a canvas has no idea what var(--pv) means, so
// the palette has to be resolved to real colours. It is read back out of the
// stylesheet rather than restated here: those values were found by searching
// OKLCH space against a protan, deutan and tritan checker, and a second copy
// of them is a copy that drifts away from the one that was validated. The map
// below is only reached when there is no computed style to read at all.
//
// The fallback values are dark-theme defaults. When the stylesheet is not
// available, the canvas falls back to these; when it is, the computed style
// wins and respects prefers-color-scheme.
const INK_FALLBACK = {
  '--pv':'#cf7b26', '--load':'#4678cc', '--batt':'#2aa198', '--grid':'#b0486e',
  '--batt-dis':'#d1495b', '--ink2':'#c8cbd9', '--ink3':'#8d92a8', '--grid-line':'rgba(255,255,255,.08)',
  // Reached only when there is no computed style at all, which is the dark
  // theme's case by definition — with a stylesheet the media query answers.
  '--theme':'dark', '--zero-rule':'rgba(255,255,255,.28)', '--wash-rgb':'255,255,255',
};
const inkCache = {};
function ink(name) {
  if (inkCache[name] === undefined) {
    const computed = typeof getComputedStyle === 'function'
      ? getComputedStyle(document.documentElement) : null;
    const value = computed ? String(computed.getPropertyValue(name) || '').trim() : '';
    inkCache[name] = value || INK_FALLBACK[name];
  }
  return inkCache[name];
}
// Whether the light theme is in force, read as a word the stylesheet declares
// rather than inferred from a colour. Inferring it meant parsing --panel's rgba
// and summing the channels against a threshold, which is a rule nobody would
// know they had broken by restyling a panel.
function isLightTheme() {
  return ink('--theme') === 'light';
}

// Fills are the series colour at lower opacity and never a colour of their
// own, so a fill cannot introduce a hue the palette was not checked for.
function fade(name, alpha) {
  const raw = String(ink(name)).trim().replace('#', '');
  const hex = raw.length === 3 ? raw.replace(/./g, (c) => c + c) : raw;
  const n = parseInt(hex, 16);
  if (hex.length !== 6 || !Number.isFinite(n)) return ink(name);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

// Tick labels were 9.5px, the smallest text anywhere on the page against a body
// of 14px, and unreadable on a normal-DPI display. 12px puts them in the same
// range as the legend and the range buttons, which is where a label somebody is
// expected to read belongs. The gutters below grew with it: a taller label needs
// more height under the plot, and a wider one needs more room beside it, or the
// text clips instead of shrinking.
const AXIS_FONT = '12px system-ui,-apple-system,"Segoe UI",sans-serif';

// A gap is a break, never bridged. A null reading, or a row the collector
// wrote with an error because the poll failed, enters the series as null and
// uPlot leaves the hole — spanGaps stays off on every series for that reason.
// A comms outage has to look like one rather than like a straight line drawn
// across it, and a missing reading must not arrive on the canvas as a zero.
function frame(points) {
  const rows = (points || []).filter((p) => p && Number.isFinite(Date.parse(p.timestamp)));
  return {
    rows,
    // uPlot's time scale counts in seconds, not milliseconds.
    xs: rows.map((p) => Date.parse(p.timestamp) / 1000),
    of: (key) => rows.map((p) => (p.error ? null : numOrNull(p[key]))),
  };
}
const anyReading = (...columns) => columns.some((c) => c.some((v) => v !== null));

// The same two formats the previous chart used, chosen by the span on screen
// rather than by the range button that was pressed: zoomed into one hour of a
// thirty-day range, a date on every tick names the same day six times over.
function timeTicks(u, splits) {
  const span = u.scales.x.max - u.scales.x.min;
  return splits.map((t) => {
    const when = new Date(t * 1000);
    return span > 3 * 86400
      ? when.toLocaleDateString(undefined, { month:'short', day:'numeric' })
      : when.toLocaleTimeString(undefined, { hour:'2-digit', minute:'2-digit' });
  });
}

// Power is stored in watts and read in kilowatts.
const kwTicks = (u, splits) =>
  splits.map((v) => (v / 1000).toFixed(Math.abs(v) >= 1000 || v === 0 ? 0 : 1));

// Fresh objects every call: uPlot fills defaults into the axis it is handed,
// and two axes sharing one nested grid object would share the result.
const axis = (extra) => Object.assign({
  stroke: () => ink('--ink2'),
  font: AXIS_FONT,
  ticks: { show: false },
  grid: { stroke: () => ink('--grid-line'), width: 1 },
}, extra);

// `space` is the minimum room uPlot leaves between ticks before it drops to a
// coarser increment. Its default is 50px, which was fine at 9.5px type and is
// not at 12px: "12:00 PM" measures about 53px, so neighbouring labels touched.
// uPlot centres labels and does no collision avoidance of its own, so the
// spacing is the only thing standing between a readable axis and an overlapping
// one — 80 leaves a clear gap at the widest format timeTicks produces.
const timeAxis = () => axis({ size: 32, space: 80, values: timeTicks });
const kwAxis = () => axis({ size: 56, gap: 6, values: kwTicks });

// The zero line is not a gridline: it is the thing a signed reading is signed
// against. Drawn brighter, over the grid and under the series, which is where
// the hand-written chart had it.
const zeroRule = () => ({
  hooks: {
    drawAxes: (u) => {
      const sc = u.scales.y;
      if (sc.min === null || sc.min === undefined || sc.min > 0 || sc.max < 0) return;
      const { left, top, width, height } = u.bbox;
      const y = Math.round(u.valToPos(0, 'y', true)) + 0.5;
      if (y < top || y > top + height) return;
      const ctx = u.ctx;
      ctx.save();
      ctx.strokeStyle = ink('--zero-rule');
      ctx.lineWidth = uPlot.pxRatio || 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + width, y);
      ctx.stroke();
      ctx.restore();
    },
  },
});

// Band shading plugin. Draws tariff band windows as background shading, varying
// opacity only — no new hue. The maintainer is colour blind and every hue has to
// be measured against every other, so a band with its own colour would be one
// nobody checked; a tariff also has as many bands as it likes, which two colours
// could never say. Luminance carries it instead, and luminance is the one
// distinction that survives every form of colour vision deficiency.
//
// On a dark panel the wash is white, so more opacity reads *brighter*. The more
// expensive the band, the more it stands out, which puts the eye on the costly
// hours. On a light panel the wash is dark, so more opacity reads *darker* —
// the expensive hours are still the ones that stand out, just in the opposite
// direction. The legend text must reverse accordingly.
//
// ``getWindows`` is a function, not an array, and that is load-bearing. ``paint``
// builds a chart once and afterwards only calls ``setData`` on it, so a plugin
// rebuilt with fresh windows on a later draw is discarded — the chart keeps the
// plugin it was constructed with. Closing over an array therefore froze the
// shading at whatever range was drawn first: switching from 24 hours to 30 days
// left twenty-nine of those days shaded from yesterday's windows, while the
// legend beside it described the new ones. Reading through a function means the
// one long-lived plugin always paints what was last fetched.
//
// Windows are handed in rather than fetched here because drawing is synchronous.
// No shading at all when there are none; absent data is not zero.
function bandShade(getWindows) {
  const scale = (windows) => {
    const prices = [...new Set(windows.map((w) => w.price_per_kwh))]
      .filter((p) => p !== null && p !== undefined)
      .sort((a, b) => a - b);
    const out = {};
    // Cheapest barely there, dearest clearly visible. Spread across however many
    // distinct prices there are rather than capped at a fixed number of steps: a
    // tariff may have four bands, and clamping would give the top two the same
    // shade and quietly say they cost the same.
    const LOW = 0.04;
    const HIGH = 0.18;
    const last = prices.length - 1;
    prices.forEach((p, i) => {
      out[p] = last <= 0 ? HIGH : LOW + ((HIGH - LOW) * i) / last;
    });
    return out;
  };

  return {
    hooks: {
      drawAxes: (u) => {
        const windows = getWindows();
        if (!windows || !windows.length) return;
        const opacities = scale(windows);
        const { left, top, width, height } = u.bbox;
        const ctx = u.ctx;
        ctx.save();
        ctx.beginPath();
        ctx.rect(left, top, width, height);
        ctx.clip();
        // ``ink`` caches, as it does for every token, so switching the system
        // theme with the page open keeps the old wash until a reload. That is
        // the same for every colour on every chart and not worth a cache
        // invalidation of its own; it is written down so nobody reads this as
        // live and is surprised.
        const wash = ink('--wash-rgb');

        for (const w of windows) {
          // A stretch no band covers comes back with a null band and no price —
          // the endpoint returns it rather than dropping it, so unpriced energy
          // shows up instead of quietly vanishing. It must not be shaded: any
          // wash here would read as a band, and a middling one would read as a
          // middling rate. Absent is absent, so it is left plain.
          const opacity = opacities[w.price_per_kwh];
          if (opacity === undefined) continue;

          const x0 = Math.max(left, u.valToPos(new Date(w.start).getTime() / 1000, 'x', true));
          const x1 = Math.min(left + width, u.valToPos(new Date(w.end).getTime() / 1000, 'x', true));
          if (x1 <= x0) continue;

          ctx.fillStyle = `rgba(${wash},${opacity})`;
          ctx.fillRect(x0, top, x1 - x0, height);
        }

        ctx.restore();
      },
    },
  };
}

// Which chart the pointer is on, if any. One variable rather than a flag per
// chart, because the question is which chart owns the readout: the synced
// crosshair is what puts the same instant on the others, and three tooltips
// lighting up at once is three things to read instead of one.
//
// A touch claims it and does not release on lift. uPlot drives its cursor from
// the mouse events a browser synthesises after touchend, so a readout that
// released on lift would be a readout no tablet ever shows.
let hoveredChart = null;

// The hover readout. Each chart carries its own so the values belong to the
// series under the pointer.
function readout(id, rows) {
  let tip = null;
  return {
    hooks: {
      init: (u) => {
        tip = document.createElement('div');
        tip.className = 'tip';
        u.over.appendChild(tip);
        const claim = () => { hoveredChart = id; };
        u.over.addEventListener('mouseenter', claim);
        u.over.addEventListener('touchstart', claim, { passive: true });
        u.over.addEventListener('mouseleave', () => {
          if (hoveredChart === id) hoveredChart = null;
          tip.classList.remove('on');
        });
      },
      setCursor: (u) => {
        if (!tip) return;
        const idx = u.cursor.idx;
        if (hoveredChart !== id || idx === null || idx === undefined) {
          tip.classList.remove('on');
          return;
        }
        const when = new Date(u.data[0][idx] * 1000);
        // Absent stays absent in the readout too. A dash is a reading nobody
        // took and a zero is a reading of nothing, and on a chart whose whole
        // argument is that difference they cannot be allowed to look alike.
        const body = rows.map(([label, si, fmt]) => {
          const v = numOrNull(u.data[si][idx]);
          return `<div class="row"><u>${esc(label)}</u><b>${v === null ? DASH : fmt(v)}</b></div>`;
        }).join('');
        tip.innerHTML = `<div class="when">${esc(when.toLocaleString())}</div>${body}`;
        tip.classList.add('on');
        // Kept inside the plot rather than allowed to push the page sideways —
        // a wall tablet has no horizontal scrollbar to rescue it.
        const room = u.over.clientWidth || 0;
        const wide = tip.offsetWidth || 150;
        tip.style.left =
          `${Math.max(2, Math.min(Math.max(room - wide - 2, 2), u.cursor.left + 12))}px`;
        tip.style.top = '6px';
      },
      destroy: () => {
        if (hoveredChart === id) hoveredChart = null;
        tip = null;
      },
    },
  };
}

const dots = (name) => ({ size: 4.5, width: 0, stroke: () => ink(name), fill: () => ink(name) });

// Points are left to uPlot's own judgement rather than switched off: it draws
// them only once the samples are far enough apart to have room, which is
// exactly when a single reading standing alone between two gaps would
// otherwise be a line segment of zero length and so invisible.
const trace = (label, name, width, extra) => Object.assign({
  label, scale: 'y', spanGaps: false, width,
  stroke: () => ink(name), points: dots(name),
}, extra);

// Carried along for the readout only, never drawn: the battery chart answers
// "and what was the state of charge then", and the state of charge chart
// answers the reverse. A hidden series takes no part in ranging its scale.
const carried = () => ({ show: false, scale: 'y', spanGaps: false });

// Kept, and no longer used on the Power flow chart. Solar read as volume there
// rather than as a line, which was the better picture of a harvest — but two
// area fills leave no room for the tariff shading behind them, and on a sunny
// day this one covers most of the plot. Grid keeps its fill because a grid line
// vanishes under the home line; solar has no such problem, so solar gave way.
// Left defined because the volume reading is a real if minor loss and may be
// wanted back. Filled to the zero line rather than the floor of the chart, or a
// negative axis would put the fill on the wrong side of nothing.
function pvFill(u) {
  const grad = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
  grad.addColorStop(0, fade('--pv', .5));
  grad.addColorStop(1, fade('--pv', .04));
  return grad;
}

// Grid import is filled, and it is now the only series here that is. When
// the house runs on the grid, import *equals* house load to the watt, so a grid
// line lies exactly under the home line and vanishes beneath it. An area cannot
// vanish that way — the body of it shows below the coincident line even where
// the two edges are identical. A dashed line was tried first and was too faint
// to see at all, which is the honest reason this is a fill.
function gridFill(u) {
  const grad = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
  grad.addColorStop(0, fade('--grid', .42));
  grad.addColorStop(1, fade('--grid', .05));
  return grad;
}

// Charge is green and discharge red, a pair measured under simulated
// protanopia, deuteranopia and tritanopia rather than chosen by eye — those
// three, not "every form", because that is what was actually run. The split is
// exactly at the zero
// line, so position carries the sign and the hue only reinforces it. The
// discharge half is given more opacity because it has to show through against
// a dark panel at all.
function battFill(u) {
  const { top, height } = u.bbox;
  const grad = u.ctx.createLinearGradient(0, top, 0, top + height);
  const at = Math.max(0, Math.min(1, (u.valToPos(0, 'y', true) - top) / Math.max(height, 1)));
  grad.addColorStop(0, fade('--batt', .34));
  grad.addColorStop(at, fade('--batt', .34));
  grad.addColorStop(at, fade('--batt-dis', .55));
  grad.addColorStop(1, fade('--batt-dis', .55));
  return grad;
}

// Headroom above the highest reading and below the lowest, with a floor of one
// kilowatt so a quiet night is not drawn as a dramatic range of noise. Null
// bounds mean nothing was read at all, which is not a range of zero.
function powerRange(u, min, max) {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1000];
  const hi = Math.max(1000, max);
  const lo = Math.min(0, min);
  const pad = (hi - lo) * 0.08 || 500;
  return [lo - pad, hi + pad];
}

// Symmetric about zero on purpose: an asymmetric scale would draw a 3 kW
// charge and a 3 kW discharge at different heights, and the whole point of
// such a chart is that the two are the same quantity with opposite signs.
function batteryRange(u, min, max) {
  const seen = [min, max].filter(Number.isFinite).map(Math.abs);
  const mag = Math.max(1000, ...seen);
  return [-mag * 1.08, mag * 1.08];
}

const chartBase = (extra) => Object.assign({
  padding: [12, 14, 0, 0],
  // The panel header already names the series; uPlot's own legend would say it
  // again in a second style.
  legend: { show: false },
  cursor: {
    // uPlot's own zoom, left switched on: drag a window on the time axis,
    // double-click anywhere to go back to the whole range.
    drag: { x: true, y: false, setScale: true },
    // One vertical crosshair and no horizontal one. These charts are read
    // against time, not against whatever value the pointer is level with.
    y: false,
    sync: { key: SYNC_KEY, setSeries: false, scales: ['x', null] },
  },
}, extra);

// Live instances by chart id, so a refresh updates the data in place instead
// of tearing the canvas down and losing the zoom with it.
const CHARTS = {};

// uPlot needs pixels and will not resize itself. The width is re-read on every
// draw as well as watched, because a chart can start life in a hidden view
// where the container measures zero wide — with only the observer it comes back
// from the other view stretched, and with only the redraw it never follows the
// window.
function fit(wrap, u, height) {
  const width = Math.floor(wrap.clientWidth);
  if (width > 0 && (u.width !== width || u.height !== height)) u.setSize({ width, height });
}

function chartMessage(id, text) {
  const held = CHARTS[id];
  if (held) {
    if (held.ro) held.ro.disconnect();
    held.u.destroy();
    delete CHARTS[id];
  }
  const wrap = $(id + 'Wrap');
  if (wrap) wrap.innerHTML = `<div class="nodata">${esc(text)}</div>`;
}

// Set when the owner picks a different range, so the next paint drops any
// zoom rather than silently keeping a window from the previous range. Without
// this the fetch runs, the button lights up, and nothing on screen changes.
let rangeChanged = false;

// A one-line status above the charts, for a page that has a #chartNote to put
// it in. Used for a failed refresh, where the alternative — replacing the
// charts with an error string — throws away both the data and any zoom over a
// problem that usually lasts one poll.
function note(text) {
  const el = $('chartNote');
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
}

// uPlot zooms by drag-select and resets by double-click, neither of which is
// visible. The button appears only while a chart is actually zoomed, so it
// doubles as the hint that zooming happened at all.
function refreshZoomState() {
  const anyZoomed = Object.keys(CHARTS).some((id) => {
    const held = CHARTS[id] && CHARTS[id].u;
    if (!held || !held.data[0] || held.data[0].length < 2) return false;
    const xs = held.data[0];
    return held.scales.x.min > xs[0] || held.scales.x.max < xs[xs.length - 1];
  });
  const btn = $('zoomReset');
  if (btn) btn.hidden = !anyZoomed;
}

function resetZoom() {
  for (const id of Object.keys(CHARTS)) {
    const held = CHARTS[id] && CHARTS[id].u;
    if (held && held.data[0] && held.data[0].length) {
      held.setScale('x', { min: held.data[0][0], max: held.data[0][held.data[0].length - 1] });
    }
  }
  refreshZoomState();
}

function paint(id, spec, data) {
  if (typeof uPlot === 'undefined') { chartMessage(id, 'chart library did not load'); return; }
  const wrap = $(id + 'Wrap');
  let held = CHARTS[id];
  if (!held) {
    wrap.innerHTML = spec.unit ? `<span class="unit">${esc(spec.unit)}</span>` : '';
    // A hidden view measures zero wide and there is no laying a plot out inside
    // no pixels, so the chart is built at a placeholder width and corrected the
    // moment the container has one.
    const width = Math.max(Math.floor(wrap.clientWidth), 320);
    const u = new uPlot(Object.assign({ width, height: spec.height }, spec.opts), data, wrap);
    held = CHARTS[id] = { u, ro: null };
    if (typeof ResizeObserver === 'function') {
      held.ro = new ResizeObserver((entries) => {
        const w = Math.floor(entries[0].contentRect.width);
        // The old SVGs used a viewBox with height:auto, so height tracked width.
      // A fixed pixel height flattens every chart on a wide screen and changes
      // how large a swing looks, which is the thing these are read for.
      const h = Math.round(spec.height * Math.min(1.35, Math.max(0.85, w / 900)));
      if (w > 0 && (w !== u.width || h !== u.height)) u.setSize({ width: w, height: h });
      });
      held.ro.observe(wrap);
    }
  } else {
    // A refresh must not throw away a zoom. A minute after dragging out a
    // window the owner is still looking at it, so the scales are reset only
    // when the chart was showing the whole range to begin with.
    const was = held.u.data[0];
    const zoomed = was.length > 1
      && (held.u.scales.x.min > was[0] || held.u.scales.x.max < was[was.length - 1]);
    // setData only commits when it is also resetting the scales, so holding a
    // zoom means asking for the repaint separately. Without it the new data
    // sits in u.data while the canvas keeps showing the old, and the readout
    // reports values that are not on screen.
    const keep = zoomed && !rangeChanged;
    held.u.setData(data, !keep);
    if (keep) held.u.redraw();
  }
  fit(wrap, held.u, spec.height);
}
