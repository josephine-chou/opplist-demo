// "Viewing as": the place of sign-in in this copy of the site. There are no accounts and no passwords. The
// visitor picks one of the invented people of the snapshot; the pages then see that person as signed in, so
// menus, panels and the store's rules follow, exactly as they follow an account on a real installation.
const KEY = "opplist-demo.site.viewing-as";
const auth = { currentUser: null };
const watchers = new Set();
let started = false, listPeople = () => [];

// boot.js: how to list the people the switch offers ({ email, name, role, costs, report }), in the order shown.
// Asked every time, so a person added or switched off under Users joins or leaves the switch.
export function setPeople(fn) { listPeople = fn; }

export function getAuth() { return auth; }

// the person chosen last time; a link can name one instead: index.html?as=ava.tran@jojofreight.example
function remembered() {
  try { const asked = new URLSearchParams(globalThis.location?.search || "").get("as"); if (asked) return asked.toLowerCase(); } catch (_) { /* no address bar (Node) */ }
  try { return globalThis.localStorage?.getItem(KEY) || ""; } catch (_) { return ""; }
}
function remember(email) { try { if (email) globalThis.localStorage?.setItem(KEY, email); else globalThis.localStorage?.removeItem(KEY); } catch (_) { /* a private window: not remembered */ } }
const userOf = email => ({ email, uid: `demo-${email}` });
function announce() { for (const fn of [...watchers]) { try { fn(auth.currentUser); } catch (e) { console.error(e); } } }

// The first watcher starts the session: the person chosen last time, or the first of the list.
export function onAuthStateChanged(a, fn) {
  watchers.add(fn);
  if (!started) {
    started = true;
    const email = remembered();
    const all = listPeople(), first = all.find(p => p.email === email) || all[0];
    auth.currentUser = first ? userOf(first.email) : null;
  }
  queueMicrotask(() => fn(auth.currentUser));
  return () => watchers.delete(fn);
}
// the one way in: an address of the list (the password argument of the old call is not looked at)
export async function signInWithEmailAndPassword(a, email) {
  const p = listPeople().find(x => x.email === String(email || "").toLowerCase());
  if (!p) { const e = new Error("That person is not part of the demo."); e.code = "auth/user-not-found"; throw e; }
  auth.currentUser = userOf(p.email); remember(p.email); announce();
  return { user: auth.currentUser };
}
export async function signOut() { auth.currentUser = null; announce(); }
// the people the switch offers
export const people = () => listPeople().map(p => ({ ...p }));
