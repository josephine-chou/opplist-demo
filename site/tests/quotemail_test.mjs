// Unit checks of quotemail.js. The file runs in Node (node tests/quotemail_test.mjs, from the site folder) and in the
// browser runner. Every case, name, address and price here is invented.
import * as q from "../quotemail.js";
let fails = 0;
const eq = (label, got, want) => { const ok = JSON.stringify(got) === JSON.stringify(want); fails += ok ? 0 : 1; console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `: ${JSON.stringify(got)} (want ${JSON.stringify(want)})`}`); };

const TEAM = "quotes@jojofreight.example";
const fcl = { opp: "OPP-1211", req: "Altiplano Carga", rtype: "Agent", mode: "FCL", incoterm: "DAP", oc: "Vietnam", dc: "Peru", lane: "HAIPHONG - CALLAO",
  routes: [{ from: "HAIPHONG", to: "CALLAO", mode: "FCL", incoterm: "DAP", scope: "Port to door" }],
  cargo: [{ what: "2 x 20GP" }, { what: "3 x 40HQ" }], commodity: "Ceramic tiles",
  reply: { on: "2026-11-20", subject: "[OPP-1211] Pedido de tarifa FCL HAIPHONG - CALLAO", mid: "<k7@altiplanocarga.example>", name: "Paola Arredondo", first: "Paola",
    to: "paola@altiplanocarga.example", cc: ["trafico@altiplanocarga.example"] } };
const air = { opp: "OPP-1212", req: "Blue Mesa Transport", mode: "Air", incoterm: "FCA", oc: "USA", dc: "Colombia", lane: "DALLAS - BOGOTA",
  routes: [{ from: "DALLAS", to: "BOGOTA", mode: "Air", incoterm: "FCA", scope: "Door to port" }],
  cargo: [{ what: "4 crates", weight: "410 kg", cbm: "", dims: "110 x 80 x 90 cm (4)" }], commodity: "Pump parts" };
const lcl = { opp: "OPP-1213", req: "Pearl Delta", mode: "LCL", incoterm: "DDP", oc: "India", dc: "USA", lane: "NHAVA SHEVA - HOUSTON, TX",
  routes: [{ from: "NHAVA SHEVA", to: "HOUSTON, TX", mode: "LCL", incoterm: "DDP" }], cargo: [{ what: "9 drums", weight: "2.4 t", cbm: "6.7 cbm" }] };

// -- numbers
eq("numbers as typed", ["3,480", "USD 3,480.25", "$72", "-35", ".75", "4.5.6", "", "INCLUDED", "EUR 18"].map(q.num), [3480, 3480.25, 72, -35, 0.75, null, null, null, 18]);
eq("rounding to the cent", [q.r2(2.675), q.r2(1.115), q.r2(-2.675), q.r2(0.1 + 0.7)], [2.68, 1.12, -2.68, 0.8]);
eq("money and rates", [q.money(8215), q.money(-7.5), q.rateText(2.45), q.rateText(0.375)], ["8,215.00", "-7.50", "2.45", "0.375"]);
eq("the words", ["included", " At Cost ", "TBC"].map(q.word), ["INCLUDED", "AT COST", ""]);

// -- sizes
eq("weights in kg, lb and tonnes", ["410 kg", "2,000 lbs", "14 t", "3 tons", "8 kgs", "weight unknown"].map(x => q.kgOf(x) === null ? null : Math.round(q.kgOf(x))), [410, 907, 14000, 3000, 8, null]);
eq("volume from the dimensions and the count", [q.dimsCbm("110 x 80 x 90 cm (4)"), q.dimsCbm("60*45*38CM", "30 cartons"), q.dimsCbm('40" x 32" x 50"')].map(x => q.r2(x)), [3.17, 3.08, 1.05]);
const cw = q.chargeable(air);
eq("air: chargeable weight from the dimensions (the larger counts)", [cw.cw, q.r2(cw.cbm), cw.fromDims], [530, 3.17, true]);
eq("LCL: W/M with the weight in tonnes", q.wm(lcl).wm, 6.7);
eq("FCL boxes", q.boxesOf(fcl), { b: { "20'": 2, "40'HQ": 3 }, all: 5 });
eq("qty for a unit", ["20'", "40'HQ", "CNTR", "TRIP", "BL", "per kg"].map(u => q.qtyFor(fcl, u)), [2, 3, 5, 5, 1, 1]);

// -- the lines it starts with
const lf = q.startLines(fcl);
eq("FCL DAP: a freight line per container type, the delivery per container, the forwarding fee per BL",
  lf.map(l => [l.charge, l.qty, l.unit]), [["OCEAN FREIGHT", "2", "20'"], ["OCEAN FREIGHT", "3", "40'HQ"], ["FINAL DELIVERY", "5", "TRIP"], ["FORWARDING FEE", "1", "BL"]]);
const la = q.startLines(air);
eq("air FCA from the US, door to port: freight and screening on the chargeable weight, the pickup, AES, the forwarding fee per HAWB",
  la.map(l => [l.charge, l.qty, l.unit]), [["AIR FREIGHT", "530", "KG"], ["SCREENING FEE", "530", "KG"], ["PICKUP", "1", "PICK UP"], ["EXPORT FILING (AES)", "1", "FILING"], ["FORWARDING FEE", "1", "HAWB"]]);
const ll = q.startLines(lcl);
eq("LCL DDP into the US: delivery, entry, duty at cost", ll.map(l => [l.charge, l.qty, l.unit, l.rate]),
  [["OCEAN FREIGHT", "6.7", "CBM", ""], ["FINAL DELIVERY", "1", "TRIP", ""], ["IMPORT ENTRY", "1", "ENTRY", ""], ["DUTY, MPF AND HMF", "", "ENTRY", "AT COST"], ["FORWARDING FEE", "1", "BL", ""]]);
eq("EXW adds the ex works charges, CIF the insurance", [q.startLines({ mode: "FCL", incoterm: "EXW", cargo: [] }).map(l => l.charge), q.startLines({ mode: "Air", incoterm: "CIF", cargo: [] }).map(l => l.charge)],
  [["OCEAN FREIGHT", "EX WORKS CHARGES", "FORWARDING FEE"], ["AIR FREIGHT", "SCREENING FEE", "CARGO INSURANCE", "FORWARDING FEE"]]);
eq("a mode without a rule starts with one empty line", q.startLines({ mode: "Project", cargo: [] }).map(l => [l.charge, l.qty]), [["", "1"], ["FORWARDING FEE", "1"]]);
eq("terms for the FCL door case", q.startTerms(fcl, lf), { cur: true, local: true, ins: true, space: true, gri: true, cargo: true, unforeseen: true, chassis: true, inland: false });
eq("terms for the US air export with a pickup", (({ gri, chassis, inland }) => ({ gri, chassis, inland }))(q.startTerms(air, la)), { gri: false, chassis: false, inland: true });
eq("markup rule: the percentage of the rules unless the requester is one of our offices, 10% without rules",
  [q.startPct(fcl, { markupPct: 12, officesNoMarkup: true }), q.startPct({ rtype: "Office" }, { markupPct: 12, officesNoMarkup: true }), q.startPct(fcl, undefined)], [12, 0, 10]);

// -- amounts, costs, totals
const card = { cur: "USD", costs: true, pct: 12, valid: "2026-12-08", transit: "38 to 42 days", routing: "", other: "", terms: q.startTerms(fcl, lf),
  opening: q.openingOf(fcl), closing: q.CLOSING, to: "paola@altiplanocarga.example", cc: "trafico@altiplanocarga.example", teamCc: true, subject: q.subjectOf(fcl, "OPP-1211"), attach: true,
  lines: q.startLines(fcl) };
card.lines[0].cost = "1,650"; card.lines[0].markup = q.markupFor("1,650", 12);
card.lines[1].cost = "2400"; card.lines[1].markup = q.markupFor("2400", 12);
card.lines[2].cost = "310"; card.lines[2].markup = "25";
card.lines[3].rate = "85";
eq("rate = cost + markup, else the typed rate", card.lines.map(l => q.rateOf(l, true)?.n), [1848, 2688, 335, 85]);
eq("amounts: qty x rate", card.lines.map(l => q.amountOf(l, true)?.n), [3696, 8064, 1675, 85]);
const T = q.totals(card);
eq("totals: sell, cost, profit, margin", [T.t, T.cost, T.profit, q.r2(T.margin)], [13520, 12050, 1470, 10.87]);
eq("without the cost block a cost is not used", [q.rateOf(card.lines[0], false), q.totals({ ...card, costs: false }).t], [null, 85]);
const hand = { ...card, lines: [...card.lines, { ...q.line("DESTINATION HANDLING", 1, "SHIPMENT", "40"), amount: "175" }, q.line("DUTIES AND TAXES", "", "ENTRY", "AT COST")] };
eq("a typed amount (a minimum) wins; AT COST is not counted", [q.amountOf(hand.lines[4], true), q.totals(hand).t, q.totals(hand).atCost], [{ n: 175, hand: true }, 13695, true]);
eq("no problems on the full card", q.problems(card, { today: "2026-11-24" }), []);
eq("problems: no rate, no date, a bad address, no To", q.problems({ costs: false, lines: [q.line("OCEAN FREIGHT", 1, "CNTR"), q.line("", "", "", "7")], valid: "", to: "", cc: "nobody" }),
  ["A line has no charge name.", "A line has no rate. Fill it in, or write AT COST, INCLUDED or IF APPLICABLE.", "Pick the Valid until date.", "This does not look like an email address: nobody."]);
eq("a past Valid until date and no To when it is not needed", q.problems({ costs: false, lines: [q.line("Y", 1, "BL", "7")], valid: "2026-11-10", to: "" }, { today: "2026-11-24", needTo: false }), ["The Valid until date is before today."]);
eq("warnings: below the floor, below cost", [q.warnings({ ...card, lines: [{ ...q.line("OCEAN FREIGHT", 1, "CNTR"), cost: "200", markup: "6" }] }, { marginFloorPct: 4 }),
  q.warnings({ ...card, lines: [{ ...q.line("OCEAN FREIGHT", 1, "CNTR"), cost: "200", markup: "0" }, { ...q.line("Y", 1, "BL"), amount: "-15" }] }, { marginFloorPct: 4 }), q.warnings(card, { marginFloorPct: 4 })],
  [["Margin 2.9%, under the 4% floor."], ["The quote is below our cost."], []]);

// -- the mail
eq("places written for people", ["PUERTO CORTES", "LONG BEACH, CA", "BOG", "HALIFAX, NS", "CÓRDOBA"].map(q.placeName), ["Puerto Cortes", "Long Beach, CA", "BOG", "Halifax, NS", "Córdoba"]);
eq("greeting from their mail, else the company", [q.greetOf(fcl), q.greetOf(air), q.greetOf({})], ["Hi Paola,", "Hi Blue Mesa Transport team,", "Hi there,"]);
eq("opening", q.openingOf(fcl), "Thank you for the request. Our quotation for FCL from Haiphong to Callao (2 x 20GP, 3 x 40HQ), DAP follows.");
eq("subject: RE: and the case number once", [q.subjectOf(fcl, "OPP-1211"), q.subjectOf({ reply: { subject: "RE: [OPP-1211] FW: RE: delivery quote" } }, "OPP-1211"), q.subjectOf({ subj: "AIR REQUEST DFW-BOG" }, "OPP-1212")],
  ["RE: [OPP-1211] Pedido de tarifa FCL HAIPHONG - CALLAO", "RE: [OPP-1211] delivery quote", "RE: [OPP-1212] AIR REQUEST DFW-BOG"]);
const t = q.table(card);
eq("the table: head, a row, the total", [t.head, t.rows[1], t.foot, t.note], [["Charge", "Qty", "Unit", "Rate (USD)", "Amount (USD)"], ["OCEAN FREIGHT", "3", "PER 40'HQ", "2,688.00", "8,064.00"], ["TOTAL", "", "", "", "13,520.00"], ""]);
eq("AT COST without remarks gives the note; a remark adds the column", [q.table(hand).note, q.table({ ...hand, lines: [...hand.lines.slice(0, 1), { ...hand.lines[5], remark: "Per the tariff code given" }] }).foot],
  ["Plus the AT COST lines.", ["TOTAL", "", "", "", "3,696.00", "Plus the AT COST lines"]]);
const after = q.afterText(card);
eq("after the table: validity, transit, numbered terms, the closing line", [after.split("\n").slice(0, 4), after.split("\n").pop()],
  [["Valid until: Dec 8, 2026", "Transit time: 38 to 42 days", "", "Terms:"], q.CLOSING]);
eq("the eighth term: chassis, ticked for the FCL door case", after.includes("8. A door delivery may need extra chassis days"), true);
const html = q.outlookHtml(card, fcl), plain = q.plainText(card, fcl);
eq("the mail never has a cost, a markup or the margin", [html.includes("1,650"), html.includes("2,400"), html.includes("12,050"), plain.includes("1,470.00"), html.includes("13,520.00")], [false, false, false, false, true]);
eq("what goes in the quote action", q.whatOf(card, 3), "Quote 3 by the quote card: OCEAN FREIGHT, FINAL DELIVERY, FORWARDING FEE. USD 13,520.00. Valid until Dec 8, 2026. Transit time 38 to 42 days.");
eq("recipients: To, CC and the team, each once", q.recipients({ to: "paola@altiplanocarga.example", cc: "trafico@altiplanocarga.example; PAOLA@altiplanocarga.example", teamCc: true }, TEAM),
  { to: ["paola@altiplanocarga.example"], cc: ["trafico@altiplanocarga.example", "quotes@jojofreight.example"] });
const req = q.quoteRequest({ id: "Qr7Lm2pTa", opp: "OPP-1211", card, c: fcl, teamAddress: TEAM, n: 3 });
eq("the request, version 2: reply by Message-ID, the table, the PDF from Downloads", [req.v, req.mails[0].reply, req.mails[0].attach, req.mails[0].table.rows.length, req.mails[0].text.split("\n")[0]],
  [2, { mid: "<k7@altiplanocarga.example>" }, [{ file: "OPP-1211_Quotation_3.pdf", from: "Downloads", then: "Documents\\JoJo quotes" }], 4, "Hi Paola,"]);
const clip = q.quoteClip(req), json = clip.slice(clip.indexOf("\n") + 1);
eq("the clipboard: a line for people without braces, then the JSON, with no cost in it", [clip.split("\n")[0].includes("{"), JSON.parse(json).ref, json.includes("1650"), json.includes("12050"), json.includes("markup")], [false, "OPP-1211", false, false, false]);
eq("no reply block without a Message-ID, no attachment when unticked", (r => [r.reply, r.attach])(q.quoteRequest({ id: "m4n5p6", opp: "OPP-1212", card: { ...card, attach: false }, c: air }).mails[0]), [undefined, undefined]);

// -- whose rates, and the vendor guard
const parts = { sources: [{ seg: "Freight", key: "orcaline", name: "", carrier: " OL " }, { seg: "Inland", key: "other", name: "Rimac Carga", carrier: "" },
  { seg: "Origin", key: "", name: "", carrier: "" }, { seg: "Depot", key: "own", name: "", carrier: "" }] };
eq("the parts kept: a vendor, a name typed, an empty one gone, an unknown part as Other", q.sourcesOf(parts),
  [{ seg: "Freight", key: "orcaline", name: "orcaline", carrier: "OL" }, { seg: "Inland", key: "", name: "Rimac Carga", carrier: "" }, { seg: "Other", key: "own", name: "Our own rate", carrier: "" }]);
eq("at most six parts", q.sourcesOf({ sources: Array.from({ length: 9 }, (_, i) => ({ seg: "Other", key: "", name: `Part ${i}`, carrier: "" })) }).length, 6);
eq("rateFrom: the Freight part's (nothing when someone was typed), else the first with a key",
  [q.rateFromOf(q.sourcesOf(parts)), q.rateFromOf([{ seg: "Inland", key: "rimac" }, { seg: "Origin", key: "own" }]), q.rateFromOf([{ seg: "Freight", key: "", name: "Harborview" }]),
   q.rateFromOf([{ seg: "Freight", key: "", name: "Harborview" }, { seg: "Inland", key: "rimac" }])], ["orcaline", "rimac", "", ""]);
eq("the parts as one line", q.sourcesText(q.sourcesOf(parts)), "Freight orcaline (OL), Inland Rimac Carga, Other Our own rate");
eq("a saved quote's parts back on a card; none gives one Freight line", [q.sourcesForCard(q.sourcesOf(parts))[1], q.sourcesForCard([])],
  [{ seg: "Inland", key: "other", name: "Rimac Carga", carrier: "" }, [{ seg: "Freight", key: "", name: "", carrier: "" }]]);
const vcase = { rr: [{ vendor: "orcaline" }, { vendor: "soloagent" }, { vendor: "tidewater", pending: true }, { vendor: "jjh" }],
  reply: { to: "paola@altiplanocarga.example", cc: ["trafico@altiplanocarga.example", "rates@orcaline.example", "owner@gmail.com", "desk@tidewater.example", TEAM], mid: "<k7@altiplanocarga.example>" } };
const vrows = [{ key: "orcaline", to: "sales@orcaline.example", cc: [] }, { key: "soloagent", to: "soloagent@gmail.com", cc: [] }, { key: "tidewater", to: "book@tidewater.example" },
  { key: "jjh", to: "jjh-pricing@jojofreight.example", cc: [] }];
const g = q.guardReply(vcase, vrows, TEAM);
eq("the guard: a vendor's domain out, a free mailbox and our own domain only by address, a vendor not asked yet stays", [g.to, g.cc, g.dropped],
  [["paola@altiplanocarga.example"], ["trafico@altiplanocarga.example", "owner@gmail.com", "desk@tidewater.example", "quotes@jojofreight.example"], ["rates@orcaline.example"]]);
eq("with a vendor on their mail there is no Reply All; without one the Message-ID stays", [g.mid, q.guardReply(fcl, vrows).mid], ["", "<k7@altiplanocarga.example>"]);
const sib = q.guardReply({ rr: [{ vendor: "jjh" }], reply: { to: "ava.tran@jojofreight.example", cc: ["jjh-rates@jojofreight.example", "victor.lang@jojofreight.example"], mid: "<s4@jojofreight.example>" } },
  [{ key: "jjh", to: "jjh-rates@jojofreight.example", cc: [] }]);
eq("a vendor on the requester's own domain (one office asked another): only its address goes, the requester stays", [sib.to, sib.cc, sib.mid], [["ava.tran@jojofreight.example"], ["victor.lang@jojofreight.example"], ""]);
const req2 = q.quoteRequest({ id: "Qr7Lm2pTa", opp: "OPP-1211", card: { ...card, sources: parts.sources }, c: fcl, n: 3, mid: "" });
eq("the request takes the guarded Message-ID, and never carries whose rates", [req2.mails[0].reply, JSON.stringify(req2).includes("Rimac Carga"), JSON.stringify(req2).includes("sources")], [undefined, false, false]);
const qds = q.quoteDoc({ ...card, sources: parts.sources }, { opp: "OPP-1211", n: 3, date: "2026-11-24", how: "draft", pdf: "", rateFrom: "orcaline", sources: q.sourcesOf(parts) });
eq("the saved quote keeps whose rates and rateFrom", [qds.rateFrom, qds.sources.length, qds.sources[0].carrier], ["orcaline", 3, "OL"]);
eq("the mail and the PDF never say whose rates", [q.outlookHtml({ ...card, sources: parts.sources }, fcl).includes("Rimac Carga"), JSON.stringify(q.sheetOf({ ...card, sources: parts.sources }, fcl, { opp: "OPP-1211", n: 3, today: "2026-11-24" })).includes("Rimac Carga")], [false, false]);

// -- the PDF and the documents
const sh = q.sheetOf(card, fcl, { opp: "OPP-1211", n: 3, today: "2026-11-24", me: { name: "Iris", email: "iris.chen@jojofreight.example" } });
eq("the sheet: to, from, the shipment lines", [sh.to, sh.from, sh.ship.map(x => x[0]), sh.date, sh.valid],
  [["Altiplano Carga", "Attn: Paola Arredondo", "paola@altiplanocarga.example"], ["JoJo Freight Chicago", "Iris · iris.chen@jojofreight.example"],
    ["ORIGIN", "DESTINATION", "MODE", "COMMODITY", "TERMS", "SERVICE", "TRANSIT TIME"], "Nov 24, 2026", "Dec 8, 2026"]);
eq("the sheet has no cost either", JSON.stringify(sh).includes("1,650") || JSON.stringify(sh).includes("12,050"), false);
// the quotation as the template has it
eq("the template: the number, the line under the title, the parties", [sh.no, sh.title, sh.parties],
  ["JJC-Q-1211-3", "Ocean freight, FCL port to door · Vietnam to Peru", { for: { name: "Altiplano Carga", attn: "Paola Arredondo", email: "paola@altiplanocarga.example" },
    from: { name: "Iris", company: "JoJo Freight Chicago", email: "iris.chen@jojofreight.example" } }]);
eq("the template: the shipment, three to a row", sh.grid, [["Origin", "Haiphong, Vietnam"], ["Destination", "Callao, Peru"], ["Service", "Port to door"],
  ["Mode", "FCL"], ["Equipment", "2 × 20GP, 3 × 40HQ"], ["Commodity", "Ceramic tiles"], ["Incoterm", "DAP"], ["Transit time", "38 to 42 days"]]);
eq("the template: the charges with their basis (the same charge on two sizes says which), the total", [sh.charges.map(r => [r.desc, r.basis, r.rate, r.amount]), sh.total],
  [[["Ocean freight, 20'", "2 × per 20'", "1,848.00", "3,696.00"], ["Ocean freight, 40'HQ", "3 × per 40'HQ", "2,688.00", "8,064.00"],
    ["Final delivery", "5 × per trip", "335.00", "1,675.00"], ["Forwarding fee", "1 × per BL", "85.00", "85.00"]], { amount: "13,520.00", atCost: false }]);
eq("a charge as the template writes it: codes stay, a charge typed in its own case stays",
  ["EXPORT FILING (AES)", "D.O. FEE", "DUTY / MPF / HMF", "DUTY, MPF AND HMF", "ICS2 ADMIN FEE", "CY TO CY", "EXW CHARGES", "Shrink wrap (per pallet)"].map(q.niceCharge),
  ["Export filing (AES)", "D.O. fee", "Duty / MPF / HMF", "Duty, MPF and HMF", "ICS2 admin fee", "CY to CY", "EXW charges", "Shrink wrap (per pallet)"]);
eq("a basis: a unit that is a word is written small, a code stays", [q.basisOf("3", "PICK UP"), q.basisOf("", "ENTRY"), q.basisOf("4", ""), q.basisOf("6", "CNTR"), q.basisOf("2", "HAWB"), q.basisOf("", "")],
  ["3 × per pick up", "per entry", "4 ×", "6 × per container", "2 × per HAWB", ""]);
eq("the number without a quote yet; a lane inside one country names the places", [q.quoteNo("OPP-1207"), q.titleOf({ mode: "Truck", oc: "USA", dc: "USA", routes: [{ from: "MEMPHIS, TN", to: "DENVER, CO", scope: "Door to door" }] })],
  ["JJC-Q-1207", "Trucking door to door · Memphis, TN to Denver, CO"]);
const qd = q.quoteDoc(hand, { opp: "OPP-1211", n: 3, date: "2026-11-24", how: "draft", pdf: "OPP-1211_Quotation_3.pdf" });
eq("the saved quote: sell side only", [qd.lines.length, qd.lines[0], qd.lines[4], qd.lines[5], qd.total, qd.atCost, Object.keys(qd).includes("cost")],
  [6, { charge: "OCEAN FREIGHT", qty: "2", unit: "20'", rate: 1848, word: "", amount: 3696, hand: false, remark: "" },
    { charge: "DESTINATION HANDLING", qty: "1", unit: "SHIPMENT", rate: 40, word: "", amount: 175, hand: true, remark: "" },
    { charge: "DUTIES AND TAXES", qty: "", unit: "ENTRY", rate: null, word: "AT COST", amount: null, hand: false, remark: "" }, 13695, true, false]);
const cd = q.costDoc(hand, { opp: "OPP-1211" });
eq("the saved costs: each line's cost and markup, the totals", [cd.lines.slice(0, 4), cd.lines[4], cd.cost, cd.profit, cd.margin],
  [[{ cost: 1650, markup: 198 }, { cost: 2400, markup: 288 }, { cost: 310, markup: 25 }, { cost: null, markup: null }], { cost: null, markup: null }, 12050, 1645, 12.01]);
const back = q.linesFromQuote(qd, cd);
eq("the next quote starts from the last one, costs included", [back.map(l => [l.charge, l.cost, l.markup, l.rate, l.amount]), q.totals({ cur: "USD", costs: true, lines: back }).t],
  [[["OCEAN FREIGHT", "1650", "198", "", ""], ["OCEAN FREIGHT", "2400", "288", "", ""], ["FINAL DELIVERY", "310", "25", "", ""], ["FORWARDING FEE", "", "", "85", ""],
    ["DESTINATION HANDLING", "", "", "40", "175"], ["DUTIES AND TAXES", "", "", "AT COST", ""]], 13695]);
eq("without the costs it starts from the sell rates", q.totals({ cur: "USD", costs: false, lines: q.linesFromQuote(qd, null) }).t, 13695);
eq("dates: 7 and 14 days, the month's end", [q.addDays("2026-11-24", 7), q.addDays("2026-12-22", 14), q.monthEnd("2026-11-24"), q.monthEnd("2026-10-31"), q.longDay("2026-12-08")],
  ["2026-12-01", "2027-01-05", "2026-11-30", "2026-10-31", "Dec 8, 2026"]);
eq("the PDF's name", q.pdfName("OPP-1211", 4), "OPP-1211_Quotation_4.pdf");

console.log(fails ? `${fails} failed` : "all passed");
process.exit(fails ? 1 : 0);
