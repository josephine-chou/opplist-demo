// Assign List: the inquiries waiting for our quote (Take it, Assign to) and the quoted cases where the requester
// asked us something (Pending on us; Answered), from OppList.
// A New case where we asked the requester for details first (OppList's Waiting for requester) is not
// waiting for a quote yet: it is listed under Waiting for the requester until they answer, still assignable.
// The OPP No stands alone here, no LATAM or MFN label under it;
// no list shows them: the case page's Request card does. The number is copied on the case page only.
import { S, $, main, go, myOwner, pageHead, confirmBox, toast } from "../core.js";
import { esc, day, todayIso } from "../lib.js";
import { merged, act } from "../data.js";
import { showProblem } from "../issues.js";
import { seg, daysBadge, legend, caseLink, unsynced, ownerCell, wireOwnerCells, emptyLists, shortWhat } from "./common.js";

const SHOW = [["all", "All"], ["open", "Not assigned"], ["mine", "Mine"]];
// OppList's Next step for such a case reads "Waiting for requester: FU1 Nov 11"; the section already says the first part
const waitNext = r => String(r?.next || "").replace(/^Waiting for requester:\s*/i, "") || "-";
export async function viewAssign(p) {
  let show = SHOW.some(s => s[0] === p.show) ? p.show : "all";
  main.innerHTML = `<div class="page" id="pg-assign"><div id="head"></div><div id="body"><div class="muted loading">Loading…</div></div></div>`;
  const root = $("#pg-assign");
  const keep = r => show === "all" || (show === "open" ? !r.owner : !!myOwner() && r.owner === myOwner());
  wireOwnerCells(root, opp => { const m = merged(); return m.waiting.find(r => r.opp === opp) || m.pending.find(r => r.opp === opp); });
  root.addEventListener("click", async ev => {
    const s = ev.target.closest("[data-seg]");
    if (s) { show = s.dataset.seg; go("assign", { show: show === "all" ? "" : show }); return; }
    const b = ev.target.closest("[data-answered]"); if (!b) return;
    const opp = b.dataset.answered;
    if (!await confirmBox("Answered", `Mark ${opp} as answered? It leaves Pending on us, and its follow-ups start again from today.`, "Answered")) return;
    b.disabled = true;
    try { await act("answered", opp, { date: todayIso(), note: "" }); toast(`${opp}: answered.`); }
    catch (e) { b.disabled = false; showProblem(e, { part: opp }); }
  });
  function draw() {
    const m = merged();
    const quoteAll = m.waiting.filter(r => !r.wait), reqAll = m.waiting.filter(r => r.wait);
    const counts = { all: quoteAll.length, open: quoteAll.filter(r => !r.owner).length, mine: quoteAll.filter(r => myOwner() && r.owner === myOwner()).length };
    $("#head").innerHTML = pageHead("Assign List", "Inquiries waiting for our quote, and questions we owe an answer.",
      seg("Show", SHOW.map(([v, l]) => [v, `${l} <b>${counts[v]}</b>`]), show));
    const body = $("#body");
    if (S.assign === undefined) return;
    if (S.assign === null) { body.innerHTML = emptyLists(); return; }
    const wait = quoteAll.filter(keep), req = reqAll.filter(keep), pend = m.pending.filter(keep);
    const mineHint = show === "mine" && !myOwner() ? `<p class="hint">Your account is not linked to a Handled by name yet, so Mine is empty. Ask Iris.</p>` : "";
    body.innerHTML = mineHint + `
      <section class="panel" aria-labelledby="h-wait">
        <div class="panel-head"><h2 id="h-wait">Waiting for a quote <span class="muted">${wait.length}${show === "all" ? ` · ${counts.open} not assigned` : ""}</span></h2>${legend()}</div>
        ${wait.length ? `<div class="tablewrap"><table class="grid t-wait">
          <colgroup><col class="c-opp"><col class="c-req"><col><col class="c-date"><col class="c-days"><col class="c-hb"><col class="c-hold"></colgroup>
          <thead><tr><th>OPP No</th><th>Requester</th><th>Subject</th><th>Received</th><th>Days waiting</th><th>Handled by</th><th>On hold</th></tr></thead>
          <tbody>${wait.map(r => `<tr data-opp="${esc(r.opp)}"><td>${caseLink(r.opp)}${unsynced(r)}</td><td>${esc(r.req)}</td><td class="ell" title="${esc(r.subj)}">${esc(r.subj)}</td>
            <td class="nw">${esc(day(r.rcv))}</td><td>${daysBadge(r.rcv)}</td><td>${ownerCell(r)}</td><td class="muted">${esc(r.hold || "")}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">${show === "all" ? "Nothing is waiting for a quote." : show === "open" ? "Every inquiry has someone on it." : "Nothing waiting is yours."}</div>`}
      </section>
      <section class="panel" aria-labelledby="h-pend">
        <div class="panel-head"><h2 id="h-pend">Pending on us <span class="muted">${pend.length} · quoted cases where the requester asked us something</span></h2></div>
        ${pend.length ? `<div class="tablewrap"><table class="grid t-pend">
          <colgroup><col class="c-opp"><col class="c-req"><col class="c-subj2"><col class="c-date"><col class="c-days"><col><col class="c-hb"><col class="c-btn"></colgroup>
          <thead><tr><th>OPP No</th><th>Requester</th><th>Subject</th><th>Asked on</th><th>Days waiting</th><th>What they asked</th><th>Handled by</th><th><span class="sr">Answered</span></th></tr></thead>
          <tbody>${pend.map(r => `<tr data-opp="${esc(r.opp)}"><td>${caseLink(r.opp)}${unsynced(r)}</td><td>${esc(r.req)}</td><td class="ell" title="${esc(r.subj)}">${esc(r.subj)}</td>
            <td class="nw">${esc(day(r.on))}</td><td>${daysBadge(r.on)}</td><td class="what" title="${esc(r.what)}">${shortWhat(r.what, 160)}<div class="muted small">logged by ${esc(r.by || "?")}</div></td>
            <td>${ownerCell(r)}</td><td><button type="button" class="btn small" data-answered="${esc(r.opp)}">Answered</button></td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">${show === "all" ? "Nobody is waiting for an answer from us." : "Nothing here."}</div>`}
      </section>${reqAll.length ? `
      <section class="panel" aria-labelledby="h-req">
        <div class="panel-head"><h2 id="h-req">Waiting for the requester <span class="muted">${req.length} · we asked them for details first; back to Waiting for a quote when they answer</span></h2></div>
        ${req.length ? `<div class="tablewrap"><table class="grid t-req">
          <colgroup><col class="c-opp"><col class="c-req"><col><col class="c-date"><col class="c-days"><col class="c-next"><col class="c-hb"></colgroup>
          <thead><tr><th>OPP No</th><th>Requester</th><th>Subject</th><th>Since</th><th>Days waiting</th><th>Next</th><th>Handled by</th></tr></thead>
          <tbody>${req.map(r => `<tr data-opp="${esc(r.opp)}"><td>${caseLink(r.opp)}${unsynced(r)}</td><td>${esc(r.req)}</td><td class="ell" title="${esc(r.subj)}">${esc(r.subj)}</td>
            <td class="nw">${esc(day(r.wait))}</td><td>${daysBadge(r.wait)}</td><td class="muted">${esc(waitNext(m.byOpp.get(r.opp)))}</td><td>${ownerCell(r)}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">Nothing here.</div>`}
      </section>` : ""}`;
  }
  draw();
  let deferred = false;
  root.addEventListener("focusout", () => { if (deferred) { deferred = false; setTimeout(draw, 0); } });
  return {
    refresh() {
      // a list someone is picking from is not redrawn under them; it is redrawn when they leave it
      if (document.activeElement?.closest?.("#body select")) { deferred = true; return; }
      draw();
    },
  };
}
