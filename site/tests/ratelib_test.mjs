// Unit checks of ratelib.js. All vendors, cases, rates and files here are invented.
import * as R from "../ratelib.js";
let fails = 0, count = 0;
const eq = (label, got, want) => { const ok = JSON.stringify(got) === JSON.stringify(want); count += 1; fails += ok ? 0 : 1; console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `: ${JSON.stringify(got)} (want ${JSON.stringify(want)})`}`); };

const fcl = { mode: "FCL", cargo: [{ what: "3 x 20GP" }, { what: "2 x 40HC" }], routes: [{ n: 1, mode: "FCL", from: "SANTOS", to: "HOUSTON" }] };
const two = { mode: "Air", cargo: [{ what: "2 crates", weight: "2600 lbs", cbm: "" }], routes: [{ n: 1, mode: "Air" }, { n: 2, mode: "LCL" }] };
const air = { mode: "Air", cargo: [{ what: "4 cartons", weight: "48 kg", cbm: "0.42 cbm" }], routes: [{ n: 1, mode: "Air" }] };

// the lines a vendor's rates start with
eq("FCL starts with one ocean freight line for each container type", R.startLines(fcl, "FCL").map(l => [l.seg, l.charge, l.unit]), [["Freight", "OCEAN FREIGHT", "20'"], ["Freight", "OCEAN FREIGHT", "40'HQ"]]);
eq("LCL is per W/M, air per kilo, a truck per trip, and an unknown mode gives one empty line",
  [R.startLines(two, "LCL")[0].unit, R.startLines(two, "Air")[0].unit, R.startLines({}, "Truck")[0].unit, R.startLines({}, "").length], ["W/M", "KG", "TRIP", 1]);
eq("the mode comes from the route, else from the case", [R.modeFor(two, 1), R.modeFor(two, 2), R.modeFor(two, null), R.modeFor({ routes: [{ n: 1, mode: "Not specified" }] }, 1)], ["Air", "LCL", "Air", ""]);

// the quantities, counted as the quote card counts them
eq("containers of one type, and all of them", [R.autoQty(fcl, "20'", "FCL"), R.autoQty(fcl, "40'HQ", "FCL"), R.autoQty(fcl, "CNTR", "FCL"), R.autoQty(fcl, "TRIP", "FCL")], [3, 2, 5, 5]);
eq("air counts the chargeable weight: the larger of the kilos and cbm x 167, rounded up", R.autoQty(air, "KG", "Air"), 71);
eq("a weight in pounds: whole kilos by air, the W/M at sea", [R.autoQty(two, "KG", "Air"), R.autoQty(two, "W/M", "LCL")], [1180, 1.18]);
eq("a case with no cargo gives no quantity, and the line then counts 1", [R.autoQty({ cargo: [] }, "KG", "Air"), R.autoQty({ cargo: [] }, "W/M", "LCL"), R.qtyOf({ qty: "" }, null)], [null, null, 1]);
eq("a shipment and a BL count once", [R.autoQty(fcl, "SHIPMENT", "FCL"), R.autoQty(fcl, "BL", "FCL")], [1, 1]);

// what a line comes to
const L = (rate, min = "", qty = "", amount = "") => ({ seg: "Freight", charge: "LINE HAUL", unit: "KG", rate, min, qty, amount });
eq("the rate times the quantity", R.amountOf(L("2.75"), 71), { n: 195.25 });
eq("the minimum when it is the larger", R.amountOf(L("0.9", "120"), 71), { n: 120, min: true });
eq("a typed quantity wins over the one of the case", R.amountOf(L("25", "", "4"), 1180), { n: 100 });
eq("a typed amount is the amount", R.amountOf(L("25", "", "", "2,480"), 1180), { n: 2480, hand: true });
eq("the words a rate can say", ["At Cost", "included", "IF applicable", "n/a"].map(w => R.amountOf(L(w), 1)), [{ w: "AT COST" }, { w: "INCLUDED" }, { w: "IF APPLICABLE" }, { w: "N/A" }]);
eq("an empty rate and a rate that is no number count nothing", [R.amountOf(L(""), 1), R.amountOf(L("xyz"), 1)], [null, null]);
const lines = [L("950", "", "", ""), { ...L("1400", "1650"), unit: "40'HQ" }, { ...L("AT COST"), unit: "SHIPMENT" }];
eq("the total adds the numbers only and says AT COST apart", R.totalOf(lines, u => u === "KG" ? 1 : u === "40'HQ" ? 1 : null), { t: 2600, any: true, atCost: true });
eq("with no number there is no total", R.totalOf([L("AT COST")], () => 1), { t: 0, any: false, atCost: true });

// what stops a save
const card = (over = {}) => ({ vendor: "Blue Mesa Transport", route: 1, on: "2026-10-06", cur: "USD", lines: [L("950")], valid: "", transit: "", carrier: "", free: "", said: { fuel: "incl", local: "notsaid", min: "none" }, note: "", ...over });
eq("a complete card has no problem", R.problems(card(), { today: "2026-10-06" }), []);
eq("a sending day after today, a valid until that is no date, no currency", R.problems(card({ on: "2026-10-07", valid: "2026-02-31", cur: "" }), { today: "2026-10-06" }),
  ["Pick the currency.", "The day they sent the rates cannot be after today.", "Valid until is not a date: pick it from the calendar."]);
eq("a line with no rate, a rate that is no number, a negative minimum", R.problems(card({ lines: [L(""), L("9z"), L("7", "-2")] }), { today: "2026-10-06" }),
  ["Line 1: type the rate, or the amount.", "Line 2: the rate is not a number (or AT COST, INCLUDED, IF APPLICABLE, N/A).", "Line 3: the minimum is below 0."]);
eq("no line at all, and a charge with no name", [R.problems(card({ lines: [R.blankLine()] }), {}), R.problems(card({ lines: [{ ...L("7"), charge: "" }] }), {})],
  [["Type at least one charge and its rate."], ["Line 1: name the charge."]]);
eq("too many lines, a transit time and a note that are too long", R.problems(card({ lines: Array(41).fill(L("2")), transit: "t".repeat(61), note: "n".repeat(301) }), {}),
  ["At most 40 lines.", "Transit time is longer than 60 characters.", "The note is longer than 300 characters."]);

// the document the site saves
const doc = R.docOf(card({ lines: [{ ...L("950"), unit: "20'" }, { ...L("1400", "1650"), unit: "40'HQ", charge: "ocean freight" }, { ...L("at cost"), unit: "per shipment", charge: "Terminal handling", seg: "Destination" }, R.blankLine()],
  valid: "2026-10-31", transit: " 24 days ", said: { fuel: "excl", local: "bogus" } }), { opp: "OPP-1207", autoFn: u => u === "20'" ? 3 : u === "40'HQ" ? 1 : null });
eq("the saved lines: the empty line left out, names in capitals, units as the quote card writes them", doc.lines,
  [{ seg: "Freight", charge: "LINE HAUL", unit: "20'", rate: 950, min: null, qty: 3, amount: 2850, word: "" },
   { seg: "Freight", charge: "OCEAN FREIGHT", unit: "40'HQ", rate: 1400, min: 1650, qty: 1, amount: 1650, word: "" },
   { seg: "Destination", charge: "TERMINAL HANDLING", unit: "SHIPMENT", rate: null, min: null, qty: 1, amount: null, word: "AT COST" }]);
eq("the other fields of the saved document", [doc.opp, doc.vendor, doc.cur, doc.total, doc.valid, doc.transit, doc.route, doc.on, doc.said],
  ["OPP-1207", "Blue Mesa Transport", "USD", 4500, "2026-10-31", "24 days", 1, "2026-10-06", { fuel: "excl", local: "notsaid", min: "notsaid" }]);
eq("a card with no route saves no route, and no number saves no total", [R.docOf(card({ route: null }), { opp: "OPP-1208" }).route, R.docOf(card({ lines: [L("N/A")] }), { opp: "OPP-1208" }).total], [null, null]);
eq("a saved version opened again: a counted amount and the quantity of the case stay blank, a typed quantity stays",
  R.linesFromDoc(doc, u => u === "20'" ? 3 : u === "40'HQ" ? 2 : null).map(l => [l.rate, l.min, l.qty, l.amount]),
  [["950", "", "", ""], ["1400", "1650", "1", ""], ["AT COST", "", "1", ""]]);

// on the case page
const at = h => ({ toMillis: () => 1750000000000 + h * 3600000 });
const docs = [
  { id: "r1", vendor: "Rimac Carga", route: 1, cur: "USD", total: 2780, at: at(1) },
  { id: "r2", vendor: "Rimac Carga", route: 1, cur: "USD", total: 2640, at: at(6) },
  { id: "r3", vendor: "Rimac Carga", route: 1, cur: "USD", total: 850, at: at(10) },
  { id: "p1", vendor: "Pearl Delta", route: 1, cur: "USD", total: 2910, at: at(4), valid: "2026-09-20", said: { fuel: "incl", local: "notsaid", min: "none" } },
  { id: "a1", vendor: "Altiplano Carga", route: 2, cur: "USD", total: 760, at: at(2) },
  { id: "h1", vendor: "Harborview Customs", route: 1, cur: "EUR", total: 80, at: at(2) },
];
const best = R.newest(docs, ["r3"]);
eq("the newest rates of each vendor on each route, the struck one left out, the older versions counted", [...best].map(([k, v]) => [k, v.doc.id, v.older]),
  [["Rimac Carga|1|", "r2", 1], ["Altiplano Carga|2|", "a1", 0], ["Harborview Customs|1|", "h1", 0], ["Pearl Delta|1|", "p1", 0]]);
eq("Lowest is per route and currency, and needs two vendors with a total", [...R.lowest(best)], ["Rimac Carga|1|"]);
eq("the row of a vendor: its route, past valid until, not the lowest, what it did not say, its total", R.ratesOfVendor(best, R.lowest(best), "Pearl Delta", "2026-10-06").map(x => [x.route, x.past, x.lowest, R.notSaid(x.doc), R.totalText(x.doc)]),
  [[1, true, false, ["Local charges not said"], "USD 2,910.00"]]);
eq("the text of a total with AT COST lines, and of no total", [R.totalText({ cur: "USD", total: 2640, lines: [{ word: "AT COST" }] }), R.totalText({ cur: "USD", total: null })], ["USD 2,640.00 plus AT COST", "No total"]);

// Read quote drafts
const dl = (charge, unit, rate, quote, aw = "") => ({ seg: "Freight", charge, unit, rate, min: null, qty: null, amount: null, word: "", quote, asWritten: aw });
const drafts = [
  { id: "rq21-1", vendor: "Rimac Carga", route: 1, status: "open", readAt: at(5), on: "2026-10-05", source: { kind: "mail", subject: "RE: Santos to Houston rates", sent: "2026-10-05 14:40", files: ["offer.pdf"] },
    options: [{ label: "Orca Line", cur: "USD", lines: [dl("OCEAN FREIGHT", "40'HQ", 1875, "Freight per 40HQ USD 1,875"), dl("PALLETIZING", "", 28, "Wrapping USD 28 per skid pair", "per skid pair")],
      valid: "2026-10-31", transit: "19 days", carrier: "Orca Line", free: "10 days", said: { fuel: "excl", local: "incl", min: "bogus" }, note: "Storage USD 60 per day after the free time" },
      { label: "Heron Line", cur: "", lines: [dl("OCEAN FREIGHT", "40'HQ", 1720, "40HQ USD 1,720")], valid: "next month", said: {} }] },
  { id: "rq12-1", vendor: "Rimac Carga", route: 1, status: "open", readAt: at(1), options: [{ lines: [dl("OCEAN FREIGHT", "40'HQ", 7, "y")] }] },
  { id: "rq22-1", vendor: "Pearl Delta", route: 1, status: "open", readAt: at(4), source: { kind: "paste", sent: "2026-10-05" }, options: [{ lines: [dl("OCEAN FREIGHT", "40'HQ", 2050, "2050")] }] },
  { id: "rq23-1", vendor: "Altiplano Carga", route: 1, status: "saved", readAt: at(6), options: [{ lines: [] }] },
  { id: "rq24-1", vendor: "Blue Mesa Transport", route: 1, status: "open", readAt: at(7), options: [{ lines: [dl("OCEAN FREIGHT", "40'HQ", 2, "2")] }] },
  { id: "rq25-1", vendor: "Harborview Customs", route: null, status: "open", readAt: at(2), options: [{ lines: [dl("OCEAN FREIGHT", "40'HQ", 2, "2")] }] },
];
const open = R.openDrafts(drafts, [{ id: "v1", draft: "rq24-1" }], ["rq22-1"]);
eq("open drafts: the newest per vendor and route, without the one saved here, the one set aside here and the one saved in the app", [...open].map(([k, d]) => [k, d.id]),
  [["Rimac Carga|1|", "rq21-1"], ["Harborview Customs||", "rq25-1"]]);
eq("the drafts of one vendor, route by route", R.draftsOfVendor(open, "Rimac Carga").map(d => d.id), ["rq21-1"]);
const dc = R.draftCard(drafts[0], 0);
eq("an option of a draft as a card: each line with the words of the vendor and the unit as written", dc.lines.map(l => [l.charge, l.unit, l.rate, l.qty, l.amount, l.quote, l.asWritten]),
  [["OCEAN FREIGHT", "40'HQ", "1875", "", "", "Freight per 40HQ USD 1,875", ""], ["PALLETIZING", "", "28", "", "", "Wrapping USD 28 per skid pair", "per skid pair"]]);
eq("the terms of that card, with an unknown answer turned into not said", [dc.cur, dc.valid, dc.transit, dc.carrier, dc.free, dc.said, dc.note],
  ["USD", "2026-10-31", "19 days", "Orca Line", "10 days", { fuel: "excl", local: "incl", min: "notsaid" }, "Storage USD 60 per day after the free time"]);
const dc2 = R.draftCard(drafts[0], 1);
eq("the second option: no currency, a valid until that is no date, nothing said", [dc2.cur, dc2.valid, dc2.said], ["", "", { fuel: "notsaid", local: "notsaid", min: "notsaid" }]);
eq("where a reading comes from: a mail, a pasted message", [R.draftFrom(drafts[0]), R.draftFrom(drafts[2])], [{ what: "RE: Santos to Houston rates", when: "Oct 5, 14:40" }, { what: "Pasted message", when: "Oct 5" }]);
// files with no mail among them are named by their names; with no subject and no time it says Their mail
eq("where a reading comes from: files, or nothing known", [R.draftFrom({ source: { kind: "files", subject: "", sent: "2026-10-07", files: ["tariff.pdf", "surcharges.xlsx"] } }),
  R.draftFrom({ source: { kind: "mail" } }), R.draftFrom({})],
  [{ what: "tariff.pdf, surcharges.xlsx", when: "Oct 7" }, { what: "Their mail", when: "" }, { what: "Their mail", when: "" }]);

// the list of vendors not asked on the case offers at most 15 at a time
const pv = Array.from({ length: 36 }, (_, i) => ({ key: `Depot ${String(i).padStart(2, "0")}`, askedIn: [], quotedIn: [], quoted: 0, asked: 0, lastAsked: "" }));
pv.push({ key: "Puerto Condor", country: "Chile", askedIn: ["Chile"], quotedIn: ["Chile"], quoted: 4, asked: 6, lastAsked: "2026-08-19" },
  { key: "Sierra Condor", country: "Peru", askedIn: ["Chile"], quotedIn: [], quoted: 0, asked: 3, lastAsked: "2026-07-30" },
  { key: "Condor Cargo", country: "Chile", askedIn: ["Chile"], quotedIn: ["Chile"], quoted: 1, asked: 1, lastAsked: "2026-10-01" },
  { key: "Falcon Freight", country: "Chile", askedIn: [], quotedIn: [], quoted: 0, asked: 0, lastAsked: "" },
  { key: "Condor Air", country: "", askedIn: [], quotedIn: [], quoted: 0, asked: 0, lastAsked: "" });
const pc = { oc: "Chile", dc: "USA", rr: [{ vendor: "Condor Cargo" }] };
const first = R.pickList(pv, pc);
eq("at most 15 with nothing typed", first.length, 15);
eq("nearest first: quoted for the country, then worked there (its own country or a case there), then the rest; never one asked on this case",
  first.slice(0, 5), ["Puerto Condor", "Sierra Condor", "Falcon Freight", "Condor Air", "Depot 00"]);
eq("typing narrows: a name that starts with it, then a word of a name (nearest first), then anywhere", R.pickList(pv, pc, "con"),
  ["Condor Air", "Puerto Condor", "Sierra Condor", "Falcon Freight"]);
eq("a text that only one name starts with offers that one", R.pickList(pv, pc, "sie"), ["Sierra Condor"]);
eq("a text inside the names still offers at most 15", R.pickList(pv, pc, "epot").length, 15);
eq("a text that fits nothing offers nothing", R.pickList(pv, pc, "qqq"), []);
eq("no list of vendors yet", R.pickList(undefined, pc), []);

// a vendor picked on the quote card goes in at once unless a line it replaces has a price typed
const ql = (charge, qty, unit, x = {}) => ({ charge, qty, unit, rate: "", amount: "", remark: "", cost: "", markup: "", ...x });
const start = [ql("OCEAN FREIGHT", "1", "CNTR"), ql("FORWARDING FEE", "1", "BL")];
const theirs = [ql("OCEAN FREIGHT", "", "40'", { cost: "5240", markup: "524" })];
eq("nothing typed where their lines go: they go in at once", R.fillsTyped(start, theirs), false);
eq("a cost typed on the same charge and unit: not at once", R.fillsTyped([ql("OCEAN FREIGHT", "1", "40'", { cost: "4900" })], theirs), true);
eq("a typed rate or amount counts as well, another charge does not",
  [R.fillsTyped([ql("OCEAN FREIGHT", "1", "40'", { rate: "INCLUDED" })], theirs), R.fillsTyped([ql("OCEAN FREIGHT", "1", "40'", { amount: "250" })], theirs),
    R.fillsTyped([ql("FINAL DELIVERY", "1", "TRIP", { cost: "380" })], theirs)], [true, true, false]);
const merged = R.mergeQuoteLines(start, theirs);
eq("the one start line of that charge in another unit takes their price: no second ocean freight line",
  merged.map(l => [l.charge, l.qty, l.unit, l.cost]), [["OCEAN FREIGHT", "1", "40'", "5240"], ["FORWARDING FEE", "1", "BL", ""]]);
eq("two unpriced lines of that charge: their line is a line of its own, before the forwarding fee",
  R.mergeQuoteLines([ql("OCEAN FREIGHT", "2", "20'"), ql("OCEAN FREIGHT", "1", "40'HQ"), ql("FORWARDING FEE", "1", "BL")], theirs).map(l => [l.unit, l.cost]),
  [["20'", ""], ["40'HQ", ""], ["40'", "5240"], ["BL", ""]]);
eq("a quantity they do not give keeps the one of the card, a quantity they give replaces it",
  [R.mergeQuoteLines([ql("OCEAN FREIGHT", "3", "40'")], theirs)[0].qty, R.mergeQuoteLines([ql("OCEAN FREIGHT", "3", "40'")], [{ ...theirs[0], qty: "5" }])[0].qty], ["3", "5"]);

// the costs laid in follow the vendor picked, and can be taken out again
{
  const base = [ql("OCEAN FREIGHT", "1", "20'"), ql("OCEAN FREIGHT", "1", "40'HQ"), ql("FORWARDING FEE", "1", "BL", { rate: "85" })];
  const pd = [ql("OCEAN FREIGHT", "", "20'", { cost: "980" }), ql("OCEAN FREIGHT", "", "40'HQ", { cost: "1540" }), ql("FUEL SURCHARGE", "", "40'HQ", { cost: "70" })];
  const laid = R.mergeQuoteLines(base, pd, "pd1|Freight");
  eq("laid in: a replaced line remembers what it was, an added line that it was added", laid.map(l => [l.charge, l.unit, l.cost, l.via, l.was ? l.was.cost : null]),
    [["OCEAN FREIGHT", "20'", "980", "pd1|Freight", ""], ["OCEAN FREIGHT", "40'HQ", "1540", "pd1|Freight", ""], ["FUEL SURCHARGE", "40'HQ", "70", "pd1|Freight", null], ["FORWARDING FEE", "BL", "", undefined, null]]);
  eq("taken out again: the replaced lines are back as they were, the added line is gone", R.unlayLines(laid, "pd1|Freight").map(l => [l.charge, l.unit, l.cost, "via" in l]),
    [["OCEAN FREIGHT", "20'", "", false], ["OCEAN FREIGHT", "40'HQ", "", false], ["FORWARDING FEE", "BL", "", false]]);
  const touched = laid.map(l => l.unit === "20'" ? { ...l, cost: "1050" } : l);
  eq("a line changed by hand stays, unmarked, and the others go back", R.unlayLines(touched, "pd1|Freight").map(l => [l.unit, l.cost, "via" in l]),
    [["20'", "1050", false], ["40'HQ", "", false], ["BL", "", false]]);
  eq("taking out another part leaves these lines alone", R.unlayLines(laid, "zz4|Origin").length, 4);
  eq("laid in again by the same part: what the line was stays the first state", R.mergeQuoteLines(laid, pd, "pd1|Freight")[0].was.cost, "");
  const back = R.unlayLines(touched, "pd1|Freight");
  eq("only their lines that go over a typed price wait", R.typedFor(back, pd).map(l => l.unit), ["20'"]);
}

// Read quote from the site
const fl = (name, size = 2000) => ({ name, size });
eq("the kind of each file: a mail (.msg or .eml), PDF, picture, Excel, Word, text, and nothing else",
  ["FW rates.MSG", "offer.eml", "tariff.pdf", "stamp.PNG", "sheet.xls", "list.csv", "terms.docx", "memo.txt", "tool.bat", "README"].map(R.fileKind), ["MAIL", "MAIL", "PDF", "IMG", "XLS", "XLS", "DOC", "TXT", "", ""]);
eq("the kind of a reading: a mail file among them, else files, else a pasted message", [R.readKind([fl("x.pdf"), fl("y.eml")]), R.readKind([fl("x.pdf")]), R.readKind([])], ["mail", "files", "paste"]);
const six = R.addFiles([], [1, 2, 3, 4, 5, 6, 7].map(i => fl(`scan${i}.pdf`)));
eq("six files at most: the seventh is left out with the reason", [six.files.length, six.why], [6, ["At most 6 files: scan7.pdf was left out"]]);
eq("15 MB in all at most", R.addFiles([fl("big.pdf", 11 * 1048576)], [fl("more.pdf", 5 * 1048576)]).why, ["15 MB in all at most: more.pdf was left out"]);
eq("a file it cannot read, an empty file, the same file twice", R.addFiles([fl("rates.pdf")], [fl("run.bat"), fl("blank.pdf", 0), fl("rates.pdf")]),
  { files: [fl("rates.pdf")], why: ["run.bat: Read quote takes a mail, PDFs, pictures, Excel, Word or text files", "blank.pdf is empty"] });
eq("parts of 700,000 characters, and sizes in words", [R.partsOf(1), R.partsOf(700000), R.partsOf(700001), R.sizeText(2621440), R.sizeText(358400), R.sizeText(40)], [1, 1, 2, "2.5 MB", "350 KB", "1 KB"]);
eq("a line of the Outlook message list is not the mail; a mail with From: and Subject: is; four lines are not a list line",
  [R.outlookListLine("From\tSubject\tReceived\tSize\tCategories\n\nAva Tran\tFW: Pearl Delta rates\t10:42 AM\t208 KB"), R.outlookListLine("From: Paola\nSubject: offer\nReceived: yesterday\n20GP USD 1,300"),
    R.outlookListLine("From\tSubject\tReceived\nk\tl\tm\nn\to\tp\nq\tr\ts")], [true, false, false]);
const T0 = Date.parse("2026-10-08T16:00:00Z"), ago = m => new Date(T0 - m * 60000);
const reqs = [{ id: "q1", vendor: "Puerto Condor", at: ago(100), name: "Iris" }, { id: "q2", vendor: "Puerto Condor", at: ago(25), name: "Iris" }, { id: "s1", vendor: "Sierra Condor", at: ago(15), name: "Victor" }];
eq("the newest request of the vendor, not read yet", R.readState(reqs, new Map(), "Puerto Condor", { now: T0 })?.a.id, "q2");
eq("taken in and still waiting; then read: nothing to say", [R.readState(reqs, new Map([["q2", { ok: true, state: "queued" }]]), "Puerto Condor", { now: T0 })?.kind,
  R.readState(reqs, new Map([["q2", { ok: true, state: "done" }]]), "Puerto Condor", { now: T0 })], ["wait", null]);
eq("it could not be read: the reason, until rates of the vendor are saved after it", [R.readState(reqs, new Map([["s1", { ok: false, state: "done", msg: "no rates found in the file" }]]), "Sierra Condor", { now: T0 })?.msg,
  R.readState(reqs, new Map([["s1", { ok: false, state: "done", msg: "no rates found in the file" }]]), "Sierra Condor", { now: T0, rates: [{ vendor: "Sierra Condor", at: ago(4) }] })], ["no rates found in the file", null]);
eq("nothing asked in the last week", R.readState(reqs, new Map(), "Puerto Condor", { now: T0 + 8 * 86400000 }), null);
eq("requests are read now when the app ran a cycle in the last 12 minutes", [R.readerOn({ appAt: ago(4) }, T0), R.readerOn({ appAt: ago(13) }, T0), R.readerOn(null, T0)], [true, false, false]);

// Compare rates
const cl = (seg, charge, unit, amount, word = null) => ({ seg, charge, unit, rate: amount, min: null, qty: 1, amount: word ? null : amount, word });
const cdoc = (id, vendor, route, lines, more = {}) => ({ id, vendor, route, cur: "USD", lines, total: lines.reduce((t, l) => t + (typeof l.amount === "number" ? l.amount : 0), 0),
  valid: "2026-11-20", said: { fuel: "incl", local: "excl", min: "none" }, on: "2026-10-06", at: 1, ...more });
const cdocs = [
  cdoc("c1", "Condor Cargo", 1, [cl("Freight", "OCEAN FREIGHT", "40'HQ", 1880), cl("Destination", "TERMINAL HANDLING", "40'HQ", 145), cl("Destination", "FINAL DELIVERY", "TRIP", 410), cl("Other", "PALLETIZING", "SHIPMENT", 42)]),
  cdoc("f1", "Falcon Freight", 1, [cl("Freight", "OCEAN FREIGHT", "40'HQ", 1790), cl("Destination", "TERMINAL HANDLING", "40'HQ", null, "INCLUDED"), cl("Destination", "FINAL DELIVERY", "TRIP", 390)], { valid: "2026-10-01" }),
  cdoc("b1", "Blue Mesa Transport", 1, [cl("Freight", "OCEAN FREIGHT", "40'HQ", 1730), cl("Freight", "OCEAN FREIGHT", "40'HQ", 90), cl("Destination", "TERMINAL HANDLING", "40'HQ", 145),
    cl("Destination", "FINAL DELIVERY", "TRIP", 455), cl("Other", "PALLETIZING", "SHIPMENT", 42), cl("Customs", "IMPORT ENTRY", "ENTRY", null)]),
  cdoc("h1", "Heron Line", 2, [cl("Freight", "AIR FREIGHT", "KG", 640)]),
];
const cbest = R.newest(cdocs), clow = R.lowest(cbest);
eq("the routes with two vendors or more", [R.compareRoutes(cbest), R.compareRoutes(R.newest(cdocs.slice(0, 1)))], [[1], []]);
const CM = R.compareOf(cbest, clow, { route: 1, order: ["Falcon Freight", "Nobody Here", "Condor Cargo"], today: "2026-10-08" });
eq("the columns: the order of the requests, then the rest by name, and one route only", CM.cols.map(k => k.vendor), ["Falcon Freight", "Condor Cargo", "Blue Mesa Transport"]);
eq("Lowest as on the rows, and past from valid until", CM.cols.map(k => [k.lowest, k.past]), [[true, true], [false, false], [false, false]]);
eq("the parts in order, the rows in the order they first come, a second line of the same charge and unit a row of its own",
  CM.parts.map(p => [p.seg, p.rows.map(r => `${r.charge} ${r.unit}`)]),
  [["Freight", ["OCEAN FREIGHT 40'HQ", "OCEAN FREIGHT 40'HQ"]], ["Destination", ["TERMINAL HANDLING 40'HQ", "FINAL DELIVERY TRIP"]], ["Customs", ["IMPORT ENTRY ENTRY"]], ["Other", ["PALLETIZING SHIPMENT"]]]);
const crow = (seg, i) => CM.parts.find(p => p.seg === seg).rows[i];
eq("the cells: an amount, a word, a line with no figure, a charge not in their rates", [crow("Freight", 0).cells, crow("Destination", 0).cells[0], crow("Customs", 0).cells],
  [[{ n: 1790 }, { n: 1880 }, { n: 1730 }], { w: "INCLUDED" }, [null, null, { none: true }]]);
eq("the lowest of a row; none when all figures are the same or when one says INCLUDED",
  [crow("Freight", 0).low, crow("Destination", 1).low, crow("Other", 0).low, crow("Destination", 0).low],
  [[false, false, true], [true, false, false], [false, false, false], [false, false, false]]);
eq("a part with two rows has a sum, the lowest marked among the columns that have every row of it",
  [CM.parts.find(p => p.seg === "Freight").sum, CM.parts.find(p => p.seg === "Destination").sum, CM.parts.find(p => p.seg === "Other").sum],
  [{ cells: [1790, 1880, 1820], low: [false, false, false] }, { cells: [390, 555, 600], low: [true, false, false] }, null]);
eq("what each column lacks that another has", CM.cols.map(k => k.missing), [["OCEAN FREIGHT", "IMPORT ENTRY", "PALLETIZING"], ["OCEAN FREIGHT", "IMPORT ENTRY"], []]);
const cmixed = R.newest([cdoc("m1", "Condor Cargo", 1, [cl("Freight", "OCEAN FREIGHT", "40'HQ", 1880)]), cdoc("m2", "Falcon Freight", 1, [cl("Freight", "OCEAN FREIGHT", "40'HQ", 1500)], { cur: "EUR" })]);
eq("figures in two currencies have no lowest", R.compareOf(cmixed, R.lowest(cmixed), { route: 1 }).parts[0].rows[0].low, [false, false]);

// options: several sets of rates of one vendor on one route
eq("the name of an option as it is compared, and the key with it", [R.labelKey("  Orca   DIRECT "), R.keyOf("Condor Cargo", 1, "Express"), R.keyOf("Condor Cargo", 1)],
  ["orca direct", "Condor Cargo|1|express", "Condor Cargo|1|"]);
const odoc = (id, vendor, label, total, at, more = {}) => ({ id, vendor, route: 1, label, cur: "USD", total, lines: [cl("Inland", "TRUCKING", "TRIP", total)], valid: "2026-11-20", at, ...more });
const odocs = [odoc("d1", "Condor Cargo", "Dedicated", 3600, 1), odoc("d2", "Condor Cargo", "dedicated ", 3450, 2), odoc("s1", "Condor Cargo", "Shared", 1700, 3),
  odoc("n1", "Falcon Freight", undefined, 3300, 4), odoc("e1", "Heron Line", "Express", 1250, 5, { valid: "2026-09-28" })];
const obest = R.newest(odocs);
eq("each option of a vendor is current, and the same name is a new version of it", [...obest].map(([k, v]) => [k, v.doc.id, v.older]),
  [["Condor Cargo|1|dedicated", "d2", 1], ["Condor Cargo|1|shared", "s1", 0], ["Falcon Freight|1|", "n1", 0], ["Heron Line|1|express", "e1", 0]]);
eq("Lowest among every option: a past one left out when today is given, and two options of one vendor are enough",
  [[...R.lowest(obest, "2026-10-08")], [...R.lowest(obest)], [...R.lowest(R.newest(odocs.slice(0, 3)), "2026-10-08")]],
  [["Condor Cargo|1|shared"], ["Heron Line|1|express"], ["Condor Cargo|1|shared"]]);
const orow = R.ratesOfVendor(obest, R.lowest(obest, "2026-10-08"), "Condor Cargo", "2026-10-08");
eq("the row: one entry for a route, on its lowest option, with the options behind it", orow.map(x => [x.route, x.doc.id, x.lowest, x.options.map(o => o.doc.id)]), [[1, "s1", true, ["d2", "s1"]]]);
const ocase = { routes: [{ n: 1, mode: "Truck" }] };
eq("the quote card takes the option named, else the lowest, and offers the options of that route", [R.ratesForQuote(obest, "Condor Cargo", ocase, "DEDICATED")?.id, R.ratesForQuote(obest, "Condor Cargo", ocase)?.id,
  R.ratesForQuote(obest, "Condor Cargo", ocase, "Nothing")?.id, R.quoteOptions(obest, "Condor Cargo", ocase).map(d => d.id)], ["d2", "s1", "s1", ["d2", "s1"]]);
const odraft = { id: "rq40-1", vendor: "Condor Cargo", route: 1, status: "open", options: [{ label: "Dedicated" }, { label: "Shared" }, { label: "Pallet rate" }] };
eq("what became of each option: taken in the app, saved here, skipped here, the rest set aside",
  [R.takenOf({ ...odraft, taken: ["saved", "", ""] }), R.takenOf(odraft, [{ draft: "rq40-1", option: 1 }]), R.takenOf(odraft, [], [{ draft: "rq40-1", option: 2 }]),
   R.takenOf(odraft, [{ draft: "rq40-1", option: 0 }], [{ draft: "rq40-1" }])],
  [["saved", "", ""], ["", "saved", ""], ["", "", "skipped"], ["saved", "skipped", "skipped"]]);
eq("the next option not taken, going round", [R.nextUntaken(["saved", "", ""], 0), R.nextUntaken(["", "saved", "skipped"], 1), R.nextUntaken(["saved", "skipped"], 0)], [1, 0, -1]);
eq("with options a draft stays open until every option is taken; without them the first save closes it",
  [[...R.openDrafts([odraft], [{ draft: "rq40-1", option: 0 }], [], true).keys()], [...R.openDrafts([odraft], [{ draft: "rq40-1", option: 0 }], [{ draft: "rq40-1", option: 1 }, { draft: "rq40-1", option: 2 }], true).keys()],
   [...R.openDrafts([odraft], [{ draft: "rq40-1", option: 0 }], [], false).keys()], [...R.openDrafts([odraft], [], ["rq40-1"], true).keys()]],
  [["Condor Cargo|1|"], [], [], []]);
const oc = (label, more = {}) => ({ vendor: "Condor Cargo", cur: "USD", on: "2026-10-06", lines: [{ seg: "Inland", charge: "TRUCKING", unit: "TRIP", rate: "3450", min: "", qty: "", amount: "" }], said: {}, label, ...more });
const lp = (c, ex) => R.problems(c, { today: "2026-10-08", existing: ex }).filter(x => /option/i.test(x));
eq("the name of an option: a new one needs a name the vendor does not have; a set beside named ones needs a name; one opened again keeps its own",
  [lp(oc("", { isNew: true }), ["Dedicated"]), lp(oc(" dedicated", { isNew: true }), ["Dedicated"]), lp(oc("Shared", { isNew: true }), ["Dedicated"]), lp(oc(""), ["Dedicated"]), lp(oc(""), [""]),
   lp(oc("Dedicated"), ["Dedicated"]), lp(oc("", { fixed: true }), ["", "Shared"]), lp(oc("z".repeat(61)), [])].map(x => x.length),
  [1, 1, 0, 1, 0, 0, 0, 1]);
eq("the saved version keeps the name of the option with its spaces tidied; a card with no label saves none", [R.docOf(oc("  ORCA   DIRECT "), { opp: "OPP-1231" }).label, "label" in R.docOf({ ...oc(""), label: undefined }, { opp: "OPP-1231" })],
  ["ORCA DIRECT", false]);
const ocm = R.compareOf(obest, R.lowest(obest, "2026-10-08"), { route: 1, order: ["Condor Cargo", "Falcon Freight"], today: "2026-10-08" });
eq("Compare rates has a column for each option, the options of a vendor together", ocm.cols.map(k => [k.vendor, k.label, k.lowest]),
  [["Condor Cargo", "dedicated", false], ["Condor Cargo", "Shared", true], ["Falcon Freight", "", false], ["Heron Line", "Express", false]]);
console.log(fails ? `${fails} of ${count} FAILED` : `ALL ${count} PASSED`);
process.exit(fails ? 1 : 0);
