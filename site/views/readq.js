// Read quote from the site: a drawer from the right, like the rates drawer, opened from a vendor's Rate requests
// row. The vendor's reply goes in as their mail dragged in from Outlook (a .msg, or an .eml), as the files they sent
// (PDF, pictures, Excel, Word, text), or as a pasted message, with the day they sent it and, on a case with several
// routes, the route. Submit sends the files up in parts (uploads/) and writes the read action; Claude reads it at the
// next update, and the row shows "Read from their mail: check" when the draft is ready. The words on the site never
// name the computer it is read on: "Submit", "the next update".
import { S, confirmBox } from "../core.js";
import { esc, todayIso, validIso, asDate, nowMs } from "../lib.js";
import { readQuote } from "../data.js";
import { failHtml } from "../issues.js";
import * as Q from "../quotemail.js";
import * as R from "../ratelib.js";
import { letter } from "../editlib.js";
import { canRates } from "./rates.js";

const ENVELOPE = s => `<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/></svg>`;
const ACCEPT = ".msg,.eml,.pdf,.png,.jpg,.jpeg,.gif,.webp,.xlsx,.xls,.csv,.docx,.txt";
const LIST_LINE = "This is the line from Outlook's message list, not the mail. Open the mail, click in its text, press Ctrl+A and Ctrl+C, and paste again.";
const hhmm = v => asDate(v)?.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" }) || "";

export function readPanel(opp, cur, { onDone } = {}) {
  let open = false, dr = null, scrim = null, vendor = "", route = null, files = [], text = "", sent = "", err = "", busy = false, step = "", done = null;
  const q = id => dr?.querySelector(`#${id}`) || null;
  // a file dropped outside the drawer would make the browser open it and leave the site
  const guard = ev => { if (ev.dataTransfer?.types?.includes("Files")) ev.preventDefault(); };

  // ------------------------------------------------------------------ opening and closing
  function show(v, { route: rt } = {}) {
    hide();
    const c = cur(); if (!c || !v || !canRates(c)) return;
    vendor = v; route = R.routesOf(c).length > 1 ? (typeof rt === "number" ? rt : null) : null;
    files = []; text = ""; sent = todayIso(); err = ""; busy = false; step = ""; done = null; open = true;
    scrim = document.createElement("div"); scrim.className = "ask-scrim";
    dr = document.createElement("aside"); dr.className = "ask-dr rq-dr";
    dr.setAttribute("role", "dialog"); dr.setAttribute("aria-modal", "false"); dr.setAttribute("aria-labelledby", "rq-title");
    dr.innerHTML = `<div class="dr-head"><div class="t"><h2 id="rq-title" tabindex="-1"></h2><div class="dr-sub"></div></div>
        <button type="button" class="xbtn" data-rq-x aria-label="Close">&times;</button></div>
      <div class="dr-body"></div>
      <div class="dr-foot"></div>
      <input type="file" id="rq-file" multiple accept="${ACCEPT}" hidden>`;
    document.body.append(scrim, dr);
    wire(); paint();
    requestAnimationFrame(() => requestAnimationFrame(() => dr?.classList.add("open")));
    document.addEventListener("keydown", onKey, true);
    document.addEventListener("dragover", guard); document.addEventListener("drop", guard);
    setTimeout(() => q("rq-title")?.focus(), 260);
  }
  function hide() {
    if (!open) return;
    open = false;
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("dragover", guard); document.removeEventListener("drop", guard);
    dr?.remove(); scrim?.remove(); dr = scrim = null; files = []; text = ""; done = null;
  }
  async function close(force = false) {
    if (busy && !force) return;
    if (!force && !done && (files.length || text.trim()) && !await confirmBox("Close Read quote", "Close without submitting? The files and the text here are not kept.", "Close")) return;
    hide(); onDone?.("closed");
  }
  function onKey(ev) {
    if (ev.key !== "Escape" || !open || document.querySelector(".modal")) return;
    // stopped here: the confirm box close() may open listens for Escape too, and would take this one and close at once
    ev.preventDefault(); ev.stopPropagation(); close();
  }

  // ------------------------------------------------------------------ drawing
  function paint() {
    if (!dr) return;
    const c = cur() || {}, rs = R.routesOf(c), rt = rs.length > 1 ? R.routeOf(c, route) : rs[0] || null;
    q("rq-title").textContent = `Read quote: ${vendor}`;
    const ends = rt && (rt.from || rt.to) ? `${Q.placeName(rt.from)} to ${Q.placeName(rt.to)}` : c.lane || "";
    dr.querySelector(".dr-sub").textContent = [opp, rt ? `route ${letter(rt.n)}` : "", rt?.mode || c.mode || "", ends, Q.cargoPhrase(c), rt?.incoterm || Q.incotermOf(c)].filter(Boolean).join(" · ");
    dr.querySelector(".dr-body").innerHTML = bodyHtml(c, rs);
    paintFoot();
  }
  const intro = `<p class="muted small m0">Drag their reply in from Outlook. Its rates are put on this row as a draft. Nothing is kept as their rates until someone checks each line against the mail and saves it.</p>`;
  function bodyHtml(c, rs) {
    if (done) {
      return `${intro}<div class="rq-sent" role="status"><b>Submitted at ${esc(hhmm(done))}.</b><span>${R.readerOn(S.meta?.readQuote)
        ? "It is usually read within a minute or two, and the row shows “Read from their mail: check” when the draft is ready."
        : "Reading is paused right now: it is read at the next update, and the row shows “Read from their mail: check” when the draft is ready."} You can close this.</span></div>`;
    }
    const kind = R.readKind(files), dis = busy ? " disabled" : "";
    const drop = files.length
      ? `<ul class="rq-files">${files.map((f, i) => { const k = R.fileKind(f.name); return `<li><span class="kind k-${k.toLowerCase()}">${k}</span><span class="nm" title="${esc(f.name)}">${esc(f.name)}</span><span class="sz">${esc(R.sizeText(f.size))}</span>${busy ? "" : `<button type="button" class="x" data-rq-rm="${i}" aria-label="Remove ${esc(f.name)}">&times;</button>`}</li>`; }).join("")}</ul>
        ${busy || files.length >= R.READ_FILES ? "" : `<div class="rq-drop slim" id="rq-drop">${ENVELOPE(18)}<span class="sm">Drop more here, or <button type="button" class="linkbtn" data-rq-pick>choose files</button></span></div>`}`
      : `<div class="rq-drop" id="rq-drop">${ENVELOPE(34)}<div class="big">Drop their mail here, straight from Outlook</div>
          <div class="sm">or the PDFs, pictures or Excel files they sent · up to ${R.READ_FILES} files, 15 MB in all · <button type="button" class="linkbtn" data-rq-pick>Choose files</button></div></div>`;
    const lbl = files.length ? "A note for the reading (optional)" : "Or paste their message (WhatsApp, a chat)";
    const ph = files.length ? "e.g. use the CMA CGM option only" : "Paste the message as they wrote it";
    const routeSel = rs.length > 1 ? `<label>Route <select id="rq-route"${dis}><option value=""${route === null ? " selected" : ""}>Any route the mail names</option>${rs.map(r => `<option value="${r.n}"${r.n === route ? " selected" : ""}>${esc(`${letter(r.n)} · ${[r.mode, `${Q.placeName(r.from)} to ${Q.placeName(r.to)}`].filter(Boolean).join(" · ")}`)}</option>`).join("")}</select></label>` : "";
    return `${intro}${drop}
      <label class="rq-lbl">${lbl}<textarea id="rq-text" maxlength="${R.READ_TEXT}" placeholder="${esc(ph)}"${dis}>${esc(text)}</textarea></label>
      <div class="rq-row">${routeSel}<label>They sent it on <input type="date" id="rq-sent" value="${esc(sent)}" max="${todayIso()}"${dis}></label>
        <span class="muted small">${kind === "mail" ? "For a mail, the mail's own date is used." : ""}</span></div>
      ${err ? `<div class="rq-err" role="alert">${err}</div>` : ""}`;
  }
  function paintFoot() {
    const foot = dr.querySelector(".dr-foot");
    if (done) { foot.innerHTML = `<span class="grow"></span><button type="button" class="btn primary" data-rq-x>Close</button>`; return; }
    const ready = files.length > 0 || text.trim().length > 0;
    foot.innerHTML = `<span class="info" id="rq-step">${esc(busy ? step || "Sending…" : "Usually read within a minute or two.")}</span><span class="grow"></span>`
      + `<button type="button" class="btn" data-rq-x${busy ? " disabled" : ""}>Cancel</button><button type="button" class="btn primary" id="rq-go"${ready && !busy ? "" : " disabled"}>Submit</button>`;
  }
  const keepFocus = fn => { const id = document.activeElement?.id; fn(); if (id) q(id)?.focus(); };

  // ------------------------------------------------------------------ files and the text
  function add(list) {
    if (busy || done) return;
    const got = R.addFiles(files, [...(list || [])]);
    files = got.files; err = got.why.map(esc).join("<br>");
    paint();
  }

  // ------------------------------------------------------------------ submitting
  async function submit() {
    if (busy || done) return;
    const c = cur(), today = todayIso();
    if (!c || !canRates(c)) { err = "This case cannot take rates now."; paint(); return; }
    if (!files.length && !text.trim()) { err = "Drop their mail or the files they sent, or paste their message."; paint(); return; }
    if (!files.length && R.outlookListLine(text)) { err = esc(LIST_LINE); paint(); return; }
    if (!validIso(sent) || sent > today) { err = "The day they sent it: a day up to today."; paint(); return; }
    busy = true; err = ""; step = files.length ? "Sending the files…" : "Sending…"; paint();
    try {
      await readQuote(opp, { vendor, route, kind: R.readKind(files), text: text.trim(), sent }, files, (k, n) => {
        step = `Sending the files: ${k} of ${n} parts`; const s = q("rq-step"); if (s) s.textContent = step;
      });
      busy = false; done = new Date(nowMs()); paint(); onDone?.("submitted");
    } catch (e) { busy = false; err = failHtml(e, { part: `${opp} Read quote` }); paint(); }
  }

  // ------------------------------------------------------------------ events
  function wire() {
    dr.addEventListener("click", ev => {
      if (ev.target.closest("[data-rq-x]")) { close(); return; }
      if (ev.target.closest("[data-rq-pick]")) { q("rq-file")?.click(); return; }
      const rm = ev.target.closest("[data-rq-rm]");
      if (rm && !busy) { files.splice(Number(rm.dataset.rqRm), 1); err = ""; paint(); return; }
      if (ev.target.closest("#rq-go")) submit();
    });
    scrim.addEventListener("click", () => close());
    dr.addEventListener("change", ev => {
      const el = ev.target;
      if (el.id === "rq-file") { add(el.files); el.value = ""; return; }
      if (el.id === "rq-sent") { sent = el.value; return; }
      if (el.id === "rq-route") { route = el.value === "" ? null : Number(el.value); keepFocus(paint); }
    });
    dr.addEventListener("input", ev => {
      if (ev.target.id !== "rq-text") return;
      const had = text.trim().length > 0; text = ev.target.value;
      if (had !== text.trim().length > 0) paintFoot();
    });
    // their mail dragged in from Outlook arrives as a file (.msg), as any file does
    dr.addEventListener("dragover", ev => { if (!ev.dataTransfer?.types?.includes("Files") || busy || done) return; ev.preventDefault(); q("rq-drop")?.classList.add("over"); });
    dr.addEventListener("dragleave", ev => { if (!dr.contains(ev.relatedTarget)) q("rq-drop")?.classList.remove("over"); });
    dr.addEventListener("drop", ev => { if (!ev.dataTransfer?.files?.length) return; ev.preventDefault(); q("rq-drop")?.classList.remove("over"); add(ev.dataTransfer.files); });
  }

  return { show, leave: hide, isOpen: () => open };
}
