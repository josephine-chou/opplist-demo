// The rate request mail: what the card writes from a case, as pure functions (no store and
// no DOM, so they can be tested in Node). One mail per vendor, the case number first in the subject, a greeting in the
// vendor's language, no signature (Outlook adds the sender's own).

// Spanish-speaking countries greet with Hola, Brazil and Portugal with Olá, everyone else with Hi
const HOLA = new Set(["Argentina", "Bolivia", "Chile", "Colombia", "Costa Rica", "Cuba", "Dominican Republic", "Ecuador", "El Salvador",
  "Guatemala", "Honduras", "Mexico", "Nicaragua", "Panama", "Paraguay", "Peru", "Puerto Rico", "Spain", "Uruguay", "Venezuela"]);
const OLA = new Set(["Brazil", "Portugal"]);
export const greetingWord = country => OLA.has(country) ? "Olá" : HOLA.has(country) ? "Hola" : "Hi";
export const greeting = (name, country) => `${greetingWord(country)} ${name ? `${name} team` : "team"},`;

// the countries a vendor for this case works in: the ends of the case outside the US (the origin of a US import, the
// destination of a US export, both ends of a cross-trade such as China to Mexico). OppList's lists/vendors counts
// askedIn and quotedIn the same way.
export function farCountries(c) {
  const out = [];
  for (const x of [c?.oc, c?.dc]) if (x && x !== "USA" && x !== "Unknown" && !out.includes(x)) out.push(x);
  return out;
}
const modeOf = c => (c?.mode && c.mode !== "Not specified" ? c.mode : "") || (c?.routes || [])[0]?.mode || "";
const termsOf = c => c?.incoterm || (c?.routes || [])[0]?.incoterm || "";
// origin and destination as the case writes them: the first route, else the lane
export function endsOf(c) {
  const r = (c?.routes || [])[0];
  if (r && (r.from || r.to)) return [r.from || "", r.to || ""];
  const [a = "", b = ""] = String(c?.lane || "").split(" - ");
  return [a.trim(), b.trim()];
}
const BOX = /^\s*(\d+)\s*x\s*(\d{2})\s*'?\s*([A-Z]{2})\s*$/i;
// FCL: the containers as the subject writes them ("1x20GP + 1x40GP")
const boxes = c => (c?.cargo || []).map(x => BOX.exec(x.what || "")).filter(Boolean).map(m => `${m[1]}x${m[2]}${m[3].toUpperCase()}`);

// [OPP-1201] RATE REQUEST // XIAMEN - MANZANILLO // FCL 1x20GP + 1x40GP // DDP
export function subjectOf(opp, c) {
  const [from, to] = endsOf(c), mode = modeOf(c);
  const cargo = mode === "FCL" ? boxes(c).join(" + ") : "";
  return [`[${opp}] RATE REQUEST`, [from, to].filter(Boolean).join(" - "), [mode, cargo].filter(Boolean).join(" "), termsOf(c)]
    .filter(Boolean).join(" // ");
}

// one line per cargo line: "1 x 40HQ", or "3 pallets / 450 kg / 2.1 cbm", with its dimensions under it
export function cargoLines(c) {
  const out = [];
  for (const x of c?.cargo || []) {
    const line = [x.what, x.weight, x.cbm].filter(Boolean).join(" / ");
    if (line) out.push(line);
    if (x.dims) out.push(`Dimension: ${x.dims}`);
  }
  return out;
}

// the cargo lines' HS codes, each once (OppList pushes them; the edit forms' own values when the case
// has them, so a code just typed on the site is in the next mail); never the cargo value, which a vendor does not need
export function hsCodes(c) {
  const out = [];
  for (const x of c?.edm?.cargo || c?.cargo || []) for (const h of String(x.hs || "").split(/\s*,\s*/)) if (h && !out.includes(h)) out.push(h);
  return out;
}

// the message without its greeting (each mail starts with its own); a line the case has no value for is left out
export function bodyOf(c) {
  const [from, to] = endsOf(c), mode = modeOf(c), terms = termsOf(c), lines = cargoLines(c), hs = hsCodes(c);
  const out = ["A new request has come in; the details follow. May we have your rates for it:", ""];
  if (from) out.push(`Origin      : ${from}`);
  if (to) out.push(`Destination : ${to}`);
  if (mode) out.push(`Mode        : ${mode}`);
  if (terms) out.push(`Terms       : ${terms}`);
  if (lines.length || c?.commodity || hs.length) {
    out.push("", "Shipment details:", ...lines);
    if (c?.commodity) out.push(`Commodity   : ${c.commodity}`);
    if (hs.length) out.push(`HS code     : ${hs.join(", ")}`);
  }
  out.push("", "Along with the rates, tell us how long the transit takes, until when they hold and what they leave out.", "", "Many thanks.");
  return out.join("\n");
}
export const mailOf = (body, name, country) => `${greeting(name, country)}\n\n${body}`;

// what the case does not say that a vendor will ask for
export function missingOf(c) {
  const m = [], [from, to] = endsOf(c), mode = modeOf(c), cargo = c?.cargo || [];
  if (!from || !to) m.push("origin or destination");
  if (!mode) m.push("mode");
  if (!cargo.length) m.push("cargo");
  else if (mode !== "FCL") {
    if (!cargo.some(x => x.weight)) m.push("weight");
    if (!cargo.some(x => x.cbm || x.dims)) m.push("volume or dimensions");
  }
  if (!c?.commodity) m.push("commodity");
  if (!termsOf(c)) m.push("incoterm");
  return m;
}

// the vendors ticked when the card is opened for the first time: they quoted for one of this case's countries before
// and are not asked on this case yet; the ones quoting most first, at most five
export const MAX_PRETICK = 5;
const meets = (list, countries) => (list || []).some(x => countries.includes(x));
export function pretick(vendors, c) {
  const far = farCountries(c), asked = new Set((c?.rr || []).map(r => r.vendor));
  if (!far.length) return [];
  return (vendors || [])
    .filter(v => meets(v.quotedIn, far) && !asked.has(v.key) && v.to)
    .sort((a, b) => (b.quoted || 0) - (a.quoted || 0) || String(b.lastAsked || "").localeCompare(String(a.lastAsked || "")) || a.key.localeCompare(b.key))
    .slice(0, MAX_PRETICK).map(v => v.key);
}
// does a vendor work in one of these countries (its own country, or a case there it was asked on)
export const worksIn = (v, countries) => countries.includes(v.country) || meets(v.askedIn, countries);
export const quotedFor = (v, countries) => meets(v.quotedIn, countries);

export const EMAIL = /^[^\s@<>(),;:"]+@[^\s@<>(),;:"]+\.[A-Za-z]{2,}$/;
// "#JJC-MFN <jjc-mfn@x.example>" or "a@x.example": the address alone, for the To and CC lines
export const addressOf = s => { const m = /<([^>]+)>/.exec(String(s || "")); return (m ? m[1] : String(s || "")).trim(); };
// "#JJC-MFN <jjc-mfn@x.example>": the name shown beside the CC box ("#JJC-MFN"); the address when there is no name
export const nameOf = s => { const m = /^(.*?)\s*</.exec(String(s || "")); return (m && m[1].trim()) || addressOf(s); };
// addresses as the helper takes them: the address alone, only well-formed ones, each once
export function cleanList(list) {
  const out = [];
  for (const x of list || []) { const a = addressOf(x); if (EMAIL.test(a) && !out.some(y => y.toLowerCase() === a.toLowerCase())) out.push(a); }
  return out;
}

// ---------------------------------------------------------------- several addresses in one box
// "a@x.example; b@x.example, c@y.example", one per line, or "Name <a@x.example>": {good, bad}, each address once, in order
export function parseAddresses(text) {
  const good = [], bad = [];
  for (const piece of String(text || "").split(/[;,\n]+/)) {
    const p = piece.trim(); if (!p) continue;
    for (const raw of p.includes("<") ? [addressOf(p)] : p.split(/\s+/)) {
      const a = raw.trim().replace(/^mailto:/i, ""); if (!a) continue;
      if (!EMAIL.test(a)) { if (!bad.includes(a)) bad.push(a); continue; }
      if (!good.some(x => x.toLowerCase() === a.toLowerCase())) good.push(a);
    }
  }
  return { good, bad };
}
export const domainOf = a => (String(a || "").split("@")[1] || "").trim().toLowerCase();
// a mailbox provider names nobody's company (OppList's matching.FREE_MAIL_STEMS): gmail.com, yahoo.com.br ...
const FREE_STEMS = new Set(["gmail", "googlemail", "hotmail", "outlook", "yahoo", "ymail", "rocketmail", "live", "msn", "aol",
  "icloud", "me", "mac", "protonmail", "proton", "pm", "qq", "163", "126", "sina", "sohu", "foxmail", "yandex", "gmx", "mail",
  "email", "zoho", "fastmail", "tutanota", "terra", "uol", "bol"]);
export function isFreeMail(domain) {
  const labels = String(domain || "").toLowerCase().split(".").filter(Boolean);
  while (labels.length > 2 && ["www", "mail", "smtp", "webmail", "email", "correo", "mx"].includes(labels[0])) labels.shift();
  return FREE_STEMS.has(labels[0] || "");
}
// one mail per company: the addresses at one domain together, the first To and the rest CC; a free-mail address is a
// company of its own (as OppList takes it). [{email, cc}]
export function byCompany(addresses) {
  const groups = [];
  for (const a of addresses) {
    const d = domainOf(a), g = isFreeMail(d) ? null : groups.find(x => x.domain === d);
    if (g) g.cc.push(a); else groups.push({ domain: isFreeMail(d) ? "" : d, email: a, cc: [] });
  }
  return groups.map(({ email, cc }) => ({ email, cc }));
}

// ---------------------------------------------------------------- Make Outlook drafts
// What Create drafts puts on the clipboard for the helper on the computer (JoJo\OutlookHelper): a line for people,
// then the request as JSON. The helper reads from the first "{" to the last "}", so the first line has no brace.
// mails: [{ name, to, cc, text }] as the drawer shows them; the same id when the same drafts are copied again.
export const MAX_DRAFTS = 30;      // the most the helper makes at once
export function draftsRequest(opp, id, mails, subject) {
  return { kind: "jojo-drafts", v: 1, id, app: "OppList", ref: opp, folder: "OppList",
    mails: mails.map(m => ({ name: m.name, to: [m.to], cc: [...(m.cc || [])], subject, text: m.text })) };
}
export const draftsClip = req => `Mail drafts for ${req.ref} (${req.mails.length} ${req.mails.length === 1 ? "mail" : "mails"}), `
  + `as a request for the Outlook helper.\n${JSON.stringify(req)}`;
