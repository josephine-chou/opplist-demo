// OppList team site: the shell (side menu, routing, who the site is viewed as) and the hand-off to the page
// modules in views/. Plain ES modules, no build step. OppList stays the master: it pushes the lists here and
// takes in what people do here. This copy runs on a snapshot of invented data (store/), with no account.
import { signInWithEmailAndPassword, signOut, onAuthStateChanged, people } from "./store/auth.js";
import { doc, getDoc, updateDoc, serverTimestamp, changeCount, resetStore } from "./store/firestore.js";
import { auth, db, S, $, main, booted, go, params, friendlyError, isAdmin, canReport, canCosts, myOwner, confirmBox } from "./core.js";
import { support, COMPANY, APP_NAME } from "./config.js";
import { startLive, stopLive, onData, onLoadError, merged } from "./data.js";
import { initProblems, problemHtml, wireReportButton, issueCount, paintBadge, logProblem } from "./issues.js";
import { esc, todayIso } from "./lib.js";
import { initSearch, showSearch, refreshSearch } from "./search.js";
import { viewAssign } from "./views/assign.js";
import { viewBoard, boardCount } from "./views/board.js";
import { viewInquiries } from "./views/inquiries.js";
import { viewCase } from "./views/case.js";
import { viewFollowups } from "./views/followups.js";
import { viewReport } from "./views/report.js";
import { viewIssues } from "./views/issues.js";
import { viewUsers } from "./views/users.js";

const ICON = {
  assign: "M3 11.5L5 4.5h10l2 7v4H3z M3 11.5h4.5l1 2h3l1-2H17",
  board: "M7.5 3.5h5v2h-5z M6 4.5H4.5v12h11v-12H14 M7.5 11l2 2 3.5-4",
  inquiries: "M8.5 3.5a5 5 0 1 0 0 10a5 5 0 1 0 0-10z M12.2 12.2l4.3 4.3",
  followups: "M10 3.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13z M10 6.5V10l2.5 2",
  report: "M4 16.5v-6 M8.5 16.5v-10 M13 16.5v-4 M17 16.5V7 M3 16.5h15",
  issues: "M10 3.5l7.5 13h-15z M10 8.5v3.5 M10 14.4v.1",
  users: "M7.5 9a3 3 0 1 0 0-6a3 3 0 1 0 0 6z M2.5 16.5c0-2.8 2.2-5 5-5s5 2.2 5 5 M13 9.2a2.6 2.6 0 1 0 0-5.2 M14.5 11.8c1.9.5 3 2.2 3 4.7",
  flag: "M5 17V3.5 M5 3.5h10l-2 3.5 2 3.5H5",
  shut: "M12 5l-5 5 5 5 M16 5v10",
  open: "M8 5l5 5-5 5 M4 5v10",
};
// [route, label, who may open it]
const PAGES = [["assign", "Assign List", "all"], ["board", "My board", "all"], ["inquiries", "Inquiries", "all"], ["followups", "Follow-ups", "all"],
  ["report", "Monthly report", "report"]];
const ADMIN_PAGES = [["issues", "Issues"], ["users", "Users"]];
const VIEWS = { assign: viewAssign, board: viewBoard, inquiries: viewInquiries, case: viewCase, followups: viewFollowups, report: viewReport, issues: viewIssues, users: viewUsers };
const allowed = () => ["assign", "board", "inquiries", "case", "followups", ...(canReport() ? ["report"] : []), ...(isAdmin() ? ["issues", "users"] : [])];
const svg = (d, size = 18) => `<svg width="${size}" height="${size}" viewBox="0 0 20 20" aria-hidden="true"><path d="${d}" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
// the company's name as plain type with one rule under it: the only "logo" this copy has
const wordmark = (cls = "") => `<span class="wordmark ${cls}">${esc(COMPANY)}</span>`;
// what a person may see, in the words of the switch
const roleLine = p => p.role === "admin" ? "Administrator: every page, costs included" : p.costs ? "Member with Costs: sees costs and margins" : "Member: sells, sees no costs";

if (!booted.ok) {
  main.innerHTML = `<div class="signin"><div class="si-box"><div class="si-head">${wordmark("dark")}<div class="si-name">${esc(APP_NAME)}</div></div>
    <div class="si-body"><h1>No demo data</h1><p class="muted">${esc(booted.reason)}</p>
    <p class="muted">Run <code>python -m opplist demo --out demo</code> in the repository, then open the site through <code>run_demo</code>.</p></div></div></div>`;
} else {
  initProblems();   // errors nobody caught are logged, with a notice so the person can report them
  initSearch();     // the search box at the top right of every page (search.js)
  initSwitch();     // "Viewing as", beside it
  onLoadError((e, what) => { console.error(what, e); if (S.me) logProblem(e, { part: `loading ${what}` }); });
  onAuthStateChanged(auth, onPerson);
  window.addEventListener("hashchange", () => { if (S.me) route(); });
  onData(onChange);
  // the day changes while the page is open (Days waiting count on): repaint after midnight
  let lastDay = todayIso();
  setInterval(() => { if (todayIso() !== lastDay) { lastDay = todayIso(); if (S.me && handle?.refresh) handle.refresh(new Set(["day"])); } }, 60000);
  // read by the browser tests
  window.__opplist = { S, merged };
}

// ---------------------------------------------------------------- who the site is viewed as
let handle = null;   // the open page: { refresh(what), leave() }
async function onPerson(user) {
  leavePage(); stopLive();
  S.user = user; S.me = null; S.view = null;
  paintSwitch();
  if (!user) { showShell(false); viewChoose(); return; }
  const email = (user.email || "").toLowerCase();
  let me = null;
  try { const snap = await getDoc(doc(db, "users", email)); if (snap.exists()) me = snap.data(); }
  catch (e) { if (auth.currentUser === user) { showShell(false); viewChoose(`${email}: ${friendlyError(e)}`); } return; }
  if (auth.currentUser !== user) return;   // someone else was chosen while this was loading
  if (!me || me.active === false) { showShell(false); viewChoose(`${me?.name || email} is switched off under Users. Pick someone else, or reset the demo.`); return; }
  S.me = { ...me, email };
  updateDoc(doc(db, "users", email), { lastLogin: serverTimestamp() }).catch(e => console.warn("lastLogin", e));
  startLive();
  showShell(true);
  paintSwitch();
  route();
  if (isAdmin()) issueCount(true);
}
function onChange(what) {
  if (!S.me) return;
  if (what.has("me")) {
    // switched off or removed while viewing: out; a page they may no longer open (Monthly report): the Assign List
    if (!S.meDoc || S.meDoc.active === false) { signOut(auth); return; }
    if (S.view && !allowed().includes(S.view)) { go("assign"); return; }
    paintSwitch();
  }
  renderNav(S.view);
  refreshSearch();
  if (handle?.refresh) { try { handle.refresh(what); } catch (e) { console.error(e); logProblem(e, { part: "refresh" }); } }
}
const viewAs = email => signInWithEmailAndPassword(auth, email, "").catch(e => viewChoose(friendlyError(e)));

// the switch in the top bar: always visible, so it is clear that nobody is signed in and whose view this is
function initSwitch() {
  const bar = $("#topbar .tb-in");
  const box = document.createElement("div"); box.className = "va"; box.id = "va";
  bar.prepend(box);
}
function paintSwitch() {
  const box = $("#va"); if (!box) return;
  const cur = auth.currentUser?.email || "";
  box.innerHTML = `<span class="va-tag" title="Invented data. Nothing you do here leaves this browser.">Demo</span>
    <label for="va-who">Viewing as</label>
    <select id="va-who" title="${esc(S.me ? roleLine({ role: S.me.role, costs: S.me.costs }) : "")}">${people().map(p => `<option value="${esc(p.email)}"${p.email === cur ? " selected" : ""}>${esc(p.name)} · ${p.role === "admin" ? "admin" : p.costs ? "with Costs" : "no Costs"}</option>`).join("")}</select>`;
  $("#va-who").onchange = ev => viewAs(ev.target.value);
}
// the page in place of a sign-in page: pick a person
function viewChoose(note = "") {
  main.innerHTML = `<div class="signin"><div class="si-box wide"><div class="si-head">${wordmark("dark")}<h1 class="si-name">${esc(APP_NAME)}</h1><p class="si-sub">A demo on invented data. Choose whose view to open.</p></div>
    <div class="si-body">${note ? `<div class="err" role="alert">${esc(note)}</div>` : ""}
      ${people().map(p => `<button type="button" class="who" data-who="${esc(p.email)}"><b>${esc(p.name)}</b><span>${esc(roleLine(p))}</span></button>`).join("")}
      <p class="si-help">There are no accounts and no passwords here. Everything stays in this browser. <button type="button" class="linkbtn" id="b-reset">Reset demo</button></p>
    </div></div></div>`;
  main.querySelectorAll("[data-who]").forEach(b => b.onclick = () => viewAs(b.dataset.who));
  $("#b-reset").onclick = resetDemo;
}
async function resetDemo() {
  const n = changeCount();
  if (!await confirmBox("Reset demo", n ? "Everything you did on this site is forgotten and the pages go back to the snapshot." : "Nothing has been changed yet. Reload the snapshot anyway?", "Reset")) return;
  resetStore(); location.hash = ""; location.reload();
}

// ---------------------------------------------------------------- the side menu
const SHUT_KEY = "opplist.side.shut";
function shutPref() { try { const v = localStorage.getItem(SHUT_KEY); return v === null ? innerWidth < 900 : v === "1"; } catch (_) { return innerWidth < 900; } }
function setShut(v) { try { localStorage.setItem(SHUT_KEY, v ? "1" : "0"); } catch (_) { /* private window: not remembered */ } }
function showShell(on) {
  const side = $("#side"); side.hidden = !on;
  document.body.classList.toggle("signed-in", on);
  showSearch(on);
  $("#topbar").hidden = false;   // the switch stays on show when nobody is chosen
  if (!on) { side.innerHTML = ""; return; }
  side.classList.toggle("shut", shutPref());
}
function badges() {
  const m = S.inquiries || S.assign ? merged() : null;
  return {
    assign: m ? m.waiting.filter(r => !r.owner && !r.wait).length : 0,   // not the ones waiting for the requester
    board: boardCount(),
  };
}
function renderNav(cur) {
  const side = $("#side"); if (!S.me || side.hidden) return;
  const shut = side.classList.contains("shut"), b = badges();
  const item = (route, label, badge, title = label) => `<a class="nv${route === cur || (cur === "case" && route === "inquiries") ? " on" : ""}" href="#/${route}"${route === cur ? ' aria-current="page"' : ""} title="${esc(title)}">${svg(ICON[route], shut ? 20 : 18)}<span class="lb">${esc(label)}</span>${badge ? `<span class="nb">${badge > 99 ? "99+" : badge}</span>` : ""}</a>`;
  const pages = PAGES.filter(p => p[2] === "all" || (p[2] === "report" && canReport()));
  const name = S.me.name || S.me.email, n = changeCount();
  side.innerHTML = `<div class="side-in">
    ${shut ? `<button type="button" class="nv tog" id="btn-side" aria-label="Open the menu" title="Open the menu">${svg(ICON.open, 18)}</button>`
      : `<div class="brand"><a class="brand-app" href="#/assign" title="${esc(APP_NAME)} home (Assign List)">${wordmark()}<span>${esc(APP_NAME)}</span></a></div>`}
    ${pages.map(([r, l]) => item(r, l, b[r] || 0)).join("")}
    ${isAdmin() ? `<div class="nv-sec">ADMIN</div>${ADMIN_PAGES.map(([r, l]) => r === "issues"
      ? `<a class="nv${cur === r ? " on" : ""}" href="#/${r}"${cur === r ? ' aria-current="page"' : ""} title="${l}">${svg(ICON[r], shut ? 20 : 18)}<span class="lb">${l}</span><span class="nb" id="issuebadge" hidden></span></a>`
      : item(r, l, 0)).join("")}` : ""}
    <div class="side-user">${shut ? `<span class="avatar" title="${esc(name)}">${esc(name.charAt(0).toUpperCase())}</span>`
      : `<span class="nm">${esc(name)}</span><span class="rl">${isAdmin() ? "Admin" : canCosts() ? "Member · Costs" : "Member"}${myOwner() && !name.toLowerCase().startsWith(myOwner().toLowerCase()) ? ` · ${esc(myOwner())}` : ""}${myOwner() ? "" : " · not linked"}</span>
      <button type="button" class="lk" id="btn-out">Switch person</button>
      <button type="button" class="lk" id="btn-reset" title="Forget what was done on this site and go back to the snapshot">Reset demo${n ? ` (${n} saved)` : ""}</button>`}</div>
    ${isAdmin() ? "" : `<button type="button" class="nv sub" id="btn-report" title="Report an issue">${svg(ICON.flag, shut ? 20 : 18)}<span class="lb">Report an issue</span></button>`}
    ${shut ? "" : `<button type="button" class="nv sub" id="btn-side" aria-label="Collapse the menu" title="Collapse the menu">${svg(ICON.shut, 18)}<span class="lb">Collapse</span></button>`}
  </div>`;
  $("#btn-side").onclick = () => { side.classList.toggle("shut"); setShut(side.classList.contains("shut")); renderNav(S.view); };
  const out = $("#btn-out"); if (out) out.onclick = () => signOut(auth);
  const reset = $("#btn-reset"); if (reset) reset.onclick = resetDemo;
  wireReportButton();
  if (isAdmin()) paintBadge();
}

// ---------------------------------------------------------------- routing
let navSeq = 0;
function leavePage() { navSeq++; if (handle?.leave) { try { handle.leave(); } catch (e) { console.warn(e); } } handle = null; }
async function route() {
  const ok = allowed();
  const view = location.hash.replace(/^#\/?/, "").split("?")[0] || "assign";
  if (!ok.includes(view)) { go("assign"); return; }
  leavePage();
  document.querySelectorAll(".modal").forEach(m => m.close?.());
  S.view = view; renderNav(view);
  const seq = ++navSeq;   // a page that finishes loading after the person has moved on must not overwrite the new page
  main.scrollTop = 0; window.scrollTo(0, 0);
  try { const h = await VIEWS[view](params(), () => seq === navSeq); if (seq === navSeq) handle = h || null; else h?.leave?.(); }
  catch (e) { if (seq !== navSeq) return; console.error(e); main.innerHTML = `<div class="page"><div class="note-box">Could not load this page: ${problemHtml(e)}</div></div>`; }
}
