// Edit Cargo and Route as plain
// functions, no store and no DOM, so they can be tested in Node: the fields of cases/{OPP}.ed, the checks OppList
// makes before it takes a change in (site_edit.check_value: the person hears it here, not later as a refusal), what a
// person changed and what they saw, the edits not taken in yet laid over ed, the place picker's searches (OppList's
// places, then the official lists of the country chosen), and the words the case page shows.

export const CARGO_FIELDS = ["pieces", "pack", "cq", "ct", "l", "w", "h", "du", "wt", "wu", "wb", "cbm", "cb", "com", "hs", "val", "cur"];
export const ROUTE_FIELDS = ["oc", "o", "dc", "d", "pod", "mode", "inc", "scope"];
export const PLACE_FIELDS = ["o", "d", "pod"];
// the words OppList uses for each field (site_edit.LABELS), on the Timeline and in its results
export const LABELS = { pieces: "Pieces", pack: "Packing", l: "Length", w: "Width", h: "Height", du: "Size unit",
  wt: "Weight", wu: "Weight unit", wb: "Weight basis", cbm: "Volume", cb: "Volume basis", ct: "Container type", cq: "Containers",
  com: "Commodity", hs: "HS code", val: "Cargo value", cur: "Currency", oc: "Origin country", dc: "Destination country",
  o: "Origin", d: "Destination", pod: "Port of discharge", mode: "Mode", inc: "Incoterm", scope: "Service" };
export const DIMS_UNITS = ["cm", "in", "mm", "m"];
export const WEIGHT_UNITS = ["kg", "lbs", "t"];
export const WEIGHT_BASES = [["total", "in total"], ["each", "each piece"], ["container", "per container"]];
export const VOLUME_BASES = [["total", "in total"], ["each", "each piece"]];
// what OppList reads when a unit or basis is not stored (its Cargo card and its push): the form shows these, and an
// empty value and the default are the same, so opening a form never changes them
export const DEFAULTS = { wu: "kg", wb: "total", cb: "total" };
export const TEXT_MAX = 200, MAX_PLACES = 4;
export const HS_HINT = "4 to 10 digits each, dots or spaces as written (2507.00.10); several with commas";

const squeeze = s => String(s ?? "").split(/\s+/).filter(Boolean).join(" ");
export const textOf = v => squeeze(v) || null;
const blank = v => v === null || v === undefined || v === "" || (Array.isArray(v) && !v.length);

// a number as people type it: 7200, 7,200, 3.25, 12,5 (a comma between groups of three is thousands, otherwise the
// decimal mark; with both marks the last one is the decimal); null when empty, NaN when it is not a number
export function numOf(text) {
  let t = String(text ?? "").replace(/\s/g, "");
  if (!t) return null;
  if (/^[.,]\d/.test(t)) t = "0" + t;
  if (!/^\d[\d,.]*$/.test(t)) return NaN;
  if (t.includes(",") && t.includes(".")) {
    const dec = t.lastIndexOf(",") > t.lastIndexOf(".") ? "," : ".";
    t = t.split(dec === "," ? "." : ",").join("").replace(",", ".");
  } else if (t.includes(",")) {
    t = /^\d{1,3}(,\d{3})+$/.test(t) ? t.replace(/,/g, "") : t.replace(",", ".");
  }
  return /^\d+(\.\d+)?$/.test(t) ? Number(t) : NaN;
}
// 7200, 3.25, 0.333 (three decimals at most, no trailing zeros)
export const fmtNum = v => String(Math.round(Number(v) * 1000) / 1000);
export const numIn = v => v === null || v === undefined || v === "" ? "" : fmtNum(v);

// OppList's cargo.hs_code_of: the codes typed, ", " between them, each 4 to 10 digits with dots or spaces as written
export function hsOf(text) {
  const parts = String(text ?? "").split(/[,;/]/).map(squeeze).filter(Boolean);
  if (!parts.length) return { codes: null, why: "" };
  for (const p of parts) {
    const n = p.replace(/\D/g, "").length;
    if (!/^\d[\d. ]*\d$/.test(p) || n < 4 || n > 10) return { codes: null, why: HS_HINT };
  }
  return { codes: parts.join(", "), why: "" };
}

// ------------------------------------------------------------------ reading the forms
// the cargo form's boxes (text as typed, the lists' values) in ed's format, with what is wrong with each
export function readCargo(raw) {
  const vals = {}, errs = {};
  const num = (k, { whole = false, low = 0 } = {}) => {
    const v = numOf(raw[k]);
    if (v === null) vals[k] = null;
    else if (Number.isNaN(v)) errs[k] = "Type a number";
    else if (whole && !Number.isInteger(v)) errs[k] = "A whole number";
    else if (v < low) errs[k] = `At least ${low}`;
    else vals[k] = v;
  };
  num("pieces", { whole: true }); num("cq", { whole: true, low: 1 });
  for (const k of ["l", "w", "h", "wt", "cbm", "val"]) num(k);
  for (const k of ["pack", "ct", "du", "wu", "wb", "cb"]) vals[k] = textOf(raw[k]);
  const com = textOf(raw.com);
  if (com && com.length > TEXT_MAX) errs.com = `At most ${TEXT_MAX} characters`; else vals.com = com;
  const hs = hsOf(raw.hs);
  if (hs.why) errs.hs = hs.why; else vals.hs = hs.codes;
  const cur = squeeze(raw.cur);
  if (cur && !/^[A-Za-z]{3}$/.test(cur)) errs.cur = "Three letters (USD)";
  else vals.cur = cur ? cur.toUpperCase() : vals.val !== null && vals.val !== undefined ? "USD" : null;   // OppList's default
  return { vals, errs };
}
// the route form: the lists' values, and the places picked
export function readRoute(raw, picks = {}) {
  const vals = {}, errs = {};
  for (const k of ["oc", "dc", "mode", "inc", "scope"]) vals[k] = textOf(raw[k]);
  vals.o = picks.o || null;
  vals.d = (picks.d || []).filter(Boolean).slice(0, MAX_PLACES);
  vals.pod = (picks.pod || []).filter(Boolean).slice(0, MAX_PLACES);
  for (const k of PLACE_FIELDS) for (const p of [].concat(vals[k] || [])) {
    if (String(p.k || p.t || "").length > TEXT_MAX) errs[k] = `At most ${TEXT_MAX} characters`;
  }
  return { vals, errs };
}
// what goes together: a size needs its unit, containers their type and number; a new line needs something OppList
// counts (cargo.is_blank: pieces, length, weight, volume, containers or commodity). Checked only where something
// changed, so an old line's gaps never stop a change elsewhere.
export function checkCargo(v, seen = null) {
  const errs = {}, adding = !seen;
  const moved = (...f) => adding || f.some(k => !sameField(k, seen[k], v[k]));
  if (moved("l", "w", "h", "du") && [v.l, v.w, v.h].some(x => !blank(x)) && !v.du) errs.du = "Pick the unit";
  if (moved("cq", "ct")) {
    if (!blank(v.cq) && !v.ct) errs.ct = "Pick the type";
    if (v.ct && blank(v.cq)) errs.cq = "How many?";
  }
  if (adding && [v.pieces, v.l, v.wt, v.cbm, v.cq, v.com].every(blank)) errs._ = "Fill in at least the pieces, the length, the weight, the volume, the containers or the commodity.";
  return errs;
}
export function checkRoute(v, seen = null) {
  return !seen && !v.dc && !(v.d || []).length ? { _: "Pick the destination country or a destination." } : {};
}

// ------------------------------------------------------------------ what changed
const fold = s => squeeze(s).toLowerCase();
export const offCode = o => o ? (o.sys === "IATA" ? o.code : `${o.cc}${o.code}`) : "";
// a place as OppList compares it (site_edit._same: by key or by text); a pick from the official lists is its code, so
// it is a change even when the words typed to find it are the old text's
const placeRef = p => !p ? "" : p.off ? `off:${p.off.sys}:${offCode(p.off)}:${p.off.kind}` : p.k ? "k:" + fold(p.k) : "t:" + fold(p.t);
export function same(a, b) {
  if (Array.isArray(a) || Array.isArray(b)) { a = a || []; b = b || []; return a.length === b.length && a.every((x, i) => same(x, b[i])); }
  if ((a && typeof a === "object") || (b && typeof b === "object")) return placeRef(a) === placeRef(b);
  if (blank(a) || blank(b)) return blank(a) && blank(b);
  if (typeof a === "number" && typeof b === "number") return Math.abs(a - b) < 1e-9;
  return fold(a) === fold(b);
}
const withDefault = (f, v) => blank(v) && f in DEFAULTS ? DEFAULTS[f] : v;
export const sameField = (f, a, b) => same(withDefault(f, a), withDefault(f, b));
// change: from (what the person saw) and to (the new values) for the fields that differ
export function changes(seen, vals, fields) {
  const from = {}, to = {};
  for (const f of fields) {
    if (!(f in vals)) continue;
    const was = seen?.[f] ?? (f === "d" || f === "pod" ? [] : null);
    if (sameField(f, was, vals[f])) continue;
    from[f] = was; to[f] = vals[f];
  }
  return { from, to, n: Object.keys(to).length };
}
// add: what was filled in, without a unit or a basis whose number is empty
export function addValues(vals, part) {
  const to = {};
  for (const [f, v] of Object.entries(vals)) if (!blank(v)) to[f] = v;
  if (part === "cargo") {
    if (blank(to.wt)) { delete to.wu; delete to.wb; }
    if (blank(to.cbm)) delete to.cb;
    if ([to.l, to.w, to.h].every(blank)) delete to.du;
    if (blank(to.val)) delete to.cur;
  }
  return to;
}

// ------------------------------------------------------------------ the edits not taken in yet, laid over ed
// acts: the case's actions after OppList's last pull, oldest first; an edit's line or route is its `target` (the
// action's own field `id`; the site keeps the document id as `id`)
export const letter = n => { let s = "", k = Number(n) || 0; while (k > 0) { const r = (k - 1) % 26; s = String.fromCharCode(65 + r) + s; k = Math.floor((k - 1) / 26); } return s; };
const seqOf = l => [...String(l || "")].reduce((n, ch) => n * 26 + ch.charCodeAt(0) - 64, 0);
export function mergeEd(ed, acts) {
  if (!ed) return null;
  const copy = list => (list || []).map(x => ({ ...x, pend: [] }));
  const out = { cargo: copy(ed.cargo), routes: copy(ed.routes) };
  for (const a of acts || []) {
    if (a.type !== "edit") continue;
    const list = a.part === "cargo" ? out.cargo : a.part === "route" ? out.routes : null;
    if (!list) continue;
    if (a.op === "change" && a.to) {
      const x = list.find(r => r.id !== null && r.id === a.target);
      if (x) { Object.assign(x, a.to); x.pend = [...new Set([...x.pend, ...Object.keys(a.to)])]; x.by = a.name || ""; }
    } else if (a.op === "add" && a.to) {
      list.push({ ...a.to, id: null, n: "", added: true, pend: Object.keys(a.to), by: a.name || "", aid: a.id || "" });
    } else if (a.op === "strike") {
      const i = list.findIndex(r => r.id !== null && r.id === a.target);
      if (i >= 0 && list.filter(r => !r.added).length > 1) list.splice(i, 1);
    }
  }
  // the lines and routes being added take the next numbers and letters
  let n = Math.max(0, ...out.cargo.filter(x => !x.added).map(x => Number(x.n) || 0));
  for (const x of out.cargo) if (x.added) x.n = ++n;
  let s = Math.max(0, ...out.routes.filter(x => !x.added).map(x => seqOf(x.n)));
  for (const x of out.routes) if (x.added) x.n = letter(++s);
  return out;
}
export const canStrike = list => (list || []).filter(x => !x.added).length > 1;

// ------------------------------------------------------------------ the words the case page shows
export const placeName = p => !p ? "" : p.k || (p.off ? `${p.off.name || p.off.code} (${offCode(p.off)})` : "") || p.t || "";
export function show(v) {
  if (blank(v)) return "-";
  if (Array.isArray(v)) return v.map(show).join(" / ");
  if (typeof v === "object") return placeName(v) || "-";
  if (typeof v === "number") return fmtNum(v);
  return String(v);
}
// packing.label: '1 pallet', '25 big bags' (the unit in lower case, its singular for one piece)
export function singular(unit) {
  const t = squeeze(unit).toLowerCase();
  for (const tail of ["xes", "ches", "shes", "sses", "zzes"]) if (t.endsWith(tail)) return t.slice(0, -2);
  if (t.endsWith("ies") && t.length > 4) return t.slice(0, -3) + "y";
  if (t.endsWith("s") && !t.endsWith("ss")) return t.slice(0, -1);
  return t;
}
export function whatOf(x) {
  if (!blank(x.cq)) return `${x.cq} x ${x.ct || "?"}`;
  if (x.pack) return blank(x.pieces) ? squeeze(x.pack).toLowerCase() : `${fmtNum(x.pieces)} ${Number(x.pieces) === 1 ? singular(x.pack) : squeeze(x.pack).toLowerCase()}`;
  if (!blank(x.pieces)) return `${fmtNum(x.pieces)} ${Number(x.pieces) === 1 ? "piece" : "pieces"}`;
  return "";
}
// cargo.value_text: 'USD 12,500' (cents only when there are any)
export function valueText(val, cur) {
  if (blank(val)) return "";
  const s = Number(val).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${cur || "USD"} ${s.endsWith(".00") ? s.slice(0, -3) : s}`;
}
// one Cargo row: x the line of ed (with the edits waiting laid over), b OppList's own words for it (cases/{OPP}.cargo[]).
// OppList's words stay where nothing is waiting; the size comes from ed whenever its three numbers are there (the
// pushed dims are the text as first written)
export function cargoCells(x, b) {
  const pend = new Set(x.pend || []), any = (...f) => !b || f.some(k => pend.has(k));
  const own = {
    weight: blank(x.wt) ? "" : `${fmtNum(x.wt)} ${x.wu || "kg"}${x.wb === "each" ? " each" : x.wb === "container" ? " per container" : ""}`,
    volume: blank(x.cbm) ? "" : `${fmtNum(x.cbm)} cbm${x.cb === "each" ? " each" : ""}`,
  };
  const what = any("pieces", "pack", "ct", "cq") ? whatOf(x) : b.what || whatOf(x);
  const weight = any("wt", "wu", "wb") ? own.weight : b.weight || own.weight;
  // with no volume stated, OppList's figure is the one it worked out from the size (gone once the size is changing)
  const volume = blank(x.cbm) ? (any("cbm", "cb", "l", "w", "h", "du", "pieces") ? "" : b.cbm || "")
    : any("cbm", "cb") ? own.volume : b.cbm || own.volume;
  const dims = [x.l, x.w, x.h].every(v => !blank(v)) ? `${fmtNum(x.l)} x ${fmtNum(x.w)} x ${fmtNum(x.h)}${x.du ? " " + x.du : ""}`
    : any("l", "w", "h", "du") ? "" : b.dims || "";
  const goods = [x.hs ? `HS ${x.hs}` : "", valueText(x.val, x.cur)].filter(Boolean).join(" · ");
  return { n: x.n, what, com: x.com || "", goods, weight, volume, dims };
}
// one Route row from ed: the origin place (else its country), the destinations, the ports of discharge
export function routeCells(x, b) {
  return { n: x.n, from: placeName(x.o) || x.oc || "", to: (x.d || []).map(placeName).filter(Boolean).join(" / ") || x.dc || "",
    pod: (x.pod || []).map(placeName).filter(Boolean).join(" / "), mode: x.mode || "", inc: x.inc || "", scope: x.scope || "", result: b?.result || "" };
}
// a pending edit on the Timeline, as OppList words it once taken in: "Changed cargo line 1" and "Pieces 10 → 12;
// Weight 7000 kg → 7200 kg" (the fields in the form's order; a number with its unit, the line's when it is not changed)
const UNIT_OF = { wt: "wu", l: "du", w: "du", h: "du", val: "cur" };
const UNITS = new Set(["wu", "du", "cur"]);
export function editText(a, ed) {
  const cargo = a.part !== "route";
  const x = ((cargo ? ed?.cargo : ed?.routes) || []).find(r => r.id === a.target);
  const thing = cargo ? (x ? `cargo line ${x.n}` : "a cargo line") : (x ? `route ${x.n}` : "a route");
  const text = a.op === "add" ? `Added ${cargo ? "a cargo line" : "a route"}` : a.op === "strike" ? `Struck out ${thing}` : `Changed ${thing}`;
  if (a.op === "strike") return { text, sub: "" };
  const to = a.to || {}, from = a.from || {};
  const shown = (f, v, side) => {
    if (blank(v)) return "-";
    const u = UNIT_OF[f], unit = !u ? "" : (side[u] ?? (u in to ? to[u] : x?.[u])) || (f === "wt" ? "kg" : f === "val" ? "USD" : "");
    if (f === "cbm") return `${fmtNum(v)} cbm`;
    if (f === "val") return valueText(v, unit);
    return unit ? `${show(v)} ${unit}` : show(v);
  };
  // a unit sent with its number is said with the number, not on its own
  const fields = (cargo ? CARGO_FIELDS : ROUTE_FIELDS).filter(f => f in to && !(UNITS.has(f) && Object.keys(UNIT_OF).some(n => UNIT_OF[n] === f && n in to)));
  const sub = fields.map(f => a.op === "add" ? `${LABELS[f] || f} ${shown(f, to[f], to)}` : `${LABELS[f] || f} ${shown(f, from[f], from)} → ${shown(f, to[f], to)}`).join("; ");
  return { text, sub };
}

// ------------------------------------------------------------------ the place picker
const folded = s => String(s ?? "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
// how well a folded text matches what was typed: 0 all of it, 1 its start, 2 a word's start, 3 inside it, -1 not at all
function rank(t, q) {
  if (!t || !q) return -1;
  if (t === q) return 0;
  if (t.startsWith(q)) return 1;
  if ((" " + t).includes(" " + q)) return 2;
  return q.length >= 3 && t.includes(q) ? 3 : -1;
}
const best = (texts, q) => texts.reduce((m, t) => { const r = rank(t, q); return r >= 0 && (m < 0 || r < m) ? r : m; }, -1);
// lists/places rows [key, name, country, type, code, aliases], folded once
export const prepPlaces = rows => (rows || []).map(r => ({ r, f: [r[0], r[1], ...String(r[5] || "").split(/\s*,\s*/)].map(folded).filter(Boolean),
  code: String(r[4] || "").toUpperCase() }));
// OppList's places that match what was typed, in the country chosen (all of them when none is)
export function searchPlaces(prepped, typed, country = "", max = 8) {
  const q = folded(typed), up = squeeze(typed).toUpperCase().replace(/\s/g, "");
  if (!q) return [];
  const hits = [];
  for (const p of prepped || []) {
    if (country && p.r[2] !== country) continue;
    const s = p.code && p.code === up ? 0 : best(p.f, q);
    if (s >= 0) hits.push({ p, s });
  }
  return hits.sort((a, b) => a.s - b.s || String(a.p.r[0]).localeCompare(String(b.p.r[0]))).slice(0, max).map(h => h.p.r);
}
// the kinds a UN/LOCODE entry can be, from its functions (p port, a airport): always a city
export const kindsOf = flags => ["city", ...(String(flags || "").includes("p") ? ["seaport"] : []), ...(String(flags || "").includes("a") ? ["airport"] : [])];
const SEA = new Set(["FCL", "LCL", "RORO", "Break Bulk", "Sea-Air"]), AIR = new Set(["Air"]), LAND = new Set(["Truck", "Cross-border Truck"]);
// what an entry most likely is on this route: a port for an ocean mode, an airport for Air, a city by truck; with no
// mode, what its functions say when they say one thing
export function kindOf(flags, mode = "") {
  const f = String(flags || ""), p = f.includes("p"), a = f.includes("a");
  if (SEA.has(mode)) return p ? "seaport" : a && mode === "Sea-Air" ? "airport" : "city";
  if (AIR.has(mode)) return a && !/[prt]/.test(f) ? "airport" : "city";   // a city code with an airport among its functions (USCHI) is the city
  if (LAND.has(mode)) return "city";
  return p && !a ? "seaport" : a && !/[prt]/.test(f) ? "airport" : "city";
}
// official/{CC}.json, folded once: loc [code, name, sub, flags, lat, lon], air [iata, name, municipality, lat, lon]
export function prepOfficial(file) {
  if (!file || file.lf) return file;
  return { ...file, lf: (file.loc || []).map(r => folded(r[1])), af: (file.air || []).map(r => [folded(r[1]), folded(r[2])]) };
}
export function searchOfficial(file, typed, { mode = "", max = 8 } = {}) {
  const f = prepOfficial(file), q = folded(typed), up = squeeze(typed).toUpperCase().replace(/\s/g, "");
  if (!f || !q) return [];
  const cc = f.c, hits = [], iatas = new Set((f.air || []).map(r => r[0]));
  (f.air || []).forEach((r, i) => {
    const s = r[0] === up ? 0 : best(f.af[i], q);
    if (s >= 0) hits.push({ s, sys: "IATA", cc, code: r[0], name: r[1], sub: r[2] || "", flags: "a", kind: "airport", kinds: ["airport"], lat: r[3] ?? null, lon: r[4] ?? null });
  });
  (f.loc || []).forEach((r, i) => {
    const flags = r[3] || "";
    if (iatas.has(r[0]) && flags.includes("a") && !flags.includes("p")) return;    // its IATA entry stands for it
    const s = r[0] === up || cc + r[0] === up ? 0 : rank(f.lf[i], q);
    if (s >= 0) hits.push({ s, sys: "UN/LOCODE", cc, code: r[0], name: r[1], sub: r[2] || "", flags, kind: kindOf(flags, mode), kinds: kindsOf(flags), lat: r[4] ?? null, lon: r[5] ?? null });
  });
  const pref = k => AIR.has(mode) ? (k === "airport" ? 0 : 1) : SEA.has(mode) ? (k === "seaport" ? 0 : 1) : 0;
  return hits.sort((a, b) => a.s - b.s || pref(a.kind) - pref(b.kind) || a.name.localeCompare(b.name)).slice(0, max);
}
// what the picker lists: OppList's places first; then the official entries OppList does not have yet (one it has by
// the same code is its OppList place instead)
export function pickList({ prepped, file, typed, country = "", mode = "", max = 8 }) {
  const places = searchPlaces(prepped, typed, country, max);
  const byCode = new Map();
  for (const p of prepped || []) if (p.code) byCode.set(p.code, p.r);
  const official = [];
  for (const e of searchOfficial(file, typed, { mode, max: max * 3 })) {
    const r = byCode.get(offCode(e)) || (e.sys === "IATA" ? byCode.get(e.cc + e.code) : null);
    if (r) { if (!places.includes(r) && (!country || r[2] === country) && places.length < max + 2) places.push(r); continue; }
    if (official.length < max) official.push(e);
  }
  return { places, official };
}
// the value an official entry is sent as: what was typed to find it, and the entry
export function offPick(e, typed, kind = e.kind) {
  return { t: squeeze(typed) || e.name, off: { sys: e.sys, cc: e.cc, code: e.code, name: e.name, sub: e.sub || null, kind,
    lat: typeof e.lat === "number" ? e.lat : null, lon: typeof e.lon === "number" ? e.lon : null } };
}
