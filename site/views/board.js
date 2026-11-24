// My board: what the signed-in person owes an answer, their follow-ups that are due, the actions OppList could not
// take in, and their own notes (with an optional case and a reminder date). Everyone can see notes; a note linked
// to a case also shows on that case. Notes stay on the site: OppList does not take them in.
import { S, $, main, myOwner, isAdmin, pageHead, confirmBox, toast, modal } from "../core.js";
import { esc, day, todayIso, daysSince, plural, stamp, validIso } from "../lib.js";
import { merged, act, addNote, updateNote, actionLabel } from "../data.js";
import { showProblem, failHtml } from "../issues.js";
import { caseLink, unsynced, shortWhat, emptyLists } from "./common.js";

// the follow-up steps that are the person's to do: FU1 and FU2; Decide is the admin's, so only the admin
// sees it on the board
const fuSteps = () => isAdmin() ? ["FU1", "FU2", "Decide"] : ["FU1", "FU2"];
// what My board counts in the side menu
export function boardCount() {
  if (!S.me) return 0;
  const me = myOwner(), today = todayIso();
  let n = S.myNotes.filter(x => !x.done && x.remind && x.remind <= today).length;
  if (me && (S.assign || S.inquiries)) {
    const m = merged();
    n += m.pending.filter(r => r.owner === me).length;
    n += m.rows.filter(r => r.st === "Quoted" && !r.pc && r.owner === me && fuSteps().includes(r.fu)).length;
  }
  return n;
}
const OPP_RE = /^OPP-\d{4,6}$/;
export function normOpp(s) {
  const t = String(s || "").toUpperCase().replace(/\s+/g, "");
  const m = t.match(/^(?:OPP-?)?(\d{1,6})$/);
  return m ? `OPP-${m[1].padStart(4, "0")}` : t;
}

export async function viewBoard() {
  main.innerHTML = `<div class="page" id="pg-board"><div id="head"></div><div class="chips-row" id="sum"></div>
    <div class="cols">
      <div class="col-main" id="left"><div class="muted loading">Loading…</div></div>
      <aside class="col-side" aria-labelledby="h-notes"><section class="panel">
        <div class="panel-head col"><h2 id="h-notes">Notes</h2><span class="muted small">Yours, by reminder date. Everyone can see notes; one linked to a case also shows on that case.</span></div>
        <form class="note-new" id="f-note">
          <label class="field">New note<input type="text" name="text" maxlength="2000" required placeholder="e.g. Call the vendor about their reply"></label>
          <div class="note-row">
            <label class="field">Case<input type="text" name="opp" list="dl-opp" placeholder="No case" autocomplete="off" spellcheck="false"></label>
            <label class="field">Remind me<input type="date" name="remind"></label>
            <button type="submit" class="btn primary">Add</button>
          </div>
          <datalist id="dl-opp"></datalist>
          <div class="err" id="note-err"></div>
        </form>
        <div id="notes"></div>
      </section></aside>
    </div></div>`;
  const root = $("#pg-board");
  let showDone = false;

  $("#f-note").onsubmit = async ev => {
    ev.preventDefault();
    const f = ev.target, err = $("#note-err"); err.textContent = "";
    const text = f.text.value.trim(), opp = f.opp.value.trim() ? normOpp(f.opp.value) : "", remind = f.remind.value;
    try {
      if (!text) throw new Error("Write the note first.");
      if (opp && !OPP_RE.test(opp)) throw new Error("A case number looks like OPP-1201.");
      if (opp && S.inquiries && !merged().byOpp.has(opp)) throw new Error(`${opp} is not in the lists.`);
      if (remind && !validIso(remind)) throw new Error("Pick the reminder date from the calendar.");
      const btn = f.querySelector("button[type=submit]"); btn.disabled = true;
      try { await addNote({ text, opp, remind }); } finally { btn.disabled = false; }
      f.reset(); toast("Note added.");
    } catch (e) { err.innerHTML = failHtml(e, { part: "note" }); }
  };
  root.addEventListener("click", async ev => {
    const b = ev.target.closest("[data-answered]");
    if (b) {
      const opp = b.dataset.answered;
      if (!await confirmBox("Answered", `Mark ${opp} as answered? It leaves Pending on us, and its follow-ups start again from today.`, "Answered")) return;
      b.disabled = true;
      try { await act("answered", opp, { date: todayIso(), note: "" }); toast(`${opp}: answered.`); } catch (e) { b.disabled = false; showProblem(e, { part: opp }); }
      return;
    }
    if (ev.target.closest("#b-done")) { showDone = !showDone; drawNotes(); return; }
    const ed = ev.target.closest("[data-edit]"); if (ed) editNote(S.myNotes.find(n => n.id === ed.dataset.edit));
  });
  root.addEventListener("change", async ev => {
    const cb = ev.target.closest("input[data-done]"); if (!cb) return;
    cb.disabled = true;
    try { await updateNote(cb.dataset.done, { done: cb.checked }); } catch (e) { cb.checked = !cb.checked; showProblem(e, { part: "note" }); } finally { cb.disabled = false; }
  });

  function draw() {
    const me = myOwner(), today = todayIso();
    $("#head").innerHTML = pageHead("My board", "What you owe an answer, what is due, and your own to-do list.");
    const left = $("#left");
    const notesDue = S.myNotes.filter(x => !x.done && x.remind && x.remind <= today).length;
    if (S.assign === undefined && S.inquiries === undefined) { drawNotes(); return; }
    const m = merged();
    const owe = me ? m.pending.filter(r => r.owner === me) : [];
    const fus = me ? m.rows.filter(r => r.st === "Quoted" && !r.pc && r.owner === me && fuSteps().includes(r.fu)).sort((a, b) => String(a.since).localeCompare(String(b.since))) : [];
    $("#sum").innerHTML = [`<span class="chip big ${owe.length ? "c-owe" : ""}">${plural(owe.length, "question")} to answer</span>`,
      `<span class="chip big ${fus.length ? "c-blue" : ""}">${plural(fus.length, "follow-up")} due</span>`,
      `<span class="chip big ${notesDue ? "c-blue" : ""}">${notesDue ? `${plural(notesDue, "note")} for today` : "No notes for today"}</span>`].join("");
    const dl = $("#dl-opp");
    if (dl && !dl.childElementCount) dl.innerHTML = m.rows.filter(r => (r.st === "New" || r.st === "Quoted") && !r.pc).map(r => `<option value="${esc(r.opp)}">${esc(r.req)} · ${esc(r.lane)}</option>`).join("");
    const noLink = !me ? `<p class="hint">Your account is not linked to a Handled by name yet, so nothing here is yours. Ask Iris to link it.</p>` : "";
    const fails = S.myFails.length ? `<section class="panel warnbox" aria-labelledby="h-fail"><div class="panel-head"><h2 id="h-fail">Not accepted <span class="muted">${S.myFails.length}</span></h2></div>
      ${S.myFails.map(f => `<div class="li"><div>${f.opp ? caseLink(f.opp) : ""} ${esc(actionLabel(f.type))}: ${esc(f.msg || "This could not be recorded.")}</div><div class="muted small">${esc(stamp(f.at))} · tell Iris if it matters</div></div>`).join("")}</section>` : "";
    left.innerHTML = noLink + fails + (S.assign === null && S.inquiries === null ? emptyLists() : `
      <section class="panel" aria-labelledby="h-owe">
        <div class="panel-head"><h2 id="h-owe">Owe an answer <span class="muted">${owe.length} · Pending on us where you answer</span></h2></div>
        ${owe.length ? owe.map(o => {
          const r = m.byOpp.get(o.opp) || {};
          return `<div class="li owe"><div class="li-main"><div class="li-top">${caseLink(o.opp)}<span>${esc(o.req)}</span><span class="muted">${esc(r.lane || "")}${r.mode ? ` · ${esc(r.mode)}` : ""}</span>${unsynced(o)}</div>
            <div class="said">“${shortWhat(o.what, 220)}”</div>
            <div class="muted small">Asked ${esc(day(o.on))} · <b>${plural(Math.max(daysSince(o.on) ?? 0, 0), "day")}</b> · logged by ${esc(o.by || "?")}</div></div>
            <button type="button" class="btn" data-answered="${esc(o.opp)}">Answered</button></div>`;
        }).join("") : `<div class="empty">Nothing to answer.</div>`}
      </section>
      <section class="panel" aria-labelledby="h-fu">
        <div class="panel-head"><h2 id="h-fu">Follow-ups due <span class="muted">${fus.length} · your cases</span></h2><a href="#/followups?who=mine" class="small">All follow-ups</a></div>
        ${fus.length ? `<div class="tablewrap"><table class="grid t-fu">
          <thead><tr><th>Step</th><th>OPP No</th><th>Requester</th><th>Lane</th><th>Since</th><th>Days</th><th></th></tr></thead>
          <tbody>${fus.map(r => `<tr><td><span class="chip ${r.fu === "Decide" ? "st-lost" : "st-quoted"}">${esc(r.fu === "Decide" ? "Decide" : `${r.fu} due`)}</span></td><td>${caseLink(r.opp)}${unsynced(r)}</td><td>${esc(r.req)}</td>
            <td>${esc(r.lane)} · ${esc(r.mode)}</td><td class="nw">${esc(day(r.since))}</td><td>${daysSince(r.since) ?? ""}</td>
            <td class="nw">${r.fu === "Decide" ? `<span class="muted small">${isAdmin() ? "Decide in OppList" : "Iris decides"}</span>` : `<a href="#/case?opp=${esc(r.opp)}&do=fu">Log follow-up</a>`}</td></tr>`).join("")}</tbody></table></div>`
          : `<div class="empty">No follow-ups due on your cases.</div>`}
      </section>`);
    drawNotes();
  }
  function drawNotes() {
    const today = todayIso();
    const open = S.myNotes.filter(n => !n.done).sort((a, b) => (a.remind || "9999").localeCompare(b.remind || "9999") || (ms(b.at) - ms(a.at)));
    const done = S.myNotes.filter(n => n.done).sort((a, b) => ms(b.updatedAt) - ms(a.updatedAt));
    const one = n => `<div class="note${n.done ? " done" : ""}"><input type="checkbox" data-done="${esc(n.id)}" ${n.done ? "checked" : ""} aria-label="Done: ${esc(n.text)}">
      <div class="note-main"><div class="note-text">${esc(n.text)}</div>
        <div class="muted small">${n.remind ? (n.remind <= today && !n.done ? `<span class="chip ${n.remind < today ? "c-red" : "c-amber"}">${n.remind < today ? `Due ${esc(day(n.remind))}` : "Today"}</span>` : esc(day(n.remind))) : "No reminder"}${n.opp ? ` · ${caseLink(n.opp)}` : ""} · <button type="button" class="linkbtn small" data-edit="${esc(n.id)}">Edit</button></div></div></div>`;
    $("#notes").innerHTML = (open.length ? open.map(one).join("") : `<div class="empty">No open notes.</div>`)
      + (done.length ? `<div class="done-row"><button type="button" class="linkbtn small" id="b-done" aria-expanded="${showDone}">${showDone ? "Hide" : "Show"} done (${done.length})</button></div>${showDone ? done.map(one).join("") : ""}` : "");
  }
  draw();
  // the note form is not redrawn, so a note being typed is never lost
  return { refresh() { draw(); } };
}
const ms = v => v?.toMillis ? v.toMillis() : v instanceof Date ? v.getTime() : 0;

function editNote(n) {
  if (!n) return;
  const bg = modal("Edit note", `<form id="f-edit">
      <label class="field">Note<textarea name="text" rows="3" maxlength="2000" required>${esc(n.text)}</textarea></label>
      <label class="field">Remind me<input type="date" name="remind" value="${esc(n.remind || "")}"></label>
      ${n.opp ? `<p class="muted small m0">Linked to ${esc(n.opp)}. A note keeps its case.</p>` : ""}
      <div class="err" id="ed-err"></div></form>`,
    { buttons: `<button type="button" class="btn" data-close>Cancel</button><button type="submit" form="f-edit" class="btn primary">Save</button>` });
  bg.querySelector("#f-edit").onsubmit = async ev => {
    ev.preventDefault(); const f = ev.target, text = f.text.value.trim();
    if (!text) { bg.querySelector("#ed-err").textContent = "The note cannot be empty."; return; }
    try { await updateNote(n.id, { text, remind: f.remind.value || "" }); bg.close(); toast("Note saved."); }
    catch (e) { bg.querySelector("#ed-err").innerHTML = failHtml(e, { part: "note" }); }
  };
}
