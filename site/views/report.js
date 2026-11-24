// Monthly report: the report OppList exports each month, shown as it is (the same page as the PDF; nothing is
// re-laid out here). It is kept in the store behind its rules (reports/{yyyy-mm}), never as a public site file;
// only the admin and the people ticked below can open it.
// Last month is also here live until the administrator exports it ("(live)" in the list).
import { doc, updateDoc, serverTimestamp } from "../store/firestore.js";
import { S, $, main, db, isAdmin, pageHead, go, toast } from "../core.js";
import { esc, monthName, stamp } from "../lib.js";
import { getReport, listUsers, whenLoaded } from "../data.js";
import { problemHtml, showProblem } from "../issues.js";
import { select } from "./common.js";

export async function viewReport(p, alive) {
  if (S.meta === undefined) { main.innerHTML = `<div class="page"><div class="muted loading">Loading…</div></div>`; await whenLoaded("meta"); if (!alive()) return null; }
  const list =(S.meta?.reports || []).slice().sort((a, b) => String(b.month).localeCompare(String(a.month)));
  const month = list.some(r => r.month === p.m) ? p.m : list[0]?.month || "";
  const right = list.length ? `<label class="fld"><span class="fl">Month</span>${select("r-month", list.map(r => [r.month, monthName(r.month) + (r.live ? " (live)" : "")]), month, { minWidth: 170 })}</label>
      <button type="button" class="btn primary" id="r-pdf" disabled>Download PDF</button>` : "";
  main.innerHTML = `<div class="page" id="pg-report">${pageHead("Monthly report", "The monthly report, the same as the PDF.", right)}
    <div id="r-who"></div><div id="r-body">${list.length ? '<div class="muted loading">Loading the report…</div>' : '<div class="panel"><div class="empty">No report yet. Last month comes up here with the next update.</div></div>'}</div></div>`;
  if ($("#r-month")) $("#r-month").onchange = ev => go("report", { m: ev.target.value });
  if (isAdmin()) whoCanOpen(alive);
  if (!month) return null;
  let rep;
  try { rep = await getReport(month); }
  catch (e) { if (alive()) $("#r-body").innerHTML = `<div class="note-box">Could not load the report: ${problemHtml(e, { part: month })}</div>`; return null; }
  if (!alive()) return null;
  if (!rep?.html) { $("#r-body").innerHTML = `<div class="panel"><div class="empty">The ${esc(monthName(month))} report is listed but not here yet. It comes with the next update.</div></div>`; return null; }
  $("#r-body").innerHTML = `<section class="panel" aria-label="${esc(rep.title || monthName(month))}">
      <div class="panel-head"><h2>${esc(rep.title || monthName(month))}</h2><span class="muted small">${rep.live ? `Live, not exported yet · updated ${esc(stamp(rep.updatedAt))}` : `Exported ${esc(stamp(rep.exportedAt))}`}</span></div>
      <div class="report-wrap"><iframe id="r-frame" title="${esc(rep.title || "Monthly report")}" sandbox="allow-same-origin allow-modals"></iframe></div></section>`;
  const fr = $("#r-frame");
  fr.addEventListener("load", () => {
    try {
      const d = fr.contentDocument;
      const fit = () => { fr.style.height = `${d.documentElement.scrollHeight + 4}px`; fr.style.minWidth = `${d.documentElement.scrollWidth}px`; };
      fit(); setTimeout(fit, 400);   // after the web fonts have come in
      $("#r-pdf").disabled = false;
    } catch (e) { console.warn(e); }
  });
  fr.srcdoc = rep.html;
  $("#r-pdf").onclick = () => {
    try { fr.contentWindow.focus(); fr.contentWindow.print(); toast("In the print box, choose Save as PDF.", 4000); }
    catch (e) { showProblem(e, { part: `${month} PDF` }); }
  };
  return null;
}

async function whoCanOpen(alive) {
  const box = $("#r-who");
  let users;
  try { users = await listUsers(); } catch (e) { if (alive()) box.innerHTML = `<div class="note-box">${problemHtml(e)}</div>`; return; }
  if (!alive()) return;
  const people = users.filter(u => u.role !== "admin").sort((a, b) => String(a.name || a.email).localeCompare(String(b.name || b.email)));
  const draw = () => {
    const n = people.filter(u => u.report && u.active !== false).length + 1;
    box.innerHTML = `<section class="panel" aria-labelledby="h-who"><div class="panel-head"><h2 id="h-who">Who can open Monthly report <span class="muted">· only you see this box</span></h2><span class="muted small">${n} ${n === 1 ? "person" : "people"}</span></div>
      <div class="who-list"><label class="chk"><input type="checkbox" checked disabled> ${esc(S.me.name || S.me.email)} · Admin, always</label>
      ${people.map(u => `<label class="chk${u.active === false ? " off" : ""}"><input type="checkbox" data-u="${esc(u.email)}" ${u.report ? "checked" : ""}> ${esc(u.name || u.email)}${u.active === false ? " (switched off)" : ""}</label>`).join("")}</div>
      <p class="hint small">People without a tick do not see Monthly report in their menu, and the database will not hand them the report even if they type the address.</p></section>`;
  };
  draw();
  box.addEventListener("change", async ev => {
    const cb = ev.target.closest("input[data-u]"); if (!cb) return;
    const u = people.find(x => x.email === cb.dataset.u); cb.disabled = true;
    try { await updateDoc(doc(db, "users", u.email), { report: cb.checked, updatedAt: serverTimestamp(), updatedBy: S.me.email }); u.report = cb.checked; toast(`${u.name || u.email}: ${cb.checked ? "can open" : "cannot open"} Monthly report.`); draw(); }
    catch (e) { cb.checked = !cb.checked; cb.disabled = false; showProblem(e, { part: "report access" }); }
  });
}
