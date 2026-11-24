// The search box at the top right of every page: finds any case in the lists, all cases by
// default, or only the open ones, or only the person's own. It lives in the top bar of the shell, outside the pages, so
// a page that redraws (every sync) never wipes what is being typed. Up to 8 cases show under the box; Enter opens the
// highlighted one; the last line opens Inquiries with the same search for the rest.
import { S, $, go, myOwner } from "./core.js";
import { esc, norm, day } from "./lib.js";
import { merged } from "./data.js";
import { statusChip } from "./views/common.js";

const MAX = 8;
const SCOPES = [["all", "All cases"], ["open", "Open cases"], ["mine", "My cases"]];
const OPEN = st => st === "New" || st === "Quoted";
let hits = [], more = 0, at = -1;

export function initSearch() {
  const bar = $("#topbar");
  bar.innerHTML = `<div class="tb-in"><div class="gs" role="search">
    <select id="gs-scope" aria-label="Search in">${SCOPES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
    <span class="gs-box"><svg width="16" height="16" viewBox="0 0 20 20" aria-hidden="true"><path d="M8.5 3.5a5 5 0 1 0 0 10a5 5 0 1 0 0-10z M12.2 12.2l4.3 4.3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/></svg>
      <input id="gs-q" type="search" placeholder="Search cases" title="Search by OPP No, requester, place, country, commodity or subject" autocomplete="off" spellcheck="false"
        role="combobox" aria-label="Search cases" aria-expanded="false" aria-controls="gs-pop" aria-autocomplete="list"></span>
    <div class="gs-pop" id="gs-pop" role="listbox" aria-label="Cases found" hidden></div></div></div>`;
  const q = $("#gs-q"), scope = $("#gs-scope"), pop = $("#gs-pop");
  q.addEventListener("input", () => find());
  q.addEventListener("focus", () => { if (q.value.trim()) find(); });
  scope.addEventListener("change", () => { if (q.value.trim()) find(); q.focus(); });
  q.addEventListener("keydown", ev => {
    if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
      if (pop.hidden) { find(); return; }
      const n = hits.length + (more ? 1 : 0); if (!n) return;
      ev.preventDefault(); at = (at + (ev.key === "ArrowDown" ? 1 : n - 1)) % n; paint();
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      if (!q.value.trim()) return;
      if (at >= 0 && at < hits.length) open(hits[at].opp);
      else if (at === hits.length && more) all();
      else if (hits.length) open(hits[0].opp);
    } else if (ev.key === "Escape") { close(); }
  });
  pop.addEventListener("mousedown", ev => ev.preventDefault());   // keep the focus in the box while clicking a line
  pop.addEventListener("click", ev => {
    const row = ev.target.closest("[data-opp]"); if (row) { ev.preventDefault(); open(row.dataset.opp); return; }
    if (ev.target.closest("[data-all]")) { ev.preventDefault(); all(); }
  });
  document.addEventListener("click", ev => { if (!ev.target.closest(".gs")) close(); });
  window.addEventListener("hashchange", close);
}
export function showSearch(on) {
  $("#topbar").hidden = !on;
  if (!on) { const q = $("#gs-q"); if (q) q.value = ""; close(); }
}
// a sync arrived while the list is open: the lines follow it
export function refreshSearch() { if (!$("#gs-pop")?.hidden) find(true); }

function close() { const pop = $("#gs-pop"); if (!pop) return; pop.hidden = true; $("#gs-q").setAttribute("aria-expanded", "false"); at = -1; }
function open(opp) { $("#gs-q").value = ""; close(); go("case", { opp }); }
function all() {
  const qv = $("#gs-q").value.trim(), sc = $("#gs-scope").value;
  close(); go("inquiries", { q: qv, closed: sc === "open" ? "" : "1", who: sc === "mine" ? "mine" : "" });
}

// a case number typed as 642, 0642 or OPP-642 finds that case first
function caseNo(qv) { const m = /^(?:opp)?[\s-]*0*(\d{1,6})$/i.exec(qv.trim()); return m ? Number(m[1]) : null; }
function find(keep = false) {
  const qv = $("#gs-q").value, pop = $("#gs-pop"), sc = $("#gs-scope").value;
  if (!qv.trim()) { close(); return; }
  if (!keep) at = -1;
  if (S.inquiries === undefined) { show(`<div class="gs-note">Loading the lists…</div>`); return; }
  if (S.inquiries === null) { show(`<div class="gs-note">No lists yet. They appear after the first update.</div>`); return; }
  if (sc === "mine" && !myOwner()) { hits = []; more = 0; show(`<div class="gs-note">Your account is not linked to a Handled by name yet, so My cases is empty. Ask Iris.</div>`); return; }
  const n = caseNo(qv), words = norm(qv).split(" ").filter(Boolean);
  const rows = merged().rows.filter(r => sc === "all" || (sc === "open" ? OPEN(r.st) && !r.pc : r.owner === myOwner()));   // a Won or Lost sent is not open
  const found = [];
  for (const r of rows) {
    const exact = n !== null && Number(String(r.opp).slice(4)) === n;
    const hay = norm(`${r.opp} ${r.req} ${r.lane} ${r.subj} ${r.mode} ${r.owner} ${r.st} ${r.oc || ""} ${r.dc || ""} ${r.com || ""}`);   // the countries too (oc, dc), and the commodity (com)
    if (exact || words.every(w => hay.includes(w))) found.push({ r, exact });
  }
  found.sort((a, b) => (b.exact - a.exact) || String(b.r.rcv).localeCompare(String(a.r.rcv)) || b.r.opp.localeCompare(a.r.opp));
  hits = found.slice(0, MAX).map(x => x.r); more = Math.max(found.length - MAX, 0);
  if (at >= hits.length + (more ? 1 : 0)) at = -1;
  if (!found.length) { show(`<div class="gs-note">No case ${sc === "open" ? "open " : sc === "mine" ? "of yours " : ""}matches “${esc(qv.trim())}”.${sc !== "all" ? " Try All cases." : ""}</div>`); return; }
  show(hits.map((r, i) => `<a class="gs-row" role="option" id="gs-o${i}" aria-selected="false" href="#/case?opp=${esc(r.opp)}" data-opp="${esc(r.opp)}">
      <span class="gs-l1"><span class="gs-opp">${esc(r.opp)}</span>${statusChip(r.st, r.legacy)}<span class="gs-req">${esc(r.req || "")}</span><span class="gs-date">${esc(day(r.rcv))}</span></span>
      <span class="gs-l2">${esc([r.lane, r.mode, r.owner || (OPEN(r.st) ? "Not assigned" : "")].filter(Boolean).join(" · "))}</span></a>`).join("")
    + (more ? `<a class="gs-all" role="option" id="gs-o${hits.length}" aria-selected="false" href="#/inquiries" data-all="1">See all ${found.length} in Inquiries</a>` : ""));
}
function show(html) { const pop = $("#gs-pop"); pop.innerHTML = html; pop.hidden = false; $("#gs-q").setAttribute("aria-expanded", "true"); paint(); }
function paint() {
  const q = $("#gs-q"), opts = [...$("#gs-pop").querySelectorAll("[role=option]")];
  opts.forEach((o, i) => { o.setAttribute("aria-selected", String(i === at)); o.classList.toggle("on", i === at); });
  if (at >= 0 && opts[at]) { q.setAttribute("aria-activedescendant", opts[at].id); opts[at].scrollIntoView({ block: "nearest" }); }
  else q.removeAttribute("aria-activedescendant");
}
