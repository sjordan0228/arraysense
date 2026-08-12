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

const LIGHT_TOKENS = `
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
      /* The hover readout and the crosshair sit over a chart, which is not a
         panel. The readout must be opaque enough to read over whatever the
         chart shows, and a crosshair is a 1px line that would read as a
         different strength at any of the tint steps — so each carries its own
         inverted value: a near-solid white readout with a soft shadow, and a
         dark crosshair where the dark theme's is white. */
      --tip:rgba(255,255,255,.96);
      --tip-shadow:0 6px 22px rgba(0,0,0,.18);
      --cursor-x:rgba(0,0,0,.34);
      /* The same walk through the same hues, lightened: dawn rather than dusk.
         Keeping the shape means the page still reads as this installation's
         rather than as a generic light theme. */
      --page:linear-gradient(168deg,#eef1f8 0%,#e7ebf5 34%,#efe8f3 62%,#f8ece3 85%,#fdf4e7 100%);
      --glow:radial-gradient(circle,rgba(255,186,96,.20),transparent 66%);
  `;

// One definition, applied two ways: when the device asks for light and nothing
// has overridden it, and when something explicitly has. Written once and
// interpolated rather than pasted twice — two copies of a palette is how the two
// drift, which is the same reason there is one copy of the tariff grammar.
//
// The media query is scoped to :root:not([data-theme]) so an explicit choice
// always wins over the device's. With data-theme set, only the attribute rules
// match, and :root's own dark values stand unless the light ones override them.
const LIGHT_TOKEN_BLOCK = `
  :root[data-theme="light"] { ${LIGHT_TOKENS} }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme]) { ${LIGHT_TOKENS} }
  }
`;

const BASE_CSS = `
    /* The efficiency budget bar. Texture and order carry the meaning; see
       drawWaterfall for why hue deliberately does not. */
    .wf-track{position:relative;display:flex;align-items:stretch;height:34px;width:100%;
      border:1px solid var(--panel-b);border-radius:4px;overflow:hidden;
      background:var(--tint)}
    .wf-seg{height:100%}
    .wf-actual{background:var(--pv)}
    .wf-lost{background:repeating-linear-gradient(135deg,
      var(--zero-rule) 0 3px,transparent 3px 7px)}
    .wf-mark{position:absolute;top:-3px;bottom:-3px;width:2px;
      background:var(--zero-rule)}
    .wf-markkey{background:var(--zero-rule);width:3px;border:0}
    /* Outlined rather than filled, and beyond the gap: it was never scored. */
    .wf-curt{background:transparent;border:1px dashed var(--zero-rule)}
    .wf-gap{width:10px;flex:0 0 10px;background:transparent}
    .wf-legend{display:flex;flex-wrap:wrap;gap:.35rem 1rem;margin-top:.4rem;
      font-size:.82rem;color:var(--muted,inherit)}
    .wf-key{display:inline-flex;align-items:center;gap:.35rem}
    .wf-sw{display:inline-block;width:14px;height:10px;border-radius:2px;
      border:1px solid var(--panel-b)}

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
    /* The hover readout and the crosshair sit over a chart, which is not the
       same surface as a panel: the readout has to be opaque enough to read
       against whatever the chart shows, and a crosshair is a 1px line that
       would read as a different strength at any of the tint steps. Dark values
       are the originals these replaced, unchanged. */
    --tip:rgba(6,8,18,.94);
    --tip-shadow:0 6px 22px rgba(0,0,0,.45);
    --cursor-x:rgba(255,255,255,.34);
    /* The page itself. Left as a literal, a light theme put light panels and
       dark text on a dark page: the headings sat on their own background and
       vanished. The panels are translucent over this, so it is the one colour
       everything else is read against. */
    --page:linear-gradient(168deg,#101a33 0%,#1b2547 34%,#3d2f56 62%,#7d4a3e 85%,#c07b3e 100%);
    --glow:radial-gradient(circle,rgba(255,198,120,.34),transparent 66%);
  }
  ${LIGHT_TOKEN_BLOCK}
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
  /* The theme button. Sized and shaped like the settings gear beside it, because
     they are the same kind of thing: a control that belongs to this browser
     rather than a reading from the inverter. */
  .themebtn{background:var(--tint);border:1px solid var(--panel-b);color:var(--ink2);
    border-radius:9px;width:30px;height:30px;line-height:1;cursor:pointer;font-size:14px;
    display:inline-flex;align-items:center;justify-content:center;font-family:inherit}
  .themebtn:hover{background:var(--tint-2);color:var(--ink)}
  .themebtn:focus-visible{outline:2px solid var(--load);outline-offset:2px}
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
  .rng button{background:var(--tint);border:1px solid var(--panel-b);color:var(--ink2);
    border-radius:7px;padding:3px 10px;font-size:11px;cursor:pointer;font-family:inherit}
  .rng button[aria-pressed="true"]{background:var(--tint-3);color:var(--ink)}
  svg{display:block;width:100%;height:auto;overflow:visible}
  /* Navigation. Five views of one installation, so the marker is a state of the
     nav rather than a heading each page repeats — landing anywhere, the lit
     entry is the answer to "where am I". Drawn as links, not buttons: four of
     the five are separate documents and the two that are not still deserve a
     URL somebody can bookmark. */
  .nav{display:flex;gap:4px;margin-bottom:14px;flex-wrap:wrap}
  .nav a{background:var(--tint);border:1px solid var(--panel-b);
    color:var(--ink3);border-radius:9px;padding:7px 15px;font:inherit;font-size:12.5px;
    line-height:1.45;cursor:pointer;text-decoration:none;transition:background .12s,color .12s}
  .nav a:hover{color:var(--ink2)}
  .nav a[aria-current="page"]{background:var(--tint-3);color:var(--ink);font-weight:500}
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
  .u-select{background:var(--tint-2);border-radius:3px}
  .u-hz .u-cursor-x{border-right:1px solid var(--cursor-x)}
  /* Hover readout. HTML rather than something painted on the canvas so it can
     wrap, use the page's own type, and sit above everything. */
  .tip{position:absolute;pointer-events:none;opacity:0;transition:opacity .09s;
    background:var(--tip);border:1px solid var(--panel-b);border-radius:9px;
    padding:8px 10px;font-size:11.5px;line-height:1.5;white-space:nowrap;z-index:5;
    box-shadow:var(--tip-shadow)}
  .tip.on{opacity:1}
  .tip .when{color:var(--ink3);font-size:10px;letter-spacing:.03em;margin-bottom:4px}
  .tip .row{display:flex;justify-content:space-between;gap:14px;
    font-variant-numeric:tabular-nums}
  .tip .row u{text-decoration:none;color:var(--ink2)}
  .tip .row b{font-weight:500;color:var(--ink)}
  .chartbar{display:flex;align-items:center;gap:11px;margin-bottom:9px;min-height:22px}
  .chartbar .note{font-size:11.5px;color:var(--warn)}
  .chartbar .zoomhint{font-size:10.5px;color:var(--ink3);margin-left:auto}
  .chartbar button{background:var(--tint);border:1px solid var(--panel-b);
    color:var(--ink2);border-radius:7px;padding:3px 11px;font:inherit;font-size:11px;cursor:pointer}
  .chartbar button:hover{background:var(--tint-3);color:var(--ink)}
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
  .iconbtn{background:var(--tint);border:1px solid var(--panel-b);color:var(--ink2);
    border-radius:8px;padding:4px 10px;font-size:13px;cursor:pointer;font-family:inherit;line-height:1.4}
  .iconbtn:hover{background:var(--tint-2);color:var(--ink)}
  .iconbtn.wide{font-size:11px;flex:1}
  details{margin-top:10px}
  summary{cursor:pointer;font-size:11px;color:var(--ink3)}
  /* --- The setup wizard / connection picker --------------------------------
     Namespaced under .setup so it shares no rule with the settings page's own
     .f controls: one renderer, mounted in two shells, carrying its own look. */
  .setup{display:flex;flex-direction:column;gap:14px;max-width:560px}
  .setup .row{display:flex;flex-direction:column;gap:5px;min-width:0}
  .setup label{font-size:12px;color:var(--ink2);font-weight:600}
  .setup select,.setup input{background:var(--tint);border:1px solid var(--panel-b);
    color:var(--ink);border-radius:8px;padding:8px 10px;font:inherit;font-size:13px;width:100%;
    font-variant-numeric:tabular-nums}
  .setup select:focus,.setup input:focus{outline:2px solid var(--pv);outline-offset:1px;
    border-color:transparent}
  .setup .hint{font-size:11px;color:var(--ink3);line-height:1.5}
  /* A caveat is not a hint. It takes the warning ink and a rule down its edge
     so it reads as something to weigh rather than as help text, and it uses
     --warn rather than a colour of its own: the palette is CVD-validated and
     this must not introduce a hue nobody checked. */
  .setup .warn-note{color:var(--warn);border-left:2px solid var(--warn);
    padding-left:8px;margin-top:-2px}
  .setup .row.bad input,.setup .row.bad select{border-color:var(--bad)}
  .setup .err{font-size:11px;color:var(--bad);line-height:1.5}
  .setup .err[hidden]{display:none}
  .setup .detectrow{display:flex;gap:8px;align-items:flex-start}
  .setup .detectrow input{flex:1}
  .setup .status{font-size:12px;line-height:1.55;min-height:1.2em}
  .setup .status.ok{color:var(--good)}
  .setup .status.bad{color:var(--bad)}
  .setup .status.warn{color:var(--warn)}
  .setup .status.busy{color:var(--ink2)}
  .setup .actions{display:flex;gap:11px;align-items:center;margin-top:4px}
  .setup .primary{background:var(--pv);border:1px solid transparent;color:#1a1204;font-weight:600;
    font-size:13px;padding:8px 18px;border-radius:8px;cursor:pointer;font-family:inherit}
  .setup .primary:hover{background:#e08c33}
  .setup .primary:disabled{opacity:.45;cursor:default}
  .setup .adv{margin-top:2px}
  .setup .adv .fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:8px}
  @media(max-width:520px){.setup .adv .fields{grid-template-columns:1fr}}
  .wizard{max-width:620px;margin:0 auto}
  .wizard .welcome{font-size:13px;color:var(--ink2);line-height:1.6;margin:2px 0 18px;max-width:70ch}
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
// The setup wizard's decisions. One renderer serves both the first-run wizard
// and the settings Connection group, and these are the choices it makes from
// the /api/setup payload — which models a driver has, which fields a transport
// needs, which battery sources a driver supports, and what to actually send.
// They are pure and DOM-free so node can check them against describe_setup's
// shape (tests/test_wizard_js.py): a field shown that a transport does not
// need, or a value sent that apply would refuse, is the same drift from the
// single source of truth this project forbids everywhere else.
// ---------------------------------------------------------------------------

// >>> setup-logic
function setupModelsFor(payload, driver) {
  for (const maker of payload.manufacturers || []) {
    for (const fam of maker.families || []) {
      if (fam.driver === driver) return fam.models || [];
    }
  }
  return [];
}

function setupMakerOf(payload, driver) {
  for (const maker of payload.manufacturers || []) {
    for (const fam of maker.families || []) {
      if (fam.driver === driver) return maker.name;
    }
  }
  return '';
}

function setupFieldsFor(payload, transport) {
  const map = (payload && payload.transports) || {};
  return Array.isArray(map[transport]) ? map[transport] : [];
}

function setupBatterySourcesFor(payload, driver) {
  const map = (payload && payload.battery_sources) || {};
  return Array.isArray(map[driver]) ? map[driver] : ['none'];
}

// The apply body: only the connection keys, only the ones with a real value.
// The transport-specific fields ride only when their transport needs them, so a
// dongle install never sends a serial_device the server would ignore and a
// serial install never sends a dongle_host. The dongle port is deliberately not
// among them — it is not offered on the form (8000 by default, set in the config
// file when it differs), so it never reaches here. An empty string is dropped:
// it means "leave the file or overlay as it is", never "set this to blank",
// which is the same rule the settings overlay follows when it merges the file.
function buildSetupBody(s) {
  const body = {};
  const put = (k, v) => {
    if (v === undefined || v === null || v === '') return;
    body[k] = v;
  };
  put('driver', s.driver);
  put('model', s.model);
  put('transport', s.transport);
  put('battery_source', s.battery_source);
  put('inverter_serial', s.inverter_serial);
  if (s.transport === 'modbus_serial') {
    put('serial_device', s.serial_device);
    put('serial_baud', s.serial_baud);
    put('serial_unit_id', s.serial_unit_id);
  } else if (s.transport === 'dongle') {
    put('dongle_host', s.dongle_host);
    put('dongle_serial', s.dongle_serial);
  }
  return body;
}
// <<< setup-logic

// ---------------------------------------------------------------------------
// The postcode lookup's decisions. The wizard and the settings page both turn
// a /api/geocode reply into one of three states — nothing matched, a single
// pick, or a list the owner chooses from — and both caption a candidate with
// its place name, region and country. These are pure and DOM-free so node can
// hold them to it (tests/test_geocode_logic_js.py), the same way the sections
// above are checked: an ambiguous reply must never be silently resolved to
// its first entry, and a reply that found nothing must never be shown as a
// found place.
// ---------------------------------------------------------------------------

// >>> geocode-logic
// How a candidate reads in a caption: the place name, the region it sits in
// and the country, each dropped when the geocoder did not supply it. Both
// pages use the same sentence so the owner sees the same town in the wizard
// and on the settings page.
function placeLabel(c) {
  return [c && c.name, c && c.admin1, c && c.country].filter(Boolean).join(', ');
}

// The one decision the box makes from a /api/geocode reply. 'none' means the
// service answered and found nothing — the reply that carries no results key
// at all — and the owner is told so and left to continue, never blocked.
// 'single' carries the one candidate, already resolved, because there is
// nothing to choose between. 'multiple' carries the whole list and no pick,
// because resolving an ambiguous postcode to its first result would guess at
// which country the owner means.
function geocodeOutcome(candidates) {
  if (!Array.isArray(candidates) || candidates.length === 0) {
    return { status: 'none', note: 'Nothing matched that name.' };
  }
  if (candidates.length === 1) {
    const c = candidates[0];
    return {
      status: 'single',
      candidate: c,
      note: `${placeLabel(c)} (${Number(c.latitude).toFixed(5)}, ${Number(c.longitude).toFixed(5)}).`,
    };
  }
  return {
    status: 'multiple',
    candidates: candidates,
    note: `${candidates.length} results — pick one.`,
  };
}
// <<< geocode-logic

// ---------------------------------------------------------------------------
// The device's declaration, from /api/capabilities. The store answers every
// query for every registry metric — one this device cannot produce reads back
// the same null a reading nobody took gives — so the declaration is the only
// thing that separates "this hardware does not exist" from "nothing arrived".
// Pages gate what they draw on these two questions rather than enumerating the
// reference inverter's shape by hand, which is how a one-string machine came
// to show two permanently empty string charts. Pure and DOM-free so node can
// hold them to it (tests/test_dashboard_caps_js.py), the same way the wizard's
// decisions are checked above.
// ---------------------------------------------------------------------------

// >>> caps-logic
// How many PV strings the device declares. null is "unknown", never zero: with
// no declaration a page falls back to whatever it drew before capabilities
// existed, because a missing leaflet must not erase real hardware.
function capStrings(caps) {
  return caps && typeof caps.pv_strings === 'number' && isFinite(caps.pv_strings)
    ? caps.pv_strings
    : null;
}

// Whether a metric may be drawn for this device. Only a declaration that names
// its metrics and leaves this one out answers no. No declaration at all (caps
// null) and a bare source (metrics null) both answer yes, because unknown must
// not suppress: absent capability is a fact a driver states, and nobody having
// stated one is not the same fact.
function capHasMetric(caps, metric) {
  if (!caps || !Array.isArray(caps.metrics)) return true;
  return caps.metrics.includes(metric);
}

// A row is only worth drawing when the metric behind it exists for this
// device. An absent reading and an absent capability both draw as a dash,
// and only one of them is a fault worth showing: a register the hardware
// never reads drawing a permanent dash teaches the reader to ignore the
// dash entirely, which is how a real outage goes quiet. capHasMetric answers
// true on an unknown declaration, so a bare source and a device whose
// declaration has not loaded yet keep every row they draw today — that is
// what makes this safe to apply everywhere.
function capRow(caps, metrics, label, value, cls) {
  const names = Array.isArray(metrics) ? metrics : [metrics];
  if (!names.every((m) => capHasMetric(caps, m))) return '';
  return kvRow(label, value, cls);
}

// The halves of a two-reading row that this device actually produces. A row
// like "H1 · H2" or "Health / cycles" names two things at once, and on a
// machine with only one of them the pair rendered a real number beside a dash
// — which reads as a broken sensor rather than as a machine built differently.
// The caller composes label and value from what comes back, so one heatsink
// reads "H1" and not "H1 · H2" with half of it missing.
//
// An empty result means the row should not be drawn at all: a label naming two
// readings the hardware does not have is worse than no row.
// Draw the surviving halves of a two-reading row as one line. `parts` is what
// capParts returned; each carries its own label, its value, and optionally its
// own formatter for rows whose halves are not the same kind of number.
//
// The label is built from the halves that survived, so a machine reporting one
// heatsink reads "H1" and one reporting both reads "H1 · H2". `lead` prefixes
// it where the row needs a noun of its own ("DC bus 1 · 2"). The whole row
// disappears when nothing survived, and shows the dash when every surviving
// half is unread — an absent reading still has to look absent.
function pairRow(parts, lead, fmt, unit, sep, vsep) {
  if (!parts.length) return '';
  // The label's separator and the value's are not always the same character:
  // "Leg 1 / Leg 2" labels with a slash and reads its watts with a dot. Both
  // default to the dot the temperature and voltage pairs use.
  const ls = sep || ' · ';
  const vs = vsep || ls;
  const label = (lead ? lead + ' ' : '') + parts.map((p) => p.label).join(ls);
  if (parts.every((p) => p.v === null)) return kvRow(label, DASH);
  // A part may bring its own formatter for a row whose halves are not the same
  // kind of number. Falling back to String rather than to whatever `fmt` holds
  // keeps a caller that passes neither from throwing here — this runs inside
  // the detail render, so an exception would take the whole panel down over one
  // row's formatting.
  const shown = parts.map((p) => (p.fmt || fmt || String)(p.v)).join(vs);
  return kvRow(label, unit ? `${shown} ${unit}` : shown);
}

function capParts(caps, parts) {
  return parts.filter((p) => capHasMetric(caps, p.metric));
}

// Module metrics are declared in caps.battery_module_metrics, a different
// list from caps.metrics — the inverter metrics live in one and the bare
// per-pack templates in the other. Asking capHasMetric for a per-pack
// reading would look in the wrong list and answer no to every device.
function capHasModuleMetric(caps, metric) {
  if (!caps || !Array.isArray(caps.battery_module_metrics)) return true;
  return caps.battery_module_metrics.includes(metric);
}
// <<< caps-logic

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
  { key:'efficiency', label:'Efficiency', href:'/efficiency' },
  { key:'settings', label:'Settings',   href:'/settings' },
];

// ---------------------------------------------------------------------------
// The efficiency budget bar.
//
// One bar the width of the day's expected production, divided into what the
// array actually made, what it lost, and what the inverter refused because
// there was nowhere to put it. The last of those is the reason this is drawn
// by hand rather than as a stacked chart: refused energy is not a loss, and a
// bar that shades it like one tells the owner their array is faulty on exactly
// the days it behaved perfectly.
//
// Hue carries almost nothing here, deliberately. The obvious encoding — solar
// orange for what you made, discharge red for what you lost — measures dE 3.6
// between those two under tritanopia and 13.6 under deuteranopia against this
// project's own validated palette, which is to say invisible to the person who
// owns this installation. So position (order along the bar), texture (solid,
// hatched, outlined) and an explicit gap carry the meaning, and colour only
// agrees with them. That ordering survives any colour vision, including none.
//
// Reads the endpoint's own segments and never recomputes them: the walk from
// expected to actual has to close, and it closes in the API where the numbers
// are, not twice.
function drawWaterfall(host, segments) {
  if (!host) return;
  host.innerHTML = '';
  if (!Array.isArray(segments) || !segments.length) return;

  const by = {};
  for (const s of segments) by[s.name] = s;
  const expected = numOrNull(by.expected && by.expected.kwh);
  const actual = numOrNull(by.actual && by.actual.kwh);
  // Absent is absent. A bar drawn from nothing would be a claim that the array
  // was expected to make nothing and made nothing.
  if (expected === null || actual === null || expected <= 0) {
    host.innerHTML = `<p class="muted">${DASH} nothing measured for this period.</p>`;
    return;
  }
  const unexplained = numOrNull(by.unexplained && by.unexplained.kwh) || 0;
  const curtailed = numOrNull(by.curtailed && by.curtailed.kwh) || 0;
  const gain = numOrNull(by.unmodelled_gain && by.unmodelled_gain.kwh) || 0;

  // Everything is scaled against the widest thing the bar has to hold, so a day
  // that beat its model does not run off the end.
  const span = Math.max(expected, actual + curtailed) || 1;
  const pct = (v) => `${Math.max(0, (v / span) * 100)}%`;

  const parts = [];
  parts.push(`<div class="wf-seg wf-actual" style="width:${pct(actual)}"
      title="Produced ${gnum(actual, 1)} kWh"></div>`);
  if (unexplained > 0) {
    parts.push(`<div class="wf-seg wf-lost" style="width:${pct(unexplained)}"
      title="Unexplained shortfall ${gnum(unexplained, 1)} kWh"></div>`);
  }
  // Deliberately not a segment of its own. A day that beat its model has that
  // surplus already inside `actual` -- the walk is expected - unexplained -
  // curtailed + gain = actual -- so drawing it again pushed the bar past its
  // own track by exactly the gain. It is marked instead by where `expected`
  // falls, which is the honest way to show production running past the model.
  // The gap is the argument: everything left of it was scored, and what sits
  // beyond it was never counted against the array at all.
  if (curtailed > 0) {
    parts.push(`<div class="wf-gap" aria-hidden="true"></div>`);
    parts.push(`<div class="wf-seg wf-curt" style="width:${pct(curtailed)}"
      title="Refused ${gnum(curtailed, 1)} kWh — nowhere to put it, not a fault"></div>`);
  }

  const legend = [
    `<span class="wf-key"><i class="wf-sw wf-actual"></i>produced ${gnum(actual, 1)} kWh</span>`,
  ];
  if (unexplained > 0) {
    legend.push(`<span class="wf-key"><i class="wf-sw wf-lost"></i>unexplained ${gnum(unexplained, 1)} kWh</span>`);
  }
  if (gain > 0) {
    legend.push(`<span class="wf-key"><i class="wf-sw wf-markkey"></i>${gnum(gain, 1)} kWh above the model</span>`);
  }
  if (curtailed > 0) {
    legend.push(`<span class="wf-key"><i class="wf-sw wf-curt"></i>curtailed ${gnum(curtailed, 1)} kWh — not counted against the array</span>`);
  }

  // Where the model said the day should have ended. Left of it is shortfall,
  // right of it is the array beating its own description.
  const mark = `<div class="wf-mark" style="left:${pct(expected)}"
      title="modelled ${gnum(expected, 1)} kWh" aria-hidden="true"></div>`;

  host.innerHTML =
    `<div class="wf-track" role="img" aria-label="Of ${gnum(expected, 1)} kilowatt hours expected, ` +
    `${gnum(actual, 1)} produced, ${gnum(unexplained, 1)} unexplained, ` +
    `${gnum(curtailed, 1)} curtailed and not counted against the array.">${parts.join('')}${mark}</div>` +
    `<div class="wf-legend">${legend.join('')}</div>`;
}

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
    if (response.status === 404) {
      // No status endpoint here means the service is in first-run setup mode,
      // which serves the wizard and /api/setup only. There is no collector to
      // be stale about, so hide the banner rather than escalate a missing
      // endpoint into "the service is not answering" over the setup form.
      showStale(null);
      staleMisses = 0;
      return;
    }
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
//
// The theme is applied here rather than waiting for the document: everything
// after this reads resolved colours, and a chart built before the attribute is
// set would take the previous theme's palette from the ink cache. The button
// itself has to wait for a <header> to exist.
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    startStaleWatch();
    mountThemeButton();
  });
} else {
  startStaleWatch();
  mountThemeButton();
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
// Which theme this browser wants: what the device says unless somebody has said
// otherwise here. Kept per browser on purpose — one household can want the wall
// tablet dark and the laptop following the room, so this is the override and the
// installation-wide default belongs in the settings registry beside it.
const THEME_KEY = 'arraysense-theme';
const THEME_ORDER = ['system', 'light', 'dark'];
const THEME_GLYPH = { system: '\u25D1', light: '\u2600', dark: '\u263E' };
const THEME_SAYS = {
  system: 'Theme: following this device',
  light: 'Theme: light',
  dark: 'Theme: dark',
};

function themeChoice() {
  let held = null;
  try {
    // Only the read is guarded, and only because private browsing refuses
    // localStorage outright. Wrapping the whole function meant a programming
    // error inside it came back as a plausible answer instead of a stack trace.
    held = localStorage.getItem(THEME_KEY);
  } catch (e) {
    return 'system';
  }
  return THEME_ORDER.includes(held) ? held : 'system';
}

// Applying a choice is one attribute: the stylesheet keys off it, and its absence
// is what lets the media query answer instead. The ink cache has to go with it —
// it holds resolved colours, and a chart drawn after a change would otherwise
// paint the previous theme's palette onto the new one's surface.
// Just the attribute, for the earliest possible moment — before any chart or
// cached colour exists, so there is nothing yet to invalidate or repaint.
function applyStoredTheme() {
  const choice = themeChoice();
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
}

function applyTheme(choice) {
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
  for (const key of Object.keys(inkCache)) delete inkCache[key];
  for (const id of Object.keys(CHARTS)) {
    const held = CHARTS[id];
    if (held && held.u) held.u.redraw();
  }
  document.dispatchEvent(new CustomEvent('themechange', { detail: { choice } }));
}

// The control itself, put into every page's header from here rather than into
// five headers by hand. A page that adds a header gets it for nothing, and a page
// that forgets cannot end up without it.
function mountThemeButton() {
  const header = document.querySelector('header');
  if (!header || header.querySelector('.themebtn')) return;
  const button = document.createElement('button');
  button.className = 'themebtn';
  button.type = 'button';
  const paint = () => {
    const choice = themeChoice();
    button.textContent = THEME_GLYPH[choice];
    button.title = THEME_SAYS[choice] + ' \u2014 click to change';
    button.setAttribute('aria-label', THEME_SAYS[choice]);
  };
  button.addEventListener('click', () => {
    const next = THEME_ORDER[(THEME_ORDER.indexOf(themeChoice()) + 1) % THEME_ORDER.length];
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch (e) {
      // Nothing to persist to; the choice still applies for this page.
    }
    applyTheme(next);
    paint();
  });
  paint();
  const right = header.lastElementChild;
  if (right && right !== header.firstElementChild) right.appendChild(button);
  else header.appendChild(button);
}

// Applied as soon as the theme block is defined, and deliberately not earlier:
// THEME_KEY and friends are const, so a call above them lands in the temporal
// dead zone. That happened, and the try below caught the ReferenceError and
// answered 'system' — a saved choice silently ignored, with nothing in the
// console and every test still green.
applyStoredTheme();

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

// A chart whose points are days must place its ticks on its own data points and
// never between them: uPlot picks the increment from the span and the pixels,
// and over six days at 1106px it chose twelve hours, so each calendar day
// carried two ticks printing the same date twice. This is the shared factory
// behind those fixed points.
//
// Reading ``u.data[0]`` rather than closing over the array it was built with is
// load-bearing. ``paint()`` builds each chart exactly once and only calls
// ``setData`` afterwards, so anything in ``spec.opts`` that captured an array
// keeps showing the FIRST range's data forever; ``u.data[0]`` is whatever
// ``setData`` last wrote, so the splits stay correct across every refresh,
// every period switch and every zoom. That is also what makes one chart correct
// at both grains: the Efficiency page reuses a single chart across the hourly
// and daily periods, so an axis built for one grain would otherwise survive
// into the other — an axis that ticks on its own points is right at both, and
// the reuse stops mattering.
function pointSplits(minPx) {
  return (u, axisIdx, min, max) => {
    const xs = (u.data[0] || []).filter((t) => t >= min && t <= max);
    if (!xs.length) return [];
    const ratio = (typeof uPlot !== 'undefined' && uPlot.pxRatio) || 1;
    const room = Math.max(1, Math.floor((u.bbox.width / ratio) / minPx));
    const step = Math.max(1, Math.ceil(xs.length / room));
    return xs.filter((_, i) => i % step === 0);
  };
}

// The daily-grain axis. Same formats as ``timeAxis``, but the splits come from
// ``pointSplits`` so a tick can never land between two days. No ``space`` here:
// uPlot consults it only when choosing its own increment, and there is no
// increment left to choose once the splits are given outright — the 80 px that
// keeps neighbouring labels apart is handed to ``pointSplits`` instead.
const pointTimeAxis = () => axis({ size: 32, splits: pointSplits(80), values: timeTicks });
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
// >>> readout-value
// A row's second element is either a series index — a number, as it always was
// — or a function (u, idx) => number | null computing a value no single series
// holds. The test is on the type and not on truthiness, because series 0 is the
// x axis and a truthiness test would call it as a function and throw on the
// first hover of every chart on every page.
function readoutValue(u, idx, si) {
  if (typeof si !== 'function') return numOrNull(u.data[si][idx]);
  // Anything that is not a finite number becomes the dash: null and undefined
  // alike, and the Infinity a division by nothing produces. Letting undefined
  // through would reach the formatter and print whatever it makes of it, which
  // is how a gap starts looking like a reading. A row function that throws is
  // caught here rather than escaping into uPlot's cursor handler, where it
  // would strand the tooltip mid-update with stale text at a stale position.
  let raw;
  try {
    raw = si(u, idx);
  } catch (err) {
    return null;
  }
  return Number.isFinite(raw) ? raw : null;
}
// <<< readout-value

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
          const v = readoutValue(u, idx, si);
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

// A fetch that cannot hang forever. A dongle behind a dead proxy can accept a
// connection and never answer, and a restart watch or an apply that awaits such
// a fetch would leave the button disabled with no way back. The abort turns a
// silent hang into a caught error the caller can report.
function fetchTimeout(url, options, ms) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), ms || 8000);
  return fetch(url, { ...(options || {}), signal: controller.signal })
    .finally(() => clearTimeout(timer));
}

// The message under a rejection, whatever shape it came in. A validation error
// from the framework is a list of {loc, msg}, and printing that list as-is put
// "[object Object]" on the form where the reason should be. A plain string is
// the service's own worded refusal and passes straight through.
function problemText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => (d && d.msg) ? d.msg : String(d)).join('; ') || 'the value was refused';
  }
  return 'the value was refused';
}

// Watch the collector come back after a save-and-restart, for both shells. The
// service SIGTERMs itself and systemd restarts it, so /api/status goes away and
// then answers again — and in first-run setup mode it did not exist to begin
// with, which is the same "not up yet" as a process mid-restart. Ready is
// therefore "reachable again after having been unreachable", never the first
// reachable poll (on the settings page the old process is still answering for
// the moment before it exits). A deadline hands the form back rather than
// spinning forever, and each poll is time-boxed so one hung request cannot stall
// the whole watch.
function watchRestart(onReady, onGiveUp) {
  const deadline = Date.now() + 90000;
  let sawDown = false;
  const poll = async () => {
    let up = false;
    try {
      const r = await fetchTimeout('/api/status', { cache: 'no-store' }, 4000);
      up = r.ok;
    } catch (e) {
      up = false;
    }
    if (!up) {
      sawDown = true;
    } else if (sawDown) {
      onReady();
      return;
    }
    if (Date.now() < deadline) setTimeout(poll, 1500);
    else onGiveUp();
  };
  setTimeout(poll, 800);
}

// ---------------------------------------------------------------------------
// The setup renderer. One component, two shells: the first-run wizard mounts it
// full-screen, the settings page mounts it as the Connection group. It draws the
// whole picker from the /api/setup payload and decides nothing itself — the pure
// functions above (setupModelsFor, setupFieldsFor, setupBatterySourcesFor,
// buildSetupBody) make every choice, so the form can only ever offer what the
// server would accept. Detect is internal and identical in both shells; apply is
// handed out through opts.onApply, because what happens after a write — a
// full-screen restart watch or a settings-page banner — is the shell's business.
//   opts = { mode: 'wizard'|'settings', applyLabel, onApply(body) }
// Returns { read } so a shell can read the mounted form.
// ---------------------------------------------------------------------------
function mountSetup(host, payload, opts) {
  opts = opts || {};
  const cur = (payload && payload.current) || {};
  // First run seeds the form from the payload's TEST-NET placeholder, which is
  // noise, so it starts from sensible defaults instead. A configured system
  // seeds from its real (redacted) current values.
  const firstRun = !!(payload && payload.first_run);
  const makers = (payload && payload.manufacturers) || [];

  // The initial driver/model/manufacturer. Every model option carries its own
  // driver ("driver::model"), because one manufacturer can hold more than one
  // family and a flat model name cannot say which built it.
  function firstDriverModel() {
    for (const mk of makers) {
      for (const fam of mk.families || []) {
        for (const m of fam.models || []) return { maker: mk.name, driver: fam.driver, model: m.name };
      }
    }
    return { maker: '', driver: '', model: '' };
  }
  const seedDriver = !firstRun && cur.driver ? cur.driver : firstDriverModel().driver;
  const seedModel = !firstRun && cur.model ? cur.model : firstDriverModel().model;
  const state = {
    manufacturer: setupMakerOf(payload, seedDriver) || firstDriverModel().maker,
    driver: seedDriver,
    model: seedModel,
    transport: !firstRun && cur.transport ? cur.transport : 'dongle',
    serial_device: !firstRun ? (cur.serial_device || '') : '',
    serial_baud: !firstRun && cur.serial_baud ? cur.serial_baud : 19200,
    serial_unit_id: !firstRun && cur.serial_unit_id ? cur.serial_unit_id : 1,
    dongle_host: !firstRun ? (cur.dongle_host || '') : '',
    dongle_serial: !firstRun ? (cur.dongle_serial || '') : '',
    inverter_serial: !firstRun ? (cur.inverter_serial || '') : '',
    battery_source: !firstRun && cur.battery_source ? cur.battery_source : '',
  };
  // A redacted echo (bullets) is a value the person has not retyped; treated as
  // "leave alone", it is cleared from the form's send but kept visible so they
  // see something is already set. buildSetupBody drops it because it stays === ''
  // only if untouched — so we keep the mask in the input and let apply's own
  // mask-drop (settings._is_mask) discard an unedited one server-side.

  const makerNames = makers.map((m) => m.name);
  const modelOptionsFor = (makerName) => {
    const mk = makers.find((m) => m.name === makerName);
    const out = [];
    for (const fam of (mk && mk.families) || []) {
      for (const m of fam.models || []) {
        out.push({ driver: fam.driver, name: m.name, caveat: m.caveat || '' });
      }
    }
    return out;
  };
  const ports = (payload && payload.ports) || [];

  function numRow(key, label, hint, min, max) {
    const lo = min === undefined ? '' : ` min="${esc(String(min))}"`;
    const hi = max === undefined ? '' : ` max="${esc(String(max))}"`;
    return `<div class="row"><label for="su_${key}">${esc(label)}</label>
      <input id="su_${key}" data-k="${key}" type="number" inputmode="numeric" step="1"${lo}${hi}
        value="${esc(String(state[key]))}">
      <span class="hint">${esc(hint)}</span></div>`;
  }
  function textRow(key, label, hint, extra) {
    return `<div class="row"><label for="su_${key}">${esc(label)}</label>
      <input id="su_${key}" data-k="${key}" type="text" value="${esc(String(state[key] || ''))}"${extra || ''}>
      <span class="hint">${esc(hint)}</span><span class="err" hidden></span></div>`;
  }

  function connectionFields() {
    if (state.transport === 'modbus_serial') {
      const listId = 'su_ports';
      const list = ports.length ? ` list="${listId}"` : '';
      const datalist = ports.length
        ? `<datalist id="${listId}">`
          + ports.map((p) => `<option value="${esc(p.stable)}">${esc(p.target)}</option>`).join('')
          + '</datalist>'
        : '';
      return textRow('serial_device', 'Serial device',
        ports.length ? 'Pick a detected adapter or type a path. A /dev/serial/by-id path survives replugging.'
          : 'The device path for the RS485 adapter, e.g. /dev/ttyUSB0.', list) + datalist
        + `<details class="adv"><summary>Advanced serial settings</summary><div class="fields">`
        + numRow('serial_baud', 'Baud rate', '19200 is the LuxPower default.', 1, 1000000)
        + numRow('serial_unit_id', 'Modbus unit id', 'Which unit answers on the bus. Usually 1.', 1, 247)
        + `</div></details>`;
    }
    // The dongle port is deliberately not offered: it is 8000 on current
    // firmware, is not among the fields the settings overlay accepts, and a box
    // that silently discarded its edits would be worse than its absence. A
    // non-standard port is set in the config file, the one place it takes.
    return textRow('dongle_host', 'Dongle address', 'The IP address or hostname of the WiFi dongle.')
      + textRow('dongle_serial', 'Dongle serial', 'Printed on the dongle, e.g. BA12345678.');
  }

  function render() {
    const models = modelOptionsFor(state.manufacturer);
    // Keep the selected model valid for the chosen manufacturer; if it moved,
    // fall to that manufacturer's first model rather than an empty box.
    if (!models.some((m) => m.name === state.model && m.driver === state.driver)) {
      if (models.length) { state.driver = models[0].driver; state.model = models[0].name; }
    }
    const batteries = setupBatterySourcesFor(payload, state.driver);
    if (!batteries.includes(state.battery_source)) state.battery_source = batteries[0] || 'none';

    const makerSel = makerNames.map((n) =>
      `<option value="${esc(n)}"${n === state.manufacturer ? ' selected' : ''}>${esc(n)}</option>`).join('');
    // A model with a caveat says so in the option itself, not only once it is
    // chosen. Someone scanning the list for their machine decides there and
    // then, and a warning that appears afterwards has already lost the argument.
    const modelSel = models.map((m) => {
      const v = `${m.driver}::${m.name}`;
      const on = m.name === state.model && m.driver === state.driver;
      const label = m.caveat ? `${m.name} — unverified` : m.name;
      return `<option value="${esc(v)}"${on ? ' selected' : ''}>${esc(label)}</option>`;
    }).join('');
    const chosenModel = models.find((m) => m.name === state.model && m.driver === state.driver);
    const modelNote = chosenModel && chosenModel.caveat
      ? `<div class="hint warn-note">${esc(chosenModel.caveat)}</div>` : '';
    const transSel = [['dongle', 'WiFi dongle'], ['modbus_serial', 'RS485 serial']].map(([v, lbl]) =>
      `<option value="${v}"${v === state.transport ? ' selected' : ''}>${lbl}</option>`).join('');
    const battLabel = {
      relayed: 'Closed loop (through the inverter)',
      none: 'None',
      direct: 'Open loop — coming soon',
    };
    const battSel = batteries.map((b) =>
      `<option value="${esc(b)}"${b === state.battery_source ? ' selected' : ''}>${esc(battLabel[b] || b)}</option>`).join('')
      + '<option value="direct" disabled>' + esc(battLabel.direct) + '</option>';
    const applyLabel = opts.applyLabel || (opts.mode === 'wizard' ? 'Set up and start' : 'Save and restart collector');

    host.classList.add('setup');
    host.innerHTML = `
      <div class="row"><label for="su_maker">Manufacturer</label>
        <select id="su_maker" data-role="maker">${makerSel}</select></div>
      <div class="row"><label for="su_model">Model</label>
        <select id="su_model" data-role="model">${modelSel}</select></div>
      ${modelNote}
      <div class="row"><label for="su_transport">Connection</label>
        <select id="su_transport" data-role="transport">${transSel}</select></div>
      <div data-role="fields">${connectionFields()}</div>
      <div class="row"><label for="su_battery">Battery</label>
        <select id="su_battery" data-role="battery">${battSel}</select></div>
      <div class="row"><label for="su_inverter_serial">Inverter serial</label>
        <div class="detectrow">
          <input id="su_inverter_serial" data-k="inverter_serial" type="text"
            value="${esc(String(state.inverter_serial || ''))}">
          <button type="button" class="primary" data-role="detect">Detect</button>
        </div>
        <span class="hint">Read it off the inverter with Detect, or type it in.</span></div>
      <div class="status" data-role="status" aria-live="polite"></div>
      <div class="actions">
        <button type="button" class="primary" data-role="apply">${esc(applyLabel)}</button>
      </div>`;

    wire();
  }

  function wire() {
    const q = (sel) => host.querySelector(sel);
    // Text/number inputs update state in place; no re-render, so focus is kept.
    for (const input of host.querySelectorAll('input[data-k]')) {
      input.addEventListener('input', () => {
        const k = input.dataset.k;
        state[k] = (input.type === 'number') ? (input.value === '' ? '' : Number(input.value)) : input.value;
      });
    }
    q('[data-role="maker"]').addEventListener('change', (e) => {
      state.manufacturer = e.target.value; render();
    });
    q('[data-role="model"]').addEventListener('change', (e) => {
      const [driver, model] = e.target.value.split('::');
      state.driver = driver; state.model = model; render();
    });
    q('[data-role="transport"]').addEventListener('change', (e) => {
      state.transport = e.target.value; render();
    });
    q('[data-role="battery"]').addEventListener('change', (e) => {
      state.battery_source = e.target.value;
    });
    q('[data-role="detect"]').addEventListener('click', detect);
    q('[data-role="apply"]').addEventListener('click', apply);
  }

  function status(msg, cls) {
    const s = host.querySelector('[data-role="status"]');
    if (s) { s.textContent = msg || ''; s.className = 'status' + (cls ? ' ' + cls : ''); }
  }

  async function detect() {
    const btn = host.querySelector('[data-role="detect"]');
    btn.disabled = true;
    status('Reading the inverter’s serial…', 'busy');
    try {
      const r = await fetchTimeout('/api/setup/detect', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(buildSetupBody(state)),
      }, 20000);
      const body = await r.json().catch(() => ({}));
      if (!r.ok) { status(problemText(body.detail) || `The probe failed (${r.status}).`, 'bad'); return; }
      if (body.serial) {
        state.inverter_serial = body.serial;
        const input = host.querySelector('[data-k="inverter_serial"]');
        if (input) input.value = body.serial;
        status(`The inverter answered with serial ${body.serial}.`, 'ok');
      } else {
        status('The probe returned no serial.', 'warn');
      }
    } catch (e) {
      status('The probe could not be reached.', 'bad');
    } finally {
      btn.disabled = false;
    }
  }

  async function apply() {
    if (typeof opts.onApply !== 'function') return;
    const btn = host.querySelector('[data-role="apply"]');
    btn.disabled = true;
    try {
      await opts.onApply(buildSetupBody(state), { status, reenable: () => { btn.disabled = false; } });
    } catch (e) {
      status('The change could not be applied.', 'bad');
      btn.disabled = false;
    }
  }

  render();
  return { read: () => ({ ...state }), status };
}

// ---------------------------------------------------------------------------
// The guided tour.
//
// A first-visit walkthrough, offered as a dismissible strip rather than
// imposed, state-aware enough to skip what an installation does not have, and
// persisted per browser because a tour is shown to a person and an installation
// is read by several — one household member waving it away on the kitchen
// tablet must not silence it on somebody else's phone. The owner's decision on
// 12 August 2026 is that dismissal is a localStorage fact under the existing
// as.* scheme (as.tourStep), not a registered setting: every write path
// through SettingsStore raises KeyError for a key outside the SETTINGS tuple,
// and a per-browser interface state is exactly what as.calDismissed and as.tab
// already are. The design this implements is
// docs/superpowers/specs/2026-08-12-guided-tour-design.md.
//
// The tour is not a running script that survives navigation. Now, Energy flow
// and Inverter are three hash views of one document, while Graphs, History,
// Costs, Efficiency and Settings are separate documents reached by full page
// loads — so a cross-page tour cannot hold its position in memory. It is a
// short sequence, resumed from a stored step at the top of every page, that
// either has a next step on this page (show it) or does not (stay quiet,
// having recorded nothing).
//
// The step list and its gates are pure and DOM-free so node can hold them to
// the same capability, status and settings payloads the pages already read
// (tests/test_tour_js.py), the same way test_dashboard_caps_js.py slices the
// caps-logic markers. Everything that touches the document sits below the
// markers.
// ---------------------------------------------------------------------------

// >>> tour-logic
// The terminal value of as.tourStep. Absent means "never offered" (the offer
// may show); a step id means "resume here"; this means the tour was finished
// or declined. Finished and declined are deliberately the same state: a
// completed tour and a skipped one must not be told apart, or a returning
// visitor who waved the offer away once would be asked again.
const TOUR_DONE = 'done';

// Every step in tour order. `page` is one of the NAV keys — the hash views
// (now, flow, inverter) are one document, the rest are separate documents.
// `selector` anchors the popover to an element present in the page's static
// markup, never one injected after a fetch. `gate` is a pure function of
// (caps, status, settings) deciding whether this step runs at all; a step's
// copy never states a number, a threshold or a capability value, because it
// must stay true whatever the cards beside it read.
const TOUR_STEPS = [
  {
    id: 'now-live',
    page: 'now',
    selector: '#pv',
    title: 'The live cards',
    body: 'These cards are the live reading of the installation — what the panels are making, '
      + 'what the house is drawing, and which way the battery is moving. The line above them '
      + 'says what mode the system is running in.',
    gate: null,
  },
  {
    id: 'now-modules',
    page: 'now',
    selector: '#mods',
    title: 'One card per battery pack',
    body: 'Each battery pack gets a card of its own here — its charge, voltage and temperature — '
      + 'so a pack running differently shows up as itself rather than hiding inside the bank total.',
    gate: tourHasModules,
  },
  {
    id: 'inverter-legs',
    page: 'inverter',
    selector: '#dtl',
    title: 'The comparisons that diagnose',
    body: 'This panel compares the readings that diagnose the system: each string against the '
      + 'others, the backup output leg by leg, and the BMS\'s word against the inverter\'s.',
    gate: tourHasBackup,
  },
  {
    id: 'flow-sankey',
    page: 'flow',
    selector: '#sankey',
    title: 'Where today\'s energy went',
    body: 'Each stream is one path the day\'s energy took — from the panels to the house, the '
      + 'battery or the grid. The wider a stream, the more energy it carries.',
    gate: null,
  },
  {
    id: 'graphs-bands',
    page: 'graphs',
    selector: '#solarBands',
    title: 'One small chart per reading',
    body: 'Every chart on this page is one reading drawn over the range you pick, so a change in '
      + 'one thing is never hidden by the scale of another.',
    gate: null,
  },
  {
    id: 'graphs-strings',
    page: 'graphs',
    selector: '#solarBands',
    title: 'Each string on its own',
    body: 'Every string of the array gets its own power, voltage and current here, so a weak '
      + 'string shows up as itself rather than being averaged into the total.',
    gate: tourHasStrings,
  },
  {
    id: 'costs-priced',
    page: 'costs',
    selector: '#cards',
    title: 'What this month costs',
    body: 'These cards price the month\'s energy against the tariff you entered — what it has '
      + 'cost so far, what the bill looks like at this pace, and what the solar and battery have saved.',
    gate: tourHasTariff,
  },
];

// A gate is a function of (caps, status, settings) — the three payloads every
// page fetches for itself — and a missing gate means the step always runs.
function tourGatePasses(step, caps, status, settings) {
  return typeof step.gate === 'function'
    ? step.gate(caps, status, settings) !== false
    : true;
}

// Whether the device reports per-pack battery data. An explicit yes only: null
// is a bare source that has not declared, and the step must fail closed (skip)
// rather than describe module cards an undeclared installation may not have.
function tourHasModules(caps, status, settings) {
  return !!(caps && caps.per_module_battery === true);
}

// Whether the backup-output panels exist. This is index.html's own rendering
// rule (caps.backup_output !== false) read from the same place: only an
// explicit "no" suppresses, and null keeps the step because the page still
// draws the panel while the declaration is unknown.
function tourHasBackup(caps, status, settings) {
  return !caps || caps.backup_output !== false;
}

// Whether strings are declared at all. capStrings already treats null as
// "unknown, never zero"; this reuses that rule so an undeclared source fails
// closed instead of walking somebody through string bands that may not exist.
function tourHasStrings(caps, status, settings) {
  const n = capStrings(caps);
  return n !== null && n >= 1;
}

// Whether a tariff is entered, read from /api/settings and not from
// /api/costs. The Costs page gates itself on this same value rendered from
// the same endpoint; asking /api/costs and inferring absence from missing
// money would conflate "nothing entered" with "the month has not started".
function tourHasTariff(caps, status, settings) {
  return !!(settings && settings.values && String(settings.values['tariff.bands'] || '').trim());
}

// The steps whose gates pass for this installation, in tour order. Boot
// filters every page against this list; a step whose gate no longer passes (a
// string removed between visits, say) is silently skipped rather than
// stranding the tour pointing at something no longer there.
function tourPassingSteps(caps, status, settings) {
  return TOUR_STEPS.filter((s) => tourGatePasses(s, caps, status, settings));
}

// The one condition that suppresses the tour outright rather than running it
// against a broken installation: the collector is not running at all, which is
// the stale banner's job to say loudly. "No data yet" is not a suppression —
// seconds after setup it is the normal state, and the tour runs against the
// skeleton.
function tourSuppressed(status) {
  if (!status || typeof status !== 'object') return false;
  if (status.running === false) return true;
  const verdict = status.staleness && status.staleness.verdict;
  return verdict === 'not_running';
}
// <<< tour-logic

// ---------------------------------------------------------------------------
// Rendering. Everything below touches the document and is deliberately outside
// the node-extracted slice above.
// ---------------------------------------------------------------------------

const TOUR_KEY = 'as.tourStep';
const TOUR_INDEX_KEYS = ['now', 'flow', 'inverter'];
const TOUR_UNSUPPORTED = '\u0000unsupported';

// The popover reuses the hover readout's --tip / --tip-shadow tokens, so it
// inherits the palette's validated light/dark contrast without introducing a
// colour of its own — the tour has no series to represent and must not add a
// hue nobody checked. The buttons copy .calhide's look.
const TOUR_CSS = `
  .tourpop{position:fixed;z-index:60;max-width:290px;background:var(--tip);
    border:1px solid var(--panel-b);border-radius:11px;box-shadow:var(--tip-shadow);
    padding:11px 13px;font-size:12.5px;line-height:1.5;text-align:left}
  .tourpop[hidden]{display:none}
  .tourpop .tourhead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
  .tourpop h2{margin:0 0 4px;font-size:13px;font-weight:600;letter-spacing:-.01em;color:var(--ink)}
  .tourpop p{margin:0 0 10px;color:var(--ink2);max-width:52ch}
  .tourpop .tourx{background:none;border:0;color:var(--ink3);cursor:pointer;font-size:12px;
    padding:2px 4px;font-family:inherit;line-height:1}
  .tourpop .tourx:hover{color:var(--ink)}
  .tourpop .tourbar{display:flex;align-items:center;gap:8px;justify-content:space-between;margin-top:2px}
  .tourpop .tourbar button{background:var(--tint);border:1px solid var(--panel-b);
    color:var(--ink2);border-radius:7px;padding:3px 11px;font:inherit;font-size:11px;cursor:pointer;font-family:inherit}
  .tourpop .tourbar button:hover{background:var(--tint-3);color:var(--ink)}
  .tourpop .tourbar button[hidden]{display:none}
  .tourpop .tourbar .tournext{background:var(--tint-3);color:var(--ink);font-weight:600}
  .tourpop .tourcount{font-size:10px;color:var(--ink3);letter-spacing:.03em}
`;
document.head.appendChild(Object.assign(document.createElement('style'), { textContent: TOUR_CSS }));

let tourData = null;
let tourDataPromise = null;
let tourCurrentStep = null;
let tourPopPoll = null;
let tourRepositionQueued = false;

function tourIsIndexDocument() {
  const p = location.pathname;
  return p === '/' || p.endsWith('/index.html');
}

function tourCurrentPage() {
  if (tourIsIndexDocument()) {
    const hash = location.hash.replace('#', '');
    return TOUR_INDEX_KEYS.includes(hash) ? hash : 'now';
  }
  const base = location.pathname.replace(/^\//, '').replace(/\.html$/, '');
  return base || 'now';
}

function tourSameDocument(page) {
  return TOUR_INDEX_KEYS.includes(page);
}

function tourHref(page) {
  const entry = NAV.find((n) => n.key === page);
  return entry ? entry.href : '/' + page;
}

function tourRead() {
  try {
    return localStorage.getItem(TOUR_KEY);
  } catch (e) {
    // Private browsing refuses localStorage outright; a tour that cannot
    // persist its position would restart on every page, so it is not offered.
    return TOUR_UNSUPPORTED;
  }
}

function tourWrite(value) {
  try {
    localStorage.setItem(TOUR_KEY, value);
    return true;
  } catch (e) {
    return false;
  }
}

async function tourFetchData() {
  const [setupRes, capsRes, statusRes, settingsRes] = await Promise.allSettled([
    fetchTimeout('/api/setup', {}, 6000),
    fetchTimeout('/api/capabilities', {}, 6000),
    fetchTimeout('/api/status', {}, 6000),
    fetchTimeout('/api/settings', {}, 6000),
  ]);
  const json = async (r) => (
    r.status === 'fulfilled' && r.value.ok ? r.value.json().catch(() => null) : null
  );
  const setup = await json(setupRes);
  const capsRaw = await json(capsRes);
  const status = await json(statusRes);
  const settings = await json(settingsRes);
  return {
    // The wizard owns index.html while the service is unconfigured; the tour
    // has nothing to point at and the offer would sit over the setup form.
    firstRun: !!(setup && setup.first_run === true),
    caps: capsRaw && Array.isArray(capsRaw.devices) ? capsRaw.devices[0] || null : null,
    status,
    settings,
  };
}

function tourEnsureData() {
  if (tourData) return Promise.resolve(tourData);
  if (!tourDataPromise) {
    tourDataPromise = tourFetchData().then((d) => { tourData = d; return d; });
  }
  return tourDataPromise;
}

function tourStepAfter(eff, id) {
  const idx = eff.findIndex((s) => s.id === id);
  return idx >= 0 && idx + 1 < eff.length ? eff[idx + 1] : null;
}

function renderPop(step, eff, next) {
  const pop = $('tourPop');
  if (!pop) return;
  const idx = eff.findIndex((s) => s.id === step.id);
  $('tourTitle').textContent = step.title;
  $('tourBody').textContent = step.body;
  $('tourBack').hidden = idx <= 0;
  $('tourNext').textContent = next ? 'Next' : 'Done';
  $('tourCount').textContent = `${idx + 1} of ${eff.length}`;
  pop.hidden = false;
}

function positionPop(step) {
  const pop = $('tourPop');
  if (!pop || pop.hidden) return;
  const target = document.querySelector(step.selector);
  const rect = target ? target.getBoundingClientRect() : null;
  const offscreen = !target || !rect
    || (rect.width === 0 && rect.height === 0)
    || target.hidden === true;
  if (offscreen) {
    // The anchor is not on screen yet — a section still waiting on its fetch,
    // or a view being switched to. Park the popover at the top of the page;
    // the poll below moves it once the anchor actually appears.
    pop.style.left = '50%';
    pop.style.top = '70px';
    pop.style.transform = 'translateX(-50%)';
    return;
  }
  pop.style.transform = '';
  const pw = pop.offsetWidth || 280;
  const ph = pop.offsetHeight || 120;
  const gap = 10;
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  let top = rect.top - ph - gap;
  let left = rect.left + rect.width / 2 - pw / 2;
  if (top < 8) top = rect.bottom + gap;
  left = Math.max(8, Math.min(left, vw - pw - 8));
  top = Math.max(8, Math.min(top, vh - ph - 8));
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function startPopPoll() {
  stopPopPoll();
  // A late-rendered anchor (the Costs cards appear only after /api/costs
  // resolves) needs the popover to move once it does. This only runs while a
  // step is open and costs one layout read per tick.
  tourPopPoll = setInterval(() => {
    if (!tourCurrentStep) { stopPopPoll(); return; }
    const pop = $('tourPop');
    if (!pop || pop.hidden) { stopPopPoll(); return; }
    positionPop(tourCurrentStep);
  }, 400);
}

function stopPopPoll() {
  if (tourPopPoll) { clearInterval(tourPopPoll); tourPopPoll = null; }
}

function hidePop() {
  const pop = $('tourPop');
  if (pop) pop.hidden = true;
  tourCurrentStep = null;
  stopPopPoll();
}

function onTourReposition() {
  if (!tourCurrentStep || tourRepositionQueued) return;
  tourRepositionQueued = true;
  requestAnimationFrame(() => {
    tourRepositionQueued = false;
    if (tourCurrentStep) positionPop(tourCurrentStep);
  });
}

function onTourHashChange() {
  // The dashboard's three views share one document. If the visitor navigates
  // away from the step's view, the popover must not keep floating over a view
  // it does not describe; it hides, and the stored step resumes on the next
  // page load.
  if (tourCurrentStep && tourCurrentPage() !== tourCurrentStep.page) hidePop();
}

function buildPop() {
  if ($('tourPop')) return;
  const pop = document.createElement('div');
  pop.className = 'tourpop';
  pop.id = 'tourPop';
  pop.hidden = true;
  pop.setAttribute('role', 'region');
  pop.setAttribute('aria-label', 'Tour step');
  pop.innerHTML = `
    <div class="tourhead">
      <h2 id="tourTitle"></h2>
      <button type="button" class="tourx" id="tourClose" title="Stop for now">✕</button>
    </div>
    <p id="tourBody"></p>
    <div class="tourbar">
      <button type="button" id="tourBack">Back</button>
      <span class="tourcount" id="tourCount"></span>
      <button type="button" class="tournext" id="tourNext">Next</button>
    </div>`;
  document.body.appendChild(pop);
  $('tourClose').addEventListener('click', () => { tourWrite(TOUR_DONE); hidePop(); });
  $('tourBack').addEventListener('click', () => { if (tourCurrentStep) tourBack(tourCurrentStep); });
  $('tourNext').addEventListener('click', () => { if (tourCurrentStep) tourAdvance(tourCurrentStep); });
  window.addEventListener('scroll', onTourReposition, { passive: true });
  window.addEventListener('resize', onTourReposition);
  window.addEventListener('hashchange', onTourHashChange);
}

function tourShow(step) {
  if (!tourData) return;
  const eff = tourPassingSteps(tourData.caps, tourData.status, tourData.settings);
  const next = tourStepAfter(eff, step.id);
  // While a step is showing, the stored value is the NEXT step to show, so a
  // page reload resumes past the step already seen. The last step stores the
  // terminal value.
  tourWrite(next ? next.id : TOUR_DONE);
  hideOffer();
  tourCurrentStep = step;
  renderPop(step, eff, next);
  // Within the dashboard document the step may live on a different hash view;
  // drive wireTabs()'s own routing rather than reimplementing it.
  if (tourSameDocument(step.page)) {
    const hash = '#' + step.page;
    if (location.hash !== hash) {
      location.hash = hash;
      requestAnimationFrame(() => requestAnimationFrame(() => positionPop(step)));
    } else {
      positionPop(step);
    }
  } else {
    positionPop(step);
  }
  startPopPoll();
}

function tourAdvance(step) {
  if (!tourData) return;
  const eff = tourPassingSteps(tourData.caps, tourData.status, tourData.settings);
  const next = tourStepAfter(eff, step.id);
  if (!next) {
    tourWrite(TOUR_DONE);
    hidePop();
    return;
  }
  if (tourSameDocument(next.page)) {
    tourShow(next);
  } else {
    // The next step is a separate document. Persist it and let that page's own
    // boot pick it up — the same path a full page navigation always takes.
    tourWrite(next.id);
    location.href = tourHref(next.page);
  }
}

function tourBack(step) {
  if (!tourData) return;
  const eff = tourPassingSteps(tourData.caps, tourData.status, tourData.settings);
  const idx = eff.findIndex((s) => s.id === step.id);
  if (idx <= 0) return;
  const prev = eff[idx - 1];
  if (tourSameDocument(prev.page)) {
    tourShow(prev);
  } else {
    tourWrite(prev.id);
    location.href = tourHref(prev.page);
  }
}

function showOffer() {
  if ($('tourOffer')) return;
  const nav = $('nav');
  if (!nav || !nav.parentNode) return;
  const box = document.createElement('div');
  box.className = 'p cal tour-offer';
  box.id = 'tourOffer';
  box.setAttribute('role', 'region');
  box.setAttribute('aria-label', 'Tour offer');
  box.innerHTML = `
    <div class="calmark" aria-hidden="true">?</div>
    <div class="calbody">
      <h2>Take a quick tour</h2>
      <p>A short walkthrough of the pages — what each card is for and where the numbers come
        from. You can stop it at any point, and this browser won't be asked again.</p>
    </div>
    <button class="calhide" id="tourOfferGo" type="button">See what's on this page</button>
    <button class="calhide" id="tourOfferClose" type="button"
      title="Don't show the tour on this browser again">Not now</button>`;
  nav.parentNode.insertBefore(box, nav.nextSibling);
  $('tourOfferGo').addEventListener('click', tourStart);
  $('tourOfferClose').addEventListener('click', () => { tourWrite(TOUR_DONE); hideOffer(); });
}

function hideOffer() {
  const box = $('tourOffer');
  if (box) box.remove();
}

function mountTourButton() {
  const header = document.querySelector('header');
  if (!header || header.querySelector('.tourbtn')) return;
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'iconbtn tourbtn';
  button.textContent = 'Show me around';
  button.title = 'Start the guided tour from the beginning';
  button.setAttribute('aria-label', 'Start the guided tour from the beginning');
  button.addEventListener('click', () => { hideOffer(); tourStart(); });
  const theme = header.querySelector('.themebtn');
  if (theme && theme.parentNode) {
    theme.parentNode.insertBefore(button, theme.nextSibling);
  } else {
    const right = header.lastElementChild;
    if (right && right !== header.firstElementChild) right.appendChild(button);
    else header.appendChild(button);
  }
}

async function tourStart() {
  const data = await tourEnsureData();
  if (!data || data.firstRun) return;
  if (tourSuppressed(data.status)) return;
  const eff = tourPassingSteps(data.caps, data.status, data.settings);
  if (!eff.length) return;
  const first = eff[0];
  hideOffer();
  if (tourSameDocument(first.page)) {
    tourShow(first);
  } else {
    // A deliberate restart begins at the very first step, wherever it lives.
    tourWrite(first.id);
    location.href = tourHref(first.page);
  }
}

async function tourBoot() {
  mountTourButton();
  const stored = tourRead();
  if (stored === TOUR_UNSUPPORTED || stored === TOUR_DONE) return;
  const data = await tourEnsureData();
  if (!data || data.firstRun) return;
  if (tourSuppressed(data.status)) return;
  const page = tourCurrentPage();
  if (stored === null) {
    if (tourIsIndexDocument()) showOffer();
    return;
  }
  // Resume from the stored step: walk the tour in order to the first step that
  // still passes its gate, and show it if this is its page. A step whose gate
  // no longer passes is silently skipped, and if nothing after the stored step
  // passes any more the tour is over.
  const eff = tourPassingSteps(data.caps, data.status, data.settings);
  const start = TOUR_STEPS.findIndex((s) => s.id === stored);
  if (start === -1) return;
  for (let i = start; i < TOUR_STEPS.length; i++) {
    const s = TOUR_STEPS[i];
    if (eff.includes(s)) {
      if (s.page === page) tourShow(s);
      return;
    }
  }
  tourWrite(TOUR_DONE);
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => { buildPop(); tourBoot(); });
} else {
  buildPop();
  tourBoot();
}
