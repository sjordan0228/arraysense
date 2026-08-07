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
// own script, so everything declared here is already in scope for it. Nothing
// runs on load except the stylesheet below.

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
       Charge and discharge deliberately share one hue: they are one series with
       a sign, separated by the zero line, and position carries that far better
       than a fifth colour could. */
    --pv:#cf7b26; --load:#4678cc; --batt:#2aa198; --grid:#b0486e;
    --batt-dis:#14625f;
    --ink:#fff; --ink2:#c8cbd9; --ink3:#8d92a8;
    --grid-line:rgba(255,255,255,.08);
    --panel:rgba(9,11,24,.55); --panel-b:rgba(255,255,255,.14);
    --good:#0ca30c; --warn:#fab219; --bad:#d03b3b;
  }
  * { box-sizing:border-box; }
  body {
    margin:0; min-height:100vh; color:var(--ink);
    font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
    background:linear-gradient(168deg,#101a33 0%,#1b2547 34%,#3d2f56 62%,#7d4a3e 85%,#c07b3e 100%);
    background-attachment:fixed;
  }
  .sunglow{position:fixed;width:420px;height:420px;border-radius:50%;right:-140px;top:-160px;
    background:radial-gradient(circle,rgba(255,198,120,.34),transparent 66%);filter:blur(16px);pointer-events:none}
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
const INK_FALLBACK = {
  '--pv':'#cf7b26', '--load':'#4678cc', '--batt':'#2aa198', '--grid':'#b0486e',
  '--batt-dis':'#14625f', '--ink3':'#8d92a8', '--grid-line':'rgba(255,255,255,.08)',
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

// Fills are the series colour at lower opacity and never a colour of their
// own, so a fill cannot introduce a hue the palette was not checked for.
function fade(name, alpha) {
  const raw = String(ink(name)).trim().replace('#', '');
  const hex = raw.length === 3 ? raw.replace(/./g, (c) => c + c) : raw;
  const n = parseInt(hex, 16);
  if (hex.length !== 6 || !Number.isFinite(n)) return ink(name);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}

const AXIS_FONT = '9.5px system-ui,-apple-system,"Segoe UI",sans-serif';

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
  stroke: () => ink('--ink3'),
  font: AXIS_FONT,
  ticks: { show: false },
  grid: { stroke: () => ink('--grid-line'), width: 1 },
}, extra);

const timeAxis = () => axis({ size: 26, values: timeTicks });
const kwAxis = () => axis({ size: 48, gap: 6, values: kwTicks });

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
      ctx.strokeStyle = 'rgba(255,255,255,.28)';
      ctx.lineWidth = uPlot.pxRatio || 1;
      ctx.beginPath();
      ctx.moveTo(left, y);
      ctx.lineTo(left + width, y);
      ctx.stroke();
      ctx.restore();
    },
  },
});

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

// Solar is what the array harvested, so it reads as volume rather than as a
// line. Filled to the zero line rather than to the floor of the chart, or a
// negative axis would put the fill on the wrong side of nothing.
function pvFill(u) {
  const grad = u.ctx.createLinearGradient(0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
  grad.addColorStop(0, fade('--pv', .5));
  grad.addColorStop(1, fade('--pv', .04));
  return grad;
}

// Charge and discharge share one hue and differ only in lightness, which is
// the distinction that survives every form of colour blindness. The split is
// exactly at the zero line, so position carries the meaning and the shade only
// confirms it. The darker half is given more opacity because it has to show
// through against a dark panel at all.
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
