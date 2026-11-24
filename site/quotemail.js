// The quote to the requester (the quote card): what
// the card computes and writes, as pure functions (no store and no DOM, so they can be tested in Node).
//   - the lines it starts with: the freight from the case's cargo, what the incoterm and the service add, our handling
//     fee, with the company's charge names
//   - Qty x Rate, a typed amount (a minimum), AT COST and the other words; one currency
//   - the cost block (admin and people with Costs): Rate = Cost + Markup, the markup from OppList's Settings
//     (meta/sync.quoteRules: markupPct, marginFloorPct; none for one of our offices), cost, sell, profit, margin
//   - the mail (a reply to the requester's mail: Hi {first name}, the table, the validity, the terms), the clipboard
//     request for Make Outlook drafts (v 2: Reply All by the mail's Message-ID, an Outlook table, the PDF attached),
//     the PDF's contents, and the documents the site saves (quotes/{id} for everyone, quoteCosts/{id} for Costs)
// Never in the mail, the PDF or the clipboard: a cost, a markup, the profit or the margin.
import { endsOf, cleanList, parseAddresses, domainOf, isFreeMail } from "./askmail.js";

export const CURRENCIES = ["USD", "EUR", "MXN", "CNY", "BRL", "CLP", "PEN", "COP"];
// the charge names (one name per thing, so they can be counted)
export const CHARGES = ["OCEAN FREIGHT", "AIR FREIGHT", "TRUCKING", "FUEL SURCHARGE", "EX WORKS CHARGES", "PICKUP", "ORIGIN HANDLING",
  "EXPORT FILING (AES)", "VGM SUBMISSION", "SCREENING FEE", "DOCUMENTATION FEE", "BILL OF LADING FEE", "AIR WAYBILL FEE", "FORWARDING FEE",
  "AMS FILING", "ISF FILING", "ENS FILING", "TERMINAL HANDLING", "CFS CHARGES", "DESTINATION HANDLING", "DELIVERY ORDER FEE", "IMPORT ENTRY",
  "CUSTOMS CLEARANCE", "DUTY, MPF AND HMF", "DUTIES AND TAXES", "DRAYAGE", "CHASSIS", "FINAL DELIVERY", "CARGO INSURANCE", "PALLETIZING", "STORAGE"];
export const UNITS = ["CNTR", "20'", "40'", "40'HQ", "45'HQ", "BL", "HBL", "HAWB", "AWB", "KG", "CBM", "SHIPMENT", "ENTRY", "FILING", "TRIP", "PICK UP", "PALLET", "DAY"];
// what a rate can say instead of a number (a line that does not apply says IF APPLICABLE, it is not deleted)
export const WORDS = ["AT COST", "INCLUDED", "IF APPLICABLE", "N/A"];
// the lines often added, by mode (the card offers the ones not on it yet)
export const SUGGEST = {
  FCL: [["TERMINAL HANDLING", "CNTR"], ["DRAYAGE", "CNTR"], ["CHASSIS", "DAY"], ["DESTINATION HANDLING", "SHIPMENT"], ["CUSTOMS CLEARANCE", "ENTRY"], ["DUTIES AND TAXES", "ENTRY", "AT COST"], ["CARGO INSURANCE", "SHIPMENT"]],
  Air: [["PICKUP", "PICK UP"], ["AIR WAYBILL FEE", "AWB"], ["FUEL SURCHARGE", "KG"], ["DESTINATION HANDLING", "SHIPMENT"], ["CUSTOMS CLEARANCE", "ENTRY"], ["FINAL DELIVERY", "TRIP"], ["CARGO INSURANCE", "SHIPMENT"]],
  LCL: [["CFS CHARGES", "CBM"], ["DOCUMENTATION FEE", "BL"], ["DESTINATION HANDLING", "SHIPMENT"], ["FINAL DELIVERY", "TRIP"], ["CUSTOMS CLEARANCE", "ENTRY"], ["CARGO INSURANCE", "SHIPMENT"]],
};
// the standard terms a quotation can carry (the card ticks the ones that fit the case; startTerms)
export const TERMS = [
  { k: "cur", t: card => `Rates are in ${card?.cur || "USD"}; no other currency applies.` },
  { k: "local", t: () => "Local charges at origin and at destination are extra wherever the shipper does not settle them." },
  { k: "ins", t: () => "Cargo insurance is not part of these rates. Ask us and we quote it separately." },
  { k: "space", t: () => "Space and equipment are confirmed at booking, not by this quotation." },
  { k: "gri", t: () => "A surcharge the carrier announces after this quotation, a general rate increase or a peak season surcharge for example, is passed on as announced." },
  { k: "cargo", t: () => "Quoted for general cargo: not hazardous, stackable, on standard 48 x 40 inch pallets, nothing oversized." },
  { k: "unforeseen", t: () => "Costs nobody can plan are not included, among them customs examinations, demurrage, detention and storage caused by delays at a port, a rail ramp or a trucker." },
  { k: "chassis", t: () => "A door delivery may need extra chassis days or a chassis repositioning; these are charged at cost when they occur." },
  { k: "inland", t: () => "Inland moves depend on trucker capacity. Pre-pull, yard or rail storage, per diem, extra chassis days and waiting time are charged when they occur." },
];
export const MAX_LINES = 40;
export const CLOSING = "Questions are welcome at any time. Moving this cargo for you would be our pleasure.";
// what the PDF says about the company (an invented address)
export const COMPANY = "JoJo Freight Chicago";
export const COMPANY_LINE = "JoJo Freight Chicago · 100 Demo Plaza, Chicago, IL 60000, USA";

// ---------------------------------------------------------------- numbers and words
const CUR_RX = new RegExp(`^(${CURRENCIES.join("|")}|US\\$|\\$|€)\\s*`, "i");
// "2,150", "USD 2,150.50", "$95", "-50" as numbers; anything else null
export function num(s) {
  const t = String(s ?? "").trim().replace(CUR_RX, "").replace(/,/g, "");
  return /^-?(\d+(\.\d+)?|\.\d+)$/.test(t) ? Number(t) : null;
}
export const word = s => { const t = String(s ?? "").trim().toUpperCase(); return WORDS.includes(t) ? t : ""; };
// to the cent, without binary fractions showing (2.675 is 2.68)
export const r2 = n => (n < 0 ? -1 : 1) * Math.round(Math.abs(n) * 100 + 1e-6) / 100;
export const money = n => Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
export const rateText = n => Number(n || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 });
const up = s => String(s ?? "").trim().toUpperCase();
const txt = s => String(s ?? "").trim();
// a unit as the card writes it: "PER KG" typed in is KG (the table says PER itself)
export const unitText = s => up(s).replace(/^PER\s+/, "");

// ---------------------------------------------------------------- dates
const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
export const longDay = iso => { const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso || ""); return m ? `${MON[+m[2] - 1]} ${+m[3]}, ${m[1]}` : ""; };
const at12 = iso => new Date(`${iso}T12:00:00Z`);
const isoOfDate = d => d.toISOString().slice(0, 10);
export const addDays = (iso, n) => { const d = at12(iso); d.setUTCDate(d.getUTCDate() + n); return isoOfDate(d); };
export const monthEnd = iso => { const d = at12(iso); return isoOfDate(new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0, 12))); };

// ---------------------------------------------------------------- the case
// the mode and incoterm as the case writes them (the first route when the case has none)
export const modeOf = c => { const m = (c?.mode && c.mode !== "Not specified" ? c.mode : "") || (c?.routes || [])[0]?.mode || ""; return m === "Not specified" ? "" : m; };
export const incotermOf = c => up(c?.incoterm || (c?.routes || [])[0]?.incoterm || "");
export const scopeOf = c => txt((c?.routes || [])[0]?.scope || "");
const isUS = x => x === "USA";
// one of our offices as the requester: no markup (quoteRules.officesNoMarkup)
export const isOffice = c => c?.rtype === "Office";

const WEIGHT = /(\d[\d,]*(?:\.\d+)?)\s*(kgs?|kilos?|kilograms?|lbs?|pounds?|t|tons?|tonnes?|mt)\b/i;
const VOLUME = /(\d[\d,]*(?:\.\d+)?)\s*(cbm|m3|m³|cubic met(?:er|re)s?)/i;
const N_ = '(\\d+(?:\\.\\d+)?)\\s*(?:"|in\\b|cm\\b)?\\s*';
const DIMS = new RegExp(`${N_}(?:x|×|\\*)\\s*${N_}(?:x|×|\\*)\\s*(\\d+(?:\\.\\d+)?)\\s*(cm|mm|m|in|inch|inches|")?`, "i");
export function kgOf(text) {
  const m = WEIGHT.exec(String(text || "")); if (!m) return null;
  const n = Number(m[1].replace(/,/g, "")), u = m[2].toLowerCase();
  if (/^(lbs?|pounds?)$/.test(u)) return n * 0.45359237;
  if (/^(t|tons?|tonnes?|mt)$/.test(u)) return n * 1000;
  return n;
}
export const cbmOf = text => { const m = VOLUME.exec(String(text || "")); return m ? Number(m[1].replace(/,/g, "")) : null; };
// "120 x 100 x 58 cm (3)", "57*43*41CM" with "20 cartons", 48" x 40" x 47": the volume in cbm, else null
export function dimsCbm(dims, what = "") {
  const m = DIMS.exec(String(dims || "")); if (!m) return null;
  const u = (m[4] || "cm").toLowerCase();
  const f = u === "mm" ? 0.1 : u === "m" ? 100 : ["in", "inch", "inches", '"'].includes(u) ? 2.54 : 1;
  const each = Number(m[1]) * f * Number(m[2]) * f * Number(m[3]) * f / 1e6;
  const count = Number((/\((\d+)\)/.exec(String(dims)) || [])[1] || (/^\s*(\d+)\b/.exec(String(what || "")) || [])[1] || 1);
  return each * count;
}
// the case's weight and volume, summed over its cargo lines; a line with dimensions and no volume counts by those
export function sizes(c) {
  let kg = 0, cbm = 0, okKg = false, okCbm = false, fromDims = false;
  for (const x of c?.cargo || []) {
    const k = kgOf(x.weight);
    if (k !== null) { kg += k; okKg = true; }
    const v = cbmOf(x.cbm);
    if (v !== null) { cbm += v; okCbm = true; }
    else { const d = dimsCbm(x.dims, x.what); if (d !== null) { cbm += d; okCbm = true; fromDims = true; } }
  }
  return { kg, cbm, okKg, okCbm, fromDims };
}
// air: the chargeable weight, the larger of the actual weight and cbm x 167, whole kilos up
export function chargeable(c) {
  const s = sizes(c); if (!s.okKg && !s.okCbm) return null;
  const vol = s.cbm * 167;
  return { cw: Math.ceil(Math.max(s.kg, vol) - 1e-9), vol, ...s };
}
// LCL: W/M, the larger of the cbm and the tonnes (1 cbm = 1,000 kg)
export function wm(c) { const s = sizes(c); if (!s.okCbm && !s.okKg) return null; return { wm: r2(Math.max(s.cbm, s.kg / 1000)), ...s }; }

const BOX = /^\s*(\d+)\s*x\s*(\d{2})\s*'?\s*([A-Z]{2})?\s*$/i;
const UNIT_OF = { "20GP": "20'", "20DV": "20'", "20ST": "20'", "40GP": "40'", "40DV": "40'", "40ST": "40'", "40HQ": "40'HQ", "40HC": "40'HQ", "45HQ": "45'HQ", "45HC": "45'HQ" };
const boxUnit = m => UNIT_OF[(m[2] + (m[3] || "GP")).toUpperCase()] || "CNTR";
export function boxesOf(c) {
  const b = {}; let all = 0;
  for (const x of c?.cargo || []) { const m = BOX.exec(x.what || ""); if (m) { const k = boxUnit(m); b[k] = (b[k] || 0) + Number(m[1]); all += Number(m[1]); } }
  return { b, all };
}
// how many of a unit this case has: containers of that type, all containers, the chargeable kilos, the W/M, else 1
export function qtyFor(c, unit) {
  const u = unitText(unit), { b, all } = boxesOf(c);
  if (b[u] !== undefined) return b[u];
  if (u === "CNTR") return all || 1;
  if (u === "TRIP") return modeOf(c) === "FCL" && all ? all : 1;
  if (u === "KG") { const w = chargeable(c); return w ? w.cw : 1; }
  if (u === "CBM") { const w = wm(c); return w ? w.wm : 1; }
  return 1;
}

// ---------------------------------------------------------------- the lines the card starts with
export const line = (charge = "", qty = "", unit = "", rate = "") =>
  ({ charge, qty: qty === "" || qty === null || qty === undefined ? "" : String(qty), unit, rate, amount: "", remark: "", cost: "", markup: "" });
// the freight of each cargo line, then what the incoterm and the service add, then our handling fee
// (every quotation has one; per BL at sea, per HAWB by air)
export function startLines(c) {
  const out = [], mode = modeOf(c), inc = incotermOf(c), scope = scopeOf(c).toLowerCase(), { all } = boxesOf(c);
  if (mode === "FCL") {
    for (const x of c?.cargo || []) { const m = BOX.exec(x.what || ""); if (m) out.push(line("OCEAN FREIGHT", m[1], boxUnit(m))); }
    if (!out.length) out.push(line("OCEAN FREIGHT", "", "CNTR"));
  }
  if (mode === "Air") { const w = chargeable(c); out.push(line("AIR FREIGHT", w ? w.cw : "", "KG"), line("SCREENING FEE", w ? w.cw : "", "KG")); }
  if (mode === "LCL") { const w = wm(c); out.push(line("OCEAN FREIGHT", w ? w.wm : "", "CBM")); }
  if (!out.length) out.push(line("", "1", ""));
  const door = ["DAP", "DDP", "DDU", "DPU"].includes(inc) || /to door/.test(scope);
  if (inc === "EXW") out.push(line("EX WORKS CHARGES", 1, "SHIPMENT"));
  else if (/^door to/.test(scope)) out.push(line("PICKUP", 1, "PICK UP"));
  if (door) out.push(line("FINAL DELIVERY", mode === "FCL" && all ? all : 1, "TRIP"));
  if (inc === "DDP") out.push(line("IMPORT ENTRY", 1, "ENTRY"), line(isUS(c?.dc) ? "DUTY, MPF AND HMF" : "DUTIES AND TAXES", "", "ENTRY", "AT COST"));
  if (inc === "CIF" || inc === "CIP") out.push(line("CARGO INSURANCE", 1, "SHIPMENT"));
  if (isUS(c?.oc)) out.push(line("EXPORT FILING (AES)", 1, "FILING"));
  out.push(line("FORWARDING FEE", 1, mode === "Air" ? "HAWB" : "BL"));
  return out;
}
// the terms ticked at first: GRI at sea, chassis for an FCL with a delivery, the inland one with a US leg on land,
// insurance unless the quote has it
export function startTerms(c, lines) {
  const mode = modeOf(c), inc = incotermOf(c), has = n => (lines || []).some(l => up(l.charge) === n);
  const door = has("FINAL DELIVERY") || has("DRAYAGE"), us = isUS(c?.oc) || isUS(c?.dc);
  return { cur: true, local: true, ins: !["CIF", "CIP"].includes(inc) && !has("CARGO INSURANCE"), space: true, gri: mode === "FCL" || mode === "LCL",
    cargo: true, unforeseen: true, chassis: door && mode === "FCL", inland: us && (door || has("PICKUP")) };
}
// the markup a line starts with: the rule's percentage of its cost (none for one of our offices)
export const markupFor = (cost, pct) => num(cost) === null ? "" : String(r2(num(cost) * (Number(pct) || 0) / 100));
export function startPct(c, rules) {
  if (isOffice(c) && rules?.officesNoMarkup !== false) return 0;
  const p = Number(rules?.markupPct); return Number.isFinite(p) && p >= 0 ? p : 10;
}

// ---------------------------------------------------------------- what a line comes to
// card.costs: the cost block is on (admin and people with Costs). A line with a cost sells at cost + markup.
export function rateOf(l, costs) {
  if (costs && num(l?.cost) !== null) return { n: r2(num(l.cost) + (num(l.markup) || 0)), auto: true };
  const w = word(l?.rate); if (w) return { w };
  const r = num(l?.rate); return r === null ? null : { n: r };
}
const qtyOf = l => txt(l?.qty) ? num(l.qty) : 1;
// typed by hand (a minimum, say), else a word, else Qty x Rate (no Qty: 1)
export function amountOf(l, costs) {
  const typed = txt(l?.amount);
  if (typed) { const n = num(typed); return n !== null ? { n, hand: true } : { w: typed.toUpperCase(), hand: true }; }
  const r = rateOf(l, costs); if (!r) return null; if (r.w) return { w: r.w };
  const q = qtyOf(l); if (q === null) return null;
  return { n: r2(q * r.n) };
}
export function costOf(l, costs) { if (!costs || num(l?.cost) === null) return 0; const q = qtyOf(l); return q === null ? 0 : r2(q * num(l.cost)); }
export const used = card => (card?.lines || []).filter(l => txt(l.charge) || txt(l.rate) || txt(l.amount) || txt(l.cost));
export function totals(card) {
  let t = 0, cost = 0, atCost = false;
  for (const l of used(card)) {
    const a = amountOf(l, card.costs);
    if (a?.n !== undefined) t += a.n;
    if (a?.w === "AT COST") atCost = true;
    cost += costOf(l, card.costs);
  }
  t = r2(t); cost = r2(cost);
  const profit = r2(t - cost);
  return { t, cost, atCost, profit, margin: t ? profit / t * 100 : 0 };
}

// what stops the draft: [text]
export function problems(card, { needTo = true, today = "" } = {}) {
  const out = [], ls = used(card);
  if (!ls.length) out.push("Add at least one line.");
  if (ls.length > MAX_LINES) out.push(`Up to ${MAX_LINES} lines on one quote.`);
  const noName = ls.filter(l => !txt(l.charge)).length;
  const noRate = ls.filter(l => txt(l.charge) && !amountOf(l, card.costs)).length;
  if (noName) out.push(`${noName === 1 ? "A line has" : `${noName} lines have`} no charge name.`);
  if (noRate) out.push(`${noRate === 1 ? "A line has" : `${noRate} lines have`} no rate. Fill it in, or write AT COST, INCLUDED or IF APPLICABLE.`);
  if (!card.valid) out.push("Pick the Valid until date.");
  else if (today && card.valid < today) out.push("The Valid until date is before today.");
  const to = parseAddresses(card.to), cc = parseAddresses(card.cc), bad = [...to.bad, ...cc.bad];
  if (bad.length) out.push(`${bad.length === 1 ? "This does" : "These do"} not look like an email address: ${bad.join(", ")}.`);
  else if (needTo && !to.good.length) out.push("Write the To address.");
  return out;
}
// what to look at before sending, never a stop (the cost block only)
export function warnings(card, rules) {
  if (!card?.costs) return [];
  const T = totals(card), floor = Number(rules?.marginFloorPct ?? 5), out = [];
  if (!T.t || !T.cost) return out;
  if (T.profit < 0) out.push("The quote is below our cost.");
  // the floor's number only; the site never names where it is set
  else if (T.margin < floor) out.push(`Margin ${T.margin.toFixed(1)}%, under the ${floor}% floor.`);
  return out;
}

// ---------------------------------------------------------------- the mail
const MODE_WORDS = { Air: "air freight", FCL: "FCL", LCL: "LCL" };
// SHANGHAI as Shanghai, NEW YORK, NY as New York, NY; an airport code (PVG) stays
export function placeName(s) {
  const t = txt(s); if (!t || /^[A-Z]{3}$/.test(t)) return t;
  return t.split(/(,\s*)/).map((part, i) => /^,/.test(part) ? part
    : i > 0 && /^[A-Za-z]{2}$/.test(part) ? part.toUpperCase()
    : part.toLowerCase().replace(/(^|[\s\-/(])([a-zà-þ])/g, (m, a, b) => a + b.toUpperCase())).join("");
}
export const cargoPhrase = c => (c?.cargo || []).map(x => [txt(x.what), txt(x.weight)].filter(Boolean).join(", ")).filter(Boolean).join(", ");
export function requestPhrase(c) {
  const [from, to] = endsOf(c), mode = modeOf(c), inc = incotermOf(c), cargo = cargoPhrase(c);
  let p = MODE_WORDS[mode] || (mode ? mode : "your shipment");
  if (from && to) p += ` from ${placeName(from)} to ${placeName(to)}`;
  if (cargo) p += ` (${cargo})`;
  if (inc) p += `, ${inc}`;
  return p;
}
export const greetOf = c => `Hi ${txt(c?.reply?.first) || (txt(c?.req) ? `${txt(c.req)} team` : "there")},`;
export const openingOf = c => `Thank you for the request. Our quotation for ${requestPhrase(c)} follows.`;
// RE: [OPP-1201] their subject, as the acknowledgment writes it (OppList's numbered subject in cases/{OPP}.reply)
export function subjectOf(c, opp) {
  let s = txt(c?.reply?.subject) || txt(c?.subjFull || c?.subj);
  const tag = `[${opp}]`;
  for (let i = 0; i < 4; i++) s = s.replace(/^\s*(re|fw|fwd|rv|aw|tr)\s*:\s*/i, "").replace(new RegExp(`^\\s*\\[${opp}\\]\\s*`), "");
  return `RE: ${tag}${s ? ` ${s}` : ""}`;
}
// the table as the mail, the PDF and Make Outlook drafts show it (sell prices only)
export function table(card) {
  const ls = used(card), cur = card.cur || "USD", anyRemark = ls.some(l => txt(l.remark));
  const head = ["Charge", "Qty", "Unit", `Rate (${cur})`, `Amount (${cur})`, ...(anyRemark ? ["Remarks"] : [])];
  const align = ["l", "r", "l", "r", "r", "l"].slice(0, head.length);
  const rows = ls.map(l => {
    const a = amountOf(l, card.costs), r = rateOf(l, card.costs), u = unitText(l.unit);
    return [up(l.charge), txt(l.qty), u ? `PER ${u}` : "", r ? (r.n !== undefined ? rateText(r.n) : r.w) : txt(l.rate).toUpperCase(),
      a ? (a.n !== undefined ? money(a.n) : a.w) : "", ...(anyRemark ? [txt(l.remark)] : [])];
  });
  const T = totals(card);
  const foot = ["TOTAL", "", "", "", money(T.t), ...(anyRemark ? [T.atCost ? "Plus the AT COST lines" : ""] : [])];
  return { head, align, rows, foot, note: !anyRemark && T.atCost ? "Plus the AT COST lines." : "" };
}
export const termsOf = card => TERMS.filter(x => card?.terms?.[x.k]).map(x => x.t(card));
// what follows the table: the validity, the transit time, the routing, remarks, the terms and the closing line
export function afterText(card) {
  const out = [`Valid until: ${longDay(card.valid) || "[date]"}`];
  if (txt(card.transit)) out.push(`Transit time: ${txt(card.transit)}`);
  if (txt(card.routing)) out.push(`Routing: ${txt(card.routing)}`);
  let t = out.join("\n");
  if (txt(card.other)) t += `\n\nRemarks: ${txt(card.other)}`;
  const tt = termsOf(card);
  if (tt.length) t += `\n\nTerms:\n${tt.map((x, i) => `${i + 1}. ${x}`).join("\n")}`;
  return txt(card.closing) ? `${t}\n\n${txt(card.closing)}` : t;
}
const escH = s => String(s ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
// Copy the quote (no Make Outlook drafts): the mail as Outlook takes it from the clipboard, the table a table
export function outlookHtml(card, c) {
  const t = table(card), F = "font-family:Calibri,sans-serif;font-size:11pt";
  const cell = (v, i, h) => `<td style="${F};border:1px solid #BFBFBF;padding:3pt 6pt;${h ? "background:#F2F2F2;font-weight:bold;" : ""}${t.align[i] === "r" ? "text-align:right;white-space:nowrap;" : ""}">${escH(v)}</td>`;
  const para = x => x.split("\n").map(l => `<p style="margin:0;${F}">${l.trim() ? escH(l) : "&nbsp;"}</p>`).join("");
  return `${para(`${greetOf(c)}\n\n${txt(card.opening)}\n`)}<table style="border-collapse:collapse">${[t.head, ...t.rows].map((r, k) => `<tr>${r.map((v, i) => cell(v, i, k === 0)).join("")}</tr>`).join("")}`
    + `<tr>${t.foot.map((v, i) => cell(v, i, true)).join("")}</tr></table>${para(`\n${(t.note ? `${t.note}\n\n` : "") + afterText(card)}`)}`;
}
export function plainText(card, c) {
  const t = table(card);
  return `${greetOf(c)}\n\n${txt(card.opening)}\n\n${[t.head, ...t.rows, t.foot].map(r => r.join("\t")).join("\n")}\n\n${(t.note ? `${t.note}\n\n` : "") + afterText(card)}`;
}
// the quote action's what: the case's timeline in OppList (OppList adds the [quote:<id>] mark)
export function whatOf(card, n) {
  const names = [...new Set(used(card).map(l => up(l.charge)).filter(Boolean))], T = totals(card);
  return [`Quote ${n} by the quote card: ${names.join(", ")}.`, `${card.cur || "USD"} ${money(T.t)}${T.atCost ? " plus the AT COST lines" : ""}.`,
    `Valid until ${longDay(card.valid)}.`, txt(card.transit) ? `Transit time ${txt(card.transit)}.` : ""].filter(Boolean).join(" ").slice(0, 1000);
}
export const pdfName = (opp, n) => `${opp}_Quotation_${n}.pdf`;

// ---------------------------------------------------------------- whose rates: which vendor each part
// of a quote came from (an airline, a co-loader, an inland carrier). Kept with the quote
// for the case; never in the mail, the PDF or what Make Outlook drafts gets
export const SEGMENTS = ["Freight", "Inland", "Origin", "Destination", "Customs", "Other"];
export const MAX_SOURCES = 6;
export const source = (seg = "Freight") => ({ seg, key: "", name: "", carrier: "" });
// the parts that say something, as saved: {seg, key (a vendor's key on the case, "own", or "" for a name typed),
// name (the vendor, "Our own rate" or the name typed), carrier}
export function sourcesOf(card) {
  const out = [];
  for (const s of card?.sources || []) {
    const seg = SEGMENTS.includes(s?.seg) ? s.seg : "Other";
    const key = txt(s?.key) === "other" ? "" : txt(s?.key).slice(0, 80);
    const name = (key === "own" ? "Our own rate" : key || txt(s?.name)).slice(0, 80), carrier = txt(s?.carrier).slice(0, 40);
    if (!name && !carrier) continue;
    // which of the vendor's options the part used, when it has options
    out.push({ seg, key, name, carrier, ...(key && key !== "own" && typeof s?.label === "string" ? { label: txt(s.label).slice(0, 60) } : {}) });
    if (out.length >= MAX_SOURCES) break;
  }
  return out;
}
// the quote's rateFrom (what Won's "Whose rate was used" starts from): the Freight part's (empty when that is someone
// typed, not a vendor of the case); with no Freight part, the first part that names a vendor or our own rate
export function rateFromOf(list) {
  const all = list || [], fr = all.find(s => s.seg === "Freight");
  if (fr) return fr.key || "";
  const pick = all.find(s => s.key);
  return pick ? pick.key : "";
}
export const sourcesText = list => (list || []).map(s => `${s.seg} ${s.name || "?"}${s.label ? `, ${s.label}` : ""}${s.carrier ? ` (${s.carrier})` : ""}`).join(", ");
// a saved quote's parts back on a card (the next quote starts from the last one)
export const sourcesForCard = list => (list || []).length
  ? list.map(s => ({ seg: s.seg, key: s.key || (s.name ? "other" : ""), name: s.key ? "" : s.name || "", carrier: s.carrier || "", ...(typeof s.label === "string" ? { label: s.label } : {}) }))
  : [source("Freight")];

// ---------------------------------------------------------------- the quote never goes to a vendor
// An address of a vendor on this case's rate requests (its To and CC on lists/vendors; its whole domain, unless that
// is a free mailbox, our own or the requester's own, as when an office of ours asked another) is taken out of To and
// CC, and when one is on their mail there is no Reply All: a Reply All copies everyone on it. OppList does the same;
// this is the site's own guard.
export function guardReply(c, vendorRows = [], teamAddress = "") {
  const r = c?.reply || {}, ours = domainOf(teamAddress);
  const theirs = new Set([].concat(r.to || []).map(a => domainOf(a)).filter(Boolean));
  const keys = new Set((c?.rr || []).filter(x => x.vendor && !x.pending).map(x => x.vendor));
  const addrs = new Set(), doms = new Set();
  for (const v of vendorRows || []) {
    if (!keys.has(v.key)) continue;
    for (const a of [v.to, ...(v.cc || [])].filter(Boolean)) {
      addrs.add(String(a).trim().toLowerCase());
      const d = domainOf(a); if (d && d !== ours && !theirs.has(d) && !isFreeMail(d)) doms.add(d);
    }
  }
  const isVendor = a => { const x = String(a || "").trim().toLowerCase(); return !!x && (addrs.has(x) || doms.has(domainOf(x))); };
  const to = [].concat(r.to || []).filter(Boolean), cc = [].concat(r.cc || []).filter(Boolean);
  const dropped = [...to, ...cc].filter(isVendor);
  return { to: to.filter(a => !isVendor(a)), cc: cc.filter(a => !isVendor(a)), mid: dropped.length ? "" : txt(r.mid), dropped };
}
// the addresses the mail goes to: the card's To and CC, each once, with the team's address when ticked
export function recipients(card, teamAddress = "") {
  const to = cleanList(parseAddresses(card.to).good), cc = cleanList([...parseAddresses(card.cc).good, ...(card.teamCc && teamAddress ? [teamAddress] : [])])
    .filter(a => !to.some(x => x.toLowerCase() === a.toLowerCase()));
  return { to, cc };
}
// Make Outlook drafts, v 2 (JoJo\OutlookHelper): one mail with the table as an Outlook table, the text after it,
// a Reply All under the requester's mail when its Message-ID is known, and the PDF from Downloads attached
export function quoteRequest({ id, opp, card, c, teamAddress = "", n = 1, mid = txt(c?.reply?.mid) }) {
  const t = table(card), { to, cc } = recipients(card, teamAddress);
  mid = txt(mid);
  return { kind: "jojo-drafts", v: 2, id, app: "OppList", ref: opp, folder: "OppList",
    mails: [{ name: txt(c?.reply?.name) || txt(c?.req) || opp, to, cc, subject: txt(card.subject), text: `${greetOf(c)}\n\n${txt(card.opening)}`,
      table: { head: t.head, align: t.align, rows: t.rows, foot: t.foot }, after: (t.note ? `${t.note}\n\n` : "") + afterText(card),
      ...(mid ? { reply: { mid } } : {}),
      ...(card.attach ? { attach: [{ file: pdfName(opp, n), from: "Downloads", then: "Documents\\JoJo quotes" }] } : {}) }] };
}
export const quoteClip = req => `Mail draft for ${req.ref} (1 mail), as a request for the Outlook helper.\n${JSON.stringify(req)}`;

// ---------------------------------------------------------------- the PDF (what quotepdf.js draws)
export function sheetOf(card, c, { opp, n, today, me = {} }) {
  const [from, to] = endsOf(c), mode = modeOf(c), cargo = cargoPhrase(c), { to: addr } = recipients(card);
  const place = (p, country) => [placeName(p), country && country !== "Unknown" ? country : ""].filter(Boolean).join(", ");
  const ship = [["ORIGIN", place(from, c?.oc)], ["DESTINATION", place(to, c?.dc)], ["MODE", [mode, cargo].filter(Boolean).join(", ")],
    ["COMMODITY", txt(c?.commodity)], ["TERMS", incotermOf(c)], ["SERVICE", scopeOf(c)], ["TRANSIT TIME", txt(card.transit)], ["ROUTING", txt(card.routing)]]
    .filter(([, v]) => v);
  // the quotation as the template has it: the number, the line under the title, the parties, the
  // shipment in a grid, the charges with their basis, the total
  const T = totals(card);
  const grid = [["Origin", place(from, c?.oc)], ["Destination", place(to, c?.dc)], ["Service", scopeOf(c)], ["Mode", mode],
    [mode === "FCL" ? "Equipment" : "Cargo", cargo.replace(/(\d)\s*[xX]\s*(?=\S)/g, "$1 × ")], ["Commodity", txt(c?.commodity)],
    ["Incoterm", incotermOf(c)], ["Transit time", txt(card.transit)], ["Routing", txt(card.routing)]].filter(([, v]) => v);
  return { ref: opp, n, date: longDay(today), valid: longDay(card.valid) || "[date]", cur: card.cur || "USD",
    to: [txt(c?.req), txt(c?.reply?.name) ? `Attn: ${txt(c.reply.name)}` : "", addr[0] || ""].filter(Boolean),
    from: [COMPANY, [txt(me.name), txt(me.email)].filter(Boolean).join(" · ")].filter(Boolean),
    ship, table: table(card), remarks: txt(card.other), terms: termsOf(card), company: COMPANY_LINE,
    no: quoteNo(opp, n), title: titleOf(c),
    parties: { for: { name: txt(c?.req), attn: txt(c?.reply?.name), email: addr[0] || "" }, from: { name: txt(me.name), company: COMPANY, email: txt(me.email) } },
    grid, charges: chargeRows(card), total: { amount: money(T.t), atCost: !!T.atCost } };
}
// the quotation's number, JJC-Q- and the case's digits and the quote's number (OPP-1207, quote 1:
// JJC-Q-1207-1); the PDF's file name stays OPP-1207_Quotation_1.pdf
export const quoteNo = (opp, n) => `JJC-Q-${txt(opp).replace(/^OPP-/i, "")}${n ? `-${n}` : ""}`;
// the line under the title: what and how, then where ("Ocean freight, FCL door to door · USA to Honduras")
const MODE_TITLE = { FCL: "Ocean freight, FCL", LCL: "Ocean freight, LCL", Air: "Air freight", Truck: "Trucking", "Cross-border Truck": "Cross-border trucking", Rail: "Rail freight" };
export function titleOf(c) {
  const mode = modeOf(c), scope = scopeOf(c), [from, to] = endsOf(c);
  const what = [MODE_TITLE[mode] || mode, scope.toLowerCase()].filter(Boolean).join(" ");
  const known = x => x && x !== "Unknown" ? x : "";
  let a = known(c?.oc), b = known(c?.dc);
  if (!a || !b || a === b) { a = placeName(from); b = placeName(to); }
  return [what, a && b ? `${a} to ${b}` : ""].filter(Boolean).join(" · ");
}
// a charge as the template writes it: OCEAN FREIGHT as Ocean freight; what reads as a code stays (AES, BL, VGM, D.O.,
// ICS2, 20', DUTY / MPF / HMF); a charge typed in its own case stays as typed
const ACRONYMS = new Set(("ACI AES AFR AMS AWB BAF CAF CBM CFR CFS CIF CIP CPT CY DAP DAT DDP DG DO DPU DTHC EBS EDI EEI ENS EU EXW " +
  "FAS FCA FCL FOB FSC FTL GRI HAWB HBL HMF HQ IMO IPI ISF ISPS LCL LSS LTL MAWB MBL MPF NVOCC OOG OTHC PSS SED SSC THC TSA UK UN " +
  "US USA USD ULD VGM").split(" "));
export function niceCharge(s) {
  const t = txt(s);
  if (!t || /[a-z]/.test(t)) return t;
  const out = t.split(/(\s+)/).map(tok => {
    if (!tok.trim()) return tok;
    const core = tok.replace(/^[(\[]+|[)\],;:]+$/g, "");
    return /[0-9'./&]/.test(core) || /^\(.*\)$/.test(tok) || ACRONYMS.has(core) || (core.length <= 4 && !/[AEIOUY]/.test(core)) ? tok : tok.toLowerCase();
  }).join("");
  return out.charAt(0).toUpperCase() + out.slice(1);
}
// the basis of a line: "1 × per 20'", "2 × per trip", "1 × per BL"; a unit that is a word is written small
const UNIT_WORDS = { CNTR: "container", KG: "kg" };
const basisUnit = u => UNIT_WORDS[u] || (/^[A-Z ]+$/.test(u) && /[AEIOUY]/.test(u) && !ACRONYMS.has(u) ? u.toLowerCase() : u);
export const basisOf = (qty, unit) => { const q = txt(qty), u = unitText(unit); return u ? `${q ? `${q} × ` : ""}per ${basisUnit(u)}` : q ? `${q} ×` : ""; };
// the lines as the template shows them; the same charge on several container sizes says which ("Ocean freight, 20'")
const SIZES = new Set(["20'", "40'", "40'HQ", "45'HQ"]);
export function chargeRows(card) {
  const ls = used(card), seen = new Map();
  ls.forEach(l => seen.set(up(l.charge), (seen.get(up(l.charge)) || 0) + 1));
  return ls.map(l => {
    const a = amountOf(l, card.costs), r = rateOf(l, card.costs), u = unitText(l.unit);
    return { desc: niceCharge(l.charge) + (seen.get(up(l.charge)) > 1 && SIZES.has(u) ? `, ${u}` : ""), basis: basisOf(l.qty, l.unit),
      rate: r ? (r.n !== undefined ? rateText(r.n) : r.w) : txt(l.rate).toUpperCase(), amount: a ? (a.n !== undefined ? money(a.n) : a.w) : "",
      remark: txt(l.remark) };
  });
}

// ---------------------------------------------------------------- what the site keeps
// quotes/{id}, for everyone: the lines as quoted (sell side), the total, the validity and the rest of the quote
export function quoteDoc(card, { opp, n, date, how, pdf, rateFrom = "", sources = [] }) {
  const T = totals(card);
  return { opp, n, date, cur: card.cur || "USD", how, pdf: pdf || "", subject: txt(card.subject).slice(0, 300), rateFrom, sources,
    lines: used(card).map(l => {
      const r = rateOf(l, card.costs), a = amountOf(l, card.costs);
      return { charge: up(l.charge).slice(0, 80), qty: txt(l.qty).slice(0, 20), unit: unitText(l.unit).slice(0, 20), rate: r?.n ?? null,
        word: (r?.w || (a?.hand && a.w) || "").slice(0, 40), amount: a?.n ?? null, hand: !!a?.hand, remark: txt(l.remark).slice(0, 200) };
    }),
    total: T.t, atCost: T.atCost, valid: card.valid, transit: txt(card.transit).slice(0, 100), routing: txt(card.routing).slice(0, 200),
    remarks: txt(card.other).slice(0, 1000), terms: termsOf(card) };
}
// quoteCosts/{same id}, for admin and Costs only: each line's cost and markup, the totals, the margin
export function costDoc(card, { opp }) {
  const T = totals(card);
  return { opp, cur: card.cur || "USD", pct: Number(card.pct) || 0,
    lines: used(card).map(l => num(l.cost) === null ? { cost: null, markup: null } : { cost: num(l.cost), markup: num(l.markup) ?? 0 }),
    cost: T.cost, profit: T.profit, margin: r2(T.margin) };
}
// a card that starts from a saved quote (the next quote on the case starts from the last one); its costs too for
// people who can read them
export function linesFromQuote(q, qc) {
  return (q?.lines || []).map((x, i) => {
    const k = qc?.lines?.[i], l = line(x.charge, x.qty, x.unit, "");
    if (k && k.cost !== null && k.cost !== undefined) { l.cost = String(k.cost); l.markup = String(k.markup ?? 0); }
    else l.rate = x.word && !x.hand ? x.word : x.rate !== null && x.rate !== undefined ? String(x.rate) : "";
    if (x.hand) l.amount = x.amount !== null && x.amount !== undefined ? String(x.amount) : x.word || "";
    l.remark = x.remark || "";
    return l;
  });
}
