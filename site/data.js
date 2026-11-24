// Store reads and writes. What OppList pushes (meta/sync, lists/assign, lists/inquiries, cases/{OPP}) is
// read live; what people do here is written as actions (never changed afterwards) and notes. An action OppList has
// not taken in yet is laid over the lists (merged), so everyone sees it at once, also when OppList is off.
import { doc, collection, onSnapshot, query, where, setDoc, updateDoc, getDoc, getDocs, serverTimestamp, Timestamp, writeBatch }
  from "./store/firestore.js";
import { db, S, APP_VERSION, myOwner } from "./core.js";
import { day, nowMs } from "./lib.js";
import { mergeEd } from "./editlib.js";
import { READ_PART } from "./ratelib.js";

// ---------------------------------------------------------------- change notices
const listeners = new Set();
export function onData(fn) { listeners.add(fn); return () => listeners.delete(fn); }
let pending = null;
function changed(what) {
  S.ver++;
  // several snapshots arriving together repaint once
  if (pending) { pending.add(what); return; }
  pending = new Set([what]);
  queueMicrotask(() => { const w = pending; pending = null; for (const fn of [...listeners]) { try { fn(w); } catch (e) { console.error(e); } } });
}
// resolves once the first snapshot of S[key] (meta, assign, inquiries) is in
export function whenLoaded(key) {
  if (S[key] !== undefined) return Promise.resolve();
  return new Promise(res => { const off = onData(() => { if (S[key] !== undefined) { off(); res(); } }); });
}
let failHandler = e => console.warn(e);
export function onLoadError(fn) { failHandler = fn; }

// ---------------------------------------------------------------- live data for the whole session
let unsubs = [], overlayUnsub = null, overlayFrom = -1;
const ms = v => v?.toMillis ? v.toMillis() : v instanceof Date ? v.getTime() : 0;
// an action as the site keeps it: `id` is the document's id. An edit has a field of its own called id (the
// line's or route's id in OppList); it is kept as `target`
const asAction = (id, x) => x.type === "edit" ? { ...x, target: x.id ?? null, id } : { id, ...x };

export function startLive() {
  stopLive();
  const me = S.me.email;
  const watch = (ref, set, what) => unsubs.push(onSnapshot(ref, snap => { set(snap); changed(what); }, err => { failHandler(err, what); }));
  watch(doc(db, "users", me), s => { S.meDoc = s.exists() ? s.data() : null; if (S.meDoc) Object.assign(S.me, S.meDoc, { email: me }); }, "me");
  watch(doc(db, "meta", "sync"), s => { S.meta = s.exists() ? s.data() : null; watchOverlay(); }, "meta");
  watch(doc(db, "lists", "assign"), s => { S.assign = s.exists() ? s.data() : null; }, "assign");
  watch(doc(db, "lists", "inquiries"), s => { S.inquiries = s.exists() ? s.data() : null; }, "inquiries");
  // the vendors OppList found in the mail (it pushes this list once it can take rate requests in)
  watch(doc(db, "lists", "vendors"), s => { S.vendors = s.exists() ? s.data() : null; }, "vendors");
  watch(query(collection(db, "notes"), where("by", "==", me)),
    s => { S.myNotes = s.docs.map(d => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) })); }, "notes");
  // what OppList could not apply of this person's actions in the last two weeks (My board shows them)
  watch(query(collection(db, "actionResults"), where("by", "==", me), where("at", ">", Timestamp.fromMillis(nowMs() - 14 * 86400000))),
    s => { S.myFails = s.docs.map(d => ({ id: d.id, ...d.data() })).filter(r => r.ok === false); }, "fails");
}
export function stopLive() {
  unsubs.forEach(u => u()); unsubs = []; S.ver++;
  if (overlayUnsub) overlayUnsub(); overlayUnsub = null; overlayFrom = -1;
  Object.assign(S, { meta: undefined, assign: undefined, inquiries: undefined, vendors: undefined, overlay: [], local: [], myNotes: [], myFails: [] });
}
// the actions saved after OppList's last pull (meta/sync.pulledAt): not in its lists yet
function watchOverlay() {
  const from = ms(S.meta?.pulledAt);
  if (from === overlayFrom && overlayUnsub) return;
  overlayFrom = from;
  if (overlayUnsub) overlayUnsub();
  // what OppList has just taken in leaves the overlay now, not when the new query answers (else it shows twice)
  S.overlay = S.overlay.filter(a => ms(a.at) > from);
  S.local = S.local.filter(a => !(a.confirmed && from > a.savedBefore));
  overlayUnsub = onSnapshot(query(collection(db, "actions"), where("at", ">", Timestamp.fromMillis(from))), snap => {
    S.overlay = snap.docs.map(d => asAction(d.id, d.data({ serverTimestamps: "estimate" })));
    const ids = new Set(S.overlay.map(a => a.id));
    // a saved action of this tab is dropped once the database has it, or once OppList has pulled since it was saved
    S.local = S.local.filter(a => !ids.has(a.id) && !(a.confirmed && from > a.savedBefore));
    changed("overlay");
  }, err => failHandler(err, "overlay"));
}

// ---------------------------------------------------------------- the lists with the actions laid over them
const AFFECTS = { assign: "Handled by", quote: "Quote sent", followup: "Follow-up", waiting: "Waiting on us", answered: "Answered", ask: "Rate request", close: "Won or Lost", edit: "Cargo or Route", rates: "Vendor rates", discard: "Read quote set aside", read: "Read quote", waitreq: "Waiting for requester", inforec: "Info received" };
export const actionLabel = t => AFFECTS[t] || t;
export function overlayActions() {
  const ids = new Set(S.overlay.map(a => a.id));
  return [...S.overlay, ...S.local.filter(a => !ids.has(a.id))].sort((a, b) => ms(a.at) - ms(b.at));
}
let cache = { ver: -1, val: null };
export function merged() {
  if (cache.ver === S.ver && cache.val) return cache.val;
  // pc: a colleague's Won or Lost, waiting for the admin (OppList's pendingClose, or one not taken in yet)
  const rows = (S.inquiries?.rows || []).map(r => ({ ...r, pc: r.pendingClose || null }));
  const byOpp = new Map(rows.map(r => [r.opp, r]));
  // wait = the day we asked the requester for details (OppList's note, from lists/inquiries); such a case
  // is not ours to quote yet, so the Assign List shows it under Waiting for the requester
  // latam: OppList's LATAM flag, from lists/inquiries too; mfn the same way
  let waiting = (S.assign?.waiting || []).map(r => ({ ...r, wait: byOpp.get(r.opp)?.wait || "", latam: !!byOpp.get(r.opp)?.latam, mfn: !!byOpp.get(r.opp)?.mfn }));
  let pend = (S.assign?.pending || []).map(r => ({ ...r, latam: !!byOpp.get(r.opp)?.latam, mfn: !!byOpp.get(r.opp)?.mfn }));
  const mark = (x, a) => { if (x) x.unsynced = [...(x.unsynced || []), a.type]; };
  for (const a of overlayActions()) {
    const r = byOpp.get(a.opp);
    if (a.type === "assign") {
      for (const x of [r, ...waiting.filter(w => w.opp === a.opp), ...pend.filter(p => p.opp === a.opp)]) if (x) { x.owner = a.to || ""; mark(x, a); }
    } else if (a.type === "quote") {
      if (r) {
        if (r.st === "New") Object.assign(r, { st: "Quoted", quoted: a.date, next: "FU1", due: "" });
        Object.assign(r, { fu: "", since: a.date, last: a.date, lastText: `Quoted ${day(a.date)}` }); mark(r, a);
      }
      waiting = waiting.filter(w => w.opp !== a.opp);
      if (r) r.wait = "";
    } else if (a.type === "followup") {
      if (r) { Object.assign(r, { fu: "", since: a.date, last: a.date, lastText: `Follow-up ${day(a.date)}` }); mark(r, a); }
    } else if (a.type === "waiting") {
      const p = { on: a.date, what: a.what, by: a.name };
      if (r) { Object.assign(r, { pend: p, fu: "", last: a.date, lastText: `They asked ${day(a.date)}` }); if (a.who) r.owner = a.who; mark(r, a); }
      pend = pend.filter(x => x.opp !== a.opp);
      const row = { opp: a.opp, req: r?.req || "", subj: r?.subj || "", on: a.date, what: a.what, owner: a.who || r?.owner || "", by: a.name, latam: !!r?.latam, mfn: !!r?.mfn };
      mark(row, a); pend.push(row);
    } else if (a.type === "answered") {
      if (r) { Object.assign(r, { pend: null, last: a.date, lastText: `Answered ${day(a.date)}` }); mark(r, a); }
      pend = pend.filter(x => x.opp !== a.opp);
    } else if (a.type === "waitreq" || a.type === "inforec") {
      // Waiting for requester set or ended here; OppList's next sync says when FU1 is due
      const since = a.type === "waitreq" ? a.date : "";
      if (r && r.st === "New") { Object.assign(r, { wait: since, next: since ? "" : "Send the quote", due: "" }); mark(r, a); }
      for (const w of waiting.filter(x => x.opp === a.opp)) w.wait = since;
    } else if (a.type === "ask") {
      // vendors asked: "nothing done yet" no longer holds (OppList's next sync says what comes next)
      if (r) { if (r.st === "New" && (/^Nothing done yet/.test(r.next || "") || r.wait)) r.next = "Send the quote"; r.wait = ""; mark(r, a); }
      for (const w of waiting.filter(x => x.opp === a.opp)) w.wait = "";     // a rate request ends a wait
    } else if (a.type === "close") {
      // Won or Lost waits for the administrator; the case does not change here
      if (r) { r.pc = { result: a.result, by: a.name, at: a.date, id: a.id, evidence: a.evidence }; mark(r, a); }
    }
  }
  // a Won or Lost sent from the site leaves the open lists at once; Inquiries shows it
  // under Closed, marked waiting for the administrator, and it comes back if it is not confirmed
  const closing = new Set(rows.filter(r => r.pc).map(r => r.opp));
  waiting = waiting.filter(w => !closing.has(w.opp)); pend = pend.filter(p => !closing.has(p.opp));
  // the Assign List's order: Not assigned first, then the oldest first
  waiting.sort((x, y) => (!!x.owner - !!y.owner) || String(x.rcv).localeCompare(String(y.rcv)) || x.opp.localeCompare(y.opp));
  pend.sort((x, y) => (!!x.owner - !!y.owner) || String(x.on).localeCompare(String(y.on)) || x.opp.localeCompare(y.opp));
  cache = { ver: S.ver, val: { rows, byOpp, waiting, pending: pend } };
  return cache.val;
}
// the same actions laid over one case document (the case page)
export function mergedCase(c) {
  if (!c) return c;
  const out = { ...c };
  const r = merged().byOpp.get(c.opp);
  out.pc = c.pendingClose || null;
  if (r) for (const k of ["st", "owner", "pend", "fu", "since", "next", "due", "quoted", "last", "lastText", "unsynced", "pc"]) if (k in r) out[k] = r[k];
  out.waitingActs = overlayActions().filter(a => a.opp === c.opp);
  // Waiting for requester as set or ended here (or ended by a quote or a rate request)
  for (const a of out.waitingActs) {
    if (a.type === "waitreq" && out.st === "New") out.wait = { since: a.date, what: a.what, next: "" };
    else if (a.type === "inforec" || a.type === "quote" || a.type === "ask") delete out.wait;
  }
  // rate requests logged here and not taken in yet show in the Rate requests list, marked
  const asked = out.waitingActs.filter(a => a.type === "ask").flatMap(a => [...(a.vendors || []), ...(a.adhoc || []).map(x => x.email)]
    .map(v => ({ vendor: v, asked: a.date, replied: "", quoted: false, pending: true })));
  if (asked.length) out.rr = [...(c.rr || []), ...asked];
  // rates typed for a vendor the case has no rate request for (OppList makes one when it takes them in)
  const have = new Set((out.rr || []).map(x => x.vendor));
  for (const a of out.waitingActs.filter(x => x.type === "rates" && x.vendor && !have.has(x.vendor))) {
    out.rr = [...(out.rr || []), { vendor: a.vendor, asked: "", replied: "", quoted: true, pending: true }]; have.add(a.vendor);
  }
  // Cargo and Route as the edit forms see them, OppList's ed with the edits not taken in yet laid over it
  out.edm = c.ed ? mergeEd(c.ed, out.waitingActs) : null;
  return out;
}

// ---------------------------------------------------------------- writes
// an action: Take it, Assign to, Log quote sent, Log follow-up, Waiting on us, Answered. Who and when are the
// server's; OppList takes it in at its next sync and writes actionResults/{same id}.
// evidence (a close with the mail pasted): written with the action in one go, under the same id.
// quote and costs (the quote card): quotes/{same id} (everyone reads it) and quoteCosts/{same id} (admin and
// Costs only), written with the quote action in one go; id: the one the card already used for its request
export const newActionId = () => doc(collection(db, "actions")).id;
// rates: vendorRates/{same id} (everyone reads it), written with the rates action in one go
export async function act(type, opp, fields = {}, { evidence = null, id = "", quote = null, costs = null, rates = null } = {}) {
  const ref = id ? doc(db, "actions", id) : doc(collection(db, "actions"));
  const rec = { type, opp, by: S.me.email, name: S.me.name || "", owner: myOwner(), app: APP_VERSION, ...fields };
  const local = { ...asAction(ref.id, rec), at: new Date(nowMs()), local: true, confirmed: false, savedBefore: Infinity };
  S.local.push(local); changed("local");
  try {
    const before = ms(S.meta?.pulledAt);
    if (evidence || quote || rates) {
      const b = writeBatch(db);
      b.set(ref, { ...rec, at: serverTimestamp() });
      if (evidence) b.set(doc(db, "evidence", ref.id), { images: evidence.images || [], text: evidence.text || "", by: S.me.email, name: S.me.name || "", at: serverTimestamp() });
      if (quote) b.set(doc(db, "quotes", ref.id), { ...quote, by: S.me.email, name: S.me.name || "", at: serverTimestamp() });
      if (quote && costs) b.set(doc(db, "quoteCosts", ref.id), { ...costs, by: S.me.email, at: serverTimestamp() });
      if (rates) b.set(doc(db, "vendorRates", ref.id), { ...rates, by: S.me.email, name: S.me.name || "", at: serverTimestamp() });
      await b.commit();
    } else await setDoc(ref, { ...rec, at: serverTimestamp() });
    local.confirmed = true; local.savedBefore = before;
  } catch (e) { S.local = S.local.filter(x => x !== local); changed("local"); throw e; }
  return ref.id;
}
// Read quote from the site. The files go up first, each in parts
// of at most 700,000 base64 characters (uploads/<action id>-<file>-<part>, a few to a commit; nobody reads them here),
// then the read action under that id. onStep(done, all) after each commit of parts
const asBase64 = file => new Promise((res, rej) => {
  const r = new FileReader();
  r.onload = () => res(String(r.result).replace(/^data:[^,]*,/, "")); r.onerror = () => rej(r.error || new Error("The file could not be read."));
  r.readAsDataURL(file);
});
export async function readQuote(opp, fields, files = [], onStep = () => {}) {
  const id = newActionId(), parts = [], list = [];
  for (let i = 0; i < files.length; i++) {
    const b64 = await asBase64(files[i]), n = Math.max(1, Math.ceil(b64.length / READ_PART));
    list.push({ name: String(files[i].name).slice(0, 120), type: String(files[i].type || "").slice(0, 100), size: files[i].size, parts: n });
    for (let k = 0; k < n; k++) parts.push({ id: `${id}-${i}-${k}`, data: b64.slice(k * READ_PART, (k + 1) * READ_PART) });
  }
  for (let k = 0; k < parts.length; k += 6) {
    const b = writeBatch(db);
    for (const x of parts.slice(k, k + 6)) b.set(doc(db, "uploads", x.id), { data: x.data, by: S.me.email, at: serverTimestamp() });
    await b.commit(); onStep(Math.min(k + 6, parts.length), parts.length);
  }
  return act("read", opp, { ...fields, files: list }, { id });
}
export async function addNote({ text, opp = "", remind = "" }) {
  const ref = doc(collection(db, "notes"));
  await setDoc(ref, { text, opp, remind, done: false, by: S.me.email, name: S.me.name || "", at: serverTimestamp(), updatedAt: serverTimestamp() });
  return ref.id;
}
export const updateNote = (id, fields) => updateDoc(doc(db, "notes", id), { ...fields, updatedAt: serverTimestamp() });

// OppList's places for the place picker (lists/places; no case data): read when an edit
// form first needs it, again after a quarter of an hour, and returned in one
// form, as the place picker uses it: rows [key, name, country, type, code, aliases], countries [name, iso2]. The document format
// takes no array inside an array, so OppList writes each row and country as a map; a row as an array
// is read too
let places = null;
const placeRow = r => Array.isArray(r) ? r : [r?.k || "", r?.name || "", r?.country || "", r?.type || "", r?.code || "", r?.aliases || ""];
const countryRow = c => Array.isArray(c) ? c : [c?.name || "", c?.iso2 || ""];
export async function getPlaces() {
  if (places && nowMs() - places.t < 15 * 60000) return places.v;
  const s = await getDoc(doc(db, "lists", "places"));
  const v = s.exists() ? s.data() : {};
  places = { t: nowMs(), v: { rows: (v.rows || []).map(placeRow).filter(r => r[0]), countries: (v.countries || []).map(countryRow).filter(c => c[0]) } };
  return places.v;
}

// what a colleague pasted with a Won or Lost: only the admin can read it
export async function getEvidence(id) { const s = await getDoc(doc(db, "evidence", id)); return s.exists() ? s.data() : null; }
// a case's rate request card: read once when it opens, saved as it is edited, in the editor's name
export async function getCard(opp) { const s = await getDoc(doc(db, "cards", opp)); return s.exists() ? s.data({ serverTimestamps: "estimate" }) : null; }
export const saveCard = (opp, card) => setDoc(doc(db, "cards", opp), { ...card, opp, by: S.me.email, name: S.me.name || "", at: serverTimestamp() });

// ---------------------------------------------------------------- one case
export function watchCase(opp, fn, onErr) { return onSnapshot(doc(db, "cases", opp), s => fn(s.exists() ? s.data() : null), onErr); }
// the quotes made with the card on a case (everyone), and their costs (admin and Costs only: the store refuses
// the query for anyone else, so it is only made for them)
const byAt = list => list.sort((a, b) => ms(a.at) - ms(b.at) || (a.n || 0) - (b.n || 0));
export function watchQuotes(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "quotes"), where("opp", "==", opp)), s => fn(byAt(s.docs.map(d => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) })))), onErr);
}
export function watchQuoteCosts(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "quoteCosts"), where("opp", "==", opp)), s => fn(new Map(s.docs.map(d => [d.id, d.data({ serverTimestamps: "estimate" })]))), onErr);
}
// the vendors' rates typed on the case (every version; the case page shows each vendor's newest per route)
export function watchVendorRates(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "vendorRates"), where("opp", "==", opp)), s => fn(byAt(s.docs.map(d => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) })))), onErr);
}
// the Read quote drafts on the case (OppList writes them; every status, newest first)
export function watchRateDrafts(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "rateDrafts"), where("opp", "==", opp)),
    s => fn(s.docs.map(d => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) })).sort((a, b) => ms(b.readAt) - ms(a.readAt))), onErr);
}
// the case's Read quote requests from the site, and OppList's answers to them
export function watchReads(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "actions"), where("opp", "==", opp), where("type", "==", "read")),
    s => fn(byAt(s.docs.map(d => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) })))), onErr);
}
export function watchReadResults(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "actionResults"), where("opp", "==", opp), where("type", "==", "read")),
    s => fn(new Map(s.docs.map(d => [d.id, d.data({ serverTimestamps: "estimate" })]))), onErr);
}
export function watchCaseNotes(opp, fn, onErr) {
  return onSnapshot(query(collection(db, "notes"), where("opp", "==", opp)), s => fn(s.docs.map(d => ({ id: d.id, ...d.data({ serverTimestamps: "estimate" }) }))), onErr);
}

// ---------------------------------------------------------------- admin and the report
export async function listUsers() { return (await getDocs(collection(db, "users"))).docs.map(d => d.data()); }
export async function getReport(month) { const s = await getDoc(doc(db, "reports", month)); return s.exists() ? s.data() : null; }
