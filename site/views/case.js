// The case page: what OppList holds on one inquiry (read only here) and the four things people log on the web:
// Log quote sent, Log follow-up, Waiting on us, Add note; plus Handled by (Take it, Change) and Answered; and
// Ask vendors (views/ask.js; a drawer from the right), opened from Rate requests once OppList
// pushes its vendor list; and Quote to requester (views/quote.js, the same kind of drawer), the first
// button of Update this case once OppList pushes its quote rules, with the case's Quotes listed under Rate requests.
// Edit on the Cargo and Route panels (views/edit.js, the same kind of drawer) once OppList pushes ed for
// the case; the panels show OppList's lines with the edits not taken in yet laid over them. Only one drawer is open at
// a time.
// Each vendor's rates for this shipment are typed from its Rate requests row (views/rates.js,
// the same kind of drawer); the row shows "Rates saved" and valid to per route, and Lowest; no total and no
// "not said" on the row.
// A vendor's reply read with Read quote on the computer OppList runs on comes as a draft (rateDrafts):
// its row offers "Read from their mail: check", which opens the drawer with it; OppList's link opens it straight away.
// Read quote on a vendor's
// row (views/readq.js, the same kind of drawer): anyone sends their mail, files or a pasted message to be read;
// the row says it waits (or is paused until the next update), could not be read, or offers the draft as above.
import { S, $, main, myOwner, go, confirmBox, isAdmin, canCosts } from "../core.js";
import { esc, day, todayIso, validIso, daysSince, plural, stamp, cut, asDate, nowMs } from "../lib.js";
import { merged, mergedCase, act, addNote, watchCase, watchCaseNotes, watchQuotes, watchQuoteCosts, watchVendorRates, watchRateDrafts, watchReads, watchReadResults, actionLabel } from "../data.js";
import { showProblem, failHtml, problemHtml } from "../issues.js";
import { statusChip, unsynced, ownerCell, ownerChip, wireOwnerCells, ownerOptions, shortWhat, tagChips, shownStatus, COPY_ICON } from "./common.js";
import { nextCell } from "./inquiries.js";
import { askPanel } from "./ask.js";
import { quotePanel, showQuote } from "./quote.js";
import { money, sourcesText } from "../quotemail.js";
import { closeForm, closeLabel, reasons, showEvidence } from "./close.js";
import { editPanel, editable } from "./edit.js";
import { ratesPanel, canRates } from "./rates.js";
import { readPanel } from "./readq.js";
import { comparePanel } from "./compare.js";
import { cargoCells, routeCells, editText, letter } from "../editlib.js";
import * as R from "../ratelib.js";

const DIRECTION = { "Cross-trade": "Triangle" };
const HOW = ["Email", "Phone", "WhatsApp", "Teams", "Other"];
const OPEN = st => st === "New" || st === "Quoted";

export async function viewCase(p) {
  const opp = String(p.opp || "").toUpperCase();
  if (!/^OPP-\d{4,6}$/.test(opp)) { go("inquiries"); return null; }
  main.innerHTML = `<div class="page" id="pg-case">
    <div class="case-top" id="c-top">
      <nav class="crumb" aria-label="Breadcrumb"><a href="#/inquiries">Inquiries</a><span aria-hidden="true">/</span><span>${esc(opp)}</span></nav>
      <div id="c-head"><div class="muted loading">Loading…</div></div><div id="c-next"></div></div><div id="c-pend"></div>
    <div class="cols">
      <div class="col-main"><div id="c-main" class="contents"></div><div id="c-tl" class="contents"></div></div>
      <aside class="col-side">
        <section class="panel act-panel" aria-labelledby="h-act"><div class="panel-head"><h2 id="h-act">Update this case</h2></div>
          <div class="act-grid" id="c-btns"></div><p class="hint small" id="c-lock"></p><div id="c-flash" role="status"></div><div id="c-panel"></div></section>
        <section class="panel" aria-labelledby="h-notes"><div class="panel-head"><h2 id="h-notes">Notes</h2></div><div id="c-notes"><div class="empty muted">Loading…</div></div></section>
      </aside>
    </div></div>`;
  const root = $("#pg-case");
  // the case's head (number, requester, Handled by, Next step) stays at the top while the page scrolls
  const top = $("#c-top"), onScroll = () => top.classList.toggle("stuck", window.scrollY > 2);
  window.addEventListener("scroll", onScroll, { passive: true }); onScroll();
  let doc0 = undefined, notes = [], panel = "", flashT = 0, doTimer = 0, quotes = [], qcosts = new Map(), quotesErr = null, vrates = [], vratesErr = null;
  let drafts = [], draftsIn = false, vratesIn = false, rqTimer = 0, rqReads = [], readRes = new Map();
  const unsub = [
    watchCase(opp, d => { doc0 = d; draw(); }, e => { $("#c-main").innerHTML = `<div class="note-box">Could not load ${esc(opp)}: ${problemHtml(e, { part: opp })}</div>`; }),
    watchCaseNotes(opp, list => { notes = list; drawNotes(); }, e => { $("#c-notes").innerHTML = `<div class="empty">${problemHtml(e, { part: `${opp} notes` })}</div>`; }),
    // the quotes made with the card (everyone), their costs (admin and Costs only)
    watchQuotes(opp, list => { quotes = list; quotesErr = null; redrawQuotes(); }, e => { quotesErr = e; redrawQuotes(); }),
    ...(canCosts() ? [watchQuoteCosts(opp, m => { qcosts = m; redrawQuotes(); }, () => { qcosts = new Map(); })] : []),
    // the vendors' rates typed on the case (everyone)
    watchVendorRates(opp, list => { vrates = list; vratesErr = null; vratesIn = true; const c = doc0 !== undefined ? cur() : null; if (c) drawMain(c); }, e => { vratesErr = e; vratesIn = true; const c = doc0 !== undefined ? cur() : null; if (c) drawMain(c); }),
    // the Read quote drafts OppList wrote for the case (everyone)
    watchRateDrafts(opp, list => { drafts = list; draftsIn = true; const c = doc0 !== undefined ? cur() : null; if (c) drawMain(c); }, () => { drafts = []; draftsIn = true; }),
    // Read quote requests sent from the site, and OppList's answers to them (everyone)
    watchReads(opp, list => { rqReads = list; const c = doc0 !== undefined ? cur() : null; if (c) drawMain(c); }, () => { rqReads = []; }),
    watchReadResults(opp, m => { readRes = m; const c = doc0 !== undefined ? cur() : null; if (c) drawMain(c); }, () => { readRes = new Map(); }),
  ];
  // the Quotes panel follows its two lists without redrawing the rest of the page
  function redrawQuotes() {
    const c = doc0 !== undefined ? cur() : null, box = $("#c-quotes");
    if (!c) return;
    if (box) box.outerHTML = quotesHtml(); else drawMain(c);
  }
  // the case as it stands with the actions OppList has not taken in yet
  function cur() {
    if (doc0 && !doc0.gone) return mergedCase(doc0);
    if (doc0?.gone) return null;   // OppList no longer lists it (out of scope, registered in error)
    const r = merged().byOpp.get(opp);
    return r ? mergedCase({ ...r, cargo: [], routes: [], rr: [], tl: [], partial: true }) : null;
  }
  wireOwnerCells(root, () => cur());
  // Ask vendors: the drawer stays open across the case's live updates (it never redraws what is being typed)
  const ask = askPanel(opp, () => cur(), { onLogged: (what, n) => { drawMain(cur()); if (what === "logged") flash(`Logged ${plural(n, "rate request")}.`); } });
  const canAsk = c => !!(c && OPEN(c.st) && !c.office && S.vendors);
  // Quote to requester: once OppList pushes its quote rules, for a New or Quoted case of ours
  const qp = quotePanel(opp, () => cur(), { quotes: () => quotes, costsOf: id => qcosts.get(id) || null, rates: () => ratesNow(),
    // a reading waiting to be checked, offered under Rates from (Check it opens it in the rates drawer)
    drafts: () => [...draftsNow().values()], onCheck: id => { if (qp.isOpen()) qp.leave(); openDraft(id); },
    onDone: (what, n) => { const c = cur(); drawButtons(c); if (c) drawMain(c); if (what === "quoted") flash(`Quote ${n} saved and logged as sent.`); if (what === "logged") flash("Logged the quote."); } });
  const quoteOn = () => !!S.meta?.quoteRules;
  // Edit Cargo and Route; the panels redraw with the change laid over them at once
  const ep = editPanel(opp, () => cur(), { onDone: (what, msg) => { const c = cur(); if (c) drawMain(c); if (what === "saved" && msg) flash(msg); } });
  // not while a Won or Lost sent from the site waits for the administrator
  const canQuote = c => !!(c && OPEN(c.st) && !c.office && !c.partial && !c.pc && quoteOn());
  // Waiting for requester from the site, a New case of ours only,
  // offered once OppList takes them in (meta/sync.waitReq)
  const waitReqOn = () => S.meta?.waitReq === true;
  const canWaitReq = c => !!(c && waitReqOn() && c.st === "New" && !c.office && !c.partial && !c.pc);
  // a vendor's rates; each vendor's newest per route, not struck out in OppList (cases/{OPP}.rx)
  // the discards not taken in yet too (a draft's options skipped here)
  const discardsNow = () => (cur()?.waitingActs || []).filter(a => a.type === "discard").map(a => ({ draft: a.draft, option: a.option }));
  const ratesNow = () => ({ best: R.newest(vrates, cur()?.rx || []), docs: vrates, discards: discardsNow() });
  const rp = ratesPanel(opp, () => cur(), { rates: ratesNow, onDone: (what, msg) => { const c = cur(); if (c) drawMain(c); if ((what === "saved" || what === "discarded" || what === "progress") && msg) flash(msg); } });
  // Read quote from the site, while OppList says it reads them (meta/sync.readQuote.on)
  const rqp = readPanel(opp, () => cur(), { onDone: () => { const c = cur(); if (c) drawMain(c); } });
  // Compare rates, the vendors' rates side by side; a column
  // opens their rates, uses them in the quote card, or a vendor with none yet its reading or Add rates
  const cmp = comparePanel(opp, () => cur(), { rates: ratesNow, drafts: () => draftsNow(), canQuote: c => canQuote(c), canRates: c => canRates(c),
    onRates: (vendor, route, label) => { if (ask.isOpen()) ask.leave(); if (qp.isOpen()) qp.leave(); ep.leave(); rqp.leave(); rp.show(vendor, { route, label }); drawButtons(cur()); },
    onQuote: (vendor, label) => { if (ask.isOpen()) ask.leave(); ep.leave(); rp.leave(); rqp.leave(); qp.show("card", { use: vendor, label }); drawButtons(cur()); },
    onDraft: id => { openDraft(id); } });
  // the open Read quote drafts, less those saved or set aside here and not taken in by OppList yet
  const draftsNow = () => R.openDrafts(drafts, vrates, discardsNow(), S.meta?.options === true);
  function openDraft(id) {
    const draft = [...draftsNow().values()].find(d => d.id === id);
    if (!draft) { flash("That reading is not open any more: it was saved, set aside or replaced, or it is too old."); return false; }
    if (ask.isOpen()) ask.leave();
    if (qp.isOpen()) qp.leave();
    ep.leave(); rqp.leave(); cmp.leave();
    rp.show(draft.vendor, { route: typeof draft.route === "number" ? draft.route : undefined, draft });
    drawButtons(cur()); return true;
  }

  root.addEventListener("click", async ev => {
    // opening redraws the button as pressed; closing redraws it through onLogged
    if (ev.target.closest("[data-ask-open]")) { if (qp.isOpen()) qp.leave(); ep.leave(); rp.leave(); rqp.leave(); cmp.leave(); ask.toggle(); drawMain(cur()); drawButtons(cur()); return; }
    // Compare rates
    if (ev.target.closest("[data-cmp-open]")) {
      if (ask.isOpen()) { ask.leave(); drawMain(cur()); }
      if (qp.isOpen()) qp.leave();
      ep.leave(); rp.leave(); rqp.leave();
      cmp.show(); drawButtons(cur()); return;
    }
    if (ev.target.closest("[data-quote-open]")) {
      if (ask.isOpen()) ask.leave();
      if (ep.isOpen()) ep.leave();
      rp.leave(); rqp.leave(); cmp.leave();
      drawMain(cur()); qp.toggle(); drawButtons(cur()); return;
    }
    const ed = ev.target.closest("[data-ed]");
    if (ed) {
      const [part, id] = ed.dataset.ed.split(":");
      if (ask.isOpen()) ask.leave();
      if (qp.isOpen()) qp.leave();
      rp.leave(); rqp.leave(); cmp.leave();
      await ep.show(part, id === "new" ? null : Number(id));
      const c = cur(); drawButtons(c); if (c) drawMain(c);
      return;
    }
    // a Read quote draft on a vendor's row
    const rq = ev.target.closest("[data-rq]");
    if (rq) { openDraft(rq.dataset.rq); return; }
    // Read quote on a vendor's row (or Read quote again after one that could not be read)
    const rqb = ev.target.closest("[data-readq]");
    if (rqb) {
      if (rqb.disabled) return;
      if (ask.isOpen()) ask.leave();
      if (qp.isOpen()) qp.leave();
      ep.leave(); rp.leave(); cmp.leave();
      rqp.show(rqb.dataset.readq); drawButtons(cur()); return;
    }
    // a vendor's rates (its row's total, Add rates, or a vendor picked under the list)
    const rb = ev.target.closest("[data-rates]");
    if (rb) {
      if (ask.isOpen()) ask.leave();
      if (qp.isOpen()) qp.leave();
      ep.leave(); rqp.leave(); cmp.leave();
      rp.show(rb.dataset.rates, { route: rb.dataset.route ? Number(rb.dataset.route) : undefined, label: rb.dataset.label || "" });
      const c = cur(); drawButtons(c); return;
    }
    const qv = ev.target.closest("[data-quote-view]");
    if (qv) { const q = quotes.find(x => x.id === qv.dataset.quoteView); if (q) showQuote(q, canCosts() ? qcosts.get(q.id) || null : null); return; }
    if (ev.target.closest("[data-ev-open]")) { const c = cur(); if (c?.pc) showEvidence(c.pc.id, c.pc); return; }
    const b = ev.target.closest("[data-panel]");
    if (b && !b.disabled) { openPanel(panel === b.dataset.panel ? "" : b.dataset.panel); return; }
    if (ev.target.closest("[data-cancel]")) { openPanel(""); return; }
    // Info received ends the wait (OppList writes the note, as its own button does)
    const inf = ev.target.closest("[data-inforec]");
    if (inf) {
      if (!await confirmBox("Info received", `Mark that the requester sent what we needed for ${opp}? It waits for our quote again from today.`, "Info received")) return;
      inf.disabled = true;
      try { await act("inforec", opp, { date: todayIso() }); flash("Info received: it waits for our quote again."); if (panel === "waitreq") openPanel(""); }
      catch (e) { inf.disabled = false; showProblem(e, { part: opp }); }
      return;
    }
    const ans = ev.target.closest("[data-answered]");
    if (ans) {
      if (!await confirmBox("Answered", `Mark ${opp} as answered? It leaves Pending on us, and its follow-ups start again from today.`, "Answered")) return;
      ans.disabled = true;
      try { await act("answered", opp, { date: todayIso(), note: "" }); flash("Answered."); }
      catch (e) { ans.disabled = false; showProblem(e, { part: opp }); }
    }
  });

  // the list under the box offers at most 15 vendors, the ones that fit what is typed
  root.addEventListener("input", ev => {
    if (ev.target.id !== "rt-pick-v") return;
    const dl = $("#rt-pick-dl"), c = cur();
    if (dl && c) dl.innerHTML = R.pickList(S.vendors?.rows, c, ev.target.value).map(k => `<option value="${esc(k)}">`).join("");
  });
  // rates from a vendor the case has not asked, picked from OppList's vendor list
  root.addEventListener("submit", ev => {
    if (ev.target.id !== "rt-pick") return;
    ev.preventDefault();
    const typed = ev.target.querySelector("#rt-pick-v")?.value.trim().toLowerCase() || "";
    const key = (S.vendors?.rows || []).find(r => String(r.key || "").toLowerCase() === typed)?.key;
    const err = ev.target.querySelector("#rt-pick-err");
    if (!key) { if (err) err.textContent = "Pick a vendor from the list."; return; }
    if (ask.isOpen()) ask.leave();
    if (qp.isOpen()) qp.leave();
    ep.leave(); rqp.leave(); cmp.leave();
    rp.show(key);
  });

  function draw() {
    const c = cur();
    if (doc0 === undefined && !c) return;
    if (!c) {
      $("#c-head").innerHTML = `<div class="page-head"><div class="ph-text"><h1>${oppTitle()}</h1><p>This case is not in the lists${S.inquiries === undefined ? " yet (still loading)" : ""}. It may be out of scope, registered in error, or newer than the last update.</p></div></div>`;
      $("#c-main").innerHTML = ""; $("#c-tl").innerHTML = ""; $("#c-next").innerHTML = ""; $("#c-pend").innerHTML = ""; $("#c-panel").innerHTML = ""; panel = "";
      ask.leave(); qp.leave(); ep.leave(); rp.leave(); cmp.leave();
      drawButtons(null); drawNotes(); return;
    }
    const dir = DIRECTION[c.direction] || c.direction;
    $("#c-head").innerHTML = `<div class="page-head case-head"><div class="ph-text">
        <div class="title-row"><h1>${oppTitle()}</h1>${statusChip(c.st, c.legacy)}${pcChip(c)}${dir ? `<span class="chip owner">${esc(dir)}</span>` : ""}${c.mode ? `<span class="chip owner">${esc(c.mode)}</span>` : ""}${tagChips(c)}${unsynced(c)}</div>
        <p>${[c.req, c.commodity, c.lane, c.rcv ? `received ${day(c.rcv, true)}` : ""].filter(Boolean).map(esc).join(" · ")}</p></div>
      <div class="ph-right"><span class="muted small">Handled by</span>${c.office ? `<span class="muted">${esc(c.office)} office</span>` : OPEN(c.st) ? ownerCell(c) : ownerChip(c.owner)}</div></div>`;
    $("#c-next").innerHTML = `<p class="nextline"><span class="muted">Next step</span> ${nextCell(c)}${c.st === "Quoted" && c.since ? ` <span class="muted small">· clock from ${esc(day(c.since))}</span>` : ""}</p>`;
    const pd = c.pend;
    $("#c-pend").innerHTML = pd ? `<div class="banner bn-pend" role="status">
        <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M3.5 4.5h13v8.5H9l-3.5 3v-3h-2z M10 7v2.6 M10 11.3v.2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>
        <div class="bn-text"><b>Pending on us since ${esc(day(pd.on))} · ${plural(Math.max(daysSince(pd.on) ?? 0, 0), "day")}</b>
          <span class="said">${esc(c.req || "They")} asked: “${shortWhat(pd.what, 400)}”</span>
          <span class="small">Logged by ${esc(pd.by || "?")}${c.owner ? ` · ${esc(c.owner)} answers` : ""}</span></div>
        <button type="button" class="btn amber" data-answered="1">Answered</button></div>` : "";
    // we asked the requester for details first (OppList's Waiting for requester): what and since when
    // (what comes next is on the Next step line above)
    const wt = OPEN(c.st) && c.wait ? (typeof c.wait === "object" ? c.wait : { since: c.wait }) : null;
    if (wt?.since) $("#c-pend").innerHTML += `<div class="banner bn-wait" role="status" id="c-wait">
        <svg width="20" height="20" viewBox="0 0 20 20" aria-hidden="true"><path d="M10 3.5a6.5 6.5 0 1 0 0 13a6.5 6.5 0 1 0 0-13z M10 6.5V10l2.5 2" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>
        <div class="bn-text"><b>Waiting for the requester since ${esc(day(wt.since))} · ${plural(Math.max(daysSince(wt.since) ?? 0, 0), "day")}</b>
          ${wt.what ? `<span class="said">We asked: “${shortWhat(wt.what, 400)}”</span>` : ""}
          <span class="small">No quote until they answer; then it waits for our quote again.</span></div>${canWaitReq(c) ? '<button type="button" class="btn" data-inforec>Info received</button>' : ""}</div>`;
    drawMain(c); drawButtons(c); drawNotes();
    if (!canAsk(c)) ask.leave(); else ask.refresh();
    if (!canQuote(c)) qp.leave(); else qp.refresh();
    if (!editable(c)) ep.leave(); else ep.refresh();
    rp.refresh();
    if (rqp.isOpen() && !canRates(c)) rqp.leave();     // a case closed meanwhile takes no more requests
  }
  // a Won or Lost waiting for the administrator, who gets a button that opens what was pasted
  function pcChip(c) {
    if (!c?.pc) return "";
    const t = `${c.pc.by ? `${c.pc.by} says ` : ""}${c.pc.result}${c.pc.at ? ` on ${day(c.pc.at)}` : ""}. The case changes when Iris confirms it.`;
    return isAdmin() && c.pc.id ? `<button type="button" class="chip c-grey chip-btn" data-ev-open title="${esc(t)} Click to see the mail.">${esc(closeLabel(c.pc))}</button>`
      : `<span class="chip c-grey" title="${esc(t)}">${esc(closeLabel(c.pc))}</span>`;
  }
  // the case number copies when clicked (a list's number opens the case, with a copy button)
  function oppTitle() { return `<button type="button" class="h1copy" data-copy-opp="${esc(opp)}" title="Click to copy [${esc(opp)}]">${esc(opp)}${COPY_ICON}</button>`; }
  function drawMain(c) {
    // the LATAM and MFN labels sit in the head with the status, the direction and the mode
    const reqRows = [["Requester", `${esc(c.req || "?")}${c.rtype ? ` <span class="muted">· ${esc(c.rtype)}</span>` : ""}`], ["Received", esc(day(c.rcv, true))],
      ["Subject", esc(c.subjFull || c.subj || "")], ["Incoterm", esc(c.incoterm || "")], ["Commodity", esc(c.commodity || "")],
      ...(c.hold ? [["On hold", esc(c.hold)]] : []), ...(c.quoted ? [["Quoted", esc(day(c.quoted, true))]] : []),
      ...(c.closed ? [[shownStatus(c.st, c.legacy), `${esc(day(c.closed, true))}${c.closedReason ? ` · ${esc(c.closedReason)}` : ""}`]] : [])];
    const subOf = a => a.type === "ask" ? [...(a.vendors || []), ...(a.adhoc || []).map(x => x.email)].join(", ")
      : a.type === "close" ? (a.evidence === "forwarded" ? "The mail was forwarded to Iris" : "The mail is pasted for Iris") + (a.rateFrom ? ` · rate used: ${a.rateFrom === "own" ? "our own" : a.rateFrom}` : "")
      : a.type === "rates" ? `${typeof a.total === "number" ? `${a.cur} ${money(a.total)}` : "No total"}${a.valid ? ` · valid to ${day(a.valid)}` : ""}`
      : a.type === "discard" ? "" 
      : a.what || a.note || "";
    const tl = [...(c.waitingActs || []).slice().reverse().map(a => {
      // a change of Cargo or Route says what changed (OppList's own line replaces it once taken in)
      const et = a.type === "edit" ? editText(a, c.edm || c.ed) : null;
      return { at: a.date || dayOf(a.at), text: et ? et.text : actionText(a), sub: et ? et.sub : subOf(a), who: a.name, pending: true };
    }), ...(c.tl || [])];
    const edOn = editable(c), open = ep.current(), editing = (p, id) => !!open && open.id !== null && open.part === p && open.id === id;
    // with ed, the lines are OppList's with the edits waiting laid over them (editlib.cargoCells, routeCells)
    const cargoRows = c.edm ? c.edm.cargo.map(x => ({ x, v: cargoCells(x, x.added ? null : (c.cargo || []).find(b => b.n === x.n)) }))
      : (c.cargo || []).map(b => ({ x: null, v: { n: b.n, what: b.what || "", com: b.commodity || "", goods: [b.hs ? `HS ${b.hs}` : "", b.value || ""].filter(Boolean).join(" · "), weight: b.weight || "", volume: b.cbm || "", dims: b.dims || "" } }));
    const routeRows = c.edm ? c.edm.routes.map(x => ({ x, v: routeCells(x, x.added ? null : (c.routes || []).find(b => (typeof b.n === "number" ? letter(b.n) : b.n) === x.n)) }))
      : (c.routes || []).map(b => ({ x: null, v: { n: typeof b.n === "number" ? letter(b.n) : b.n, from: b.from || "", to: b.to || "", pod: "", mode: b.mode || "", inc: b.incoterm || "", scope: b.scope || "", result: b.result || "" } }));
    const edCell = (p, x, n) => !edOn ? "" : `<td class="edc">${x && x.id !== null ? `<button type="button" class="linkbtn small" data-ed="${p}:${x.id}" aria-label="Edit ${p === "cargo" ? "cargo line" : "route"} ${esc(n)}">Edit</button>`
      : `<span class="muted small" title="Saved here; OppList adds it at its next update, then it can be changed">-</span>`}</td>`;
    const edTh = edOn ? `<th class="edc"><span class="sr">Edit</span></th>` : "";
    $("#c-main").innerHTML = `
      <section class="panel" aria-labelledby="h-req"><div class="panel-head"><h2 id="h-req">Request</h2></div>
        <dl class="kv">${reqRows.filter(([, v]) => v).map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v}</dd>`).join("")}</dl></section>
      <section class="panel" aria-labelledby="h-cargo"><div class="panel-head"><h2 id="h-cargo">Cargo</h2>${edOn ? `<button type="button" class="btn small" data-ed="cargo:new">Add line</button>` : ""}</div>
        ${cargoRows.length ? `<div class="tablewrap"><table class="grid t-cargo"><thead><tr><th>#</th><th>What</th><th>Commodity</th><th>Weight</th><th>Volume</th><th>Dimensions</th>${edTh}</tr></thead>
          <tbody>${cargoRows.map(({ x, v }) => `<tr${x && editing("cargo", x.id) ? ' class="editing"' : ""}><td>${esc(v.n)}</td><td class="b">${esc(v.what)}</td><td>${esc(v.com)}${v.goods ? `<div class="muted small">${esc(v.goods)}</div>` : ""}</td>
            <td>${v.weight ? esc(v.weight) : '<span class="muted">-</span>'}</td><td>${esc(v.volume)}</td><td>${esc(v.dims)}</td>${edCell("cargo", x, v.n)}</tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">${c.partial ? "The details come with the next update." : "No cargo lines."}</div>`}</section>
      <section class="panel" aria-labelledby="h-route"><div class="panel-head"><h2 id="h-route">Route</h2>${edOn ? `<button type="button" class="btn small" data-ed="route:new">Add route</button>` : ""}</div>
        ${routeRows.length ? `<div class="tablewrap"><table class="grid t-route"><thead><tr><th>#</th><th>From</th><th>To</th><th>Mode</th><th>Incoterm</th><th>Service</th><th>Result</th>${edTh}</tr></thead>
          <tbody>${routeRows.map(({ x, v }) => `<tr${x && editing("route", x.id) ? ' class="editing"' : ""}><td>${esc(v.n)}</td><td>${esc(v.from)}</td><td>${esc(v.to)}${v.pod ? `<div class="muted small">POD ${esc(v.pod)}</div>` : ""}</td><td>${esc(v.mode)}</td><td>${esc(v.inc)}</td><td>${esc(v.scope)}</td><td>${v.result ? statusChip(v.result) : ""}</td>${edCell("route", x, v.n)}</tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">${c.partial ? "The details come with the next update." : esc(c.lane || "No route.")}</div>`}</section>
      <section class="panel" aria-labelledby="h-rr"><div class="panel-head"><h2 id="h-rr">Rate requests <span class="muted">${c.rr?.length || 0}</span></h2>
          <span class="ph-btns">${R.compareRoutes(R.newest(vrates, c.rx || [])).length ? '<button type="button" class="btn small" data-cmp-open aria-haspopup="dialog">Compare rates</button>' : ""}
          ${canAsk(c) ? `<button type="button" class="btn small${ask.isOpen() ? " on" : ""}" data-ask-open aria-expanded="${ask.isOpen()}">Ask vendors</button>` : ""}</span></div>
        ${c.rr?.length ? `<div class="tablewrap"><table class="grid t-rr"><thead><tr><th>Vendor</th><th>Asked on</th><th>Reply</th><th>Their rates, this shipment</th></tr></thead>
          <tbody>${rrRows(c)}</tbody></table></div>`
          : `<div class="empty">No rate requests logged.</div>`}
        ${vratesErr ? `<p class="hint small">The vendors' rates could not be read: ${problemHtml(vratesErr, { part: `${opp} rates` })}</p>` : ""}
        ${canRates(c) && S.vendors?.rows?.length ? pickHtml(c) : ""}
        ${S.vendors || !OPEN(c.st) || c.office ? "" : '<p class="hint small">Asking vendors from this page is coming soon.</p>'}</section>
      ${quotes.length || quoteOn() ? quotesHtml() : ""}`;
    $("#c-tl").innerHTML = `
      <section class="panel" aria-labelledby="h-tl"><div class="panel-head"><h2 id="h-tl">Timeline <span class="muted small">newest first</span></h2></div>
        ${tl.length ? `<ol class="tl">${tl.map(t => `<li${t.pending ? ' class="pending"' : ""}><span class="tl-when">${esc(day(t.at))}</span><span class="tl-what"><span>${esc(t.text)}${t.who ? ` <span class="muted">· ${esc(t.who)}</span>` : ""}</span>${t.sub ? `<span class="muted small">${esc(cut(t.sub, 300))}</span>` : ""}</span></li>`).join("")}</ol>`
          : `<div class="empty">Nothing yet.</div>`}</section>`;
    cmp.refresh();                                     // Compare rates follows the case's rates
  }
  // the Rate requests rows with each vendor's newest rates (on its first row), route by route on a case with
  // several, Lowest, valid until (past in orange); Add rates where it has none yet (Rates saved, no total)
  function rrRows(c) {
    // Lowest leaves out the rates past their valid until once OppList keeps options (it does the same then)
    const today = todayIso(), best = R.newest(vrates, c.rx || []), low = R.lowest(best, S.meta?.options === true ? today : ""), multi = R.routesOf(c).length > 1, seen = new Set(), can = canRates(c), od = draftsNow();
    const rq = S.meta?.readQuote || null;
    return c.rr.map(v => {
      const first = !seen.has(v.vendor); seen.add(v.vendor);
      const isKey = v.vendor && !v.vendor.includes("@");
      const list = first && isKey ? R.ratesOfVendor(best, low, v.vendor, today) : [];
      // the row says their rates are saved and until when, not what they come to nor what
      // they did not say (both in the drawer it opens and on the quote card)
      // with options, Rates saved opens the lowest of them (else the first)
      const cells = list.map(x => `<div class="rt-cell"><button type="button" class="linkbtn" data-rates="${esc(v.vendor)}" data-route="${x.route ?? ""}" data-label="${esc(x.doc.label || "")}" title="Open their rates">${multi && x.route ? `${esc(letter(x.route))} · ` : ""}Rates saved</button>`
        + `${x.doc.valid ? ` <span class="small ${x.past ? "past" : "muted"}">· valid to ${esc(day(x.doc.valid))}</span>` : ""}${x.lowest ? ' <span class="chip c-green">Lowest</span>' : ""}</div>`).join("");
      // a reading of their mail waiting to be checked, route by route
      const reads = first && isKey && can ? R.draftsOfVendor(od, v.vendor).map(d => `<div class="rt-cell"><button type="button" class="linkbtn rq-chip" data-rq="${esc(d.id)}" title="Open it, check each line against their mail, then save it or set it aside">${multi && typeof d.route === "number" ? `${esc(letter(d.route))} · ` : ""}Read from their mail: check</button></div>`).join("") : "";
      const missing = multi ? R.routesOf(c).filter(r => !list.some(x => x.route === r.n)) : [];
      let add = !first || !isKey || !can ? "" : !list.length ? `<button type="button" class="linkbtn small" data-rates="${esc(v.vendor)}">Add rates</button>`
        : missing.length ? `<button type="button" class="linkbtn small" data-rates="${esc(v.vendor)}" data-route="${missing[0].n}">Add rates for ${esc(letter(missing[0].n))}</button>` : "";
      // Read quote on the row while OppList reads them; a request not read yet, or one that could not be read
      const rs = first && isKey ? R.readState(rqReads, readRes, v.vendor, { rates: vrates }) : null;
      const st = !rs ? "" : rs.kind === "wait"
        ? (R.readerOn(rq) ? `<div class="rq-st" data-rqw><span class="dot"></span>Reading their quote · submitted by ${esc(rs.a.name || "someone")} ${esc(when(rs.a.at))}</div>`
          : `<div class="rq-st off" data-rqw><span class="dot"></span>Waiting · submitted by ${esc(rs.a.name || "someone")} ${esc(when(rs.a.at))} · read at the next update</div>`)
        : `<div class="rq-st bad"><span class="dot"></span>Could not read it${rs.msg ? `: ${esc(rs.msg)}` : ""}${first && isKey && can && rq?.on ? ` <button type="button" class="linkbtn" data-readq="${esc(v.vendor)}"${rq.capReached ? " disabled" : ""}>Read quote again</button>` : ""}</div>`;
      const rbtn = first && isKey && can && rq?.on && !rs ? `<button type="button" class="linkbtn small" data-readq="${esc(v.vendor)}"${rq.capReached ? ` disabled title="This month's Read quote limit is reached"` : ""}>Read quote</button>` : "";
      if (rbtn) add = add ? `${add}<span class="muted small"> · </span>${rbtn}` : `<div class="rt-cell">${rbtn}</div>`;
      return `<tr><td>${esc(v.vendor)}</td><td class="nw">${esc(day(v.asked))}</td><td>${v.quoted ? `<span class="chip c-green">Quoted${v.replied ? ` ${esc(day(v.replied))}` : ""}</span>` : v.replied ? `<span class="chip">Replied ${esc(day(v.replied))}</span>` : '<span class="muted">Waiting</span>'}</td>`
        + `<td class="rt-td">${st}${reads}${cells}${add}${!st && !reads && !cells && !add ? '<span class="muted">-</span>' : ""}</td></tr>`;
    }).join("");
  }
  // when a Read quote request was sent: "at 3:42 PM" today, else "on Nov 16, 3:42 PM"
  function when(v) {
    const d = asDate(v); if (!d) return "";
    const t = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
    return d.toDateString() === new Date(nowMs()).toDateString() ? `at ${t}` : `on ${d.toLocaleDateString("en-US", { month: "short", day: "numeric" })}, ${t}`;
  }
  // rates from a vendor the case has not asked (OppList makes the rate request when it takes them in)
  function pickHtml(c) {
    const keys = R.pickList(S.vendors?.rows, c);          // at most 15 at a time
    if (!keys.length) return "";
    return `<form class="rt-pick" id="rt-pick"><label class="small" for="rt-pick-v">Rates from a vendor not asked here:</label>
      <input type="text" id="rt-pick-v" list="rt-pick-dl" placeholder="Type a vendor's name" autocomplete="off" maxlength="100">
      <datalist id="rt-pick-dl">${keys.map(k => `<option value="${esc(k)}">`).join("")}</datalist>
      <button type="submit" class="btn small">Type their rates</button><span class="err small" id="rt-pick-err" role="alert"></span></form>`;
  }
  // the quotes made with the card, newest first; cost, profit and margin for the admin and Costs only
  function quotesHtml() {
    const rows = quotes.slice().reverse().map(q => {
      const k = canCosts() ? qcosts.get(q.id) : null;
      return `<div class="qrow" data-q="${esc(q.id)}"><span class="muted small nw">${esc(day(q.date))}</span>
        <span><b>Quote ${esc(q.n)}</b> · ${esc(q.name || q.by || "")} · ${esc(q.cur)} ${esc(money(q.total))}${q.atCost ? " plus AT COST" : ""}${q.valid ? ` · valid to ${esc(day(q.valid))}` : ""}${q.sources?.length ? `<span class="src-line">Rates from ${esc(sourcesText(q.sources))}</span>` : ""}</span>
        <button type="button" class="linkbtn small" data-quote-view="${esc(q.id)}">View</button>
        ${k ? `<span class="s">Cost ${esc(q.cur)} ${esc(money(k.cost))} · profit ${esc(q.cur)} ${esc(money(k.profit))} · margin ${esc(Number(k.margin || 0).toFixed(1))}%</span>` : ""}</div>`;
    }).join("");
    return `<section class="panel" aria-labelledby="h-quotes" id="c-quotes"><div class="panel-head"><h2 id="h-quotes">Quotes <span class="muted">${quotes.length}</span></h2></div>
      ${quotesErr ? `<div class="empty">${problemHtml(quotesErr, { part: `${opp} quotes` })}</div>` : quotes.length ? `<div class="qrows">${rows}</div>`
        : `<div class="empty">No quote made with the card yet. A quote made with Quote to requester is kept here: the sell prices for everyone${canCosts() ? ", the costs for you and the others with Costs access" : ""}.</div>`}</section>`;
  }
  function actionText(a) {
    if (a.type === "assign") return a.to ? `Handled by ${a.to}` : "Not assigned";
    if (a.type === "quote") return `Quote sent${a.sentBy ? ` by ${a.sentBy}` : ""}`;
    if (a.type === "followup") return `Follow-up (${a.how})${a.answered ? ", they answered" : ""}`;
    if (a.type === "waiting") return `They asked us${a.who ? ` · ${a.who} answers` : ""}`;
    if (a.type === "answered") return "Answered";
    if (a.type === "ask") { const n = (a.vendors || []).length + (a.adhoc || []).length; return `Asked ${plural(n, "vendor")} for rates`; }
    if (a.type === "close") return `Marked ${a.result}${a.reason ? ` (${a.reason})` : ""} · waiting for Iris`;
    if (a.type === "rates") return `Rates from ${a.vendor}`;
    if (a.type === "discard") return "A reading of a vendor's mail set aside";
    return actionLabel(a.type);
  }
  function drawButtons(c) {
    const st = c?.st, quoted = st === "Quoted";
    const btn = (key, label, ok, title = "") => `<button type="button" class="btn act${panel === key ? " on" : ""}" data-panel="${key}" aria-pressed="${panel === key}"${ok ? "" : ` disabled title="${esc(title)}"`}>${label}</button>`;
    const qOpen = qp.isOpen(), qTitle = c?.partial ? "The case is still loading" : "Only for a case that is New or Quoted";
    $("#c-btns").innerHTML = (quoteOn() ? `<button type="button" class="btn act${qOpen ? " on" : ""}" data-quote-open aria-expanded="${qOpen}"${canQuote(c) ? "" : ` disabled title="${esc(qTitle)}"`}>${quoted ? "Quote again" : "Quote to requester"}</button>`
      : btn("quote", quoted ? "Log quote sent again" : "Log quote sent", OPEN(st) && !c?.office, "Only for a case that is New or Quoted"))
      + (waitReqOn() ? btn("waitreq", "Waiting for requester", canWaitReq(c), quoted ? "A quoted case has its own follow-ups" : c?.pc ? "A Won or Lost is waiting to be confirmed" : "Only for a New case") : "")
      + btn("fu", "Log follow-up", quoted && !c?.office, "Opens once the quote is logged")
      + btn("wait", "Waiting on us", quoted && !c?.office, "For a quoted case where the requester asked us something")
      + btn("note", "Add note", !!c)
      + (reasons() ? btn("close", c?.pc ? closeLabel(c.pc) : "Won or Lost", OPEN(st) && !c?.office && !c?.pc,
        c?.pc ? "Iris has not confirmed the last one yet" : "Only for a case that is New or Quoted") : "");
    $("#c-lock").textContent = c && !quoted && OPEN(st) ? "Log follow-up and Waiting on us open once the quote is logged." : c && !OPEN(st) ? `${c.legacy && st === "Dropped" ? "This is a Legacy case from the old workbook; its outcome was not recorded." : `This case is ${st}.`} Add a note if something needs Iris.` : "";
  }
  function flash(msg) { $("#c-flash").innerHTML = `<p class="flash">${esc(msg)}</p>`; clearTimeout(flashT); flashT = setTimeout(() => { const f = $("#c-flash"); if (f) f.innerHTML = ""; }, 6000); }

  function openPanel(key) {
    panel = key; const c = cur(); drawButtons(c);
    const box = $("#c-panel"); const today = todayIso();
    const whoDefault = myOwner() || c?.owner || "";
    const foot = (hint) => `<div class="err" id="p-err"></div><div class="row-end"><button type="button" class="btn" data-cancel>Cancel</button><button type="submit" class="btn primary">Save</button></div>${hint ? `<p class="hint small">${hint}</p>` : ""}`;
    if (!key) { box.innerHTML = ""; return; }
    // Whose rate was used starts from the last quote's rates (its Freight part)
    if (key === "close") { closeForm(box, opp, c, { rateDefault: quotes.slice(-1)[0]?.rateFrom || "", onCancel: () => openPanel(""), onDone: msg => { openPanel(""); flash(msg); } }); return; }
    if (key === "quote") box.innerHTML = `<form class="sub-panel" id="f-p"><h3>${c?.st === "Quoted" ? "Log quote sent again" : "Log quote sent"}</h3>
        <label class="field">Date sent<input type="date" name="date" value="${today}" max="${today}" required></label>
        <label class="field">What was quoted (optional)<textarea name="what" rows="2" maxlength="1000" placeholder="e.g. 1x40HQ port to port, valid to Nov 15"></textarea></label>
        <label class="field">Sent by<select name="sentBy">${ownerOptions(whoDefault, "Pick who sent it")}</select></label>
        ${foot("Prices stay in the email for now.")}</form>`;
    if (key === "fu") {
      const step = /^FU\d$/.test(c?.next || "") ? c.next : c?.fu && c.fu !== "Decide" ? c.fu : "";
      box.innerHTML = `<form class="sub-panel" id="f-p"><h3>Log follow-up${step ? ` · ${esc(step)}` : ""}</h3>
        <label class="field">Date<input type="date" name="date" value="${today}" max="${today}" required></label>
        <label class="field">How<select name="how">${HOW.map(h => `<option>${h}</option>`).join("")}</select></label>
        <fieldset class="field"><legend>Did they answer?</legend>
          <label class="radio"><input type="radio" name="ans" value="no" checked> No answer yet</label>
          <label class="radio"><input type="radio" name="ans" value="yes"> They answered</label></fieldset>
        <label class="field">Note (optional)<textarea name="note" rows="2" maxlength="1000"></textarea></label>
        ${foot("")}</form>`;
    }
    if (key === "wait") box.innerHTML = `<form class="sub-panel" id="f-p"><h3>Waiting on us</h3>
        <label class="field">What did they ask?<textarea name="what" rows="3" maxlength="1000" required placeholder="e.g. Can you also quote LCL for the 20GP part?"></textarea></label>
        <label class="field">Asked on<input type="date" name="date" value="${today}" max="${today}" required></label>
        <label class="field">Who answers<select name="who">${ownerOptions(c?.owner || whoDefault, "Pick who answers")}</select></label>
        ${foot("Shows at the top of this case, under Pending on us on the Assign List, and on the answerer's My board, until someone presses Answered.")}</form>`;
    // what we still need from them and the day we asked; while one is waiting, what it says, to change it
    if (key === "waitreq") {
      const now = c?.wait && typeof c.wait === "object" ? c.wait : null;
      box.innerHTML = `<form class="sub-panel" id="f-p"><h3>Waiting for requester</h3>
        <label class="field">What we still need from them<textarea name="what" rows="3" maxlength="300" required placeholder="e.g. the dimensions and weight; the pickup address">${esc(now?.what || "")}</textarea></label>
        <label class="field">We asked them on<input type="date" name="date" value="${esc(validIso(now?.since || "") ? now.since : today)}" max="${today}" required></label>
        ${foot(`The case stays New. It shows at the top of this case and under Waiting for the requester on the Assign List, and the follow-ups to them come due on its Next step, until they reply, the case moves on, or someone presses Info received.${now ? " Saving again replaces what it says." : ""}`)}</form>`;
    }
    if (key === "note") box.innerHTML = `<form class="sub-panel" id="f-p"><h3>Add note</h3>
        <label class="field">Note<textarea name="text" rows="3" maxlength="2000" required></textarea></label>
        <label class="field">Remind me (optional)<input type="date" name="remind"></label>
        ${foot("Everyone can see notes. A reminder date also puts it on your My board.")}</form>`;
    const f = $("#f-p"); f.querySelector("textarea, input, select")?.focus();
    f.onsubmit = async ev => {
      ev.preventDefault();
      const err = $("#p-err"), btn = f.querySelector("button[type=submit]"); err.textContent = "";
      try {
        const date = f.date?.value || "";
        if (f.date && (!validIso(date) || date > todayIso())) throw new Error("Pick a date up to today.");
        btn.disabled = true;
        if (key === "quote") {
          if (!f.sentBy.value) throw new Error("Pick who sent the quote.");
          await act("quote", opp, { date, what: f.what.value.trim(), sentBy: f.sentBy.value });
        } else if (key === "fu") {
          await act("followup", opp, { date, how: f.how.value, answered: f.ans.value === "yes", note: f.note.value.trim() });
        } else if (key === "wait") {
          if (!f.what.value.trim()) throw new Error("Write what they asked.");
          await act("waiting", opp, { date, what: f.what.value.trim(), who: f.who.value });
        } else if (key === "waitreq") {
          if (!f.what.value.trim()) throw new Error("Write what we still need from them.");
          await act("waitreq", opp, { date, what: f.what.value.trim().slice(0, 300) });
        } else if (key === "note") {
          if (!f.text.value.trim()) throw new Error("Write the note first.");
          if (f.remind.value && !validIso(f.remind.value)) throw new Error("Pick the reminder date from the calendar.");
          await addNote({ text: f.text.value.trim(), opp, remind: f.remind.value || "" });
        }
        openPanel("");
        flash(key === "note" ? "Note saved." : key === "waitreq" ? "Saved: it waits for the requester." : "Saved.");
      } catch (e) { btn.disabled = false; err.innerHTML = failHtml(e, { part: opp }); }
    };
  }
  function drawNotes() {
    // only the site's own notes
    const web = notes.slice().sort((a, b) => ms(b.at) - ms(a.at));
    const box = $("#c-notes"); if (!box) return;
    if (!web.length) { box.innerHTML = `<div class="empty">No notes yet.</div>`; return; }
    box.innerHTML = web.map(n => `<div class="li${n.done ? " done" : ""}"><div class="note-text">${esc(n.text)}</div>
        <div class="muted small">${esc(n.name || n.by)} · ${esc(stamp(n.at))}${n.remind ? ` · remind ${esc(day(n.remind))}` : ""}${n.done ? " · done" : ""}</div></div>`).join("");
  }
  draw();
  if (p.do && ["quote", "fu", "wait", "note", "close"].includes(p.do)) {
    // the Log follow-up links on My board and Follow-ups open the panel straight away (once the case is known); a
    // quote link opens the quote card's Just log it once the card is there
    const want = p.do; let tries = 0;
    doTimer = setInterval(() => {
      const c = cur(); if (!(c && !c.partial) && ++tries <= 40) return;
      clearInterval(doTimer);
      if (!c) return;
      if (want === "quote" && quoteOn()) { if (canQuote(c)) { qp.show("just"); drawButtons(c); } return; }
      if (!$(`[data-panel="${want}"]`)?.disabled) openPanel(want);
    }, 100);
  }
  // a request waiting says whether it is being read now, which changes with the clock as well as with the data
  let rqOnWas = R.readerOn(S.meta?.readQuote);
  const rqTick = setInterval(() => {
    const on = R.readerOn(S.meta?.readQuote);
    if (on !== rqOnWas && root.querySelector(".rq-st[data-rqw]")) { const c = cur(); if (c) drawMain(c); }
    rqOnWas = on;
  }, 30000);
  if (p.draft) {
    // OppList's link (Check and save on the team site) opens the drawer on its draft once the case, its rates and
    // its drafts are here
    let tries = 0;
    rqTimer = setInterval(() => {
      const c = cur(); if (!(c && !c.partial && draftsIn && vratesIn) && ++tries <= 80) return;
      clearInterval(rqTimer);
      if (c) openDraft(String(p.draft));
    }, 100);
  }
  return {
    refresh() { draw(); },
    leave() { unsub.forEach(u => u()); clearTimeout(flashT); clearInterval(doTimer); clearInterval(rqTimer); clearInterval(rqTick); ask.leave(); qp.leave(); ep.leave(); rp.leave(); rqp.leave(); cmp.leave(); window.removeEventListener("scroll", onScroll); },
  };
}
const ms = v => v?.toMillis ? v.toMillis() : v instanceof Date ? v.getTime() : 0;
// an action's day for the Timeline when it carries no date of its own (an edit): the day it was saved, local time
const dayOf = v => { const t = ms(v); if (!t) return ""; const d = new Date(t); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
