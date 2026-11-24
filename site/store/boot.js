// Start of the page: load the snapshot, set the clock to its moment, hand the documents to the store.
// The only request this copy of the site makes is for that one file, beside the site itself.
import { SNAPSHOT } from "../config.js";
import { setClock } from "../lib.js";
import { seed, peek, list, wallToMillis } from "./firestore.js";
import { getAuth, setPeople } from "./auth.js";
import { standIn } from "./standin.js";

// resolves { ok: true } or { ok: false, reason } (the shell shows the reason instead of a page)
export async function boot() {
  let snapshot;
  try {
    const res = await fetch(new URL("../" + SNAPSHOT, import.meta.url));
    if (!res.ok) return { ok: false, reason: `The demo data is not there yet (${SNAPSHOT}, status ${res.status}).` };
    snapshot = await res.json();
  } catch (e) {
    return { ok: false, reason: `The demo data could not be read (${SNAPSHOT}).` };
  }
  start(snapshot, safeStorage());
  return { ok: true };
}
// the same start without a request, for the tests
export function start(snapshot, storage = null) {
  setClock(wallToMillis(snapshot.now));
  const auth = getAuth();
  seed(snapshot, { storage, viewer: () => (auth.currentUser ? peek(`users/${auth.currentUser.email}`) || null : null), helper: standIn });
  // the people of the "Viewing as" switch: the active users, the administrator first
  setPeople(() => {
    const users = list("users").filter(u => u && u.email && u.active !== false);
    users.sort((a, b) => (b.role === "admin") - (a.role === "admin") || (b.costs === true) - (a.costs === true) || String(a.name).localeCompare(String(b.name)));
    return users.map(u => ({ email: u.email, name: u.name || u.email, role: u.role || "member", costs: u.role === "admin" || u.costs === true, report: u.role === "admin" || u.report === true }));
  });
  standIn.start();
}
function safeStorage() { try { const s = globalThis.localStorage; s.getItem("x"); return s; } catch (_) { return null; } }
