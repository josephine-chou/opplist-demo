// unit checks of askmail.js: the rate request mail, its addresses and the request for the Outlook helper
import * as m from "../askmail.js";
let fails = 0;
const eq = (label, got, want) => { const ok = JSON.stringify(got) === JSON.stringify(want); fails += ok ? 0 : 1; console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `: ${JSON.stringify(got)} (want ${JSON.stringify(want)})`}`); };

// a complete FCL case between two countries outside the US, with one vendor asked already
const tiles = { oc: "Vietnam", dc: "Peru", lane: "HAIPHONG - CALLAO", mode: "FCL", incoterm: "DAP",
  routes: [{ from: "HAIPHONG", to: "CALLAO", mode: "FCL", incoterm: "DAP" }],
  cargo: [{ what: "2 x 40HQ" }, { what: "1 x 20GP" }], commodity: "Ceramic tiles", rr: [{ vendor: "K1" }] };
eq("subject: case number, lane, mode with the containers, terms", m.subjectOf("OPP-1207", tiles), "[OPP-1207] RATE REQUEST // HAIPHONG - CALLAO // FCL 2x40HQ + 1x20GP // DAP");
eq("far countries of a cross-trade: both ends", m.farCountries(tiles), ["Vietnam", "Peru"]);
eq("far countries of a US import: the origin", m.farCountries({ oc: "India", dc: "USA" }), ["India"]);
eq("far countries of a US export: the destination", m.farCountries({ oc: "USA", dc: "Colombia" }), ["Colombia"]);
eq("far countries of a domestic case: none", m.farCountries({ oc: "USA", dc: "USA" }), []);
eq("the same country at both ends counts once", m.farCountries({ oc: "Brazil", dc: "Brazil" }), ["Brazil"]);
eq("Unknown is not a country", m.farCountries({ oc: "Unknown", dc: "Ecuador" }), ["Ecuador"]);
eq("greeting words by country", ["Portugal", "Brazil", "Colombia", "Spain", "Vietnam", ""].map(m.greetingWord), ["Olá", "Olá", "Hola", "Hola", "Hi", "Hi"]);

const vendors = [
  { key: "K1", to: "k1@k1.example", quoted: 12, quotedIn: ["Peru"], askedIn: ["Peru"], country: "Peru" },
  { key: "K2", to: "k2@k2.example", quoted: 4, quotedIn: ["Vietnam"], askedIn: ["Vietnam"], country: "Vietnam" },
  { key: "K3", to: "", quoted: 10, quotedIn: ["Peru"], askedIn: ["Peru"], country: "Peru" },
  { key: "K4", to: "k4@k4.example", quoted: 6, quotedIn: ["Peru"], askedIn: ["Peru"], country: "Peru" },
  { key: "K5", to: "k5@k5.example", quoted: 9, quotedIn: ["Chile"], askedIn: ["Chile", "Peru"], country: "Chile" },
  { key: "K6", to: "k6@k6.example", quoted: 2, quotedIn: ["Vietnam"], askedIn: ["Vietnam"], country: "Vietnam", lastAsked: "2026-03-11" },
  { key: "K7", to: "k7@k7.example", quoted: 2, quotedIn: ["Vietnam"], askedIn: ["Vietnam"], country: "Vietnam", lastAsked: "2026-05-27" },
  { key: "K8", to: "k8@k8.example", quoted: 0, quotedIn: [], askedIn: ["Vietnam"], country: "Vietnam" },
];
eq("pretick: quoted for an end, not asked on this case, with an address, most quotes first, then the latest asked", m.pretick(vendors, tiles), ["K4", "K2", "K7", "K6"]);
eq("pretick stops at five", m.pretick([...Array(9)].map((_, i) => ({ key: `N${i}`, to: "n@n.example", quoted: i, quotedIn: ["Colombia"] })), { oc: "USA", dc: "Colombia" }), ["N8", "N7", "N6", "N5", "N4"]);
eq("no pretick for a domestic case", m.pretick(vendors, { oc: "USA", dc: "USA" }), []);
eq("worksIn: by a case the vendor was asked on there, not by another country", [vendors[4], vendors[7]].map(v => m.worksIn(v, ["Peru"])), [true, false]);
eq("nothing missing on a complete FCL case", m.missingOf(tiles), []);
eq("what a thin air case does not say", m.missingOf({ oc: "USA", dc: "Chile", lane: "CHICAGO - SANTIAGO", mode: "Air", cargo: [{ what: "5 cartons" }] }), ["weight", "volume or dimensions", "commodity", "incoterm"]);
eq("the address out of a display name", m.addressOf("#JJC-OPS <jjc-ops@jojofreight.example>"), "jjc-ops@jojofreight.example");
eq("the email pattern", ["k@m.io", "k m@n.io", "k@m", "k@m.n"].map(x => m.EMAIL.test(x)), [true, false, false, false]);
const body = m.bodyOf(tiles);
eq("the first lines of the body", body.split("\n").slice(0, 8), ["A new request has come in; the details follow. May we have your rates for it:", "", "Origin      : HAIPHONG", "Destination : CALLAO", "Mode        : FCL", "Terms       : DAP", "", "Shipment details:"]);
eq("the mail starts with the greeting", m.mailOf(body, "Altiplano Carga", "Ecuador").split("\n")[0], "Hola Altiplano Carga team,");

// the request for the Outlook helper
eq("the name beside the CC box", [m.nameOf("#JJC-OPS <jjc-ops@jojofreight.example>"), m.nameOf("jjc-ops@jojofreight.example")], ["#JJC-OPS", "jjc-ops@jojofreight.example"]);
eq("addresses cleaned for the helper", m.cleanList(["Desk <desk@rimaccarga.example>", "desk@rimaccarga.example", "DESK@RIMACCARGA.EXAMPLE", "no address here", "", "rates@pearldelta.example"]), ["desk@rimaccarga.example", "rates@pearldelta.example"]);
const req = m.draftsRequest("OPP-1207", "t7m2", [
  { name: "Rimac Carga", to: "desk@rimaccarga.example", cc: ["sales@rimaccarga.example", "jjc-ops@jojofreight.example"], text: m.mailOf(body, "Rimac Carga", "Peru") },
  { name: "hello@bluemesa.example", to: "hello@bluemesa.example", cc: [], text: m.mailOf(body, "", "") }], "[OPP-1207] RATE REQUEST");
eq("the request: kind, version, id, app, case and folder", [req.kind, req.v, req.id, req.app, req.ref, req.folder], ["jojo-drafts", 1, "t7m2", "OppList", "OPP-1207", "OppList"]);
eq("one mail each: To as a list, CC, the subject, the whole text", [req.mails.length, req.mails[0].to, req.mails[0].cc, req.mails[0].subject, req.mails[1].text.split("\n")[0]],
  [2, ["desk@rimaccarga.example"], ["sales@rimaccarga.example", "jjc-ops@jojofreight.example"], "[OPP-1207] RATE REQUEST", "Hi team,"]);
const clip = m.draftsClip(req), first = clip.split("\n")[0];
eq("the clipboard: a line for people without a brace, then the JSON the helper reads (first { to last })",
  [first, first.includes("{"), JSON.stringify(JSON.parse(clip.slice(clip.indexOf("{"), clip.lastIndexOf("}") + 1)))],
  ["Mail drafts for OPP-1207 (2 mails), as a request for the Outlook helper.", false, JSON.stringify(req)]);
eq("a brace in a mail does not break it", JSON.parse((c => c.slice(c.indexOf("{"), c.lastIndexOf("}") + 1))(m.draftsClip(m.draftsRequest("OPP-1203", "b", [{ name: "y", to: "y@y.example", cc: [], text: "quote {door to door}" }], "s")))).mails[0].text, "quote {door to door}");

// several addresses in one box
eq("addresses split by ; , and new lines, names and mailto: taken off, each once",
  m.parseAddresses("ana@rimaccarga.example; LEO@rimaccarga.example, Paola Arredondo <paola@altiplanocarga.example>\nmailto:rafael@pearldelta.example ana@rimaccarga.example"),
  { good: ["ana@rimaccarga.example", "LEO@rimaccarga.example", "paola@altiplanocarga.example", "rafael@pearldelta.example"], bad: [] });
eq("two addresses with only a space between them", m.parseAddresses("ana@rimaccarga.example leo@pearldelta.example").good, ["ana@rimaccarga.example", "leo@pearldelta.example"]);
eq("a wrong address is named", m.parseAddresses("ana@rimaccarga.example; quotes@bluemesa, , ;").bad, ["quotes@bluemesa"]);
eq("free mail, whatever the country suffix; a company with a mail. host is not", ["outlook.com", "yahoo.com.mx", "live.co.uk", "mail.com", "mail.orcaline.example", "bluemesa.example"].map(m.isFreeMail),
  [true, true, true, true, false, false]);
eq("one mail per company: the first address To, the rest CC; a free mail address on its own",
  m.byCompany(["ana@rimaccarga.example", "leo@pearldelta.example", "iva@RimacCarga.example", "tina@gmail.com", "omar@gmail.com"]),
  [{ email: "ana@rimaccarga.example", cc: ["iva@RimacCarga.example"] }, { email: "leo@pearldelta.example", cc: [] }, { email: "tina@gmail.com", cc: [] }, { email: "omar@gmail.com", cc: [] }]);
eq("one mail: singular", m.draftsClip(m.draftsRequest("OPP-1203", "b", [{ name: "y", to: "y@y.example", cc: [], text: "t" }], "s")).split("\n")[0].includes("(1 mail)"), true);

// the HS codes of the cargo lines, each once, after the commodity; the cargo value is never in the mail
const hsCase = { ...tiles, cargo: [{ what: "2 x 40HQ", hs: "6907.21, 6907.22", value: "USD 48,300" }, { what: "1 x 20GP", hs: "6907.22" }] };
eq("HS codes: each once, in the order of the lines", m.hsCodes(hsCase), ["6907.21", "6907.22"]);
eq("the mail: the HS code under Commodity, no value", m.bodyOf(hsCase).split("\n").filter(l => /^(Commodity|HS code)/.test(l) || l.includes("48,300")),
  ["Commodity   : Ceramic tiles", "HS code     : 6907.21, 6907.22"]);
eq("the mail: no HS line without codes", m.bodyOf(tiles).includes("HS code"), false);
eq("HS codes: the values of the edit form when the case has them", m.hsCodes({ ...hsCase, edm: { cargo: [{ hs: "6907.40.10" }] } }), ["6907.40.10"]);
console.log(fails ? `${fails} FAILED` : "ALL PASSED");
process.exit(fails ? 1 : 0);
