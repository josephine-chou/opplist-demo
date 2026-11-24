// A vendor's rates for one shipment: typed on the case page from a Rate requests row, saved as vendorRates/{id} with a
// rates action of the same id. Pure functions (no store and no DOM, so they can be tested in Node):
//   - the lines a vendor's rates start with (the freight from the case's cargo), the units as the quote card writes
//     them (so a vendor's line goes into the quote as it is) plus W/M
//   - what each line comes to for this shipment: Qty from the case's cargo (the quote card's counts) unless typed,
//     max(Rate x Qty, Min) unless an amount is typed, or a word (AT COST, INCLUDED, IF APPLICABLE, N/A)
//   - the document the site saves, what stops a save, and on the case page each vendor's newest rates per route,
//     Lowest per route and currency, what the vendor did not say
//   - the Read quote drafts a row offers, and a draft's option as
//     the drawer's card (each line with what the vendor wrote)
//   - whether a vendor picked on the quote card can go in at once (fillsTyped), and a
//     start line in another unit taking their price (mergeQuoteLines)
import * as Q from "./quotemail.js";
import { farCountries, worksIn, quotedFor } from "./askmail.js";
import { day, cut, nowMs } from "./lib.js";

export const SEGS = ["Freight", "Inland", "Origin", "Destination", "Customs", "Other"];
// the quote card's units (quotemail.js UNITS), with W/M for LCL; a container type first, as vendors quote
export const UNITS = ["20'", "40'", "40'HQ", "45'HQ", "CNTR", "KG", "CBM", "W/M", "SHIPMENT", "BL", "HBL", "HAWB", "AWB", "ENTRY", "FILING", "TRIP", "PICK UP", "PALLET", "DAY"];
export const WORDS = Q.WORDS;
// the three things to ask, and what the vendor said about each
export const SAID = [["fuel", "Fuel"], ["local", "Local charges"], ["min", "Minimum"]];
export const SAID_VALUES = [["incl", "In the rates"], ["excl", "On top"], ["none", "None"], ["notsaid", "Not said"]];
export const MAX_LINES = 40;
export const SHORT_MAX = 60;           // transit, carrier, free time (OppList checks the same)
export const NOTE_MAX = 300;
export const LABEL_MAX = 60;           // an option's name (OppList checks the same)
export const TEXT_MAX = 200;           // a charge's name, a unit

const txt = s => String(s ?? "").trim();
const up = s => txt(s).toUpperCase();
const ms = v => v?.toMillis ? v.toMillis() : v instanceof Date ? v.getTime() : Number(v) || 0;
const iso = s => { if (!/^\d{4}-\d{2}-\d{2}$/.test(s || "")) return false; const d = new Date(s + "T00:00:00Z"); return !isNaN(d) && d.toISOString().slice(0, 10) === s; };

export const blankLine = (seg = "Freight", charge = "", unit = "") => ({ seg, charge, unit, rate: "", min: "", qty: "", amount: "" });

// ---------------------------------------------------------------- the routes
// a route's number (cases/{OPP}.routes[].n), a letter shown; the case's mode when the route has none
export const routesOf = c => (c?.routes || []).filter(r => typeof r.n === "number");
export const routeOf = (c, n) => routesOf(c).find(r => r.n === n) || null;
export function modeFor(c, n) {
  const r = n === null || n === undefined ? null : routeOf(c, n);
  const m = r?.mode && r.mode !== "Not specified" ? r.mode : Q.modeOf(c);
  return m === "Not specified" ? "" : m;
}
// the route the rates are for: the one asked, else the case's only route, else none (a case with several routes picks)
export const defaultRoute = c => { const rs = routesOf(c); return rs.length ? rs[0].n : null; };

// ---------------------------------------------------------------- what a line comes to
// how many of a unit this shipment has, as the quote card counts it (containers of that type, all containers, the
// chargeable kilos by air, the W/M); null when the case does not say
export function autoQty(c, unit, mode) {
  const u = Q.unitText(unit);
  if (!u) return null;
  const cc = { ...(c || {}), mode: mode || Q.modeOf(c) };
  if (u === "W/M") { const w = Q.wm(cc); return w ? w.wm : null; }
  if (u === "KG" && !Q.chargeable(cc)) return null;
  if ((u === "CBM") && !Q.wm(cc)) return null;
  return Q.qtyFor(cc, u);
}
// the quantity a line counts: typed, else the case's, else 1
export function qtyOf(l, auto) {
  if (txt(l?.qty)) return Q.num(l.qty);
  return auto === null || auto === undefined ? 1 : auto;
}
// a word, an amount typed by hand (a lump sum), else max(Rate x Qty, Min); null when there is nothing to count
export function amountOf(l, auto) {
  const w = Q.word(l?.rate); if (w) return { w };
  if (txt(l?.amount)) { const n = Q.num(l.amount); return n === null ? null : { n: Q.r2(n), hand: true }; }
  const r = Q.num(l?.rate); if (r === null) return null;
  const q = qtyOf(l, auto); if (q === null) return null;
  const min = Q.num(l?.min);
  const v = Q.r2(r * q);
  return min !== null && min > v ? { n: Q.r2(min), min: true } : { n: v };
}
export const used = lines => (lines || []).filter(l => txt(l.charge) || txt(l.rate) || txt(l.amount));
export function totalOf(lines, autoFn = () => null) {
  let t = 0, any = false, atCost = false;
  for (const l of used(lines)) {
    const a = amountOf(l, autoFn(l.unit));
    if (a?.n !== undefined) { t += a.n; any = true; }
    if (a?.w === "AT COST") atCost = true;
  }
  return { t: Q.r2(t), any, atCost };
}

// ---------------------------------------------------------------- the lines it starts with
// the freight of the case's cargo: an FCL line per container type, air per chargeable kilo, LCL per W/M, a truck per
// trip; else one empty line. The vendor's own lines are typed under them.
export function startLines(c, mode) {
  const out = [];
  if (mode === "FCL") {
    const { b } = Q.boxesOf(c);
    for (const u of Object.keys(b)) out.push(blankLine("Freight", "OCEAN FREIGHT", u));
    if (!out.length) out.push(blankLine("Freight", "OCEAN FREIGHT", "CNTR"));
  } else if (mode === "Air") out.push(blankLine("Freight", "AIR FREIGHT", "KG"));
  else if (mode === "LCL") out.push(blankLine("Freight", "OCEAN FREIGHT", "W/M"));
  else if (mode === "Truck" || mode === "Cross-border Truck") out.push(blankLine("Inland", "TRUCKING", "TRIP"));
  else out.push(blankLine());
  return out;
}
// a saved version's lines as the drawer edits them: an amount kept only when it was typed (not what the line counts
// to), a quantity only when it is not the case's own count any more
export function linesFromDoc(d, autoFn = () => null) {
  const s = v => v === null || v === undefined ? "" : String(v);
  const out = (d?.lines || []).map(l => {
    const x = { seg: SEGS.includes(l.seg) ? l.seg : "Other", charge: s(l.charge), unit: s(l.unit), rate: l.word || s(l.rate), min: s(l.min), qty: "", amount: "" };
    const auto = autoFn(x.unit);
    if (l.qty !== null && l.qty !== undefined && !(auto !== null && Math.abs(Number(l.qty) - auto) < 1e-9)) x.qty = s(l.qty);
    if (!l.word && l.amount !== null && l.amount !== undefined) {
      const counted = amountOf({ ...x, amount: "" }, auto);
      if (!(counted?.n !== undefined && Math.abs(counted.n - Number(l.amount)) < 0.005)) x.amount = s(l.amount);
    }
    return x;
  });
  return out.length ? out : [blankLine()];
}

// ---------------------------------------------------------------- saving
// what stops a save: [text]
export function problems(card, { today = "", existing = [] } = {}) {
  const out = [];
  // an option's name. existing: the names of the vendor's options on the route.
  // An option opened again keeps its name (fixed); + Another option needs a name the vendor does not have there yet; any
  // other (a first set, a draft's option) needs one when the vendor has named options there (the same name is a new
  // version of that option)
  if (typeof card?.label === "string" && !card.fixed) {
    const k = labelKey(card.label), have = new Set((existing || []).map(labelKey));
    if (txt(card.label).length > LABEL_MAX) out.push(`The option's name is longer than ${LABEL_MAX} characters.`);
    else if (card.isNew && !k) out.push("Name this option.");
    else if (card.isNew && have.has(k)) out.push(`${card.vendor || "The vendor"} already has an option named “${txt(card.label)}” on this route: open it under Their options.`);
    else if (!k && [...have].some(x => x)) out.push(`Name this option: ${card.vendor || "the vendor"} has named options on this route (the same name saves a new version of that one).`);
  }
  if (!/^[A-Z]{3}$/.test(card?.cur || "")) out.push("Pick the currency.");
  if (!iso(card?.on)) out.push("Pick the day they sent the rates.");
  else if (today && card.on > today) out.push("The day they sent the rates cannot be after today.");
  if (txt(card?.valid) && !iso(card.valid)) out.push("Valid until is not a date: pick it from the calendar.");
  const ls = used(card?.lines);
  if (!ls.length) out.push("Type at least one charge and its rate.");
  if (ls.length > MAX_LINES) out.push(`At most ${MAX_LINES} lines.`);
  (card?.lines || []).forEach((l, i) => {
    if (!used([l]).length) return;
    const n = i + 1, w = Q.word(l.rate);
    if (!txt(l.charge)) out.push(`Line ${n}: name the charge.`);
    if (!SEGS.includes(l.seg)) out.push(`Line ${n}: pick the part.`);
    if (!w && txt(l.rate) && Q.num(l.rate) === null) out.push(`Line ${n}: the rate is not a number (or AT COST, INCLUDED, IF APPLICABLE, N/A).`);
    if (!w && !txt(l.rate) && !txt(l.amount)) out.push(`Line ${n}: type the rate, or the amount.`);
    for (const [f, label] of [["min", "minimum"], ["qty", "quantity"], ["amount", "amount"]]) {
      if (!txt(l[f])) continue;
      const v = Q.num(l[f]);
      if (v === null) out.push(`Line ${n}: the ${label} is not a number.`);
      else if (v < 0) out.push(`Line ${n}: the ${label} is below 0.`);
    }
    if (!w && Q.num(l.rate) !== null && Q.num(l.rate) < 0) out.push(`Line ${n}: the rate is below 0.`);
    if (txt(l.charge).length > TEXT_MAX || txt(l.unit).length > TEXT_MAX) out.push(`Line ${n}: the charge or the unit is too long.`);
  });
  for (const [f, label] of [["transit", "Transit time"], ["carrier", "Carrier"], ["free", "Free time"]]) {
    if (txt(card?.[f]).length > SHORT_MAX) out.push(`${label} is longer than ${SHORT_MAX} characters.`);
  }
  if (txt(card?.note).length > NOTE_MAX) out.push(`The note is longer than ${NOTE_MAX} characters.`);
  return out;
}
// the vendorRates document (without by, name and at, which the site adds): every line as OppList keeps it
export function docOf(card, { opp, autoFn = () => null }) {
  const lines = used(card.lines).map(l => {
    const w = Q.word(l.rate), auto = autoFn(l.unit), a = amountOf(l, auto), q = qtyOf(l, auto);
    return { seg: l.seg, charge: up(l.charge), unit: Q.unitText(l.unit), rate: w ? null : Q.num(l.rate), min: w ? null : Q.num(l.min),
      qty: q === null || q === undefined ? null : q, amount: a?.n !== undefined ? a.n : null, word: w };
  });
  const T = totalOf(card.lines, autoFn);
  const said = Object.fromEntries(SAID.map(([k]) => [k, SAID_VALUES.some(([v]) => v === card.said?.[k]) ? card.said[k] : "notsaid"]));
  const lab = typeof card.label === "string" ? { label: txt(card.label).replace(/\s+/g, " ").slice(0, LABEL_MAX) } : {};
  return { opp, vendor: card.vendor, ...lab, cur: card.cur, total: T.any ? T.t : null, lines, valid: txt(card.valid),
    transit: txt(card.transit), carrier: txt(card.carrier), free: txt(card.free), said, note: txt(card.note),
    route: typeof card.route === "number" ? card.route : null, on: card.on };
}

// ---------------------------------------------------------------- on the case page
// each vendor's newest rates per route that OppList has not struck out (cases/{OPP}.rx): {key: {doc, older}},
// key "vendor|route"
// a vendor's rates are per route and option ("" for its one set);
// an option's name is compared without regard to case or extra spaces
export const labelKey = s => String(s ?? "").trim().replace(/\s+/g, " ").toLowerCase();
export const keyOf = (vendor, route, label = "") => `${vendor}|${route ?? ""}|${labelKey(label)}`;
export function newest(docs, rx = []) {
  const struck = new Set(rx || []);
  const out = new Map();
  const sorted = (docs || []).filter(d => !struck.has(d.id)).sort((a, b) => ms(a.at) - ms(b.at) || String(a.id).localeCompare(String(b.id)));
  for (const d of sorted) {
    const k = keyOf(d.vendor, d.route, d.label), was = out.get(k);
    out.set(k, { doc: d, older: was ? was.older + 1 : 0 });
  }
  return out;
}
// the lowest total in each route and currency, when at least two sets of rates gave one: the set of the lowest keys.
// Every vendor's options count, each a set; with today, the ones whose valid until has passed do not
export function lowest(best, today = "") {
  const groups = new Map();
  for (const [k, { doc }] of best) {
    if (typeof doc.total !== "number" || (today && doc.valid && doc.valid < today)) continue;
    const g = `${doc.route ?? ""}|${doc.cur}`;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push([k, doc.total]);
  }
  const out = new Set();
  for (const list of groups.values()) {
    if (list.length < 2) continue;
    const min = Math.min(...list.map(([, t]) => t));
    for (const [k, t] of list) if (t === min) out.add(k);
  }
  return out;
}
export const notSaid = d => SAID.filter(([k]) => d?.said?.[k] === "notsaid").map(([, label]) => `${label} not said`);
export const totalText = d => typeof d?.total === "number" ? `${d.cur} ${Q.money(d.total)}${(d.lines || []).some(l => l.word === "AT COST") ? " plus AT COST" : ""}` : "No total";
// a vendor's options on one route, "" first, then by name: [{key, doc, older}]
export function optionsOf(best, vendor, route) {
  const out = [];
  for (const [k, v] of best || new Map()) if (v.doc.vendor === vendor && (v.doc.route ?? null) === (route ?? null)) out.push({ key: k, doc: v.doc, older: v.older });
  return out.sort((a, b) => labelKey(a.doc.label).localeCompare(labelKey(b.doc.label)));
}
// a vendor's rates on its Rate requests row, route by route: [{route, key, doc, older, lowest, past, options}].
// With options, one entry a route: the lowest of them (else the first), Lowest when any of them is
export function ratesOfVendor(best, low, vendor, today = "") {
  const routes = new Map();
  for (const [k, v] of best) {
    if (v.doc.vendor !== vendor) continue;
    const r = v.doc.route ?? null;
    if (!routes.has(r)) routes.set(r, []);
    routes.get(r).push({ key: k, doc: v.doc, older: v.older });
  }
  const out = [];
  for (const [route, list] of routes) {
    list.sort((a, b) => labelKey(a.doc.label).localeCompare(labelKey(b.doc.label)));
    const pick = list.find(x => low.has(x.key)) || list[0];
    out.push({ route, key: pick.key, doc: pick.doc, older: pick.older, lowest: list.some(x => low.has(x.key)),
      past: !!(pick.doc.valid && today && pick.doc.valid < today), options: list });
  }
  return out.sort((a, b) => (a.route ?? 0) - (b.route ?? 0));
}

// ---------------------------------------------------------------- Compare rates
// the routes where two vendors or more have rates; the
// Rate requests panel offers Compare rates when there is one
export function compareRoutes(best) {
  const n = new Map();
  for (const { doc } of (best || new Map()).values()) { const r = doc.route ?? null; n.set(r, (n.get(r) || 0) + 1); }
  return [...n].filter(([, k]) => k >= 2).map(([r]) => r).sort((a, b) => (a ?? 0) - (b ?? 0));
}
// the lowest of one row: marked when two figures or more are in one currency and they are not all the same
function lowMarks(vals, curs) {
  const have = vals.flatMap((v, i) => typeof v === "number" ? [i] : []);
  if (have.length < 2 || new Set(have.map(i => curs[i])).size > 1) return vals.map(() => false);
  const ns = have.map(i => vals[i]), min = Math.min(...ns);
  if (Math.max(...ns) - min < 0.005) return vals.map(() => false);
  return vals.map(v => typeof v === "number" && v - min < 0.005);
}
// the vendors' newest rates on one route side by side.
// cols: each vendor's (the Rate requests order, then the rest by name), lowest as on the rows (lowest()), past when
// valid until is before today, missing: the charges another vendor has and this one does not.
// parts, in SEGS order: rows, one per charge and unit in the order they first come (a vendor's second line of the same
// charge and unit is a row of its own); a cell is {n} its amount for this shipment, {w} a word, {none} a line with no
// figure, null not in their rates; low marks the lowest figure (lowMarks), none when the row says INCLUDED. A part
// with two rows or more has its sum (the figures there), the lowest marked among the columns with every row of it
export function compareOf(best, low, { route = null, order = [], today = "" } = {}) {
  const pos = v => { const i = order.indexOf(v); return i < 0 ? order.length : i; };
  const cols = [...(best || new Map())].filter(([, v]) => (v.doc.route ?? null) === (route ?? null))
    .map(([key, v]) => ({ key, vendor: v.doc.vendor, label: txt(v.doc.label), doc: v.doc, lowest: !!low?.has(key), past: !!(v.doc.valid && today && v.doc.valid < today), missing: [] }))
    .sort((a, b) => pos(a.vendor) - pos(b.vendor) || String(a.vendor).localeCompare(String(b.vendor)) || labelKey(a.label).localeCompare(labelKey(b.label)));
  const byKey = new Map();
  cols.forEach((col, ci) => {
    const seen = new Map();
    for (const l of col.doc.lines || []) {
      const seg = SEGS.includes(l.seg) ? l.seg : "Other", charge = up(l.charge), unit = Q.unitText(l.unit);
      const base = `${seg}|${charge}|${unit}`, n = (seen.get(base) || 0) + 1; seen.set(base, n);
      if (!byKey.has(`${base}|${n}`)) byKey.set(`${base}|${n}`, { seg, charge, unit, cells: cols.map(() => null) });
      byKey.get(`${base}|${n}`).cells[ci] = l.word ? { w: l.word } : typeof l.amount === "number" ? { n: l.amount } : { none: true };
    }
  });
  const curs = cols.map(k => k.doc.cur), rows = [...byKey.values()];
  for (const r of rows) r.low = r.cells.some(x => x?.w === "INCLUDED") ? r.cells.map(() => false) : lowMarks(r.cells.map(x => x?.n ?? null), curs);
  const parts = SEGS.map(seg => {
    const rs = rows.filter(r => r.seg === seg);
    if (!rs.length) return null;
    if (rs.length < 2) return { seg, rows: rs, sum: null };
    const cells = cols.map((_, ci) => { const ns = rs.map(r => r.cells[ci]?.n).filter(v => typeof v === "number"); return ns.length ? Q.r2(ns.reduce((a, b) => a + b, 0)) : null; });
    const whole = cols.map((_, ci) => rs.every(r => r.cells[ci] && !r.cells[ci].none));
    return { seg, rows: rs, sum: { cells, low: lowMarks(cells.map((v, ci) => whole[ci] ? v : null), curs) } };
  }).filter(Boolean);
  for (const p of parts) for (const r of p.rows) r.cells.forEach((x, ci) => { if (!x) cols[ci].missing.push(r.charge); });
  return { cols, parts };
}

// ---------------------------------------------------------------- the quote card (Rates from, Cost from)
// a vendor's lines of one part as the quote card's lines: its rate as the cost (a minimum or a lump sum spread over the
// quantity, so the cost comes to what the vendor charges), the markup by the card's rule; a word stays a word
export function quoteLines(doc, seg, { pct = 0 } = {}) {
  return (doc?.lines || []).filter(l => l.seg === seg).map(l => {
    const x = Q.line(up(l.charge), l.qty === null || l.qty === undefined ? "" : l.qty, Q.unitText(l.unit), "");
    if (l.word) { x.rate = l.word; return x; }
    let per = typeof l.rate === "number" ? l.rate : null;
    const q = typeof l.qty === "number" && l.qty > 0 ? l.qty : 1;
    if (typeof l.amount === "number" && (per === null || Math.abs(per * q - l.amount) > 0.005)) per = Q.r2(l.amount / q);
    if (per === null) return x;
    x.cost = String(per); x.markup = Q.markupFor(per, pct);
    return x;
  });
}
const sameLine = (a, b) => up(a.charge) === up(b.charge) && Q.unitText(a.unit) === Q.unitText(b.unit);
const blankQuote = l => !txt(l.charge) && !txt(l.rate) && !txt(l.amount) && !txt(l.cost);
const priced = l => !!(txt(l.cost) || txt(l.rate) || txt(l.amount));
// the card's lines with a vendor's laid in: the same charge and unit is replaced (its remark kept), a new one goes
// before our handling fee; empty lines go. With no line of that charge and unit, the one line of that charge
// with no price yet takes theirs (the card's start line in another unit: "OCEAN FREIGHT 1 CNTR" for a 40' reefer);
// a quantity they did not give keeps the card's.
// The costs follow the vendor picked: with `via` (which part and version laid them in), each
// line laid in keeps, never in the quotation, `via`, `was` (the line before, null for a line it added) and `set` (what
// it was set to), so unlayLines can take them out again
const LAID = ["charge", "qty", "unit", "cost", "rate", "amount"];
const setOf = l => Object.fromEntries(LAID.map(k => [k, txt(l[k])]));
const wasOf = l => ({ charge: l.charge, qty: l.qty, unit: l.unit, cost: l.cost, markup: l.markup, rate: l.rate, amount: l.amount });
export function mergeQuoteLines(lines, add, via = "") {
  const out = (lines || []).filter(l => !blankQuote(l)).map(l => ({ ...l }));
  for (const a of add) {
    let i = out.findIndex(l => sameLine(l, a));
    if (i < 0) {
      const open = out.flatMap((l, k) => up(l.charge) === up(a.charge) && !priced(l) ? [k] : []);
      if (open.length === 1) i = open[0];
    }
    if (i >= 0) {
      const old = out[i], { via: _v, was: _w, set: _s, ...keep } = old;
      const next = { ...keep, qty: txt(a.qty) ? a.qty : old.qty, unit: a.unit, rate: a.rate, cost: a.cost, markup: a.markup, amount: "" };
      out[i] = via ? { ...next, via, was: old.via === via ? old.was : wasOf(old), set: setOf(next) } : next;
      continue;
    }
    const line = via ? { ...a, via, was: null, set: setOf(a) } : a;
    const h = out.findIndex(l => up(l.charge) === "FORWARDING FEE");
    if (h >= 0) out.splice(h, 0, line); else out.push(line);
  }
  return out.length ? out : [Q.line("", "1", "")];
}
// the lines a part laid in (via) taken out: one still as it was set goes back to what it was, or out when it was
// added; one changed by hand (its charge, quantity, unit, cost, rate or amount) is the person's and stays, unmarked
export function unlayLines(lines, via) {
  if (!via) return lines;
  const out = [];
  for (const l of lines || []) {
    if (l.via !== via) { out.push(l); continue; }
    const { via: _v, was, set, ...rest } = l;
    const same = !!set && LAID.every(k => txt(l[k]) === set[k]);
    if (!same) out.push(rest);
    else if (was) out.push({ ...rest, ...was });
  }
  return out.length ? out : [Q.line("", "1", "")];
}
// a vendor picked in Rates from goes into the card at once, unless a line it would replace
// (as mergeQuoteLines finds it) already has a cost, a rate or an amount typed; then Use their rates.
// typedFor: the lines of theirs that would replace a line with a price typed (the rest go in at once)
export const typedFor = (lines, add) => (add || []).filter(a => (lines || []).some(l => sameLine(l, a) && priced(l)));
export const fillsTyped = (lines, add) => typedFor(lines, add).length > 0;
// the vendor's newest rates the quote card offers: its route A's (the case's first route), else its first route's.
// Of that route's options, the one named (label), else the lowest total, else the first
export function ratesForQuote(best, vendor, c, label = null) {
  const mine = [...(best || new Map())].filter(([, v]) => v.doc.vendor === vendor).map(([, v]) => v.doc);
  if (!mine.length) return null;
  const first = defaultRoute(c);
  const route = mine.some(d => (d.route ?? null) === first) ? first : mine.map(d => d.route ?? null).sort((a, b) => (a ?? 0) - (b ?? 0))[0];
  const opts = mine.filter(d => (d.route ?? null) === route).sort((a, b) => labelKey(a.label).localeCompare(labelKey(b.label)));
  if (label !== null && label !== undefined) { const named = opts.find(d => labelKey(d.label) === labelKey(label)); if (named) return named; }
  const priced = opts.filter(d => typeof d.total === "number");
  return priced.length ? priced.reduce((m, d) => d.total < m.total ? d : m) : opts[0];
}
// the options the quote card offers for a vendor: those of the route ratesForQuote takes
export function quoteOptions(best, vendor, c) {
  const d = ratesForQuote(best, vendor, c);
  return d ? optionsOf(best, vendor, d.route ?? null).map(x => x.doc) : [];
}

// ---------------------------------------------------------------- Read quote drafts
// the drafts the Rate requests rows offer: open ones (OppList's status), not saved here already (a vendorRates document
// names it) nor set aside here (a discard action OppList has not taken in yet), the newest per vendor and route.
// {key "vendor|route": draft}
// With options, a draft stays open while one of its options is not taken (saved
// or skipped, by OppList's taken or here: a vendorRates document from it, a discard with or without option); gone are the
// discard actions not taken in yet ({draft, option}, or a draft id alone)
export function openDrafts(drafts, docs = [], gone = [], options = false) {
  const away = (gone || []).map(g => typeof g === "string" ? { draft: g } : g || {});
  const saved = new Set((docs || []).map(d => d?.draft).filter(Boolean)), whole = new Set(away.filter(g => typeof g.option !== "number").map(g => g.draft)), out = new Map();
  for (const d of drafts || []) {
    if (!d || d.status !== "open" || !txt(d.vendor) || !(d.options || []).length) continue;
    if (options ? !takenOf(d, docs, away).some(t => !t) : saved.has(d.id) || whole.has(d.id)) continue;
    const k = keyOf(d.vendor, typeof d.route === "number" ? d.route : null);
    if (!out.has(k) || ms(d.readAt) > ms(out.get(k).readAt)) out.set(k, d);
  }
  return out;
}
// what has become of each option of a draft: "" (not yet), "saved" or "skipped": OppList's taken, a vendorRates
// document saved from it here, a discard of it here (with option, that one; without, every one not taken)
export function takenOf(draft, docs = [], discards = []) {
  const n = (draft?.options || []).length, t = [];
  for (let i = 0; i < n; i++) {
    const o = draft?.taken?.[i];
    t.push(o === "saved" || o === "skipped" ? o : (docs || []).some(v => v?.draft === draft.id && v.option === i) ? "saved" : "");
  }
  for (const a of discards || []) {
    if (!a || a.draft !== draft?.id) continue;
    if (typeof a.option === "number") { if (a.option < n && !t[a.option]) t[a.option] = "skipped"; }
    else for (let i = 0; i < n; i++) if (!t[i]) t[i] = "skipped";
  }
  return t;
}
// the next option not taken after `from` (going round), -1 when none
export function nextUntaken(taken, from = 0) {
  const n = (taken || []).length;
  for (let k = 1; k <= n; k++) { const i = (from + k) % n; if (!taken[i]) return i; }
  return -1;
}
// a vendor's open drafts, route by route
export const draftsOfVendor = (open, vendor) => [...(open?.values() || [])].filter(d => d.vendor === vendor)
  .sort((a, b) => (typeof a.route === "number" ? a.route : 0) - (typeof b.route === "number" ? b.route : 0));
// one option of a draft as the drawer's card: its lines as a saved version's (linesFromDoc), each with the vendor's
// words (quote) and the unit as written when it is not one of the drawer's (asWritten)
export function draftCard(draft, i = 0, autoFn = () => null) {
  const o = (draft?.options || [])[i] || {};
  const src = o.lines || [];
  const lines = linesFromDoc({ lines: src }, autoFn).map((l, n) => ({ ...l, quote: txt(src[n]?.quote), asWritten: txt(src[n]?.asWritten) }));
  const said = Object.fromEntries(SAID.map(([k]) => [k, SAID_VALUES.some(([v]) => v === o.said?.[k]) ? o.said[k] : "notsaid"]));
  return { label: txt(o.label).slice(0, LABEL_MAX), cur: /^[A-Z]{3}$/.test(o.cur || "") ? o.cur : "", lines, valid: iso(o.valid) ? o.valid : "", transit: txt(o.transit).slice(0, SHORT_MAX),
    carrier: txt(o.carrier).slice(0, SHORT_MAX), free: txt(o.free).slice(0, SHORT_MAX), said, note: txt(o.note).slice(0, NOTE_MAX) };
}
// where the reading came from: the subject and when it was sent, nothing more:
// { what: "RE: RFQ", when: "Nov 14, 09:12" }; a paste { what: "Pasted message", when: "Nov 14" }; files sent from the
// site with no mail among them: their names
export function draftFrom(draft) {
  const s = draft?.source || {}, files = (s.files || []).map(txt).filter(Boolean);
  const what = s.kind === "paste" ? "Pasted message" : txt(s.subject) || (s.kind === "files" && files.length ? cut(files.join(", "), 120) : "Their mail");
  const m = /^(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2}))?/.exec(txt(s.sent));
  return { what, when: m ? `${day(m[1])}${m[2] ? `, ${m[2]}` : ""}` : "" };
}

// "Rates from a vendor not asked here" offers at most 15 vendors at a time. With nothing
// typed, the ones nearest this case as Ask vendors orders them (quoted for its countries before, then asked there, then
// the rest; the most quoted, the most asked, the latest asked first); typing narrows them, names that start with it
// first, then a word of the name, then anywhere in it. Vendors this case has asked are never offered.
export const PICK_MAX = 15;
export function pickList(vendors, c, text = "", max = PICK_MAX) {
  const asked = new Set((c?.rr || []).map(x => x.vendor)), far = farCountries(c);
  const t = String(text || "").trim().toLowerCase();
  const group = v => !far.length ? 2 : quotedFor(v, far) ? 0 : worksIn(v, far) ? 1 : 2;
  const fit = v => {
    if (!t) return 0;
    const k = String(v.key).toLowerCase();
    return k.startsWith(t) ? 0 : k.split(/[^a-z0-9]+/).some(w => w && w.startsWith(t)) ? 1 : k.includes(t) ? 2 : -1;
  };
  return (vendors || []).filter(v => v?.key && !asked.has(v.key) && fit(v) >= 0)
    .sort((a, b) => fit(a) - fit(b) || group(a) - group(b) || (b.quoted || 0) - (a.quoted || 0) || (b.asked || 0) - (a.asked || 0)
      || String(b.lastAsked || "").localeCompare(String(a.lastAsked || "")) || String(a.key).localeCompare(String(b.key)))
    .slice(0, max).map(v => v.key);
}

// ---------------------------------------------------------------- Read quote from the site
// What the drawer takes: at most 6 files and 15 MB in all, a mail file (.msg from Outlook, or
// .eml) and what Read quote reads in a mail (PDF, pictures, Excel, Word, text), or a pasted message of at most 20,000
// characters. The files go up in parts of at most 700,000 base64 characters.
export const READ_FILES = 6, READ_BYTES = 15 * 1024 * 1024, READ_TEXT = 20000, READ_PART = 700000;
export const extOf = name => ((/\.([a-z0-9]+)$/i.exec(txt(name)) || [])[1] || "").toLowerCase();
const IMG = ["png", "jpg", "jpeg", "gif", "webp"], XLS = ["xlsx", "xls", "csv"];
// the chip a file gets in the drawer, "" for a file Read quote does not take
export function fileKind(name) {
  const e = extOf(name);
  return e === "msg" || e === "eml" ? "MAIL" : e === "pdf" ? "PDF" : IMG.includes(e) ? "IMG" : XLS.includes(e) ? "XLS" : e === "docx" ? "DOC" : e === "txt" ? "TXT" : "";
}
// the read action's kind: a mail file among them, else files, else a pasted message
export const readKind = files => (files || []).some(f => fileKind(f.name) === "MAIL") ? "mail" : (files || []).length ? "files" : "paste";
// files added to those already there: the ones taken, and why the others were not
export function addFiles(have, add) {
  const out = [...(have || [])], why = [];
  for (const f of add || []) {
    if (!f || out.some(x => x.name === f.name && x.size === f.size)) continue;
    if (!fileKind(f.name)) { why.push(`${f.name}: Read quote takes a mail, PDFs, pictures, Excel, Word or text files`); continue; }
    if (!(f.size > 0)) { why.push(`${f.name} is empty`); continue; }
    if (out.length >= READ_FILES) { why.push(`At most ${READ_FILES} files: ${f.name} was left out`); continue; }
    if (out.reduce((k, x) => k + x.size, 0) + f.size > READ_BYTES) { why.push(`15 MB in all at most: ${f.name} was left out`); continue; }
    out.push(f);
  }
  return { files: out, why };
}
export const partsOf = b64 => Math.max(1, Math.ceil(b64 / READ_PART));
export const sizeText = b => b >= 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`;
// a paste copied in Outlook's message list (a header line with From, Subject and Received among its tab-separated
// columns, and a row or two), not from the mail: OppList refuses it, so the drawer says so first
export function outlookListLine(text) {
  const lines = String(text ?? "").split(/\r\n|\r|\n/).filter(l => l.trim());
  if (!lines.length || lines.length > 3) return false;
  const cols = new Set(lines[0].split("\t").map(c => c.trim().toLowerCase()));
  return ["from", "subject", "received"].every(c => cols.has(c));
}
// a vendor's newest request from the site and where it stands, from OppList's answer (actionResults: state "queued",
// then "done" with ok): {kind: "wait", a} not read yet; {kind: "fail", a, msg} it could not be read, until rates of that
// vendor are saved after it; null when it was read, or nothing was asked in the last week
export function readState(reads, results, vendor, { rates = [], days = 7, now = nowMs() } = {}) {
  const a = (reads || []).filter(x => x.vendor === vendor).sort((x, y) => ms(y.at) - ms(x.at))[0];
  if (!a || now - ms(a.at) > days * 86400000) return null;
  const r = results && results.get ? results.get(a.id) : null;
  if (!r || r.state === "queued") return { kind: "wait", a };
  if (r.ok === false && !(rates || []).some(d => d.vendor === vendor && ms(d.at) > ms(a.at))) return { kind: "fail", a, msg: txt(r.msg) };
  return null;
}
// whether requests are being read now: OppList ran a cycle in the last 12 minutes (meta/sync.readQuote.appAt)
export const readerOn = (rq, now = nowMs()) => !!(rq && rq.appAt) && now - ms(rq.appAt) <= 12 * 60000;
