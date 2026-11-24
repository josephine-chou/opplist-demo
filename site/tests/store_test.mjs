// The local store (store/firestore.js) against a small snapshot: reads, queries, a write that reaches a
// listener, the rules for costs, evidence and users, what is kept across a reload, and Reset.
// Runs in Node (node site/tests/store_test.mjs) and in the browser (tests/run.html?t=store).
import * as F from "../store/firestore.js";
import { start } from "../store/boot.js";
import { getAuth, signInWithEmailAndPassword, signOut, people } from "../store/auth.js";
import { todayIso, nowMs } from "../lib.js";
import { standIn } from "../store/standin.js";

let fails = 0;
const eq = (label, got, want) => { const ok = JSON.stringify(got) === JSON.stringify(want); fails += ok ? 0 : 1; console.log(`${ok ? "PASS" : "FAIL"}  ${label}${ok ? "" : `: ${JSON.stringify(got)} (wanted ${JSON.stringify(want)})`}`); };
const tick = () => new Promise(res => setTimeout(res, 0));
const codeOf = async fn => { try { await fn(); return "ok"; } catch (e) { return e.code || String(e); } };

const IRIS = "iris.chen@jojofreight.example", VICTOR = "victor.lang@jojofreight.example", AVA = "ava.tran@jojofreight.example";
const T = "2026-05-12T09:25:00.000";
const snapshot = () => ({
  made: "7-2026-05-12", now: "2026-05-12T09:30:00",
  docs: {
    "meta/sync": { pushedAt: { __time: T }, pulledAt: { __time: T }, owners: ["Iris", "Victor", "Ava"], readQuote: { on: true, appAt: { __time: T } } },
    "lists/assign": { waiting: [{ opp: "OPP-1201", owner: "" }], pending: [] },
    "cases/OPP-1201": { opp: "OPP-1201", st: "New", req: "Altiplano Carga" },
    "cases/OPP-1202": { opp: "OPP-1202", st: "Quoted", req: "Rimac Carga" },
    "reports/2026-04": { month: "2026-04", title: "APRIL REPORT", html: "<p>report</p>" },
    [`users/${IRIS}`]: { email: IRIS, name: "Iris Chen", owner: "Iris", role: "admin", active: true },
    [`users/${VICTOR}`]: { email: VICTOR, name: "Victor Lang", owner: "Victor", role: "member", active: true, costs: true, report: true },
    [`users/${AVA}`]: { email: AVA, name: "Ava Tran", owner: "Ava", role: "member", active: true, costs: false, report: false },
    "quotes/q1": { opp: "OPP-1202", n: 1, by: VICTOR, at: { __time: "2026-05-11T15:00:00.000" } },
    "quoteCosts/q1": { opp: "OPP-1202", by: VICTOR, cost: 1800 },
    "notes/n1": { opp: "OPP-1202", text: "call back on Thursday", by: VICTOR, at: { __time: "2026-05-11T16:00:00.000" } },
    "issues/E-1": { ref: "E-1", status: "open", by: AVA, at: { __time: "2026-05-10T10:00:00.000" } },
    "issues/E-2": { ref: "E-2", status: "resolved", by: AVA, at: { __time: "2026-05-11T10:00:00.000" } },
    "issues/E-3": { ref: "E-3", status: "open", by: VICTOR, at: { __time: "2026-05-12T08:00:00.000" } },
  },
});
// a localStorage of its own, so the test leaves the browser's alone
function memoryStorage() {
  const m = new Map();
  return { get length() { return m.size; }, key: i => [...m.keys()][i] ?? null, getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => { m.set(k, String(v)); }, removeItem: k => { m.delete(k); } };
}
const db = F.getFirestore(), auth = getAuth();
const as = email => signInWithEmailAndPassword(auth, email, "");

const kept = memoryStorage();
start(snapshot(), kept);
standIn.stop();

// ---------------------------------------------------------------- the clock and the people
eq("the clock starts at the snapshot's moment", todayIso(), "2026-05-12");
eq("the people of the switch: the administrator first, then Costs", people().map(p => p.name), ["Iris Chen", "Victor Lang", "Ava Tran"]);
eq("nobody else can be viewed as", await codeOf(() => as("someone@elsewhere.example")), "auth/user-not-found");

// ---------------------------------------------------------------- reads
await as(IRIS);
const one = await F.getDoc(F.doc(db, "cases", "OPP-1201"));
eq("a document is read by its path", [one.exists(), one.id, one.data().req], [true, "OPP-1201", "Altiplano Carga"]);
eq("a document that is not there", (await F.getDoc(F.doc(db, "cases", "OPP-1299"))).exists(), false);
const got = one.data(); got.req = "changed by the caller";
eq("a read hands out a copy", (await F.getDoc(F.doc(db, "cases", "OPP-1201"))).data().req, "Altiplano Carga");
const meta = (await F.getDoc(F.doc(db, "meta", "sync"))).data();
eq("a time comes back as a time", [typeof meta.pushedAt.toMillis(), meta.pushedAt.toDate().getHours(), meta.pushedAt.toDate().getMinutes()], ["number", 9, 25]);

// ---------------------------------------------------------------- queries
const ids = async q => (await F.getDocs(q)).docs.map(d => d.id);
eq("where ==", await ids(F.query(F.collection(db, "issues"), F.where("status", "==", "open"))), ["E-1", "E-3"]);
eq("where > on a time", await ids(F.query(F.collection(db, "issues"), F.where("at", ">", F.Timestamp.fromMillis(F.wallToMillis("2026-05-11T00:00:00"))))), ["E-2", "E-3"]);
eq("two conditions are both asked", await ids(F.query(F.collection(db, "issues"), F.where("status", "==", "open"), F.where("by", "==", AVA))), ["E-1"]);
eq("orderBy desc with a limit", await ids(F.query(F.collection(db, "issues"), F.orderBy("at", "desc"), F.limit(2))), ["E-3", "E-2"]);
eq("a count", (await F.getCountFromServer(F.query(F.collection(db, "issues"), F.where("status", "==", "open")))).data().count, 2);
eq("a whole collection", (await ids(F.collection(db, "cases"))).length, 2);
eq("a collection inside a document is refused with a sentence", (() => { try { F.collection(db, "cases", "OPP-1201", "notes"); return "ok"; } catch (e) { return e.code; } })(), "unimplemented");

// ---------------------------------------------------------------- a write reaches the listeners
const seen = [];
const off = F.onSnapshot(F.query(F.collection(db, "actions"), F.where("opp", "==", "OPP-1201")), s => seen.push(s.docs.map(d => d.data().type)));
await tick();
eq("a listener gets the answer as it is at first", seen, [[]]);
const ref = F.doc(F.collection(db, "actions"));
eq("a new document gets an id of its own", [ref.id.length, ref.path], [20, `actions/${ref.id}`]);
await F.setDoc(ref, { type: "assign", opp: "OPP-1201", to: "Iris", by: IRIS, at: F.serverTimestamp() });
await tick();
eq("and the write when it happens", seen, [[], ["assign"]]);
const saved = (await F.getDoc(ref)).data();
eq("the server time is the clock's time", Math.abs(saved.at.toMillis() - nowMs()) < 5000, true);
await F.setDoc(F.doc(db, "notes", "n2"), { opp: "OPP-1202", text: "another case", by: IRIS, at: F.serverTimestamp() });
await tick();
eq("a write that the query does not match is not announced", seen.length, 2);
off();
await F.setDoc(F.doc(F.collection(db, "actions")), { type: "followup", opp: "OPP-1201", by: IRIS, at: F.serverTimestamp() });
await tick();
eq("a listener that was switched off hears nothing more", seen.length, 2);

// a batch lands whole or not at all
const batch = F.writeBatch(db), a2 = F.doc(F.collection(db, "actions"));
batch.set(a2, { type: "quote", opp: "OPP-1201", by: IRIS, at: F.serverTimestamp() });
batch.set(F.doc(db, "quotes", a2.id), { opp: "OPP-1201", n: 1, by: "someone.else@jojofreight.example" });
eq("a batch with one refused write is refused", await codeOf(() => batch.commit()), "permission-denied");
eq("and none of it was written", (await F.getDoc(a2)).exists(), false);
eq("update of a document that is not there", await codeOf(() => F.updateDoc(F.doc(db, "notes", "nope"), { text: "x" })), "not-found");
await F.updateDoc(F.doc(db, "notes", "n2"), { text: "changed", "more.deep": 1 });
const n2 = (await F.getDoc(F.doc(db, "notes", "n2"))).data();
eq("update changes the named fields only, a dotted name inside a map", [n2.text, n2.opp, n2.by, n2.more], ["changed", "OPP-1202", IRIS, { deep: 1 }]);

// ---------------------------------------------------------------- the rules
eq("the pushed documents are read only", await codeOf(() => F.setDoc(F.doc(db, "cases", "OPP-1201"), { st: "Won" })), "permission-denied");
eq("an action is written once", await codeOf(() => F.setDoc(ref, { type: "assign", opp: "OPP-1201", to: "Ava", by: IRIS })), "permission-denied");
eq("the administrator reads the costs", (await F.getDoc(F.doc(db, "quoteCosts", "q1"))).data().cost, 1800);
await as(VICTOR);
eq("so does a member with Costs", (await ids(F.query(F.collection(db, "quoteCosts"), F.where("opp", "==", "OPP-1202")))), ["q1"]);
eq("a member reads the monthly report when ticked", (await F.getDoc(F.doc(db, "reports", "2026-04"))).exists(), true);
eq("a member cannot list the users", await codeOf(() => F.getDocs(F.collection(db, "users"))), "permission-denied");
eq("a member cannot change a user", await codeOf(() => F.setDoc(F.doc(db, "users", AVA), { costs: true }, { merge: true })), "permission-denied");
eq("except the time they were last seen", await codeOf(() => F.updateDoc(F.doc(db, "users", VICTOR), { lastLogin: F.serverTimestamp() })), "ok");
eq("a member cannot read what was pasted with a Won or Lost", await codeOf(() => F.getDoc(F.doc(db, "evidence", "x"))), "permission-denied");
eq("nobody writes in another person's name", await codeOf(() => F.setDoc(F.doc(F.collection(db, "actions")), { type: "assign", opp: "OPP-1201", by: AVA })), "permission-denied");
await as(AVA);
eq("a member without Costs is refused the costs", await codeOf(() => F.getDoc(F.doc(db, "quoteCosts", "q1"))), "permission-denied");
eq("as a query too", await codeOf(() => F.getDocs(F.query(F.collection(db, "quoteCosts"), F.where("opp", "==", "OPP-1202")))), "permission-denied");
eq("but reads the quote itself", (await F.getDoc(F.doc(db, "quotes", "q1"))).data().n, 1);
eq("and not the monthly report", await codeOf(() => F.getDoc(F.doc(db, "reports", "2026-04"))), "permission-denied");
eq("nobody changes another person's note", await codeOf(() => F.updateDoc(F.doc(db, "notes", "n1"), { text: "mine now" })), "permission-denied");
const refused = [];
F.onSnapshot(F.query(F.collection(db, "quoteCosts"), F.where("opp", "==", "OPP-1202")), () => refused.push("data"), e => refused.push(e.code));
await tick();
eq("a refused listener is told so", refused, ["permission-denied"]);
await signOut(auth);
eq("with nobody chosen nothing is read", await codeOf(() => F.getDoc(F.doc(db, "cases", "OPP-1201"))), "permission-denied");

// ---------------------------------------------------------------- the administrator changes a user; the switch follows
await as(IRIS);
await F.setDoc(F.doc(db, "users", AVA), { costs: true }, { merge: true });
await as(AVA);
eq("Costs ticked under Users opens the costs", (await F.getDoc(F.doc(db, "quoteCosts", "q1"))).data().cost, 1800);
await as(IRIS);
await F.setDoc(F.doc(db, "users", VICTOR), { active: false }, { merge: true });
eq("a person switched off leaves the switch", people().map(p => p.name), ["Iris Chen", "Ava Tran"]);

// ---------------------------------------------------------------- uploads are counted and dropped
const before = F.stats.uploads;
await F.setDoc(F.doc(db, "uploads", "u-0-0"), { data: "AAAA", by: IRIS, at: F.serverTimestamp() });
eq("an uploaded part is counted", F.stats.uploads - before, 1);
eq("and not kept", (await F.getDoc(F.doc(db, "uploads", "u-0-0"))).exists(), false);

// ---------------------------------------------------------------- the stand-in keeps the update time moving
const was = F.peek("meta/sync").pushedAt.toMillis();
standIn.beat();
eq("the time of the last update moves on", F.peek("meta/sync").pushedAt.toMillis() > was, true);

// ---------------------------------------------------------------- a reload keeps what was done; Reset forgets it
const count = F.changeCount();
eq("the changes are counted (the update time is not one of them)", count > 0 && !Object.keys(JSON.parse(kept.getItem("opplist-demo.site.changes:7-2026-05-12"))).includes("meta/sync"), true);
start(snapshot(), kept);
standIn.stop();
await as(IRIS);
eq("after a reload the action is still there", (await F.getDoc(ref)).data().type, "assign");
eq("and the user's tick", (await F.getDoc(F.doc(db, "users", AVA))).data().costs, true);
eq("with the same number of changes", F.changeCount(), count);
F.resetStore();
start(snapshot(), kept);
standIn.stop();
await as(IRIS);
eq("after Reset the action is gone", (await F.getDoc(ref)).exists(), false);
eq("and the snapshot's values are back", (await F.getDoc(F.doc(db, "users", AVA))).data().costs, false);
// a snapshot made on another day does not get an older snapshot's changes
await F.setDoc(F.doc(db, "notes", "n9"), { text: "kept for this snapshot only", by: IRIS, at: F.serverTimestamp() });
start({ ...snapshot(), made: "7-2026-05-13" }, kept);
standIn.stop();
await as(IRIS);
eq("changes kept for another snapshot are dropped", (await F.getDoc(F.doc(db, "notes", "n9"))).exists(), false);
// without any storage (a private window) the store still works
start(snapshot(), null);
standIn.stop();
await as(IRIS);
await F.setDoc(F.doc(db, "notes", "n10"), { text: "not kept anywhere", by: IRIS, at: F.serverTimestamp() });
eq("no storage: the write still lands", (await F.getDoc(F.doc(db, "notes", "n10"))).data().text, "not kept anywhere");

console.log(fails ? `${fails} FAILED` : "ALL PASSED");
process.exit(fails ? 1 : 0);
