// Follow-ups: the quoted cases whose next step OppList says is due today: FU1, FU2, and Decide (no answer after
// the follow-ups; the admin decides). Cases where the requester asked something are on Pending on us.
import { S, $, main, myOwner, pageHead, go } from "../core.js";
import { esc, day, daysSince, plural } from "../lib.js";
import { merged } from "../data.js";
import { seg, caseLink, unsynced, ownerChip, emptyLists } from "./common.js";

const GROUPS = [
  ["FU1", "FU1 due", "first follow-up after the quote", "Quoted on"],
  ["FU2", "FU2 due", "they answered the first follow-up", "Last activity"],
  ["Decide", "Decide", "no answer after the follow-ups; Iris decides", "Last activity"],
];
export async function viewFollowups(p) {
  const who = p.who === "mine" ? "mine" : "all";
  main.innerHTML = `<div class="page" id="pg-fu"><div id="head"></div><div id="body"><div class="muted loading">Loading…</div></div></div>`;
  const root = $("#pg-fu");
  root.addEventListener("click", ev => { const s = ev.target.closest("[data-seg]"); if (s) go("followups", { who: s.dataset.seg === "mine" ? "mine" : "" }); });
  function draw() {
    const m = merged();
    const due = m.rows.filter(r => r.st === "Quoted" && !r.pc && GROUPS.some(g => g[0] === r.fu));   // not one closing
    const mine = due.filter(r => myOwner() && r.owner === myOwner());
    $("#head").innerHTML = pageHead("Follow-ups", "Quoted cases with no answer yet, by the next step. Days are calendar days.",
      seg("Whose", [["mine", `Mine <b>${mine.length}</b>`], ["all", `All <b>${due.length}</b>`]], who));
    const body = $("#body");
    if (S.inquiries === undefined) return;
    if (S.inquiries === null) { body.innerHTML = emptyLists(); return; }
    const rows = who === "mine" ? mine : due;
    const asked = m.rows.filter(r => r.st === "Quoted" && !r.pc && r.pend).length;
    body.innerHTML = (who === "mine" && !myOwner() ? `<p class="hint">Your account is not linked to a Handled by name yet, so Mine is empty. Ask Iris.</p>` : "")
      + GROUPS.map(([key, title, hint, dateHead]) => {
        const g = rows.filter(r => r.fu === key).sort((a, b) => String(a.since).localeCompare(String(b.since)) || a.opp.localeCompare(b.opp));
        return `<section class="panel" aria-labelledby="h-${key}"><div class="panel-head"><h2 id="h-${key}">${esc(title)} <span class="muted">${g.length} · ${esc(hint)}</span></h2></div>
          ${g.length ? `<div class="tablewrap"><table class="grid t-fus">
            <colgroup><col class="c-opp"><col class="c-req"><col><col class="c-mode"><col class="c-date"><col class="c-n"><col class="c-hb2"><col class="c-btn2"></colgroup>
            <thead><tr><th>OPP No</th><th>Requester</th><th>Lane</th><th>Mode</th><th>${esc(dateHead)}</th><th>Days</th><th>Handled by</th><th><span class="sr">Action</span></th></tr></thead>
            <tbody>${g.map(r => `<tr><td>${caseLink(r.opp)}${unsynced(r)}</td><td>${esc(r.req)}</td><td>${esc(r.lane)}</td><td>${esc(r.mode)}</td>
              <td class="nw">${esc(day(r.since))}</td><td>${daysSince(r.since) ?? ""}</td><td>${ownerChip(r.owner)}</td>
              <td class="nw">${key === "Decide" ? `<a href="#/case?opp=${esc(r.opp)}">Open</a>` : `<a class="btn small outline" href="#/case?opp=${esc(r.opp)}&do=fu">Log follow-up</a>`}</td></tr>`).join("")}</tbody></table></div>`
            : `<div class="empty">None.</div>`}</section>`;
      }).join("")
      + (asked ? `<p class="foot muted small">${plural(asked, "more case")} got a question from the requester: see <a href="#/assign">Pending on us</a>.</p>` : "");
  }
  draw();
  return { refresh() { draw(); } };
}
