// Problem reports. Anyone signed in can report a problem (the sidebar button), and the app logs the failures it
// catches itself: errors on a page, saves and loads that fail, job sheets that are not accepted. Each one gets a short
// reference, R-XXXXX for a report and E-XXXXX for a failure the app caught, kept in issues/{ref} with the details.
// The email to the administrator is written in the person's own Outlook (a mailto link; the site sends nothing) and
// carries the reference and a link to the record on the Issues page.
import { doc, setDoc, updateDoc, collection, query, where, getCountFromServer, serverTimestamp } from "./store/firestore.js";
import { db, S, modal, friendlyError, params, APP_VERSION } from "./core.js";
import { support } from "./config.js";
import { esc, nowMs } from "./lib.js";

const ALPHA = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";   // no 0/O or 1/I/L, so a reference is easy to read out and type
export function newRef(prefix) { const a = new Uint32Array(5); crypto.getRandomValues(a); return `${prefix}-${[...a].map(x => ALPHA[x % ALPHA.length]).join("")}`; }
// what someone types into a search box: e7k3q9, E 7K3Q9 and E-7K3Q9 are the same reference
export function normRef(s) { const t = String(s || "").toUpperCase().replace(/[^0-9A-Z]/g, ""); return /^[ER][0-9A-Z]{5}$/.test(t) ? `${t[0]}-${t.slice(1)}` : null; }

export const KIND = { report: "Report", error: "Error", upload: "Upload not accepted" };
const PAGE = { assign: "Assign List", board: "My board", inquiries: "Inquiries", case: "Case", followups: "Follow-ups", report: "Monthly report", issues: "Issues", users: "Users" };

// the app's messages as plain text (some carry <br> and escaped names)
export const plain = s => String(s ?? "").replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]*>/g, "").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&").trim();
export const cut = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
export const issueLink = ref => `${location.origin}${location.pathname}#/issues?ref=${ref}`;

// where the person is: the page and, when known, the part of it
function place({ part } = {}) {
  const view = S.view || location.hash.replace(/^#\/?/, "").split("?")[0];
  const p = params();
  const label = PAGE[view] || view || "Home";
  return { page: cut(view || "", 40), hash: (location.hash || (view ? `#/${view}` : "")).slice(0, 300), where: cut([label, part || p.opp || p.tab || ""].filter(Boolean).join(", "), 300) };
}
const client = () => ({ app: APP_VERSION, ua: navigator.userAgent.slice(0, 300), screen: `${screen.width}x${screen.height}`, window: `${innerWidth}x${innerHeight}`, online: navigator.onLine });
const base = (kind, at) => ({ kind, by: S.me.email, name: S.me.name || "", role: S.me.role || "", ...place(at), client: client(), status: "open" });

async function create(rec) {
  try { await setDoc(doc(db, "issues", rec.ref), { ...rec, at: serverTimestamp(), ...(rec.reported ? { reportedAt: serverTimestamp() } : {}) }); return true; }
  catch (e) { console.warn("problem record not saved", e); return false; }
}

// ---------------------------------------------------------------- failures the app catches
const logged = new Map();   // ref -> { rec, saved }: the failures logged since the page was opened, for their Report this
const bySig = new Map();    // person and signature -> ref: the same failure on the same page is logged once per person
let autoCount = 0;
const AUTO_MAX = 20;        // a page caught in a loop of errors stops logging after this many

// logs a failure (kind error, or upload for a job sheet not accepted) and returns its reference; null when not signed in
export function logProblem(err, { kind = "error", part, file } = {}) {
  if (!S.me?.email) return null;
  const message = cut(plain(err?.message ?? err), 2000), code = cut(err?.code || "", 100);
  const rec = { ...base(kind, { part }), source: "auto", reported: false };
  const sig = cut(`${kind}|${rec.page}|${code}|${message}`, 300);
  const key = `${S.me.email}|${sig}`;   // someone else signing in on the same tab gets their own record
  if (bySig.has(key)) return bySig.get(key);
  if (autoCount >= AUTO_MAX) return null;
  autoCount++;
  Object.assign(rec, { ref: newRef("E"), title: cut(kind === "error" ? friendlyError(err) : message, 300), message, code, stack: cut(err?.stack || "", 6000), sig, note: "", ...(file ? { file: cut(file, 300) } : {}) });
  bySig.set(key, rec.ref);
  logged.set(rec.ref, { rec, saved: create(rec) });
  return rec.ref;
}

// the line under a failure message: people get Report this; the administrator gets a link to the record
export function refLine(ref, { lead = "", label = "Report this" } = {}) {
  if (!ref) return "";
  if (S.me?.role === "admin") return `<span class="pref">Ref <a href="#/issues?ref=${ref}">${ref}</a></span>`;
  return `<span class="pref">Ref ${ref} &middot; ${lead}<a href="#" data-report="${ref}">${label}</a></span>`;
}
// a failure shown in the page: the friendly message, logged, with its reference
export function problemHtml(err, at) { const ref = logProblem(err, at); return `<span class="err">${esc(friendlyError(err))}</span>${refLine(ref)}`; }
// a failure in a form: a message the app wrote itself (a plain Error, such as "Enter the person's name.") is shown as it
// is; anything else (the database refused, a bug) is logged with its reference
export function failHtml(err, at) { return err && err.constructor === Error && !err.code ? `<span class="err">${esc(err.message)}</span>` : problemHtml(err, at); }
// a failure shown in a box
export function showProblem(err, at) {
  const ref = logProblem(err, at), admin = S.me?.role === "admin";
  const bg = modal("Something went wrong", `<p style="margin:0 0 8px">${esc(friendlyError(err))}</p>${ref ? `<div class="muted">Reference ${admin ? `<a href="#/issues?ref=${ref}">${ref}</a>` : ref}</div>` : ""}`,
    { buttons: `<button type="button" class="btn" data-close>Close</button>${ref && !admin ? `<button type="button" class="btn primary" data-report="${ref}">Report this</button>` : ""}` });
  bg.querySelectorAll("[data-report], a[href^='#/issues']").forEach(x => x.addEventListener("click", () => bg.close()));
}

// errors nobody caught (a bug in a page): logged, with a small notice so the person can report what they were doing
export function initProblems() {
  window.addEventListener("error", ev => {
    const m = String(ev.message || "");
    if (ev.filename && !ev.filename.startsWith(location.origin)) return;   // browser extensions, other sites
    if (!ev.error && (m === "Script error." || /ResizeObserver loop/i.test(m))) return;
    notice(logProblem(ev.error || new Error(m)));
  });
  window.addEventListener("unhandledrejection", ev => notice(logProblem(ev.reason ?? new Error("Unhandled promise rejection"))));
  document.addEventListener("click", ev => {
    const a = ev.target.closest?.("[data-report]"); if (!a) return;
    ev.preventDefault(); a.closest("#pnote")?.remove(); reportDialog(a.dataset.report);
  });
  window.addEventListener("hashchange", () => document.getElementById("pnote")?.remove());
}
function notice(ref) {
  if (!ref) return;
  document.getElementById("pnote")?.remove();
  const el = document.createElement("div"); el.id = "pnote"; el.className = "pnote no-print"; el.setAttribute("role", "alert");
  el.innerHTML = `<div><b>Something went wrong on this page.</b>${refLine(ref)}</div><button type="button" class="x" aria-label="Close">&times;</button>`;
  el.querySelector(".x").onclick = () => el.remove();
  document.body.appendChild(el);
}

// ---------------------------------------------------------------- the Report a problem box and the email
export function wireReportButton() {
  const b = document.getElementById("btn-report"); if (!b) return;
  b.onclick = () => reportDialog();
}

function emailParts(rec, text) {
  const subject = `[OppList] ${rec.ref}: ${cut(rec.kind === "report" ? text.split("\n")[0] || "Problem report" : rec.title, 80)}`;
  const lines = [text || (rec.kind === "upload" ? "The site did not take my file." : "Something went wrong on the OppList site."), "",
    `Reference: ${rec.ref}`, `Page: ${rec.where}`];
  if (rec.kind !== "report") lines.push(`Problem: ${cut(rec.title, 400)}`);
  if (rec.file) lines.push(`File: ${rec.file}`);
  lines.push("", `Details in the system: ${issueLink(rec.ref)}`);
  return { subject, body: lines.join("\r\n") };
}
const emailText = (rec, text) => { const { subject, body } = emailParts(rec, text.trim()); return `To: ${support.email}\r\nSubject: ${subject}\r\n\r\n${body}`; };
export async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = document.createElement("textarea"); ta.value = text; ta.style.cssText = "position:fixed;opacity:0"; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (__) { /* nothing more to try */ }
    ta.remove();
  }
  if (btn) { const t = btn.textContent; btn.textContent = "Copied"; setTimeout(() => { btn.textContent = t; }, 1500); }
}

// from the sidebar (a new report, R-) or from a failure's Report this (that failure's reference)
export function reportDialog(ref) {
  if (!S.me) return;
  const fromErr = ref ? logged.get(ref) : null;
  const rec = fromErr ? fromErr.rec : { ...base("report"), source: "user", reported: true, ref: newRef("R"), title: "", message: "", code: "", stack: "", note: "" };
  if (!fromErr) rec.sig = `report|${rec.ref}`;
  const bg = modal("Report an issue", `<div class="rp">
      <div class="rp-ref">Reference <b>${rec.ref}</b></div>
      ${fromErr ? `<div class="rp-prob"><div class="lb">${rec.kind === "upload" ? "File not accepted" : "The problem"}</div>${esc(rec.title)}</div>` : ""}
      <div class="field"><label for="rp-note">${fromErr ? "What were you doing? (optional)" : "What happened?"}</label>
        <textarea id="rp-note" rows="5" maxlength="4000" placeholder="${fromErr ? "For example: logging a follow-up on OPP-1201." : "What were you doing, and what did you expect to see?"}"></textarea></div>
      <div class="rp-meta">Page: ${esc(rec.where)}</div>
      <div class="rp-tip">The report is saved when you press Copy details, and ${esc(support.name)} sees it under Issues. An installation also opens an email with the details filled in; this demo sends no mail.</div>
      <div class="err" id="rp-err"></div></div>`,
    { buttons: `<button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary" id="rp-copy">Copy details</button>` });
  
  const note = bg.querySelector("#rp-note"), err = bg.querySelector("#rp-err");
  note.addEventListener("input", () => { err.textContent = ""; });
  setTimeout(() => note.focus(), 0);
  // Copy details saves the report too (the tip above says either button does), then shows the reference
  bg.querySelector("#rp-copy").onclick = async ev => {
    const text = note.value.trim();
    if (!fromErr && !text) { err.textContent = "Describe the problem first."; note.focus(); return; }
    await copyText(emailText(rec, text), ev.currentTarget);
    sent(bg, rec, text, saveReport(rec, text, fromErr), true);
  };
}
async function saveReport(rec, text, fromErr) {
  try {
    if (fromErr && await fromErr.saved) await updateDoc(doc(db, "issues", rec.ref), { reported: true, note: text, reportedAt: serverTimestamp() });
    else {
      Object.assign(rec, { reported: true, note: text }, fromErr ? {} : { title: cut(text.split("\n")[0], 300) });
      if (!await create(rec)) return false;
      if (fromErr) fromErr.saved = Promise.resolve(true);
    }
    rec.reported = true; rec.note = text;
    return true;
  } catch (e) { console.warn("report not saved", e); return false; }
}
function sent(bg, rec, text, saving, copied = false) {
  const body = bg.querySelector(".modal-body"), row = bg.querySelector(".modal-foot"); if (!body) return;
  body.innerHTML = `<div class="rp"><p class="rp-ok">${copied ? `The details are copied. ${esc(support.name)} sees the report under Issues; paste the details into an email or a chat if you want to add a screenshot.` : "The report is saved."}</p>
    <div class="rp-ref">Reference <b>${rec.ref}</b> <span id="rp-saved">saving&hellip;</span></div>
</div>`;
  row.innerHTML = `<button type="button" class="btn" id="rp-copy2">Copy details</button><button type="button" class="btn primary" data-close>Close</button>`;
  row.querySelector("[data-close]").onclick = () => bg.close();
  row.querySelector("#rp-copy2").onclick = ev => copyText(emailText(rec, text), ev.currentTarget);
  Promise.race([saving, new Promise(r => setTimeout(() => r(null), 8000))]).then(ok => {
    const s = bg.querySelector("#rp-saved"); if (!s) return;
    s.textContent = ok ? "saved with the details" : ok === null ? "not saved yet (no connection?); the email has the details" : "could not be saved in the system; the email has the details";
    s.className = ok ? "ok" : "warn";
  });
}

// ---------------------------------------------------------------- the open count (admin: sidebar badge, Overview)
let count = { n: null, at: 0 };
export async function issueCount(fresh = false) {
  if (S.me?.role !== "admin") return null;
  if (fresh || count.n === null || nowMs() - count.at > 60000) {
    try { const c = await getCountFromServer(query(collection(db, "issues"), where("status", "==", "open"))); count = { n: c.data().count, at: nowMs() }; }
    catch (e) { console.warn("issue count", e); }
  }
  paintBadge();
  return count.n;
}
export function paintBadge() {
  const el = document.getElementById("issuebadge"); if (!el) return;
  const n = count.n || 0; el.textContent = n > 99 ? "99+" : String(n); el.hidden = !n;
}
