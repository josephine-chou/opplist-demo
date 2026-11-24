// Shared runtime: the store's handles, the app's state, small DOM helpers, modals.
import { getAuth } from "./store/auth.js";
import { getFirestore } from "./store/firestore.js";
import { boot } from "./store/boot.js";
import { esc, stamp, asDate, nowMs } from "./lib.js";

// saved with every action and problem record; a real installation uses it to tell an old copy of the code
export const APP_VERSION = "demo";

export const auth = getAuth();
export const db = getFirestore();
// the snapshot is loaded before any page module runs ({ ok } or { ok: false, reason })
export const booted = await boot();

// S.me: the users/ document of the person the site is viewed as. S.meta, S.assign, S.inquiries: what OppList
// pushed (live). S.overlay: actions saved here that OppList has not taken in yet. S.myNotes: the person's own notes.
export const S = { user: null, me: null, view: null, meta: undefined, assign: undefined, inquiries: undefined, vendors: undefined, overlay: [], local: [], myNotes: [], myFails: [], ver: 0 };

export const $ = sel => document.querySelector(sel);
export const main = document.getElementById("main");
export function toast(msg, ms = 2800) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms); }
export const isAdmin = () => S.me?.role === "admin";
export const canReport = () => isAdmin() || S.me?.report === true;
// the quote card's costs, markups and margins are for the admin and the people ticked for Costs
export const canCosts = () => isAdmin() || S.me?.costs === true;
// the person's name in OppList's Handled by: Mine, Take it and My board use it
export const myOwner = () => (S.me?.owner || "").trim();
export const owners = () => (S.meta?.owners || []).filter(Boolean);

export function params() { const q = location.hash.split("?")[1] || ""; return Object.fromEntries(new URLSearchParams(q)); }
export function link(view, p = {}) { const q = new URLSearchParams(Object.fromEntries(Object.entries(p).filter(([, v]) => v !== undefined && v !== null && v !== ""))).toString(); return `#/${view}${q ? "?" + q : ""}`; }
export function go(view, p = {}) { location.hash = link(view, p); }

export function friendlyError(e) {
  const c = e?.code || "";
  if (c.includes("permission-denied")) return "Not allowed for the person you are viewing as.";
  if (c.includes("not-found")) return "That record is not there any more.";
  if (c.includes("unimplemented")) return "This copy of the site runs on a snapshot and cannot do that.";
  return e?.message || String(e);
}

// a modal with a title, body html and buttons; returns the overlay element
let modalSeq = 0;
export function modal(title, bodyHtml, { wide = false, buttons = "" } = {}) {
  const bg = document.createElement("div"); bg.className = "modal";
  const hid = `mh-${++modalSeq}`;
  bg.innerHTML = `<div class="modal-box${wide ? " wide" : ""}" role="dialog" aria-modal="true" aria-labelledby="${hid}"><div class="modal-head"><h2 id="${hid}">${esc(title)}</h2><button type="button" class="xbtn" data-close aria-label="Close">&times;</button></div><div class="modal-body">${bodyHtml}</div><div class="modal-foot">${buttons || '<button type="button" class="btn" data-close>Close</button>'}</div></div>`;
  document.body.appendChild(bg);
  const opened = Date.now();   // the real clock on purpose: this measures a double click, not a date
  let closed = false;
  const close = () => { if (closed) return; closed = true; bg.remove(); document.removeEventListener("keydown", onKey); bg.dispatchEvent(new Event("closed")); };
  const onKey = ev => { if (ev.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);
  bg.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", close));
  // a click on the backdrop closes the box, but not the second click of a double click that opened it
  bg.addEventListener("mousedown", ev => { if (ev.target === bg && Date.now() - opened > 500) close(); });
  bg.close = close;
  return bg;
}
// resolves true for the OK button and false for any other way out (Cancel, the cross, Escape, the backdrop)
export function confirmBox(title, text, okLabel = "Yes") {
  return new Promise(res => {
    const bg = modal(title, `<p class="m0">${esc(text)}</p>`, { buttons: `<button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="cf-ok">${esc(okLabel)}</button>` });
    bg.addEventListener("closed", () => res(false));
    bg.querySelector("#cf-ok").onclick = () => { res(true); bg.close(); };
    setTimeout(() => bg.querySelector("#cf-ok")?.focus(), 0);
  });
}

// ---------------------------------------------------------------- the line that says how fresh the data is
// OppList pushes every few minutes while it is open. Older than STALE_MIN: the computer it runs on is
// probably off; what people do here is kept and reaches OppList at its next sync.
const STALE_MIN = 20;
export function syncChip() {
  const at = asDate(S.meta?.pushedAt);
  if (!at) return `<span class="sync stale" title="No update yet">Waiting for the first update</span>`;
  const mins = (nowMs() - at.getTime()) / 60000;
  if (mins <= STALE_MIN) return `<span class="sync" title="The lists update every few minutes">Updated · ${esc(stamp(at))}</span>`;
  return `<span class="sync stale" title="No update since then. What you save here is kept and goes into the lists at the next update.">Last update · ${esc(stamp(at))}</span>`;
}
export const pageHead = (title, sub, right = "", sync = true) => `<div class="page-head"><div class="ph-text"><h1>${esc(title)}</h1>${sub ? `<p>${sub}</p>` : ""}</div><div class="ph-right">${right}${sync ? syncChip() : ""}</div></div>`;
