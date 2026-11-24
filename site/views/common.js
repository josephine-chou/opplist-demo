// Pieces the pages share: Days waiting badges, chips, the Handled by cell, filter controls.
import { esc, cut, daysSince, day } from "../lib.js";
import { S, link, owners, myOwner, toast, modal } from "../core.js";
import { act } from "../data.js";
import { showProblem } from "../issues.js";

// ---------------------------------------------------------------- filters (also used by the Issues page)
export function select(id, options, value, { minWidth = 140, label = "" } = {}) {
  return `<select id="${id}" style="min-width:${minWidth}px"${label ? ` aria-label="${esc(label)}"` : ""}>${options.map(([v, l]) => `<option value="${esc(v)}"${String(v) === String(value) ? " selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
}
export const grp = (label, inner) => `<label class="fld"><span class="fl">${label}</span>${inner}</label>`;
export const filters = inner => `<div class="bar">${inner}</div>`;
export const statusLine = items => `<div class="countsrow"><div class="counts">${items.filter(Boolean).map(x => `<span>${x}</span>`).join("")}</div></div>`;
// All / Not assigned / Mine: a row of toggle buttons
export function seg(name, items, value) {
  return `<div class="seg" role="group" aria-label="${esc(name)}">${items.map(([v, l]) => `<button type="button" data-seg="${esc(v)}" aria-pressed="${v === value}">${l}</button>`).join("")}</div>`;
}

// ---------------------------------------------------------------- Days waiting: calendar days, three colours
export const tier = n => n >= 7 ? "red" : n >= 3 ? "amber" : "green";
export function daysBadge(iso) {
  const n = daysSince(iso);
  if (n === null) return `<span class="muted">-</span>`;
  const v = Math.max(n, 0);
  return `<span class="days t-${tier(v)}" title="${v} calendar ${v === 1 ? "day" : "days"} since ${esc(day(iso, true))}"><b>${v}</b><span>${v === 1 ? "day" : "days"}</span></span>`;
}
export const legend = () => `<span class="legend">Oldest first · calendar days · <span class="lg t-green">0 to 2</span><span class="lg t-amber">3 to 6</span><span class="lg t-red">7 or more</span></span>`;

// ---------------------------------------------------------------- chips and links
const ST = { New: "new", Quoted: "quoted", Won: "won", Lost: "lost", Dropped: "dropped", Cancelled: "dropped" };
// a legacy row, imported from the old workbook, is stored Dropped and shows Legacy
export const LEGACY_TITLE = "Imported from the old workbook; outcome not recorded";
export const shownStatus = (st, legacy) => legacy && st === "Dropped" ? "Legacy" : st;
export const statusChip = (st, legacy = false) => !st ? ""
  : legacy && st === "Dropped" ? `<span class="chip st-dropped" title="${LEGACY_TITLE}">Legacy</span>`
  : `<span class="chip st-${ST[st] || "new"}">${esc(st)}</span>`;
// OppList's LATAM flag (its countries and requester, or a choice made in OppList); nothing when it is not
export const latamChip = r => r?.latam ? ` <span class="chip c-latam" title="LATAM in OppList">LATAM</span>` : "";
// the requester is an agent with a MFN number in OppList (OppList sends mfn: true)
export const wcaChip = r => r?.mfn ? ` <span class="chip c-mfn" title="The requester is a MFN member (its MFN ID in OppList)">MFN</span>` : "";
export const tagChips = r => latamChip(r) + wcaChip(r);

// Make Outlook drafts, explained (Ask vendors and the quote card); where to set it up comes from OppList's
// Settings (meta/sync.helperFolder), never written in the site's files, which are public
export function helperBox() {
  const folder = String(S.meta?.helperFolder || "").trim();
  modal("Make Outlook drafts", `<p class="m0">A small program on the person's own computer, separate from this site and not part of this demo. It reads what the site copied to the clipboard and turns it into Outlook drafts with the person's own signature: one per vendor for a rate request, one to the requester for a quote. It sends nothing and deletes nothing.</p>
    <p class="m0">${folder ? `An installation says here where to set it up: <b class="mono">${esc(folder)}</b>.` : "Here the site still copies the request and logs the action, so the rest of the page works as it would with the program."}</p>
    <p class="m0 muted small">Each page also lets you copy the mail by hand.</p>`);
}
export const caseLink = (opp, extra = {}) => `<a class="opp" href="${link("case", { opp, ...extra })}">${esc(opp)}</a>`;
// the OPP No is copied on the case page only: its number copies when clicked;
// a list's number just opens the case
export const COPY_ICON = `<svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true"><rect x="5.5" y="5.5" width="8" height="8" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.4"/><path d="M10.5 3.5v-.5a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3v5A1.5 1.5 0 0 0 4 9.5h.5" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>`;
async function copyPlain(text) {
  try { await navigator.clipboard.writeText(text); return true; } catch { /* the old way below */ }
  const ta = document.createElement("textarea"); ta.value = text; ta.setAttribute("readonly", ""); ta.style.cssText = "position:fixed;opacity:0";
  document.body.appendChild(ta); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove(); return ok;
}
// one listener for every page: the case page's number copies the OPP No, in brackets,
// "[OPP-1201]", as it goes in a mail's subject
document.addEventListener("click", async ev => {
  const b = ev.target.closest?.("[data-copy-opp]"); if (!b) return;
  ev.preventDefault(); ev.stopPropagation();
  const opp = `[${b.dataset.copyOpp}]`;
  toast(await copyPlain(opp) ? `Copied ${opp}` : `Could not copy ${opp}. Select it and press Ctrl+C.`);
});
// no "Not synced yet" mark; what someone did shows at once, and the line at the top of the
// page says how fresh the lists are
export const unsynced = () => "";
export const ownerChip = name => name ? `<span class="chip owner">${esc(name)}</span>` : `<span class="muted">Not assigned</span>`;
export const emptyLists = () => `<div class="panel"><div class="empty">No lists yet. They appear after the first update.</div></div>`;

// ---------------------------------------------------------------- Handled by: a chip with Change, or Assign to and Take it
// anyone can give a case to anyone; Take it gives it to yourself.
export function ownerOptions(value, blank = "Assign to…") {
  const list = owners();
  const vals = value && !list.includes(value) ? [value, ...list] : list;
  return `<option value="">${esc(blank)}</option>${vals.map(o => `<option value="${esc(o)}"${o === value ? " selected" : ""}>${esc(o)}</option>`).join("")}`;
}
export function ownerCell(row, { take = true } = {}) {
  const opp = esc(row.opp);
  if (row.owner) {
    return `<span class="hb" data-hb="${opp}">${ownerChip(row.owner)}<button type="button" class="linkbtn small" data-change="${opp}">Change</button></span>`;
  }
  const me = myOwner();
  return `<span class="hb" data-hb="${opp}"><select class="sel-sm" data-assign="${opp}" aria-label="Assign ${opp} to">${ownerOptions("")}</select>${take ? `<button type="button" class="btn small outline" data-take="${opp}"${me ? "" : ' disabled title="Your account is not linked to a Handled by name yet. Ask Iris."'}>Take it</button>` : ""}</span>`;
}
// one listener on the page's root for Take it, Assign to and Change
export function wireOwnerCells(root, findRow) {
  root.addEventListener("click", async ev => {
    const take = ev.target.closest("[data-take]"), ch = ev.target.closest("[data-change]");
    if (take) { take.disabled = true; await assign(take.dataset.take, myOwner(), take); }
    if (ch) {
      const opp = ch.dataset.change, row = findRow(opp), cell = ch.closest(".hb");
      cell.innerHTML = `<select class="sel-sm" data-assign="${esc(opp)}" aria-label="Change who handles ${esc(opp)}">${ownerOptions(row?.owner || "", "Not assigned")}</select><button type="button" class="linkbtn small" data-keep>Keep</button>`;
      cell.querySelector("select").focus();
      cell.querySelector("[data-keep]").onclick = () => { cell.outerHTML = ownerCell(row || { opp, owner: "" }); };
    }
  });
  root.addEventListener("change", async ev => {
    const sel = ev.target.closest("select[data-assign]"); if (!sel) return;
    const opp = sel.dataset.assign, row = findRow(opp);
    if ((row?.owner || "") === sel.value) return;
    sel.disabled = true;
    await assign(opp, sel.value, sel);
  });
}
export async function assign(opp, to, el) {
  try {
    await act("assign", opp, { to });
    toast(to ? `${opp}: handled by ${to}.` : `${opp}: not assigned.`);
  } catch (e) { if (el) el.disabled = false; showProblem(e, { part: opp }); }
}

export const shortWhat = (s, n = 140) => esc(cut(s || "", n));
