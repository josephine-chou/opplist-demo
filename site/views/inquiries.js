// Inquiries: every inquiry OppList counts as ours (scope In), newest first, in three groups: New (waiting for our
// quote), Quoted (waiting for the requester; the ones where they asked us something on top) and Closed (folded).
import { S, $, main, myOwner, owners, pageHead } from "../core.js";
import { esc, day, norm, monthName, plural, nowMs } from "../lib.js";
import { merged } from "../data.js";
import { seg, select, grp, caseLink, unsynced, ownerChip, emptyLists, shortWhat, shownStatus, LEGACY_TITLE } from "./common.js";

const F = { q: "", month: "", mode: "", hb: "", who: "all", closed: false };   // kept while the tab is open
const CLOSED = ["Won", "Lost", "Dropped", "Cancelled"];

export function nextCell(r) {
  // a colleague's Won or Lost, until the administrator confirms it
  if (r.pc) return `<span class="chip c-grey" title="${esc(r.pc.by || "Someone")} says ${esc(r.pc.result)}. The case changes when Iris confirms it.">${esc(r.pc.result)}? waiting for Iris</span>`;
  if (r.office) return `<span class="muted">Handled by ${esc(r.office)}</span>`;
  if (r.st === "Quoted") {
    if (r.pend) return `<span class="chip c-owe">Answer them</span>`;
    if (r.fu) return `<span class="chip ${r.fu === "Decide" ? "c-red" : "c-blue"}">${esc(r.fu === "Decide" ? "Decide" : `${r.fu} due`)}</span>`;
    if (r.next) return `<span class="muted">${esc(r.next)}${r.due ? ` ${esc(day(r.due))}` : ""}</span>`;
    return `<span class="muted">-</span>`;
  }
  // we asked the requester for details first; OppList's Next step reads "Waiting for requester: FU1 Nov 11"
  if (r.st === "New" && r.wait) {
    const rest = String(r.next || "").replace(/^Waiting for requester:\s*/i, "");
    return `<span class="chip c-grey" title="We asked the requester for details on ${esc(day(r.wait))}. It waits for our quote again once they answer.">Waiting for requester</span>${rest ? ` <span class="muted small">${esc(rest)}</span>` : ""}`;
  }
  if (r.st === "New") return `<span>${esc(r.next || "Send the quote")}</span>`;
  return `<span class="muted"${isLegacy(r) ? ` title="${LEGACY_TITLE}"` : ""}>${esc(shownStatus(r.st, r.legacy))}${r.closed ? ` ${esc(day(r.closed))}` : ""}</span>`;
}
// a legacy row's last activity is the import's "Dropped Aug 6"; it reads Legacy, like its Next step
const isLegacy = r => !!r.legacy && r.st === "Dropped";
const lastCell = r => { const t = r.lastText || day(r.last); return isLegacy(r) ? String(t).replace(/^Dropped\b/, "Legacy") : t; };
const laneCell = r => `${esc(r.lane)}${r.pend ? `<div class="asked" title="${esc(r.pend.what)}">They asked: “${shortWhat(r.pend.what, 110)}”</div>` : ""}`;
const hbCell = r => r.office ? `<span class="muted">${esc(r.office)} office</span>` : ownerChip(r.owner);
function table(rows) {
  return `<div class="tablewrap"><table class="grid t-inq">
    <colgroup><col class="c-opp"><col class="c-date"><col class="c-req"><col><col class="c-mode"><col class="c-next"><col class="c-hb2"><col class="c-last"></colgroup>
    <thead><tr><th>OPP No</th><th>Received</th><th>Requester</th><th>Lane</th><th>Mode</th><th>Next step</th><th>Handled by</th><th>Last activity</th></tr></thead>
    <tbody>${rows.map(r => `<tr class="${r.pend ? "pendrow" : ""}"><td>${caseLink(r.opp)}${unsynced(r)}</td><td class="nw">${esc(day(r.rcv, r.rcv?.slice(0, 4) !== new Date(nowMs()).getFullYear().toString()))}</td>
      <td>${esc(r.req)}</td><td>${laneCell(r)}</td><td>${esc(r.mode)}</td><td>${nextCell(r)}</td><td>${hbCell(r)}</td><td class="muted small">${esc(lastCell(r))}</td></tr>`).join("")}</tbody></table></div>`;
}

export async function viewInquiries(p = {}) {
  // from the search box's "See all": that search, nothing else filtered; closed ones open unless it searched open cases
  if (p.q !== undefined) Object.assign(F, { q: p.q, month: "", mode: "", hb: "", who: p.who === "mine" ? "mine" : "all", closed: p.closed === "1" });
  main.innerHTML = `<div class="page" id="pg-inq"><div id="head"></div>
    <div class="bar" role="search" id="filters"></div><div id="body"><div class="muted loading">Loading…</div></div></div>`;
  const root = $("#pg-inq");
  function drawFilters() {
    const m = merged();
    const months = [...new Set(m.rows.map(r => String(r.rcv || "").slice(0, 7)).filter(Boolean))].sort().reverse();
    const modes = [...new Set(m.rows.map(r => r.mode).filter(Boolean))].sort();
    $("#filters").innerHTML = `<label class="fld grow"><span class="fl">Search</span><input type="search" id="f-q" value="${esc(F.q)}" placeholder="OPP No, requester, place, country or subject" autocomplete="off"></label>`
      + grp("Month", select("f-month", [["", "All months"], ...months.map(x => [x, monthName(x)])], F.month, { minWidth: 150 }))
      + grp("Mode", select("f-mode", [["", "All modes"], ...modes.map(x => [x, x])], F.mode, { minWidth: 120 }))
      + grp("Handled by", select("f-hb", [["", "Anyone"], ["-", "Not assigned"], ...owners().map(x => [x, x])], F.hb, { minWidth: 130 }))
      + `<div class="fld"><span class="fl">Whose</span>${seg("Whose", [["all", "All"], ["mine", "Mine"]], F.who)}</div>`;
  }
  root.addEventListener("input", ev => { if (ev.target.id === "f-q") { F.q = ev.target.value; drawBody(); } });
  root.addEventListener("change", ev => {
    const id = ev.target.id;
    if (id === "f-month") F.month = ev.target.value; else if (id === "f-mode") F.mode = ev.target.value; else if (id === "f-hb") F.hb = ev.target.value; else return;
    drawBody();
  });
  root.addEventListener("click", ev => {
    const s = ev.target.closest("[data-seg]");
    if (s) { F.who = s.dataset.seg; root.querySelectorAll("[data-seg]").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.seg === F.who))); drawBody(); return; }
    if (ev.target.closest("#b-closed")) { F.closed = !F.closed; drawBody(); }
  });
  function keep(r) {
    if (F.month && !String(r.rcv || "").startsWith(F.month)) return false;
    if (F.mode && r.mode !== F.mode) return false;
    if (F.hb === "-" ? !!r.owner : F.hb && r.owner !== F.hb) return false;
    if (F.who === "mine" && !(myOwner() && r.owner === myOwner())) return false;
    if (F.q) { const q = norm(F.q); if (!norm(`${r.opp} ${r.req} ${r.lane} ${r.subj} ${r.mode} ${r.owner} ${r.oc || ""} ${r.dc || ""} ${r.com || ""}`).includes(q)) return false; }   // the commodity too (com)
    return true;
  }
  function drawBody() {
    const body = $("#body");
    if (S.inquiries === undefined) return;
    if (S.inquiries === null) { body.innerHTML = emptyLists(); return; }
    const m = merged(), all = m.rows.filter(keep);
    const byNewest = (a, b) => String(b.rcv).localeCompare(String(a.rcv)) || b.opp.localeCompare(a.opp);
    // a Won or Lost sent here goes to Closed at once, marked waiting for the administrator
    const fresh = all.filter(r => r.st === "New" && !r.pc).sort(byNewest);
    const quoted = all.filter(r => r.st === "Quoted" && !r.pc).sort((a, b) => (!!b.pend - !!a.pend) || byNewest(a, b));
    const closedOn = r => r.pc?.at || r.closed || r.rcv;
    const closed = all.filter(r => CLOSED.includes(r.st) || r.pc).sort((a, b) => String(closedOn(b)).localeCompare(String(closedOn(a))) || byNewest(a, b));
    // a legacy row (stored Dropped) is counted as Legacy
    const waitingJ = closed.filter(r => r.pc && !CLOSED.includes(r.st)).length;
    const counts = [...[...CLOSED, "Legacy"].map(s => [s, closed.filter(r => !r.pc && shownStatus(r.st, r.legacy) === s).length]), ["waiting for Iris", waitingJ]]
      .filter(([, n]) => n).map(([s, n]) => `${s} ${n}`).join(" · ");
    const mineHint = F.who === "mine" && !myOwner() ? `<p class="hint">Your account is not linked to a Handled by name yet, so Mine is empty. Ask Iris.</p>` : "";
    body.innerHTML = mineHint + `
      <section class="panel" aria-labelledby="h-new"><div class="panel-head"><h2 id="h-new">New <span class="muted">${fresh.length} · waiting for our quote${fresh.some(r => r.wait) ? ` · ${plural(fresh.filter(r => r.wait).length, "is", "are")} waiting for the requester first` : ""}</span></h2></div>
        ${fresh.length ? table(fresh) : `<div class="empty">No new inquiries${F.q || F.month || F.mode || F.hb || F.who !== "all" ? " with these filters" : ""}.</div>`}</section>
      <section class="panel" aria-labelledby="h-q"><div class="panel-head"><h2 id="h-q">Quoted <span class="muted">${quoted.length} · waiting for the requester${quoted.some(r => r.pend) ? ` · ${plural(quoted.filter(r => r.pend).length, "is", "are")} waiting for us, on top` : ""}</span></h2></div>
        ${quoted.length ? table(quoted) : `<div class="empty">Nothing quoted${F.q || F.month || F.mode || F.hb || F.who !== "all" ? " with these filters" : ""}.</div>`}</section>
      <section class="panel" aria-labelledby="h-closed"><div class="panel-head"><h2 id="h-closed">Closed <span class="muted">${counts || "none"}</span></h2>
        ${closed.length ? `<button type="button" class="btn small" id="b-closed" aria-expanded="${F.closed}">${F.closed ? "Hide closed" : "Show closed"}</button>` : ""}</div>
        ${F.closed && closed.length ? table(closed) : ""}</section>`;
  }
  function draw() { $("#head").innerHTML = pageHead("Inquiries", "Every inquiry, newest first."); drawFilters(); drawBody(); }
  draw();
  return {
    refresh() {
      $("#head").innerHTML = pageHead("Inquiries", "Every inquiry, newest first.");
      if (!document.activeElement?.closest?.("#filters")) drawFilters();
      drawBody();
    },
  };
}
