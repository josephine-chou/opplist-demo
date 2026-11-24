// unit checks of editlib.js: node tests/editlib_test.mjs (from the site folder), or the browser test page
import * as E from "../editlib.js";
let fails = 0;
const eq = (label, got, want) => { const ok = JSON.stringify(got) === JSON.stringify(want); fails += ok ? 0 : 1; console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `: ${JSON.stringify(got)} (want ${JSON.stringify(want)})`}`); };

// numbers as typed
eq("numbers: plain, thousands, decimals, decimal comma", ["8400", "8,400", "6.75", "18,5", "2,345.5", "2.345,5", ".25", " 60 "].map(E.numOf), [8400, 8400, 6.75, 18.5, 2345.5, 2345.5, 0.25, 60]);
eq("numbers: empty is null, words and negatives are not numbers", [E.numOf(""), E.numOf(null), ...["xyz", "-8", "3.4.5", "14,2,6", "9 lbs"].map(x => Number.isNaN(E.numOf(x)))], [null, null, true, true, true, true, true]);
eq("numbers shown: no trailing zeros, three decimals", [8400, 6.75, 0.1 + 0.7, 2 / 3].map(E.fmtNum), ["8400", "6.75", "0.8", "0.667"]);

// HS codes
eq("HS: one, several, spaces collapsed", ["3920.10.20", "8413.70, 8414.59", "8413.70;8414.59", " 9403 60 "].map(t => E.hsOf(t).codes), ["3920.10.20", "8413.70, 8414.59", "8413.70, 8414.59", "9403 60"]);
eq("HS: too short, too long, letters", ["456", "98765432109", "HS8413"].map(t => E.hsOf(t).why === E.HS_HINT), [true, true, true]);
eq("HS: empty is no code", E.hsOf("   "), { codes: null, why: "" });

// the cargo form
const r1 = E.readCargo({ pieces: "18", pack: "Crates", cq: "", ct: "", l: "110", w: "90", h: "140", du: "cm", wt: "8,400", wu: "kg", wb: "total", cbm: "", cb: "total", com: "  ceramic   tiles ", hs: "6907.21", val: "23,750", cur: "" });
eq("cargo read: numbers as numbers, text squeezed, USD when a value has no currency", r1.vals,
  { pieces: 18, cq: null, l: 110, w: 90, h: 140, wt: 8400, cbm: null, val: 23750, pack: "Crates", ct: null, du: "cm", wu: "kg", wb: "total", cb: "total", com: "ceramic tiles", hs: "6907.21", cur: "USD" });
eq("cargo read: nothing wrong", r1.errs, {});
const r2 = E.readCargo({ pieces: "4.5", cq: "0", wt: "bulky", hs: "34", cur: "CA$" });
eq("cargo read: a part piece, no containers, a word for a weight, a bad HS code and currency", Object.keys(r2.errs).sort(), ["cq", "cur", "hs", "pieces", "wt"]);
eq("cargo read: the currency upper case", E.readCargo({ val: "25", cur: "brl" }).vals.cur, "BRL");
eq("cargo check: a size needs its unit; containers their type and number", E.checkCargo({ l: 95, du: null, cq: 3, ct: null }), { du: "Pick the unit", ct: "Pick the type" });
eq("cargo check: a new line needs something that counts", E.checkCargo({ pieces: null, l: null, wt: null, cbm: null, cq: null, com: null, w: 5 })._ !== undefined, true);
eq("cargo check: an old gap is not held against a change elsewhere", E.checkCargo({ ct: "20GP", cq: null, wt: 1300 }, { ct: "20GP", cq: null, wt: 1100 }), {});
eq("route check: a new route needs where it goes", [E.checkRoute({ dc: null, d: [] })._ !== undefined, E.checkRoute({ dc: "Peru", d: [] }), E.checkRoute({ dc: null, d: [{ k: "LIM" }] })], [true, {}, {}]);

// what changed
const seen = { id: 57, n: 1, pieces: 15, pack: "Crates", l: 110, w: 90, h: 140, du: "cm", wt: 8100, wu: null, wb: null, cbm: null, cb: null, ct: null, cq: null, com: "Ceramic tiles", hs: null, val: null, cur: null };
const ch = E.changes(seen, { ...r1.vals }, E.CARGO_FIELDS);
eq("change: only what differs, with what was seen; capitals and spaces are not a change; kg and total are the defaults",
  [ch.from, ch.to], [{ pieces: 15, wt: 8100, hs: null, val: null, cur: null }, { pieces: 18, wt: 8400, hs: "6907.21", val: 23750, cur: "USD" }]);
eq("change: nothing", E.changes(seen, { ...seen }, E.CARGO_FIELDS).n, 0);
eq("change: a basis set from empty is a change", E.changes(seen, { ...seen, wb: "each" }, E.CARGO_FIELDS).to, { wb: "each" });
const rs = { id: 14, n: "A", oc: "USA", o: { k: "ORD" }, dc: "Peru", d: [{ k: "LIM" }], pod: [], mode: "Air", inc: "FCA", scope: null };
const callao = E.offPick({ sys: "UN/LOCODE", cc: "PE", code: "CLL", name: "Callao", sub: "CAL", kind: "seaport", lat: -12.05, lon: -77.15 }, "callao");
eq("change: places by key or text; an official pick differs even from its own words",
  E.changes({ ...rs, o: { t: "Callao" } }, { ...rs, o: callao, d: [{ k: "lim" }] }, E.ROUTE_FIELDS).to, { o: callao });
eq("change: a destination added", E.changes(rs, { ...rs, d: [{ k: "LIM" }, { t: "Arequipa" }] }, E.ROUTE_FIELDS).from, { d: [{ k: "LIM" }] });
eq("add: what was filled in, no orphan units", E.addValues({ pieces: 6, pack: "Drums", wt: null, wu: "kg", wb: "total", cbm: null, cb: "total", l: null, w: null, h: null, du: "cm", val: null, cur: "USD", com: "Resin" }, "cargo"),
  { pieces: 6, pack: "Drums", com: "Resin" });
eq("the official pick as sent", callao, { t: "callao", off: { sys: "UN/LOCODE", cc: "PE", code: "CLL", name: "Callao", sub: "CAL", kind: "seaport", lat: -12.05, lon: -77.15 } });

// the edits that wait, laid over ed
const ed = { cargo: [{ ...seen }, { ...seen, id: 58, n: 2, pieces: 2, wt: 640 }], routes: [{ ...rs }, { ...rs, id: 15, n: "B", mode: "LCL" }] };
const acts = [
  { type: "edit", id: "e1", part: "cargo", op: "change", target: 57, from: { wt: 8100 }, to: { wt: 8400 }, name: "Iris" },
  { type: "edit", id: "e2", part: "cargo", op: "add", to: { pieces: 4, pack: "Cartons" }, name: "Iris" },
  { type: "edit", id: "e3", part: "route", op: "strike", target: 15, name: "Victor" },
  { type: "edit", id: "e4", part: "route", op: "add", to: { dc: "Chile" }, name: "Victor" },
  { type: "quote", id: "e5" },
];
const m = E.mergeEd(ed, acts);
eq("merged: the change shows, marked as waiting", [m.cargo[0].wt, m.cargo[0].pend, m.cargo[0].by], [8400, ["wt"], "Iris"]);
eq("merged: the line added takes the next number, no id", [m.cargo[2].n, m.cargo[2].id, m.cargo[2].added], [3, null, true]);
eq("merged: the route struck out is gone, the one added takes the next letter", m.routes.map(r => r.n), ["A", "B"]);
eq("merged: ed itself untouched", ed.cargo[0].wt, 8100);
eq("merged: the last route is never struck", E.mergeEd({ cargo: [], routes: [{ ...rs }] }, [{ type: "edit", part: "route", op: "strike", target: 14 }]).routes.length, 1);
eq("letters", [1, 3, 26, 27, 53].map(E.letter), ["A", "C", "Z", "AA", "BA"]);
eq("strike only while more than one is left", [E.canStrike(m.cargo), E.canStrike([{ id: 1 }, { id: null, added: true }])], [true, false]);

// the words on the page
eq("what: containers, packing, pieces", [E.whatOf({ cq: 3, ct: "20GP" }), E.whatOf({ pieces: 1, pack: "Crates" }), E.whatOf({ pieces: 40, pack: "Jumbo bags" }), E.whatOf({ pieces: 5, pack: "Boxes" }), E.whatOf({ pieces: 1, pack: "Boxes" }), E.whatOf({ pieces: 7 })],
  ["3 x 20GP", "1 crate", "40 jumbo bags", "5 boxes", "1 box", "7 pieces"]);
eq("value text: thousands marked, cents only when there are any, USD when no currency", [E.valueText(23750, "USD"), E.valueText(23750.5, "EUR"), E.valueText(null, "USD"), E.valueText(6, null)], ["USD 23,750", "EUR 23,750.50", "", "USD 6"]);
const b1 = { n: 1, what: "15 crates", commodity: "Ceramic tiles", weight: "8100 kg", cbm: "20.8 cbm", dims: "110x90x140cm" };
eq("cells: the pushed words where nothing waits; the size from ed", E.cargoCells({ ...seen, pend: [] }, b1),
  { n: 1, what: "15 crates", com: "Ceramic tiles", goods: "", weight: "8100 kg", volume: "20.8 cbm", dims: "110 x 90 x 140 cm" });
eq("cells: the changed parts from ed; the worked-out volume goes while the size changes", E.cargoCells({ ...seen, wt: 8400, wb: "each", l: 100, hs: "6907.21", val: 23750, cur: "USD", pend: ["wt", "wb", "l", "hs", "val", "cur"] }, b1),
  { n: 1, what: "15 crates", com: "Ceramic tiles", goods: "HS 6907.21 · USD 23,750", weight: "8400 kg each", volume: "", dims: "100 x 90 x 140 cm" });
eq("route cells", E.routeCells({ ...rs, d: [{ k: "LIM" }, callao], pod: [{ t: "Paita" }] }, { result: "Lost" }),
  { n: "A", from: "ORD", to: "LIM / Callao (PECLL)", pod: "Paita", mode: "Air", inc: "FCA", scope: "", result: "Lost" });
eq("timeline: a change, the weight with the line's unit (kg when none is stored)", E.editText(acts[0], ed), { text: "Changed cargo line 1", sub: "Weight 8100 kg → 8400 kg" });
eq("timeline: an add and a strike", [E.editText(acts[1], ed).text, E.editText(acts[1], ed).sub, E.editText(acts[2], ed).text], ["Added a cargo line", "Pieces 4; Packing Cartons", "Struck out route B"]);
eq("timeline: the form's order, a unit said with its number, the value with its currency", E.editText({ type: "edit", part: "cargo", op: "add",
  to: { wt: 312.5, wu: "lbs", wb: "total", pieces: 6, pack: "Drums", l: 95, w: 60, h: 75, du: "in", val: 4800, cur: "EUR", cbm: 1.8 } }, ed).sub,
  "Pieces 6; Packing Drums; Length 95 in; Width 60 in; Height 75 in; Weight 312.5 lbs; Weight basis total; Volume 1.8 cbm; Cargo value EUR 4,800");
eq("timeline: a unit changed on its own is said on its own", E.editText({ type: "edit", part: "cargo", op: "change", target: 57, from: { du: "cm" }, to: { du: "mm" } }, ed).sub, "Size unit cm → mm");
eq("timeline: a route's places", E.editText({ type: "edit", part: "route", op: "change", target: 14, from: { o: { k: "ORD" }, d: [{ k: "LIM" }] }, to: { o: callao, d: [{ k: "LIM" }, { t: "Arequipa" }] } }, ed).sub,
  "Origin ORD → Callao (PECLL); Destination LIM → LIM / Arequipa");

// the place picker
const rows = [["ROSARIO", "Rosario", "Argentina", "seaport", "ARROS", "Port of Rosario, Puerto de Rosario"], ["BHI", "Bahía Blanca Comandante Espora", "Argentina", "airport", "BHI", ""],
  ["BAHIA BLANCA", "Bahía Blanca", "Argentina", "city", "ARBHI", ""], ["SAN ANTONIO", "San Antonio", "Chile", "seaport", "CLSAI", ""],
  ["SANTA FE", "Santa Fe", "Argentina", "city", "ARSFN", ""], ["SAN DIEGO, CA", "San Diego", "USA", "city", "", ""]];
const P = E.prepPlaces(rows);
eq("places: a code, a start, an alias, accents", [E.searchPlaces(P, "bhi").map(r => r[0]), E.searchPlaces(P, "bahia b").map(r => r[0]), E.searchPlaces(P, "puerto").map(r => r[0])], [["BHI"], ["BAHIA BLANCA", "BHI"], ["ROSARIO"]]);
eq("places: only the country chosen", E.searchPlaces(P, "san", "Chile").map(r => r[0]), ["SAN ANTONIO"]);
eq("kinds: by the mode, else by what the entry is; a city code with an airport among its functions is the city", [E.kindOf("prtm", "FCL"), E.kindOf("prtm", "Air"), E.kindOf("a", "Air"), E.kindOf("ao", "Air"), E.kindOf("pao", "Air"), E.kindOf("prtm", ""), E.kindOf("pao", ""), E.kindOf("prtm", "Truck"), E.kindOf("a", "Sea-Air")],
  ["seaport", "city", "airport", "airport", "city", "seaport", "city", "city", "airport"]);
eq("kinds offered", [E.kindsOf("prtm"), E.kindsOf("pao"), E.kindsOf("")], [["city", "seaport"], ["city", "seaport", "airport"], ["city"]]);
// a small official list of one country: loc rows are [code, name, subdivision, functions, lat, lon], air rows are [iata, name, municipality, lat, lon]
const AR = { c: "AR", loc: [["EZE", "Ezeiza Apt/Buenos Aires", "B", "a", -34.8167, -58.5333], ["ROS", "Rosario", "S", "prtm", -32.95, -60.65], ["BUE", "Buenos Aires", "C", "rtom", -34.6, -58.3833], ["RCU", "Rio Cuarto", "X", "a", null, null]],
  air: [["EZE", "Ministro Pistarini International Airport", "Buenos Aires", -34.8222, -58.5358], ["MDZ", "El Plumerillo International Airport", "Mendoza", -32.8317, -68.7929]] };
const o1 = E.searchOfficial(AR, "buenos aires", { mode: "Air" });
eq("official: for Air the airport found by its city comes before the city itself; a UN/LOCODE airport its IATA entry stands for is left out",
  o1.map(e => `${e.sys} ${e.code} ${e.kind}`), ["IATA EZE airport", "UN/LOCODE BUE city"]);
eq("official: by code, five letters too", [E.searchOfficial(AR, "ROS")[0]?.name, E.searchOfficial(AR, "arros")[0]?.name, E.searchOfficial(AR, "mdz")[0]?.code], ["Rosario", "Rosario", "MDZ"]);
const pl = E.pickList({ prepped: P, file: AR, typed: "rosario", country: "Argentina", mode: "FCL" });
eq("pick list: the site's own Rosario, and not the official one again", [pl.places.map(r => r[0]), pl.official.map(e => e.code)], [["ROSARIO"], []]);
const pl2 = E.pickList({ prepped: P, file: AR, typed: "mendoza", country: "Argentina", mode: "Air" });
eq("pick list: an official airport the site's own places do not have", [pl2.places.length, pl2.official.map(e => `${e.code} ${e.kind}`)], [0, ["MDZ airport"]]);
eq("official pick: null coordinates stay null, an empty subdivision is null", E.offPick({ sys: "UN/LOCODE", cc: "AR", code: "RCU", name: "Rio Cuarto", sub: "", kind: "airport", lat: null, lon: undefined }, ""),
  { t: "Rio Cuarto", off: { sys: "UN/LOCODE", cc: "AR", code: "RCU", name: "Rio Cuarto", sub: null, kind: "airport", lat: null, lon: null } });
// a second small list, prepared once as a loaded file is: a city with two airports, and its own code that also has the airport function
const US = E.prepOfficial({ c: "US", loc: [["NYC", "New York", "NY", "prtam", 40.7, -74], ["JFK", "John F. Kennedy Apt/New York", "NY", "a", 40.6333, -73.7833],
  ["LGA", "La Guardia Apt/New York", "NY", "a", 40.7667, -73.8667], ["ALB", "Albany", "NY", "prtam", 42.65, -73.75]],
  air: [["JFK", "John F Kennedy International Airport", "New York", 40.6398, -73.7789], ["LGA", "LaGuardia Airport", "New York", 40.7772, -73.8726],
    ["EWR", "Newark Liberty International Airport", "Newark", 40.6925, -74.1687]] });
const t0 = Date.now(); let n = 0;
for (const q of ["n", "ne", "new", "new ", "new y", "new yo", "new yor", "new york"]) n += E.searchOfficial(US, q, { mode: "Air" }).length;
const ms = Date.now() - t0;
eq("the prepared list: every keystroke of a name searched in well under a second", ms < 900, true);
const nyc = E.searchOfficial(US, "new york", { mode: "Air" });
eq("the prepared list: New York for Air starts with its two airports; the city code USNYC is the city", [nyc.slice(0, 2).map(e => e.code).sort(), nyc.find(e => e.code === "NYC")?.kind], [["JFK", "LGA"], "city"]);
console.log(fails ? `${fails} FAILED` : "ALL PASSED"); process.exit(fails ? 1 : 0);
